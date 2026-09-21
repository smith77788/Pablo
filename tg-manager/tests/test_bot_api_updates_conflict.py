"""Сбор аудитории не показывает пустой успех там, где чтение невозможно.

У бота с включённым вебхуком Telegram отвечает на getUpdates кодом 409:
«Conflict: can't use getUpdates method while webhook is active». Это не
временный сбой — повтор не поможет, пока вебхук стоит.

Обработка была такая: `data.get("result", []) if data.get("ok") else []`. То
есть 409 превращался в пустой список, и владелец видел «Получено апдейтов: 0,
Новых пользователей: +0» — экран выглядел успешным сбором, хотя не прочитал
ни одного обновления и прочитать не мог. Молчаливый ноль здесь хуже ошибки: по
нему кажется, что боту просто никто не писал.

Вебхуки ставятся продуктом самим (`managed_bot_webhooks.register_webhook` для
ботов с `use_webhook = true`), а сбор аудитории про этот флаг не знал вовсе.
"""
from __future__ import annotations

import asyncio

import pytest

from services import bot_api


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload
        self.calls = 0

    def post(self, url, **kwargs):
        self.calls += 1
        return _FakeResponse(self._payload, self._payload.get("error_code") or 200)


_CONFLICT = {
    "ok": False,
    "error_code": 409,
    "description": ("Conflict: can't use getUpdates method while webhook is "
                    "active; use deleteWebhook to delete the webhook first"),
}


def _run(coro):
    return asyncio.run(coro)


def test_fetch_updates_explains_instead_of_returning_empty():
    session = _FakeSession(_CONFLICT)

    with pytest.raises(bot_api.UpdatesUnavailable) as got:
        _run(bot_api.fetch_updates(session, "t0ken"))

    assert "вебхук" in str(got.value).lower(), (
        f"причина должна быть понятна владельцу по-русски: {got.value!r}"
    )


def test_scan_all_users_explains_too():
    session = _FakeSession(_CONFLICT)

    with pytest.raises(bot_api.UpdatesUnavailable):
        _run(bot_api.scan_all_users(session, "t0ken"))


def test_conflict_is_recognised_by_description_without_the_code():
    """Код может приехать не тот, а текст Telegram говорит прямо."""
    data = {"ok": False, "error_code": 400,
            "description": "Conflict: can't use getUpdates method while webhook is active"}
    assert bot_api._updates_conflict(data)


def test_ordinary_failures_are_not_mistaken_for_a_conflict():
    """Обычная ошибка не должна превращаться в рассказ про вебхук."""
    assert bot_api._updates_conflict({"ok": True, "result": []}) is None
    assert bot_api._updates_conflict(
        {"ok": False, "error_code": 401, "description": "Unauthorized"}) is None
    assert bot_api._updates_conflict(
        {"ok": False, "error_code": 500, "description": "Internal"}) is None


def test_normal_collection_still_works():
    payload = {
        "ok": True,
        "result": [
            {"update_id": 5, "message": {"from": {"id": 11, "username": "arthur"}}},
            {"update_id": 6, "message": {"from": {"id": 12, "is_bot": True}}},
        ],
    }
    session = _FakeSession(payload)

    updates = _run(bot_api.fetch_updates(session, "t0ken"))
    users = bot_api.extract_users_from_updates(updates)

    assert len(updates) == 2
    assert [u["user_id"] for u in users] == [11], "бот не должен попасть в аудиторию"


def test_both_audience_buttons_handle_the_refusal():
    """Иначе исключение из клиента уронит хендлер вместо объяснения."""
    import inspect

    from bot.handlers import audience

    src = inspect.getsource(audience)
    assert src.count("UpdatesUnavailable") >= 2, (
        "обе кнопки сбора обязаны ловить отказ клиента"
    )
