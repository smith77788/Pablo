"""Ре-аудит add-account до идеала: качество импорта сессий.

Два дефекта, найденных ре-аудитом шага 1:
  1. Любой сбой валидации сваливался в «невалидная сессия» — мёртвый прокси/сеть/
     флуд выглядели как испорченная сессия, и пользователь мог удалить рабочую.
     Теперь причина КЛАССИФИЦИРУЕТСЯ (прокси/сеть vs недействительная сессия).
  2. Синхронная валидация N сессий (до 15с каждая) молча вешала запрос по тайм-ауту.
     Теперь порция ограничена (MAX_PER_IMPORT) с честным сообщением про остаток.
"""
from __future__ import annotations

import asyncio

from services import session_importer


class _FakePool:
    async def fetchval(self, q, *a):
        return None

    async def execute(self, q, *a):
        return "INSERT 0 1"

    async def fetchrow(self, q, *a):
        return None


def _patch(monkeypatch, validate_result):
    monkeypatch.setattr(session_importer, "detect_format", lambda line: "string_session")
    monkeypatch.setattr(session_importer, "extract_session_string", lambda data, fmt: data)

    async def _fake_validate(session_string, proxy_url=None):
        return validate_result
    monkeypatch.setattr(session_importer, "validate_session", _fake_validate)


def test_proxy_error_not_reported_as_invalid_session(monkeypatch):
    _patch(monkeypatch, {"valid": False, "error": "proxy connect timeout"})
    r = asyncio.run(session_importer.import_sessions(_FakePool(), 1, "SESSIONSTRING"))
    assert r["imported"] == 0 and r["failed"] == 1
    joined = " ".join(r["errors"]).lower()
    # причина — про прокси/сеть, НЕ «невалидная сессия»
    assert "прокси" in joined or "подключ" in joined
    assert "невалидная сессия" not in joined


def test_expired_session_classified(monkeypatch):
    _patch(monkeypatch, {"valid": False, "error": "The authorization key is unregistered"})
    r = asyncio.run(session_importer.import_sessions(_FakePool(), 1, "SESSIONSTRING"))
    joined = " ".join(r["errors"]).lower()
    assert "недействительна" in joined or "релог" in joined


def test_bulk_capped_with_honest_message(monkeypatch):
    _patch(monkeypatch, {"valid": False, "error": "proxy timeout"})
    raw = "\n".join(f"SESSION{i}" for i in range(25))
    r = asyncio.run(session_importer.import_sessions(_FakePool(), 1, raw))
    # обработано не больше лимита за раз
    assert r["failed"] <= 20
    joined = " ".join(r["errors"]).lower()
    assert "не обработано" in joined or "порцией" in joined
