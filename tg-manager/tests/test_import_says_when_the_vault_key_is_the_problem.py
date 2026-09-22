"""Импорт сессий называет отказ шифрования своим именем, и только один раз.

`encrypt_token` стоял ВНУТРИ того же try, что и запись в базу, а except
подписывал любой отказ как «ошибка БД». Значит проблема с хранилищем секретов
— ключа нет, ключ сменили, библиотека шифрования не встала — приходила
владельцу как проблема с базой. Он идёт чинить базу, а дело в ключе.

Причина при этом одна на всю партию: если шифрование не работает, оно не
заработает ни на второй строке, ни на пятисотой. Владелец получал пятьсот
одинаковых неверных сообщений подряд, и настоящую причину в этом потоке было
не разглядеть.

Теперь шифрование проверяется отдельно от записи, называется прямо, и импорт
останавливается на первой же такой строке: остальные считаются необработанными
и об этом сказано.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from services import session_importer as SI


class _Pool:
    def __init__(self):
        self.inserted = []

    async def fetchval(self, q, *a):
        return 1

    async def fetchrow(self, q, *a):
        return None

    async def fetch(self, q, *a):
        return []

    async def execute(self, q, *a):
        self.inserted.append(a)
        return "INSERT 0 1"


@pytest.fixture
def _five_good_sessions(monkeypatch):
    async def _ok(sess, proxy_url=None, timeout_s=None):
        return {"valid": True, "phone": "+79990000000", "user_id": 1,
                "first_name": "И", "username": "u"}

    monkeypatch.setattr(SI, "validate_session", _ok)
    monkeypatch.setattr(SI, "detect_format", lambda d: "string")
    monkeypatch.setattr(SI, "extract_session_string", lambda d, f: d)
    return "\n".join(f"sess{i}" for i in range(5))


@pytest.fixture
def _working_vault():
    """Рабочее шифрование. В этой песочнице нет библиотеки Crypto, а проверять
    надо поведение импортёра, а не наличие пакета."""
    with patch("services.token_vault.encrypt_token", side_effect=lambda t: "ENC:" + t):
        yield


def _import(raw, pool=None):
    return asyncio.run(SI.import_sessions(pool or _Pool(), 1, raw))


def test_a_broken_vault_is_not_called_a_database_error(_five_good_sessions):
    with patch("services.token_vault.encrypt_token",
               side_effect=RuntimeError("ключ шифрования не задан")):
        res = _import(_five_good_sessions)

    assert res["imported"] == 0
    text = " ".join(res["errors"]).lower()
    assert "ошибка бд" not in text, (
        f"отказ шифрования подписан как проблема с базой: {res['errors']}"
    )
    assert "зашифров" in text, res["errors"]
    assert "ключ" in text, "владельцу не сказано, где искать причину"


def test_the_same_cause_is_reported_once_not_five_hundred_times(_five_good_sessions):
    with patch("services.token_vault.encrypt_token",
               side_effect=RuntimeError("ключ шифрования не задан")):
        res = _import(_five_good_sessions)

    encryption_errors = [e for e in res["errors"] if "зашифров" in e.lower()]
    assert len(encryption_errors) == 1, (
        f"одна и та же причина повторена {len(encryption_errors)} раз: "
        f"{res['errors']}"
    )
    assert res["failed"] == 5, "необработанные строки обязаны быть посчитаны"
    assert "не обработано" in " ".join(res["errors"])


def test_a_real_database_failure_is_still_a_database_failure(
        _five_good_sessions, _working_vault):
    class _BrokenPool(_Pool):
        async def execute(self, q, *a):
            raise RuntimeError("соединение с базой потеряно")

    res = _import(_five_good_sessions, _BrokenPool())

    assert res["imported"] == 0
    assert all("ошибка БД" in e for e in res["errors"] if "Строка" in e), res["errors"]
    assert len([e for e in res["errors"] if "ошибка БД" in e]) == 5, (
        "сбой базы у каждой строки свой — останавливаться на первом нельзя"
    )


def test_a_healthy_import_is_untouched(_five_good_sessions, _working_vault):
    pool = _Pool()
    res = _import(_five_good_sessions, pool)
    assert res["imported"] == 5 and res["failed"] == 0
    assert len(pool.inserted) == 5
