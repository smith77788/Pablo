"""Слабый ключ шифрования не должен применяться молча.

TOKEN_ENCRYPTION_KEY шифрует строки сессий, токены ботов и прокси. Запасные
варианты опасны каждый по-своему: ключ из токена бота ломается при смене
токена, а ключ по умолчанию лежит в исходниках, то есть шифрования нет вовсе.
Раньше оба варианта включались без единой строки в логе.
"""
from __future__ import annotations

import importlib
import logging

import pytest


@pytest.fixture
def vault(monkeypatch):
    def _load(**env):
        for k in ("TOKEN_ENCRYPTION_KEY", "MANAGER_BOT_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        import services.token_vault as tv
        return importlib.reload(tv)
    return _load


def test_explicit_key_is_silent(vault, caplog):
    tv = vault(TOKEN_ENCRYPTION_KEY="proper-secret-key")
    with caplog.at_level(logging.WARNING):
        tv._key()
    assert not caplog.records


def test_bot_token_fallback_warns_once(vault, caplog):
    tv = vault(MANAGER_BOT_TOKEN="1234567890:AAH" + "x" * 32)
    with caplog.at_level(logging.WARNING):
        tv._key()
        tv._key()
        tv._key()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "предупреждение не должно засорять лог"
    assert "TOKEN_ENCRYPTION_KEY" in warnings[0].getMessage()


def test_default_key_is_an_error_not_a_warning(vault, caplog):
    tv = vault()
    with caplog.at_level(logging.WARNING):
        tv._key()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "НЕ ЗАШИФРОВАНЫ" in errors[0].getMessage()


def test_roundtrip_still_works_on_every_key_source(vault):
    for env in ({"TOKEN_ENCRYPTION_KEY": "k1"},
                {"MANAGER_BOT_TOKEN": "1:t"},
                {}):
        tv = vault(**env)
        enc = tv.encrypt_token("1234567890:AAHsecretsecretsecret")
        assert enc.startswith("ENC:")
        assert tv.decrypt_token(enc) == "1234567890:AAHsecretsecretsecret"
