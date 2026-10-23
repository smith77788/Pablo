"""Смена ключа шифрования замечается на старте, а не по одному аккаунту.

Все секреты продукта — строки сессий (tg_accounts, booster_sessions), прокси
(user_proxies), токены ботов (bot_warehouse), ключи SMM-панелей (smm_panels) —
лежат зашифрованными одним ключом из token_vault. Если ключ сменился,
расшифровка падает не сразу, а при первом обращении к каждой строке: аккаунты
по одному перестают подключаться, прокси «не работают», и в логе остаётся лишь
троттленное предупреждение token_vault раз в минуту. Со стороны это выглядит
как отказ Telegram, а не как ошибка конфигурации.

Сменить ключ незаметно легче всего, когда TOKEN_ENCRYPTION_KEY не задан: тогда
ключ выводится из токена бота, и обычная ротация токена бота обесценивает всё
хранилище.

Канарейка — строка, зашифрованная тем же ключом и сохранённая рядом с
секретами. Каждый старт её расшифровывает и, если не выходит, говорит об этом
прямо, со списком того, что стало нечитаемым.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from database import db
from services import token_vault


def _run(coro):
    return asyncio.run(coro)


class _Conn:
    """Соединение-заглушка с одной строкой vault_key_check."""

    def __init__(self, canary: str | None = None, table_missing: bool = False):
        self.canary = canary
        self.table_missing = table_missing
        self.executed: list[str] = []

    async def execute(self, q, *a):
        self.executed.append(" ".join(q.split()))
        if "INSERT INTO vault_key_check" in q:
            self.canary = a[0]
        return None

    async def fetchrow(self, q, *a):
        if self.table_missing:
            raise RuntimeError('relation "vault_key_check" does not exist')
        if self.canary is None:
            return None
        return {"canary": self.canary}


def _with_key(key: str):
    """Подменить ключ шифрования на время одного вызова."""
    return _KeyCtx(key)


class _KeyCtx:
    def __init__(self, key):
        self.key = key

    def __enter__(self):
        self.prev = os.environ.get("TOKEN_ENCRYPTION_KEY")
        os.environ["TOKEN_ENCRYPTION_KEY"] = self.key
        return self

    def __exit__(self, *a):
        if self.prev is None:
            os.environ.pop("TOKEN_ENCRYPTION_KEY", None)
        else:
            os.environ["TOKEN_ENCRYPTION_KEY"] = self.prev
        return False


def test_first_start_saves_canary():
    conn = _Conn(canary=None)
    with _with_key("ключ-один"):
        assert _run(db.verify_vault_key(conn)) == "initialized"
    assert conn.canary and conn.canary.startswith("ENC:"), "канарейка обязана быть зашифрована"


def test_same_key_passes():
    with _with_key("ключ-один"):
        conn = _Conn(canary=token_vault.encrypt_token(db._VAULT_CANARY_PLAIN))
        assert _run(db.verify_vault_key(conn)) == "ok"


def test_changed_key_is_detected():
    """Главный случай: секреты шифровали одним ключом, стартуем с другим."""
    with _with_key("ключ-один"):
        stored = token_vault.encrypt_token(db._VAULT_CANARY_PLAIN)
    with _with_key("ключ-другой"):
        conn = _Conn(canary=stored)
        assert _run(db.verify_vault_key(conn)) == "changed"


def test_changed_key_message_names_what_broke(caplog):
    with _with_key("ключ-один"):
        stored = token_vault.encrypt_token(db._VAULT_CANARY_PLAIN)
    with _with_key("ключ-другой"):
        conn = _Conn(canary=stored)
        with caplog.at_level("ERROR"):
            _run(db.verify_vault_key(conn))
    text = caplog.text
    for tbl in ("tg_accounts", "user_proxies", "bot_warehouse", "smm_panels"):
        assert tbl in text, f"в сообщении не названа таблица {tbl}"
    assert "TOKEN_ENCRYPTION_KEY" in text, "не сказано, что именно вернуть"


def test_canary_never_stored_in_plaintext():
    """В базу уходит шифротекст, а не сама строка — иначе проверка ничего не проверяет."""
    conn = _Conn(canary=None)
    with _with_key("ключ-один"):
        _run(db.verify_vault_key(conn))
    assert db._VAULT_CANARY_PLAIN not in conn.canary


def test_strict_mode_refuses_to_start():
    with _with_key("ключ-один"):
        stored = token_vault.encrypt_token(db._VAULT_CANARY_PLAIN)
    with _with_key("ключ-другой"):
        conn = _Conn(canary=stored)
        with pytest.raises(db.SchemaMigrationError):
            _run(db.verify_vault_key(conn, strict=True))


def test_missing_table_does_not_block_startup():
    """Миграция ещё не легла — самопроверка молчит, но старт не ломает."""
    conn = _Conn(canary=None, table_missing=True)
    with _with_key("ключ-один"):
        assert _run(db.verify_vault_key(conn)) == "skipped"


def test_table_is_created_by_a_migration():
    """Таблицу заводит миграция, а не код при первом обращении.

    Создатель «по требованию» не оставляет следа в схеме: перестали его звать —
    и раздел молча исчезает. В этом репозитории на таком приёме уже терялся
    модуль «Воркфлоу».
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    files = [f for f in root.glob("schema*.sql")
             if "CREATE TABLE IF NOT EXISTS vault_key_check" in f.read_text(encoding="utf-8")]
    assert files, "нет миграции, создающей vault_key_check"

    import inspect

    src = inspect.getsource(db.verify_vault_key)
    assert "CREATE TABLE" not in src, "таблица создаётся в коде в обход миграции"


def test_legacy_plaintext_canary_is_not_mistaken_for_valid():
    """Строка без метки ENC: — не доказательство ключа.

    decrypt_token возвращает такое значение как есть (совместимость со старыми
    незашифрованными записями), поэтому канарейка, записанная в открытом виде,
    «совпала» бы при ЛЮБОМ ключе и проверка стала бы пустой.
    """
    conn = _Conn(canary="что-то-не-то")
    with _with_key("ключ-один"):
        assert _run(db.verify_vault_key(conn)) == "changed"
