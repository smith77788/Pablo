"""Управление email-аккаунтами и отправка писем.

Владелец добавляет почтовые аккаунты (SMTP-реквизиты), пароль шифруется
через token_vault. Отправка — через aiosmtplib (async SMTP).

Чистое ядро: валидация, сборка письма, шифрование — без БД. Тонкая
персистенция: add/remove/list/get + send_email + mass_send через шину.
"""
from __future__ import annotations

import email.message
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

# ── Валидация ────────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
)

SMTP_PORTS = (25, 465, 587, 2525)
DEFAULT_SMTP_PORT = 587


def valid_email(addr: str) -> bool:
    return bool(addr and _EMAIL_RE.match(addr.strip()))


def valid_smtp_host(host: str) -> bool:
    if not host or len(host) > 253:
        return False
    return bool(re.match(r"^[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", host.strip()))


def valid_smtp_port(port: int | str) -> int | None:
    try:
        p = int(port)
    except (TypeError, ValueError):
        return None
    return p if 1 <= p <= 65535 else None


# ── Сборка MIME-письма ───────────────────────────────────────────────────────

def build_message(
    from_addr: str,
    to_addr: str,
    subject: str,
    body_html: str,
    display_name: str = "",
) -> email.message.EmailMessage:
    msg = email.message.EmailMessage()
    if display_name:
        msg["From"] = f"{display_name} <{from_addr}>"
    else:
        msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(re.sub(r"<[^>]+>", "", body_html), subtype="plain")
    msg.add_alternative(body_html, subtype="html")
    return msg


# ── Шифрование пароля ────────────────────────────────────────────────────────

def encrypt_password(pwd: str) -> str:
    from services.token_vault import encrypt_token
    return encrypt_token(pwd)


def decrypt_password(enc: str) -> str:
    from services.token_vault import decrypt_token
    return decrypt_token(enc)


# ── Известные провайдеры (автозаполнение) ────────────────────────────────────

KNOWN_PROVIDERS: list[dict[str, Any]] = [
    {"name": "Gmail", "smtp_host": "smtp.gmail.com", "smtp_port": 587,
     "hint": "Используйте пароль приложения (не основной пароль)"},
    {"name": "Yandex", "smtp_host": "smtp.yandex.ru", "smtp_port": 465,
     "hint": "Включите IMAP/SMTP в настройках ящика"},
    {"name": "Mail.ru", "smtp_host": "smtp.mail.ru", "smtp_port": 465,
     "hint": "Включите IMAP/SMTP и создайте пароль приложения"},
    {"name": "Outlook / Hotmail", "smtp_host": "smtp-mail.outlook.com", "smtp_port": 587,
     "hint": ""},
]


def detect_provider(email_addr: str) -> dict[str, Any] | None:
    if not email_addr:
        return None
    domain = email_addr.rsplit("@", 1)[-1].lower()
    mapping = {
        "gmail.com": "Gmail",
        "googlemail.com": "Gmail",
        "yandex.ru": "Yandex",
        "yandex.com": "Yandex",
        "ya.ru": "Yandex",
        "mail.ru": "Mail.ru",
        "bk.ru": "Mail.ru",
        "inbox.ru": "Mail.ru",
        "list.ru": "Mail.ru",
        "internet.ru": "Mail.ru",
        "outlook.com": "Outlook / Hotmail",
        "hotmail.com": "Outlook / Hotmail",
        "live.com": "Outlook / Hotmail",
    }
    name = mapping.get(domain)
    if not name:
        return None
    return next((p for p in KNOWN_PROVIDERS if p["name"] == name), None)


# ── Отправка (async) ────────────────────────────────────────────────────────

async def send_email(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_pass: str,
    from_addr: str,
    to_addr: str,
    subject: str,
    body_html: str,
    display_name: str = "",
    use_tls: bool | None = None,
) -> dict[str, Any]:
    """Отправить одно письмо. Возвращает {"ok": True} или {"ok": False, "error": ...}."""
    import aiosmtplib

    if use_tls is None:
        use_tls = smtp_port == 465

    msg = build_message(from_addr, to_addr, subject, body_html, display_name)
    try:
        await aiosmtplib.send(
            msg,
            hostname=smtp_host,
            port=smtp_port,
            username=smtp_user,
            password=smtp_pass,
            use_tls=use_tls,
            start_tls=not use_tls and smtp_port != 25,
            timeout=30,
        )
        return {"ok": True}
    except aiosmtplib.SMTPAuthenticationError:
        return {"ok": False, "error": "Ошибка авторизации — проверьте логин/пароль"}
    except aiosmtplib.SMTPConnectError:
        return {"ok": False, "error": f"Не удалось подключиться к {smtp_host}:{smtp_port}"}
    except (OSError, aiosmtplib.SMTPException) as exc:
        return {"ok": False, "error": str(exc)[:200]}


