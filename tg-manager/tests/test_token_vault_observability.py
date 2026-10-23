"""token_vault: сбой расшифровки ENC:-строки не должен быть тихим.

Неверный/ротированный TOKEN_ENCRYPTION_KEY роняет расшифровку всех сессий/токенов/
прокси. Раньше decrypt_token молча возвращал шифротекст → весь флот «мёртв» без
причины в логах. Проверяем: legacy-plaintext по-прежнему тих, а провал расшифровки
ПОМЕЧЕННОЙ строки логируется (с троттлингом) и НЕ бросает.
"""
from __future__ import annotations

import logging

import pytest

from services import token_vault as tv


def test_plaintext_passthrough_is_silent(caplog):
    """Legacy plaintext (без маркера ENC:) — тихий passthrough, без warning."""
    with caplog.at_level(logging.WARNING, logger="services.token_vault"):
        assert tv.decrypt_token("1234:ABC-plain-token") == "1234:ABC-plain-token"
        assert tv.decrypt_token("") == ""
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_empty_is_silent(caplog):
    with caplog.at_level(logging.WARNING, logger="services.token_vault"):
        assert tv.decrypt_token(None) is None  # type: ignore[arg-type]
    assert not caplog.records


def test_marked_but_corrupt_logs_and_returns_raw(caplog):
    """ENC:-помеченная, но битая строка → warning + возврат исходного (без throw)."""
    tv._last_decrypt_warn = 0.0  # сбросить троттл для детерминизма
    bad = tv._MARKER + "not-valid-base64-or-ciphertext!!!"
    with caplog.at_level(logging.WARNING, logger="services.token_vault"):
        out = tv.decrypt_token(bad)
    assert out == bad  # данные не теряем
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warns) == 1, "сбой расшифровки ENC: должен логироваться"
    # секрет не утёк в текст сообщения
    assert "not-valid-base64" not in warns[0].getMessage()


def test_warning_is_throttled(caplog):
    """Троттл: подряд идущие сбои не заливают лог (1/мин)."""
    tv._last_decrypt_warn = 0.0
    bad = tv._MARKER + "garbage"
    with caplog.at_level(logging.WARNING, logger="services.token_vault"):
        for _ in range(5):
            tv.decrypt_token(bad)
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warns) == 1, "троттл должен ограничить до одного сообщения"


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("Crypto") is None,
    reason="pycryptodome недоступен (round-trip проверяется в CI)",
)
def test_roundtrip_encrypt_decrypt(monkeypatch):
    """Корректная строка: encrypt→decrypt возвращает оригинал, без warning."""
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "unit-test-key-123")
    secret = "1234567890:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    enc = tv.encrypt_token(secret)
    assert enc.startswith(tv._MARKER) and enc != secret
    assert tv.decrypt_token(enc) == secret
