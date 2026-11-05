"""Почтовые аккаунты: валидация, сборка письма, шифрование, провайдеры, БД-обёртки.

Владелец добавляет email-аккаунты (SMTP) для отправки писем. Пароль шифруется
через token_vault. Проверяем чистое ядро + обёртки на заглушке пула.
"""
from __future__ import annotations

import asyncio

from services import email_service as E

# ── Валидация ────────────────────────────────────────────────────────────────

def test_valid_email():
    assert E.valid_email("user@gmail.com")
    assert E.valid_email("a.b+c@yandex.ru")
    assert not E.valid_email("")
    assert not E.valid_email("nope")
    assert not E.valid_email("@no-user.com")
    assert not E.valid_email("no-domain@")


def test_valid_smtp_host():
    assert E.valid_smtp_host("smtp.gmail.com")
    assert E.valid_smtp_host("smtp.yandex.ru")
    assert not E.valid_smtp_host("")
    assert not E.valid_smtp_host("a" * 254)
    assert not E.valid_smtp_host("localhost")


def test_valid_smtp_port():
    assert E.valid_smtp_port(587) == 587
    assert E.valid_smtp_port("465") == 465
    assert E.valid_smtp_port(0) is None
    assert E.valid_smtp_port(99999) is None
    assert E.valid_smtp_port("abc") is None
    assert E.valid_smtp_port(None) is None


# ── Сборка письма ───────────────────────────────────────────────────────────

def test_build_message_basic():
    msg = E.build_message("a@b.com", "c@d.com", "Тест", "<b>Привет</b>")
    assert msg["From"] == "a@b.com"
    assert msg["To"] == "c@d.com"
    assert msg["Subject"] == "Тест"


def test_build_message_display_name():
    msg = E.build_message("a@b.com", "c@d.com", "Тест", "<b>Hi</b>", display_name="Бот")
    assert "Бот" in msg["From"]
    assert "a@b.com" in msg["From"]


# ── Шифрование пароля ────────────────────────────────────────────────────────

def test_password_encrypt_roundtrip():
    pwd = "SuperSecret123!"
    enc = E.encrypt_password(pwd)
    assert enc != pwd
    assert enc.startswith("ENC:")
    assert E.decrypt_password(enc) == pwd


def test_empty_password():
    assert E.encrypt_password("") == ""
    assert E.decrypt_password("") == ""


# ── Детекция провайдера ──────────────────────────────────────────────────────

def test_detect_gmail():
    p = E.detect_provider("user@gmail.com")
    assert p and p["name"] == "Gmail"
    assert p["smtp_host"] == "smtp.gmail.com"


def test_detect_yandex():
    p = E.detect_provider("user@yandex.ru")
    assert p and p["name"] == "Yandex"


def test_detect_mailru():
    for domain in ("mail.ru", "bk.ru", "inbox.ru"):
        p = E.detect_provider(f"user@{domain}")
        assert p and p["name"] == "Mail.ru"


def test_detect_unknown():
    assert E.detect_provider("user@custom-domain.org") is None
    assert E.detect_provider("") is None


# ── Обёртки БД на заглушке ───────────────────────────────────────────────────

