"""Email tools for AI agents — IMAP reading and SMTP sending."""
import imaplib
import smtplib
import email as email_lib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from datetime import datetime
import os
from typing import Any

from tools.database_tools import save_message, get_customer_by_email


def _imap_connect() -> imaplib.IMAP4_SSL:
    host = os.environ["EMAIL_IMAP_HOST"]
    port = int(os.environ.get("EMAIL_IMAP_PORT", "993"))
    user = os.environ["EMAIL_USER"]
    password = os.environ["EMAIL_PASSWORD"]
    conn = imaplib.IMAP4_SSL(host, port)
    conn.login(user, password)
    return conn


def _decode_header_value(value: str) -> str:
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def _extract_body(msg: email_lib.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


def fetch_unread_emails(folder: str = "INBOX", limit: int = 20) -> list[dict]:
    """Fetch unread emails and save them to the messages table."""
    conn = _imap_connect()
    try:
        conn.select(folder)
        _, data = conn.search(None, "UNSEEN")
        uids = data[0].split()
        uids = uids[-limit:] if len(uids) > limit else uids

        results = []
        for uid in uids:
            _, raw = conn.fetch(uid, "(RFC822)")
            msg = email_lib.message_from_bytes(raw[0][1])

            from_addr = _decode_header_value(msg.get("From", ""))
            subject = _decode_header_value(msg.get("Subject", ""))
            body = _extract_body(msg)
            date_str = msg.get("Date", "")

            sender_email = from_addr
            if "<" in from_addr:
                sender_email = from_addr.split("<")[1].rstrip(">").strip()

            customer = get_customer_by_email(sender_email)
            customer_id = customer["id"] if customer else None

            message_id = save_message(
                channel="email",
                content=body,
                direction="inbound",
                subject=subject,
                customer_id=customer_id,
            )

            conn.store(uid, "+FLAGS", "\\Seen")

            results.append({
                "message_id": message_id,
                "from": from_addr,
                "sender_email": sender_email,
                "subject": subject,
                "body": body,
                "date": date_str,
                "customer_id": customer_id,
                "customer_name": customer["name"] if customer else None,
            })

        return results
    finally:
        conn.logout()


def send_email(to: str, subject: str, body: str, customer_id: int | None = None) -> bool:
    """Send an email via SMTP and log it in the messages table."""
    host = os.environ["EMAIL_SMTP_HOST"]
    port = int(os.environ.get("EMAIL_SMTP_PORT", "587"))
    user = os.environ["EMAIL_USER"]
    password = os.environ["EMAIL_PASSWORD"]
    from_addr = os.environ.get("EMAIL_FROM", user)

    msg = MIMEMultipart("alternative")
    msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP(host, port) as server:
        server.ehlo()
        server.starttls()
        server.login(user, password)
        server.sendmail(from_addr, [to], msg.as_string())

    save_message(
        channel="email",
        content=body,
        direction="outbound",
        subject=subject,
        customer_id=customer_id,
    )
    return True


def reply_to_email(
    original_subject: str,
    to: str,
    body: str,
    customer_id: int | None = None,
) -> bool:
    """Reply to an email thread (prepends Re: if needed)."""
    subject = original_subject if original_subject.startswith("Re:") else f"Re: {original_subject}"
    return send_email(to=to, subject=subject, body=body, customer_id=customer_id)


def get_email_folders() -> list[str]:
    """List available mailbox folders."""
    conn = _imap_connect()
    try:
        _, folders = conn.list()
        result = []
        for f in folders:
            parts = f.decode().split('"."')
            if parts:
                name = parts[-1].strip().strip('"')
                result.append(name)
        return result
    finally:
        conn.logout()


def search_emails(query: str, folder: str = "INBOX", limit: int = 10) -> list[dict]:
    """Search emails by subject or sender."""
    conn = _imap_connect()
    try:
        conn.select(folder)
        _, subject_data = conn.search(None, f'SUBJECT "{query}"')
        _, from_data = conn.search(None, f'FROM "{query}"')

        uids_set = set(subject_data[0].split()) | set(from_data[0].split())
        uids = list(uids_set)[-limit:]

        results = []
        for uid in uids:
            _, raw = conn.fetch(uid, "(RFC822)")
            msg = email_lib.message_from_bytes(raw[0][1])
            results.append({
                "from": _decode_header_value(msg.get("From", "")),
                "subject": _decode_header_value(msg.get("Subject", "")),
                "date": msg.get("Date", ""),
                "snippet": _extract_body(msg)[:300],
            })
        return results
    finally:
        conn.logout()
