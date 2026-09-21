"""«Пригласить аудиторию» в ноде-сообществе подставляло сырой числовой
tg_chat_id вместо инвайт-ссылки — инвайт падал у всех аккаунтов, кроме того
единственного, что случайно уже видел этот чат.

Первопричина. Регистрация ноды (`community_node_create` /
`nodes_engine.register_community_node`) принимает только число — никакого
username/access_hash/привязанного аккаунта не сохраняется. `inviteToCommunity`
подставляла этот голый `tg_chat_id` прямо в поле массового инвайта.
`get_entity(числовой_id)` у Telethon резолвится ТОЛЬКО если у аккаунта уже
есть эта entity в кеше сессии — у произвольного инвайтера её нет.

Фикс переиспользует то, что уже накоплено: `community_node_members`
(заполняется «Оживить», nodes_engine) хранит, какие аккаунты ФЛОТА реально
состоят в ноде. `community_node_invite_link` пробует их по порядку (админы
сначала) и экспортирует настоящую invite-ссылку — её и подставляет
`inviteToCommunity` вместо голого id.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

from tests.miniapp_source import miniapp_source

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 991801

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ── статика: маршрут, обработчик, фронтенд ───────────────────────────────────

def test_route_registered():
    src = _read("services/mini_app_api.py")
    assert ('app.router.add_get("/api/miniapp/community/node/{node_id}/invite_link", '
            'community_node_invite_link)') in src


def test_handler_prefers_admin_members_and_is_owner_scoped():
    src = _read("services/mini_app_api.py")
    i = src.index("async def community_node_invite_link")
    seg = src[i:src.index("\n    async def ", i + 10)]
    assert "community_node_members" in seg
    assert "owner_id=$2" in seg or "AND a.owner_id=$2" in seg
    assert "'admin' THEN 0" in seg, "админы должны пробоваться первыми — им точно можно экспортировать ссылку"
    assert "get_channel_invite_link" in seg


def test_frontend_invite_to_community_fetches_link_not_raw_id():
    ui = miniapp_source()
    i = ui.index("async function inviteToCommunity")
    seg = ui[i:ui.index("\n}", i)]
    assert "/invite_link" in seg
    assert "massInviteGroup" in seg
    assert "r.invite_link" in seg


def test_invite_button_passes_node_id_not_only_chat_id():
    ui = miniapp_source()
    assert "inviteToCommunity(${id},${chatId})" in ui, (
        "кнопка обязана передавать id ноды — без него не собрать invite_link"
    )


# ── реальный Postgres: только известные аккаунты флота, в правильном порядке ─

pytestmark_pg = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _mk():
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        files = ["schema.sql"] + sorted(
            glob.glob(os.path.join(ROOT, "schema_v*.sql")),
            key=lambda p_: int(re.search(r"schema_v(\d+)", p_).group(1)))
        for f in files:
            path = f if os.path.isabs(f) else os.path.join(ROOT, f)
            if not os.path.exists(path):
                continue
            try:
                await p.execute(open(path, encoding="utf-8").read())
            except Exception:
                pass
        return p

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.execute("DELETE FROM community_nodes WHERE owner_id=$1", OWNER))
    _run(p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(pool.execute("DELETE FROM community_nodes WHERE owner_id=$1", OWNER))
    _run(pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER))
    yield


async def _mk_account(pool, phone: str) -> int:
    return await pool.fetchval(
        "INSERT INTO tg_accounts(owner_id, phone, session_str, acc_status, is_active) "
        "VALUES($1,$2,'s','active',TRUE) RETURNING id",
        OWNER, phone,
    )


async def _mk_node(pool) -> int:
    return await pool.fetchval(
        "INSERT INTO community_nodes(owner_id, tg_chat_id, title, is_active) "
        "VALUES($1,-1009988776655,'test node',TRUE) RETURNING id",
        OWNER,
    )


def test_member_query_orders_admin_before_plain_member(pool):
    node_id = _run(_mk_node(pool))
    member_acc = _run(_mk_account(pool, "+79990001111"))
    admin_acc = _run(_mk_account(pool, "+79990002222"))
    _run(pool.execute(
        "INSERT INTO community_node_members(node_id, account_id, role) VALUES($1,$2,'member')",
        node_id, member_acc))
    _run(pool.execute(
        "INSERT INTO community_node_members(node_id, account_id, role) VALUES($1,$2,'admin')",
        node_id, admin_acc))

    rows = _run(pool.fetch(
        """SELECT a.id
           FROM community_node_members m
           JOIN tg_accounts a ON a.id=m.account_id
           WHERE m.node_id=$1 AND a.owner_id=$2 AND a.is_active=TRUE
                 AND a.session_str IS NOT NULL AND a.session_str <> ''
           ORDER BY CASE m.role WHEN 'admin' THEN 0 WHEN 'moderator' THEN 1 ELSE 2 END,
                    m.joined_at""",
        node_id, OWNER))
    assert [r["id"] for r in rows] == [admin_acc, member_acc], (
        "админ обязан идти первым в очереди попыток — у него точно есть право "
        "экспортировать ссылку"
    )


def test_node_with_no_known_members_is_the_documented_failure_mode(pool):
    """Нода без единого известного аккаунта (никогда не 'оживлялась') — ждём
    честный пустой список кандидатов, а не попытку угадать наугад."""
    node_id = _run(_mk_node(pool))
    rows = _run(pool.fetch(
        "SELECT a.id FROM community_node_members m JOIN tg_accounts a ON a.id=m.account_id "
        "WHERE m.node_id=$1", node_id))
    assert rows == []
