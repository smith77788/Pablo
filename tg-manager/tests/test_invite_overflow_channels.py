"""Переливание инвайта по резерву каналов.

Сценарий владельца: выбрать канал, задать оформление (описание, пост),
аудиторию и аккаунты, запустить. Канал упёрся в лимит приглашённых или
поймал флуд — Infragram берёт следующий пустой канал из резерва, оформляет
его так же и приглашает туда оставшихся, и так по кругу.
"""
from __future__ import annotations

import pytest

from services import invite_overflow as ovf
from services import mass_inviter_engine as mie
from services import op_worker
from tests.test_invite_queue_scheduler import (  # noqa: F401 — фикстуры стенда
    _Pool,
    _reset_account_claims,
    _run,
    stand,
)

RESERVE = [
    {"channel_id": 501, "access_hash": 1, "username": "", "title": "R1", "acc_id": 1},
    {"channel_id": 502, "access_hash": 2, "username": "", "title": "R2", "acc_id": 2},
]


@pytest.fixture
def chain(stand, monkeypatch):
    """Стенд инвайта + учёт цепочки в памяти вместо базы."""

    def _install(responder, *, rows=None, reserve=RESERVE, busy=()):
        s = stand(lambda acc_id, refs, dry=False: {"ok": 0, "failed": 0, "errors": []})
        s.groups = []          # в какой канал ушёл каждый батч
        s.prepared = []        # какие каналы резерва оформлены и чем
        s.marks = []

        async def _invite(sess, acc, group, refs, pace_mult=1.0, bulk=None):
            s.groups.append((group, list(refs)))
            s.calls.append((int(acc["id"]), list(refs)))
            return responder(group, list(refs))
        monkeypatch.setattr(mie, "invite_batch", _invite)

        async def _rows(pool, owner_id, key):
            return list(rows or [])

        async def _busy(pool, owner_id, key):
            return set(busy)

        async def _load(pool, owner_id, ids):
            return [dict(c) for c in reserve if c["channel_id"] in ids]

        async def _mark(pool, owner_id, key, ref, cid, status, **k):
            s.marks.append((ref, cid, status, k.get("ok_delta", 0)))

        async def _prepare(acc, ch, setup):
            s.prepared.append((int(ch["channel_id"]), dict(setup), int(acc["id"])))
            return {"ok": True, "ref": f"https://t.me/+ch{ch['channel_id']}",
                    "done": ["описание", "пост"] if setup else [], "warnings": []}

        async def _status(sess, acc, group):
            return {"ok": True, "can_promote": False}

        monkeypatch.setattr(ovf, "chain_rows", _rows)
        monkeypatch.setattr(ovf, "busy_channel_ids", _busy)
        monkeypatch.setattr(ovf, "load_reserve", _load)
        monkeypatch.setattr(ovf, "mark", _mark)
        monkeypatch.setattr(ovf, "prepare_channel", _prepare)
        monkeypatch.setattr(mie, "channel_admin_status", _status)
        return s
    return _install


def _all_ok(group, refs):
    return {"ok": len(refs), "failed": 0, "errors": [], "untried": []}


TARGETS = [f"u{i}" for i in range(1, 13)]


def test_member_limit_moves_rest_to_next_reserve_channel(chain):
    """Главный упёрся в лимит участников — остаток уходит в канал резерва."""
    def responder(group, refs):
        if group == "@main":
            return {"ok": 0, "failed": 0, "untried": list(refs),
                    "errors": ["group error: в чате достигнут лимит участников Telegram"]}
        return _all_ok(group, refs)

    s = chain(responder)
    res = _run(_Pool(), TARGETS, group="@main", account_ids=[1, 2],
               reserve_channels=[501, 502],
               channel_setup={"about": "Про нас", "post": "Привет!"})

    invited = [r for g, refs in s.groups if g != "@main" for r in refs]
    assert sorted(invited) == sorted(TARGETS), "все цели приглашены в канал резерва"
    assert {g for g, _ in s.groups} == {"@main", "https://t.me/+ch501"}
    assert s.prepared and s.prepared[0][0] == 501
    assert s.prepared[0][1]["about"] == "Про нас", "канал резерва оформлен так же"
    assert ("@main", None, ovf.ST_FULL, 0) in s.marks, "главный отмечен как заполненный"
    assert res["status"] == "done" and res["ok"] == 12
    assert "📺 Каналы:" in res["summary"]


