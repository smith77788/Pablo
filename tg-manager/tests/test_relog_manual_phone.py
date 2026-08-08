"""Регрессия: релог аккаунта без сохранённого номера — ручной ввод, не тупик.

Баг (жалоба пользователя): «Релог → номер телефона не сохранён». Аккаунты,
импортированные сессией без phone, было НЕВОЗМОЖНО релогнуть — кнопка вела в тупик
(«Используйте обычный вход»). Фикс: если номера нет — просим ввести его, сохраняем
в эту же строку аккаунта и запускаем обычную отправку кода → сессия восстанавливается.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_relog_manual_phone_flow_wired():
    src = _read("bot/handlers/accounts.py")
    # новый FSM-стейт есть
    assert "waiting_relog_phone = State()" in src
    # cb_relog при отсутствии номера НЕ тупикует, а переходит в ввод номера
    relog = src[src.index("async def cb_relog_account"):src.index("async def _send_relog_code")]
    assert "AccountLogin.waiting_relog_phone" in relog
    assert "Используйте обычный вход" not in relog  # старый тупик убран
    # хендлер ввода номера: валидирует формат, сохраняет phone в аккаунт, шлёт код
    h = src[src.index("async def relog_phone_entered"):]
    h = h[:2000]
    assert "UPDATE tg_accounts SET phone=" in h
    assert "_send_relog_code" in h
    # Номер принимается в любом формате (пробелы, без плюса) и нормализуется
    # общим normalize_phone — жёсткий regex ^\+\d{7,15}$ отсекал валидные вводы.
    assert "normalize_phone" in h
    # общий помощник отправки кода существует и переводит в waiting_code
    assert "async def _send_relog_code" in src
    helper = src[src.index("async def _send_relog_code"):src.index("async def relog_phone_entered")]
    assert "AccountLogin.waiting_code" in helper and "start_login(phone)" in helper
