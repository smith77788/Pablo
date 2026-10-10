"""Контракт экрана VA, изоляция владельцев и отказоустойчивость чтений."""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from services import channel_admin as ca
from services import va_control, va_references
from services import va_workspace as workspace

OWNER = 42
CHANNEL = -1001234567890
SECTIONS = (
    ("plan", ca, "get_plan", "План публикаций"),
    ("drafts", ca, "list_drafts", "Черновики"),
    ("report", ca, "channel_report", "Отчёт"),
    ("events", ca, "events", "Журнал"),
    ("references", va_references, "list_refs", "Каналы-образцы"),
    ("control", va_control, "load", "Решения и связи"),
)


class Pool:
    def __init__(self):
        self.channel = {"channel_id": CHANNEL, "title": "Канал", "username": "channel"}
        self.admin = {"owner_id": OWNER, "channel_id": CHANNEL, "enabled": True,
                      "topic": "Тема", "posts_per_day": 3}
        self.brain = {"owner_id": OWNER, "channel_key": str(CHANNEL), "brand_rules": {},
                      "pillars": ["Новости", "Польза"], "mix_weights": {"Новости": 2},
                      "autonomy_mode": "manual", "max_streak": 2, "dup_threshold": 0.6}
        self.calls = []
        self.errors = {}

    async def fetchrow(self, sql, *args):
        if "FROM managed_channels" in sql:
            section = "channel"
            assert "owner_id=$1 AND channel_id=$2" in sql
            assert args == (OWNER, CHANNEL)
        elif "FROM va_channel_admin" in sql:
            section = "admin"
            assert self.calls == ["channel"]
            assert "owner_id=$1 AND channel_id=$2" in sql
            assert args == (OWNER, CHANNEL)
        else:
            assert "FROM va_channel_brain" in sql
            section = "brain"
            assert self.calls == ["channel", "admin"]
            assert "owner_id=$1 AND channel_key=$2" in sql
            assert args == (OWNER, str(CHANNEL))
        self.calls.append(section)
        if section in self.errors:
            raise self.errors[section]
        return getattr(self, section)


@pytest.fixture
def env(monkeypatch):
    pool = Pool()
    values = {
        "plan": [{"id": 1, "slot_at": None, "pillar": "Новости", "topic": "Тема"}],
        "drafts": [{"id": 2, "channel_id": str(CHANNEL), "body": "Текст"}],
        "report": {"posts_7d": 3, "avg_views_7d": 20, "members": None},
        "events": [{"kind": "info", "text": "Запущено", "at": None}],
        "references": [{"id": 3, "username": "example", "kind": "competitor"}],
        "control": {"cards": [{"key": "knowledge", "level": "ok"}], "decisions": []},
    }
    calls = []

    def reader(key):
        async def read(actual_pool, owner_id, channel_id):
            assert actual_pool is pool
            assert (owner_id, channel_id) == (OWNER, CHANNEL)
            assert pool.calls == ["channel", "admin", "brain"]
            calls.append(key)
            return values[key]
        return read

    for key, module, name, _ in SECTIONS:
        monkeypatch.setattr(module, name, reader(key))
    return SimpleNamespace(pool=pool, values=values, calls=calls)


async def test_ownership_before_any_other_read(env):
    env.pool.channel = None
    assert await workspace.load_workspace(env.pool, OWNER, CHANNEL) is None
    assert env.pool.calls == ["channel"]
    assert env.calls == []


async def test_existing_contract_and_owner_scope(env):
    result = await workspace.load_workspace(env.pool, OWNER, CHANNEL)
    assert result == {
        "ok": True,
        "channel": {"id": str(CHANNEL), "title": "Канал", "username": "channel"},
        "settings": ca.settings_public(env.pool.admin),
        "pillars": [{"name": "Новости", "weight": 2}, {"name": "Польза", "weight": 1}],
        **env.values,
        "warnings": [],
    }
    assert sorted(env.calls) == sorted(env.values)


async def test_uninstalled_channel_keeps_defaults_without_extra_reads(env):
    env.pool.admin = None
    env.pool.brain = None
    env.pool.channel.update(title=None, username=None)
    result = await workspace.load_workspace(env.pool, OWNER, CHANNEL)
    assert result == {
        "ok": True,
        "channel": {"id": str(CHANNEL), "title": "", "username": ""},
        "settings": ca.settings_public(None), "pillars": [],
        "plan": [], "drafts": [], "report": None, "events": [], "references": [], "control": {},
        "warnings": [],
    }
    assert env.calls == []


@pytest.mark.parametrize("section", ["channel", "admin", "brain"])
@pytest.mark.parametrize("error_type", [RuntimeError, TimeoutError, asyncio.CancelledError])
async def test_critical_errors_are_not_masked(env, section, error_type):
    error = error_type("Ошибка базовых данных")
    env.pool.errors[section] = error
    with pytest.raises(error_type) as caught:
        await workspace.load_workspace(env.pool, OWNER, CHANNEL)
    assert caught.value is error
    assert env.calls == []


