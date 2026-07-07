"""Регрессия: proxy_url шифруется at-rest, но все листья потребления работают.

proxy_url — межтабличный ключ (UNIQUE user_proxies, PK infra_memory_proxies,
изоляция по IP). Недетерминированный шифр не должен ломать: подключение
(_parse_proxy), извлечение IP (extract_ip_from_proxy), ключи памяти
(infra_memory record/get), дедуп (proxy_fp), отображение.
"""
from __future__ import annotations

import inspect

PROXY = "socks5://user:pass@203.0.113.7:1080"


def test_proxy_fingerprint_deterministic_and_encryption_agnostic():
    from services.token_vault import encrypt_token, proxy_fingerprint

    fp = proxy_fingerprint(PROXY)
    assert fp == proxy_fingerprint(PROXY) and len(fp) == 64
    # fp шифра == fp plaintext → дедуп по proxy_fp работает после шифрования
    assert proxy_fingerprint(encrypt_token(PROXY)) == fp
    assert proxy_fingerprint("socks5://other:1080") != fp


def test_parse_proxy_decrypts_encrypted_url():
    from services import account_manager
    from services.token_vault import encrypt_token

    enc = encrypt_token(PROXY)
    parsed = account_manager._parse_proxy(enc)
    assert parsed is not None
    # (socks.SOCKS5, host, port, True, user, pass)
    assert parsed[1] == "203.0.113.7"
    assert parsed[2] == 1080
    assert parsed[4] == "user" and parsed[5] == "pass"
    # legacy plaintext тоже парсится
    assert account_manager._parse_proxy(PROXY)[1] == "203.0.113.7"


def test_extract_ip_decrypts_before_regex():
    from services.proxy_selector import extract_ip_from_proxy
    from services.token_vault import encrypt_token

    # изоляция: IP извлекается из зашифрованного proxy_url
    assert extract_ip_from_proxy(encrypt_token(PROXY)) == "203.0.113.7"
    # legacy plaintext
    assert extract_ip_from_proxy(PROXY) == "203.0.113.7"


def test_infra_memory_key_normalized_to_plaintext():
    """record(зашифр) и get(зашифр) должны попадать в один plaintext-ключ."""
    from services import infra_memory
    from services.token_vault import encrypt_token

    infra_memory._proxy_memory.clear()
    # записываем по зашифрованному, читаем по другому шифротексту того же прокси
    enc1 = encrypt_token(PROXY)
    enc2 = encrypt_token(PROXY)
    assert enc1 != enc2  # недетерминирован
    for _ in range(10):
        infra_memory.record_proxy_op(enc1, "join", success=True)
    # ключ в памяти — plaintext (один), не два шифротекста
    keys = [k for k in infra_memory._proxy_memory if k[1] == "join"]
    assert keys == [(PROXY, "join")], keys
    # get по другому шифротексту находит ту же запись
    score_enc = infra_memory.get_proxy_score(enc2, "join")
    score_plain = infra_memory.get_proxy_score(PROXY, "join")
    assert score_enc == score_plain > 0.5
    infra_memory._proxy_memory.clear()


def test_write_paths_encrypt_and_use_proxy_fp():
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in ("services/mini_app_api.py", "bot/handlers/proxy_manager.py"):
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            src = f.read()
        assert "encrypt_token(proxy_url)" in src, f"{rel}: proxy_url пишется без шифрования"
        assert "proxy_fp" in src, f"{rel}: нет proxy_fp для дедупа"
        assert "ON CONFLICT(owner_id, proxy_fp)" in src or "ON CONFLICT (owner_id, proxy_fp)" in src, (
            f"{rel}: ON CONFLICT всё ещё по proxy_url (сломается на недетерм. шифре)"
        )


def test_db_maintenance_no_sql_equality_join_on_encrypted():
    from services import db_maintenance

    src = inspect.getsource(db_maintenance)
    # старый SQL-join user_proxies.proxy_url = infra_memory_proxies.proxy_url сломан шифром
    assert "proxy_url = infra_memory_proxies.proxy_url" not in src, (
        "db_maintenance всё ещё сравнивает зашифрованный proxy_url через SQL-equality"
    )
    assert "decrypt_token" in src, "db_maintenance должен расшифровывать user-прокси перед сверкой"
