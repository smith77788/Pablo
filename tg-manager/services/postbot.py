"""Распознавание и публикация подготовленных постов из ``@PostBot``.

PostBot выдаёт короткую ссылку-код. Такой код нельзя отправлять в канал как
обычный текст: Telegram должен сначала получить inline-результат бота, а затем
отправить именно этот результат. Только этот путь сохраняет вложения, кнопки и
форматирование подготовленного поста.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

POSTBOT_USERNAME = "PostBot"
log = logging.getLogger(__name__)
_EXPLICIT_REFERENCE_RE = re.compile(
    r"^\s*@?postbot\s*[: ]\s*([A-Za-z0-9_-]{6,64})\s*$",
    re.IGNORECASE,
)
_EXPLICIT_INTENT_RE = re.compile(r"^\s*@postbot\b", re.IGNORECASE)
# PostBot currently issues codes such as ``40599y1mvoikjid``. Bare values are
# accepted only in this narrow form, otherwise an ordinary one-word post could
# be mistaken for a bot reference.
_BARE_REFERENCE_RE = re.compile(r"^\s*(\d{4,8}[a-z0-9]{8,40})\s*$")
_RETRYABLE_RESULT_ERRORS = {"InlineResultExpiredError", "QueryIdInvalidError"}
_BOT_UNAVAILABLE_ERRORS = {
    "BotInlineDisabledError",
    "BotResponseTimeoutError",
    "UsernameInvalidError",
    "UsernameNotOccupiedError",
}


@dataclass(frozen=True, slots=True)
class PostBotReference:
    code: str


class PostBotPublishError(RuntimeError):
    """Понятная пользователю ошибка получения или отправки PostBot-поста."""


def parse_reference(value: str | None) -> PostBotReference | None:
    """Вернуть PostBot-код, только если всё сообщение является ссылкой на него."""
    if not isinstance(value, str):
        return None
    explicit = _EXPLICIT_REFERENCE_RE.fullmatch(value)
    if explicit:
        return PostBotReference(explicit.group(1))
    bare = _BARE_REFERENCE_RE.fullmatch(value)
    if bare and any(char.isalpha() for char in bare.group(1)):
        return PostBotReference(bare.group(1))
    return None


def has_explicit_intent(value: str | None) -> bool:
    """Пользователь явно написал ``@PostBot``, даже если код после него битый."""
    return isinstance(value, str) and bool(_EXPLICIT_INTENT_RE.match(value))


def _query_error(exc: BaseException) -> PostBotPublishError | None:
    name = type(exc).__name__
    if isinstance(exc, asyncio.TimeoutError) or name == "BotResponseTimeoutError":
        return PostBotPublishError(
            "PostBot не ответил вовремя. Повторите публикацию через несколько секунд"
        )
    if name in _BOT_UNAVAILABLE_ERRORS:
        return PostBotPublishError(
            "PostBot сейчас недоступен для этого аккаунта. Откройте @PostBot "
            "этим аккаунтом и повторите публикацию"
        )
    return None


def _check_content_safety(result: Any) -> None:
    """Не позволить inline-коду обходить общий текстовый safety-гейт."""
    prepared_text = str(getattr(getattr(result, "message", None), "message", "") or "")
    if not prepared_text:
        return
    try:
        from services import content_safety

        verdict = content_safety.scan_text(prepared_text)
    except Exception:
        log.exception("Не удалось проверить содержимое PostBot перед публикацией")
        raise PostBotPublishError(
            "Не удалось безопасно проверить публикацию PostBot. Повторите позже"
        )
    if verdict.blocked:
        raise PostBotPublishError(
            "Публикация PostBot заблокирована проверкой безопасности содержимого"
        )


async def publish(
    client: Any,
    peer: Any,
    reference: PostBotReference,
    *,
    timeout: float,
) -> Any:
    """Получить inline-результат PostBot и отправить его в ``peer``.

    Результат может истечь между двумя MTProto-запросами. В этом редком случае
    выполняется один новый запрос; повтор безопасен, потому что истёкший result id
    Telegram не отправил.
    """
    for attempt in range(2):
        try:
            results = await asyncio.wait_for(
                client.inline_query(
                    POSTBOT_USERNAME,
                    reference.code,
                    entity=peer,
                ),
                timeout=timeout,
            )
        except PostBotPublishError:
            raise
        except Exception as exc:
            friendly = _query_error(exc)
            if friendly is not None:
                raise friendly from exc
            raise

        if not results:
            raise PostBotPublishError(
                "PostBot не нашёл публикацию по этому коду. Проверьте код "
                "и убедитесь, что пост не удалён"
            )
        _check_content_safety(results[0])
        try:
            return await asyncio.wait_for(
                results[0].click(entity=peer),
                timeout=timeout,
            )
        except Exception as exc:
            if type(exc).__name__ in _RETRYABLE_RESULT_ERRORS and attempt == 0:
                continue
            # Ошибки канала, FloodWait и сети должен классифицировать общий
            # отправитель: это сохраняет карантин, повтор и честный статус.
            raise

    raise PostBotPublishError("Не удалось обновить просроченный результат PostBot")
