"""Claude (Anthropic) — первоклассный путь генерации через официальный SDK.

Модель по умолчанию — claude-opus-4-8 с adaptive thinking и effort=xhigh. Вызов
идёт СТРИМИНГОМ (`messages.stream` + `get_final_message`): при большом
`max_tokens` (64000) не-стриминговый запрос упирается в HTTP-таймаут SDK, поэтому
стриминг обязателен, а не опция.

Ключ — `ANTHROPIC_API_KEY` (env или override из БД через `ai_providers._key`).
Если ключа нет — `enabled()` возвращает False, и вызывающий уходит на
OpenAI-совместимый failover (OpenRouter/Groq/Gemini/Ollama). SDK импортируется
лениво внутри вызова: отсутствие пакета `anthropic` не ломает импорт модуля.

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


def enabled() -> bool:
    """True, если задан ANTHROPIC_API_KEY (env или override из БД)."""
    return bool(_key("ANTHROPIC_API_KEY"))


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
    key = _key("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY не задан")
    from anthropic import AsyncAnthropic  # ленивый импорт — не ломает импорт модуля

    client = AsyncAnthropic(api_key=key, timeout=timeout)
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