async def test_connection(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_pass: str,
    use_tls: bool | None = None,
) -> dict[str, Any]:
    """Проверить подключение к SMTP без отправки."""
    import aiosmtplib

    if use_tls is None:
        use_tls = smtp_port == 465

    try:
        smtp = aiosmtplib.SMTP(
            hostname=smtp_host,
            port=smtp_port,
            use_tls=use_tls,
            start_tls=not use_tls and smtp_port != 25,
            timeout=15,
        )
        await smtp.connect()
        await smtp.login(smtp_user, smtp_pass)
        await smtp.quit()
        return {"ok": True}
    except aiosmtplib.SMTPAuthenticationError:
        return {"ok": False, "error": "Ошибка авторизации — проверьте логин/пароль"}
    except aiosmtplib.SMTPConnectError:
        return {"ok": False, "error": f"Не удалось подключиться к {smtp_host}:{smtp_port}"}
    except (OSError, aiosmtplib.SMTPException) as exc:
        return {"ok": False, "error": str(exc)[:200]}


# ── Персистенция (pool = asyncpg) ───────────────────────────────────────────

async def add_account(
    pool, owner_id: int, email_addr: str,
    smtp_host: str, smtp_port: int,
    smtp_user: str, smtp_pass: str,
    display_name: str = "",
    daily_limit: int = 50,
) -> dict[str, Any]:
    enc = encrypt_password(smtp_pass)
    row = await pool.fetchrow(
        """INSERT INTO email_accounts
               (owner_id, email, smtp_host, smtp_port, smtp_user, smtp_pass_enc,
                display_name, daily_limit)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
           ON CONFLICT (owner_id, email) DO UPDATE
               SET smtp_host=$3, smtp_port=$4, smtp_user=$5, smtp_pass_enc=$6,
                   display_name=$7, daily_limit=$8, is_active=TRUE, updated_at=now()
           RETURNING id""",
        owner_id, email_addr, smtp_host, smtp_port, smtp_user, enc,
        display_name, daily_limit,
    )
    return {"id": row["id"], "email": email_addr}


async def remove_account(pool, owner_id: int, account_id: int) -> bool:
    r = await pool.fetchrow(
        "DELETE FROM email_accounts WHERE id=$1 AND owner_id=$2 RETURNING id",
        account_id, owner_id,
    )
    return r is not None


async def list_accounts(pool, owner_id: int) -> list[dict]:
    rows = await pool.fetch(
        """SELECT id, email, smtp_host, smtp_port, smtp_user, display_name,
                  daily_limit, sent_today, is_active,
                  created_at, updated_at
           FROM email_accounts WHERE owner_id=$1
           ORDER BY created_at""",
        owner_id,
    )
    return [dict(r) for r in rows]


async def get_account(pool, owner_id: int, account_id: int) -> dict | None:
    r = await pool.fetchrow(
        """SELECT id, email, smtp_host, smtp_port, smtp_user, smtp_pass_enc,
                  display_name, daily_limit, sent_today, is_active
           FROM email_accounts WHERE id=$1 AND owner_id=$2""",
        account_id, owner_id,
    )
    return dict(r) if r else None


async def toggle_account(pool, owner_id: int, account_id: int, active: bool) -> bool:
    r = await pool.fetchrow(
        """UPDATE email_accounts SET is_active=$3, updated_at=now()
           WHERE id=$1 AND owner_id=$2 RETURNING id""",
        account_id, owner_id, active,
    )
    return r is not None


async def increment_sent(pool, account_id: int) -> None:
    await pool.execute(
        "UPDATE email_accounts SET sent_today=sent_today+1, updated_at=now() WHERE id=$1",
        account_id,
    )


async def reset_daily_counters(pool, owner_id: int) -> None:
    await pool.execute(
        "UPDATE email_accounts SET sent_today=0 WHERE owner_id=$1",
        owner_id,
    )


async def send_via_account(
    pool, owner_id: int, account_id: int,
    to_addr: str, subject: str, body_html: str,
) -> dict[str, Any]:
    """Отправить письмо через конкретный аккаунт владельца."""
    acc = await get_account(pool, owner_id, account_id)
    if not acc:
        return {"ok": False, "error": "Аккаунт не найден"}
    if not acc["is_active"]:
        return {"ok": False, "error": "Аккаунт отключён"}
    if acc["sent_today"] >= acc["daily_limit"]:
        return {"ok": False, "error": "Дневной лимит отправки исчерпан"}
    smtp_pass = decrypt_password(acc["smtp_pass_enc"])
    result = await send_email(
        acc["smtp_host"], acc["smtp_port"],
        acc["smtp_user"], smtp_pass,
        acc["email"], to_addr, subject, body_html,
        display_name=acc.get("display_name", ""),
    )
    if result["ok"]:
        await increment_sent(pool, account_id)
    return result
