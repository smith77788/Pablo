"""Поведенческий движок инвайтинга: чем аккаунт занят МЕЖДУ приглашениями.

Зачем это вообще нужно
──────────────────────
Задержки между действиями делают ритм похожим на человеческий, но не меняют
главного: если весь след аккаунта в API — это `InviteToChannel`, `InviteToChannel`,
`InviteToChannel`, то никакая пауза его не спасёт. Живой человек, добавляя
знакомых в чат, между делом открывает список диалогов, появляется онлайн, что-то
читает. Аккаунт, который не делает НИЧЕГО кроме приглашений, отделяется от живого
тривиально — по составу вызовов, а не по их темпу.

Поэтому здесь — набор дешёвых, безобидных и НЕ видимых посторонним действий,
которые вплетаются в паузы инвайта.

Чего этот модуль намеренно НЕ делает
────────────────────────────────────
* не пишет сообщений, не ставит реакций, не вступает в каналы — всё это видно
  снаружи, имеет собственные лимиты и само по себе баноопасно; цель здесь —
  разбавить след, а не добавить второй рискованный поток;
* не ходит по чужим ресурсам — только по собственным диалогам аккаунта;
* никогда не поднимает исключение и не задерживает операцию дольше своего
  бюджета: разбавление следа не стоит того, чтобы из-за него падал инвайт.

Прогрев (`account_warmer`) решает другую задачу — растит аккаунт по плану на
дни вперёд. Здесь — секунды внутри уже идущей операции, поэтому набор действий
свой: строго read-only и с жёстким потолком по времени.
"""

from __future__ import annotations

import asyncio
import logging
import random

log = logging.getLogger(__name__)

# Вероятность, что пауза будет чем-то заполнена. Не 100%: человек тоже не делает
# одно и то же между каждой парой действий, а ровное «после каждого инвайта —
# ровно одно действие» — такой же машинный признак, как ровная задержка.
DEFAULT_PROBABILITY = 0.35

# Потолок на одно вплетение. Инвайт — операция пользователя, и она не должна
# заметно тормозить из-за косметики.
_ACTION_BUDGET_S = 12.0

# Действия и их веса. Онлайн-статус самый частый и самый дешёвый: у реального
# клиента он меняется постоянно.
_ACTIONS: list[tuple[str, int]] = [
    ("presence", 4),
    ("dialogs", 3),
    ("read", 2),
]


def _pick_action() -> str:
    names = [a for a, _ in _ACTIONS]
    weights = [w for _, w in _ACTIONS]
    return random.choices(names, weights=weights, k=1)[0]


async def _act_presence(client) -> None:
    """Короткое появление онлайн.

    offline=True выставляется в finally: аккаунт, застрявший в «вечно онлайн»
    из-за ошибки между двумя вызовами, — сам по себе аномалия.
    """
    from telethon.tl.functions.account import UpdateStatusRequest

    went_online = False
    try:
        await client(UpdateStatusRequest(offline=False))
        went_online = True
        await asyncio.sleep(random.uniform(1.5, 4.0))
    finally:
        if went_online:
            try:
                await client(UpdateStatusRequest(offline=True))
            except Exception:
                log.debug("invite_behavior: offline reset failed")


async def _act_dialogs(client) -> None:
    """Открыть список диалогов — самый обычный запрос любого клиента."""
    await client.get_dialogs(limit=random.randint(5, 15))
    await asyncio.sleep(random.uniform(0.8, 2.5))


async def _act_read(client) -> None:
    """Прочитать несколько сообщений в СВОЁМ случайном диалоге.

    Ходим только по собственным диалогам аккаунта: чужие ресурсы — это уже
    другой риск и другие лимиты.
    """
    dialogs = await client.get_dialogs(limit=10)
    if not dialogs:
        return
    d = random.choice(list(dialogs))
    await client.get_messages(d, limit=random.randint(3, 10))
    await asyncio.sleep(random.uniform(0.8, 2.0))


_HANDLERS = {"presence": _act_presence, "dialogs": _act_dialogs, "read": _act_read}


async def humanize(acc: dict, *, probability: float = DEFAULT_PROBABILITY) -> str | None:
    """Вплести одно человеческое действие от лица аккаунта.

    Возвращает имя выполненного действия или None (не выпало по вероятности,
    нет сессии, действие не удалось). Не бросает исключений НИКОГДА: разбавление
    следа — это улучшение, а не обязательство, и оно не имеет права уронить
    операцию, ради которой затевалось.
    """
    if random.random() >= probability:
        return None
    session = (acc or {}).get("session_str") or ""
    if not session:
        return None

    action = _pick_action()
    try:
        return await asyncio.wait_for(_run(session, acc, action), timeout=_ACTION_BUDGET_S)
    except asyncio.CancelledError:
        # Отмену операции пробрасываем: она не наша, чтобы её глотать.
        raise
    except Exception:
        log.debug("invite_behavior: %s failed acc=%s", action, (acc or {}).get("id"))
        return None


async def _run(session: str, acc: dict, action: str) -> str | None:
    from services.account_manager import _make_client

    client = _make_client(session, dict(acc or {}))
    try:
        await client.connect()
        await _HANDLERS[action](client)
        return action
    finally:
        try:
            await client.disconnect()
        except Exception:
            log.debug("invite_behavior: disconnect failed")
