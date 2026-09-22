"""Async Telegram Bot API wrapper for managed (target) bots."""

from __future__ import annotations
import asyncio
import logging
import aiohttp
from config import MAX_CONCURRENT

_semaphore: asyncio.Semaphore | None = None

TG = "https://api.telegram.org/bot{token}/{method}"
TG_FILE = "https://api.telegram.org/file/bot{token}/{file_path}"


def api_url(token: str, method: str) -> str:
    """Адрес метода Bot API. Единственная дверь: токен расшифровывается ЗДЕСЬ.

    `managed_bots.token` хранится зашифрованным (token_vault). Вызывающие
    читают колонку по-разному — где-то через db.fetch_bots, которая
    расшифровывает, где-то сырым SELECT, — и половина мест про расшифровку
    забывала. Шифротекст в адресе даёт «/botENC:…/», Telegram отвечает 401, и
    функция выглядит просто сломанной: «команды бота не показываются»,
    «вебхук не ставится». Единственный надёжный ответ — расшифровывать в самой
    последней точке, через которую проходят все.

    decrypt_token — passthrough для строк без метки ENC:, поэтому уже
    расшифрованный токен проходит без изменений, а двойного расшифрования не
    бывает.
    """
    from services.token_vault import decrypt_token

    return TG.format(token=decrypt_token(token or ""), method=method)

_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BASE_BACKOFF = 1.0

# ── Потолок на ВСТРОЕННЫЙ сон по 429 ─────────────────────────────────────────
# Telegram отдаёт ботам retry_after и в сотни секунд (рассылка по многим чатам,
# лимит на бота целиком). Спать ровно столько, сколько он попросил, — значит
# повесить вызывающего (рассылку, воронку, релей оператору) на всё это время,
# а с тремя ретраями подряд — втрое дольше.
#
# Уважать паузу надо, но ждать должна ОЧЕРЕДЬ, а не занятый исполнитель. Поэтому
# клиент сам отсыпает только короткие паузы (их дешевле переждать на месте, чем
# перепланировать), а длинную ОТДАЁТ НАВЕРХ нетронутой: в ответе остаётся
# настоящий parameters.retry_after, и вызывающий переносит задачу.
_MAX_INLINE_RETRY_AFTER_S = 30


def retry_after_of(data: dict | None) -> int | None:
    """Пауза из ответа Telegram (parameters.retry_after) — или None.

    Единственное место, где это поле читается: Telegram присылает
    ``"parameters": null`` наравне с объектом, и ``data["parameters"]["..."]``
    на таком ответе падает AttributeError внутри ретрай-цикла.
    """
    if not isinstance(data, dict):
        return None
    params = data.get("parameters") or {}
    if not isinstance(params, dict):
        return None
    raw = params.get("retry_after")
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


log = logging.getLogger(__name__)


def _sem() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    return _semaphore


