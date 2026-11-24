"""Причина провала операции — по-русски (регресс).

Было: текст исключения (Telethon/сеть) уходил владельцу как есть — в карточку
операции и в уведомление «❌ завершилась с ошибкой». Владелец не читает
по-английски. Кроме того, исключение вставлялось в HTML-уведомление без
экранирования: «<» в тексте ломал разметку, и уведомление не уходило вовсе.
"""
from __future__ import annotations

import inspect

from services import op_worker
from services.op_errors import explain_error_ru as ex


def test_known_telegram_errors_are_explained():
    assert ex("A wait of 300 seconds is required").startswith("Telegram просит подождать 300 с")
    assert ex("PEER_FLOOD").startswith("Telegram ограничил аккаунт")
    assert ex("The channel specified is private and you lack permission").startswith("Канал приватный")
    assert ex("INVITE_HASH_EXPIRED").startswith("Пригласительная ссылка просрочена")
    assert ex("Connection to Telegram failed 5 time(s)").startswith("Нет связи")


def test_raw_text_is_kept_for_debugging():
    assert "тех.: PEER_FLOOD" in ex("PEER_FLOOD")


def test_russian_text_passes_through_and_unknown_is_labeled():
    assert ex("Флот занят другими операциями") == "Флот занят другими операциями"
    assert ex("KeyError: 'x'") == "Непредвиденная ошибка: KeyError: 'x'"
    assert ex("") == "" and ex(None) == ""


def test_worker_uses_explanation_and_escapes_html():
    src = inspect.getsource(op_worker._run_op_task)
    assert "explain_error_ru(str(e)" in src
    assert "<code>{str(e)[:200]}</code>" not in src
    assert "_html_mod.escape(explain_error_ru" in src


def test_short_explanation_for_log_rows():
    from services.op_errors import explain_error_short as sh
    assert sh("USER_PRIVACY_RESTRICTED") == "Настройки приватности пользователя не позволяют"
    assert sh("The user has been deleted") == "Аккаунт пользователя удалён"
    assert sh("You can't write in this chat") == "Аккаунту запрещено писать в этот чат"
    assert sh("отправлено · акк 3") == "отправлено · акк 3"
    assert sh("weird thing") == "weird thing"  # нераспознанное — как есть


def test_operation_log_endpoint_translates_messages():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "services" / "mini_app_api.py").read_text(
        encoding="utf-8")
    i = src.index("async def operation_log(")
    body = src[i:src.index("async def cancel_operation(", i)]
    assert "explain_error_short" in body and '"message_raw"' in body
