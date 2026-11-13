"""Блок «Безопасность аккаунта» в карточке: 2FA, запасной ключ, чужие сеансы.

Запасной ключ (второй наш сеанс, оживляет аккаунт при смерти основной сессии)
управлялся только флот-широким тумблером щита — по конкретному аккаунту не было
видно, защищён он или нет. Карточка также не различала «2FA установлен» и «пароль
2FA сохранён у нас» (без сохранённого аккаунт не вернуть).
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_detail_returns_security_block():
    src = _src("services/mini_app_accounts.py")
    body = src[src.index("async def account_detail("):]
    body = body[:body.index("async def accounts_export(")]
    assert '"security": security' in body, "account_detail не отдаёт блок security"
    # Три сигнала выживаемости + статус автозащиты.
    assert "twofa_saved" in body and "has_reserve" in body
    assert "reserve_session_enc IS NOT NULL" in body, "признак запасного ключа читается из базы"
    assert "get_shield_config" in body, "статус автозащиты щита должен попадать в карточку"


def test_card_renders_security_signals():
    html = _src("mini_app/index.html")
    card = html[html.index("function buildAccDetail("):]
    card = card[:card.index("\nfunction ", 1)]
    assert "securityHtml" in card and "${securityHtml}" in card, "блок безопасности не вставлен в карточку"
    assert "Запасной ключ" in card, "нет сигнала о запасном ключе"
    assert "2FA" in card
    # Чужие сеансы в блоке безопасности ведут в экран сеансов.
    assert "openAccSessions(" in card


def test_security_warns_when_2fa_password_not_saved():
    html = _src("mini_app/index.html")
    card = html[html.index("function buildAccDetail("):]
    card = card[:card.index("\nfunction ", 1)]
    # Опасное состояние: 2FA есть, но пароль у нас не сохранён — аккаунт не вернуть.
    assert "пароль НЕ сохранён" in card or "пароль не сохранён" in card.lower(), (
        "карточка должна предупреждать о 2FA без сохранённого пароля"
    )
