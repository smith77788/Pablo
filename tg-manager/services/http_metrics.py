"""Время ответа и исходы HTTP-ручек мини-аппа — в метрики и в лог медленных.

ЗАЧЕМ. В метриках были операции, флуды и пул, но не было самого видимого
человеку: сколько отвечает мини-апп. «Тормозит» узнавалось из жалобы, и
понять, КАКАЯ ручка медленная или сыплет 500, можно было только чтением логов.

ЧТО ПИШЕТСЯ.
  * infragram_http_requests_total{route, method, status} — status классом
    (2xx/3xx/4xx/5xx), а route — ШАБЛОНОМ пути (`/api/miniapp/bots/{bot_id}`),
    не самим путём: иначе каждый id — новая строка метрики, и сборщик тонет.
  * infragram_http_request_seconds{route, method} — сумма и число, среднее
    считает сборщик;
  * infragram_http_request_max_seconds{route, method} — максимум за минуту:
    среднее прячет редкие долгие ответы, а именно их видит человек;
  * ответ дольше SLOW_SEC — предупреждение в лог с шаблоном пути и временем.

Поток событий (SSE) живёт минутами по замыслу и сюда не идёт. Сбой самой
метрики ответ не трогает.
"""
from __future__ import annotations

import logging
import time

from aiohttp import web

from services import metrics

log = logging.getLogger(__name__)

REQUESTS = "infragram_http_requests_total"
SECONDS = "infragram_http_request_seconds"
MAX_SECONDS = "infragram_http_request_max_seconds"

SLOW_SEC = 3.0
_WINDOW_SEC = 60.0
_STREAM_PREFIXES = ("/api/miniapp/events",)

# (route, method) → (максимум, начало окна). Пишет только middleware этого
# процесса; метрики per-process по замыслу (см. services/metrics.py).
_window_max: dict[tuple[str, str], tuple[float, float]] = {}


def route_label(request: web.Request) -> str:
    try:
        resource = request.match_info.route.resource
    except Exception:
        resource = None
    if resource is None:
        return "unmatched"
    try:
        return resource.canonical or "unmatched"
    except Exception:
        return "unmatched"


def _status_class(status: int) -> str:
    return f"{int(status) // 100}xx" if status else "unknown"


def record(route: str, method: str, status: int, seconds: float,
           now: float | None = None) -> None:
    labels = {"route": route, "method": method}
    metrics.inc(REQUESTS, {**labels, "status": _status_class(status)})
    metrics.observe(SECONDS, seconds, labels)
    now = time.monotonic() if now is None else now
    key = (route, method)
    cur, started = _window_max.get(key, (0.0, now))
    if now - started >= _WINDOW_SEC:
        cur, started = 0.0, now
    cur = max(cur, seconds)
    _window_max[key] = (cur, started)
    metrics.gauge(MAX_SECONDS, cur, labels)
    if seconds >= SLOW_SEC:
        log.warning("HTTP: медленный ответ %s %s — %.1f с (статус %s)",
                    method, route, seconds, status)


def make_middleware():
    @web.middleware
    async def http_metrics_middleware(request: web.Request, handler):
        if request.rel_url.path.startswith(_STREAM_PREFIXES):
            return await handler(request)
        t0 = time.monotonic()
        status = 500
        try:
            resp = await handler(request)
            status = getattr(resp, "status", 200)
            return resp
        except web.HTTPException as exc:
            status = exc.status
            raise
        finally:
            try:
                record(route_label(request), request.method, status,
                       time.monotonic() - t0)
            except Exception:
                pass

    return http_metrics_middleware