async def _call(
    session: aiohttp.ClientSession, token: str, method: str, **params
) -> dict:
    """Call Bot API with automatic retry on network errors and 429/5xx.

    Retries up to 3 times with exponential backoff for transient errors.
    Respects Telegram's retry_after header on 429 responses.
    """
    url = api_url(token, method)
    payload = {k: v for k, v in params.items() if v is not None}
    last_error = None

    for attempt in range(_MAX_RETRIES):
        try:
            async with _sem():
                async with session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    http_status = resp.status
                    data = await resp.json()

            # Ответ не-объектом (шлюз/прокси вместо Telegram) — дальше по коду
            # везде .get(); приводим к понятной форме, а не падаем AttributeError.
            if not isinstance(data, dict):
                return {
                    "ok": False,
                    "error_code": http_status,
                    "description": f"Неожиданный ответ Telegram (HTTP {http_status})",
                }

            status = data.get("error_code", 0) or http_status
            if status in _RETRYABLE_STATUSES or not data.get("ok"):
                if status == 429:
                    retry_after = retry_after_of(data)
                    if retry_after is None:
                        retry_after = 5
                    # Длинную паузу на месте не отсыпаем: возвращаем ответ как
                    # есть — в нём настоящий retry_after, и вызывающий перенесёт
                    # задачу вместо того, чтобы стоять.
                    if retry_after > _MAX_INLINE_RETRY_AFTER_S:
                        log.info(
                            "bot_api %s: Telegram просит ждать %dс (> %dс) — "
                            "отдаём наверх для переноса, не спим",
                            method,
                            retry_after,
                            _MAX_INLINE_RETRY_AFTER_S,
                        )
                        return data
                    log.debug(
                        "bot_api %s rate-limited, sleeping %ds (attempt %d/%d)",
                        method,
                        retry_after,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    if attempt < _MAX_RETRIES - 1:
                        await asyncio.sleep(retry_after)
                        continue
                    # All retries exhausted on 429 — return the real response so
                    # callers (send_message / send_photo) can propagate retry_after
                    return data
                if status in (500, 502, 503, 504) and attempt < _MAX_RETRIES - 1:
                    backoff = _BASE_BACKOFF * (2**attempt)
                    log.debug(
                        "bot_api %s HTTP %d, retrying in %.1fs (attempt %d/%d)",
                        method,
                        status,
                        backoff,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    await asyncio.sleep(backoff)
                    continue
                # Non-retryable error — return as-is
                return data
            return data

        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            last_error = e
            if attempt < _MAX_RETRIES - 1:
                backoff = _BASE_BACKOFF * (2**attempt)
                log.debug(
                    "bot_api %s network error, retrying in %.1fs: %s",
                    method,
                    backoff,
                    e,
                )
                await asyncio.sleep(backoff)
                continue
            log.warning(
                "bot_api %s failed after %d retries: %s", method, _MAX_RETRIES, e
            )

    # All retries exhausted
    return {
        "ok": False,
        "error_code": 0,
        "description": f"Network error after {_MAX_RETRIES} retries: {last_error}",
    }


# ── Bot info ──────────────────────────────────────────────────────────────


async def get_me(session: aiohttp.ClientSession, token: str) -> dict | None:
    data = await _call(session, token, "getMe")
    return data.get("result") if data.get("ok") else None


# ── Profile editing ───────────────────────────────────────────────────────


async def set_name(
    session: aiohttp.ClientSession, token: str, name: str, language_code: str = ""
) -> bool:
    data = await _call(
        session, token, "setMyName", name=name, language_code=language_code or None
    )
    return data.get("ok", False)


async def set_description(
    session: aiohttp.ClientSession,
    token: str,
    description: str,
    language_code: str = "",
) -> bool:
    data = await _call(
        session,
        token,
        "setMyDescription",
        description=description,
        language_code=language_code or None,
    )
    return data.get("ok", False)


async def set_short_description(
    session: aiohttp.ClientSession,
    token: str,
    short_description: str,
    language_code: str = "",
) -> bool:
    data = await _call(
        session,
        token,
        "setMyShortDescription",
        short_description=short_description,
        language_code=language_code or None,
    )
    return data.get("ok", False)


async def get_my_name(
    session: aiohttp.ClientSession, token: str, language_code: str = ""
) -> str:
    data = await _call(session, token, "getMyName", language_code=language_code or None)
    return data.get("result", {}).get("name", "") if data.get("ok") else ""


async def get_my_description(
    session: aiohttp.ClientSession, token: str, language_code: str = ""
) -> str:
    data = await _call(
        session, token, "getMyDescription", language_code=language_code or None
    )
    return data.get("result", {}).get("description", "") if data.get("ok") else ""


async def get_my_short_description(
    session: aiohttp.ClientSession, token: str, language_code: str = ""
) -> str:
    data = await _call(
        session, token, "getMyShortDescription", language_code=language_code or None
    )
    return data.get("result", {}).get("short_description", "") if data.get("ok") else ""


async def set_photo(
    session: aiohttp.ClientSession,
    token: str,
    photo_bytes: bytes,
    filename: str = "photo.jpg",
) -> bool:
    """Upload raw photo bytes to the managed bot via multipart form.

    Идёт мимо ``_call`` (там JSON, а здесь multipart), поэтому ретраи и разбор
    ошибок продублированы здесь же. Без них единственный обрыв сети на загрузке
    аватара улетал вызывающему СЫРЫМ исключением — в отличие от всех соседних
    методов файла, которые возвращают bool. Как и в ``_call``, длинную паузу по
    429 на месте не отсыпаем.
    """
    url = api_url(token, "setMyPhoto")
    for attempt in range(_MAX_RETRIES):
        try:
            form = aiohttp.FormData()
            form.add_field(
                "photo", photo_bytes, filename=filename, content_type="image/jpeg"
            )
            async with _sem():
                async with session.post(
                    url, data=form, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    http_status = resp.status
                    data = await resp.json()
            if not isinstance(data, dict):
                log.warning("bot_api setMyPhoto: неожиданный ответ HTTP %d", http_status)
                return False
            if data.get("ok"):
                return True
            status = data.get("error_code", 0) or http_status
            if status == 429:
                retry_after = retry_after_of(data) or 5
                if retry_after > _MAX_INLINE_RETRY_AFTER_S or attempt >= _MAX_RETRIES - 1:
                    log.info(
                        "bot_api setMyPhoto: Telegram просит ждать %dс — не спим",
                        retry_after,
                    )
                    return False
                await asyncio.sleep(retry_after)
                continue
            if status in (500, 502, 503, 504) and attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(_BASE_BACKOFF * (2**attempt))
                continue
            log.warning(
                "bot_api setMyPhoto: %s", str(data.get("description", ""))[:120]
            )
            return False
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(_BASE_BACKOFF * (2**attempt))
                continue
            log.warning("bot_api setMyPhoto failed after %d retries: %s",
                        _MAX_RETRIES, e)
    return False


async def delete_my_photo(session: aiohttp.ClientSession, token: str) -> bool:
    data = await _call(session, token, "deleteMyPhoto")
    return data.get("ok", False)


# ── Webhooks ──────────────────────────────────────────────────────────────


async def set_webhook(session: aiohttp.ClientSession, token: str, url: str) -> dict:
    return await _call(
        session,
        token,
        "setWebhook",
        url=url,
        allowed_updates=["message", "callback_query", "chat_member"],
    )


async def delete_webhook(session: aiohttp.ClientSession, token: str) -> dict:
    return await _call(session, token, "deleteWebhook")


async def get_webhook_info(session: aiohttp.ClientSession, token: str) -> dict:
    data = await _call(session, token, "getWebhookInfo")
    return data.get("result", {}) if data.get("ok") else {}


# ── Audience collection ───────────────────────────────────────────────────


class UpdatesUnavailable(Exception):
    """getUpdates невозможен для этого бота. Текст — готовое объяснение владельцу."""


def _updates_conflict(data: dict) -> str | None:
    """Причина, по которой getUpdates не сработает, или None.

    Telegram отвечает 409 Conflict, когда у бота включён вебхук: два способа
    получать обновления одновременно не работают, и это НЕ временный сбой —
    повтор не поможет, пока вебхук стоит.

    Раньше такой ответ просто превращался в пустой список: сбор аудитории
    показывал владельцу «Новых пользователей: +0» и выглядел успешным, хотя не
    прочитал ни одного обновления и прочитать не мог. Молчаливый ноль хуже
    ошибки: по нему кажется, что писать боту никто не начинал.
    """
    if data.get("ok"):
        return None
    desc = str(data.get("description") or "").lower()
    if data.get("error_code") == 409 or "webhook is active" in desc:
        return ("У этого бота включён вебхук — Telegram не отдаёт обновления "
                "через getUpdates, пока он стоит. Аудитория у такого бота "
                "набирается сама из входящих сообщений; разовый сбор доступен "
                "после отключения вебхука.")
    return None


async def fetch_updates(session: aiohttp.ClientSession, token: str) -> list[dict]:
    """Pull up to 100 pending updates.

    Бросает UpdatesUnavailable, если getUpdates у этого бота невозможен, —
    вызывающий обязан сказать владельцу причину, а не показать пустой сбор.
    """
    data = await _call(session, token, "getUpdates", offset=0, limit=100, timeout=0)
    reason = _updates_conflict(data)
    if reason:
        raise UpdatesUnavailable(reason)
    return data.get("result", []) if data.get("ok") else []


async def scan_all_users(
    session: aiohttp.ClientSession,
    token: str,
    start_offset: int = 0,
    max_batches: int = 50,
) -> tuple[list[dict], int]:
    """Собрать пользователей из ожидающих обновлений, НЕ подтверждая их.

    Возвращает (список без дублей, наибольший update_id). Этот update_id —
    только для показа: сохранять его как оффсет НЕЛЬЗЯ, иначе обновления
    пропадут для автоответчика.

    ЧТО БЫЛО. Функция листала обновления пачками, и каждая следующая просьба
    шла с `offset = последний + 1`. Для Telegram это ПОДТВЕРЖДЕНИЕ: всё, что
    раньше этого номера, он забывает и больше не отдаёт никому. А очередь
    обновлений у бота одна на всех.

    Значит нажатие «Сканировать все апдейты» съедало входящие сообщения,
    которых автоответчик ещё не разобрал: человек написал боту, сбор аудитории
    записал его в базу — и на этом всё. Ни авто-ответа, ни запуска воронки, ни
    `/start`, ни пересылки оператору. Подписчик остался без ответа навсегда, а
    владелец об этом не узнал: экран показывал успешный сбор.

    Листать при этом было незачем. Telegram хранит только НЕподтверждённые
    обновления, а автоответчик опрашивает бота каждые 10 секунд и подтверждает
    всё, что разобрал. Поэтому «50 пачек по 100» — это в лучшем случае одно
    десятисекундное окно: пятьдесят просьб уходили за тем, чего там нет.

    ЧТО СТАЛО. Одна просьба с `offset=0` — Telegram отдаёт ожидающие
    обновления и НИЧЕГО не забывает. Пользователей записываем, сами обновления
    остаются в очереди, и автоответчик разбирает их своим чередом.

    `start_offset` и `max_batches` сохранены для совместимости вызова и больше
    ни на что не влияют.
    """
    data = await _call(session, token, "getUpdates", offset=0, limit=100, timeout=0)
    reason = _updates_conflict(data)
    if reason:
        raise UpdatesUnavailable(reason)
    batch = data.get("result", []) if data.get("ok") else []

    seen: set[int] = set()
    users: list[dict] = []
    last_id = start_offset
    for upd in batch:
        uid_update = upd.get("update_id", 0)
        if uid_update > last_id:
            last_id = uid_update
        msg = (
            upd.get("message")
            or upd.get("edited_message")
            or upd.get("callback_query")
        )
        if not msg:
            continue
        from_user = msg.get("from") or {}
        uid = from_user.get("id")
        if not uid or uid in seen or from_user.get("is_bot"):
            continue
        seen.add(uid)
        users.append(
            {
                "user_id": uid,
                "username": from_user.get("username"),
                "first_name": from_user.get("first_name"),
                "last_name": from_user.get("last_name"),
                "language_code": from_user.get("language_code"),
            }
        )

    return users, last_id


def extract_users_from_updates(updates: list[dict]) -> list[dict]:
    """Parse unique users from a batch of Telegram updates."""
    seen: set[int] = set()
    users: list[dict] = []
    for upd in updates:
        msg = (
            upd.get("message") or upd.get("edited_message") or upd.get("callback_query")
        )
        if not msg:
            continue
        from_user = msg.get("from") or {}
        uid = from_user.get("id")
        if not uid or uid in seen or from_user.get("is_bot"):
            continue
        seen.add(uid)
        users.append(
            {
                "user_id": uid,
                "username": from_user.get("username"),
                "first_name": from_user.get("first_name"),
                "last_name": from_user.get("last_name"),
                "language_code": from_user.get("language_code"),
            }
        )
    return users


# ── Sending ───────────────────────────────────────────────────────────────


def _build_inline_keyboard(buttons: list[dict] | None) -> dict | None:
    """Build Telegram inline_keyboard from list of {text, url} dicts."""
    if not buttons:
        return None
    return {
        "inline_keyboard": [[{"text": b["text"], "url": b["url"]}] for b in buttons]
    }


async def send_message(
    session: aiohttp.ClientSession,
    token: str,
    chat_id: int,
    text: str,
    buttons: list[dict] | None = None,
    reply_markup: dict | None = None,
    disable_notification: bool = False,
) -> tuple[bool, int | None]:
    """Returns (success, retry_after_seconds_or_None)."""
    params: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if disable_notification:
        params["disable_notification"] = True
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    elif buttons:
        kb = _build_inline_keyboard(buttons)
        if kb:
            params["reply_markup"] = kb
    data = await _call(session, token, "sendMessage", **params)
    if data.get("ok"):
        return True, None
    error_code = data.get("error_code", 0)
    if error_code == 429:
        retry = retry_after_of(data) or 5
        return False, retry
    return False, None


# Категории, означающие, что ПОЛУЧАТЕЛЬ действительно недоступен: он
# заблокировал бота, удалил аккаунт или никогда не начинал диалог. Только на
# них подписчика можно выводить из аудитории (is_active=FALSE). Всё остальное —
# либо наша вина (кривой текст рассылки), либо недоступность Telegram, и живой
# подписчик за это выбывать не должен.
RECIPIENT_GONE = frozenset({"blocked", "deactivated", "not_started"})


def recipient_is_gone(category: str) -> bool:
    """Можно ли по этой категории выводить подписчика из аудитории."""
    return category in RECIPIENT_GONE


def classify_send_error(error_code: int, description: str) -> str:
    """Категория ошибки доставки для понятной статистики рассылки.

    Возвращает: 'flood' | 'blocked' | 'deactivated' | 'not_started' |
    'unauthorized' | 'unavailable' | 'bad_request' | 'other'.

    Три последние категории раньше сливались в 'other', и рассылка не могла
    отличить мёртвого подписчика от собственной сломанной разметки или от
    недоступного Telegram. Цена различия высокая: по 'other' подписчиков
    помечали неактивными, то есть одна незакрытая HTML-скобка в тексте
    выносила из аудитории ВСЕХ, кому её отправили.
    """
    d = (description or "").lower()
    if error_code == 429:
        return "flood"
    if "blocked" in d:
        return "blocked"
    if "deactivated" in d:
        return "deactivated"
    if "chat not found" in d or "user not found" in d or "can't initiate" in d or "bot can't initiate" in d:
        return "not_started"
    # Токен отозван/подменён: дальше бессмысленно бить по всей аудитории.
    if error_code == 401 or "unauthorized" in d:
        return "unauthorized"
    # Сеть/шлюз/Telegram лежит. error_code == 0 ставит _call, когда до Telegram
    # вообще не достучались.
    if error_code in (0, 500, 502, 503, 504) or "network error" in d:
        return "unavailable"
    # 400 с любым другим текстом — это МЫ прислали что-то не то (разметка,
    # длина, битый file_id). Получатель тут ни при чём.
    if error_code == 400:
        return "bad_request"
    return "other"


async def send_message_classified(
    session: aiohttp.ClientSession,
    token: str,
    chat_id: int,
    text: str,
    buttons: list[dict] | None = None,
    reply_markup: dict | None = None,
    disable_notification: bool = False,
) -> tuple[bool, int | None, str]:
    """Как send_message, но возвращает ещё категорию ошибки: (ok, retry_after, category)."""
    params: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if disable_notification:
        params["disable_notification"] = True
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    elif buttons:
        kb = _build_inline_keyboard(buttons)
        if kb:
            params["reply_markup"] = kb
    data = await _call(session, token, "sendMessage", **params)
    if data.get("ok"):
        return True, None, ""
    error_code = data.get("error_code", 0)
    cat = classify_send_error(error_code, data.get("description", ""))
    if error_code == 429:
        return False, retry_after_of(data) or 5, cat
    return False, None, cat


async def send_photo(
    session: aiohttp.ClientSession,
    token: str,
    chat_id: int,
    photo: str,
    caption: str = "",
    buttons: list[dict] | None = None,
    disable_notification: bool = False,
) -> tuple[bool, int | None]:
    """Send a photo by file_id. Returns (success, retry_after_seconds_or_None)."""
    params: dict = {"chat_id": chat_id, "photo": photo}
    if disable_notification:
        params["disable_notification"] = True
    if caption:
        params["caption"] = caption
        params["parse_mode"] = "HTML"
    kb = _build_inline_keyboard(buttons)
    if kb:
        params["reply_markup"] = kb
    data = await _call(session, token, "sendPhoto", **params)
    if data.get("ok"):
        return True, None
    error_code = data.get("error_code", 0)
    if error_code == 429:
        retry = retry_after_of(data) or 5
        return False, retry
    return False, None


async def send_photo_classified(
    session: aiohttp.ClientSession,
    token: str,
    chat_id: int,
    photo: str,
    caption: str = "",
    buttons: list[dict] | None = None,
    disable_notification: bool = False,
) -> tuple[bool, int | None, str]:
    """Как send_photo, но с категорией ошибки: (ok, retry_after, category).

    Нужен рассылке наравне с текстовым вариантом: без категории она не может
    отличить заблокировавшего бота подписчика от недоступного Telegram.
    """
    params: dict = {"chat_id": chat_id, "photo": photo}
    if disable_notification:
        params["disable_notification"] = True
    if caption:
        params["caption"] = caption
        params["parse_mode"] = "HTML"
    kb = _build_inline_keyboard(buttons)
    if kb:
        params["reply_markup"] = kb
    data = await _call(session, token, "sendPhoto", **params)
    if data.get("ok"):
        return True, None, ""
    error_code = data.get("error_code", 0)
    cat = classify_send_error(error_code, data.get("description", ""))
    if error_code == 429:
        return False, retry_after_of(data) or 5, cat
    return False, None, cat


# ── Batch operations ──────────────────────────────────────────────────────


async def batch_get_me(
    session: aiohttp.ClientSession, tokens: list[str]
) -> dict[str, dict | None]:
    """Call getMe on many bots concurrently. Returns {token: result}."""
    results = await asyncio.gather(
        *(get_me(session, t) for t in tokens), return_exceptions=True
    )
    return {
        token: (r if not isinstance(r, Exception) else None)
        for token, r in zip(tokens, results)
    }


# ── Commands ──────────────────────────────────────────────────────────────


async def get_my_commands(
    session: aiohttp.ClientSession, token: str, language_code: str = ""
) -> list[dict]:
    data = await _call(
        session, token, "getMyCommands", language_code=language_code or None
    )
    return data.get("result", []) if data.get("ok") else []


async def set_my_commands(
    session: aiohttp.ClientSession,
    token: str,
    commands: list[dict],
    language_code: str = "",
) -> bool:
    data = await _call(
        session,
        token,
        "setMyCommands",
        commands=commands,
        language_code=language_code or None,
    )
    return data.get("ok", False)


async def delete_my_commands(
    session: aiohttp.ClientSession, token: str, language_code: str = ""
) -> bool:
    data = await _call(
        session, token, "deleteMyCommands", language_code=language_code or None
    )
    return data.get("ok", False)


async def batch_set_commands(
    session: aiohttp.ClientSession,
    tokens: list[str],
    commands: list[dict],
    language_code: str = "",
) -> tuple[int, int]:
    """Apply commands to many bots concurrently. Returns (success, failed)."""
    results = await asyncio.gather(
        *(set_my_commands(session, t, commands, language_code) for t in tokens),
        return_exceptions=True,
    )
    ok = sum(1 for r in results if r is True)
    return ok, len(results) - ok
