"""Выбор свободных аккаунтов под разогрев — на НАСТОЯЩЕМ Postgres.

ЗАЧЕМ ОТДЕЛЬНО. Заглушка пула принимает любые параметры и не исполняет SQL,
поэтому ни ошибка СВЯЗЫВАНИЯ (bigint[] против int[]), ни отсутствующая колонка
(`op_lease_until`, `trust_score`) на юнит-тестах не видны в принципе. А цена
такой ошибки здесь ровно та же, что у самой находки: `fleet_availability`
намеренно fail-open — сломанный запрос вернул бы пустой словарь, выбор снова
стал бы слепым, и всё это с зелёными тестами.

Второе: вес занятости судит по ЖИВОЙ АРЕНДЕ (`op_lease_until > now()`), а не по
флагу `in_operation`. Истечение аренды — поведение самой базы, на заглушке его
не воспроизвести вовсе.

КАК ЗАПУСТИТЬ — см. докстринг tests/test_invite_e2e_postgres.py.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

OWNER = 990377
_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
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


async def _wipe(p) -> None:
    await p.execute("DELETE FROM chat_warmup_log WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM chat_warmup_sessions WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)


async def _add(p, i: int, *, status: str = "active", active: bool = True,
               session: str | None = "s", lease_s: int | None = None,
               cooldown_s: int | None = None, trust: int = 50) -> int:
    """Аккаунт флота в нужном состоянии; lease_s<0 — аренда уже истекла."""
    return await p.fetchval(
        """INSERT INTO tg_accounts(owner_id, phone, session_str, is_active,
                                   acc_status, username, trust_score,
                                   in_operation, op_lease_owner, op_lease_until,
                                   cooldown_until)
           VALUES($1,$2,$3,$4,$5,$6,$7,
                  $8,
                  CASE WHEN $8 THEN 'w-test' ELSE NULL END,
                  CASE WHEN $9::int IS NULL THEN NULL
                       ELSE now() + make_interval(secs => $9::int) END,
                  CASE WHEN $10::int IS NULL THEN NULL
                       ELSE now() + make_interval(secs => $10::int) END)
           RETURNING id""",
        OWNER, f"+7993{i:07d}", session, active, status, f"acc{i}", trust,
        lease_s is not None, lease_s, cooldown_s)


def test_the_readiness_query_really_runs(pool):
    from services import chat_warmup as cw

    async def _t():
        await _wipe(pool)
        free = await _add(pool, 1)
        leased = await _add(pool, 2, lease_s=300)
        cooling = await _add(pool, 3, cooldown_s=1800)
        dead = await _add(pool, 4, status="banned")
        off = await _add(pool, 6, active=False)
        # Аккаунта без строки сессии в живой схеме быть не может (session_str
        # NOT NULL) — этот гейт проверяется юнит-тестом; здесь он стоит как
        # страховка на случай, если миграция снимет ограничение.

        got = await cw.fleet_availability(
            pool, [free, leased, cooling, dead, off], owner_id=OWNER)

        assert got, (
            "запрос готовности не исполнился на живой базе — fail-open вернул "
            "пустоту, и выбор аккаунтов снова слепой"
        )
        assert got[free]["code"] == cw.FREE
        assert got[leased]["code"] == cw.BUSY_LEASED
        assert got[cooling]["code"] == cw.BUSY_COOLDOWN
        for acc in (dead, off):
            assert got[acc]["code"] == cw.BUSY_UNFIT, got[acc]
        assert got[free]["label"] == "acc1", "подпись аккаунта не собралась"

    _run(_t())


def test_an_expired_lease_frees_the_account(pool):
    """Аренда истекает сама — именно так флот возвращается в оборот."""
    from services import chat_warmup as cw

    async def _t():
        await _wipe(pool)
        # Флаг `in_operation` остаётся TRUE (его чистит отдельный реконсилер), а
        # аренда уже истекла: арбитр захвата такой аккаунт отдаёт, значит и
        # разогрев обязан считать его свободным.
        stale = await _add(pool, 7, lease_s=-60)
        assert await pool.fetchval(
            "SELECT in_operation FROM tg_accounts WHERE id=$1", stale) is True

        got = await cw.fleet_availability(pool, [stale], owner_id=OWNER)
        assert got[stale]["code"] == cw.FREE, (
            "залипший флаг in_operation выводит из разогрева свободный аккаунт: "
            "сессия будет молчать при живом флоте"
        )

    _run(_t())


def test_the_owner_scope_holds(pool):
    """Чужой аккаунт по id не должен становиться участником разогрева."""
    from services import chat_warmup as cw

    async def _t():
        await _wipe(pool)
        mine = await _add(pool, 8)
        alien = await pool.fetchval(
            "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,"
            "acc_status) VALUES($1,$2,'s',TRUE,'active') RETURNING id",
            OWNER + 1, "+79949999999")
        try:
            got = await cw.fleet_availability(pool, [mine, alien], owner_id=OWNER)
            assert got[mine]["code"] == cw.FREE
            assert got[alien]["code"] == cw.BUSY_UNFIT, (
                "аккаунт другого владельца прошёл как годный"
            )
        finally:
            await pool.execute("DELETE FROM tg_accounts WHERE id=$1", alien)

    _run(_t())


def test_the_fleet_of_fiftyone_picks_the_free_thirty(pool):
    """Случай владельца целиком, на живой базе."""
    from services import chat_warmup as cw

    async def _t():
        await _wipe(pool)
        free = [await _add(pool, 100 + i) for i in range(21)]
        leased = [await _add(pool, 200 + i, lease_s=300) for i in range(10)]
        dead = [await _add(pool, 300 + i, status="banned") for i in range(20)]
        everyone = free + leased + dead
        assert len(everyone) == 51

        chosen, detail = await cw.pick_fleet(pool, everyone, 30, owner_id=OWNER)

        assert len(chosen) == 30
        assert set(free) <= set(chosen), (
            "свободные аккаунты не попали в сессию — ровно та жалоба владельца"
        )
        assert not (set(chosen) & set(dead)), (
            "забаненные заняли места в сессии, а мест всего 30"
        )
        assert sum(d["count"] for d in detail) == 21
        # И состав сессии реально сохраняется таким, каким выбран.
        s = await cw.create_session(pool, OWNER, "t.me/probe_chat", chosen)
        assert sorted(int(a) for a in s["account_ids"]) == sorted(chosen)

    _run(_t())


def test_a_barred_account_leaves_the_queue_on_a_live_table(pool):
    """Запрет письма читается из журнала и меняет вес аккаунта."""
    from services import chat_warmup as cw

    async def _t():
        await _wipe(pool)
        a = await _add(pool, 9)
        b = await _add(pool, 10)
        s = await cw.create_session(pool, OWNER, "t.me/probe_chat2", [a, b])
        s["account_ids"] = [a, b]
        await cw._note_write_forbidden(
            pool, s, b, RuntimeError("You can't write in this chat"))

        got = await cw.fleet_availability(pool, [a, b], owner_id=OWNER,
                                          session_id=int(s["id"]))
        assert got[a]["code"] == cw.FREE
        assert got[b]["code"] == cw.BUSY_NO_WRITE, (
            "отметка запрета письма не вычитывается обратно — аккаунт будет "
            "выбираться снова и снова и съедать ходы"
        )
        order = cw._speaker_order([a, b], last_speaker=None, busy=got)
        assert order[0] == a

    _run(_t())


def test_the_diagnosis_runs_on_a_live_table(pool):
    from services import chat_warmup as cw

    async def _t():
        await _wipe(pool)
        free = await _add(pool, 11)
        cooling = await _add(pool, 12, cooldown_s=1800)
        s = await cw.create_session(pool, OWNER, "t.me/probe_chat3",
                                    [free, cooling])
        s["account_ids"] = [free, cooling]

        out = await cw.diagnose(pool, OWNER, s)
        assert out["ready"] == 1, out
        reasons = {a["label"]: a["reason"] for a in out["accounts"]}
        assert reasons["acc11"] == ""
        assert "пауза Telegram" in reasons["acc12"], reasons

    _run(_t())
