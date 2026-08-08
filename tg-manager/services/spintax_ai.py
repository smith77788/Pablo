"""Вызов LLM для spintax-модуля — общий для бота и Mini App.

Перебирает уже подключённые провайдеры (OpenRouter/Groq/Gemini/Ollama) через
OpenAI-совместимый API и возвращает сырой ответ модели. Для качества специально
использует сильные модели (слабые 3B/7B дают брак: чужой язык, выдуманные слова,
потерю смысла) — их можно переопределить переменной окружения ``SPIN_MODELS``.
"""

from __future__ import annotations

import logging
import os

from services.ai_providers import configured_providers
from services.logger import log_exc_swallow
from services.spintax_service import SpintaxServiceError

log = logging.getLogger(__name__)

_SPIN_TEMPERATURE = 0.35  # низкая: модель точнее следует тексту, меньше «творчества»

# Сильные модели специально для /spin. Порядок = приоритет; для OpenRouter
# переопределяется переменной окружения SPIN_MODELS (список через запятую).
_SPIN_PREFERRED_MODELS: dict[str, str] = {
    "openrouter": (
        "deepseek/deepseek-chat-v3-0324:free,"
        "meta-llama/llama-3.3-70b-instruct:free,"
        "google/gemini-2.0-flash-exp:free"
    ),
    "groq": "llama-3.3-70b-versatile",
    "gemini": "gemini-2.0-flash",
}


def models_for(provider) -> list[str]:
    """Список моделей для /spin: сильные впереди, дефолтные провайдера — запас."""
    if provider.name == "openrouter":
        raw = os.getenv("SPIN_MODELS", "").strip() or _SPIN_PREFERRED_MODELS["openrouter"]
    else:
        raw = _SPIN_PREFERRED_MODELS.get(provider.name, "")
    preferred = [m.strip() for m in raw.split(",") if m.strip()]
    # запасные модели провайдера, которых ещё нет в списке
    return preferred + [m for m in provider.models if m not in preferred]


async def complete(system: str, user: str) -> str:
    """Единичный запрос к LLM. Предпочитает Claude Opus 4.8 (если задан
    ANTHROPIC_API_KEY), иначе/при сбое — перебор OpenAI-совместимых провайдеров."""
    # 1) Claude Opus 4.8 (adaptive thinking, effort=xhigh) — предпочтительный путь.
    from services import ai_claude

    if ai_claude.enabled():
        try:
            text = await ai_claude.complete(system, user)
            if text.strip():
                return text
        except Exception:  # noqa: BLE001 - failover на OpenAI-совместимые провайдеры
            log_exc_swallow(log, "spin: Claude недоступен, failover на OpenAI-провайдеры")

    # 2) OpenAI-совместимый failover (OpenRouter/Groq/Gemini/Ollama).
    providers = configured_providers()
    if not providers:
        if ai_claude.enabled():
            raise SpintaxServiceError("Claude не ответил, других AI-провайдеров нет")
        raise SpintaxServiceError(
            "AI не настроен: добавьте ANTHROPIC_API_KEY, OPENROUTER_API_KEY, "
            "GROQ_API_KEY или GEMINI_API_KEY"
        )
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise SpintaxServiceError("библиотека openai не установлена") from exc

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    last_error: Exception | None = None
    for provider in providers:
        for model in models_for(provider):
            client = AsyncOpenAI(
                api_key=provider.api_key,
                base_url=provider.base_url,
                timeout=45.0,
            )
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=2500,
                    temperature=_SPIN_TEMPERATURE,
                )
                text = response.choices[0].message.content or ""
                if text.strip():
                    return text
            except Exception as exc:  # noqa: BLE001 - failover по провайдерам
                last_error = exc
                log_exc_swallow(log, f"spin: провайдер {provider.name}/{model} не ответил")
                continue
    raise SpintaxServiceError(
        f"ни один AI-провайдер не ответил: {last_error}" if last_error else "AI недоступен"
    )
