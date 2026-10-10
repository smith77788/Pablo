"""Быстрое удаление невоскрешаемых аккаунтов (бан/деактивация/отозванная сессия).

mini_app_api не импортируется в песочнице — проверяем на уровне исходников:
1. Бэкенд: фильтр `dead` = banned/deactivated/session_expired; stats-счётчик dead;
   масс-операция `delete` (жёсткое удаление, скоуп owner/admin).
2. Фронт: KPI «Мёртвые», кнопка «Удалить мёртвые», delete в runAccMass +
   confirm, purgeDeadAccounts(). Конфликт (cooldown) в dead НЕ входит.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_backend_dead_filter_and_delete_op():
    api = _read("services/mini_app_api.py")
    where = api[api.index("def _accounts_where"):]
    where = where[:where.index("return ")]
    assert 'flt == "dead"' in where
    assert "IN ('banned','deactivated','session_expired')" in where
    # масс-операция удаления
    assert 'if op == "delete":' in api
    assert "DELETE FROM tg_accounts WHERE id=ANY($1::bigint[])" in api


def test_backend_stats_has_dead():
    api = _read("services/mini_app_api.py")
    assert "IN ('banned','deactivated','session_expired')) AS dead" in api


def test_frontend_has_dead_kpi_and_purge():
    ui = _read("mini_app/index.html")
    assert "filterAcc('dead'" in ui
    assert 'id="kpi-dead"' in ui
    assert "function purgeDeadAccounts" in ui
    assert 'id="purgeDeadBtn"' in ui
    # delete-путь в масс-действиях + подтверждение необратимости
    assert "runAccMass('delete')" in ui
    assert "op==='delete'" in ui


def test_conflict_cooldown_not_in_dead():
    """Конфликтные (AUTH_KEY_DUPLICATED → cooldown) НЕ попадают в 'dead' —
    их лечит перезалив, а не удаление."""
    api = _read("services/mini_app_api.py")
    # изолируем ИМЕННО SQL-условие фильтра dead (строку clauses.append),
    # комментарии не считаем.
    after = api[api.index('flt == "dead"'):]
    sql_line = next(l for l in after.splitlines()
                    if "clauses.append(" in l and "acc_status" in l)
    assert "'banned','deactivated','session_expired'" in sql_line
    assert "cooldown" not in sql_line and "spamblock" not in sql_line
