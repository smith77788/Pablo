"""Одиночные операции уважают паузу Telegram и мёртвый статус аккаунта.

Массовые исполнители берут аккаунты через единую дверь
`resource_selector.select_all_active`, и она отсеивает два состояния, при
которых работать нельзя:

  * `cooldown_until` — пауза, назначенная Telegram. Явный выбор аккаунта её не
    отменяет: `include_ids` идёт вместе с `respect_cooldown=True`.
  * `acc_status` в ('banned', 'deactivated', 'session_expired', 'spamblock') —
    `is_active=TRUE` для этого не признак: спамблок ставит статус, а аккаунт
    остаётся активным.

Одиночные исполнители (объявление в группы, публикация в каналы аккаунта,
папка-чатлист, гигиена аккаунта) достают аккаунт сырым `SELECT ... WHERE id=$1
AND is_active=TRUE`, то есть мимо обоих фильтров — и шли в окно паузы за новым
штрафом или работали заведомо мёртвой сессией. Причём комментарий в
`_exec_create_chatlist_folder` сам же предупреждал: «а НЕ сырым SELECT мимо неё:
иначе можно взять аккаунт в кулдауне/на мёртвом прокси».

Пауза — не вина владельца, поэтому операция уходит в очередь (`requeue` +
`defer_s`) и продолжится сама. Мёртвый статус — отказ: ждать нечего.
"""
from __future__ import annotations

import asyncio

import pytest

from services import op_worker, account_manager, flood_engine


ACC = 8101

# Статусы, при которых аккаунт не годится для действия. Держим их здесь списком,
# а не берём из op_worker: тогда при пропавшей защите падают сами проверки, а не
# сбор модуля, и видно, какая именно связка сломалась.
_DEAD = ("banned", "deactivated", "session_expired", "spamblock")


class _FakePool:
    """Пул, отвечающий на запрос гейта и на выборку аккаунта операции."""

    def __init__(self, status="active", cd_left=0.0, channels=1):
        self.status = status
        self.cd_left = float(cd_left)
        self.channels = channels
        self.gate_reads = 0

    async def fetchrow(self, query, *args):
        if "COALESCE(acc_status, 'active') AS st" in query:
            self.gate_reads += 1
            return {"st": self.status, "cd_left": self.cd_left}
        if "FROM tg_accounts WHERE id=" in query:
            return {"id": ACC, "session_str": "s", "first_name": "acc", "phone": "+1",
                    "username": "", "device_model": "", "system_version": "",
                    "app_version": "", "lang_code": "", "system_lang_code": "",
                    "cf_relay_url": None, "proxy_id": None, "proxy_url": None}
        if "SELECT status FROM operation_queue" in query:
            return {"status": "running"}
        return None

    async def fetch(self, query, *args):
        if "FROM operation_log" in query:
            return []
        if "FROM managed_channels" in query:
            return [{"id": 1, "channel_id": -1001, "access_hash": 0, "username": ""}]
        return []

    async def execute(self, query, *args):
        return "UPDATE 1"


def _run(coro):
    # Свежий loop: общий мог быть закрыт другим async-тестом (pytest-asyncio).
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def stubs(monkeypatch):
    """Сеть и захват — заглушками; любое обращение к Telegram записывается."""
    touched: list[str] = []

    async def _dialogs(session, **kw):
        touched.append("get_dialogs")
        return []

    async def _post(session, target, text, **kw):
        touched.append("post_to_channel")
        return {"msg_id": 1}

    async def _claim(acc_id):
        touched.append("claim")
        return True

    async def _release(ids):
        return None

    async def _quarantined(pool, acc_id):
        return False

    monkeypatch.setattr(account_manager, "get_dialogs", _dialogs)
    monkeypatch.setattr(account_manager, "post_to_channel", _post)
    monkeypatch.setattr(op_worker, "try_claim_account", _claim)
    monkeypatch.setattr(op_worker, "release_accounts", _release)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _quarantined)
    op_worker._cancel_cache.clear()
    flood_engine._flood_state.pop(ACC, None)
    yield touched
    flood_engine._flood_state.pop(ACC, None)


