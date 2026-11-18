"""Автопилот ИИ-ассистента: ИИ исполняет действия сразу, а не только черновиком.

Просьба владельца (18.11.2026): «Система не обращается к нашему ИИ для решения
любых проблем хотя могла бы… я просил чтобы наш ИИ был задействован везде и не
только давал рекомендации и черновики операций а и реально запускал действия в
том числе на своё усмотрение».

Автопилот — на владельца, по умолчанию ВЫКЛЮЧЕН (явное согласие). Снимает ровно
шаг ручного подтверждения в чате: действие идёт тем же execute_action →
operation_bus со всеми гейтами исполнителей.
"""
from __future__ import annotations

import asyncio
import os

from services import ai_autopilot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


# ── Хелпер на заглушке пула (поведение set→get) ──────────────────────────────

class _FakePool:
    """Минимальный пул: хранит ai_autopilot по user_id, понимает UPSERT и SELECT."""
    def __init__(self):
        self.store: dict[int, bool] = {}

    async def execute(self, sql, *args):
        if "INSERT INTO platform_users" in sql and "ai_autopilot" in sql:
            self.store[int(args[0])] = bool(args[1])
        return "INSERT 0 1"

    async def fetchval(self, sql, *args):
        if "ai_autopilot" in sql and "platform_users" in sql:
            return self.store.get(int(args[0]), False)
        return None


def test_default_disabled():
    pool = _FakePool()
    assert asyncio.run(ai_autopilot.is_enabled(pool, 777)) is False


def test_set_then_get_roundtrip():
    pool = _FakePool()
    assert asyncio.run(ai_autopilot.set_enabled(pool, 777, True)) is True
    assert asyncio.run(ai_autopilot.is_enabled(pool, 777)) is True
    assert asyncio.run(ai_autopilot.set_enabled(pool, 777, False)) is False
    assert asyncio.run(ai_autopilot.is_enabled(pool, 777)) is False


def test_read_failure_is_closed_false():
    class _Broken:
        async def fetchval(self, *a, **k):
            raise RuntimeError("db down")
    assert asyncio.run(ai_autopilot.is_enabled(_Broken(), 1)) is False


# ── Проводка в ассистенте ────────────────────────────────────────────────────

def test_assistant_consults_autopilot_and_executes():
    src = _src("bot/handlers/ai_assistant.py")
    pend = src[src.index('reply.get("__pending__")'):]
    pend = pend[:pend.index("# Regular AI response")]
    assert "ai_autopilot" in pend and "is_enabled(" in pend
    assert "execute_action(" in pend, "в режиме автопилота действие должно исполняться"
    # Есть кнопка включения на подтверждении и обработчики вкл/выкл.
    assert 'action="autopilot_on"' in src and 'action="autopilot_off"' in src
    assert "cb_ai_autopilot_on" in src and "cb_ai_autopilot_off" in src


def test_still_defaults_to_confirmation():
    """Без автопилота путь прежний: показывается подтверждение, а не авто-запуск."""
    src = _src("bot/handlers/ai_assistant.py")
    assert 'callback_data=AiCb(action="confirm_action")' in src
    # Авто-исполнение — только под if _auto.
    pend = src[src.index('reply.get("__pending__")'):]
    assert "if _auto:" in pend


def test_migration_adds_column():
    api = _src("services/mini_app_api.py")
    assert "ADD COLUMN IF NOT EXISTS ai_autopilot BOOLEAN" in api
    assert os.path.exists(os.path.join(ROOT, "schema_v320_ai_autopilot.sql"))
