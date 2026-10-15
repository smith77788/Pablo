"""«Поднять флот» уводило аккаунт из-под живой операции — проверено на Postgres.

РАЗРЫВ. Кнопка «Поднять флот» (`/api/miniapp/accounts/revive`) снимала
операционные блокировки со ВСЕХ аккаунтов владельца, у которых есть сессия:
`in_operation=FALSE` одним апдейтом плюс `op_worker.release_accounts` по всему
списку. Задуман он для флота, который переподключили, а залипшие флаги от
сожжённой сессии остались.

Но операции он не спрашивал. Нажатие в момент работы рассылки означало:
аккаунт, которым прямо сейчас коннектится живая задача, объявлен свободным — и
в БД, и в памяти арбитра. `_db_claim` пускал по условию `in_operation = FALSE`,
то есть следующая операция законно забирала ту же сессию, и Telegram убивал
auth-key (AUTH_KEY_DUPLICATED, безвозвратно). `renew_leases` при этом переставал
её продлевать и молчал: число продлённых строк возвращалось и выбрасывалось.

Теперь занятый аккаунт не трогают, арбитром в `_db_claim` стала АРЕНДА (а не
флаг), а расхождение памяти и БД о флоте пишется в лог и в счётчик.

Нужен настоящий Postgres: заглушка пула не исполняет SQL, то есть главное
свойство — «UPDATE обошёл занятую строку, а конкурент её не получил» — на
юните не проверяется в принципе. Рецепт запуска — в docstring
tests/test_account_lease_postgres.py.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN (см. docstring)")

OWNER = 994711
BUSY = 9947001        # ведёт живая операция: аренда в БД
STUCK = 9947002       # брошенный флаг: аренда истекла, его и надо поднять
IN_MEMORY = 9947003   # занят этим процессом: аренды в БД нет, в памяти есть

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    """Один цикл на модуль: пул asyncpg привязан к циклу, в котором создан."""
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _boot():
        conn = await asyncpg.connect(DSN)
        files = ["schema.sql"] + sorted(
            glob.glob("schema_v*.sql"),
            key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)))
        for f in files:
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass  # схемы идемпотентны
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


@pytest.fixture(autouse=True)
def fleet(pool):
    """Три аккаунта владельца: занятый арендой, брошенный и занятый в памяти."""
    from services import op_worker

    async def _mk():
        await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
        for aid in (BUSY, STUCK, IN_MEMORY):
            await pool.execute(
                "INSERT INTO tg_accounts(id, owner_id, phone, session_str, "
                "                        is_active, in_operation) "
                "VALUES($1,$2,$3,'sess',TRUE,TRUE)",
                aid, OWNER, f"+799900{aid}")
        # Живая аренда ЧУЖОГО процесса: так выглядит аккаунт, которым прямо
        # сейчас работает операция на другой реплике.
        await pool.execute(
            "UPDATE tg_accounts SET op_lease_owner='worker-A', "
            "       op_lease_until = now() + make_interval(mins => 10) "
            " WHERE id=$1", BUSY)
        # Брошенный: аренда истекла, флаг залип — ровно то, для чего кнопка.
        await pool.execute(
            "UPDATE tg_accounts SET op_lease_owner='worker-dead', "
            "       op_lease_until = now() - make_interval(mins => 1) "
            " WHERE id=$1", STUCK)

    _run(_mk())
    op_worker.init_op_worker_pool(pool)
    # Память арбитра — от предыдущего теста файла, а не от процесса: чистим
    # только свои id, чужие аккаунты этого же процесса трогать нельзя.
    op_worker._accounts_in_use.difference_update({BUSY, STUCK})
    op_worker._accounts_in_use.add(IN_MEMORY)
    yield
    op_worker._accounts_in_use.discard(IN_MEMORY)
    _run(pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER))


def _revive(pool, monkeypatch):
    """Нажать «Поднять флот» по-настоящему: HTTP, маршруты, живая БД."""
    from aiohttp import ClientSession, web
    from services import mini_app_api as M
    from services import security as _sec

    monkeypatch.setattr(M, "_get_uid", lambda request: OWNER)

    async def go():
        _sec._rate_limiter._requests.clear()
        app = web.Application()
        M.setup_routes(app, pool)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with ClientSession() as s:
                async with s.post(
                    f"http://127.0.0.1:{port}/api/miniapp/accounts/revive",
                    data=json.dumps({}),
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    return resp.status, await resp.json()
        finally:
            await runner.cleanup()

    return _run(go())


def _row(pool, aid):
    return _run(pool.fetchrow(
        "SELECT in_operation, op_lease_owner, "
        "       (op_lease_until IS NOT NULL AND op_lease_until > now()) AS leased "
        "  FROM tg_accounts WHERE id=$1", aid))


def test_the_busy_account_keeps_its_lease(pool, monkeypatch):
    """Аккаунт под живой арендой кнопка не трогает — иначе сессию заберёт второй."""
    st, d = _revive(pool, monkeypatch)
    assert st == 200, d

    busy = _row(pool, BUSY)
    assert busy["in_operation"] is True, (
        "«Поднять флот» снял блокировку с аккаунта, которым работает операция: "
        "следующая операция возьмёт ту же сессию — AUTH_KEY_DUPLICATED")
    assert busy["leased"] is True and busy["op_lease_owner"] == "worker-A", (
        "аренда живой операции стёрта — её владелец больше не сможет её продлить")
    assert d.get("busy_skipped", 0) >= 1, (
        "владельцу не сказали, что часть флота не тронута: он решит, что кнопка "
        "не сработала, и будет жать её снова")


def test_a_second_executor_still_cannot_take_it(pool, monkeypatch):
    """Последствие, а не только поле в строке: конкурент не получает аккаунт."""
    from services import op_worker

    _revive(pool, monkeypatch)
    assert op_worker._db_claim is not None
    got = _run(op_worker._db_claim([BUSY]))
    assert got == [], (
        "после «Поднять флот» другой исполнитель забрал аккаунт, которым идёт "
        "работа: одна auth-key сессия коннектится из двух мест")


def test_the_abandoned_account_is_still_revived(pool, monkeypatch):
    """Смысл кнопки сохранён: брошенный флаг снимается, аккаунт снова берётся."""
    from services import op_worker

    _revive(pool, monkeypatch)
    stuck = _row(pool, STUCK)
    assert stuck["in_operation"] is False, "брошенный аккаунт остался заблокированным"
    assert stuck["op_lease_owner"] is None and stuck["leased"] is False, (
        "снятый флаг при оставшейся аренде — состояние, в котором аккаунт нельзя "
        "ни взять, ни продлить")
    assert _run(op_worker._db_claim([STUCK])) == [STUCK], (
        "поднятый аккаунт обязан снова захватываться операцией")


def test_an_account_busy_in_memory_is_spared_too(pool, monkeypatch):
    """Правда о занятости живёт и в памяти процесса — её тоже спрашиваем.

    У прогрева, призрака и пре-флайта аренды в БД нет: они держат аккаунт в
    `_accounts_in_use`. Кнопка снимала и это (`release_accounts` по всему
    списку), то есть сессия под живым коннектом объявлялась свободной.
    """
    from services import op_worker

    _revive(pool, monkeypatch)
    assert IN_MEMORY in op_worker._accounts_in_use, (
        "«Поднять флот» снял in-memory блокировку с аккаунта под живым коннектом")
    assert _row(pool, IN_MEMORY)["in_operation"] is True, (
        "флаг занятого в памяти аккаунта снят — его заберёт операция")


def test_the_renewal_drift_is_not_silent(pool):
    """Продлили меньше, чем держим — значит аренду снял кто-то другой.

    Это предвестник AUTH_KEY_DUPLICATED: задачи ещё работают сессиями, а через
    TTL аккаунт законно заберёт другой исполнитель. Раньше число продлённых
    строк возвращалось и выбрасывалось вызывающим — следа не оставалось.
    """
    from services import op_worker

    seen: list[tuple] = []
    orig = op_worker._reliability_metric
    held_before = set(op_worker._accounts_in_use)
    op_worker._reliability_metric = lambda name, n=1, **lbl: seen.append((name, n))
    try:
        # Держим в памяти ровно два аккаунта (чужие id этого же процесса в
        # замере только путали бы арифметику), а аренда в БД — у одного.
        _run(pool.execute(
            "UPDATE tg_accounts SET in_operation=TRUE, op_lease_owner=$2, "
            "       op_lease_until = now() + make_interval(mins => 10) "
            " WHERE id=$1", STUCK, op_worker._WORKER_ID))
        op_worker._accounts_in_use.clear()
        op_worker._accounts_in_use.update({STUCK, BUSY})
        renewed = _run(op_worker.renew_leases())
    finally:
        op_worker._reliability_metric = orig
        op_worker._accounts_in_use.clear()
        op_worker._accounts_in_use.update(held_before)

    assert renewed == 1, f"продлена не своя аренда: {renewed}"
    assert ("infragram_account_lease_renew_drift_total", 1) in seen, (
        "расхождение памяти и БД о занятости флота не оставило ни следа: "
        f"записано {seen}")
