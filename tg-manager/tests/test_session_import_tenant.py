"""Session Import (2D) — регрессия: дедуп не должен раскрывать чужой аккаунт.

import_sessions дедуплицирует сессии глобально (одна Telegram-сессия = один
аккаунт на платформе), но раньше сообщение об ошибке возвращало внутренний id
существующего аккаунта БЕЗ проверки владельца — импортируя сессию, пользователь
узнавал id аккаунта ДРУГОГО владельца (cross-tenant disclosure). Теперь id
раскрывается только своему владельцу.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.test_executors import FakePool
from services import session_importer

# Валидная по формату string_session строка (>=100 base64-символов).
_LINE = "1" + "A" * 130

_VALID = {"valid": True, "phone": "+100", "user_id": 1, "first_name": "T", "username": "t"}


@pytest.mark.asyncio
async def test_foreign_owner_account_id_not_disclosed():
    pool = FakePool(fetchrow={"id": 999, "owner_id": 777})  # чужой владелец
    with patch.object(session_importer, "validate_session", return_value=_VALID):
        res = await session_importer.import_sessions(pool, owner_id=5, raw_data=_LINE)
    assert res["imported"] == 0 and res["failed"] == 1
    joined = " ".join(res["errors"])
    assert "999" not in joined, "утёк id чужого аккаунта"
    assert "используется на платформе" in joined


@pytest.mark.asyncio
async def test_own_account_id_is_shown():
    pool = FakePool(fetchrow={"id": 999, "owner_id": 5})  # свой владелец
    with patch.object(session_importer, "validate_session", return_value=_VALID):
        res = await session_importer.import_sessions(pool, owner_id=5, raw_data=_LINE)
    assert res["failed"] == 1
    assert "id=999" in " ".join(res["errors"])  # свой id показываем
