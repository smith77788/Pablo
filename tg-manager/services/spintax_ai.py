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
    failures: list[tuple[str, str]] = []
    for provider in providers:
        client = AsyncOpenAI(api_key=provider.api_key, base_url=provider.base_url, timeout=45.0)
        tried: list[str] = []
        models = list(models_for(provider))
        discovered = False
        while models:
            model = models.pop(0)
            tried.append(model)
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=2500,
                    temperature=_SPIN_TEMPERATURE,
                )
                choice = response.choices[0]
                text = choice.message.content or ""
                if getattr(choice, "finish_reason", None) == "length":
                    # Модель упёрлась в лимит ответа (у «думающих» моделей его съедают
                    # рассуждения): текст оборван на полуслове — в канал такой не отдаём.
                    failures.append((f"{provider.name}/{model}", _CUT))
                    log.warning("ИИ: %s/%s — %s", provider.name, model, _CUT)
                elif text.strip():
                    return text
                else:
                    failures.append((f"{provider.name}/{model}", "пустой ответ"))
            except Exception as exc:  # noqa: BLE001 - failover по провайдерам
                reason = explain_error(exc)
                failures.append((f"{provider.name}/{model}", reason))
                log.warning("ИИ: %s/%s не ответил — %s", provider.name, model, reason)
            # Бесплатные модели OpenRouter снимают без предупреждения, и зашитый
            # список целиком превращается в «404 No endpoints found». Когда
            # заготовленные кончились, а хоть одна оказалась снятой, берём
            # живые бесплатные модели прямо из каталога провайдера.
            if (not models and not discovered and provider.name == "openrouter"
                    and any(r == _GONE for _, r in failures)):
                discovered = True
                models = [m for m in await _live_free_models(client) if m not in tried]
    raise SpintaxServiceError(summarize_failures(failures))


_GONE = "модель снята у провайдера"
_CUT = "ответ оборван по лимиту длины"


def explain_error(exc: Exception) -> str:
    """Ошибка провайдера → короткая причина по-русски (для журнала и экрана)."""
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    if status == 404 or "no endpoints found" in text or "model_not_found" in text \
            or "does not exist" in text:
        return _GONE
    if status == 401 or "invalid api key" in text or "unauthorized" in text:
        return "ключ не принят"
    if status == 402 or "insufficient" in text or "credits" in text:
        return "на счёте нет средств"
    if status == 429 or "rate limit" in text:
        return "превышен лимит запросов"
    if "timeout" in text or "timed out" in text:
        return "не ответила вовремя"
    if isinstance(status, int) and status >= 500:
        return "сбой на стороне провайдера"
    return "ошибка: " + str(exc)[:80]


def summarize_failures(failures: list[tuple[str, str]]) -> str:
    """Все попытки → одно понятное сообщение: какие модели пробовали и почему нет."""
    if not failures:
        return "ИИ недоступен"
    by_reason: dict[str, list[str]] = {}
    for model, reason in failures:
        by_reason.setdefault(reason, []).append(model)
    parts = []
    for reason, models in by_reason.items():
        shown = ", ".join(models[:3]) + (f" и ещё {len(models) - 3}" if len(models) > 3 else "")
        parts.append(f"{reason}: {shown}")
    return (f"ИИ не ответил — перепробовано моделей: {len(failures)}. "
            + "; ".join(parts))[:600]


async def _live_free_models(client, limit: int = 4) -> list[str]:
    """Живые бесплатные модели из каталога OpenRouter (сильные семейства первыми)."""
    try:
        page = await client.models.list()
        ids = [m.id for m in getattr(page, "data", []) or []]
    except Exception as exc:  # noqa: BLE001 - каталог недоступен — просто нечего добавить
        log.warning("ИИ: каталог моделей OpenRouter недоступен — %s", explain_error(exc))
        return []
    free = [i for i in ids if isinstance(i, str) and i.endswith(":free")]
    prefer = ("deepseek", "llama-3.3", "qwen", "gemini", "mistral-small", "gemma")

    def rank(mid: str) -> int:
        # «Думающие» модели тратят лимит ответа на рассуждения и обрывают текст — в конец.
        if any(k in mid for k in ("-r1", "think", "reason", "qwq")):
            return len(prefer) + 1
        for n, key in enumerate(prefer):
            if key in mid:
                return n
        return len(prefer)

    return sorted(free, key=rank)[:limit]
