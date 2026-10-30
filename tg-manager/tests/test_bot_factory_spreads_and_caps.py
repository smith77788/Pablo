"""Фабрика ботов: потолок «ботов на аккаунт», равномерность и НЕ зависать на одном.

Жалоба владельца (Операция #745, 100 ботов на 50 аккаунтов, «максимум 2 на
аккаунт»): операция зависла на одном аккаунте, аккаунты не работали независимо,
а с одного аккаунта создалось сразу 5 ботов вместо 2.

Три причины, и все три здесь под регрессом:

1. ПОТОЛОК не применялся. `bot_count` шёл только в `total` как множитель, а при
   создании исполнитель лил ботов на первый живой аккаунт, пока тот не выбывал.
   Теперь на шаг берётся аккаунт с наименьшим числом уже созданных и не выше
   потолка `bot_count` → каждый аккаунт получает ровно свою квоту, равномерно.
2. ВЫБЫТИЕ флуд-аккаунта. Флудящий у BotFather аккаунт раньше оставался в
   ротации первым, и каждый следующий бот снова бил по нему. Теперь он выбывает
   из ротации операции (суточный лимит BotFather за её время не снимется).
3. ЗАВИСАНИЕ. `create_bot_via_botfather` пережидал лимит «try again in N seconds»
   ВНУТРИ вызова — при суточном лимите (85623с ≈ 24ч) это вешало всю операцию.
   Теперь длинный лимит возвращается вызывающему как flood_wait без ожидания.

op_worker в тестовой среде поднимается (заглушка telethon из conftest), поэтому
проверяем ПОВЕДЕНИЕМ: гоняем исполнителя на фейковом пуле с мок-BotFather и
смотрим, кто сколько ботов создал.
"""
from __future__ import annotations

import asyncio

import pytest

from services import account_manager


# ── фейковая инфраструктура ───────────────────────────────────────────────────

class _FakePool:
    async def execute(self, *a, **k):
        return None

    async def fetch(self, *a, **k):
        return []

    async def fetchrow(self, *a, **k):
        return None

    async def fetchval(self, *a, **k):
        return None


class _FakeGet:
    def __init__(self, n):
        self._n = n

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return {"ok": True, "result": {"id": 900000 + self._n, "username": f"made{self._n}bot"}}


class _FakeSession:
    _n = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, timeout=None):
        _FakeSession._n += 1
        return _FakeGet(_FakeSession._n)


def _accounts(ids):
    return [{"id": i, "session_str": f"sess-{i}", "username": f"acc{i}"} for i in ids]


def _patch_common(monkeypatch, create_fn, account_ids):
    import aiohttp
    from services import op_worker, resource_selector

    async def _sel(pool, owner_id, include_ids=None, min_trust_score=0.0, **k):
        return _accounts(include_ids or account_ids)

    monkeypatch.setattr(resource_selector, "select_all_active", _sel)
    monkeypatch.setattr(account_manager, "create_bot_via_botfather", create_fn)

    async def _claim(ids):
        return list(ids)

    async def _noop(*a, **k):
        return None

    async def _false(*a, **k):
        return False

    async def _empty(*a, **k):
        return set()

    monkeypatch.setattr(op_worker, "try_claim_accounts", _claim)
    monkeypatch.setattr(op_worker, "release_accounts", _noop)
    monkeypatch.setattr(op_worker, "completed_steps", _empty)
    monkeypatch.setattr(op_worker, "_is_cancelled", _false)
    monkeypatch.setattr(op_worker, "_safe_execute", _noop)
    monkeypatch.setattr(op_worker, "cancellable_pause", _noop)
    monkeypatch.setattr(op_worker, "_note_flood_penalty", _noop)
    monkeypatch.setattr(op_worker, "_journal_done", _noop)
    monkeypatch.setattr(op_worker, "_journal_write", _noop)
    monkeypatch.setattr(op_worker.session_simulator, "typing_delay", _noop)
    monkeypatch.setattr(op_worker.session_simulator, "chaos_factor", lambda: 1.0)
    monkeypatch.setattr(op_worker.session_simulator, "time_of_day_factor", lambda: 1.0)
    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    return op_worker


def _run_multi(op_worker, account_ids, bot_count):
    params = {"bot_count": bot_count, "bot_name": "Доставка", "base_username": "dostavka"}
    return asyncio.run(
        op_worker._exec_bot_factory_multi(
            _FakePool(), object(), 745, 111, params, account_ids))


# ── потолок и равномерность ───────────────────────────────────────────────────

