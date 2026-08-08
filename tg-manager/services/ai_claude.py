"""Claude (Anthropic) — первоклассный путь генерации через официальный SDK.

Модель по умолчанию — claude-opus-4-8 с adaptive thinking и effort=xhigh. Вызов
идёт СТРИМИНГОМ (`messages.stream` + `get_final_message`): при большом
`max_tokens` (64000) не-стриминговый запрос упирается в HTTP-таймаут SDK, поэтому
стриминг обязателен, а не опция.

Подключение — БЕЗ обязательного API-ключа: SDK сам резолвит креды окружения по
цепочке (см. ниже). Поддерживаются три способа, в порядке приоритета:
  1) явный API-ключ  — `ANTHROPIC_API_KEY` (env или override из БД);
  2) явный auth-token — `ANTHROPIC_AUTH_TOKEN` (env или override из БД) —
     подключение напрямую по Bearer-токену, без API-ключа;
  3) ambient (OAuth-профиль на диске / Workload Identity Federation / любой
     дефолтный резолв SDK) — включается флагом `ANTHROPIC_USE_AMBIENT=1`, чтобы
     не пытаться подключиться, когда ничего не настроено.
Если ничего не задано — `enabled()` = False, и вызывающий уходит на
OpenAI-совместимый failover. SDK импортируется лениво: отсутствие пакета
`anthropic` не ломает импорт модуля. Базовый URL — `ANTHROPIC_BASE_URL`
(если задан в окружении), иначе дефолт SDK.

Переопределения через env: ANTHROPIC_MODEL, ANTHROPIC_EFFORT, ANTHROPIC_MAX_TOKENS.
"""

from __future__ import annotations

import logging
import os

from services.ai_providers import _key

log = logging.getLogger(__name__)

# adaptive thinking у Opus 4.8 ВЫКЛЮЧЕН по умолчанию — включаем явно.
# effort=xhigh — рекомендованный уровень для кодинга/агентных задач.
MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")
EFFORT: str = os.getenv("ANTHROPIC_EFFORT", "xhigh")


def _max_tokens() -> int:
    try:
        return int(os.getenv("ANTHROPIC_MAX_TOKENS", "64000"))
    except (TypeError, ValueError):
        return 64000


def _explicit_credential() -> tuple[str | None, str | None]:
    """Явный кред: ('api_key', k) | ('auth_token', t) | (None, None)."""
    api_key = _key("ANTHROPIC_API_KEY")
    if api_key:
        return "api_key", api_key
    auth_token = _key("ANTHROPIC_AUTH_TOKEN")
    if auth_token:
        return "auth_token", auth_token
    return None, None


def _ambient_allowed() -> bool:
    """Разрешён ли keyless-резолв кредов SDK (OAuth-профиль/WIF/дефолт). По флагу
    ANTHROPIC_USE_AMBIENT — иначе не пытаемся, чтобы не падать без настройки."""
    return os.getenv("ANTHROPIC_USE_AMBIENT", "").strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    """True, если есть чем подключиться: явный ключ/токен или ambient-режим."""
    kind, _val = _explicit_credential()
    return bool(kind) or _ambient_allowed()


def _make_client(timeout: float):
    """Собрать AsyncAnthropic: явный ключ/токен, иначе — keyless (SDK резолвит
    креды окружения: OAuth-профиль, WIF, ANTHROPIC_BASE_URL)."""
    from anthropic import AsyncAnthropic  # ленивый импорт

    kind, val = _explicit_credential()
    if kind == "api_key":
        return AsyncAnthropic(api_key=val, timeout=timeout)
    if kind == "auth_token":
        return AsyncAnthropic(auth_token=val, timeout=timeout)
    # keyless: без явного креда — SDK резолвит окружение сам (auth_token из env,
    # OAuth-профиль на диске, Workload Identity Federation).
    return AsyncAnthropic(timeout=timeout)


def _extract_text(message) -> str:
    """Собрать текст из блоков ответа, пропустив thinking-блоки (при adaptive
    thinking в content приходят и они, у Opus 4.8 их текст по умолчанию пуст)."""
    parts = []
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts).strip()


async def complete(system: str, user: str, *, timeout: float = 120.0) -> str:
    """Единичный запрос к Claude Opus 4.8 (adaptive thinking, effort=xhigh),
    стримингом. Возвращает текст ответа. Кидает, если ключ не задан или SDK/сеть
    отказали (вызывающий делает failover)."""
    if not enabled():
        raise RuntimeError("Anthropic не настроен: задайте ANTHROPIC_API_KEY, "
                           "ANTHROPIC_AUTH_TOKEN или ANTHROPIC_USE_AMBIENT=1")
    client = _make_client(timeout)
    # Стриминг обязателен при большом max_tokens (иначе таймаут SDK); финальное
    # сообщение берём через get_final_message() — не нужно ловить события руками.
    async with client.messages.stream(
        model=MODEL,
        max_tokens=_max_tokens(),
        thinking={"type": "adaptive"},
        output_config={"effort": EFFORT},
        system=system,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        message = await stream.get_final_message()
    return _extract_text(message)
