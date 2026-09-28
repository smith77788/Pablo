"""Регресс: исполнитель массовой проверки ограничений каналов/чатов/ботов.

_exec_check_owned_restrictions перечисляет НАШИ сущности (managed_channels по
owner_id, managed_bots по added_by), проверяет каждую аккаунтом-наблюдателем и
складывает вердикты в честные счётчики + summary, а находки — в restriction_events
(общий пульс). Здесь Telethon/арбитр замоканы: проверяем оркестрацию, счётчики,
per-target лог и запись находок, а также что op_type подключён в реестр/диспетч.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from services import op_worker
from services import entity_restriction_check as erc
from services import operation_bus


class _FakePool:
    """Пул, различающий запросы по таблице в тексте. Пишущие — считаем."""
    def __init__(self, channels, bots, accounts):
        self._channels = channels
        self._bots = bots
        self._accounts = accounts
        self.logged: list = []          # строки operation_log
        self.events: list = []          # строки restriction_events (через _record_event)

    async def fetch(self, query, *a):
        q = query.lower()
        if "managed_channels" in q:
            return self._channels
        if "managed_bots" in q:
            return self._bots
        if "tg_accounts" in q:
            return self._accounts
        return []

    async def execute(self, query, *a):
        q = query.lower()
        if "insert into operation_log" in q:
            # (op_id, step_num, target, status, message)
            self.logged.append({"target": a[2], "status": a[3], "message": a[4]})
        elif "insert into restriction_events" in q:
            self.events.append(a)
        return "UPDATE 1"

    async def fetchval(self, query, *a):
        return 0


def _run(coro):
    return asyncio.run(coro)


class _FakeClient:
    async def disconnect(self):
        return None


def _ch(cid, username, title="Канал", type_="channel", acc_id=1):
    return {"channel_id": cid, "username": username, "title": title,
            "type": type_, "acc_id": acc_id}


def _bot(bid, username, first_name="Бот"):
    return {"bot_id": bid, "username": username, "first_name": first_name}


def _acc(i):
    return {"id": i, "session_str": "sess", "first_name": f"A{i}", "phone": "+1",
            "username": "u", "proxy_url": None}


def _verdict(status, reason="r"):
    return {"status": status, "label": erc.status_label(status),
            "reason_ru": reason, "signals": {}, "meta": {"title": "X"}}


def _patches(pool, probe_results):
    """Общий набор моков вокруг исполнителя."""
    return [
        patch.object(op_worker, "_claim_available_accounts",
                     AsyncMock(side_effect=lambda op_id, accs, owner: accs)),
        patch.object(op_worker, "_is_cancelled", AsyncMock(return_value=False)),
        patch("services.infra_memory.is_account_quarantined",
              AsyncMock(return_value=False)),
        patch("services.account_manager.connect_client",
              AsyncMock(return_value=_FakeClient())),
        patch("services.entity_restriction_check.probe_entity",
              AsyncMock(side_effect=probe_results)),
        patch("services.shadowban_monitor._record_event", AsyncMock()),
        patch("asyncio.sleep", new=AsyncMock()),
    ]


def _exec(pool, probe_results, params=None):
    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _patches(pool, probe_results):
            stack.enter_context(p)
        return _run(op_worker._exec_check_owned_restrictions(
            pool, None, 7, 99, params or {}))


def test_counts_and_summary_across_mixed_verdicts():
    pool = _FakePool(
        channels=[_ch(101, "a"), _ch(102, "b"), _ch(103, "c")],
        bots=[_bot(201, "botx")],
        accounts=[_acc(1)],
    )
    probe = [
        _verdict(erc.STATUS_CLEAN),
        _verdict(erc.STATUS_HIDDEN),
        _verdict(erc.STATUS_RESTRICTED, "блокировка по стране"),
        _verdict(erc.STATUS_CLEAN),  # бот
    ]
    res = _exec(pool, probe)
    assert res["status"] == "done"
    assert res["checked"] == 4
    assert res["counts"].get(erc.STATUS_CLEAN) == 2
    assert res["counts"].get(erc.STATUS_HIDDEN) == 1
    assert res["counts"].get(erc.STATUS_RESTRICTED) == 1
    assert "Проверено сущностей: 4" in res["summary"]


def test_problem_verdicts_recorded_to_restriction_events():
    pool = _FakePool(channels=[_ch(101, "a")], bots=[], accounts=[_acc(1)])
    with patch("services.shadowban_monitor._record_event", AsyncMock()) as rec, \
         patch.object(op_worker, "_claim_available_accounts",
                      AsyncMock(side_effect=lambda op_id, accs, owner: accs)), \
         patch.object(op_worker, "_is_cancelled", AsyncMock(return_value=False)), \
         patch("services.infra_memory.is_account_quarantined", AsyncMock(return_value=False)), \
         patch("services.account_manager.connect_client", AsyncMock(return_value=_FakeClient())), \
         patch("services.entity_restriction_check.probe_entity",
               AsyncMock(side_effect=[_verdict(erc.STATUS_RESTRICTED, "порно-блок")])), \
         patch("asyncio.sleep", new=AsyncMock()):
        res = _run(op_worker._exec_check_owned_restrictions(
            pool, None, 7, 99, {}))
    assert res["counts"].get(erc.STATUS_RESTRICTED) == 1
    # Находка ушла в общий пульс (event_type + severity из чистого модуля).
    assert rec.await_count == 1
    args = rec.await_args_list[0].args
    assert args[2] == "channel_restricted"   # event_type
    assert args[3] == "critical"             # severity


def test_clean_verdict_not_recorded_as_event():
    pool = _FakePool(channels=[_ch(101, "a")], bots=[], accounts=[_acc(1)])
    res = _exec(pool, [_verdict(erc.STATUS_CLEAN)])
    assert res["counts"].get(erc.STATUS_CLEAN) == 1
    assert pool.events == []            # ничего не записано в restriction_events
    # но per-target лог всё равно есть — вердикт по каждой сущности виден
    assert len(pool.logged) == 1
    assert pool.logged[0]["status"] == "ok"


def test_dead_observer_makes_entity_check_failed_not_crash():
    # Наблюдатель не подключился (connect_client падает) — сущность честно
    # помечается «не удалось проверить», прогон не падает.
    pool = _FakePool(channels=[_ch(101, "a")], bots=[], accounts=[_acc(1)])
    with patch.object(op_worker, "_claim_available_accounts",
                      AsyncMock(side_effect=lambda op_id, accs, owner: accs)), \
         patch.object(op_worker, "_is_cancelled", AsyncMock(return_value=False)), \
         patch("services.infra_memory.is_account_quarantined", AsyncMock(return_value=False)), \
         patch("services.account_manager.connect_client",
               AsyncMock(side_effect=Exception("AUTH_KEY_DUPLICATED"))), \
         patch("asyncio.sleep", new=AsyncMock()):
        res = _run(op_worker._exec_check_owned_restrictions(pool, None, 7, 99, {}))
    assert res["status"] == "done"
    assert res["counts"].get(erc.STATUS_CHECK_FAILED) == 1


def test_no_entities_fails_clearly():
    pool = _FakePool(channels=[], bots=[], accounts=[_acc(1)])
    res = _exec(pool, [])
    assert res["status"] == "failed"
    assert "Нет" in res["summary"]


def test_no_accounts_fails_clearly():
    pool = _FakePool(channels=[_ch(101, "a")], bots=[], accounts=[])
    res = _exec(pool, [])
    assert res["status"] == "failed"


# ── реестр/диспетч: op_type подключён во всех трёх местах ─────────────────────

def test_op_type_registered_and_dispatched():
    assert "check_owned_restrictions" in operation_bus.OP_REGISTRY
    meta = operation_bus.OP_REGISTRY["check_owned_restrictions"]
    assert meta.get("description") and meta.get("icon")
    table = op_worker.dispatch_table()
    assert table.get("check_owned_restrictions") is op_worker._exec_check_owned_restrictions


# ── API + UI: цепочка UI→route→submit подключена ──────────────────────────────

def test_api_routes_and_submit_wired():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    api = (root / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    # оба роута зарегистрированы
    assert '"/api/miniapp/check_owned_restrictions/targets"' in api
    assert '"/api/miniapp/check_owned_restrictions"' in api
    # submit идёт именно этим op_type (а не прямой INSERT мимо operation_bus)
    assert 'submit(pool, uid, "check_owned_restrictions"' in api
    # боты скоупятся по added_by, каналы — по owner_id (managed_bots без owner_id)
    assert "FROM managed_bots WHERE added_by=$1" in api


def test_ui_screen_wired():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    html = (root / "mini_app" / "index.html").read_text(encoding="utf-8")
    js = (root / "mini_app" / "screens" / "shield_check.js").read_text(encoding="utf-8")
    # точка входа (плитка «Безопасность») и подключение экрана
    assert 'onclick="openShieldCheck()"' in html
    assert 'src="screens/shield_check.js"' in html
    # экран зовёт наш API и переходит в детали операции
    assert "/api/miniapp/check_owned_restrictions" in js
    assert "function openShieldCheck" in js and "function runShieldCheck" in js
