"""Флуд — отказ аккаунту, а не цели: цель не теряется, пауза записывается.

ЧТО БЫЛО СЛОМАНО.

1. Цель, на которой пришёл флуд, считалась «попробованной». Движок писал её в
   ошибки (failed += 1), исполнитель срезал batch[:ok+failed] — и цель уходила в
   дедуп как обработанная, а в очередь не возвращалась. Telegram её при этом не
   добавлял и не отклонял. Каждый флуд стоил одного человека из аудитории, а
   пакетный флуд (INVITE_BULK_API) — всего батча. Короткую паузу движок
   пережидал, но цель, на которой она пришла, тоже списывал в ошибки.

2. Короткие паузы, пережитые на месте, не попадали в пульс аккаунта
   (flood_engine): адаптивный темп на них не учился.

3. Финальные шаги прогона гоняли промоутера под флудом. Промоут-трюк шёл
   промоутером, даже если тот в этом же прогоне словил PeerFlood, — до 400
   EditAdmin подряд аккаунтом под ограничением. Ссылку в ЛС всем недостижимым
   слал ОДИН аккаунт одним вызовом с потолком 180с: на 200 целях вызов
   обрывался примерно на сороковом сообщении, итог показывал «доставлено 0», а
   флуд отправителя не записывался.
"""
from __future__ import annotations

import asyncio

import pytest
from telethon.errors import FloodWaitError, PeerFloodError  # стаб-классы из conftest

from services import account_manager
from services import mass_inviter_engine as mie
from services import op_worker
from tests.test_invite_queue_scheduler import (  # noqa: F401 — фикстуры стенда
    _Pool,
    _reset_account_claims,
    _run,
    stand,
)


def _flood(seconds: int) -> FloodWaitError:
    e = FloodWaitError("flood")
    e.seconds = seconds
    return e


# ── движок: кого НЕ пробовали, говорит поимённо ──────────────────────────────

class _Client:
    """InviteToChannel падает заданной ошибкой на заданной цели (по очереди)."""

    def __init__(self, raise_on: dict):
        self.raise_on = {k: list(v) for k, v in raise_on.items()}
        self.invited: list = []
        self._last = None

    async def get_entity(self, ref):
        self._last = ref
        return ref

    async def __call__(self, request):
        q = self.raise_on.get(self._last)
        if q:
            raise q.pop(0)
        self.invited.append(self._last)

        class _R:
            missing_invitees: list = []
        return _R()

    async def disconnect(self):
        return None


@pytest.fixture
def engine(monkeypatch):
    def _install(client):
        async def _connect(*a, **k):
            return client

        async def _group(c, ref, acc_id=None):
            return "GRP"

        async def _fast(*a, **k):
            return None

        monkeypatch.setattr(account_manager, "connect_client", _connect)
        monkeypatch.setattr(mie, "_resolve_group_entity", _group)
        monkeypatch.setattr(asyncio, "sleep", _fast)
        return client
    return _install


@pytest.mark.asyncio
async def test_long_flood_target_is_untried_not_failed(engine):
    c = engine(_Client({"@b": [_flood(3600)]}))
    res = await mie.invite_batch("s", {"id": 1}, "@g", ["@a", "@b", "@c"], bulk=False)
    assert res["ok"] == 1
    assert res["failed"] == 0, "флуд — отказ аккаунту, цель не провалена"
    assert res["flood_wait"] == 3600
    assert res["untried"] == ["@b", "@c"], "цель, на которой пришёл флуд, не пробовали"


@pytest.mark.asyncio
async def test_peer_flood_returns_whole_tail(engine):
    engine(_Client({"@a": [PeerFloodError("peer")]}))
    res = await mie.invite_batch("s", {"id": 1}, "@g", ["@a", "@b"], bulk=False)
    assert res["peer_flood"] is True
    assert res["failed"] == 0
    assert res["untried"] == ["@a", "@b"]


@pytest.mark.asyncio
async def test_short_flood_retries_same_target_and_reports_pause(engine):
    c = engine(_Client({"@b": [_flood(7)]}))
    res = await mie.invite_batch("s", {"id": 1}, "@g", ["@a", "@b", "@c"], bulk=False)
    assert res["ok"] == 3, "после короткой паузы та же цель приглашается повторно"
    assert c.invited == ["@a", "@b", "@c"]
    assert res["failed"] == 0
    assert res["untried"] == []
    assert res["short_floods"] == [7], "пережитая пауза уходит исполнителю для пульса"


@pytest.mark.asyncio
async def test_connect_failure_leaves_every_target_untried(monkeypatch):
    async def _boom(*a, **k):
        raise ConnectionError("нет сети")
    monkeypatch.setattr(account_manager, "connect_client", _boom)
    res = await mie.invite_batch("s", {"id": 1}, "@g", ["@a", "@b"], bulk=False)
    assert res["untried"] == ["@a", "@b"]


