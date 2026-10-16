"""Возобновлённое пакетное применение не публикует пост во второй раз.

Пакетная публикация (`action='post_pin'`) идёт по объектам проекта с темпом
20–45 секунд на канал: для сети из нескольких сотен объектов прогон занимает
часы. Перезапуск процесса в это окно штатный — каждый деплой возвращает живую
операцию в очередь (`shutdown`), и её docstring прямо описывает контракт:
«исполнитель считает прогресс заново, пропуская уже взятые цели по
operation_log».

Этот исполнитель журнал не читал и не писал. Возобновлённый прогон начинал
список с начала и публиковал ВТОРОЙ пост в каждом канале, который его уже
получил, — подписчики видят дубль, а Telegram видит повторяющуюся
автоматическую активность. Рядом, в фабрике каналов, та же задача решена
`completed_steps` ровно затем, чтобы повтор не создавал второй комплект.

Нужен живой Postgres: идемпотентность держится журналом в базе, и проверять
её заглушкой пула бессмысленно.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993441
_SEQ = [0]

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    _run(_clean(p))
    _run(p.execute(
        "INSERT INTO platform_users(user_id, username) VALUES($1,'gp_bulk_test') "
        "ON CONFLICT (user_id) DO NOTHING", OWNER))
    yield p
    _run(_clean(p))
    _run(p.close())


async def _clean(p) -> None:
    await p.execute(
        "DELETE FROM operation_log WHERE op_id IN "
        "(SELECT id FROM operation_queue WHERE owner_id=$1)", OWNER)
    await p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM global_presence_plans WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM managed_channels WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM platform_users WHERE user_id=$1", OWNER)


def _seed(pool, n: int = 3):
    """Проект с n готовыми объектами и операция пакетной публикации."""
    _SEQ[0] += 1
    k = _SEQ[0]

    async def _mk():
        acc_id = await pool.fetchval(
            "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active) "
            "VALUES($1,$2,'session',TRUE) RETURNING id", OWNER, f"+7001{OWNER}{k}")
        plan_id = await pool.fetchval(
            "INSERT INTO global_presence_plans(owner_id, name_pattern, status) "
            "VALUES($1,'Новости {{CITY}}','done') RETURNING id", OWNER)
        for i in range(n):
            await pool.execute(
                "INSERT INTO global_presence_targets(plan_id, asset_type, status, "
                "   result_asset_id, selected_account_id, planned_name, planned_about, "
                "   city, city_slug, country, country_code) "
                "VALUES($1,'channel','done',$2,$3,$4,'описание','Казань','kazan',"
                "        'Россия','RU')",
                plan_id, -100700000 - k * 100 - i, acc_id, f"Канал {i + 1}")
        op_id = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, "
            "   label, started_at) "
            "VALUES($1,'gp_bulk_apply','running',$2::jsonb,$3,'публикация',now()) "
            "RETURNING id", OWNER, '{}', n)
        return int(plan_id), int(op_id), int(acc_id)

    return _run(_mk())


@pytest.fixture
def quiet(monkeypatch, pool):
    """Телеграма нет, аккаунты свободны, темп нулевой. Возвращает список постов."""
    from services import account_manager, op_worker, resource_selector, session_simulator

    posted: list[int] = []

    async def _post(session, channel_id, text, **kw):
        posted.append(int(channel_id))
        return {"ok": True}

    async def _pin(session, channel_id, **kw):
        return {"ok": True}

    async def _select_all(p, owner_id, include_ids=None, respect_cooldown=True, **kw):
        rows = await p.fetch(
            "SELECT id, session_str, owner_id FROM tg_accounts WHERE owner_id=$1", owner_id)
        return [dict(r) for r in rows]

    async def _claim(ids):
        return [int(i) for i in ids]

    async def _release(ids):
        return None

    async def _not_quarantined(p, acc_id, **kw):
        return False

    monkeypatch.setattr(account_manager, "post_to_channel", _post)
    monkeypatch.setattr(account_manager, "pin_last_channel_post", _pin)
    monkeypatch.setattr(resource_selector, "select_all_active", _select_all)
    monkeypatch.setattr(op_worker, "try_claim_accounts", _claim)
    monkeypatch.setattr(op_worker, "release_accounts", _release)
    monkeypatch.setattr(
        op_worker._infra_mem, "is_account_quarantined", _not_quarantined)
    monkeypatch.setattr(session_simulator, "chaos_factor", lambda *a, **kw: 0.0)
    return posted


def test_a_restart_does_not_publish_the_post_a_second_time(pool, quiet):
    """Тот же op_id после возврата в очередь не постит в каналы повторно."""
    from services import op_worker

    plan_id, op_id, _acc = _seed(pool, n=3)
    params = {"plan_id": plan_id, "action": "post_pin", "post_template": "Привет, {{CITY}}"}

    res1 = _run(op_worker._exec_gp_bulk_apply(pool, None, op_id, OWNER, params))
    assert len(quiet) == 3, f"первый прогон опубликовал {len(quiet)} постов: {res1}"

    # Деплой: shutdown возвращает операцию в очередь с done_items=0, новый
    # процесс подхватывает её с теми же params.
    _run(pool.execute(
        "UPDATE operation_queue SET status='running', done_items=0 WHERE id=$1", op_id))
    op_worker._cancel_cache.pop(op_id, None)

    res2 = _run(op_worker._exec_gp_bulk_apply(pool, None, op_id, OWNER, params))

    assert len(quiet) == 3, (
        f"после возобновления опубликовано постов: {len(quiet)} вместо 3 — каналы, "
        f"уже получившие пост, получают его второй раз ({res2})")


def test_the_journal_marks_only_channels_that_really_got_the_post(pool, quiet, monkeypatch):
    """Канал, в который пост не ушёл, остаётся в работе для следующего прогона."""
    from services import account_manager, op_worker

    plan_id, op_id, _acc = _seed(pool, n=3)
    params = {"plan_id": plan_id, "action": "post_pin", "post_template": "Привет"}
    seen: list[int] = []

    async def _post_one_fails(session, channel_id, text, **kw):
        seen.append(int(channel_id))
        if len(seen) == 2:
            return {"error": "CHAT_WRITE_FORBIDDEN"}
        quiet.append(int(channel_id))
        return {"ok": True}

    monkeypatch.setattr(account_manager, "post_to_channel", _post_one_fails)

    _run(op_worker._exec_gp_bulk_apply(pool, None, op_id, OWNER, params))
    assert len(quiet) == 2, f"ушло постов: {len(quiet)}"

    failed_channel = seen[1]
    _run(pool.execute(
        "UPDATE operation_queue SET status='running', done_items=0 WHERE id=$1", op_id))
    op_worker._cancel_cache.pop(op_id, None)
    seen.clear()

    _run(op_worker._exec_gp_bulk_apply(pool, None, op_id, OWNER, params))

    assert seen == [failed_channel], (
        f"второй прогон обратился к каналам {seen}: повтор обязан вернуться "
        "ровно к тому, куда пост не ушёл")
