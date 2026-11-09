"""Авария, при которой лежит весь процесс, видна снаружи.

Всё, что сообщало о поломке, жило внутри процесса: когда он лежит целиком,
сообщать некому. Пульс внешнему монитору — единственный сигнал, который
переживает смерть процесса: перестал стучать — монитор поднимает тревогу сам.
Ошибки уходят во внешний сервис с трассировкой, но без секретов.
"""
from __future__ import annotations

import asyncio
import pathlib

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from services import observability as obs

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_heartbeat_is_off_without_a_secure_address(monkeypatch):
    monkeypatch.delenv("MONITOR_HEARTBEAT_URL", raising=False)
    assert obs.heartbeat_url() is None
    monkeypatch.setenv("MONITOR_HEARTBEAT_URL", "http://insecure.example.com/hb")
    assert obs.heartbeat_url() is None
    monkeypatch.setenv("MONITOR_HEARTBEAT_URL", "https://uptime.example.com/hb/abc/")
    assert obs.heartbeat_url() == "https://uptime.example.com/hb/abc"
    monkeypatch.delenv("MONITOR_HEARTBEAT_URL")
    # Без адреса цикл пульса сразу выходит, а не крутится вхолостую.
    assert asyncio.run(asyncio.wait_for(obs.run_heartbeat(None, None), 1)) is None


def test_interval_cannot_be_typoed_into_hammering(monkeypatch):
    monkeypatch.setenv("MONITOR_HEARTBEAT_EVERY_SEC", "1")
    assert obs.heartbeat_every_sec() == 15
    monkeypatch.setenv("MONITOR_HEARTBEAT_EVERY_SEC", "мусор")
    assert obs.heartbeat_every_sec() == 60


def _beat(monkeypatch, readiness) -> list[str]:
    from services import payment_webhook
    monkeypatch.setattr(payment_webhook, "readiness", readiness)
    hits: list[str] = []

    async def any_path(request):
        hits.append(request.path)
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/{tail:.*}", any_path)

    async def go():
        server = TestServer(app)
        await server.start_server()
        try:
            async with aiohttp.ClientSession() as http:
                await obs.beat_once(None, http, str(server.make_url("/hb/abc")))
        finally:
            await server.close()
    asyncio.run(go())
    return hits


def test_healthy_process_says_alive(monkeypatch):
    async def ok(pool):
        return {"ok": True}
    assert _beat(monkeypatch, ok) == ["/hb/abc"]


def test_sick_process_reports_failure_instead_of_alive(monkeypatch):
    """База не отвечает — «жив» слать нельзя: монитор считал бы сервис здоровым."""
    async def sick(pool):
        return {"ok": False}
    assert _beat(monkeypatch, sick) == ["/hb/abc/fail"]


def test_broken_check_counts_as_failure(monkeypatch):
    async def boom(pool):
        raise RuntimeError("проверка упала")
    assert _beat(monkeypatch, boom) == ["/hb/abc/fail"]


def test_unreachable_monitor_does_not_break_the_beat(monkeypatch):
    from services import payment_webhook

    async def ok(pool):
        return {"ok": True}
    monkeypatch.setattr(payment_webhook, "readiness", ok)

    async def go():
        async with aiohttp.ClientSession() as http:
            return await obs.beat_once(None, http, "http://127.0.0.1:9/hb")
    assert asyncio.run(go()) is True


def test_every_role_starts_the_heartbeat_and_error_tracking():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "_observability.init_error_tracking(" in src
    i = src.index("_observability.run_heartbeat")
    assert "_web_resilient(" in src[i - 120:i], (
        "пульс запущен не во всех ролях: смерть воркера никто не заметит")


def test_readiness_is_one_check_for_endpoint_and_heartbeat():
    from services import payment_webhook

    class _Pool:
        async def fetchval(self, q):
            return 1

    body = asyncio.run(payment_webhook.readiness(_Pool()))
    assert body["ok"] is True and body["db"]["ok"] is True


# ── Ошибки наружу: без секретов ─────────────────────────────────────────────

_TOKEN = "7012345678:AA" + "b" * 33


def test_secrets_are_scrubbed_before_sending():
    event = {
        "exception": {"values": [{"type": "ValueError", "value": f"bad token {_TOKEN}"}]},
        "logentry": {"message": f"call with {_TOKEN}"},
        "request": {"data": "body with password"},
    }
    out = obs._scrub(event)
    text = repr(out)
    assert _TOKEN not in text, "токен бота ушёл бы во внешний сервис"
    assert "request" not in out, "тело запроса ушло бы во внешний сервис"


def test_error_tracking_is_off_without_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert obs.init_error_tracking() is False


def test_error_tracking_never_sends_frame_locals(monkeypatch):
    sentry_sdk = pytest.importorskip("sentry_sdk")
    seen: dict = {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: seen.update(kw))
    monkeypatch.setenv("SENTRY_DSN", "https://k@errors.example.com/1")
    assert obs.init_error_tracking("web") is True
    assert seen["include_local_variables"] is False, (
        "в локальных переменных кадров — строки сессий и пароли 2FA")
    assert seen["send_default_pii"] is False
    assert seen["max_request_body_size"] == "never"
    assert seen["before_send"] is obs._scrub
