"""Чистая сборка РЕАЛЬНОЙ поисковой выдачи Telegram (единый порядок всех типов).

Баг, который это чинит: позицию считали ВНУТРИ одного типа — бот искался среди
результатов-пользователей, канал среди результатов-каналов. Но пользователю
поиск показывает ОДИН список, где вперемешку каналы, боты, чаты. Поэтому бот,
реально стоящий 3-м (над ним два канала), среди «только пользователей» выходил
1-м — система врала, что мы в топе.

Здесь порядок берётся как его вернул Telegram (`contacts.Search.results` —
глобальная выдача), пиры сопоставляются со своими сущностями, и позиция
считается СКВОЗНОЙ по всем типам сразу. Это и есть настоящее место в выдаче.

Функция чистая (без сети/Telethon) — её и проверяют тесты; парсинг ответа
Telethon в порядок пиров делает вызывающий (account_manager).
"""
from __future__ import annotations


def merge_ranked(order: list[tuple], users: dict, chats: dict) -> list[dict]:
    """order — пиры в порядке выдачи Telegram: [("user"|"channel"|"chat", id), …].
    users — {id: {"username","first_name","is_bot"}}; chats — {id: {"username",
    "title","is_megagroup"}}. Возвращает сквозной ранжированный список с реальной
    позицией (1-based) по ВСЕМ типам, дедуп по (тип,id) с сохранением первого.
    """
    seen: set = set()
    out: list[dict] = []
    pos = 0
    for kind, pid in order or []:
        if kind == "user":
            e = users.get(pid)
            key = ("u", pid)
            if not e or key in seen:
                continue
            seen.add(key)
            pos += 1
            out.append({
                "position": pos,
                "kind": "bot" if e.get("is_bot") else "user",
                "is_bot": bool(e.get("is_bot")),
                "username": e.get("username", "") or "",
                "first_name": e.get("first_name", "") or "",
                "title": "",
                "tg_user_id": pid,
                "channel_id": None,
            })
        elif kind in ("channel", "chat"):
            e = chats.get(pid)
            key = ("c", pid)
            if not e or key in seen:
                continue
            seen.add(key)
            pos += 1
            out.append({
                "position": pos,
                "kind": "group" if e.get("is_megagroup") else "channel",
                "is_bot": False,
                "username": e.get("username", "") or "",
                "first_name": "",
                "title": e.get("title", "") or "",
                "tg_user_id": None,
                "channel_id": pid,
            })
    return out
