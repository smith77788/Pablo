"""Обычные человеческие действия прогрева — те, что ничего не пишут в Telegram.

Зачем отдельный модуль
----------------------
Репертуар прогрева до этого состоял из 20 действий, и почти каждое «весомое»
(вступление, реакция, комментарий, голос в опросе, пересылка) **что-то пишет**.
Пишущие действия и есть то, за что аккаунт получает ограничения, поэтому
`_progressive_actions` их справедливо режет — и у свежего аккаунта оставалось
ровно три разрешённых действия: presence, диалоги, чтение канала. День за днём
один и тот же треугольник — это и не разнообразие, и не человек.

Здесь собраны действия, которых в наборе не хватало: всё то, что живой человек
делает в Telegram постоянно и что **не расходует ни одной пишущей квоты** —
листает ленту глубже, открывает фото, читает комментарии, ходит по ссылкам,
смотрит похожие каналы, заглядывает в Избранное, стикеры, папки, настройки.
Ban-риска у них нет по построению: это те же запросы, которые официальный
клиент шлёт сам, просто мы их шлём осознанно.

Два побочных эффекта, ради которых это и делается:

* **больше просмотренных постов.** `read_channel` брал последние 10–15
  сообщений; `deep_scroll` уходит на 2–4 страницы вглубь, `view_posts`
  засчитывает реальный просмотр (`messages.getMessagesViews`, increment=True) —
  то самое «смотреть больше постов».
* **больше каналов.** `explore_similar` и поиск возвращают ссылки на каналы,
  которых аккаунт ещё не знает. Они складываются в личный список интересов
  (`account_warmup_interests`), и набор каналов аккаунта растёт сам — как у
  человека, а не остаётся навсегда зашитой в код двадцаткой на весь флот.

Контракт примитива
------------------
`async def <name>(client, target: str = "", found: list | None = None) -> bool`

* возвращает `True`, только если действие реально выполнено (тихое «ну и ладно»
  здесь запрещено: прогрев, который рапортует успех, ничего не сделав, — худший
  из возможных багов этого модуля, мы его уже ловили);
* фатальные ошибки (бан/мёртвая сессия) **пробрасываются** — их разбирает
  вызывающий; всё остальное гасится и даёт `False`;
* `found` — необязательный выходной список: туда складываются найденные
  @каналы для персонального списка интересов.

Имена TL-запросов сверены с telethon 1.45.0. Это важно: `tests/conftest.py`
подменяет telethon предельно снисходительной заглушкой, поэтому опечатка в
имени TL-класса проходит все юнит-тесты и падает только в проде.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

# Ошибки, означающие «аккаунта больше нет» — их нельзя гасить, иначе прогрев
# будет долбить мёртвую сессию до конца дня. Единственный источник правды:
# account_warmer импортирует этот же набор.
FATAL_ERRORS = frozenset(
    {
        "UserDeactivatedBanError",
        "UserDeactivatedError",
        "AuthKeyUnregisteredError",
        "PhoneNumberBannedError",
        "SessionRevokedError",
        "SessionExpiredError",
    }
)

# Запросы поиска для «глобального» исследования — те же темы, что у остального
# прогрева, чтобы интересы аккаунта не расходились с его нишей.
_URL_RE = re.compile(r"https?://t\.me/([A-Za-z0-9_]{4,32})")
_USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{4,32})")


def _reraise_if_fatal(exc: BaseException) -> None:
    if type(exc).__name__ in FATAL_ERRORS:
        raise exc


def _collect(found: list | None, refs) -> None:
    """Сложить найденные каналы в выходной список, без дублей и мусора."""
    if found is None:
        return
    for ref in refs:
        ref = str(ref or "").strip()
        if not ref:
            continue
        if not ref.startswith("@"):
            ref = "@" + ref.lstrip("@")
        if len(ref) < 5 or len(ref) > 33:
            continue
        if ref not in found:
            found.append(ref)


# ── чтение вглубь ────────────────────────────────────────────────────────────


async def deep_scroll(client, target: str = "", found: list | None = None) -> bool:
    """Листает ленту канала вглубь — не только «последние 10», а 2–4 экрана.

    Человек, открывший интересный канал, уходит в историю на сотню сообщений.
    Аккаунт, который ежедневно видит ровно последние десять постов и ни одного
    старого, ведёт себя как программа опроса, а не как читатель.
    """
    try:
        entity = await client.get_entity(target)
        seen = 0
        pages = random.randint(2, 4)
        for _page in range(pages):
            batch = await client.get_messages(
                entity, limit=random.randint(15, 40), add_offset=seen
            )
            if not batch:
                break
            seen += len(batch)
            # «Читаем» не все — глаз цепляется за несколько на экране.
            for _m in list(batch)[: random.randint(3, 8)]:
                await asyncio.sleep(random.uniform(0.4, 1.6))
            await asyncio.sleep(random.uniform(1.5, 5.0))
        return seen > 0
    except Exception as e:
        _reraise_if_fatal(e)
        log_exc_swallow(log, "warmup deep_scroll %s", target)
        return False


async def view_posts(client, target: str = "", found: list | None = None) -> bool:
    """Засчитывает РЕАЛЬНЫЙ просмотр постов (messages.getMessagesViews).

    Остальные действия читают историю «мимо счётчика»: для канала такой аккаунт
    остаётся подписчиком, который не открыл ни одного поста. Здесь просмотр
    учитывается по-настоящему — это и есть обычное поведение читателя, и оно
    ничего не пишет.
    """
    try:
        from telethon.tl.functions.messages import GetMessagesViewsRequest

        entity = await client.get_entity(target)
        msgs = await client.get_messages(entity, limit=random.randint(10, 30))
        ids = [m.id for m in (msgs or []) if getattr(m, "id", None)]
        if not ids:
            return False
        await client(GetMessagesViewsRequest(peer=entity, id=ids, increment=True))
        # Просмотр — это время: столько, сколько уходит на пролистывание пачки.
        await asyncio.sleep(random.uniform(3.0, 10.0))
        return True
    except Exception as e:
        _reraise_if_fatal(e)
        log_exc_swallow(log, "warmup view_posts %s", target)
        return False


async def read_comments(client, target: str = "", found: list | None = None) -> bool:
    """Открывает обсуждение поста и ЧИТАЕТ комментарии (не пишет их).

    Комментарий писать рискованно и разрешено только зрелому аккаунту; читать
    комментарии не рискованно вовсе, а на стороне Telegram это заход в связанную
    группу — ровно то, что делает обычный подписчик.
    """
    try:
        entity = await client.get_entity(target)
        msgs = await client.get_messages(entity, limit=20)
        with_replies = [
            m
            for m in (msgs or [])
            if getattr(m, "replies", None) and getattr(m.replies, "replies", 0) > 0
        ]
        if not with_replies:
            return False
        post = random.choice(with_replies[:8])
        read = 0
        async for _reply in client.iter_messages(
            entity, limit=random.randint(5, 20), reply_to=post.id
        ):
            read += 1
            await asyncio.sleep(random.uniform(0.4, 1.5))
        await asyncio.sleep(random.uniform(1.0, 4.0))
        return read > 0
    except Exception as e:
        _reraise_if_fatal(e)
        log_exc_swallow(log, "warmup read_comments %s", target)
        return False


async def open_media(client, target: str = "", found: list | None = None) -> bool:
    """Открывает фотографию из поста — то есть реально её скачивает.

    Подписчик, не открывший ни одной картинки за месяц, — это профиль
    парсера. Ограничиваемся фотографиями: видео и документы весят столько, что
    флот из сотни аккаунтов выжрал бы трафик прокси без всякой пользы.
    """
    try:
        entity = await client.get_entity(target)
        msgs = await client.get_messages(entity, limit=25)
        photos = [m for m in (msgs or []) if getattr(m, "photo", None)]
        if not photos:
            return False
        msg = random.choice(photos)
        # Пауза перед открытием — человек сначала видит превью.
        await asyncio.sleep(random.uniform(0.8, 3.0))
        data = await client.download_media(msg, file=bytes)
        # Рассматривание.
        await asyncio.sleep(random.uniform(2.0, 8.0))
        return bool(data)
    except Exception as e:
        _reraise_if_fatal(e)
        log_exc_swallow(log, "warmup open_media %s", target)
        return False


async def open_link(client, target: str = "", found: list | None = None) -> bool:
    """Разворачивает ссылку из поста (messages.getWebPage) — переход по ссылке.

    Заодно самый естественный источник новых каналов: в постах постоянно висят
    t.me/<канал>, и человек по ним ходит.
    """
    try:
        from telethon.tl.functions.messages import GetWebPageRequest

        entity = await client.get_entity(target)
        msgs = await client.get_messages(entity, limit=25)
        urls: list[str] = []
        for m in msgs or []:
            text = getattr(m, "text", "") or ""
            urls += re.findall(r"https?://[^\s)\]]+", text)
            _collect(found, _URL_RE.findall(text))
        if not urls:
            return False
        url = random.choice(urls[:20])
        await asyncio.sleep(random.uniform(0.5, 2.5))
        await client(GetWebPageRequest(url=url, hash=0))
        await asyncio.sleep(random.uniform(3.0, 9.0))
        return True
    except Exception as e:
        _reraise_if_fatal(e)
        log_exc_swallow(log, "warmup open_link %s", target)
        return False


async def search_in_channel(client, target: str = "", found: list | None = None) -> bool:
    """Ищет по словам внутри канала — обычный поиск в открытом чате."""
    try:
        from services.account_warmer import _WARMUP_SEARCH_QUERIES

        entity = await client.get_entity(target)
        query = random.choice(_WARMUP_SEARCH_QUERIES)
        # Набор запроса занимает время.
        await asyncio.sleep(random.uniform(1.5, 5.0))
        res = await client.get_messages(entity, search=query, limit=10)
        for _m in list(res or [])[: random.randint(2, 6)]:
            await asyncio.sleep(random.uniform(0.5, 2.0))
        # Пустая выдача — не ошибка: поиск состоялся.
        return True
    except Exception as e:
        _reraise_if_fatal(e)
        log_exc_swallow(log, "warmup search_in_channel %s", target)
        return False


# ── исследование: откуда берутся НОВЫЕ каналы ────────────────────────────────


async def explore_similar(client, target: str = "", found: list | None = None) -> bool:
    """Смотрит «похожие каналы» (channels.getChannelRecommendations).

    Это кнопка официального клиента, а не наша выдумка. Для прогрева она ценна
    вдвойне: даёт аккаунту каналы, которых нет в общем для всего флота списке,
    то есть разводит графы интересов аккаунтов друг от друга.
    """
    try:
        from telethon.tl.functions.channels import GetChannelRecommendationsRequest

        entity = await client.get_entity(target)
        res = await client(GetChannelRecommendationsRequest(channel=entity))
        chats = list(getattr(res, "chats", None) or [])
        _collect(found, [getattr(c, "username", "") for c in chats])
        # Просмотр выдачи.
        for _c in chats[: random.randint(2, 6)]:
            await asyncio.sleep(random.uniform(0.6, 2.2))
        await asyncio.sleep(random.uniform(1.0, 4.0))
        return True
    except Exception as e:
        _reraise_if_fatal(e)
        log_exc_swallow(log, "warmup explore_similar %s", target)
        return False


# ── обычный быт клиента ──────────────────────────────────────────────────────


async def read_saved(client, target: str = "", found: list | None = None) -> bool:
    """Заходит в «Избранное» и перечитывает сохранённое.

    Прогрев умеет пересылать посты в Избранное (`forward_to_saved`) и никогда
    туда не заходит. Папка, куда только кладут и никогда не открывают, — это
    след скрипта.
    """
    try:
        msgs = await client.get_messages("me", limit=random.randint(5, 20))
        for _m in list(msgs or [])[: random.randint(2, 6)]:
            await asyncio.sleep(random.uniform(0.6, 2.2))
        await asyncio.sleep(random.uniform(1.0, 4.0))
        return True
    except Exception as e:
        _reraise_if_fatal(e)
        log_exc_swallow(log, "warmup read_saved")
        return False


async def check_stickers(client, target: str = "", found: list | None = None) -> bool:
    """Открывает панель стикеров: свои наборы и «популярные»."""
    try:
        from telethon.tl.functions.messages import (
            GetAllStickersRequest,
            GetFeaturedStickersRequest,
        )

        await client(GetAllStickersRequest(hash=0))
        await asyncio.sleep(random.uniform(1.0, 4.0))
        if random.random() < 0.6:
            await client(GetFeaturedStickersRequest(hash=0))
            await asyncio.sleep(random.uniform(1.0, 4.0))
        return True
    except Exception as e:
        _reraise_if_fatal(e)
        log_exc_swallow(log, "warmup check_stickers")
        return False


async def top_peers(client, target: str = "", found: list | None = None) -> bool:
    """Запрашивает «частые контакты» — то, что клиент делает сам при открытии
    поиска. Аккаунт, у которого этот запрос не звучал ни разу, отличается от
    аккаунта живого клиента на уровне трафика."""
    try:
        from telethon.tl.functions.contacts import GetTopPeersRequest

        await client(
            GetTopPeersRequest(
                offset=0,
                limit=random.randint(10, 20),
                hash=0,
                correspondents=True,
                groups=True,
                channels=True,
            )
        )
        await asyncio.sleep(random.uniform(2.0, 6.0))
        return True
    except Exception as e:
        _reraise_if_fatal(e)
        log_exc_swallow(log, "warmup top_peers")
        return False


async def open_folders(client, target: str = "", found: list | None = None) -> bool:
    """Открывает папки и закреплённые чаты — навигация обычного пользователя."""
    try:
        from telethon.tl.functions.messages import (
            GetDialogFiltersRequest,
            GetPinnedDialogsRequest,
        )

        await client(GetDialogFiltersRequest())
        await asyncio.sleep(random.uniform(1.0, 3.5))
        await client(GetPinnedDialogsRequest(folder_id=0))
        await asyncio.sleep(random.uniform(1.5, 5.0))
        return True
    except Exception as e:
        _reraise_if_fatal(e)
        log_exc_swallow(log, "warmup open_folders")
        return False


async def settings_peek(client, target: str = "", found: list | None = None) -> bool:
    """Заглядывает в настройки приватности — редкое, но совершенно рядовое
    действие живого пользователя. Ничего не меняет: только читает."""
    try:
        from telethon.tl.functions.account import GetPrivacyRequest
        from telethon.tl.types import InputPrivacyKeyStatusTimestamp

        await client(GetPrivacyRequest(key=InputPrivacyKeyStatusTimestamp()))
        await asyncio.sleep(random.uniform(2.0, 7.0))
        return True
    except Exception as e:
        _reraise_if_fatal(e)
        log_exc_swallow(log, "warmup settings_peek")
        return False


async def channel_stories(client, target: str = "", found: list | None = None) -> bool:
    """Смотрит истории конкретного канала и отмечает их просмотренными.

    Существующий `story_view` запрашивает общую ленту историй и на свежем
    аккаунте без контактов почти всегда пуст. Здесь — истории канала, который
    аккаунт и так читает.
    """
    try:
        from telethon.tl.functions.stories import (
            GetPeerStoriesRequest,
            ReadStoriesRequest,
        )

        entity = await client.get_entity(target)
        res = await client(GetPeerStoriesRequest(peer=entity))
        stories = list(getattr(getattr(res, "stories", None), "stories", None) or [])
        if not stories:
            return False
        for _s in stories[: random.randint(1, 5)]:
            await asyncio.sleep(random.uniform(1.5, 6.0))
        max_id = max(int(getattr(s, "id", 0) or 0) for s in stories)
        if max_id:
            await client(ReadStoriesRequest(peer=entity, max_id=max_id))
        return True
    except Exception as e:
        _reraise_if_fatal(e)
        log_exc_swallow(log, "warmup channel_stories %s", target)
        return False
