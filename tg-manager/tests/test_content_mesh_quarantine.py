"""Content Mesh — регрессия (#7): не репостить через аккаунт в карантине.

_process_delivery выбирал аккаунт, отсеивая только banned/deactivated/
session_expired по acc_status, но НЕ проверял единый пульс здоровья
(is_account_quarantined: флуд/cooldown/недавнее ограничение) перед отправкой
через аккаунт → мог репостить контент флагнутым аккаунтом (эскалация). Теперь
при карантине доставка откладывается (не error — аккаунт восстановится).
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

import pytest

from services import content_mesh


class _QPool:
    """fetchrow отдаёт строку по имени таблицы в запросе; execute пишет лог."""

    def __init__(self, mesh, target, acc):
        self._rows = [
            ("content_meshes", mesh),
            ("mesh_targets", target),
            ("tg_accounts", acc),
        ]
        self.execs: list[str] = []

    async def fetchrow(self, q, *a):
        for name, row in self._rows:
            if name in q:
                return row
        return None

    async def execute(self, q, *a):
        self.execs.append(q)
        return "UPDATE 1"

    async def fetchval(self, q, *a):
        return None


@pytest.mark.asyncio
async def test_quarantined_account_defers_delivery():
    pool = _QPool(
        mesh={"id": 1, "enabled": True, "source_account_id": 7, "source_channel": "@s", "append_text": ""},
        target={"id": 2, "enabled": True, "target_channel": "@t"},
        acc={"id": 7, "session_str": "sess", "device_model": None},
    )
    item = {"id": 99, "mesh_id": 1, "target_id": 2, "source_msg_id": 5}

    with patch("services.infra_memory.is_account_quarantined", AsyncMock(return_value=True)):
        await content_mesh._process_delivery(pool, item)

    # доставка отложена, а не отправлена/запорота
    assert any("15 minutes" in q for q in pool.execs), "карантинный аккаунт не отложен"
    assert not any("status='sent'" in q for q in pool.execs)
    assert not any("status='error'" in q for q in pool.execs)


def test_gate_before_client_creation():
    """Гейт должен стоять ДО _make_client (иначе коннект флагнутым аккаунтом уже риск)."""
    src = inspect.getsource(content_mesh._process_delivery)
    gate = src.index("is_account_quarantined")
    make = src.index("_make_client")
    assert gate < make, "quarantine-гейт должен быть до создания клиента"
