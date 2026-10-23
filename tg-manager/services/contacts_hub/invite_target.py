"""Каким адресом приглашать контакт из хранилища.

Порядок: @username → телефон → голый числовой id.

Голый id годится только аккаунту, который этого человека уже видел (у него в
книге контактов или в общем чате): без access_hash Telegram его не найдёт.
Хранилище собирает контакты со всех аккаунтов, а инвайт раздаёт людей по всему
флоту — и раньше id стоял ВПЕРЕДИ телефона. Человек из книги аккаунта A уходил
аккаунту B голым id, B его не находил, и выходило, что из хранилища реально
приглашают только те аккаунты, у кого эти люди уже в контактах.

Телефон любой аккаунт флота добавит сам: движок импортирует номер в свою книгу
контактов, приглашает и убирает его обратно (`invite_by_phones`). Поэтому номер
— раньше id: с ним работает весь флот, а не только «родной» аккаунт контакта.
"""
from __future__ import annotations

import json


def _phones(raw) -> list[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(p).strip() for p in raw if p and str(p).strip()]


def invite_target(c) -> tuple[str | None, str | None]:
    """('user', '@username' | 'id') или ('phone', '+номер') или (None, None)."""
    uname = str((c.get("username") if hasattr(c, "get") else c["username"]) or "")
    uname = uname.strip().lstrip("@")
    if uname:
        return "user", "@" + uname
    try:
        raw_ph = c.get("phones") if hasattr(c, "get") else c["phones"]
    except (KeyError, IndexError):
        raw_ph = None
    ph = _phones(raw_ph)
    if ph:
        num = ph[0]
        return "phone", num if num.startswith("+") else "+" + num.lstrip("+")
    try:
        uid = c.get("telegram_user_id") if hasattr(c, "get") else c["telegram_user_id"]
    except (KeyError, IndexError):
        uid = None
    if uid:
        return "user", str(uid)
    return None, None
