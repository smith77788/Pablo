"""Канал можно сменить аватар — не только название/описание/username.

Жалоба владельца: «не могу менять аватарки в каналах». Правка канала умела
title/about/username, но не аватар, хотя account_manager.set_channel_photo
существует (его зовёт гео-исполнитель при создании). Добавлен op avatar →
chan_avatar: исполнитель качает картинку по SSRF-гарду, уникализирует и ставит.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from services import op_worker
from services.mini_app_api import _CHANNEL_EDIT_OPS, channel_edit_worker_op

_ROOT = Path(__file__).resolve().parent.parent
_CHAN = (_ROOT / "services" / "mini_app_channels.py").read_text(encoding="utf-8")
_HTML = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_avatar_is_a_known_channel_edit_op():
    assert _CHANNEL_EDIT_OPS.get("avatar") == "chan_avatar"
    assert channel_edit_worker_op("avatar") == "chan_avatar"


def test_executor_knows_chan_avatar():
    assert "chan_avatar" in op_worker._CHAN_EXEC_OPS


def test_executor_fetches_safely_and_sets_channel_photo():
    src = inspect.getsource(op_worker._exec_bulk_chan_exec)
    assert 'op == "chan_avatar"' in src, "исполнитель обязан обрабатывать chan_avatar"
    # Картинка из URL качается через SSRF-гард и с потолком размера.
    assert "public_http_session" in src and "read_capped" in src
    # Ставится именно фото канала.
    assert "set_channel_photo" in src
    # Уникализация, чтобы у сети не было байт-в-байт одинакового фото.
    assert "uniquify" in src


def test_handler_validates_avatar_url():
    assert 'op == "avatar"' in _CHAN and "http://" in _CHAN and "https://" in _CHAN


def test_ui_offers_avatar_with_picker():
    assert '<option value="avatar">' in _HTML
    assert "id=\"ceAvatarPick\"" in _HTML
    assert "pickMediaInto('ceValue'" in _HTML
    # ceToggle ведёт себя по-особому для avatar (показывает загрузчик).
    assert "op==='avatar'" in _HTML