class _Pool:
    """Заглушка email_accounts: хранит строки в памяти."""

    def __init__(self):
        self.rows: list[dict] = []
        self._id = 0

    async def fetchrow(self, q, *a):
        if "INSERT INTO email_accounts" in q:
            existing = None
            for r in self.rows:
                if r["owner_id"] == a[0] and r["email"] == a[1]:
                    existing = r
                    break
            if existing:
                existing.update(smtp_host=a[2], smtp_port=a[3],
                                smtp_user=a[4], smtp_pass_enc=a[5],
                                display_name=a[6], daily_limit=a[7],
                                is_active=True)
                return {"id": existing["id"]}
            self._id += 1
            row = {
                "id": self._id, "owner_id": a[0], "email": a[1],
                "smtp_host": a[2], "smtp_port": a[3],
                "smtp_user": a[4], "smtp_pass_enc": a[5],
                "display_name": a[6], "daily_limit": a[7],
                "sent_today": 0, "is_active": True,
                "created_at": "now", "updated_at": "now",
            }
            self.rows.append(row)
            return {"id": self._id}
        if "DELETE FROM email_accounts" in q:
            for i, r in enumerate(self.rows):
                if r["id"] == a[0] and r["owner_id"] == a[1]:
                    self.rows.pop(i)
                    return {"id": r["id"]}
            return None
        if "UPDATE email_accounts SET is_active" in q:
            for r in self.rows:
                if r["id"] == a[0] and r["owner_id"] == a[1]:
                    r["is_active"] = a[2]
                    return {"id": r["id"]}
            return None
        if "SELECT" in q and "smtp_pass_enc" in q:
            for r in self.rows:
                if r["id"] == a[0] and r["owner_id"] == a[1]:
                    return r
            return None
        return None

    async def fetch(self, q, *a):
        if "SELECT" in q and "email_accounts" in q:
            return [r for r in self.rows if r["owner_id"] == a[0]]
        return []

    async def execute(self, q, *a):
        if "sent_today=sent_today+1" in q:
            for r in self.rows:
                if r["id"] == a[0]:
                    r["sent_today"] = r.get("sent_today", 0) + 1
        elif "sent_today=0" in q:
            for r in self.rows:
                if r["owner_id"] == a[0]:
                    r["sent_today"] = 0
        return "OK"


def test_add_and_list():
    pool = _Pool()
    r = asyncio.run(E.add_account(
        pool, 42, "test@gmail.com",
        "smtp.gmail.com", 587, "test@gmail.com", "pass123",
        display_name="Тест", daily_limit=100))
    assert r["id"] == 1 and r["email"] == "test@gmail.com"
    items = asyncio.run(E.list_accounts(pool, 42))
    assert len(items) == 1
    assert items[0]["email"] == "test@gmail.com"


def test_add_upsert_same_email():
    pool = _Pool()
    asyncio.run(E.add_account(pool, 42, "a@b.com", "s.b.com", 587, "a@b.com", "p1"))
    asyncio.run(E.add_account(pool, 42, "a@b.com", "s.b.com", 465, "a@b.com", "p2"))
    items = asyncio.run(E.list_accounts(pool, 42))
    assert len(items) == 1
    assert items[0]["smtp_port"] == 465


def test_remove():
    pool = _Pool()
    r = asyncio.run(E.add_account(pool, 42, "x@y.com", "s.y.com", 587, "x", "p"))
    assert asyncio.run(E.remove_account(pool, 42, r["id"]))
    assert asyncio.run(E.list_accounts(pool, 42)) == []


def test_remove_wrong_owner():
    pool = _Pool()
    r = asyncio.run(E.add_account(pool, 42, "x@y.com", "s.y.com", 587, "x", "p"))
    assert not asyncio.run(E.remove_account(pool, 999, r["id"]))


def test_toggle():
    pool = _Pool()
    r = asyncio.run(E.add_account(pool, 42, "x@y.com", "s.y.com", 587, "x", "p"))
    assert asyncio.run(E.toggle_account(pool, 42, r["id"], False))
    acc = asyncio.run(E.get_account(pool, 42, r["id"]))
    assert acc and not acc["is_active"]


def test_daily_counter():
    pool = _Pool()
    r = asyncio.run(E.add_account(pool, 42, "x@y.com", "s.y.com", 587, "x", "p"))
    asyncio.run(E.increment_sent(pool, r["id"]))
    asyncio.run(E.increment_sent(pool, r["id"]))
    acc = asyncio.run(E.get_account(pool, 42, r["id"]))
    assert acc["sent_today"] == 2
    asyncio.run(E.reset_daily_counters(pool, 42))
    acc = asyncio.run(E.get_account(pool, 42, r["id"]))
    assert acc["sent_today"] == 0


def test_get_nonexistent():
    pool = _Pool()
    assert asyncio.run(E.get_account(pool, 42, 999)) is None


def test_owner_isolation():
    pool = _Pool()
    asyncio.run(E.add_account(pool, 1, "a@b.com", "s", 587, "u", "p"))
    asyncio.run(E.add_account(pool, 2, "c@d.com", "s", 587, "u", "p"))
    assert len(asyncio.run(E.list_accounts(pool, 1))) == 1
    assert len(asyncio.run(E.list_accounts(pool, 2))) == 1
    assert asyncio.run(E.get_account(pool, 1, 2)) is None