@pytest.mark.asyncio
async def test_bulk_flood_does_not_burn_the_batch(engine):
    """Пакетный флуд: раньше failed = весь пакет — пятеро в дедуп разом."""
    class _Bulk(_Client):
        async def __call__(self, request):
            raise PeerFloodError("peer")
    engine(_Bulk({}))
    res = await mie.invite_batch("s", {"id": 1}, "@g", ["@a", "@b", "@c"], bulk=True)
    assert res["peer_flood"] is True
    assert res["failed"] == 0
    assert res["untried"] == ["@a", "@b", "@c"]


@pytest.mark.asyncio
async def test_link_dm_flood_returns_untried_and_short_pause_retries(monkeypatch):
    sent: list = []
    answers = {"u2": [{"flood_wait": 5}], "u3": [{"peer_flood": True}]}

    async def _dm(sess, ref, text, _acc=None):
        q = answers.get(ref)
        if q:
            return q.pop(0)
        sent.append(ref)
        return {"ok": True}

    async def _fast(*a, **k):
        return None

    monkeypatch.setattr(account_manager, "send_dm", _dm)
    monkeypatch.setattr(asyncio, "sleep", _fast)
    res = await mie.invite_via_link_batch("s", {"id": 1}, "https://t.me/+x",
                                          ["u1", "u2", "u3", "u4"])
    assert sent == ["u1", "u2"], "после короткой паузы ссылка уходит тому же человеку"
    assert res["ok"] == 2
    assert res["failed"] == 0
    assert res["peer_flood"] is True
    assert res["untried"] == ["u3", "u4"]
    assert res["short_floods"] == [5]


# ── исполнитель: цель флуда возвращается в очередь, пауза — в пульс ───────────

def test_untried_targets_go_back_to_queue_not_to_dedup(stand, monkeypatch):
    """Пакет: u2 не резолвится (провал), остальные не тронуты из-за флуда.

    Срез batch[:ok+failed] засчитал бы «попробованной» u1 — она ушла бы в
    дедуп и больше не приглашалась никогда, а u2 пошла бы на второй круг.
    """
    recorded: list = []

    async def _remember(pool, owner_id, group_key, op_id, targets):
        recorded.extend(str(t) for t in targets)
        return True

    monkeypatch.setattr(op_worker, "_record_invited_targets", _remember)

    def responder(acc_id, refs, dry=False):
        if acc_id == 1:
            return {"ok": 0, "failed": 1, "errors": ["u2: нет в Telegram"],
                    "peer_flood": True,
                    "untried": [r for r in refs if r != "u2"]}
        return {"ok": len(refs), "failed": 0, "errors": [], "untried": []}

    s = stand(responder)
    res = _run(_Pool(), ["u1", "u2", "u3", "u4", "u5"])

    acc2_refs = [r for acc, refs in s.calls if acc == 2 for r in refs]
    assert set(acc2_refs) == {"u1", "u3", "u4", "u5"}, (
        "цели, до которых флуд не дал дойти, обязан подобрать сосед")
    assert "u2" not in acc2_refs, "провал по самой цели повторно не гоняем"
    assert res["ok"] == 4


def test_short_floods_are_written_to_account_pulse(stand, monkeypatch):
    from services import flood_engine as fe
    seen: list = []

    async def _rec(pool, acc_id, wait_seconds=0, action_type="", operation_id=None):
        seen.append((int(acc_id), int(wait_seconds), action_type))

    def responder(acc_id, refs, dry=False):
        return {"ok": len(refs), "failed": 0, "errors": [], "untried": [],
                "short_floods": [9] if acc_id == 1 else []}

    stand(responder)
    monkeypatch.setattr(fe, "record_flood", _rec)
    _run(_Pool(), ["u1", "u2"], account_ids=[1])
    assert (1, 9, "invite") in seen


# ── финальные шаги: промоутер под флудом не работает, ссылки по флоту ─────────

