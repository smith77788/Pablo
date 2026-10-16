"""Пауза, назначенная Telegram, переживает прогон и не обходится следующей целью.

`bounded_flood_sleep` обрезает флуд-паузу внутри прогона и обещает в докстринге,
что лимиты Telegram это не обходит: «штраф аккаунту уже записан в flood_engine,
и дальше его держат пейсинг и карантин». Для публикаторов обещание было
неправдой сразу с двух сторон:

* `bulk_post_to_channel` и `bulk_post_chans` вообще НЕ звали `record_flood` —
  FloodWait попадал только в отчёт («пропущен») и в журнал целей. В движке
  темпа аккаунт оставался отдохнувшим: следующая операция брала его сразу и
  шла за новым штрафом, а подборщики флота (`resource_selector`,
  `fleet_pulse`, `flood_engine`) не знали, что аккаунт под паузой.
* `bulk_post_chans` ведёт ВЕСЬ прогон одним аккаунтом, а пауза назначается
  аккаунту на метод публикации, а не каналу. После урезанного сна прогон шёл
  в следующий канал тем же аккаунтом — то есть повторным запросом в окно
  паузы, за которым Telegram присылает новый FloodWait и продлевает штраф
  (`record_flood` добавляет +30с за каждый подряд идущий флуд).
  `mass_publish` считал темп тем же способом: `delay_seconds` владельца, без
  вопроса к `flood_engine`.

Ждать всю паузу занятым слотом по-прежнему нельзя — это простой флота. Поэтому
короткий остаток досиживается на месте, а длинный ждёт в очереди: операция
возвращается с `requeue`/`defer_s`, а повтор пропускает уже опубликованные цели
по журналу (`completed_targets`), так что дублей не будет.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from services import op_worker, account_manager, flood_engine, resource_selector


class _FakePool:
    """Минимальный пул: каналы, аккаунт, журнал целей и счётчик прогресса."""

    def __init__(self, channels=2):
        self.channels = channels
        self.logged: list[tuple] = []
        self.done_items = 0

    async def fetch(self, query, *args):
        if "FROM operation_log" in query:
            return []
        if "FROM managed_channels" in query:
            return [{"id": i, "channel_id": -1000 - i, "access_hash": 0, "username": ""}
                    for i in range(1, self.channels + 1)]
        return []

    async def fetchrow(self, query, *args):
        if "SELECT status FROM operation_queue" in query:
            return {"status": "running"}
        if "FROM tg_accounts WHERE id=" in query:
            return {"id": args[0], "session_str": f"s{args[0]}",
                    "first_name": f"acc{args[0]}", "phone": "+1",
                    "device_model": "", "system_version": "", "app_version": "",
                    "lang_code": "", "system_lang_code": "", "proxy_url": None}
        return None

    async def execute(self, query, *args):
        if "INSERT INTO operation_log" in query:
            self.logged.append((args[2], args[3]))
        if "SET done_items=done_items+1" in query:
            self.done_items += 1
        return "UPDATE 1"


def _run(coro):
    # Свежий loop: общий мог быть закрыт другим async-тестом (pytest-asyncio).
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def harness(monkeypatch):
    """Заглушки сети и захвата + запись всех снов и всех постов."""
    posted: list[str] = []
    slept: list[float] = []
    replies: dict = {"result": {"msg_id": 1}, "per_call": []}

    async def _post(session, target, text, **kw):
        posted.append(str(session))
        if replies["per_call"]:
            return replies["per_call"].pop(0)
        return replies["result"]

    async def _claim_one(acc_id):
        return True

    async def _claim_many(ids):
        return list(ids)

    async def _release(ids):
        return None

    async def _quarantined(pool, acc_id):
        return False

    _real_sleep = asyncio.sleep

    async def _no_sleep(secs=0, *a, **k):
        slept.append(float(secs or 0))
        await _real_sleep(0)

    monkeypatch.setattr(account_manager, "post_to_channel", _post)
    monkeypatch.setattr(op_worker, "try_claim_account", _claim_one)
    monkeypatch.setattr(op_worker, "try_claim_accounts", _claim_many)
    monkeypatch.setattr(op_worker, "release_accounts", _release)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _quarantined)
    monkeypatch.setattr(op_worker.asyncio, "sleep", _no_sleep)
    op_worker._cancel_cache.clear()
    yield {"posted": posted, "slept": slept, "replies": replies}


@pytest.fixture
def clean_flood_state():
    """Штраф живёт в памяти процесса — убираем свои аккаунты до и после теста."""
    ids = (7001, 7002, 7003, 7004, 7005)

    def _wipe():
        for _i in ids:
            flood_engine._flood_state.pop(_i, None)

    _wipe()
    yield ids
    _wipe()


# ── bulk_post_chans: один аккаунт → много каналов ────────────────────────────

def test_long_pause_stops_the_run_instead_of_the_next_channel(harness, clean_flood_state):
    """Длинная пауза: второй канал НЕ публикуется, операция уходит в очередь."""
    acc_id = clean_flood_state[0]
    harness["replies"]["per_call"] = [{"error": "FLOOD_WAIT_3600", "flood_wait": 3600}]
    pool = _FakePool(channels=2)

    res = _run(op_worker._exec_bulk_post_chans(
        pool, None, 1, 777,
        {"acc_id": acc_id, "channel_ids": [1, 2], "text": "привет"}))

    assert len(harness["posted"]) == 1, (
        "второй канал ушёл тем же аккаунтом внутрь окна паузы — Telegram "
        "ответит новым FloodWait и продлит штраф")
    assert res["status"] == "requeue", res
    assert res["defer_s"] >= 3600, (
        "операция должна вернуться после паузы, а не раньше неё")
    assert flood_engine.seconds_until_ready(acc_id) > 0, (
        "штраф не дошёл до flood_engine — следующая операция возьмёт аккаунт "
        "как отдохнувший")


def test_short_pause_is_sat_out_and_the_run_continues(harness, clean_flood_state):
    """Короткая пауза: досиживаем остаток на месте и публикуем дальше."""
    acc_id = clean_flood_state[1]
    harness["replies"]["per_call"] = [{"error": "FLOOD_WAIT_60", "flood_wait": 60}]
    pool = _FakePool(channels=2)

    res = _run(op_worker._exec_bulk_post_chans(
        pool, None, 2, 777,
        {"acc_id": acc_id, "channel_ids": [1, 2], "text": "привет"}))

    assert res["status"] == "done", res
    assert len(harness["posted"]) == 2, "короткая пауза не повод бросать прогон"
    # record_flood добавляет +30с штрафа за флуд, поэтому ждать надо больше 60.
    assert max(harness["slept"]) >= 90, (
        f"остаток паузы не досижен, сны: {harness['slept']}")


def test_clean_run_waits_nothing_extra(harness, clean_flood_state):
    """Сторож самой проверки: без флуда гейт не должен ничего задерживать."""
    acc_id = clean_flood_state[2]
    pool = _FakePool(channels=2)

    res = _run(op_worker._exec_bulk_post_chans(
        pool, None, 3, 777,
        {"acc_id": acc_id, "channel_ids": [1, 2], "text": "привет"}))

    assert res["status"] == "done" and res["ok"] == 2, res
    assert len(harness["posted"]) == 2
    assert flood_engine.seconds_until_ready(acc_id) == 0.0
    assert all(s < 60 for s in harness["slept"]), (
        f"здоровый прогон не должен ничего пересиживать: {harness['slept']}")


# ── bulk_post_to_channel: много аккаунтов → один канал ───────────────────────

def _patch_accounts(monkeypatch, ids):
    async def _select(pool, owner_id, **kw):
        return [{"id": i, "session_str": f"s{i}", "first_name": f"acc{i}",
                 "phone": "+1"} for i in ids]
    monkeypatch.setattr(resource_selector, "select_all_active", _select)


def test_flood_on_one_account_is_recorded_for_the_whole_fleet(
        harness, clean_flood_state, monkeypatch):
    """FloodWait уходит в flood_engine, а не только в отчёт операции."""
    a, b = clean_flood_state[3], clean_flood_state[4]
    _patch_accounts(monkeypatch, [a, b])
    harness["replies"]["per_call"] = [{"error": "FLOOD_WAIT_300", "flood_wait": 300}]

    _run(op_worker._exec_bulk_post_to_channel(
        pool := _FakePool(), None, 4, 777,
        {"account_ids": [a, b], "channel_ref": "@ch", "text_to_post": "привет"}))

    assert flood_engine.seconds_until_ready(a) > 0, (
        "пауза осталась внутри прогона: подборщики флота и следующая операция "
        "считают аккаунт отдохнувшим")
    assert flood_engine.seconds_until_ready(b) == 0.0, "штраф не тому аккаунту"
    assert pool.done_items == 2, "прогресс должен считаться по всем аккаунтам"


def test_cooling_account_is_skipped_without_spending_a_request(
        harness, clean_flood_state, monkeypatch):
    """Остывающий аккаунт пропускаем, а не тратим на него запрос в окно паузы."""
    a, b = clean_flood_state[3], clean_flood_state[4]
    _patch_accounts(monkeypatch, [a, b])
    _run(flood_engine.record_flood(None, a, 300, "publish"))

    res = _run(op_worker._exec_bulk_post_to_channel(
        _FakePool(), None, 5, 777,
        {"account_ids": [a, b], "channel_ref": "@ch", "text_to_post": "привет"}))

    assert harness["posted"] == [f"s{b}"], (
        "запрос ушёл с аккаунта под паузой — Telegram вернёт FloodWait и "
        "продлит штраф")
    assert res["ok"] == 1, res


# ── mass_publish: выбор аккаунта под канал ───────────────────────────────────
#
# Функциональный прогон `mass_publish` требует поднять весь путь (гейт
# содержимого, редактор, медиа, пары канал↔аккаунт), поэтому связку проверяем
# по исходнику — как соседний tests/test_mass_publish_isolation.py. Границы
# берём по якорям кода, а не окном фиксированной длины: такое окно в этом
# репозитории уже однажды «теряло» проверяемый текст из-за добавленного
# комментария.

def _selection_block() -> str:
    src = inspect.getsource(op_worker._exec_mass_publish)
    start = src.index("candidate_accounts = [")
    end = src.index("acc = candidate_accounts[0]", start)
    return src[start:end]


def test_mass_publish_asks_the_pacing_engine_before_reusing_an_account():
    block = _selection_block()
    assert "_flood_cooldown_left" in block, (
        "канал нередко ведут несколько аккаунтов: остывающий после FloodWait "
        "брать нельзя, пост в окно паузы вернёт новый штраф")
    assert "_FLOOD_INLINE_MAX_S" in block and '"requeue"' in block, (
        "длинный остаток паузы нельзя пересиживать занятым слотом — операция "
        "должна уйти в очередь")


def test_the_selection_block_is_really_the_selection_block():
    """Сторож извлекателя: в блоке обязан быть заведомо существующий фильтр."""
    block = _selection_block()
    assert "isolated_accounts" in block, (
        "извлечён не тот фрагмент — проверка выше ничего не стережёт")
