"""Синхронизация хранилища контактов в книгу аккаунта — «как с устройства».

Владелец (05.10.2026): «когда мы подключаем аккаунт к проекту, синхронизация
должна сразу происходить как с реального устройства». Хранилище и создано, чтобы
пустой аккаунт флота получил контакты: человек из книги аккаунта виден ему по
id, с ним можно работать без поиска по номеру, а инвайт и рассылка не упираются
в «аккаунт этого человека не знает».

Как это делает настоящий клиент Telegram: при входе он отдаёт адресную книгу
телефона порциями через contacts.importContacts. Повторяем ровно это:

  * сначала читаем, что уже есть в книге (GetContacts), и шлём только недостающих
    — повтор синхронизации ничего не дублирует;
  * порции по `_BATCH`, между порциями пауза; FloodWait останавливает выгрузку
    до следующего раза и пишется в пульс аккаунта (flood_engine);
  * потолок за один проход `CONTACTS_PUSH_CAP` — остаток доедет при следующей
    синхронизации;
  * в выгрузку не попадают контакты, помеченные «личный / исключён из работы».

После выгрузки аккаунт читает свою книгу обратно в хранилище (`sync_account`):
так хранилище узнаёт, что эти люди теперь есть у этого аккаунта.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random

log = logging.getLogger(__name__)

_BATCH = 100


def _cap() -> int:
    try:
        return max(_BATCH, int(os.getenv("CONTACTS_PUSH_CAP", "1000")))
    except (TypeError, ValueError):
        return 1000


def phone_key(p) -> str:
    """Номер в сравнимом виде: только цифры (Telegram отдаёт номер без «+»)."""
    return "".join(ch for ch in str(p or "") if ch.isdigit())


async def hub_contacts(pool, owner_id: int) -> list[dict]:
    """Контакты хранилища с номером, кроме исключённых из работы. Один номер — раз."""
    rows = await pool.fetch(
        "SELECT first_name, last_name, display_name, phones FROM unified_contacts "
        "WHERE owner_id=$1 AND phones <> '[]'::jsonb AND NOT COALESCE(excluded, FALSE)",
        owner_id)
    out: list[dict] = []
    seen: set = set()
    for r in rows or []:
        phones = r["phones"]
        if isinstance(phones, str):
            try:
                phones = json.loads(phones)
            except (TypeError, ValueError):
                phones = []
        for p in phones or []:
            k = phone_key(p)
            if len(k) < 6 or k in seen:
                continue
            seen.add(k)
            first = (r["first_name"] or r["display_name"] or "").strip() or "+" + k
            out.append({"phone": "+" + k, "first_name": first[:64],
                        "last_name": (r["last_name"] or "").strip()[:64]})
    return out


async def push_contacts(session_str: str, acc: dict, contacts: list[dict]) -> dict:
    """Добавить в книгу аккаунта тех из `contacts`, кого в ней ещё нет.

    Возвращает {already, sent, imported, left, flood_wait, error?}:
      already  — уже были в книге;
      sent     — отправлено в importContacts;
      imported — из отправленных нашлись в Telegram;
      left     — не дошла очередь (потолок прохода или пауза Telegram).
    """
    from telethon.tl.functions.contacts import GetContactsRequest, ImportContactsRequest
    from telethon.tl.types import InputPhoneContact
    from services.account_manager import connect_client

    res = {"already": 0, "sent": 0, "imported": 0, "left": 0, "flood_wait": 0}
    client = None
    try:
        client = await connect_client(session_str, acc, "contacts")
        book = await asyncio.wait_for(client(GetContactsRequest(hash=0)), timeout=45)
        have = {phone_key(getattr(u, "phone", "")) for u in getattr(book, "users", []) or []}
        have.discard("")
        todo = [c for c in contacts if phone_key(c["phone"]) not in have]
        res["already"] = len(contacts) - len(todo)
        cap = _cap()
        res["left"] = max(0, len(todo) - cap)
        todo = todo[:cap]
        for i in range(0, len(todo), _BATCH):
            chunk = todo[i:i + _BATCH]
            req = ImportContactsRequest(contacts=[
                InputPhoneContact(client_id=i + j, phone=c["phone"],
                                  first_name=c["first_name"], last_name=c["last_name"])
                for j, c in enumerate(chunk)])
            try:
                out = await asyncio.wait_for(client(req), timeout=60)
            except Exception as e:
                secs = int(getattr(e, "seconds", 0) or 0)
                if "flood" in type(e).__name__.lower() or secs:
                    res["flood_wait"] = secs or 60
                    res["left"] += len(todo) - i
                    break
                raise
            res["sent"] += len(chunk)
            res["imported"] += len(getattr(out, "imported", []) or [])
            if i + _BATCH < len(todo):
                await asyncio.sleep(random.uniform(4.0, 10.0))
        return res
    except Exception as e:
        log.warning("device_sync: acc=%s: %s", (acc or {}).get("id"), e)
        res["error"] = str(e)[:200]
        return res
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


async def schedule_for_new_account(pool, owner_id: int, acc_id: int) -> int | None:
    """Поставить синхронизацию «как с устройства» сразу после подключения аккаунта.

    Отдельной операцией через общую очередь: подключение не ждёт выгрузки, а сама
    выгрузка видна в «Операциях» с итогом. Сбой постановки не мешает подключению.
    """
    try:
        from services import operation_bus
        return await operation_bus.submit(
            pool, int(owner_id), "contacts_push", {"account_ids": [int(acc_id)]},
            total_items=1, label="Синхронизация контактов с устройства",
            bypass_plan_check=True)
    except Exception:
        log.warning("device_sync: не удалось поставить синхронизацию acc=%s", acc_id,
                    exc_info=True)
        return None
