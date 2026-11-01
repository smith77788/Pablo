"""Три дыры из разбора безопасности (разрыв с лидерами, 31.10.2026).

1. Ключ шифрования нельзя было сменить: расшифровка знала только текущий
   ключ, и новый TOKEN_ENCRYPTION_KEY превращал все сессии в мусор разом.
   Теперь прежние ключи живут в TOKEN_ENCRYPTION_KEYS_PREVIOUS на чтение, а
   ключ из токена бота остаётся в связке, когда задают отдельный ключ.
2. Бэкап уходил в Telegram открытым tar.gz: телефоны, аудитории и переписка
   всех владельцев читались из одного сообщения. Теперь архив шифруется.
3. Вход в админку по секретной фразе: любая длина, любой чат, никакого
   сигнала владельцу. Теперь короткая фраза не принимается, вход только в
   личке, постоянные админы узнают о выдаче.
"""
from __future__ import annotations

import asyncio
import importlib
import io
import os
import tarfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VAULT_ENV = ("TOKEN_ENCRYPTION_KEY", "TOKEN_ENCRYPTION_KEYS_PREVIOUS",
              "MANAGER_BOT_TOKEN", "DB_BACKUP_KEY", "DB_BACKUP_ENCRYPT")


@pytest.fixture
def env(monkeypatch):
    def _set(**values):
        for k in _VAULT_ENV:
            monkeypatch.delenv(k, raising=False)
        for k, v in values.items():
            monkeypatch.setenv(k, v)
        import services.token_vault as tv
        return importlib.reload(tv)
    yield _set
    for k in _VAULT_ENV:
        monkeypatch.delenv(k, raising=False)
    import services.token_vault as tv
    importlib.reload(tv)


# ── 1. Связка ключей ─────────────────────────────────────────────────────────
def test_previous_key_still_decrypts_after_rotation(env):
    tv = env(TOKEN_ENCRYPTION_KEY="old-key-0123456789")
    enc = tv.encrypt_token("session-string")
    tv = env(TOKEN_ENCRYPTION_KEY="new-key-0123456789",
             TOKEN_ENCRYPTION_KEYS_PREVIOUS="old-key-0123456789")
    assert tv.decrypt_token(enc) == "session-string"
    assert tv.needs_reencrypt(enc) is True


def test_without_previous_key_old_value_is_unreadable(env):
    tv = env(TOKEN_ENCRYPTION_KEY="old-key-0123456789")
    enc = tv.encrypt_token("session-string")
    tv = env(TOKEN_ENCRYPTION_KEY="new-key-0123456789")
    # Прежнее поведение при неверном ключе сохраняется: шифротекст как есть.
    assert tv.decrypt_token(enc) == enc
    assert tv.needs_reencrypt(enc) is False


def test_reencrypt_moves_value_to_current_key(env):
    tv = env(TOKEN_ENCRYPTION_KEY="old-key-0123456789")
    enc = tv.encrypt_token("proxy://u:p@h:1")
    tv = env(TOKEN_ENCRYPTION_KEY="new-key-0123456789",
             TOKEN_ENCRYPTION_KEYS_PREVIOUS="old-key-0123456789")
    rotated = tv.reencrypt_token(enc)
    assert rotated != enc and rotated.startswith("ENC:")
    assert tv.needs_reencrypt(rotated) is False
    # После ротации прежний ключ можно убрать — значение читается новым.
    tv = env(TOKEN_ENCRYPTION_KEY="new-key-0123456789")
    assert tv.decrypt_token(rotated) == "proxy://u:p@h:1"


def test_reencrypt_never_touches_current_plain_or_unreadable(env):
    tv = env(TOKEN_ENCRYPTION_KEY="key-0123456789abc")
    current = tv.encrypt_token("x")
    assert tv.reencrypt_token(current) == current
    assert tv.reencrypt_token("plaintext") == "plaintext"
    assert tv.reencrypt_token("") == ""
    assert tv.reencrypt_token("ENC:bm90LWdjbQ==") == "ENC:bm90LWdjbQ=="