# ── Пауза Telegram ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("fn,params", [
    (op_worker._exec_group_announce, {"acc_id": ACC, "text": "привет"}),
    (op_worker._exec_bulk_post_chans,
     {"acc_id": ACC, "channel_ids": [1], "text": "привет"}),
])
def test_paused_account_sends_the_operation_back_to_the_queue(stubs, fn, params):
    pool = _FakePool(cd_left=1800)
    res = _run(fn(pool, None, 1, 777, params))

    assert res["status"] == "requeue", res
    assert res["defer_s"] >= 1800, "операция должна вернуться после паузы, не раньше"
    assert "пауз" in res["reason"].lower(), res
    assert "get_dialogs" not in stubs and "post_to_channel" not in stubs, (
        "запрос ушёл в Telegram внутрь окна паузы — за этим новый FloodWait")


def test_hygiene_prologue_does_not_even_claim_a_paused_account(stubs):
    """Гигиена аккаунта делает десятки записей — внутрь паузы уходила серия."""
    pool = _FakePool(cd_left=1800)
    acc, err = _run(op_worker._claim_single_account(pool, 777, {"account_id": ACC}))

    assert acc is None and err is not None
    assert err["status"] == "requeue", err
    assert "пауз" in err["summary"].lower(), err
    assert "claim" not in stubs, "нет смысла занимать аккаунт, которым нельзя работать"


def test_memory_pause_counts_even_when_the_database_knows_nothing(stubs):
    """Штраф этого прогона живёт в памяти процесса и ещё не долетел до БД."""
    _run(flood_engine.record_flood(None, ACC, 1800, "publish"))
    pool = _FakePool(cd_left=0)

    res = _run(op_worker._exec_group_announce(
        pool, None, 2, 777, {"acc_id": ACC, "text": "привет"}))

    assert res["status"] == "requeue", res
    assert "get_dialogs" not in stubs


# ── Мёртвый статус ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", _DEAD)
def test_dead_status_stops_the_operation_with_a_clear_reason(stubs, status):
    pool = _FakePool(status=status)
    res = _run(op_worker._exec_group_announce(
        pool, None, 3, 777, {"acc_id": ACC, "text": "привет"}))

    assert res["status"] == "failed", res
    assert status in res["reason"], "владелец должен понять, что именно со статусом"
    assert "get_dialogs" not in stubs, "работа мёртвой сессией — расход без результата"


def test_cooldown_status_is_not_a_dead_status():
    """`record_flood` ставит acc_status='cooldown' — это пауза, а не смерть.

    Если бы 'cooldown' попал в список мёртвых, каждый флуд отправлял бы
    операцию в `failed` вместо ожидания конца паузы.
    """
    assert "cooldown" not in _DEAD and "cooldown" not in op_worker._DEAD_ACC_STATUSES
    assert "active" not in _DEAD and "active" not in op_worker._DEAD_ACC_STATUSES


def test_the_dead_list_matches_the_single_door():
    """Расхождение со списком единой двери = более слабые правила у одиночных."""
    import inspect

    from services import resource_selector

    src = inspect.getsource(resource_selector.select_all_active)
    assert tuple(op_worker._DEAD_ACC_STATUSES) == _DEAD, (
        "список мёртвых статусов у одиночных исполнителей разошёлся с тестом")
    for st in _DEAD:
        assert f"'{st}'" in src, (
            f"статус {st} отсеивают одиночные, но не массовые — или наоборот")


# ── Сторож самой проверки ────────────────────────────────────────────────────

def test_healthy_account_is_not_held_up(stubs):
    """Детектор, который останавливает всё, сломан: здоровый аккаунт работает."""
    pool = _FakePool()
    res = _run(op_worker._exec_group_announce(
        pool, None, 4, 777, {"acc_id": ACC, "text": "привет"}))

    assert pool.gate_reads == 1, "гейт обязан спрашивать состояние аккаунта"
    assert res["status"] == "done", res
    assert "get_dialogs" in stubs, "здоровый аккаунт должен дойти до работы"


def test_unreadable_state_does_not_block_the_operation(stubs, monkeypatch):
    """fail-open: сбой чтения состояния не повод отказать в операции."""
    async def _boom(*a, **k):
        raise RuntimeError("БД недоступна")
    monkeypatch.setattr(op_worker, "_safe_fetchrow", _boom)

    assert _run(op_worker._single_account_parked(_FakePool(cd_left=1800), ACC)) is None