@pytest.fixture
def promoter_stand(stand, monkeypatch):
    """Аккаунт 1 — промоутер (создатель чата), 2 и 3 — инвайтеры."""
    accs = [{"id": i, "session_str": f"s{i}", "proxy_url": None} for i in (1, 2, 3)]

    def _install(responder, link_answer=None):
        s = stand(responder)
        s.trick_calls = []
        s.link_calls = []

        async def _sel_all(pool, owner_id, **k):
            return accs
        monkeypatch.setattr(op_worker.resource_selector, "select_all_active", _sel_all)

        async def _fetch(pool, q, *a):
            if "tg_user_id FROM tg_accounts" in q:
                return [{"id": a_["id"], "tg_user_id": 100 + a_["id"]} for a_ in accs]
            return []
        monkeypatch.setattr(op_worker, "_safe_fetch", _fetch)

        async def _status(sess, acc, group):
            return {"ok": True, "can_promote": int(acc["id"]) == 1}
        monkeypatch.setattr(mie, "channel_admin_status", _status)

        async def _promote(*a, **k):
            return True, ""
        monkeypatch.setattr(account_manager, "promote_to_admin_ex", _promote)

        async def _noop(*a, **k):
            return True
        monkeypatch.setattr(account_manager, "join_channel", _noop)
        monkeypatch.setattr(account_manager, "leave_channel", _noop)
        monkeypatch.setattr(account_manager, "demote_from_admin", _noop)

        async def _demote_batch(sess, group, uids, _acc=None):
            return {u: True for u in uids}
        monkeypatch.setattr(account_manager, "demote_from_admin_batch", _demote_batch)

        async def _link(*a, **k):
            return "https://t.me/+x"
        monkeypatch.setattr(mie, "export_group_invite_link", _link)

        async def _trick(sess, acc, group, refs):
            s.trick_calls.append((int(acc["id"]), list(refs)))
            return {"ok": 0, "failed": len(refs), "errors": [],
                    "still_blocked": list(refs), "untried": []}
        monkeypatch.setattr(mie, "add_via_promote", _trick)

        async def _via_link(sess, acc, link, refs, msg=None, pace=1.0):
            s.link_calls.append((int(acc["id"]), list(refs)))
            if link_answer:
                return link_answer(int(acc["id"]), list(refs))
            return {"ok": len(refs), "failed": 0, "errors": [], "untried": []}
        monkeypatch.setattr(mie, "invite_via_link_batch", _via_link)
        return s
    return _install


def _privacy_all(refs):
    return {"ok": 0, "failed": len(refs), "errors": [f"{r}: privacy restricted" for r in refs],
            "privacy_failed": list(refs), "untried": []}


def test_flooded_promoter_is_spared_from_trick_and_link_dm(promoter_stand, monkeypatch):
    from services import flood_engine as fe
    peer: list = []

    async def _peer(pool, acc_id, action_type="", operation_id=None):
        peer.append(int(acc_id))
    monkeypatch.setattr(fe, "record_peer_flood", _peer)

    def responder(acc_id, refs, dry=False):
        if acc_id == 1:
            return {"ok": 0, "failed": 0, "errors": [], "peer_flood": True,
                    "untried": list(refs)}
        return _privacy_all(refs)

    s = promoter_stand(responder)
    monkeypatch.setattr(fe, "record_peer_flood", _peer)
    res = _run(_Pool(), ["u1", "u2", "u3", "u4"], account_ids=[1, 2, 3])

    assert 1 in peer
    assert s.trick_calls == [], "промоут-трюк аккаунтом под PeerFlood — путь к его бану"
    senders = {acc for acc, _ in s.link_calls}
    assert senders and 1 not in senders, "ЛС недостижимым не шлёт аккаунт под флудом"
    assert "Промоут-трюк пропущен" in res["summary"]


def test_link_fallback_spreads_over_fleet_and_hands_over_on_flood(promoter_stand, monkeypatch):
    from services import flood_engine as fe
    peer: list = []

    async def _peer(pool, acc_id, action_type="", operation_id=None):
        peer.append(int(acc_id))

    def link_answer(acc_id, refs):
        if acc_id == 2:
            # первому отправителю Telegram дал PeerFlood после трёх сообщений
            return {"ok": 3, "failed": 0, "errors": [], "peer_flood": True,
                    "untried": refs[3:]}
        return {"ok": len(refs), "failed": 0, "errors": [], "untried": []}

    targets = [f"u{i}" for i in range(1, 46)]
    s = promoter_stand(lambda acc_id, refs, dry=False: _privacy_all(refs), link_answer)
    monkeypatch.setattr(fe, "record_peer_flood", _peer)
    res = _run(_Pool(), targets, account_ids=[1, 2, 3])

    assert all(len(refs) <= 20 for _, refs in s.link_calls), "порции, а не 200 одним вызовом"
    assert s.link_calls[0][0] != 1, "промоутер — последний отправитель, а не первый"
    delivered = sum(len(r) for _, r in s.link_calls) - 17  # 17 не ушли у #2 (флуд)
    assert delivered == 45, "остаток зафлуженного отправителя получают следующие"
    assert all(acc != 2 for acc, _ in s.link_calls[1:]), "зафлуженный из кругов выбыл"
    assert 2 in peer, "флуд отправителя ЛС записан в пульс"
    assert "отправлена ссылка в ЛС: 45" in res["summary"]