def test_setting_explicit_key_keeps_bot_token_key_readable(env):
    """Переход с «ключа из токена бота» на отдельный ключ не теряет данные."""
    bot = "1234567890:AAH" + "x" * 32
    tv = env(MANAGER_BOT_TOKEN=bot)
    enc = tv.encrypt_token("legacy-session")
    tv = env(MANAGER_BOT_TOKEN=bot, TOKEN_ENCRYPTION_KEY="separate-key-0123456789")
    assert tv.decrypt_token(enc) == "legacy-session"
    assert tv.needs_reencrypt(enc) is True


def test_bytes_follow_the_key_ring(env):
    tv = env(TOKEN_ENCRYPTION_KEY="old-key-0123456789")
    blob = tv.encrypt_bytes(b"chunk")
    tv = env(TOKEN_ENCRYPTION_KEY="new-key-0123456789",
             TOKEN_ENCRYPTION_KEYS_PREVIOUS="old-key-0123456789")
    assert tv.decrypt_bytes(blob) == b"chunk"


def test_canary_is_rewritten_with_current_key_during_rotation(env):
    from database import db

    tv = env(TOKEN_ENCRYPTION_KEY="old-key-0123456789")
    old_canary = tv.encrypt_token(db._VAULT_CANARY_PLAIN)
    env(TOKEN_ENCRYPTION_KEY="new-key-0123456789",
        TOKEN_ENCRYPTION_KEYS_PREVIOUS="old-key-0123456789")

    class _Conn:
        def __init__(self):
            self.updates = []

        async def fetchrow(self, sql, *a):
            return {"canary": old_canary}

        async def execute(self, sql, *args):
            self.updates.append((sql, args))

    conn = _Conn()
    status = asyncio.run(db.verify_vault_key(conn))
    assert status == "ok"
    assert conn.updates and "UPDATE vault_key_check" in conn.updates[0][0]
    import services.token_vault as tv_now
    assert tv_now.needs_reencrypt(conn.updates[0][1][0]) is False


# ── 2. Шифрование бэкапа ─────────────────────────────────────────────────────
def _tar_gz() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        raw = b"id,phone\n1,+79990000000\n"
        info = tarfile.TarInfo("data/contacts.csv")
        info.size = len(raw)
        tar.addfile(info, io.BytesIO(raw))
        man = b'{"version": 1, "row_counts": {"contacts": 1}}'
        mi = tarfile.TarInfo("manifest.json")
        mi.size = len(man)
        tar.addfile(mi, io.BytesIO(man))
    return buf.getvalue()


def _bk():
    from services import db_backup
    return importlib.reload(db_backup)


def test_encrypted_archive_hides_data_and_round_trips(env):
    env(TOKEN_ENCRYPTION_KEY="key-0123456789abc")
    bk = _bk()
    plain = _tar_gz()
    enc = bk.encrypt_archive(plain)
    assert bk.is_encrypted_archive(enc)
    assert b"+7999" not in enc and plain not in enc
    manifest, tables = bk.parse_archive(enc)
    assert tables["contacts"].endswith(b"+79990000000\n")
    assert manifest["row_counts"]["contacts"] == 1


def test_old_plain_archives_still_restore(env):
    env(TOKEN_ENCRYPTION_KEY="key-0123456789abc")
    bk = _bk()
    _m, tables = bk.parse_archive(_tar_gz())
    assert "contacts" in tables


def test_dedicated_backup_key_wins_and_wrong_key_is_a_clear_error(env):
    env(TOKEN_ENCRYPTION_KEY="key-0123456789abc", DB_BACKUP_KEY="backup-key-xyz-123")
    bk = _bk()
    enc = bk.encrypt_archive(_tar_gz())
    # Окружение потеряно, но DB_BACKUP_KEY сохранён у владельца — открывается.
    env(DB_BACKUP_KEY="backup-key-xyz-123")
    bk = _bk()
    assert bk.parse_archive(enc)[1]["contacts"]
    env(TOKEN_ENCRYPTION_KEY="another-key-0123456")
    bk = _bk()
    with pytest.raises(ValueError, match="не открывают"):
        bk.decrypt_archive(enc)


