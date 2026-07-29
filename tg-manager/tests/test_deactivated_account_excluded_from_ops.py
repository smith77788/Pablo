"""Отключённый (is_active=FALSE) аккаунт НЕ используется операциями.

Требование пользователя: пользователь должен реально «выйти из аккаунта»
(удалить/отключить), и такой аккаунт НЕ должен использоваться для дальнейших
операций, пока снова не будет авторизован/подключён.

Основной путь массовых операций уже фильтрует `is_active=TRUE`
(resource_selector), а прогрев проверяет здоровье перед действием. НО четыре
одиночных сервис-операции над аккаунтом подключали Telethon-сессию, выбирая
аккаунт ТОЛЬКО по id/owner_id/session_str — без `is_active`. Значит отключённый
аккаунт всё ещё мог быть использован. Гейтим это: их SELECT обязан содержать
`is_active=TRUE`, и при отсутствии активного аккаунта операция честно падает,
НЕ создавая клиент.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from services import op_worker

# op_type → (executor, params)
_SELF_OPS = {
    "leave_all_chats":       (op_worker._exec_leave_all_chats,      {"account_id": 7}),
    "read_all_dialogs":      (op_worker._exec_read_all_dialogs,     {"account_id": 7}),
    "delete_private_dialogs": (op_worker._exec_delete_private_dialogs, {"account_id": 7}),
    "delete_contacts":       (op_worker._exec_delete_contacts,      {"account_id": 7}),
}


class _CapturePool:
    """Отдаёт None на fetchrow (нет активного аккаунта), запоминая SQL."""

    def __init__(self):
        self.fetch_queries: list[str] = []

    async def fetchrow(self, q, *a):
        self.fetch_queries.append(q)
        return None

    async def execute(self, q, *a):
        return "OK"


def _run(coro):
    return asyncio.run(coro)


def test_self_ops_filter_is_active_in_account_fetch():
    """SELECT аккаунта для сессии обязан фильтровать is_active=TRUE."""
    for op_type, (fn, params) in _SELF_OPS.items():
        pool = _CapturePool()
        # _make_client не должен быть вызван — аккаунт не найден (отключён).
        with patch("services.account_manager._make_client") as mk:
            res = _run(fn(pool, None, 1, 99, params))
        acc_fetches = [q for q in pool.fetch_queries if "FROM tg_accounts" in q]
        assert acc_fetches, f"{op_type}: не было выборки аккаунта"
        for q in acc_fetches:
            # Проверяем ИМЕННО WHERE по tg_accounts, а не подзапрос по прокси
            # (там тоже есть up.is_active=TRUE — иначе тест был бы фиктивным).
            where = q[q.index("FROM tg_accounts"):].replace(" ", "").lower()
            assert "is_active=true" in where, \
                f"{op_type}: выборка аккаунта не фильтрует is_active — отключённый аккаунт может быть использован:\n{q}"
        # отключённый/удалённый аккаунт → честный отказ, без создания клиента
        assert res["status"] == "failed", f"{op_type}: должен отказать при неактивном аккаунте"
        mk.assert_not_called()


def test_self_ops_still_require_session_str():
    """Сохранён и фильтр session_str (аккаунт без сессии тоже не используется)."""
    for op_type, (fn, params) in _SELF_OPS.items():
        pool = _CapturePool()
        with patch("services.account_manager._make_client"):
            _run(fn(pool, None, 1, 99, params))
        acc_fetches = [q for q in pool.fetch_queries if "FROM tg_accounts" in q]
        for q in acc_fetches:
            assert "session_str IS NOT NULL" in q, f"{op_type}: потерян фильтр session_str"
