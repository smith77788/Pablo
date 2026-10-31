"""Фабрика ботов уважает пределы BotFather и не теряет потолок на повторе.

Продолжение `test_bot_factory_spreads_and_caps.py`. Проверка всего пути
нашла ещё пять мест, где фабрика либо била по BotFather зря, либо нарушала
потолок «ботов на аккаунт» владельца:

1. ОДИНОЧНЫЙ путь на суточном лимите засчитывал шаг ошибкой и шёл дальше —
   слал /newbot тому же аккаунту на КАЖДОМ оставшемся шаге, продлевая штраф.
   Теперь операция откладывается до конца паузы, а если ждать уже некуда —
   остаток закрывается сразу.
2. Предел Telegram «20 ботов на аккаунт» не распознавался: ответ BotFather
   уходил в безликую ошибку, и мульти-фабрика ставила шаги на такой аккаунт
   снова и снова (у него меньше всех созданных — он «наименее загружен»).
3. По той же причине аккаунт с ПОСТОЯННОЙ ошибкой диалога собирал на себя все
   шаги. Теперь после двух ошибок подряд он выходит из ротации.
4. Потолок не переживал повтор: счётчик «сколько создал каждый аккаунт» жил в
   памяти прогона. Теперь он восстанавливается из журнала операции.
5. Выбывшие по длинному лимиту аккаунты не добирали квоту: операция
   закрывалась недоделанной. Теперь она откладывается до конца самой
   короткой паузы.
"""
from __future__ import annotations

import asyncio

from services import account_manager
from tests.test_bot_factory_spreads_and_caps import _FakePool, _patch_common, _run_multi


def _tok(aid):
    return f"123456789:AAH{aid:03d}" + "x" * 32


# ── одиночный путь ────────────────────────────────────────────────────────────

def _patch_single(monkeypatch, create_fn, may_defer: bool):
    from services import op_worker, resource_selector
    _patch_common(monkeypatch, create_fn, [1])

    async def _sel_all(pool, owner_id, include_ids=None, **k):
        return [{"id": 1, "session_str": "s" * 40}]

    async def _claim_one(_id):
        return True

    async def _may(pool, op_id):
        return may_defer

    monkeypatch.setattr(resource_selector, "select_all_active", _sel_all)
    monkeypatch.setattr(op_worker, "try_claim_account", _claim_one)
    monkeypatch.setattr(op_worker, "_bot_factory_may_defer", _may)
    return op_worker


def _run_single(op_worker, count):
    params = {"acc_id": 1, "count": count, "name_template": "Доставка",
              "uname_template": "dostavka"}
    return asyncio.run(op_worker._exec_bot_factory(_FakePool(), object(), 745, 111, params))


def _daily_limit_create(calls):
    async def _create(session_str, bot_display_name, bot_username, _acc=None):
        calls.append(1)
        if len(calls) <= 2:
            return {"token": _tok(len(calls)), "username": bot_username}
        return {"error": "BotFather: слишком много попыток, подождите 85623с",
                "flood_wait": 85623}
    return _create


def test_single_factory_defers_on_a_daily_limit(monkeypatch):
    calls: list[int] = []
    op_worker = _patch_single(monkeypatch, _daily_limit_create(calls), may_defer=True)
    res = _run_single(op_worker, count=10)
    assert res["status"] == "requeue" and res["defer_s"] >= 85623, res
    assert len(calls) == 3, f"после суточного лимита /newbot ушёл ещё {len(calls) - 3} раз"


def test_single_factory_stops_when_it_may_not_wait(monkeypatch):
    calls: list[int] = []
    op_worker = _patch_single(monkeypatch, _daily_limit_create(calls), may_defer=False)
    res = _run_single(op_worker, count=10)
    assert len(calls) == 3, f"после суточного лимита /newbot ушёл ещё {len(calls) - 3} раз"
    assert res["ok"] == 2 and res["failed"] == 8
    assert "BotFather" in res["summary"]


def test_single_factory_stops_at_the_twenty_bot_cap(monkeypatch):
    calls: list[int] = []

    async def _create(*a, **k):
        calls.append(1)
        return {"error": "На аккаунте уже 20 ботов", "bot_limit": True}

    op_worker = _patch_single(monkeypatch, _create, may_defer=True)
    res = _run_single(op_worker, count=5)
    assert len(calls) == 1 and res["ok"] == 0 and res["failed"] == 5


# ── мульти-путь ───────────────────────────────────────────────────────────────

def test_multi_drops_an_account_at_the_twenty_bot_cap(monkeypatch):
    calls: list[int] = []

    async def _create(session_str, bot_display_name, bot_username, _acc=None):
        aid = int(_acc["id"])
        calls.append(aid)
        if aid == 1:
            return {"error": "На аккаунте уже 20 ботов", "bot_limit": True}
        return {"token": _tok(aid), "username": bot_username}

    op_worker = _patch_common(monkeypatch, _create, [1, 2, 3])
    res = _run_multi(op_worker, [1, 2, 3], bot_count=2)
    assert calls.count(1) == 1, "аккаунт на пределе 20 ботов выбирали снова"
    assert calls.count(2) == 2 and calls.count(3) == 2
    assert "предел 20 ботов" in res["summary"]


