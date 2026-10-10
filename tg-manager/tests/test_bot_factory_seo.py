"""Фабрика ботов: SEO-имена и @username перестановкой ключей (не «Бот 1, Бот 2»).

Проверяем на заглушках, что в @BotFather уходят РАЗНЫЕ имена-перестановки ключей
и валидные @username с суффиксом bot. Живой BotFather-диалог не трогаем: заглушка
create_bot_via_botfather лишь фиксирует, ЧТО ему передали (и не возвращает токен,
чтобы не уходить в getMe/БД).
"""
from __future__ import annotations

import asyncio

from services import op_worker


class _Pool:
    async def execute(self, q, *a): return "OK"
    async def fetch(self, q, *a): return []
    async def fetchval(self, q, *a): return 0
    async def fetchrow(self, q, *a): return None


def test_bot_factory_keywords_names_and_usernames(monkeypatch):
    calls = []

    async def _select_all_active(pool, owner_id, **kw):
        return [{"id": 5, "session_str": "s5", "first_name": "acc"}]
    async def _claim(_id): return True
    async def _release(_ids): return None
    async def _cancelled(pool, op_id): return False

    monkeypatch.setattr(op_worker.resource_selector, "select_all_active", _select_all_active)
    monkeypatch.setattr(op_worker, "try_claim_account", _claim)
    monkeypatch.setattr(op_worker, "release_accounts", _release)
    monkeypatch.setattr(op_worker, "_is_cancelled", _cancelled)

    from services import account_manager, session_simulator
    async def _typing(*a, **k): return None
    monkeypatch.setattr(session_simulator, "typing_delay", _typing)
    # Обнуляем анти-флуд паузы между ботами (иначе тест спит десятки секунд).
    monkeypatch.setattr(session_simulator, "chaos_factor", lambda: 0.0)
    monkeypatch.setattr(session_simulator, "time_of_day_factor", lambda: 0.0)

    async def _create_bot(session, bot_display_name=None, bot_username=None, _acc=None):
        calls.append({"name": bot_display_name, "username": bot_username})
        return {"error": "stub — токен не выдаём, чтобы не звать getMe"}
    monkeypatch.setattr(account_manager, "create_bot_via_botfather", _create_bot)
    monkeypatch.setattr(account_manager, "is_dead_session_error", lambda *_a, **_k: False)

    params = {
        "acc_id": 5, "count": 3, "name_mode": "keywords",
        "name_template": "Dostavka Moskva", "uname_template": "Dostavka_Moskva",
    }
    asyncio.run(op_worker._exec_bot_factory(_Pool(), None, 1, 42, params))

    assert len(calls) == 3
    names = [c["name"] for c in calls]
    unames = [c["username"] for c in calls]
    # имена — перестановки ключей, не «... 1/2/3»
    assert "Dostavka Moskva" in names and "Moskva Dostavka" in names
    assert not any(n.rstrip().endswith(("1", "2", "3")) for n in names)
    # username — валидные, различны, с суффиксом bot
    assert len(set(unames)) == 3
    from services import name_variator as V
    assert all(u.endswith("bot") for u in unames)
    assert all(V.valid_username(u, require_suffix="bot") for u in unames)
