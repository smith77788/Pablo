"""Одна метка здоровья аккаунта: Работает / Пауза / Нужно действие / Потерян."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from services import account_verdict as av

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _p(state, **kw):
    base = {"state": state, "reason": "причина", "ready_in_sec": None, "hint": None,
            "rehab_phase": None}
    base.update(kw)
    return base


def _row(status="active", active=True):
    return {"acc_status": status, "is_active": active}


def test_ready_is_working():
    v = av.verdict_for(_row(), _p("ready", ready_in_sec=0))
    assert v["level"] == "ok" and v["label"] == "Работает"
    assert v["reason"] == "готов к действиям"


def test_ready_with_trust_hint_keeps_the_hint():
    v = av.verdict_for(_row(), _p("ready", hint="доверие ниже порога"))
    assert v["level"] == "ok" and v["reason"] == "доверие ниже порога"


def test_cooling_and_quarantine_are_pause_with_time():
    for st in ("cooling", "quarantine"):
        v = av.verdict_for(_row(), _p(st, ready_in_sec=600))
        assert v["level"] == "pause" and v["label"] == "Пауза" and v["ready_in_sec"] == 600


def test_blocked_needs_action():
    v = av.verdict_for(_row(), _p("blocked", reason="прокси не отвечает"))
    assert v["level"] == "action" and v["reason"] == "прокси не отвечает"


def test_lost_statuses_are_lost_even_without_pulse():
    for st in ("banned", "deactivated", "deleted"):
        v = av.verdict_for(_row(st, active=False), None)
        assert v["level"] == "lost" and v["label"] == "Потерян"


def test_recoverable_dead_needs_action():
    for st in ("frozen", "session_expired", "spamblock"):
        v = av.verdict_for(_row(st), _p("dead"))
        assert v["level"] == "action", st


def test_spamblock_under_self_rehab_is_pause():
    for phase in ("appeal", "warming", "recheck"):
        v = av.verdict_for(_row("spamblock"), _p("dead", rehab_phase=phase))
        assert v["level"] == "pause", phase
    stuck = av.verdict_for(_row("spamblock"), _p("dead", rehab_phase="stuck"))
    assert stuck["level"] == "action"


def test_disabled_by_owner_is_off():
    v = av.verdict_for(_row(active=False), None)
    assert v["level"] == "off"


def test_no_pulse_no_verdict_for_active():
    assert av.verdict_for(_row(), None) is None


def test_every_pulse_state_maps_to_a_verdict():
    from services import fleet_pulse
    for st in fleet_pulse.STATE_ORDER:
        assert av.verdict_for(_row(), _p(st)) is not None, st


def test_summarize_counts_levels():
    s = av.summarize([{"level": "ok"}, {"level": "ok"}, {"level": "lost"}, None])
    assert s["ok"] == 2 and s["lost"] == 1 and s["action"] == 0


def test_bot_pulse_uses_the_same_labels():
    from bot.handlers import fleet_pulse as screen
    text = screen._render([_p("cooling", name="acc", state_label="⏳ пауза",
                              ready_in="5 мин", acc_status="active")])
    assert "Пауза: <b>1</b>" in text and "⏳ Пауза <b>acc</b>" in text


def test_wired_into_accounts_list_and_screen():
    api = open(os.path.join(ROOT, "services", "mini_app_accounts.py"), encoding="utf-8").read()
    assert "_verdict.annotate(pool, uid, rows)" in api
    js = open(os.path.join(ROOT, "mini_app", "screens", "accounts_list.js"), encoding="utf-8").read()
    assert "a.verdict && ACC_VERDICT_STYLE[a.verdict.level]" in js


def test_detail_card_uses_the_same_verdict():
    api = open(os.path.join(ROOT, "services", "mini_app_accounts.py"), encoding="utf-8").read()
    assert "_verdict.for_account(pool, uid, dict(acc))" in api
    assert '"verdict": verdict,' in api
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "const _v = d.verdict;" in html and "accVerdictLabel(_v)" in html


DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
def test_for_account_on_real_schema():
    import asyncio

    import asyncpg

    owner = 987650077

    async def _run():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            async with pool.acquire() as c:
                await c.execute("DELETE FROM tg_accounts WHERE owner_id=$1", owner)

                async def _acc(n, **kw):
                    return await c.fetchval(
                        "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, "
                        "acc_status, cooldown_until) VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
                        owner, f"+7999222{n:04d}", kw.get("session", "s"),
                        kw.get("active", True), kw.get("status", "active"), kw.get("cd"))
                ok = await _acc(1)
                cool = await _acc(2, cd=datetime.now(timezone.utc) + timedelta(hours=2))
                frozen = await _acc(3, status="frozen")
                banned = await _acc(4, status="banned")
                off = await _acc(5, active=False)
                rows = {r["id"]: dict(r) for r in await c.fetch(
                    "SELECT id, is_active, acc_status FROM tg_accounts WHERE owner_id=$1", owner)}
            got = {i: (await av.for_account(pool, owner, rows[i]))["level"]
                   for i in (ok, cool, frozen, banned, off)}
            assert got[cool] == "pause" and got[frozen] == "action"
            assert got[banned] == "lost" and got[off] == "off"
            assert got[ok] in ("ok", "pause")  # без лога действий бюджет может держать паузу
        finally:
            await pool.close()

    asyncio.run(_run())
