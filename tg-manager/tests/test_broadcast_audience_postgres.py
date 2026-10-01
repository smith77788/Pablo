"""Кого рассылка берёт в адресаты — проверено на живой базе.

ЗАЧЕМ ЖИВОЙ POSTGRES. Остальные тесты этой пары (`test_blocked_user_is_not_a_
recipient.py`, `test_suspect_subscribers_get_no_broadcast.py`) читают текст
запросов: они ловят пропавшее условие, но не могут поймать ни ошибку
связывания параметра, ни выражение, которое Postgres поймёт не так, как
задумано. А выражения тут как раз нетривиальные:

  * `block_user` пишет `is_blocked=$3::boolean, is_active = NOT $3::boolean` —
    один параметр в двух местах и с отрицанием;
  * `batch_upsert_users` в ветке ON CONFLICT ставит
    `is_active = NOT bot_users.is_blocked`, то есть читает СТАРОЕ значение
    строки в том же запросе, который её обновляет.

Заглушка пула не исполняет SQL и такие вещи не проверяет в принципе.

ЧТО ЗАКРЕПЛЯЕТСЯ. Адресат рассылки — тот, кто активен, не заблокировал бота и
не помечен накруткой. Заблокировавший выходит из аудитории сразу и возвращается
только через Telegram; человек, однажды помеченный «неактивен» по недоставке,
возвращается, когда снова пишет боту, — но блокировку это не снимает.

КАК ЗАПУСТИТЬ — см. докстринг tests/test_invite_e2e_postgres.py; переменная та
же, INFRAGRAM_TEST_DSN.
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 991802
BOT = 7718020001
TOKEN = "7718020001:AAHaudience-fixture_0123456789abcdef"

NORMAL, BLOCKED, SUSPECT, GONE = 50001, 50002, 50003, 50004

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

    async def _mk():
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        files = ["schema.sql"] + sorted(
            glob.glob(os.path.join(ROOT, "schema_v*.sql")),
            key=lambda f: int(re.search(r"schema_v(\d+)", f).group(1)))
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
    _run(p.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT))
    _run(p.close())


@pytest.fixture(autouse=True)
def audience(pool):
    """Четыре подписчика: обычный, заблокировавший, накрученный и ушедший."""
    _run(pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT))
    _run(pool.execute(
        "INSERT INTO managed_bots(token, bot_id, username, added_by) "
        "VALUES($1,$2,'audience_fixture',$3)", TOKEN, BOT, OWNER))
    _run(pool.executemany(
        "INSERT INTO bot_users(bot_id, user_id, language_code, is_active, "
        "is_blocked, suspect) VALUES($1,$2,'ru',$3,$4,$5)",
        [(BOT, NORMAL, True, False, False),
         (BOT, BLOCKED, True, True, False),
         (BOT, SUSPECT, True, False, True),
         (BOT, GONE, False, False, False)]))
    yield
    _run(pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT))


def _bcast(pool, text="Новость дня") -> int:
    """Создать рассылку и вернуть её номер."""
    return _run(pool.fetchval(
        "INSERT INTO broadcasts(bot_id, message_text, total_users, created_by) "
        "VALUES($1,$2,0,$3) RETURNING id", BOT, text, OWNER))


def _flags(pool, user_id: int) -> dict:
    row = _run(pool.fetchrow(
        "SELECT is_active, is_blocked FROM bot_users WHERE bot_id=$1 AND user_id=$2",
        BOT, user_id))
    return dict(row) if row else {}


# ── кого берут выборки адресатов ─────────────────────────────────────────────

def test_only_the_reachable_subscriber_is_a_recipient(pool):
    from database import db

    ids = _run(db.get_audience_user_ids(pool, BOT))
    assert ids == [NORMAL], (
        f"в адресатах оказались лишние: {sorted(ids)}; "
        f"{BLOCKED} заблокировал бота, {SUSPECT} помечен накруткой, "
        f"{GONE} выведен из аудитории")


def test_language_segment_is_a_recipient_list_too(pool):
    from database import db

    ids = _run(db.get_audience_by_language(pool, BOT, "ru"))
    assert ids == [NORMAL], f"сегмент по языку берёт лишних: {sorted(ids)}"


def test_new_users_segment_is_a_recipient_list_too(pool):
    """Все четверо заведены только что, то есть попадают в окно «за 7 дней»."""
    from database import db

    ids = _run(db.get_audience_new_users(pool, BOT, 7))
    assert ids == [NORMAL], f"сегмент «новые» берёт лишних: {sorted(ids)}"


def test_audience_count_matches_the_recipient_list(pool):
    """Счётчик на экране и список адресатов должны сойтись."""
    from database import db

    ids = _run(db.get_audience_user_ids(pool, BOT))
    count = _run(db.get_audience_count(pool, BOT))
    assert count == len(ids), (
        f"экран обещает {count}, а уйдёт {len(ids)}")


# ── флаги: оба параметра одного запроса ──────────────────────────────────────

def test_block_removes_and_return_restores(pool):
    from database import db

    _run(db.block_user(pool, BOT, NORMAL, True))
    assert _flags(pool, NORMAL) == {"is_active": False, "is_blocked": True}
    assert _run(db.get_audience_user_ids(pool, BOT)) == [], (
        "заблокировавший остался адресатом")

    _run(db.block_user(pool, BOT, NORMAL, False))
    assert _flags(pool, NORMAL) == {"is_active": True, "is_blocked": False}
    assert _run(db.get_audience_user_ids(pool, BOT)) == [NORMAL], (
        "вернувшийся из блокировки не вернулся в аудиторию")


def test_delivery_failure_marks_a_block_as_a_block(pool):
    from database import db

    _run(db.mark_user_inactive(pool, BOT, NORMAL, reason="blocked"))
    assert _flags(pool, NORMAL) == {"is_active": False, "is_blocked": True}

    _run(db.mark_user_inactive(pool, BOT, SUSPECT, reason="deactivated"))
    assert _flags(pool, SUSPECT)["is_blocked"] is False, (
        "удалённый аккаунт выдан за заблокировавшего")


def test_writing_again_brings_back_only_those_who_are_not_blocked(pool):
    """Ветка ON CONFLICT читает старое значение is_blocked той же строки."""
    from database import db

    _run(db.upsert_users(pool, BOT, [
        {"user_id": GONE, "first_name": "Вернулся"},
        {"user_id": BLOCKED, "first_name": "Заблокировал"},
    ]))
    assert _flags(pool, GONE) == {"is_active": True, "is_blocked": False}, (
        "однажды помеченный неактивным не вернулся, хотя снова пишет боту")
    assert _flags(pool, BLOCKED) == {"is_active": False, "is_blocked": True}, (
        "старый апдейт из хвоста getUpdates воскресил заблокировавшего")

    assert sorted(_run(db.get_audience_user_ids(pool, BOT))) == [NORMAL, GONE], (
        "вернувшийся не попал в адресаты")


# ── весь путь доставки: от 403 до следующей рассылки ──────────────────────────

def test_block_during_a_broadcast_takes_the_subscriber_out_for_good(pool,
                                                                    monkeypatch):
    """403 «bot was blocked by the user» → человек выходит из аудитории.

    Это та самая цепочка, из-за которой фикс и начался: Telegram отвечает 403,
    `bot_api.classify_send_error` называет это 'blocked', цикл рассылки зовёт
    `mark_user_inactive(reason=...)`, и в базе должны погаснуть ОБА флага. Если
    гаснет только `is_active`, след блокировки теряется; если ни один — продукт
    стучится туда же следующей рассылкой.

    Telegram подменён на уровне `bot_api._call`, поэтому разбор ответа работает
    по-боевому; база настоящая.
    """
    from services import bot_api, broadcaster

    monkeypatch.setattr(broadcaster, "BROADCAST_DELAY", 0, raising=False)
    monkeypatch.setattr(broadcaster, "_GROUP_DELAY", 0, raising=False)

    async def _call(session, token, method, **params):
        if method == "getMe":
            return {"ok": True, "result": {"id": BOT, "username": "audience_fixture"}}
        if params.get("chat_id") == NORMAL:
            return {"ok": False, "error_code": 403,
                    "description": "Forbidden: bot was blocked by the user"}
        return {"ok": True, "result": {"message_id": 1}}

    monkeypatch.setattr(bot_api, "_call", _call)

    async def _free(*a, **k):
        return False

    monkeypatch.setattr("services.brand_injection.is_free_tier", _free)

    # Снимем прежние пометки: в аудитории должен остаться один NORMAL.
    _run(pool.execute(
        "UPDATE bot_users SET is_active=FALSE WHERE bot_id=$1 AND user_id<>$2",
        BOT, NORMAL))
    assert _run(__import__("database").db.get_audience_user_ids(pool, BOT)) == [NORMAL]

    bc = _bcast(pool)

    class _Session:
        async def close(self):
            return None

    _run(broadcaster.run(pool, _Session(), bc, TOKEN, BOT, "Новость дня"))

    assert _flags(pool, NORMAL) == {"is_active": False, "is_blocked": True}, (
        "403 «заблокировал» не оставил в базе следа блокировки")

    from database import db

    assert _run(db.get_audience_user_ids(pool, BOT)) == [], (
        "следующая рассылка снова постучится к заблокировавшему")
    assert _run(db.get_audience_count(pool, BOT)) == 0, (
        "на экране аудитория всё ещё есть, хотя писать некому")

    _run(pool.execute("DELETE FROM broadcasts WHERE id=$1", bc))


def test_a_broken_text_does_not_cost_the_audience(pool, monkeypatch):
    """400 по НАШЕЙ разметке — не повод выносить подписчиков из аудитории."""
    from services import bot_api, broadcaster
    from database import db

    monkeypatch.setattr(broadcaster, "BROADCAST_DELAY", 0, raising=False)
    monkeypatch.setattr(broadcaster, "_GROUP_DELAY", 0, raising=False)

    async def _call(session, token, method, **params):
        if method == "getMe":
            return {"ok": True, "result": {"id": BOT, "username": "audience_fixture"}}
        return {"ok": False, "error_code": 400,
                "description": "Bad Request: can't parse entities: Unmatched end tag"}

    monkeypatch.setattr(bot_api, "_call", _call)

    async def _free(*a, **k):
        return False

    monkeypatch.setattr("services.brand_injection.is_free_tier", _free)

    before = _run(db.get_audience_user_ids(pool, BOT))
    assert before == [NORMAL]

    bc = _bcast(pool, "Текст с <b>незакрытой скобкой")
    _run(broadcaster.run(pool, type("S", (), {"close": lambda s: None})(), bc,
                         TOKEN, BOT, "Текст с незакрытой скобкой"))

    assert _run(db.get_audience_user_ids(pool, BOT)) == before, (
        "аудиторию вынесла наша собственная кривая разметка")

    _run(pool.execute("DELETE FROM broadcasts WHERE id=$1", bc))
