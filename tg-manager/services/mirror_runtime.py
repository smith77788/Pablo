"""Рантайм ботов-зеркал: каждый прогоняется через ТОТ ЖЕ Dispatcher.

Зеркало должно быть полностью идентично основному боту (все команды, меню,
сценарии), а не только кнопкой «открыть панель». Для этого апдейты зеркала
кормятся в основной Dispatcher с `Bot(token_зеркала)` — хендлеры отвечают через
зеркало, но логика та же.

Механика — лёгкий long-polling на зеркало (getUpdates) + `dp.feed_update`. Так
не нужен ни публичный вебхук, ни правка HTTP-сервера, и работает в любом режиме
основного бота. Запускается ТАМ ЖЕ, где обрабатывается основной бот (одна
реплика), иначе два процесса делили бы getUpdates зеркала.

Супервизор раз в минуту сверяет список зеркал: новое — поднимает поллер,
удалённое или со сменённым токеном — гасит/перезапускает. Отозванный токен (401)
гасит поллер до смены токена, не долбя Telegram впустую.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging

log = logging.getLogger(__name__)

_SYNC_INTERVAL = 60.0          # как часто сверять список зеркал
_POLL_TIMEOUT = 30            # long-poll getUpdates
_MAX_MIRRORS = 200           # потолок одновременных поллеров (защита ресурсов)


def _fp(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()[:16]


def _make_bot(token: str):
    """Bot-зеркало с теми же свойствами, что основной (HTML, проверка TLS)."""
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.enums import ParseMode
    from services.security import outbound_ssl_context
    sess = AiohttpSession()
    try:
        sess._connector_init["ssl"] = outbound_ssl_context()
    except Exception:
        pass
    return Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML),
               session=sess)


async def _close_bot(bot) -> None:
    try:
        await bot.session.close()
    except Exception:
        pass


async def _poll_one(mirror_id: int, bot, dp, pool, http, allowed_updates, dead: set) -> None:
    """Long-poll одного зеркала; каждый апдейт — в основной Dispatcher."""
    from aiogram.exceptions import (TelegramUnauthorizedError,
                                    TelegramRetryAfter, TelegramNetworkError)
    from aiogram.types import Update
    token_fp = _fp(bot.token)
    # getUpdates требует отсутствия вебхука. Накопленное не выбрасываем.
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception as e:
        log.debug("mirror_runtime: delete_webhook mirror=%s: %s", mirror_id, e)
    offset = None
    while True:
        try:
            raw = await bot.get_updates(
                offset=offset, timeout=_POLL_TIMEOUT, allowed_updates=allowed_updates)
        except TelegramUnauthorizedError:
            # Токен отозван — гасим поллер, пока владелец не сменит токен.
            log.info("mirror_runtime: зеркало %s — токен отозван, поллинг остановлен", mirror_id)
            dead.add(token_fp)
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(min(float(getattr(e, "retry_after", 5) or 5), 60.0))
            continue
        except (TelegramNetworkError, asyncio.TimeoutError):
            await asyncio.sleep(3.0)
            continue
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("mirror_runtime: get_updates mirror=%s: %s", mirror_id, e)
            await asyncio.sleep(5.0)
            continue
        for u in raw:
            offset = u.update_id + 1
            try:
                upd = u if isinstance(u, Update) else Update.model_validate(
                    u, context={"bot": bot})
                # pool/http передаём явно — хендлеры берут их из контекста.
                await dp.feed_update(bot, upd, pool=pool, http=http)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Ядовитый апдейт офсет уже сдвинул — не повторяем, как при опросе.
                log.debug("mirror_runtime: feed_update mirror=%s: %s", mirror_id, e)


async def run(pool, dp, http, main_bot=None, allowed_updates=None) -> None:
    """Супервизор зеркал: держит по поллеру на каждое, сверяет раз в минуту."""
    if dp is None:
        log.info("mirror_runtime: Dispatcher не передан — зеркала не запускаю")
        return
    tasks: dict[int, dict] = {}       # mirror_id -> {task, bot, fp}
    dead: set[str] = set()            # token_fp отозванных — не поднимать заново
    from services.token_vault import decrypt_token
    while True:
        try:
            rows = await pool.fetch(
                "SELECT id, token_enc FROM mirror_bots ORDER BY id LIMIT $1", _MAX_MIRRORS)
        except Exception as e:
            log.debug("mirror_runtime: список зеркал не прочитан: %s", e)
            rows = []
        want: dict[int, str] = {}
        for r in rows:
            try:
                want[int(r["id"])] = decrypt_token(r["token_enc"])
            except Exception as e:
                log.debug("mirror_runtime: токен зеркала %s не расшифрован: %s", r["id"], e)
        # Погасить удалённые и те, у кого сменился токен (перезапуск).
        for mid in list(tasks):
            cur = tasks[mid]
            if mid not in want or _fp(want[mid]) != cur["fp"]:
                cur["task"].cancel()
                await _close_bot(cur["bot"])
                tasks.pop(mid, None)
        # Поднять новые (не отозванные).
        for mid, token in want.items():
            if mid in tasks or _fp(token) in dead:
                continue
            try:
                bot = _make_bot(token)
            except Exception as e:
                log.warning("mirror_runtime: Bot зеркала %s не создан: %s", mid, e)
                continue
            task = asyncio.create_task(
                _poll_one(mid, bot, dp, pool, http, allowed_updates, dead),
                name=f"mirror_poll_{mid}")
            tasks[mid] = {"task": task, "bot": bot, "fp": _fp(token)}
            log.info("mirror_runtime: зеркало %s запущено", mid)
        # Сменился токен → возможно, починили отозванный: такой fp больше не в want
        # с тем же значением, а новый fp не в dead — поднимется выше. Чистим dead
        # от отпечатков, которых уже нет ни у одного зеркала.
        live_fps = {_fp(t) for t in want.values()}
        dead.intersection_update(live_fps)
        await asyncio.sleep(_SYNC_INTERVAL)