def test_per_channel_limit_spreads_audience_over_the_chain(chain):
    s = chain(_all_ok)
    res = _run(_Pool(), TARGETS, group="@main", account_ids=[1, 2], batch_size=5,
               reserve_channels=[501, 502], per_channel_limit=5)

    per = {}
    for g, refs in s.groups:
        per[g] = per.get(g, 0) + len(refs)
    assert per == {"@main": 5, "https://t.me/+ch501": 5, "https://t.me/+ch502": 2}
    assert res["ok"] == 12


def test_per_channel_limit_without_reserve_stops_instead_of_overfilling(chain):
    s = chain(_all_ok)
    res = _run(_Pool(), TARGETS, group="@main", account_ids=[1, 2], batch_size=5,
               per_channel_limit=5)
    assert sum(len(r) for _, r in s.groups) == 5, "сверх лимита канала не приглашаем"
    assert res["left"] == 7
    assert not res.get("next_op_id"), "в полный канал продолжение не планируется"


def test_reserve_exhausted_stops_with_honest_reason(chain):
    def responder(group, refs):
        return {"ok": 0, "failed": 0, "untried": list(refs),
                "errors": ["group error: нет доступа к чату (ChannelPrivateError)"]}

    s = chain(responder)
    res = _run(_Pool(), TARGETS, group="@main", account_ids=[1, 2],
               reserve_channels=[501, 502])
    assert {g for g, _ in s.groups} == {"@main", "https://t.me/+ch501", "https://t.me/+ch502"}
    assert res["left"] == 12
    assert "Резерв пустых каналов закончился" in res["summary"]


def test_continuation_resumes_in_the_active_reserve_channel(chain):
    rows = [
        {"channel_ref": "@main", "channel_id": None, "status": ovf.ST_FULL,
         "prepared": True, "invited_ok": 200},
        {"channel_ref": "https://t.me/+ch501", "channel_id": 501, "status": ovf.ST_ACTIVE,
         "prepared": True, "invited_ok": 40},
    ]
    s = chain(_all_ok, rows=rows)
    _run(_Pool(), TARGETS, group="@main", account_ids=[1, 2], reserve_channels=[501, 502])
    assert {g for g, _ in s.groups} == {"https://t.me/+ch501"}, (
        "продолжение начинает с канала, где остановились, а не с полного главного")
    assert s.prepared == [], "уже оформленный канал повторно не оформляется"


def test_full_main_from_previous_run_starts_in_reserve(chain):
    rows = [{"channel_ref": "@main", "channel_id": None, "status": ovf.ST_FULL,
             "prepared": True, "invited_ok": 200}]
    s = chain(_all_ok, rows=rows)
    _run(_Pool(), TARGETS, group="@main", account_ids=[1, 2], reserve_channels=[501])
    assert {g for g, _ in s.groups} == {"https://t.me/+ch501"}


def test_full_main_and_empty_reserve_fails_honestly(chain):
    rows = [{"channel_ref": "@main", "channel_id": None, "status": ovf.ST_FULL,
             "prepared": True, "invited_ok": 200}]
    s = chain(_all_ok, rows=rows, busy={501})
    res = _run(_Pool(), TARGETS, group="@main", account_ids=[1, 2], reserve_channels=[501])
    assert s.groups == []
    assert res["status"] == "failed" and "резерве" in res["summary"]


def test_flood_storm_in_channel_moves_to_next(chain, monkeypatch):
    monkeypatch.setenv("INVITE_FLOOD_STOP_STREAK", "2")
    accs = [{"id": i, "session_str": f"s{i}", "proxy_url": None} for i in range(1, 7)]

    async def _sel_all(pool, owner_id, **k):
        return accs
    calls = {"n": 0}

    def responder(group, refs):
        if group == "@main":
            calls["n"] += 1
            if calls["n"] > 1:
                return {"ok": 0, "failed": 0, "untried": list(refs), "errors": [],
                        "flood_wait": 120}
        return _all_ok(group, refs)

    s = chain(responder)
    monkeypatch.setattr(op_worker.resource_selector, "select_all_active", _sel_all)
    res = _run(_Pool(), [f"u{i}" for i in range(1, 31)], group="@main",
               account_ids=[a["id"] for a in accs], batch_size=5, reserve_channels=[501])
    assert "https://t.me/+ch501" in {g for g, _ in s.groups}
    assert not res["flood_storm"], "шторм в канале — переход, а не остановка"
    assert res["ok"] == 30


