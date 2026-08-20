"""Поиск потерянного контакта по имени профиля + началу @username.

Сценарий: пользователь потерял человека, помнит имя профиля (напр. «Oracle») и
начало юзернейма с числовым хвостом (@Smile*** где *** — цифры). Два прохода:

1. НАТИВНЫЙ поиск (безопасно, мгновенно) — Telegram ``contacts.SearchRequest`` по
   префиксу и по имени; отбираем людей, у кого username начинается с префикса или
   имя содержит искомое.
2. ПЕРЕБОР (@prefix + цифры) — резолвим кандидатов через ``ResolveUsername`` на живом
   аккаунте, с паузами и уважением FloodWait, с ранней остановкой при совпадении
   имени. Это баноопасный слой массовых действий: резолв username легче инвайта, но
   всё равно паузим, ротируем аккаунты и не молотим вслепую.

Чистые помощники (генерация кандидатов, матч имени) тестируются без Telegram; сам
цикл ``hunt`` принимает инъектируемый ``resolve``, поэтому пейсинг/ранний-стоп/
ротация проверяемы моками.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Iterator, Optional

log = logging.getLogger(__name__)

# Telegram username: 5–32 символа, [A-Za-z0-9_], не начинается с цифры/подчёркивания
# (для резолва достаточно набора символов и длины — префикс задаёт валидное начало).
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")

# Жёсткий потолок перебора за один прогон — чтобы 4-значный хвост (10k) не жёг флот
# бесконечно; остаток честно докручивается повторным запуском.
MAX_CANDIDATES_PER_RUN = 2000


def normalize(s: str | None) -> str:
    """Нормализация для сравнения имён/юзернеймов: нижний регистр, схлопнутые пробелы."""
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def name_matches(display_name: str | None, target: str | None) -> bool:
    """Совпадает ли имя профиля с искомым (нормализованное вхождение в обе стороны).

    'Oracle' матчит 'Oracle', 'The Oracle', 'oracle 🔮'. Пустой target → False
    (не считаем всех подряд совпадением)."""
    t = normalize(target)
    if not t:
        return False
    n = normalize(display_name)
    if not n:
        return False
    return t in n or n in t


def valid_username(username: str) -> bool:
    return bool(_USERNAME_RE.match((username or "").lstrip("@")))


def iter_candidates(prefix: str, digits: int, cap: int = MAX_CANDIDATES_PER_RUN) -> Iterator[str]:
    """Сгенерировать кандидатов ``prefix`` + число с ведущими нулями (00..99 для
    digits=2 и т.д.). Пропускаем невалидные по формату Telegram; режем по ``cap``.

    Порядок — по возрастанию номера (детерминированно, чтобы продолжение с offset
    было предсказуемым)."""
    prefix = (prefix or "").lstrip("@").strip()
    digits = int(digits)
    if not prefix or digits < 1 or digits > 6:
        return
    total = 10 ** digits
    emitted = 0
    for n in range(total):
        if emitted >= cap:
            return
        cand = f"{prefix}{n:0{digits}d}"
        if valid_username(cand):
            emitted += 1
            yield cand


# Тип резолвера: username → dict вида
#   {"username": str, "exists": bool, "user_id": int|None, "name": str,
#    "premium": bool, "flood_wait": int (сек, 0 если нет), "error": str|None}
Resolver = Callable[[str], Awaitable[dict[str, Any]]]


async def hunt(
    *,
    candidates: list[str],
    target_name: str,
    resolve: Resolver,
    pace_s: float = 1.2,
    flood_backoff_cb: Optional[Callable[[int], Awaitable[None]]] = None,
    is_cancelled: Optional[Callable[[], Awaitable[bool]]] = None,
    on_result: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None,
    stop_on_match: bool = True,
) -> dict[str, Any]:
    """Перебрать кандидатов через ``resolve`` с паузами и ранней остановкой.

    Возвращает {checked, found: [ {username,user_id,name,premium} ... ],
    matches: [подмножество found, где имя совпало], stopped_early, cancelled,
    flood_waits}. ``found`` — существующие юзернеймы; ``matches`` — те, у кого имя
    профиля совпало с искомым (это и есть кандидаты на «нашёлся»).
    """
    found: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    checked = 0
    flood_waits = 0
    stopped_early = False
    cancelled = False

    for i, uname in enumerate(candidates):
        if is_cancelled is not None and await is_cancelled():
            cancelled = True
            break
        res = await resolve(uname)
        checked += 1

        fw = int(res.get("flood_wait") or 0)
        if fw > 0:
            flood_waits += 1
            if flood_backoff_cb is not None:
                await flood_backoff_cb(fw)
            # Кандидат не проверен по сути — вернём его в конец очереди один раз?
            # Проще: считаем непроверенным (checked уже увеличен — скорректируем).
            checked -= 1
            continue

        if res.get("exists"):
            rec = {
                "username": res.get("username") or uname,
                "user_id": res.get("user_id"),
                "name": res.get("name") or "",
                "premium": bool(res.get("premium")),
            }
            found.append(rec)
            if on_result is not None:
                await on_result(rec)
            if name_matches(rec["name"], target_name):
                matches.append(rec)
                if stop_on_match:
                    stopped_early = True
                    break

        # Пауза между резолвами (последний — без паузы).
        if pace_s and i < len(candidates) - 1:
            await asyncio.sleep(pace_s)

    return {
        "checked": checked,
        "found": found,
        "matches": matches,
        "stopped_early": stopped_early,
        "cancelled": cancelled,
        "flood_waits": flood_waits,
    }


async def resolve_on_client(client, username: str) -> dict[str, Any]:
    """Один резолв ``username`` на УЖЕ подключённом Telethon-клиенте (переиспользуем
    соединение на весь перебор — connect/disconnect на каждый юзернейм недопустим).

    Возвращает dict контракта ``Resolver``. Ошибки классифицируем, не глотаем.
    """
    from telethon.tl.functions.contacts import ResolveUsernameRequest
    from telethon.errors import (
        UsernameNotOccupiedError,
        UsernameInvalidError,
        FloodWaitError,
    )

    clean = (username or "").lstrip("@").strip()
    try:
        res = await asyncio.wait_for(
            client(ResolveUsernameRequest(username=clean)), timeout=20.0
        )
        users = getattr(res, "users", None) or []
        if not users:
            return {"username": clean, "exists": False, "user_id": None, "name": "",
                    "premium": False, "flood_wait": 0, "error": None}
        u = users[0]
        first = getattr(u, "first_name", "") or ""
        last = getattr(u, "last_name", "") or ""
        name = f"{first} {last}".strip()
        return {
            "username": getattr(u, "username", None) or clean,
            "exists": True,
            "user_id": int(getattr(u, "id", 0) or 0) or None,
            "name": name,
            "premium": bool(getattr(u, "premium", False)),
            "flood_wait": 0,
            "error": None,
        }
    except UsernameNotOccupiedError:
        return {"username": clean, "exists": False, "user_id": None, "name": "",
                "premium": False, "flood_wait": 0, "error": None}
    except UsernameInvalidError:
        return {"username": clean, "exists": False, "user_id": None, "name": "",
                "premium": False, "flood_wait": 0, "error": "invalid"}
    except FloodWaitError as e:
        return {"username": clean, "exists": False, "user_id": None, "name": "",
                "premium": False, "flood_wait": int(getattr(e, "seconds", 0) or 0),
                "error": "flood_wait"}
    except (asyncio.TimeoutError, OSError, ConnectionError) as e:
        return {"username": clean, "exists": False, "user_id": None, "name": "",
                "premium": False, "flood_wait": 0, "error": f"net:{str(e)[:60]}"}
