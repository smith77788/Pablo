"""Ретеншен инвайта: маркер исхода, без которого фича не работала никогда.

ЧТО БЫЛО СЛОМАНО. Экран «Ретеншен» (mini_app_api.invite_retention_overview) и
подсказка организма (organism/world.invite_retention) считают приток запросом
`operation_log.status='ok' AND message='joined'`. Маркер 'joined' не писал НИКТО:
исполнитель инвайта логировал успешные цели как `status='ok'` без message.

Приток всегда выходил нулём → summarize отдавал retention_pct=None → экран
показывал «нет данных», а подсказка организма (порог joined >= 10) не могла
сработать в принципе. Две функции были мертвы с момента появления.

Маркер не декоративный: у метода «ссылка в ЛС» успех означает доставленную
ссылку, а не вступление, и складывать их в один счётчик значило бы завышать
ретеншен. Поэтому исход называется явно.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from services import invite_retention, op_worker


# ── Чистая свёртка ───────────────────────────────────────────────────────────

def test_summary_without_joins_is_unknown_not_zero():
    """Ноль вступивших — это «нет данных», а не «ретеншен 0%»."""
    s = invite_retention.summarize(0, 0)
    assert s["retention_pct"] is None and s["churn_pct"] is None
    assert invite_retention.health(s["retention_pct"]) == "unknown"


def test_summary_counts_retention():
    s = invite_retention.summarize(100, 25)
    assert s["retained"] == 75 and s["retention_pct"] == 75.0 and s["churn_pct"] == 25.0
    assert invite_retention.health(75.0) == "amber"
    assert invite_retention.health(95.0) == "green"
    assert invite_retention.health(10.0) == "red"


def test_churn_cannot_exceed_joins():
    """Оттока больше, чем притока (ушли приглашённые прошлого периода) —
    ретеншен не должен уходить в минус."""
    s = invite_retention.summarize(10, 40)
    assert s["retained"] == 0 and s["retention_pct"] == 0.0


# ── Маркер реально пишется исполнителем ──────────────────────────────────────

class _MarkerPool:
    """Ловит per-target записи в operation_log вместе с маркером исхода."""

    def __init__(self):
        self.done_items = 0
        self.rows: list = []

    async def execute(self, q, *a):
        if "done_items=done_items+" in q:
            self.done_items += int(a[1])
        return "OK"

    async def executemany(self, q, args):
        if "INSERT INTO operation_log" in q:
            for row in args:
                self.rows.append(row)
        return "OK"

    async def fetch(self, q, *a):
        return []

    async def fetchrow(self, q, *a):
        return None

    @property
    def outcomes(self):
        return {r[3] for r in self.rows if len(r) > 3}


def _mk_stand(monkeypatch):
    import services.mass_inviter_engine as inv
    from services import flood_engine as fe

    async def _noop(*a, **k):
        return None

    async def _afalse(*a, **k):
        return False

    async def _false_coro():
        return False

    monkeypatch.setattr(op_worker.asyncio, "sleep", lambda _x: _noop())
    monkeypatch.setattr(op_worker, "_is_cancelled", lambda *a, **k: _false_coro())
    monkeypatch.setattr(op_worker, "_safe_execute", _noop)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _afalse)
    for name in ("recommended_delay", "gaussian_delay"):
        monkeypatch.setattr(fe, name, lambda *a, **k: 0.0)
    for name in ("record_success", "record_peer_flood", "record_flood"):
        monkeypatch.setattr(fe, name, _noop)

    async def _limit(pool, acc_id):
        return {"limit": 1000, "used_today": 0, "remaining": 1000, "basis": "тест"}
    monkeypatch.setattr(fe, "recommended_daily_limit", _limit)

    accounts = [{"id": 1, "session_str": "s1", "proxy_url": None}]

    async def _sel(pool, owner_id, **k):
        return accounts
    monkeypatch.setattr(op_worker.resource_selector, "select_all_active", _sel)

    async def _fetch(pool, q, *a):
        return accounts if "tg_accounts" in q else []
    monkeypatch.setattr(op_worker, "_safe_fetch", _fetch)

    async def _batch(session, acc, group, refs, pace_mult=1.0, bulk=None):
        return {"ok": len(refs), "failed": 0, "errors": []}
    monkeypatch.setattr(inv, "invite_batch", _batch)

    async def _links(session, acc, link, refs, msg, pace_mult=1.0):
        # pace_mult: режим темпа доходит и до метода «ссылка в ЛС» — раньше он
        # спал фиксированные 2.5–5с и только после УСПЕХА.
        return {"ok": len(refs), "failed": 0, "errors": []}
    monkeypatch.setattr(inv, "invite_via_link_batch", _links)

    async def _export(session, acc, group):
        return "https://t.me/+abcdefgh12"
    monkeypatch.setattr(inv, "export_group_invite_link", _export)


def _run(pool, **params):
    p = {"group": "@testgroup", "account_ids": [1], "batch_size": 5,
         "user_refs": ["@a", "@b", "@c"]}
    p.update(params)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(op_worker._exec_mass_invite(pool, None, 77, 100, p))
    finally:
        loop.run_until_complete(op_worker.release_operation_accounts(77))
        loop.close()


def test_direct_invite_marks_targets_as_joined(monkeypatch):
    _mk_stand(monkeypatch)
    pool = _MarkerPool()
    _run(pool)
    assert pool.rows, "успешные цели вообще не записаны в журнал операции"
    assert pool.outcomes == {"joined"}, (
        f"ретеншен считает по message='joined', записано: {pool.outcomes}"
    )


def test_link_method_is_not_counted_as_joined(monkeypatch):
    """Доставленная ссылка ещё не вступление — иначе ретеншен завышается."""
    _mk_stand(monkeypatch)
    pool = _MarkerPool()
    _run(pool, invite_method="link")
    assert pool.outcomes == {"link_sent"}, (
        f"успех метода «ссылка в ЛС» не должен считаться вступлением: {pool.outcomes}"
    )


# ── Запросы читателей совпадают с тем, что пишется ───────────────────────────

def _read(rel):
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("rel", ["services/mini_app_api.py", "services/organism/world.py"])
def test_readers_query_the_marker_that_is_actually_written(rel):
    """Оба читателя ищут ровно тот маркер, который пишет исполнитель."""
    src = _read(rel)
    assert "ol.message='joined'" in src or "message='joined'" in src
    worker = _read("services/op_worker.py")
    assert '"joined"' in worker and "link_sent" in worker


def test_welcome_does_not_message_the_promote_trick_meta_target():
    """Мета-строки промоута — не получатели. Раньше фильтр исключал 'promote',
    но не 'promote_trick', и приветствие уходило несуществующему адресату."""
    src = _read("services/op_worker.py")
    i = src.index("async def _chain_welcome")
    seg = src[i:i + 2500]
    assert "message='joined'" in seg, "welcome должен отбирать по маркеру исхода"
    assert "promote_trick" in seg, "мета-цель промоут-трюка должна отсекаться"