class _Bot:
    def __init__(self):
        self.docs = []

    async def send_document(self, chat_id, document, caption=None, **kw):
        self.docs.append((document.filename, document.data, caption))


def _acoro(v):
    async def _c(*a, **k):
        return v
    return _c()


def test_run_backup_once_sends_encrypted_file(env, monkeypatch):
    env(TOKEN_ENCRYPTION_KEY="key-0123456789abc")
    bk = _bk()
    plain = _tar_gz()
    monkeypatch.setattr(bk, "backup_chat_id", lambda: 7)
    monkeypatch.setattr(bk, "create_archive", lambda pool, **k: _acoro(plain))
    bot = _Bot()
    res = asyncio.run(bk.run_backup_once(object(), bot))
    assert res["ok"] is True
    name, data, caption = bot.docs[0]
    assert name.endswith(".tar.gz.enc")
    assert bk.is_encrypted_archive(data) and bk.decrypt_archive(data) == plain
    assert "Зашифрован" in caption


def test_encryption_failure_skips_backup_instead_of_sending_plain(env, monkeypatch):
    env(TOKEN_ENCRYPTION_KEY="key-0123456789abc")
    bk = _bk()
    monkeypatch.setattr(bk, "backup_chat_id", lambda: 7)
    monkeypatch.setattr(bk, "create_archive", lambda pool, **k: _acoro(b"arch"))

    def _boom(_a):
        raise RuntimeError("нет Crypto")
    monkeypatch.setattr(bk, "encrypt_archive", _boom)
    bot = _Bot()
    res = asyncio.run(bk.run_backup_once(object(), bot))
    assert res["ok"] is False and "шифрование" in res["error"]
    assert bot.docs == []


def test_encryption_can_be_switched_off_explicitly(env, monkeypatch):
    env(TOKEN_ENCRYPTION_KEY="key-0123456789abc", DB_BACKUP_ENCRYPT="0")
    bk = _bk()
    monkeypatch.setattr(bk, "backup_chat_id", lambda: 7)
    monkeypatch.setattr(bk, "create_archive", lambda pool, **k: _acoro(b"arch"))
    bot = _Bot()
    asyncio.run(bk.run_backup_once(object(), bot))
    assert bot.docs[0][1] == b"arch" and bot.docs[0][0].endswith(".tar.gz")


# ── 3. Вход по секретной фразе ───────────────────────────────────────────────
def _admin():
    from bot.handlers import admin
    return admin


def test_secret_phrase_requires_minimum_length():
    admin = _admin()
    short = "x" * (admin.ADMIN_SECRET_MIN_LEN - 1)
    assert admin.admin_secret_matches(short, short) is False
    good = "correct horse battery"
    assert admin.admin_secret_matches(good, good) is True
    assert admin.admin_secret_matches(good + " ", good) is False
    assert admin.admin_secret_matches("", good) is False
    assert admin.admin_secret_matches("неверная фраза ок", good) is False
    assert admin.admin_secret_matches(good, "") is False


def test_secret_phrase_handler_is_private_chat_only_and_notifies():
    src = open(os.path.join(ROOT, "bot", "handlers", "admin.py"), encoding="utf-8").read()
    head = src[src.index("async def cmd_admin_secret") - 200:src.index("async def cmd_admin_secret")]
    assert 'F.chat.type == "private"' in head, "фраза принимается не только в личке"
    assert "t == ADMIN_SECRET" not in src, "вернулось прямое сравнение без порога длины"
    body = src[src.index("async def cmd_admin_secret"):src.index("async def _show_admin_main")]
    assert "_notify_admins_of_grant" in body and "AUDIT admin_session_granted" in body


def test_admins_are_notified_about_grant(monkeypatch):
    admin = _admin()
    monkeypatch.setattr("config.ADMIN_IDS", [111, 222], raising=False)

    class _U:
        id = 222
        username = "intruder"
        full_name = "X"

    class _M:
        from_user = _U()

    sent = []

    class _B:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    asyncio.run(admin._notify_admins_of_grant(_B(), _M()))
    assert [c for c, _ in sent] == [111], "уведомление ушло не тем или самому вошедшему"
    assert "@intruder" in sent[0][1] and "222" in sent[0][1]
