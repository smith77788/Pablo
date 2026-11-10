"""Хранилище как список диалогов — на живом Postgres.

Главное, что здесь стережётся, — жалоба владельца: «хранилище не реагирует на
то, что клиенты уже получили ответ, а некоторые даже воспользовались услугами,
и продолжает слать уведомления». Ждущим ответа диалог считается в одном месте
(services.vault_dialogs), и его читает и список, и напоминание организма.

Без INFRAGRAM_TEST_DSN — skip (как и test_vault_e2e_postgres.py).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

OWNER = 996001
_LOOP = None


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
                pass
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


NOW = dt.datetime.now(dt.timezone.utc)


def _clean(pool):
    async def _c():
        for t in ("vault_messages", "vault_chat_state", "unified_contacts",
                  "virtual_states"):
            await pool.execute(f"DELETE FROM {t} WHERE owner_id=$1", OWNER)
        try:
            await pool.execute("DELETE FROM bot_sales_orders WHERE owner_id=$1", OWNER)
        except Exception:
            pass
    _run(_c())


_MID = [0]


def _put(pool, chat, direction, text, minutes_ago, *, deleted=False, name="Клиент",
         media=None):
    from services.token_vault import encrypt_token
    _MID[0] += 1
    _run(pool.execute(
        """INSERT INTO vault_messages (owner_id, connection_id, chat_id, msg_id,
               direction, peer_user_id, peer_name, text_enc, media_type, msg_date,
               is_deleted)
           VALUES ($1,'c',$2,$3,$4,$2,$5,$6,$7,$8,$9)""",
        OWNER, chat, _MID[0], direction, name,
        encrypt_token(text) if text else None, media,
        NOW - dt.timedelta(minutes=minutes_ago), deleted))


def _waiting_ids(pool):
    from services import vault_dialogs as vd
    return {w["chat_id"] for w in _run(vd.waiting_chats(pool, OWNER))}


def test_answered_from_telegram_is_not_waiting(pool):
    _clean(pool)
    _put(pool, 1, "in", "Сколько стоит?", 60)
    _put(pool, 1, "out", "1000 ₽", 30)          # ответ прямо из Telegram
    _put(pool, 2, "in", "Есть свободное время завтра?", 20)
    assert _waiting_ids(pool) == {2}


def test_thanks_after_answer_is_not_waiting(pool):
    _clean(pool)
    _put(pool, 1, "in", "Сколько стоит?", 60)
    _put(pool, 1, "out", "1000 ₽", 50)
    _put(pool, 1, "in", "Спасибо большое! 🙏", 40)
    _put(pool, 2, "out", "Ждём вас", 50)
    _put(pool, 2, "in", "А можно на час позже?", 40)
    assert _waiting_ids(pool) == {2}


def test_purchase_and_handled_end_waiting(pool):
    from services import vault_dialogs as vd
    _clean(pool)
    _put(pool, 1, "in", "Хочу записаться", 60)
    _put(pool, 2, "in", "Хочу записаться", 60)
    _put(pool, 3, "in", "Хочу записаться", 60)
    _run(vd.set_state(pool, OWNER, 1, purchased=True))
    _run(vd.set_state(pool, OWNER, 2, handled=True))     # ответил по телефону
    assert _waiting_ids(pool) == {3}
    # Новое сообщение после отметки «отвечено» — снова ждёт.
    _put(pool, 2, "in", "И ещё вопрос: а парковка есть?", -1)
    assert _waiting_ids(pool) == {2, 3}


def test_paid_order_ends_waiting(pool):
    _clean(pool)
    _put(pool, 5, "in", "Оплатил, когда приходить?", 10)
    try:
        _run(pool.execute(
            "INSERT INTO bot_sales_orders (owner_id, bot_id, customer_chat_id, "
            "total_cents, currency, status, paid_at) "
            "VALUES ($1, 1, 5, 100000, 'RUB', 'paid', now())", OWNER))
    except Exception as exc:
        pytest.skip(f"bot_sales_orders другой формы: {exc}")
    assert _waiting_ids(pool) == set()


def test_peer_deleted_question_is_not_waiting(pool):
    _clean(pool)
    _put(pool, 1, "out", "Здравствуйте", 60)
    _put(pool, 1, "in", "Вопрос", 30, deleted=True)
    assert _waiting_ids(pool) == set()


def test_dialog_list_folders_unread_and_pin(pool):
    from services import vault_dialogs as vd
    _clean(pool)
    _put(pool, 1, "in", "Привет", 60, name="Анна")
    _put(pool, 1, "in", "Ты тут?", 59, name="Анна")
    _put(pool, 2, "in", "Привет", 30, name="Борис")
    _put(pool, 2, "out", "Привет!", 20, name="Борис")
    _put(pool, 3, "in", "Старое", 600, name="Вера")
    _run(vd.set_state(pool, OWNER, 3, pinned=True))
    res = _run(vd.dialogs(pool, OWNER))
    order = [c["chat_id"] for c in res["chats"]]
    assert order[0] == 3, "закреплённый — сверху"
    anna = next(c for c in res["chats"] if c["chat_id"] == 1)
    assert anna["unread"] == 2 and anna["awaiting_reply"]
    assert anna["last_preview"] == "Ты тут?"
    boris = next(c for c in res["chats"] if c["chat_id"] == 2)
    assert boris["unread"] == 0 and not boris["awaiting_reply"]
    # Прочитано — счётчик обнуляется, но ждать ответа диалог не перестаёт.
    _run(vd.set_state(pool, OWNER, 1, read=True))
    anna = next(c for c in _run(vd.dialogs(pool, OWNER))["chats"] if c["chat_id"] == 1)
    assert anna["unread"] == 0 and anna["awaiting_reply"]
    # Архив прячет из «Все» и из ждущих.
    _run(vd.set_state(pool, OWNER, 1, archived=True))
    res = _run(vd.dialogs(pool, OWNER))
    assert 1 not in [c["chat_id"] for c in res["chats"]]
    assert res["counts"]["archived"] == 1
    arch = _run(vd.dialogs(pool, OWNER, folder="archived"))
    assert [c["chat_id"] for c in arch["chats"]] == [1]
    assert _run(vd.dialogs(pool, OWNER, q="бор"))["chats"][0]["chat_id"] == 2


def test_dialog_info_counts_and_dates(pool):
    from services import vault_dialogs as vd
    _clean(pool)
    _put(pool, 7, "in", "Здравствуйте", 120)
    _put(pool, 7, "in", "Есть вопрос", 119)
    _put(pool, 7, "out", "Слушаю", 100)            # ответ через 20 минут
    _put(pool, 7, "in", None, 90, media="photo")
    _put(pool, 7, "in", "Удалю это", 80, deleted=True)
    info = _run(vd.dialog_info(pool, OWNER, 7))
    assert info["total"] == 5 and info["incoming"] == 4 and info["outgoing"] == 1
    assert info["deleted_by_peer"] == 1
    assert info["owner_reply_median_min"] == 20.0
    assert info["first_date"] < info["last_date"]
    assert info["media_by_type"][0]["type"] == "photo"
    text = vd.info_text(info)
    assert "Сообщений в диалоге: <b>5</b>" in text and "UTC" in text
    assert _run(vd.dialog_info(pool, OWNER, 424242)) is None


def test_bouncing_skips_buyers_and_live_dialogs(pool):
    """«8 ходят по кругу» не про тех, кто купил и с кем уже переписываются."""
    from services import virtual_layer as vl
    _clean(pool)
    ids = {}
    for tg in (11, 12, 13):
        ids[tg] = _run(pool.fetchval(
            "INSERT INTO unified_contacts (owner_id, telegram_user_id, first_name) "
            "VALUES ($1, $2, 'x') RETURNING id::text", OWNER, tg))
        _run(pool.execute(
            "INSERT INTO virtual_states (owner_id, entity_type, entity_id, state_key, "
            "value, updated_at) VALUES ($1,'user',$2,'pattern','bouncing', $3)",
            OWNER, ids[tg], NOW - dt.timedelta(hours=5)))
    _put(pool, 11, "out", "Давайте созвонимся", 60)        # живой разговор после отметки
    from services import vault_dialogs as vd
    _run(vd.set_state(pool, OWNER, 12, purchased=True))
    assert _run(vl.active_bouncing_ids(pool, OWNER)) == [ids[13]]
    listed = _run(vl.bouncing_list(pool, OWNER))
    assert [b["entity_id"] for b in listed] == [ids[13]]
