"""Регрессия: маппинг операций редактирования канала.

Смена НАЗВАНИЯ канала (title) раньше отсутствовала в UI, хотя
account_manager.edit_channel_title существовал. Маппинг op→worker_op — единый
источник истины для одиночного (channel_edit) и массового (channels_mass) путей.
"""
from __future__ import annotations

from services.mini_app_api import channel_edit_worker_op, _CHANNEL_EDIT_OPS


def test_title_supported():
    assert channel_edit_worker_op("title") == "chan_title"


def test_about_and_username_supported():
    assert channel_edit_worker_op("about") == "chan_about"
    assert channel_edit_worker_op("username") == "chan_uname"


def test_unknown_op_returns_none():
    assert channel_edit_worker_op("delete") is None
    assert channel_edit_worker_op("") is None
    assert channel_edit_worker_op(None) is None


def test_all_worker_ops_distinct():
    vals = list(_CHANNEL_EDIT_OPS.values())
    assert len(vals) == len(set(vals))
    assert set(vals) == {"chan_title", "chan_about", "chan_uname"}
