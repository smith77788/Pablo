"""Воронки и автоворонки исполняет движок сценариев — на живой базе.

ЧТО БЫЛО. Три исполнителя цепочек: воронки (funnel_runner), автоворонки
(auto_funnel) и сценарии (workflow_runtime). У старых двух не было цели,
журнала шагов, выручки и источника, и каждый чинился отдельно.

ЧТО ПРОВЕРЯЕМ (services/legacy_funnels.py). Подписка на воронку переходит в
прогон движка на тот же шаг и то же время; старый исполнитель её больше не
трогает; итог прогона возвращается в подписку и счётчики воронки;
выключенная воронка стоит; «стоп» в старом разделе останавливает прогон;
повторный заход — новый прогон; автоворонка не повторяет уже занятый шаг и
сохраняет кнопку-ссылку; откат возвращает подписку на тот же шаг.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from services import legacy_funnels as lf

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993772
BOT_ID = 880077124
USER = 70772

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


# ── Чистая сборка шагов ──────────────────────────────────────────────────────

def test_a_funnel_compiles_to_delay_then_message_per_step():
    steps = lf.compile_funnel([
        {"step_order": 1, "message_text": "Два", "delay_minutes": 60},
        {"step_order": 0, "message_text": "Раз", "delay_minutes": 0},
    ])
    assert steps == [
        {"type": "message", "text": "Раз", "legacy_step": 0},
        {"type": "delay", "delay_minutes": 60},
        {"type": "message", "text": "Два", "legacy_step": 1},
    ]
    assert lf.message_index(steps, 0) == 0
    assert lf.message_index(steps, 1) == 2
    assert lf.message_index(steps, 2) is None


def test_an_auto_funnel_keeps_its_link_button_and_hours():
    steps = lf.compile_auto_funnel([
        {"step_num": 1, "message_text": "Привет", "delay_hours": 0,
         "button_text": "Сайт", "button_url": "https://example.com"},
        {"step_num": 2, "message_text": "Скидка", "delay_hours": 24,
         "button_text": None, "button_url": None},
    ])
    assert steps[0]["buttons"] == [{"text": "Сайт", "url": "https://example.com"}]
    assert steps[1] == {"type": "delay", "delay_minutes": 1440}
    assert steps[2]["legacy_step"] == 2 and "buttons" not in steps[2]


def test_rollback_position_counts_the_pending_delay():
    steps = lf.compile_funnel([
        {"step_order": 0, "message_text": "Раз", "delay_minutes": 0},
        {"step_order": 1, "message_text": "Два", "delay_minutes": 120},
    ])
    at = datetime.now(timezone.utc) + timedelta(minutes=5)
    step, when = lf.legacy_position(steps, 1, at)   # стоит на задержке
    assert step == 1
    assert abs((when - (at + timedelta(minutes=120))).total_seconds()) < 1
    assert lf.legacy_position(steps, 3, at)[0] is None


# ── Живая база ──────────────────────────────────────────────────────────────

async def _cleanup(p):
    await p.execute("DELETE FROM workflow_step_log WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM workflow_runs WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM workflow_definitions WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM automation_events WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM funnels WHERE bot_id=$1", BOT_ID)
    await p.execute("DELETE FROM auto_funnels WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM bot_users WHERE bot_id=$1", BOT_ID)
    await p.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT_ID)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=6)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    _run(_cleanup(p))
    yield p
    _run(_cleanup(p))
    _run(p.close())


@pytest.fixture()
def tg(pool, monkeypatch):
    from services import bot_api, brand_injection, workflow_runtime as rt

    sent: list[dict] = []

    async def _send(http, token, chat_id, text, buttons=None, reply_markup=None,
                    disable_notification=False):
        sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup,
                     "buttons": buttons})
        return True, None, ""

    async def _send_plain(http, token, chat_id, text, *a, **kw):
        sent.append({"chat_id": chat_id, "text": text, "legacy": True})
        return True, None

    async def _free(pool, bot_id):
        return False

    monkeypatch.setattr(bot_api, "send_message_classified", _send)
    monkeypatch.setattr(bot_api, "send_message", _send_plain)
    monkeypatch.setattr(brand_injection, "is_free_tier", _free)
    monkeypatch.delenv("FUNNELS_ENGINE", raising=False)

    async def _fresh():
        await _cleanup(pool)
        await pool.execute(
            "INSERT INTO managed_bots(token, bot_id, username, is_active, added_by) "
            "VALUES($1,$2,'legacy_funnels_test_bot',TRUE,$3)",
            f"1234:legacy-funnels-{BOT_ID}", BOT_ID, OWNER)
        await pool.execute(
            "INSERT INTO bot_users(bot_id, user_id, first_name, is_active) "
            "VALUES($1,$2,'Боря',TRUE)", BOT_ID, USER)
        await pool.execute("DELETE FROM automation_cursors WHERE consumer=$1",
                           rt.CONSUMER)
        await rt.bus.get_cursor(pool, rt.CONSUMER)

    _run(_fresh())
    return sent


async def _funnel(pool, steps, active=True):
    from database import db
    fid = (await db.create_funnel(pool, BOT_ID, "Прогрев", "start"))["id"]
    for i, (text, delay) in enumerate(steps):
        await db.add_funnel_step(pool, fid, i, text, delay)
    if not active:
        await pool.execute("UPDATE funnels SET is_active=FALSE WHERE id=$1", fid)
    return fid


async def _old_runner(pool):
    """Круг старого исполнителя воронок — он же вызывает мост."""
    import aiohttp
    from services import funnel_runner
    async with aiohttp.ClientSession() as http:
        await funnel_runner.run_once(pool, http)


async def _engine(pool):
    import aiohttp
    from services import workflow_runtime as rt
    async with aiohttp.ClientSession() as http:
        await rt.tick(pool, http, None)


async def _due_now(pool):
    await pool.execute(
        "UPDATE workflow_runs SET next_run_at = now() - interval '1 second' "
        "WHERE owner_id=$1 AND status='running'", OWNER)


def _texts(sent):
    return [m["text"] for m in sent]


def test_a_funnel_subscription_runs_on_the_engine_and_reports_back(pool, tg):
    from database import db

    async def go():
        fid = await _funnel(pool, [("Раз", 0), ("Два", 30)])
        await db.subscribe_to_funnel(pool, fid, USER)
        await _old_runner(pool)
        sub = await pool.fetchrow(
            "SELECT * FROM funnel_subscriptions WHERE funnel_id=$1", fid)
        assert sub["workflow_run_id"], "подписка не передана движку"
        wf = await pool.fetchrow(
            "SELECT * FROM workflow_definitions WHERE legacy_kind='funnel' AND legacy_id=$1",
            fid)
        assert wf and wf["owner_id"] == OWNER and wf["bot_id"] == BOT_ID
        # Старый исполнитель сам ничего не шлёт, даже если круг повторить.
        await _old_runner(pool)
        assert not [m for m in tg if m.get("legacy")]
        await _engine(pool)
        assert _texts(tg) == ["Раз"]
        run = await pool.fetchrow("SELECT * FROM workflow_runs WHERE id=$1",
                                  sub["workflow_run_id"])
        assert run["status"] == "running" and run["next_run_at"] > datetime.now(timezone.utc)
        await _due_now(pool)
        await _engine(pool)
        await _due_now(pool)
        await _engine(pool)
        assert _texts(tg) == ["Раз", "Два"]
        await _old_runner(pool)   # итог — обратно в подписку
        sub = await pool.fetchrow("SELECT * FROM funnel_subscriptions WHERE id=$1",
                                  sub["id"])
        assert sub["completed"] and not sub["dropped"]
        f = await pool.fetchrow("SELECT * FROM funnels WHERE id=$1", fid)
        assert f["completed_count"] == 1
        # Журнал шагов движка ведётся и для воронки.
        n = await pool.fetchval(
            "SELECT count(*) FROM workflow_step_log WHERE workflow_id=$1 AND outcome='sent'",
            wf["id"])
        assert n == 2
        # Повторный заход — новый прогон с первого шага.
        await db.subscribe_to_funnel(pool, fid, USER)
        await _old_runner(pool)
        await _engine(pool)
        assert _texts(tg) == ["Раз", "Два", "Раз"]
    _run(go())


def test_a_switched_off_funnel_stands_and_stop_cancels_the_run(pool, tg):
    from database import db

    async def go():
        fid = await _funnel(pool, [("Раз", 0), ("Два", 0)], active=False)
        await db.subscribe_to_funnel(pool, fid, USER)
        await _old_runner(pool)
        await _engine(pool)
        assert tg == [], "выключенная воронка не должна слать"
        await pool.execute("UPDATE funnels SET is_active=TRUE WHERE id=$1", fid)
        await _old_runner(pool)
        await _engine(pool)
        assert _texts(tg)[:1] == ["Раз"]
        # «Стоп» в старом разделе — прогон отменяется на следующем круге.
        await db.unsubscribe_user_from_funnels(pool, BOT_ID, USER)
        await _old_runner(pool)
        run = await pool.fetchrow(
            "SELECT r.status FROM workflow_runs r JOIN funnel_subscriptions fs "
            "ON fs.workflow_run_id=r.id WHERE fs.funnel_id=$1", fid)
        assert run["status"] in ("cancelled", "completed")
    _run(go())


def test_a_deleted_funnel_takes_its_mirror_and_live_runs_away(pool, tg):
    from database import db

    async def go():
        fid = await _funnel(pool, [("Раз", 60)])
        await db.subscribe_to_funnel(pool, fid, USER)
        await _old_runner(pool)
        run_id = await pool.fetchval(
            "SELECT workflow_run_id FROM funnel_subscriptions WHERE funnel_id=$1", fid)
        await db.delete_funnel(pool, fid, BOT_ID)
        await _old_runner(pool)
        assert await pool.fetchval(
            "SELECT count(*) FROM workflow_definitions WHERE legacy_kind='funnel' "
            "AND legacy_id=$1", fid) == 0
        assert await pool.fetchval(
            "SELECT status FROM workflow_runs WHERE id=$1", run_id) == "cancelled"
    _run(go())


def test_an_auto_funnel_skips_a_claimed_step_and_keeps_the_link(pool, tg):
    async def go():
        afid = await pool.fetchval(
            "INSERT INTO auto_funnels(owner_id, name, bot_id) VALUES($1,'Авто',$2) "
            "RETURNING id", OWNER, BOT_ID)
        for num, text, hours, bt, bu in [
                (1, "Первое", 0, None, None),
                (2, "Второе", 0, "Купить", "https://example.com/buy"),
                (3, "Третье", 5, None, None)]:
            await pool.execute(
                "INSERT INTO auto_funnel_steps(funnel_id, step_num, delay_hours, "
                "message_text, button_text, button_url) VALUES($1,$2,$3,$4,$5,$6)",
                afid, num, hours, text, bt, bu)
        # Шаг 1 уже занят старым исполнителем (упал между отправкой и сдвигом).
        await pool.execute(
            "INSERT INTO auto_funnel_runs(funnel_id, bot_id, user_id, next_step_num, "
            "next_send_at, last_sent_step) VALUES($1,$2,$3,1,now(),1)",
            afid, BOT_ID, USER)
        from services import auto_funnel
        import aiohttp
        async with aiohttp.ClientSession() as http:
            await auto_funnel._process_due_runs(pool, http)
        await _engine(pool)
        assert _texts(tg) == ["Второе"], "занятый шаг повторно не шлём"
        markup = tg[0]["reply_markup"]
        assert markup["inline_keyboard"][0][0]["url"] == "https://example.com/buy"
        run = await pool.fetchrow(
            "SELECT r.* FROM workflow_runs r JOIN auto_funnel_runs ar "
            "ON ar.workflow_run_id=r.id WHERE ar.funnel_id=$1", afid)
        assert run["next_run_at"] > datetime.now(timezone.utc) + timedelta(hours=4)
    _run(go())


def test_rollback_hands_the_subscription_back_on_the_same_step(pool, tg, monkeypatch):
    from database import db

    async def go():
        fid = await _funnel(pool, [("Раз", 0), ("Два", 90), ("Три", 0)])
        await db.subscribe_to_funnel(pool, fid, USER)
        await _old_runner(pool)
        await _engine(pool)          # «Раз» ушло, прогон ждёт 90 минут
        assert _texts(tg) == ["Раз"]
        monkeypatch.setenv("FUNNELS_ENGINE", "legacy")
        await _old_runner(pool)
        sub = await pool.fetchrow(
            "SELECT * FROM funnel_subscriptions WHERE funnel_id=$1", fid)
        assert sub["workflow_run_id"] is None
        assert sub["current_step"] == 1
        assert sub["next_send_at"] > datetime.now(timezone.utc) + timedelta(minutes=80)
        assert await pool.fetchval(
            "SELECT count(*) FROM workflow_runs WHERE owner_id=$1 "
            "AND status IN ('running','waiting')", OWNER) == 0
        # Дальше ведёт старый исполнитель — с того же места.
        await pool.execute(
            "UPDATE funnel_subscriptions SET next_send_at=now() WHERE id=$1", sub["id"])
        await _old_runner(pool)
        assert [m["text"] for m in tg if m.get("legacy")] == ["Два"]
    _run(go())
