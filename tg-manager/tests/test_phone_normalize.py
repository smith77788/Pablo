"""Номер телефона при добавлении аккаунта — принимается в ЛЮБОМ формате.

Форма «Добавить аккаунт → Номер» требовала строго +71234567890 и молча
отвергала пробелы/скобки/дефисы/отсутствие «+» — хотя пользователь набирает
номер как привык («+7 932 726 5344»). Нормализуем к +<цифры> и на фронте, и на
бэкенде (один и тот же алгоритм — иначе confirm_code не совпадёт со start).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
BOT_ACC = (ROOT / "bot" / "handlers" / "accounts.py").read_text(encoding="utf-8")

from services.phone_utils import normalize_phone  # лёгкий модуль без тяжёлых зависимостей


def test_normalize_accepts_any_format():
    assert normalize_phone("+7 932 726 5344") == "+79327265344"
    assert normalize_phone("7 932-726-5344") == "+79327265344"      # без плюса
    assert normalize_phone("+7 (495) 123-45-67") == "+74951234567" # скобки/дефисы
    assert normalize_phone("  +79327265344  ") == "+79327265344"    # пробелы по краям
    assert normalize_phone("79327265344") == "+79327265344"


def test_normalize_rejects_garbage():
    assert normalize_phone("123") is None          # слишком коротко
    assert normalize_phone("abcdef") is None       # нет цифр
    assert normalize_phone("") is None
    assert normalize_phone("1" * 16) is None        # слишком длинно


def test_one_canonical_normalizer():
    """Мини-апп и бот зовут ОДИН нормализатор — иначе валидации разъедутся."""
    assert "from services.phone_utils import normalize_phone" in API
    assert "from services.phone_utils import normalize_phone" in BOT_ACC


def test_backend_endpoints_use_normalizer():
    """И start, и code-подтверждение обязаны нормализовать одинаково."""
    assert API.count("normalize_phone(") >= 2, "нормализатор не подключён к обоим шагам"
    # Строгую проверку «должен начинаться с +» убрали — она и отвергала формат.
    assert 'not phone.startswith("+") or len(phone) < 8' not in API


def test_bot_add_phone_accepts_any_format():
    """FSM бота (добавление/релог по номеру) тоже нормализует, а не отвергает."""
    assert BOT_ACC.count("normalize_phone(") >= 2, "бот не нормализует ввод номера"
    # Жёсткие регулярки-отказа на пользовательском вводе убраны.
    assert 'phone = (message.text or "").strip()\n\n    if not re.match(r"^\\+\\d{7,15}$", phone)' not in BOT_ACC


def test_frontend_normalizes_before_send():
    """Фронт нормализует до отправки И сохраняет нормализованный номер для шага
    подтверждения кода (иначе Telethon не свяжет код с номером)."""
    assert "function normalizePhone(" in HTML
    m = re.search(r"async function accPhoneStart\(\)\{.*?\n\}", HTML, re.S)
    assert m
    body = m.group(0)
    assert "normalizePhone(" in body, "accPhoneStart не нормализует ввод"
    # старую жёсткую регулярку-отказ убрали
    assert r"/^\+\d{7,15}$/.test(phone)" not in body
