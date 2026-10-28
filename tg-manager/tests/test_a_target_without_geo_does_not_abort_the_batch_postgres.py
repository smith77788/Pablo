"""Один объект без географии не обрывает весь пакетный прогон.

Имена, описания и посты объектов проекта собираются из гео шаблоном
`presence_planner.render_pattern`, и его докстринг обещает: «Не падает».
Обещание не выполнялось. Внутри стоит `slugify(geo.get("country", ""))`, а
`geo` — это строка базы, превращённая в словарь: колонка, которая есть, но
пуста, приходит как None, а не как отсутствующий ключ, и значение по
умолчанию не применяется. `slugify(None)` роняет TypeError.

Цена — не одна цель, а весь прогон. В `_exec_gp_bulk_apply` вызов шаблона
стоит ВНЕ try-блока элемента, поэтому исключение уходит из цикла и
операция падает целиком: объекты, стоявшие в списке после пустой строки,
не получают ничего, а уже сделанное объявляется сбоем операции. Остальной
исполнитель построен ровно наоборот — каждый элемент изолирован.

Нужен живой Postgres: None приходит именно из базы, и подделывать его
словарём значит проверять не тот путь.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993447
_SEQ = [0]

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def test_slugify_of_an_empty_column_is_an_empty_slug():
    """Пустая колонка — это пустой слаг, а не исключение."""
    from services.username_engine import slugify

    assert slugify(None) == "", (
        "slugify(None) падает, а двенадцать мест в коде зовут его как "
        "slugify(row.get('city', '')) — строка базы с пустой колонкой даёт "
        "ровно None")


def test_render_pattern_keeps_its_promise_on_an_empty_column():
    """`render_pattern` обещает «не падает» — и на пустом гео тоже."""
    from services.presence_planner import render_pattern

    out = render_pattern("Новости {{CITY}}", {"city": "Казань", "country": None,
                                              "city_slug": None, "scope": ""})
    assert "Казань" in out


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
        "INSERT INTO platform_users(user_id, username) VALUES($1,'gp_geo_test') "
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
    await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM platform_users WHERE user_id=$1", OWNER)


def test_a_target_with_an_empty_country_does_not_stop_the_others(pool, monkeypatch):
    """Пустая гео-колонка у среднего объекта не лишает поста остальных."""
    from services import account_manager, op_worker, resource_selector, session_simulator

    _SEQ[0] += 1
    k = _SEQ[0]
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

    async def _free(p, acc_id, **kw):
        return False

    monkeypatch.setattr(account_manager, "post_to_channel", _post)
    monkeypatch.setattr(account_manager, "pin_last_channel_post", _pin)
    monkeypatch.setattr(resource_selector, "select_all_active", _select_all)
    monkeypatch.setattr(op_worker, "try_claim_accounts", _claim)
    monkeypatch.setattr(op_worker, "release_accounts", _release)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _free)
    monkeypatch.setattr(session_simulator, "chaos_factor", lambda *a, **kw: 0.0)

    async def _seed():
        acc_id = await pool.fetchval(
            "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active) "
            "VALUES($1,$2,'session',TRUE) RETURNING id", OWNER, f"+7002{OWNER}{k}")
        plan_id = await pool.fetchval(
            "INSERT INTO global_presence_plans(owner_id, name_pattern, status) "
            "VALUES($1,'Новости {{CITY}}','done') RETURNING id", OWNER)
        for i in range(3):
            # У среднего объекта гео пустое — так выглядят цели, собранные до
            # генератора (колонки добавлены позже и остались NULL).
            empty = (i == 1)
            await pool.execute(
                "INSERT INTO global_presence_targets(plan_id, asset_type, status, "
                "   result_asset_id, selected_account_id, planned_name, planned_about, "
                "   city, city_slug, country, country_code) "
                "VALUES($1,'channel','done',$2,$3,$4,'описание',$5,$6,$7,$8)",
                plan_id, -100800000 - k * 100 - i, acc_id, f"Канал {i + 1}",
                None if empty else "Казань", None if empty else "kazan",
                None if empty else "Россия", None if empty else "RU")
        op_id = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, "
            "   label, started_at) "
            "VALUES($1,'gp_bulk_apply','running','{}'::jsonb,3,'публикация',now()) "
            "RETURNING id", OWNER)
        return int(plan_id), int(op_id)

    plan_id, op_id = _run(_seed())

    res = _run(op_worker._exec_gp_bulk_apply(
        pool, None, op_id, OWNER,
        {"plan_id": plan_id, "action": "post_pin",
         "post_template": "Новости {{CITY}}, {{COUNTRY}}"}))

    assert len(posted) == 3, (
        f"опубликовано постов: {len(posted)} из 3 — объект с пустой гео-колонкой "
        f"обрывает весь прогон, и следующие за ним не получают ничего ({res})")
