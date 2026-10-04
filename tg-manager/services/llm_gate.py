"""Диспетчер обращений к ИИ: лимиты провайдеров — не авария, а расписание.

ЗАЧЕМ. Бесплатные модели (OpenRouter :free, Groq, Gemini) дают ограниченное
число запросов в минуту и в сутки. Раньше каждый вызов перебирал ВСЕ модели
подряд: на исчерпанном лимите каждый такт каждого канала заново долбил десяток
моделей, получал 429 и сжигал остаток квоты на повторах. С сотней каналов под
виртуальным администратором лимит кончался за минуты, а посты получали те
каналы, которым повезло оказаться первыми в цикле.

Модуль делает три вещи:
  • ПАУЗА ПО ЛИМИТУ. Ответ 429/«квота» ставит модель (или весь провайдер, если
    кончился суточный лимит) на паузу до момента сброса — из заголовков
    Retry-After / X-RateLimit-Reset / retryDelay, а без них по нарастающей.
    Пока пауза действует, к модели не идёт НИ ОДНОГО запроса.
  • ОБЩАЯ ПАМЯТЬ. Паузы лежат в БД (llm_cooldowns): их видят все процессы и
    переживают рестарт; память процесса — лишь кэш на 30 секунд.
  • УЧЁТ. llm_usage — сколько запросов ушло, сколько упёрлось в лимит, по
    провайдерам и дням: видно, хватает ли квоты сети каналов.

Решение «писать сейчас или позже» принимают вызывающие по ready()/next_ready():
виртуальный администратор пишет посты заранее, пачками и по очереди срочности
(services/channel_admin.prewrite), поэтому пауза в лимите сдвигает написание,
а не срывает публикации.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

# Пул БД и кэш пауз. Истина — таблица llm_cooldowns; кэш перечитывается не
# реже раза в _CACHE_TTL_S, так что паузу, поставленную другим процессом,
# этот увидит в пределах полуминуты.
_STATE: dict[str, Any] = {"pool": None, "cool": {}, "loaded_at": 0.0}
_CACHE_TTL_S = 30.0

_MIN_PAUSE_S = 20           # минимальная пауза модели после 429
_MAX_PAUSE_S = 6 * 3600     # потолок нарастающей паузы без подсказки провайдера
_DAILY_MARKERS = (
    "per-day", "per day", "perday", "free-models-per-day", "requests per day",
    "daily", "rpd", "quota exceeded", "exceeded your current quota",
    "resource_exhausted", "tokens per day", "tpd",
)


def attach(pool) -> None:
    """Подключить БД (зовётся при старте фоновых циклов и API)."""
    _STATE["pool"] = pool


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cool() -> dict[str, tuple[datetime, str]]:
    return _STATE["cool"]


async def _refresh(force: bool = False) -> None:
    pool = _STATE["pool"]
    if pool is None or (not force and time.monotonic() - _STATE["loaded_at"] < _CACHE_TTL_S):
        return
    _STATE["loaded_at"] = time.monotonic()
    try:
        rows = await pool.fetch(
            "SELECT key, until, reason FROM llm_cooldowns WHERE until > now()")
    except Exception:
        log.debug("llm_gate: паузы не прочитаны", exc_info=True)
        return
    fresh = {r["key"]: (r["until"], r["reason"] or "") for r in rows or []}
    # Свои только что поставленные паузы, которые ещё не долетели до БД, не теряем.
    now = _now()
    for k, v in list(_cool().items()):
        if v[0] > now and k not in fresh:
            fresh[k] = v
    _STATE["cool"] = fresh


def _until(key: str) -> Optional[datetime]:
    v = _cool().get(key)
    if v and v[0] > _now():
        return v[0]
    return None


def model_keys(provider: str, model: str) -> list[str]:
    """Ключи пауз, которые закрывают модель: сама модель, весь провайдер и —
    для бесплатных моделей OpenRouter — их общий суточный лимит."""
    keys = [f"{provider}/{model}", provider]
    if model.endswith(":free"):
        keys.append(f"{provider}:free")
    return keys


async def blocked_until(provider: str, model: str) -> Optional[datetime]:
    """До какого момента модель на паузе (None — можно звать)."""
    await _refresh()
    times = [t for t in (_until(k) for k in model_keys(provider, model)) if t]
    return max(times) if times else None


def _seconds(v: Any) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip().lower()
    if not s:
        return None
    try:
        x = float(s)
        # X-RateLimit-Reset у OpenRouter — момент сброса в миллисекундах эпохи.
        if x > 1e12:
            return x / 1000.0 - time.time()
        if x > 1e9:
            return x - time.time()
        return x
    except ValueError:
        pass
    # Groq: «2m59.56s», «7.66s», «1h2m»
    total, found = 0.0, False
    for num, unit in re.findall(r"([\d.]+)\s*(ms|h|m|s)", s):
        found = True
        total += float(num) * {"h": 3600, "m": 60, "s": 1, "ms": 0.001}[unit]
    return total if found else None


def _next_reset(provider: str, now: datetime) -> datetime:
    """Когда сбросится суточный лимит провайдера (по их правилам)."""
    # Gemini считает сутки по тихоокеанскому времени (≈08:00 UTC), остальные — по UTC.
    hour = 8 if provider == "gemini" else 0
    reset = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if reset <= now:
        reset += timedelta(days=1)
    return reset + timedelta(minutes=2)


def classify_limit(exc: Exception) -> Optional[tuple[str, Optional[float]]]:
    """Ошибка провайдера → ('minute'|'daily'|'credits', пауза в секундах или None).

    None — это не лимит (обычная ошибка, модель не трогаем паузой).
    """
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    body = getattr(exc, "body", None)
    if body is not None:
        text += " " + str(body).lower()
    if status == 402 or "insufficient credits" in text or "insufficient_quota" in text:
        return "credits", 6 * 3600.0
    limited = status == 429 or "rate limit" in text or "rate_limit" in text \
        or "too many requests" in text or "resource_exhausted" in text \
        or "quota" in text
    if not limited:
        return None
    headers = {}
    resp = getattr(exc, "response", None)
    try:
        headers = {str(k).lower(): v for k, v in dict(getattr(resp, "headers", {}) or {}).items()}
    except Exception:
        headers = {}
    hint = None
    for h in ("retry-after", "x-ratelimit-reset", "x-ratelimit-reset-requests",
              "x-ratelimit-reset-tokens"):
        hint = _seconds(headers.get(h))
        if hint is not None and hint > 0:
            break
    if hint is None:
        m = re.search(r"retry(?:delay)?['\"]?\s*[:=]?\s*['\"]?([\d.]+)\s*s", text)
        if m:
            hint = float(m.group(1))
    kind = "daily" if any(k in text for k in _DAILY_MARKERS) else "minute"
    return kind, hint


async def note_limit(provider: str, model: str, exc: Exception) -> Optional[datetime]:
    """Записать паузу по ошибке лимита. Возвращает, до какого момента (None — не лимит)."""
    got = classify_limit(exc)
    if not got:
        return None
    kind, hint = got
    now = _now()
    if kind == "daily":
        key = f"{provider}:free" if model.endswith(":free") else provider
        until = now + timedelta(seconds=hint) if hint and hint > 600 else _next_reset(provider, now)
        reason = "исчерпан суточный лимит"
    elif kind == "credits":
        key, until, reason = provider, now + timedelta(seconds=hint or 21600), "на счёте нет средств"
    else:
        key = f"{provider}/{model}"
        prev = _cool().get(key)
        strikes = 1
        if prev and prev[1].startswith("лимит в минуту"):
            m = re.search(r"#(\d+)", prev[1])
            strikes = int(m.group(1)) + 1 if m else 2
        pause = hint if hint and hint > 0 else min(_MAX_PAUSE_S, _MIN_PAUSE_S * (2 ** (strikes - 1)))
        until = now + timedelta(seconds=max(_MIN_PAUSE_S, pause))
        reason = f"лимит в минуту #{strikes}"
    await _set(key, until, reason)
    await _count(provider, limited=True)
    log.warning("ИИ: %s/%s на паузе до %s — %s", provider, model,
                until.strftime("%H:%M:%S UTC"), reason)
    return until


async def _set(key: str, until: datetime, reason: str) -> None:
    _cool()[key] = (until, reason)
    pool = _STATE["pool"]
    if pool is None:
        return
    try:
        await pool.execute(
            "INSERT INTO llm_cooldowns(key, until, reason, updated_at) VALUES($1,$2,$3,now()) "
            "ON CONFLICT (key) DO UPDATE SET until=GREATEST(llm_cooldowns.until, EXCLUDED.until), "
            "reason=EXCLUDED.reason, updated_at=now()", key, until, reason[:200])
    except Exception:
        log.debug("llm_gate: пауза не записана в БД", exc_info=True)


async def _count(provider: str, *, ok: bool = False, limited: bool = False) -> None:
    pool = _STATE["pool"]
    if pool is None:
        return
    try:
        await pool.execute(
            "INSERT INTO llm_usage(day, provider, calls, ok, limited) "
            "VALUES(CURRENT_DATE, $1, 1, $2, $3) ON CONFLICT (day, provider) DO UPDATE SET "
            "calls=llm_usage.calls+1, ok=llm_usage.ok+EXCLUDED.ok, "
            "limited=llm_usage.limited+EXCLUDED.limited",
            provider[:40], int(ok), int(limited))
    except Exception:
        log.debug("llm_gate: учёт не записан", exc_info=True)


async def note_call(provider: str, *, ok: bool) -> None:
    """Учесть запрос, который не упёрся в лимит (удачный или с иной ошибкой)."""
    await _count(provider, ok=ok)


async def note_ok(provider: str, model: str) -> None:
    """Удачный ответ снимает нарастающую паузу модели (следующий 429 — снова с минимума)."""
    key = f"{provider}/{model}"
    if key in _cool():
        _cool().pop(key, None)
    await note_call(provider, ok=True)


async def next_ready(candidates: list[tuple[str, str]]) -> tuple[bool, Optional[datetime]]:
    """Есть ли среди кандидатов (провайдер, модель) хоть одна без паузы.

    Возвращает (готов, когда освободится ближайшая — если не готов).
    """
    if not candidates:
        return False, None
    soonest: Optional[datetime] = None
    for provider, model in candidates:
        t = await blocked_until(provider, model)
        if t is None:
            return True, None
        soonest = t if soonest is None or t < soonest else soonest
    return False, soonest


async def usage_today() -> list[dict]:
    """Запросы к ИИ за сегодня по провайдерам (для экрана)."""
    pool = _STATE["pool"]
    if pool is None:
        return []
    try:
        rows = await pool.fetch(
            "SELECT provider, calls, ok, limited FROM llm_usage WHERE day=CURRENT_DATE "
            "ORDER BY provider")
    except Exception:
        return []
    return [dict(r) for r in rows or []]


async def active_pauses() -> list[dict]:
    await _refresh(force=True)
    now = _now()
    return [{"key": k, "until": v[0].isoformat(), "reason": v[1]}
            for k, v in sorted(_cool().items()) if v[0] > now]