@pytest.mark.parametrize("section", ["admin", "brain"])
async def test_critical_conversion_errors_are_not_masked(env, section):
    if section == "admin":
        env.pool.admin["posts_per_day"] = "не число"
    else:
        env.pool.brain["max_streak"] = "не число"
    with pytest.raises(ValueError):
        await workspace.load_workspace(env.pool, OWNER, CHANNEL)
    assert env.calls == []


async def test_all_optional_reads_start_concurrently(env, monkeypatch):
    started = set()
    ready = asyncio.Event()

    def reader(key):
        async def read(*args):
            started.add(key)
            if len(started) == len(SECTIONS):
                ready.set()
            await ready.wait()
            return env.values[key]
        return read

    monkeypatch.setattr(workspace, "SECTION_TIMEOUT_SECONDS", 1)
    for key, module, name, _ in SECTIONS:
        monkeypatch.setattr(module, name, reader(key))
    result = await asyncio.wait_for(workspace.load_workspace(env.pool, OWNER, CHANNEL), 2)
    assert result["warnings"] == []
    assert {key: result[key] for key in env.values} == env.values


@pytest.mark.parametrize("key,module,name,label", SECTIONS)
@pytest.mark.parametrize("timeout", [False, True])
async def test_optional_failure_or_timeout_preserves_other_sections(
    env, monkeypatch, key, module, name, label, timeout,
):
    stopped = asyncio.Event()

    async def unavailable(*args):
        try:
            if timeout:
                await asyncio.Event().wait()
            raise RuntimeError("Секретные подробности БД")
        finally:
            stopped.set()

    monkeypatch.setattr(module, name, unavailable)
    monkeypatch.setattr(workspace, "SECTION_TIMEOUT_SECONDS", 0.01)
    result = await asyncio.wait_for(workspace.load_workspace(env.pool, OWNER, CHANNEL), 2)
    message = "превышено время ожидания загрузки." if timeout else "не удалось загрузить раздел."
    assert result["warnings"] == [f"{label}: {message}"]
    assert stopped.is_set()
    assert result[key] == (None if key == "report" else {} if key == "control" else [])
    assert result["settings"] == ca.settings_public(env.pool.admin)
    for other in env.values:
        if other != key:
            assert result[other] == env.values[other]


async def test_partial_failure_and_timeout_together(env, monkeypatch):
    async def failed(*args):
        raise RuntimeError("Ошибка отчёта")

    async def stalled(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(ca, "channel_report", failed)
    monkeypatch.setattr(ca, "events", stalled)
    monkeypatch.setattr(workspace, "SECTION_TIMEOUT_SECONDS", 0.01)
    result = await asyncio.wait_for(workspace.load_workspace(env.pool, OWNER, CHANNEL), 2)
    assert result["report"] is None and result["events"] == []
    assert result["warnings"] == [
        "Отчёт: не удалось загрузить раздел.",
        "Журнал: превышено время ожидания загрузки.",
    ]
    for key in ("plan", "drafts", "references", "control"):
        assert result[key] == env.values[key]


@pytest.mark.parametrize("cancel_child", [False, True])
async def test_cancellation_propagates_and_stops_all_readers(env, monkeypatch, cancel_child):
    started = set()
    stopped = set()
    ready = asyncio.Event()

    def reader(key):
        async def read(*args):
            started.add(key)
            if len(started) == len(SECTIONS):
                ready.set()
            try:
                await ready.wait()
                if cancel_child and key == "report":
                    raise asyncio.CancelledError()
                await asyncio.Event().wait()
            finally:
                stopped.add(key)
        return read

    monkeypatch.setattr(workspace, "SECTION_TIMEOUT_SECONDS", 10)
    for key, module, name, _ in SECTIONS:
        monkeypatch.setattr(module, name, reader(key))
    task = asyncio.create_task(workspace.load_workspace(env.pool, OWNER, CHANNEL))
    try:
        await asyncio.wait_for(ready.wait(), 2)
        if not cancel_child:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert task.cancelled()
        assert stopped == started == set(env.values)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("found", [False, True])
async def test_api_wrapper_delegates_without_changing_response(env, monkeypatch, found):
    path = Path(__file__).resolve().parents[1] / "services" / "mini_app_api.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {"_va_payload", "va_channel_get_for"}
    functions = [node for node in ast.walk(tree)
                 if isinstance(node, ast.AsyncFunctionDef) and node.name in names]
    assert len(functions) == len(names)
    result = {"ok": True, "warnings": ["Журнал: не удалось загрузить раздел."]} if found else None
    loader = AsyncMock(return_value=result)
    monkeypatch.setattr(workspace, "load_workspace", loader)
    scope = {"pool": env.pool, "_json_resp": lambda data: data,
             "_err": lambda message, status: (message, status)}
    module = ast.Module(body=functions, type_ignores=[])
    exec(compile(module, str(path), "exec"), scope)  # noqa: S102 - только локальный код API
    response = await scope["va_channel_get_for"](OWNER, CHANNEL)
    loader.assert_awaited_once_with(env.pool, OWNER, CHANNEL)
    if found:
        assert response is result
    else:
        assert response == ("Канал не найден среди ваших каналов", 404)
