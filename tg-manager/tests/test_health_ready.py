"""Проверка готовности `/health/ready`: база и цикл событий, а не «процесс жив».

`/health` отвечает «OK», даже когда база лежит: процесс жив, а продукт не
обслуживает ни одного экрана. Готовность обязана это различать — и не
зависать вместе с базой (иначе мониторинг узнаёт о сбое по своему таймауту,
а не по ответу).
"""
from __future__ import annotations

import asyncio
import json

from services import loop_lag, payment_webhook


class _Pool:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    async def fetchval(self, q):
        if self.behaviour == "ok":
            return 1
        if self.behaviour == "hang":
            await asyncio.sleep(60)
        raise ConnectionRefusedError("db down at 10.0.0.5:5432")


def _body(resp):
    return json.loads(resp.body)


async def test_ready_when_db_answers():
    resp = await payment_webhook.readiness_response(_Pool("ok"))
    assert resp.status == 200
    assert _body(resp)["ok"] is True
    assert resp.headers["Cache-Control"] == "no-store"


async def test_not_ready_when_db_fails_and_no_details_leak():
    resp = await payment_webhook.readiness_response(_Pool("fail"))
    assert resp.status == 503
    body = _body(resp)
    assert body["ok"] is False and body["db"]["ok"] is False
    assert "10.0.0.5" not in resp.text            # адрес базы наружу не уходит


async def test_hanging_db_does_not_hang_the_check(monkeypatch):
    monkeypatch.setattr(payment_webhook, "READY_DB_TIMEOUT_SEC", 0.1)
    resp = await asyncio.wait_for(
        payment_webhook.readiness_response(_Pool("hang")), 2)
    assert resp.status == 503


async def test_not_ready_when_event_loop_stalls(monkeypatch):
    monkeypatch.setattr(loop_lag, "snapshot", lambda: {
        "running": True, "lag_sec": 0.0, "max_lag_sec_1m": 12.0,
        "since_beat_sec": 0.1})
    resp = await payment_webhook.readiness_response(_Pool("ok"))
    assert resp.status == 503
    assert _body(resp)["loop"]["ok"] is False


def test_route_registered_next_to_liveness():
    src = open(payment_webhook.__file__, encoding="utf-8").read()
    assert 'app.router.add_get("/health/ready", health_ready)' in src
    assert 'app.router.add_get("/health", health)' in src


def test_bootstrap_server_says_not_ready_but_alive():
    """Пока идут миграции, живость — 200 (иначе платформа убьёт деплой), а
    готовность — честное 503."""
    from aiohttp import web

    import main

    ready = main._bootstrap_response(web, "/health/ready")
    alive = main._bootstrap_response(web, "/health")
    assert ready.status == 503 and json.loads(ready.body)["starting"] is True
    assert alive.status == 200
