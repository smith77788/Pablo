"""Регрессия: сессии шифруются at-rest, но остаются рабочими.

Закрывает разрыв «plaintext session_str в tg_accounts» (см. CLAUDE.md). Ключевые
инварианты:
  - encrypt→decrypt round-trip корректен;
  - fingerprint детерминирован и одинаков для plaintext и его шифра (дедуп);
  - _make_client — единственная точка потребления — расшифровывает сессию;
  - legacy plaintext по-прежнему работает (passthrough) — миграция ленивая.
"""
from __future__ import annotations

import inspect

import pytest

try:
    from Crypto.Cipher import AES  # noqa: F401
    _has_crypto = True
except (ImportError, OSError):
    _has_crypto = False

if _has_crypto:
    try:
        from services.token_vault import encrypt_token
        encrypt_token("probe")
    except Exception:
        _has_crypto = False

pytestmark = pytest.mark.skipif(not _has_crypto, reason="pycryptodome native module not available")


def test_encrypt_decrypt_roundtrip():
    from services.token_vault import encrypt_token, decrypt_token

    plain = "1BVtsOMAB1234567890abcdefSESSIONxyz"
    enc = encrypt_token(plain)
    assert enc.startswith("ENC:")
    assert enc != plain
    assert decrypt_token(enc) == plain


def test_fingerprint_deterministic_and_encryption_agnostic():
    from services.token_vault import encrypt_token, session_fingerprint

    plain = "SESSION_ABC_123"
    fp1 = session_fingerprint(plain)
    fp2 = session_fingerprint(plain)
    assert fp1 == fp2 and len(fp1) == 64  # sha256 hex, стабилен

    # fingerprint шифра == fingerprint plaintext → дедуп работает после шифрования
    enc = encrypt_token(plain)
    assert session_fingerprint(enc) == fp1

    # разные сессии → разные fingerprint
    assert session_fingerprint("OTHER_SESSION") != fp1
    assert session_fingerprint("") == ""


def test_encryption_is_nondeterministic():
    # именно поэтому нужен отдельный fingerprint, а не дедуп по шифротексту
    from services.token_vault import encrypt_token

    a = encrypt_token("SAME")
    b = encrypt_token("SAME")
    assert a != b  # случайный nonce


def test_make_client_decrypts_session(monkeypatch):
    """_make_client должен расшифровать сессию перед StringSession и
    пропустить legacy-plaintext без изменений."""
    from services import account_manager
    from services.token_vault import encrypt_token
    import telethon.sessions as ts

    captured: dict[str, object] = {}

    class _CapSession:
        def __init__(self, s):
            captured["session"] = s

        def __getattr__(self, _n):
            return lambda *a, **k: None

    monkeypatch.setattr(ts, "StringSession", _CapSession, raising=False)
    monkeypatch.setattr(account_manager, "_resolve_client_proxy", lambda d, low_risk=False: None)
    monkeypatch.setattr(account_manager, "_get_pool_proxy_url", lambda: None, raising=False)
    monkeypatch.setattr(account_manager, "CF_RELAY_URL", "", raising=False)
    monkeypatch.setattr(
        account_manager,
        "_normalize_device_profile",
        lambda d: {
            "device_model": "x",
            "system_version": "x",
            "app_version": "x",
            "lang_code": "en",
            "system_lang_code": "en",
        },
    )

    # зашифрованная сессия → StringSession получает PLAINTEXT
    account_manager._make_client(encrypt_token("PLAINTEXT_SESSION_XYZ"), None)
    assert captured["session"] == "PLAINTEXT_SESSION_XYZ"

    # legacy plaintext → passthrough (без миграции)
    account_manager._make_client("LEGACY_PLAIN_SESSION", None)
    assert captured["session"] == "LEGACY_PLAIN_SESSION"


def test_importer_dedups_by_fingerprint_not_ciphertext():
    """session_importer не должен дедупить только по равенству session_str —
    под недетерминированным шифром это давало бы дубликаты."""
    from services import session_importer

    src = inspect.getsource(session_importer)
    assert "session_fp" in src, "импортёр должен дедупить по session_fp"
    assert "encrypt_token(session_str)" in src, "импортёр должен шифровать сессию на вставке"


def test_add_tg_account_encrypts_before_store():
    from database import db

    src = inspect.getsource(db.add_tg_account)
    assert "encrypt_token(session_str)" in src, "add_tg_account должен шифровать session_str"
    assert "session_fp" in src, "add_tg_account должен писать session_fp для дедупа"


def test_all_session_write_paths_encrypt():
    """Все точки записи tg_accounts.session_str шифруют — иначе re-auth/регистрация
    молча вернут plaintext, обнулив шифрование. Файловый guard против регрессии."""
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in (
        "bot/handlers/accounts.py",
        "bot/handlers/auto_registrar.py",
        "services/session_importer.py",
    ):
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            src = f.read()
        # у каждого файла, пишущего session_str, должен быть encrypt_token
        if "session_str=$" in src or "session_str, " in src:
            assert "encrypt_token(" in src, f"{rel}: пишет session_str без encrypt_token"
            assert "session_fp" in src, f"{rel}: пишет session_str без session_fp"
