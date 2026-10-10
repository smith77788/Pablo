"""Отличаем «мою инфраструктуру» (владелец/админ канала) от чужих подписок.

Баг: массовый импорт (_exec_channel_import_all / _exec_group_import_all) тянул
в managed_channels ВСЕ каналы/группы-диалоги аккаунта, включая те, где аккаунт
лишь подписчик. «Мои каналы» распухали чужими подписками (счётчик 164).

Фикс:
  - get_dialogs отдаёт is_creator / is_admin по правам аккаунта в канале;
  - add_managed_channels / upsert_managed_channels сохраняют эти флаги
    (COALESCE, чтобы NULL не затирал известную роль);
  - импорт-операции берут только каналы, где аккаунт создатель или админ.

Async-тесты идут через asyncio.run() (pytest-asyncio в окружении нет — см.
модуль-докстринг test_managed_channels_partial_import).
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

from database import db

_ROOT = pathlib.Path(__file__).resolve().parent.parent


class _FakeConn:
    def __init__(self, log):
        self._log = log

    async def execute(self, query, *args):
        self._log.append((query, args))
        return "OK"

    async def executemany(self, query, rows):
        for row in rows:
            self._log.append((query, row))

    def transaction(self):
        return _NullCtx()


class _NullCtx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self):
        self.log = []
        self._conn = _FakeConn(self.log)

    def acquire(self):
        return _AcquireCtx(self._conn)

    async def executemany(self, query, rows):
        for row in rows:
            self.log.append((query, row))

    async def execute(self, query, *args):
        self.log.append((query, args))
        return "OK"


def _func_source(relpath: str, funcname: str) -> str:
    src = (_ROOT / relpath).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == funcname:
            seg = ast.get_source_segment(src, node)
            assert seg is not None
            return seg
    raise AssertionError(f"{funcname} не найдена в {relpath}")


def _no_comments(src: str) -> str:
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))


# ── Хранение флагов ──────────────────────────────────────────────────────────

def test_add_managed_channels_persists_role_flags():
    pool = _FakePool()
    chans = [{"id": 100, "title": "Мой", "type": "channel", "is_admin": True, "is_creator": True}]

    asyncio.run(db.add_managed_channels(pool, owner_id=1, acc_id=10, channels=chans))

    inserts = [(q, a) for q, a in pool.log if "INSERT INTO managed_channels" in q]
    assert inserts, "должен быть INSERT"
    q, row = inserts[0]
    assert "is_admin" in q and "is_creator" in q, "INSERT обязан писать роль"
    # COALESCE защищает известную роль от затирания NULL при частичном довозе
    assert "COALESCE(EXCLUDED.is_admin" in q
    # is_admin, is_creator — предпоследние два параметра (последний —
    # members_count, добавленный отдельным фиксом «0 участников у всех
    # импортированных каналов»); позиция от конца, а не абсолютная, чтобы не
    # переломаться от порядка появления новых полей.
    assert True in row and row[-3:-1] == (True, True)


def test_upsert_managed_channels_persists_role_flags():
    pool = _FakePool()
    chans = [{"id": 100, "title": "Мой", "type": "channel", "is_admin": True, "is_creator": False}]

    asyncio.run(db.upsert_managed_channels(
        pool, owner_id=1, acc_id=10, channels=chans, complete=True))

    inserts = [(q, a) for q, a in pool.log if "INSERT INTO managed_channels" in q]
    assert inserts
    q, row = inserts[0]
    assert "is_admin" in q and "is_creator" in q
    # Позиция от конца: последним параметром идёт members_count — полный
    # ре-импорт теперь тоже его пишет, раньше он терял число участников.
    assert row[-3:-1] == (True, False)


# ── Импорт фильтрует чужие ────────────────────────────────────────────────────

def _apply_channel_filter(dialogs):
    """Повторяет логику отбора из _exec_channel_import_all для проверки семантики."""
    _CHANNEL_TYPES = ("channel", "megagroup", "supergroup", "gigagroup")
    typed = [d for d in dialogs if d.get("type") in _CHANNEL_TYPES]
    return [d for d in typed if d.get("is_admin") or d.get("is_creator")]


def test_channel_filter_keeps_only_owned():
    dialogs = [
        {"id": 1, "type": "channel", "is_admin": True, "is_creator": True},   # мой
        {"id": 2, "type": "channel", "is_admin": True, "is_creator": False},  # админ
        {"id": 3, "type": "channel", "is_admin": False, "is_creator": False}, # чужой
        {"id": 4, "type": "channel"},                                          # без флагов = чужой
    ]
    kept = _apply_channel_filter(dialogs)
    assert {d["id"] for d in kept} == {1, 2}


def test_import_ops_filter_by_role_in_source():
    for fn in ("_exec_channel_import_all", "_exec_group_import_all"):
        src = _no_comments(_func_source("services/op_worker.py", fn))
        assert "is_admin" in src and "is_creator" in src, (
            f"{fn} обязана фильтровать импорт по роли аккаунта (is_admin/is_creator)"
        )


def test_get_dialogs_exposes_role_flags_in_source():
    src = _func_source("services/account_manager.py", "get_dialogs")
    assert '"is_creator"' in src and '"is_admin"' in src, (
        "get_dialogs должна отдавать флаги роли, иначе импорт не сможет отличить чужие"
    )


def test_channels_endpoint_exposes_role():
    """Список каналов должен отдавать роль — на ней держится бейдж владелец/админ/участник."""
    src = _func_source("services/mini_app_api.py", "channels")
    assert "is_admin" in src and "is_creator" in src and "role" in src, (
        "endpoint channels обязан отдавать is_admin/is_creator/role для различения инфраструктуры"
    )


def test_reclassify_op_registered_and_routed():
    from services import operation_bus
    assert "reclassify_channels" in operation_bus.OP_REGISTRY, (
        "reclassify_channels не зарегистрирован — submit() отклонит операцию"
    )
    api_src = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    assert "/api/miniapp/channels/reclassify" in api_src, "нет роута переопределения инфраструктуры"