def test_each_account_gets_exactly_its_cap_and_no_more(monkeypatch):
    calls: list[int] = []

    async def _create(session_str, bot_display_name, bot_username, _acc=None):
        calls.append(int(_acc["id"]))
        return {"token": f"123456789:ABC{_acc['id']}", "username": bot_username}

    op_worker = _patch_common(monkeypatch, _create, [1, 2, 3])
    res = _run_multi(op_worker, [1, 2, 3], bot_count=2)

    # 3 аккаунта × потолок 2 = 6 ботов, и НИ ОДИН аккаунт не превысил 2.
    assert res["ok"] == 6
    assert res["status"] == "done"
    for acc in (1, 2, 3):
        assert calls.count(acc) == 2, f"аккаунт {acc} создал {calls.count(acc)} ботов вместо 2"


def test_load_is_spread_not_piled_on_first_account(monkeypatch):
    # На момент 3-го бота у каждого аккаунта должно быть не больше одного:
    # равномерность, а не «5 на первый, 0 на остальные».
    calls: list[int] = []

    async def _create(session_str, bot_display_name, bot_username, _acc=None):
        calls.append(int(_acc["id"]))
        return {"token": f"123456789:ABC{_acc['id']}", "username": bot_username}

    op_worker = _patch_common(monkeypatch, _create, [1, 2, 3])
    _run_multi(op_worker, [1, 2, 3], bot_count=2)

    first_three = calls[:3]
    assert sorted(first_three) == [1, 2, 3], (
        f"первые три бота ушли не на три разных аккаунта: {first_three}")


# ── выбытие флуд-аккаунта: не зависаем на нём ─────────────────────────────────

def test_flooded_account_drops_out_and_others_continue(monkeypatch):
    calls: list[int] = []
    FLOOD = 1

    async def _create(session_str, bot_display_name, bot_username, _acc=None):
        aid = int(_acc["id"])
        calls.append(aid)
        if aid == FLOOD:
            # суточный лимит BotFather
            return {"error": "BotFather: слишком много попыток, подождите 85623с",
                    "flood_wait": 85623}
        return {"token": f"123456789:ABC{aid}", "username": bot_username}

    op_worker = _patch_common(monkeypatch, _create, [1, 2, 3])
    res = _run_multi(op_worker, [1, 2, 3], bot_count=2)

    # Флудящий аккаунт попробовали ОДИН раз и выкинули — не долбим его дальше.
    assert calls.count(FLOOD) == 1, (
        f"по флудящему аккаунту ударили {calls.count(FLOOD)} раз — он должен выбыть сразу")
    # Остальные два довели свою квоту (2+2), операция не зависла на первом.
    assert calls.count(2) == 2 and calls.count(3) == 2
    assert res["ok"] == 4
    assert res["status"] == "partial"      # 4 из 6 — честный частичный исход
    assert "выбыло" in res["summary"]


# ── зависание: длинный лимит BotFather НЕ пережидается внутри вызова ───────────

def test_create_bot_does_not_sleep_out_a_daily_limit(monkeypatch):
    """Контроль самого зависания: на «try again in 85623 seconds» возврат мгновенный.

    Старый код делал asyncio.sleep(85623) ВНУТРИ вызова — тест повис бы на сутки,
    поэтому оборачиваем в wait_for(30с): неверный код падает по таймауту, а не
    висит. Верный — возвращает flood_wait за пару секунд (мелкие человекоподобные
    паузы диалога).
    """
    class _Msg:
        def __init__(self, mid, sender, text):
            self.id, self.sender_id, self.text = mid, sender, text

    class _FakeClient:
        def __init__(self):
            self._replied = False

        async def connect(self):
            return None

        async def get_entity(self, _u):
            class _E:
                id = 777
            return _E()

        async def get_messages(self, _entity, limit=1):
            if self._replied:
                return [_Msg(200, 777,
                             "Sorry, too many attempts. Please try again in 85623 seconds.")]
            return [_Msg(100, 777, "previous")]

        async def send_message(self, _entity, _text):
            self._replied = True

    monkeypatch.setattr(account_manager, "_make_client", lambda *a, **k: _FakeClient())

    async def _noop_disc(*a, **k):
        return None

    monkeypatch.setattr(account_manager, "safe_disconnect", _noop_disc)

    async def _go():
        return await asyncio.wait_for(
            account_manager.create_bot_via_botfather("sess", "Имя", "dostavkabot"),
            timeout=30,
        )

    res = asyncio.run(_go())
    assert res.get("flood_wait") == 85623, res
    assert "token" not in res
