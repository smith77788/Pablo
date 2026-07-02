"""Регресс-тесты на настройки, разблокирующие разделы (AI-ключи, кошельки).

Гарантируют приоритет БД-override над env и корректный fallback — иначе
разделы «не исполняются из-за отсутствия настроек».
"""
from __future__ import annotations

import os


def test_ai_key_override_activates_provider(monkeypatch):
    from services import ai_providers as ap
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    ap.set_ai_keys({"OPENROUTER_API_KEY": ""})  # очистить override
    assert all(p.name != "openrouter" for p in ap.configured_providers())

    ap.set_ai_keys({"OPENROUTER_API_KEY": "sk-test-123"})
    names = [p.name for p in ap.configured_providers()]
    assert "openrouter" in names

    # снятие override возвращает провайдера в «не настроен»
    ap.set_ai_keys({"OPENROUTER_API_KEY": ""})
    assert all(p.name != "openrouter" for p in ap.configured_providers())


def test_ai_key_env_fallback(monkeypatch):
    from services import ai_providers as ap
    ap.set_ai_keys({"GROQ_API_KEY": ""})  # нет override
    monkeypatch.setenv("GROQ_API_KEY", "env-groq-key")
    assert any(p.name == "groq" for p in ap.configured_providers())


def test_ai_override_beats_env(monkeypatch):
    from services import ai_providers as ap
    monkeypatch.setenv("GEMINI_API_KEY", "env-gemini")
    ap.set_ai_keys({"GEMINI_API_KEY": "db-gemini"})
    prov = next(p for p in ap.configured_providers() if p.name == "gemini")
    assert prov.api_key == "db-gemini"  # БД имеет приоритет
    ap.set_ai_keys({"GEMINI_API_KEY": ""})


def test_payment_wallet_override(monkeypatch):
    from bot.handlers import subscription as sub
    monkeypatch.delenv("TRON_WALLET", raising=False)
    sub.set_pay_config({"TRON_WALLET": ""})
    assert sub._tron_wallet() == ""

    sub.set_pay_config({"TRON_WALLET": "TXyzWallet1234567890"})
    assert sub._tron_wallet() == "TXyzWallet1234567890"

    # env-fallback
    sub.set_pay_config({"TRON_WALLET": ""})
    monkeypatch.setenv("TRON_WALLET", "TEnvWallet999")
    assert sub._tron_wallet() == "TEnvWallet999"


def test_token_vault_roundtrip():
    from services.token_vault import encrypt_token, decrypt_token
    secret = "SECRET-API-KEY-42"
    enc = encrypt_token(secret)
    assert enc.startswith("ENC:")
    assert enc != secret
    assert decrypt_token(enc) == secret
    # legacy plaintext проходит без изменений
    assert decrypt_token("plain-legacy") == "plain-legacy"
    # повторное шифрование не двойное
    assert encrypt_token(enc) == enc
