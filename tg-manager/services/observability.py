"""Внешний мониторинг: пульс «жив» и отправка ошибок во внешний сервис.

ЗАЧЕМ. Всё, что раньше сообщало о поломке (`platform_alert`, сторож цикла,
сторож зависших сервисов), живёт ВНУТРИ процесса. Когда процесс лежит целиком —
упал, завис, не поднялся после деплоя, кончились деньги на хостинге, — сообщать
некому, и владелец узнаёт об аварии от пользователей. Требования надёжности
R1/R11: сбой замечен ≤ 2 мин, человек оповещён ≤ 5 мин, даже если лежит всё.

ДВЕ ЧАСТИ, обе включаются переменными и без них ничего не делают.

* Пульс (`MONITOR_HEARTBEAT_URL`). Раз в `MONITOR_HEARTBEAT_EVERY_SEC` (60 с)
  процесс сам проверяет готовность — база отвечает, цикл событий не стоит
  (`payment_webhook.readiness`, та же проверка, что у `/health/ready`) — и
  стучится на адрес пульса. Готов — GET адреса, нет — GET `<адрес>/fail`.
  Перестал стучаться вовсе — сервис мониторинга сам поднимает тревогу: это и
  есть сигнал «процесс мёртв», который изнутри подать нельзя. Формат адреса —
  Better Stack Heartbeats; Healthchecks.io и Cronitor понимают то же самое.
  У каждой роли (web/worker) свой монитор: значение переменной задаётся в
  каждом сервисе Railway отдельно.

* Ошибки (`SENTRY_DSN`). Каждое `log.exception`/`log.error` уходит событием с
  трассировкой и группируется по месту. DSN принимает и Sentry, и Better Stack
  Errors (совместим с Sentry SDK). Без пакета sentry-sdk — тихо выключено.
  Секреты наружу не уходят: локальные переменные кадров не отправляются (там
  токены, строки сессий, пароли 2FA), тела запросов тоже, текст проходит
  через `redact_secrets`.
"""
from __future__ import annotations

import asyncio
import logging
import os

import aiohttp

log = logging.getLogger(__name__)

_DEFAULT_EVERY = 60.0
_MIN_EVERY = 15.0


def heartbeat_url() -> str | None:
    raw = (os.getenv("MONITOR_HEARTBEAT_URL") or "").strip().rstrip("/")
    return raw if raw.startswith("https://") else None


def heartbeat_every_sec() -> float:
    try:
        value = float(os.getenv("MONITOR_HEARTBEAT_EVERY_SEC") or _DEFAULT_EVERY)
    except ValueError:
        value = _DEFAULT_EVERY
    return max(value, _MIN_EVERY)


def ping_url(base: str, ready: bool) -> str:
    return base if ready else f"{base}/fail"


async def beat_once(pool, http: aiohttp.ClientSession, base: str) -> bool:
    """Одна проверка и один стук. Возвращает готовность. Никогда не бросает."""
    from services import payment_webhook

    try:
        ready = bool((await payment_webhook.readiness(pool)).get("ok"))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — сама проверка сломалась: это и есть сбой
        ready = False
    try:
        async with http.get(ping_url(base, ready),
                            timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status >= 400:
                log.warning("observability: монитор ответил %s на пульс", resp.status)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — сеть до монитора: следующий стук догонит
        log.info("observability: пульс не доставлен: %s", type(e).__name__)
    return ready


async def run_heartbeat(pool, http: aiohttp.ClientSession) -> None:
    base = heartbeat_url()
    if not base:
        return
    log.info("observability: пульс внешнему монитору раз в %d с", int(heartbeat_every_sec()))
    while True:
        await beat_once(pool, http, base)
        await asyncio.sleep(heartbeat_every_sec())


# ── Ошибки ──────────────────────────────────────────────────────────────────

def _scrub(event: dict, _hint: dict | None = None) -> dict:
    """Последний рубеж перед отправкой: вычистить секреты из текста события."""
    from services.secret_masking import redact_secrets

    try:
        for exc in (event.get("exception") or {}).get("values") or []:
            if exc.get("value"):
                exc["value"] = redact_secrets(str(exc["value"]))
        entry = event.get("logentry") or {}
        for key in ("message", "formatted"):
            if entry.get(key):
                entry[key] = redact_secrets(str(entry[key]))
        if event.get("message"):
            event["message"] = redact_secrets(str(event["message"]))
        event.pop("request", None)
        for crumb in (event.get("breadcrumbs") or {}).get("values") or []:
            if crumb.get("message"):
                crumb["message"] = redact_secrets(str(crumb["message"]))
    except Exception:  # noqa: BLE001 — не смогли вычистить: не отправляем вовсе
        return None  # type: ignore[return-value]
    return event


def init_error_tracking(role: str | None = None) -> bool:
    """Включить отправку ошибок, если задан SENTRY_DSN. True — включено.

    Роль процесса читается из той же переменной, что у main.py, — чтобы
    проверка роли не расползалась по main.py (tests/test_process_roles.py).
    """
    role = role or (os.getenv("INFRAGRAM_ROLE") or "all").strip().lower()
    dsn = (os.getenv("SENTRY_DSN") or "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        log.warning("observability: SENTRY_DSN задан, но пакета sentry-sdk нет")
        return False
    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=(os.getenv("RAILWAY_ENVIRONMENT_NAME") or "production"),
            release=(os.getenv("RAILWAY_GIT_COMMIT_SHA") or None),
            server_name=f"infragram-{role}",
            send_default_pii=False,
            include_local_variables=False,
            max_request_body_size="never",
            traces_sample_rate=0.0,
            integrations=[LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)],
            before_send=_scrub,
        )
    except Exception as e:  # noqa: BLE001 — кривой DSN не должен ронять старт
        log.warning("observability: отправка ошибок не включена: %s", type(e).__name__)
        return False
    log.info("observability: ошибки уходят во внешний сервис")
    return True