def test_without_reserve_nothing_changes(chain):
    s = chain(_all_ok)
    res = _run(_Pool(), TARGETS, group="@main", account_ids=[1, 2])
    assert {g for g, _ in s.groups} == {"@main"}
    assert s.marks == [], "без резерва цепочка не ведётся"
    assert "📺" not in res["summary"]


# ── модуль: разбор настроек и оформление канала ──────────────────────────────

def test_clean_setup_and_reserve_ids():
    assert ovf.clean_setup({"about": " a ", "post": "p", "junk": 1}) == \
        {"about": "a", "post": "p", "pin": True}
    assert ovf.clean_setup({"post": "p", "pin": False}) == {"post": "p"}
    assert ovf.clean_setup("x") == {}
    assert len(ovf.clean_setup({"about": "x" * 999})["about"]) == 255
    assert ovf.clean_reserve_ids([3, "3", "x", 0, 5]) == [3, 5]


@pytest.mark.asyncio
async def test_prepare_channel_applies_setup_and_returns_link(monkeypatch):
    from services import account_manager as am
    done = []

    async def _about(sess, cid, about, _acc=None, access_hash=0, username=""):
        done.append(("about", cid, about))
        return True

    async def _post(sess, cid, text, access_hash=0, username="", _acc=None):
        done.append(("post", cid, text))
        return {"msg_id": 7}

    async def _pin(sess, cid, access_hash=0, username="", _acc=None):
        done.append(("pin", cid))
        return {"pinned_msg_id": 7}

    async def _link(sess, cid, _acc=None, access_hash=0):
        return "https://t.me/+abc"

    monkeypatch.setattr(am, "edit_channel_about", _about)
    monkeypatch.setattr(am, "post_to_channel", _post)
    monkeypatch.setattr(am, "pin_last_channel_post", _pin)
    monkeypatch.setattr(am, "get_channel_invite_link", _link)
    res = await ovf.prepare_channel(
        {"id": 1, "session_str": "s"}, {"channel_id": 501, "access_hash": 1},
        ovf.clean_setup({"about": "A", "post": "P"}))
    assert res["ok"] and res["ref"] == "https://t.me/+abc"
    assert ("about", 501, "A") in done and ("post", 501, "P") in done and ("pin", 501) in done


@pytest.mark.asyncio
async def test_prepare_channel_without_link_is_unusable(monkeypatch):
    from services import account_manager as am

    async def _link(*a, **k):
        return ""
    monkeypatch.setattr(am, "get_channel_invite_link", _link)
    res = await ovf.prepare_channel({"id": 1, "session_str": "s"},
                                    {"channel_id": 501}, {})
    assert not res["ok"]


# ── поверхность: настройка доходит от экрана до исполнителя ──────────────────

def test_miniapp_and_api_carry_overflow_settings():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    ui = (root / "mini_app" / "index.html").read_text(encoding="utf-8")
    js = (root / "mini_app" / "screens" / "invite.js").read_text(encoding="utf-8")
    api = (root / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    for el in ("invOverflow", "invReserveWrap", "invPerChannelLimit",
               "invSetupAbout", "invSetupPost", "invSetupMain"):
        assert f'id="{el}"' in ui, f"на экране нет {el}"
    for key in ("body.reserve_channels", "body.per_channel_limit", "body.channel_setup"):
        assert key in js, f"{key} не уходит в запрос"
    i = api.index("async def mass_inviter_submit(")
    seg = api[i:api.index("\n    async def ", i + 10)]
    assert 'params["reserve_channels"]' in seg
    # резерв — только свои каналы: чужой канал оформить и пригласить нечем
    assert "FROM managed_channels WHERE owner_id=$1" in seg
    assert 'params["per_channel_limit"]' in seg and 'params["channel_setup"]' in seg
