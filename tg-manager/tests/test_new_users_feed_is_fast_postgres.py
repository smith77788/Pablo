"""«Новые пользователи» после ускорения отдают ТО ЖЕ, что раньше, строка в строку.

Зачем. Лента `/api/miniapp/new_users` (кнопка «Лиды» на главной) была самым
медленным списком мини-аппа: агрегат по подписчикам с JOIN обратно на
`bot_users` и `NOT IN` Postgres исполнял двумя ПОЛНЫМИ проходами по таблице
подписчиков всей платформы. Замер на стенде (1,5 млн подписчиков, у владельца
50 ботов): 290 мс на запрос списка и ещё столько же на счётчик, подряд.
Теперь подписчики берутся по индексу (bot_id, first_seen) через массив ботов
владельца, имя — из той же строки (DISTINCT ON), список и счётчик идут разом:
31 мс.

Переписанный запрос легко сделать быстрым и неверным: потерять человека на
нескольких ботах, взять не тот бот, впустить свой аккаунт флота или чужого
бота. Поэтому здесь эталон — ПРЕЖНИЙ запрос дословно, а сравнивается ответ
настоящей ручки на живой базе, на данных, где есть каждая из этих ловушек.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 990771
STRANGER = 990772
BOTS = (7990771001, 7990771002, 7990771003)
FOREIGN_BOT = 7990772001
TOKEN = "123456:new-users-feed-test-token-abcdef"

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


# Прежний запрос ленты — дословно, как эталон поведения.
_OLD_LIST = """WITH own AS (
                   SELECT tg_user_id FROM tg_accounts
                   WHERE owner_id=$1 AND tg_user_id IS NOT NULL
               ),
               agg AS (
                   SELECT bu.user_id,
                          MAX(bu.first_seen) AS first_seen,
                          COUNT(DISTINCT bu.bot_id) AS bot_count,
                          (array_agg(bu.bot_id ORDER BY bu.first_seen DESC NULLS LAST))[1] AS bot_id
                   FROM bot_users bu
                   JOIN managed_bots mb ON mb.bot_id = bu.bot_id
                   WHERE mb.added_by = $1 AND bu.user_id > 0
                     AND bu.user_id NOT IN (SELECT tg_user_id FROM own)
                   GROUP BY bu.user_id
               )
               SELECT a.user_id, a.first_seen, a.bot_count, a.bot_id,
                      bu.username, bu.first_name, mb.username AS bot_username
               FROM agg a
               JOIN bot_users bu ON bu.user_id = a.user_id AND bu.bot_id = a.bot_id
               JOIN managed_bots mb ON mb.bot_id = a.bot_id
               ORDER BY a.first_seen DESC NULLS LAST, a.user_id DESC
               LIMIT $2 OFFSET $3"""

_OLD_TOTAL = """WITH own AS (
                   SELECT tg_user_id FROM tg_accounts
                   WHERE owner_id=$1 AND tg_user_id IS NOT NULL
               )
               SELECT COUNT(DISTINCT bu.user_id) FROM bot_users bu
               JOIN managed_bots mb ON mb.bot_id = bu.bot_id
               WHERE mb.added_by = $1 AND bu.user_id > 0
                 AND bu.user_id NOT IN (SELECT tg_user_id FROM own)"""


async def _cleanup(p):
    ids = list(BOTS) + [FOREIGN_BOT]
    await p.execute("DELETE FROM bot_users WHERE bot_id = ANY($1::bigint[])", ids)
    await p.execute("DELETE FROM managed_bots WHERE bot_id = ANY($1::bigint[])", ids)
    await p.execute("DELETE FROM tg_accounts WHERE owner_id = ANY($1::bigint[])",
                    [OWNER, STRANGER])


async def _seed(p):
    for i, bid in enumerate(BOTS):
        await p.execute(
            "INSERT INTO managed_bots (bot_id, token, username, added_by, is_active) "
            "VALUES ($1, $2, $3, $4, true)", bid, f"nu-test-{bid}", f"nu_bot_{i}", OWNER)
    await p.execute(
        "INSERT INTO managed_bots (bot_id, token, username, added_by, is_active) "
        "VALUES ($1, 'nu-test-foreign', 'nu_foreign', $2, true)", FOREIGN_BOT, STRANGER)
    # 300 человек: каждый третий — на двух-трёх ботах владельца, с разной
    # свежестью подписки на каждом (выбор «последнего бота» обязан совпасть);
    # каждый пятый — ещё и на чужом боте (он не должен добавить бот в счёт).
    rows = []
    for u in range(1, 301):
        uid = 880000000 + u
        n_bots = 1 + (u % 3)
        for k in range(n_bots):
            rows.append((BOTS[(u + k) % 3], uid, f"user{u}_{k}", f"Имя{u}",
                         (u * 37 + k * 11) % 1000))
        if u % 5 == 0:
            rows.append((FOREIGN_BOT, uid, f"foreign{u}", "Чужой", u % 1000))
    # Ноль и отрицательные id — не люди (служебные строки).
    rows.append((BOTS[0], 0, "zero", "Ноль", 1))
    await p.executemany(
        "INSERT INTO bot_users (bot_id, user_id, username, first_name, first_seen, is_active) "
        "VALUES ($1, $2, $3, $4, now() - make_interval(mins => $5), true)", rows)
    # Свои аккаунты флота — подписчики ботов, но не лиды. Аккаунт чужого
    # владельца с тем же tg_user_id НЕ должен прятать человека.
    for k, uid in enumerate((880000003, 880000010, 880000011)):
        await p.execute(
            "INSERT INTO tg_accounts (owner_id, phone, tg_user_id, is_active, session_str) "
            "VALUES ($1, $2, $3, true, '')", OWNER, f"+7990771{k:04d}", uid)
    await p.execute(
        "INSERT INTO tg_accounts (owner_id, phone, tg_user_id, is_active, session_str) "
        "VALUES ($1, '+79907720000', 880000020, true, '')", STRANGER)
    # Аккаунт владельца без tg_user_id — NULL не должен обнулить ленту.
    await p.execute(
        "INSERT INTO tg_accounts (owner_id, phone, is_active, session_str) "
        "VALUES ($1, '+79907719999', true, '')", OWNER)


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
    _run(_seed(p))
    yield p
    _run(_cleanup(p))
    _run(p.close())


async def _call(pool, limit, offset):
    from aiohttp.test_utils import TestClient, TestServer

    from services import mini_app_auth, payment_webhook

    app = payment_webhook.make_app(pool, MagicMock())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        tok = mini_app_auth.make_token(OWNER, TOKEN)
        r = await client.get(f"/api/miniapp/new_users?limit={limit}&offset={offset}",
                             headers={"Authorization": "Bearer " + tok})
        assert r.status == 200, await r.text()
        return await r.json()
    finally:
        await client.close()


def _norm(rows):
    return [(int(r["user_id"]), int(r["bot_count"]), int(r["bot_id"]),
             r["username"], r["first_name"], r["bot_username"]) for r in rows]


@pytest.mark.parametrize("limit,offset", [(100, 0), (100, 100), (2000, 0), (50, 250)])
def test_feed_matches_the_previous_query_row_for_row(pool, monkeypatch, limit, offset):
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("MANAGER_BOT_TOKEN", TOKEN)
    got = _run(_call(pool, limit, offset))
    want = _run(pool.fetch(_OLD_LIST, OWNER, limit, offset))
    want_total = _run(pool.fetchval(_OLD_TOTAL, OWNER))
    assert _norm(got["users"]) == _norm(want)
    assert got["total"] == want_total


def test_the_traps_are_actually_in_the_data(pool, monkeypatch):
    """Контроль самого стенда: без этих случаев сравнение ничего не доказывает."""
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("MANAGER_BOT_TOKEN", TOKEN)
    got = _run(_call(pool, 2000, 0))
    ids = {int(r["user_id"]) for r in got["users"]}
    counts = {int(r["user_id"]): int(r["bot_count"]) for r in got["users"]}
    assert len(ids) == len(got["users"]), "человек повторился в ленте"
    assert {880000003, 880000010, 880000011}.isdisjoint(ids), "свой аккаунт флота в лидах"
    assert 880000020 in ids, "чужой аккаунт флота спрятал человека"
    assert 0 not in ids
    assert max(counts.values()) == 3 and min(counts.values()) == 1
    assert counts[880000005] == 3, "чужой бот попал в счёт ботов"
    assert got["total"] == 297
