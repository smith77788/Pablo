"""Главный вебхук бота принимает апдейты только с секретом Telegram.

Дыра: POST /webhook регистрировался без secret_token, setWebhook ставился без
него же. Апдейт — обычный JSON, поле from.id пишет отправитель, поэтому любой,
кто знал адрес сервера, присылал «сообщение от админа» (/admin, правка
переменных окружения, /backup) или нажатие кнопки от имени любого владельца, и
бот исполнял его как настоящее.

Тест падает без фикса: апдейт без заголовка получал 200 и уходил в диспетчер.
"""
from __future__ import annotations

import hashlib

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from aiogram import Bot, Dispatcher, Router

from services.payment_webhook import register_telegram_webhook
from services.security import main_webhook_secret

TOKEN = "123456789:AAEhBP0av28bGtUcLzZ2Dz5UudzRo7PkLg0"
HEADER = "X-Telegram-Bot-Api-Secret-Token"


def _update(uid: int = 1) -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1, "date": 0, "text": "/admin",
            "chat": {"id": uid, "type": "private"},
            "from": {"id": uid, "is_bot": False, "first_name": "x"},
        },
    }


async def _client(seen: list):
    dp = Dispatcher()
    r = Router()

    @r.message()
    async def _any(message):  # noqa: ANN001
        seen.append(message.from_user.id)

    dp.include_router(r)
    bot = Bot(token=TOKEN)
    app = web.Application()
    # handle_in_background=False не задаём: проверяем ровно боевую регистрацию.
    register_telegram_webhook(app, dp, bot, "/webhook")
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, bot


@pytest.mark.parametrize("headers", [{}, {HEADER: "wrong-secret-value-123"}])
async def test_forged_update_without_secret_is_rejected(headers):
    seen: list = []
    client, bot = await _client(seen)
    try:
        resp = await client.post("/webhook", json=_update(777), headers=headers)
        assert resp.status == 401
        assert seen == []
    finally:
        await client.close()
        await bot.session.close()


async def test_update_with_secret_is_accepted():
    seen: list = []
    client, bot = await _client(seen)
    try:
        resp = await client.post(
            "/webhook", json=_update(5),
            headers={HEADER: main_webhook_secret(TOKEN)})
        assert resp.status == 200
    finally:
        await client.close()
        await bot.session.close()


def test_secret_is_derived_and_not_the_session_key(monkeypatch):
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    s = main_webhook_secret(TOKEN)
    assert s == main_webhook_secret(TOKEN)
    assert len(s) == 64 and all(c in "0123456789abcdef" for c in s)
    # Ключ подписи сессий мини-аппа — sha256(token): секрет вебхука не должен
    # совпадать с ним ни в каком виде (Telegram видит секрет вебхука).
    assert s != hashlib.sha256(TOKEN.encode()).hexdigest()
    assert main_webhook_secret(TOKEN + "x") != s


def test_no_secret_without_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    with pytest.raises(ValueError):
        main_webhook_secret("")


def test_explicit_secret_is_validated(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "Good_secret-value_123")
    assert main_webhook_secret(TOKEN) == "Good_secret-value_123"
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "bad secret with spaces!!")
    with pytest.raises(ValueError):
        main_webhook_secret(TOKEN)


def test_set_webhook_passes_secret():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    i = src.index("await bot.set_webhook(")
    call = src[i:src.index(")", src.index("drop_pending_updates", i))]
    assert "secret_token=main_webhook_secret(" in call
