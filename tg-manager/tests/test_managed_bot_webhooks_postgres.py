"""Подключённые боты получают обновления вебхуком — на живой базе.

ЧТО БЫЛО. Каждый подключённый бот опрашивался раз в 10 секунд, и человек
ждал ответа бота до 10 секунд. Модуль вебхуков был, но наполовину:
регистрацию никто не вызывал, очередь обновлений никто не читал, а приёмник
находил бота по первой попавшейся очереди.

ЧТО ПРОВЕРЯЕМ (services/managed_bot_webhooks.py). Приёмник пускает только
запрос с верным секретом бота; обновление разбирает тот же auto_responder,
что и опрос, и повтор того же обновления не разбирается дважды. Сверка
ставит вебхук и убирает бота из опроса; бот, до которого Telegram не
достучался, возвращается на опрос и сразу назад не переводится;
MANAGED_BOTS_DELIVERY=polling возвращает всех на опрос.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from services import managed_bot_webhooks as mbw

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993773
BOT_ID = 880077125
TOKEN = f"{BOT_ID}:hook-test-token"
USER = 70773

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


# ── Чистое ──────────────────────────────────────────────────────────────────

def test_the_public_address_comes_from_config_and_must_be_https(monkeypatch):
    for k in ("PUBLIC_BASE_URL", "WEBHOOK_URL", "MANAGED_BOTS_DELIVERY"):
        monkeypatch.delenv(k, raising=False)
    assert mbw.base_url() is None
    monkeypatch.setenv("WEBHOOK_URL", "https://app.example.com/webhook")
    assert mbw.base_url() == "https://app.example.com"
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://pub.example.com/miniapp/")
    assert mbw.base_url() == "https://pub.example.com"
    assert mbw.hook_url(mbw.base_url(), 42) == "https://pub.example.com/tgbot/hook/42"
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://insecure.example.com")
    assert mbw.base_url() is None, "Telegram шлёт вебхук только на HTTPS"
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://pub.example.com")
    monkeypatch.setenv("MANAGED_BOTS_DELIVERY", "polling")
    assert mbw.base_url() is None


def test_a_webhook_is_broken_only_with_a_fresh_error_and_a_queue():
    now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    fresh = int((now - timedelta(minutes=2)).timestamp())
    old = int((now - timedelta(hours=2)).timestamp())
    assert mbw.webhook_broken({"last_error_date": fresh, "pending_update_count": 7}, now)
    assert not mbw.webhook_broken({"last_error_date": fresh, "pending_update_count": 0}, now)
    assert not mbw.webhook_broken({"last_error_date": old, "pending_update_count": 7}, now)
    assert not mbw.webhook_broken({"pending_update_count": 7}, now)


# ── Живая база ──────────────────────────────────────────────────────────────

async def _cleanup(p):
    await p.execute("DELETE FROM bot_update_offsets WHERE bot_id=$1", BOT_ID)
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
    """Подменённый Telegram: вызовы Bot API — в `calls`, ответы — из `answers`."""
    from services import bot_api, brand_injection

    calls: list[dict] = []
    answers: dict[str, dict] = {}

    async def _call(http, token, method, **params):
        calls.append({"method": method, "token": token, **params})
        return answers.get(method, {"ok": True, "result": True})

    async def _send(http, token, chat_id, text, *a, **kw):
        calls.append({"method": "sendMessage", "chat_id": chat_id, "text": text})
        return True, None

    async def _send_c(http, token, chat_id, text, *a, **kw):
        calls.append({"method": "sendMessage", "chat_id": chat_id, "text": text})
        return True, None, ""

    async def _free(pool, bot_id):
        return False

    monkeypatch.setattr(bot_api, "_call", _call)
    monkeypatch.setattr(bot_api, "send_message", _send)
    monkeypatch.setattr(bot_api, "send_message_classified", _send_c)
    monkeypatch.setattr(brand_injection, "is_free_tier", _free)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://pub.example.com")
    monkeypatch.delenv("MANAGED_BOTS_DELIVERY", raising=False)

    async def _fresh():
        await _cleanup(pool)
        await pool.execute(
            "INSERT INTO managed_bots(token, bot_id, username, is_active, added_by) "
            "VALUES($1,$2,'hook_test_bot',TRUE,$3)", TOKEN, BOT_ID, OWNER)

    _run(_fresh())
    return calls, answers


async def _post(pool, body, secret, bot_id=BOT_ID):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    app = web.Application()
    mbw.add_routes(app, pool)
    async with TestClient(TestServer(app)) as client:
        headers = {"X-Telegram-Bot-Api-Secret-Token": secret} if secret else {}
        resp = await client.post(f"/tgbot/hook/{bot_id}", json=body, headers=headers)
        return resp.status


def _update(uid: int, text: str) -> dict:
    return {"update_id": uid, "message": {
        "message_id": uid, "date": int(time.time()), "text": text,
        "chat": {"id": USER, "type": "private"},
        "from": {"id": USER, "is_bot": False, "first_name": "Вера"}}}


def test_only_telegram_with_the_bots_secret_gets_in(pool, tg):
    async def go():
        assert await _post(pool, _update(5, "привет"), None) == 401
        assert await _post(pool, _update(5, "привет"), "чужой-секрет") == 401
        assert await _post(pool, _update(5, "привет"),
                           mbw._make_secret(TOKEN), bot_id=BOT_ID + 1) == 404
        assert await pool.fetchval(
            "SELECT count(*) FROM bot_users WHERE bot_id=$1", BOT_ID) == 0
    _run(go())


def test_an_update_is_handled_once_by_the_same_pipeline_as_polling(pool, tg):
    async def go():
        secret = mbw._make_secret(TOKEN)
        assert await _post(pool, _update(10, "привет"), secret) == 200
        assert await pool.fetchval(
            "SELECT count(*) FROM bot_users WHERE bot_id=$1 AND user_id=$2",
            BOT_ID, USER) == 1, "подписчик не записан — разбор не дошёл"
        from database import db
        assert await db.get_update_offset(pool, BOT_ID) == 10
        seen = await pool.fetchval(
            "SELECT last_seen FROM bot_users WHERE bot_id=$1 AND user_id=$2",
            BOT_ID, USER)
        # Повтор того же обновления (Telegram не дождался ответа) — мимо.
        await pool.execute(
            "UPDATE bot_users SET last_seen = now() - interval '1 day' "
            "WHERE bot_id=$1 AND user_id=$2", BOT_ID, USER)
        assert await _post(pool, _update(10, "привет"), secret) == 200
        again = await pool.fetchval(
            "SELECT last_seen FROM bot_users WHERE bot_id=$1 AND user_id=$2",
            BOT_ID, USER)
        assert again < seen, "повтор обновления разобран второй раз"
    _run(go())


def test_sync_moves_bots_to_webhooks_and_back_when_telegram_cannot_reach_us(
        pool, tg, monkeypatch):
    calls, answers = tg

    async def go():
        # Сверка идёт по ВСЕМ ботам базы: чужие тестовые боты возвращаем как были.
        others = [r["bot_id"] for r in await pool.fetch(
            "SELECT bot_id FROM managed_bots WHERE bot_id<>$1 "
            "AND NOT COALESCE(use_webhook,FALSE)", BOT_ID)]
        try:
            await _sync_scenario()
        finally:
            await pool.execute(
                "UPDATE managed_bots SET use_webhook=FALSE, webhook_failed_at=NULL "
                "WHERE bot_id = ANY($1::bigint[])", others)

    async def _sync_scenario():
        import aiohttp
        from database import db
        async with aiohttp.ClientSession() as http:
            st = await mbw.sync(pool, http)
            assert st["on"] >= 1
            hooks = [c for c in calls if c["method"] == "setWebhook"
                     and c["token"] == TOKEN]
            assert hooks and hooks[-1]["url"] == f"https://pub.example.com/tgbot/hook/{BOT_ID}"
            assert hooks[-1]["secret_token"] == mbw._make_secret(TOKEN)
            assert hooks[-1]["max_connections"] == 1
            assert hooks[-1]["drop_pending_updates"] is False
            polled = {b["bot_id"] for b in await db.get_bots_for_polling(pool)}
            assert BOT_ID not in polled, "бот на вебхуке не должен опрашиваться"

            # Telegram не может доставить: свежая ошибка, копится очередь.
            answers["getWebhookInfo"] = {"ok": True, "result": {
                "url": mbw.hook_url("https://pub.example.com", BOT_ID),
                "pending_update_count": 12,
                "last_error_date": int(time.time()) - 30,
                "last_error_message": "Connection timed out"}}
            st = await mbw.sync(pool, http)
            assert st["fallback"] >= 1
            row = await pool.fetchrow(
                "SELECT use_webhook, webhook_failed_at FROM managed_bots WHERE bot_id=$1",
                BOT_ID)
            assert row["use_webhook"] is False and row["webhook_failed_at"]
            polled = {b["bot_id"] for b in await db.get_bots_for_polling(pool)}
            assert BOT_ID in polled, "бот без рабочего вебхука должен вернуться на опрос"
            calls.clear()
            await mbw.sync(pool, http)
            assert not [c for c in calls if c["method"] == "setWebhook"
                        and c["token"] == TOKEN], "сразу назад на вебхук не переводим"

            # Принудительный опрос снимает вебхук со всех.
            await pool.execute(
                "UPDATE managed_bots SET use_webhook=TRUE, webhook_failed_at=NULL "
                "WHERE bot_id=$1", BOT_ID)
            monkeypatch.setenv("MANAGED_BOTS_DELIVERY", "polling")
            calls.clear()
            st = await mbw.sync(pool, http)
            assert [c for c in calls if c["method"] == "deleteWebhook"
                    and c["token"] == TOKEN]
            assert await pool.fetchval(
                "SELECT use_webhook FROM managed_bots WHERE bot_id=$1", BOT_ID) is False
    _run(go())


def test_polling_does_not_tear_down_our_own_webhook_on_conflict(pool, tg):
    calls, answers = tg

    async def go():
        import aiohttp
        from database import db
        from services import auto_responder
        await db.set_update_offset(pool, BOT_ID, 3)
        await pool.execute("UPDATE managed_bots SET use_webhook=TRUE WHERE bot_id=$1",
                           BOT_ID)
        answers["getUpdates"] = {"ok": False, "error_code": 409, "description":
                                 "Conflict: can't use getUpdates method while webhook is active"}
        async with aiohttp.ClientSession() as http:
            await auto_responder._process_bot(pool, http, BOT_ID, TOKEN)
        assert not [c for c in calls if c["method"] == "deleteWebhook"], (
            "опрос, начатый до перевода на вебхук, снял наш же вебхук")
    _run(go())
