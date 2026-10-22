"""Пакетное оформление продолжается с места обрыва, а не с начала списка.

Прогон идёт секундами на объект, то есть часами на сеть из сотен объектов, и
возврат в очередь посреди прогона — штатное событие: деплой, автоматический
повтор и, с недавних пор, потолок времени (`_resume_op_over_ceiling` прямо
рассчитывает, что исполнитель «пропустит уже взятые цели по operation_log»).

Исполнитель журнала не вёл и начинал список С НАЧАЛА. Цены две. Первая —
повтор реального действия с аккаунта по объекту, который уже оформлен.
Вторая хуже: прогон, который не укладывается в потолок, НИКОГДА не доходит
до хвоста списка. Последние объекты сети не получают оформления вообще, а
первые переделываются каждые несколько часов, пока операция не упрётся в
предел жизни (30 суток).

Проверяется именно продолжение: первый прогон оформляет два объекта и
обрывается, второй обязан взять оставшиеся три и не трогать первые два.
Нужен живой Postgres: отметки живут в operation_log.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993461
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
        "INSERT INTO platform_users(user_id, username) VALUES($1,'gp_long_test') "
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


@pytest.fixture
def quiet(monkeypatch, pool):
    """Телеграма нет, аккаунты свободны, темп нулевой."""
    from services import op_worker, resource_selector, session_simulator

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

    monkeypatch.setattr(resource_selector, "select_all_active", _select_all)
    monkeypatch.setattr(op_worker, "try_claim_accounts", _claim)
    monkeypatch.setattr(op_worker, "release_accounts", _release)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _free)
    monkeypatch.setattr(session_simulator, "chaos_factor", lambda *a, **kw: 0.0)


def _seed(pool, n: int = 5):
    _SEQ[0] += 1
    k = _SEQ[0]

    async def _mk():
        acc_id = await pool.fetchval(
            "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active) "
            "VALUES($1,$2,'session',TRUE) RETURNING id", OWNER, f"+7004{OWNER}{k}")
        plan_id = await pool.fetchval(
            "INSERT INTO global_presence_plans(owner_id, name_pattern, status) "
            "VALUES($1,'Новости {{CITY}}','done') RETURNING id", OWNER)
        chan = []
        for i in range(n):
            cid = -100900000 - k * 100 - i
            chan.append(cid)
            await pool.execute(
                "INSERT INTO global_presence_targets(plan_id, asset_type, status, "
                "   result_asset_id, selected_account_id, planned_name, planned_about, "
                "   city, city_slug, country, country_code) "
                "VALUES($1,'channel','done',$2,$3,$4,'описание','Казань','kazan',"
                "        'Россия','RU')",
                plan_id, cid, acc_id, f"Канал {i + 1}")
        op_id = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, "
            "   label, started_at) "
            "VALUES($1,'gp_bulk_apply','running','{}'::jsonb,$2,'оформление',now()) "
            "RETURNING id", OWNER, n)
        return int(plan_id), int(op_id), chan

    return _run(_mk())


def test_the_second_run_takes_the_objects_the_first_one_never_reached(
        pool, quiet, monkeypatch):
    """Обрыв на третьем объекте: следующий прогон начинает с третьего."""
    from services import account_manager, op_worker

    plan_id, op_id, chan = _seed(pool, n=5)
    params = {"plan_id": plan_id, "action": "about",
              "about_template": "Новости {{CITY}}"}
    touched: list[int] = []

    async def _about_until_cut(session, channel_id, text, **kw):
        touched.append(int(channel_id))
        if len(touched) == 3:
            # Так обрывается прогон на потолке времени: сторож снимает задачу.
            raise asyncio.CancelledError()
        return True

    monkeypatch.setattr(account_manager, "edit_channel_about", _about_until_cut)
    with pytest.raises(asyncio.CancelledError):
        _run(op_worker._exec_gp_bulk_apply(pool, None, op_id, OWNER, params))

    done_first = touched[:2]
    assert len(done_first) == 2, f"первый прогон оформил: {touched}"

    touched.clear()

    async def _about_ok(session, channel_id, text, **kw):
        touched.append(int(channel_id))
        return True

    monkeypatch.setattr(account_manager, "edit_channel_about", _about_ok)
    _run(pool.execute(
        "UPDATE operation_queue SET status='running', done_items=0 WHERE id=$1", op_id))
    op_worker._cancel_cache.pop(op_id, None)

    res = _run(op_worker._exec_gp_bulk_apply(pool, None, op_id, OWNER, params))

    again = [c for c in touched if c in done_first]
    assert not again, (
        f"объекты {again} оформляются второй раз: прогон начинает список с начала, "
        f"поэтому хвост сети не получает оформления никогда ({res})")
    assert sorted(touched) == sorted(chan[2:]), (
        f"второй прогон взял {touched}, а должен был взять оставшиеся {chan[2:]}")
    assert int(res.get("ok") or 0) == 5, (
        f"итог возобновлённой операции: {res.get('ok')} из 5 — оформленные прошлым "
        "прогоном обязаны входить в итог, иначе операция закрывается недобором")


def test_an_object_nobody_touched_does_not_get_a_journal_mark(
        pool, quiet, monkeypatch):
    """Отметка в журнале — только за реальное действие.

    У пакетного применения есть пути, на которых объект не трогали вовсе:
    аллокатор не выдал свободного имени, шаблон пустой, текст поста не
    собрался. Если отметить такой объект оформленным, следующий прогон
    пропустит его как сделанный — и имя не встанет НИКОГДА, причём молча:
    в итоге операции он будет числиться среди успешных.
    """
    from services import account_manager, op_worker, username_engine

    plan_id, op_id, _chan = _seed(pool, n=3)
    params = {"plan_id": plan_id, "action": "username"}

    class _NoFreeNames:
        """Пространство имён исчерпано: allocate честно отдаёт None."""

        def __init__(self, *a, **kw):
            pass

        def allocate(self, templates, ctx):
            return None

        def reserve(self, name):
            return None

    applied: list[int] = []

    async def _set_username(session, channel_id, name, **kw):
        applied.append(int(channel_id))
        return None

    monkeypatch.setattr(username_engine, "UsernameAllocator", _NoFreeNames)
    monkeypatch.setattr(account_manager, "set_channel_username", _set_username)

    res = _run(op_worker._exec_gp_bulk_apply(pool, None, op_id, OWNER, params))

    assert not applied, f"имя всё-таки ставилось: {applied}"
    marked = _run(op_worker.completed_steps(pool, op_id))
    assert not marked, (
        f"объекты {sorted(marked)} отмечены оформленными, хотя их не трогали: "
        f"следующий прогон пропустит их как сделанные, и имя не встанет "
        f"никогда ({res})")