def test_multi_drops_an_account_that_keeps_failing(monkeypatch):
    calls: list[int] = []

    async def _create(session_str, bot_display_name, bot_username, _acc=None):
        aid = int(_acc["id"])
        calls.append(aid)
        if aid == 1:
            return {"error": "Неожиданный ответ BotFather на /newbot: ..."}
        return {"token": _tok(aid), "username": bot_username}

    op_worker = _patch_common(monkeypatch, _create, [1, 2, 3])
    res = _run_multi(op_worker, [1, 2, 3], bot_count=2)
    assert calls.count(1) == 2, f"сбойный аккаунт выбран {calls.count(1)} раз"
    assert res["ok"] == 4


def test_multi_cap_survives_a_retry(monkeypatch):
    # Прошлый прогон создал аккаунтом 1 обоих его ботов (шаги 1 и 2).
    calls: list[int] = []

    async def _create(session_str, bot_display_name, bot_username, _acc=None):
        calls.append(int(_acc["id"]))
        return {"token": _tok(int(_acc["id"])), "username": bot_username}

    op_worker = _patch_common(monkeypatch, _create, [1, 2, 3])

    async def _done(pool, op_id):
        return {1, 2}

    async def _made(pool, op_id):
        return {1: 2}

    monkeypatch.setattr(op_worker, "completed_steps", _done)
    monkeypatch.setattr(op_worker, "_bot_factory_made_by_acc", _made)
    res = _run_multi(op_worker, [1, 2, 3], bot_count=2)
    assert 1 not in calls, "аккаунт, уже сделавший квоту, получил её второй раз"
    assert calls.count(2) == 2 and calls.count(3) == 2
    assert res["ok"] == 6


def test_multi_defers_to_finish_the_quota_after_the_limit(monkeypatch):
    async def _create(session_str, bot_display_name, bot_username, _acc=None):
        aid = int(_acc["id"])
        if aid == 1:
            return {"error": "лимит", "flood_wait": 7200}
        return {"token": _tok(aid), "username": bot_username}

    op_worker = _patch_common(monkeypatch, _create, [1, 2])

    async def _may(pool, op_id):
        return True

    monkeypatch.setattr(op_worker, "_bot_factory_may_defer", _may)
    res = _run_multi(op_worker, [1, 2], bot_count=2)
    assert res["status"] == "requeue" and res["defer_s"] >= 7200, res


def test_made_by_acc_is_read_from_the_journal(monkeypatch):
    from services import op_worker

    async def _ids(pool, op_id):
        return [op_id]

    async def _fetch(pool, sql, *args, **k):
        assert "status='ok'" in sql
        return [
            {"step_num": 1, "message": "@a_bot · аккаунт #7"},
            {"step_num": 2, "message": "@b_bot · аккаунт #7"},
            {"step_num": 3, "message": "@c_bot · аккаунт #9"},
            {"step_num": 4, "message": "@old_bot"},   # журнал до правки — без метки
        ]

    monkeypatch.setattr(op_worker, "journal_op_ids", _ids)
    monkeypatch.setattr(op_worker, "_journal_fetch", _fetch)
    got = asyncio.run(op_worker._bot_factory_made_by_acc(_FakePool(), 1))
    assert got == {7: 2, 9: 1}


def test_success_journal_line_names_the_account():
    # Без метки повтор не восстановит потолок — это и проверяет тест выше.
    import inspect
    from services import op_worker
    src = inspect.getsource(op_worker._exec_bot_factory_multi)
    assert src.count("{_BF_ACC_MARK}{int(candidate['id'])}") >= 2


# ── распознавание предела 20 ботов в диалоге ─────────────────────────────────

def test_dialog_recognises_the_twenty_bot_cap(monkeypatch):
    class _Msg:
        def __init__(self, mid, text):
            self.id, self.sender_id, self.text = mid, 777, text

    class _Client:
        def __init__(self):
            self.sent: list[str] = []

        async def connect(self):
            return None

        async def get_entity(self, _u):
            class _E:
                id = 777
            return _E()

        async def get_messages(self, _e, limit=1):
            if self.sent:
                return [_Msg(100 + len(self.sent),
                             "That I cannot do.\n\nYou come to me asking for more than "
                             "20 bots. But you don't even have the decency to delete "
                             "some of your old bots.")]
            return [_Msg(100, "previous")]

        async def send_message(self, _e, text):
            self.sent.append(text)

    client = _Client()
    monkeypatch.setattr(account_manager, "_make_client", lambda *a, **k: client)

    async def _nodisc(*a, **k):
        return None

    monkeypatch.setattr(account_manager, "safe_disconnect", _nodisc)
    res = asyncio.run(asyncio.wait_for(
        account_manager.create_bot_via_botfather("s" * 40, "Имя", "dostavkabot"), timeout=60))
    assert res.get("bot_limit") is True, res
    assert client.sent[0] == "/newbot" and "dostavkabot" not in client.sent
