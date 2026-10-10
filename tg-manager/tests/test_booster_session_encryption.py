"""Регрессия: booster_sessions секреты (session_str, proxy) шифруются at-rest.

Примечание: booster_sessions db-функции сейчас не имеют вызовов в коде (слой не
подключён) — тест защищает будущее подключение от plaintext-регрессии + проверяет
симметрию encrypt(write)/decrypt(read) через инспекцию исходника.
"""
from __future__ import annotations

import inspect


def test_bb_add_session_encrypts_secrets():
    from database import db

    src = inspect.getsource(db.bb_add_session)
    assert "encrypt_token(session_str)" in src, "session_str пишется без шифрования"
    assert "encrypt_token(proxy)" in src, "proxy пишется без шифрования"


def test_bb_read_functions_decrypt():
    from database import db

    for fn in (db.bb_get_session, db.bb_get_session_str, db.bb_get_sessions):
        src = inspect.getsource(fn)
        assert "decrypt_token" in src, f"{fn.__name__} не расшифровывает секреты"
