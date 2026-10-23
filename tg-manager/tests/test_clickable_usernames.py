"""Юзернеймы/каналы кликабельны — ведут в Telegram.

Жалоба владельца: «не могу из приложения переходить ни по ссылкам на каналы,
ни по их юзернеймам». Карточка канала и карточка аккаунта должны открывать
Telegram по @username (openTgUser → tg.openTelegramLink).
"""
from __future__ import annotations

import pathlib

HTML = (pathlib.Path(__file__).resolve().parent.parent
        / "mini_app" / "index.html").read_text(encoding="utf-8")


def _window(marker_start: str, size: int = 16000) -> str:
    i = HTML.index(marker_start)
    return HTML[i:i + size]


def test_channel_card_username_is_clickable():
    # buildChDetail — карточка канала: @username и строка «Ссылка» ведут в Telegram
    block = _window("function buildChDetail(")
    assert block.count("openTgUser(") >= 2, (
        "в карточке канала юзернейм/ссылка не ведут в Telegram (openTgUser)")
    assert "↗ Открыть в Telegram" in block, "нет кнопки открытия канала в Telegram"


def test_account_card_username_is_clickable():
    block = _window("function buildAccDetail(")
    assert "openTgUser(" in block, (
        "в карточке аккаунта юзернейм не ведёт в Telegram")


def test_open_tg_link_helper_exists():
    assert "function openTgLink(" in HTML
    assert "function openTgUser(" in HTML
    # оба используют нативный переход Telegram
    assert HTML.count("openTelegramLink(url)") >= 2
