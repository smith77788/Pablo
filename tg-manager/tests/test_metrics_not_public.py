"""/metrics не читается из интернета.

У сервиса на Railway один публичный порт, и эндпоинт метрик, открытый «в
расчёте на сеть», отдавал любому темп операций, флуды, смерти сессий и
насыщение пула. Теперь: с METRICS_TOKEN — только с Bearer-токеном, без него —
только запросы мимо публичного прокси (без X-Forwarded-For).
"""
from __future__ import annotations

from types import SimpleNamespace

from services.mini_app_api import metrics_request_allowed


def _req(**headers):
    return SimpleNamespace(headers=headers)


def test_direct_internal_request_is_allowed_without_token(monkeypatch):
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    assert metrics_request_allowed(_req()) is True


def test_request_through_public_proxy_is_refused_without_token(monkeypatch):
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    assert metrics_request_allowed(_req(**{"X-Forwarded-For": "203.0.113.5"})) is False


def test_token_is_required_when_configured(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "scrape-secret")
    assert metrics_request_allowed(_req()) is False
    assert metrics_request_allowed(_req(Authorization="Bearer wrong")) is False
    assert metrics_request_allowed(_req(Authorization="Bearer scrape-secret")) is True
    assert metrics_request_allowed(_req(**{
        "Authorization": "Bearer scrape-secret", "X-Forwarded-For": "203.0.113.5"})) is True


def test_handler_uses_the_gate():
    import inspect
    from services import mini_app_api
    src = inspect.getsource(mini_app_api)
    body = src[src.index("async def prom_metrics"):src.index('app.router.add_get("/metrics"')]
    assert "metrics_request_allowed(request)" in body
