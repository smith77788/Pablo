"""Регрессия: «аккаунты вступили, но не получили админку и не инвайтят».

Инвайт в чат/канал требует права `invite_users`. Движок ищет промоутера
(создатель/админ чата с add_admins) и через него выдаёт право остальным
инвайтерам (op_worker._exec_mass_invite). Две тихие поломки этого шага:

  1. промоут идёт по `tg_accounts.tg_user_id`; у аккаунтов без него (частый
     случай при импорте сессий) аккаунт МОЛЧА пропускался → инвайтер без прав →
     «вступил, но не инвайтит». Фикс: если id нет — резолвим вживую
     (account_manager.resolve_self_user_id), сохраняем и промоутим;
  2. когда промоутера нет вовсе, причина пряталась в лог шага promote. Фикс:
     выносим её в ИТОГ операции (пользователю видно, что делать).
"""
from __future__ import annotations

import asyncio
import inspect
import re

from services import op_worker, account_manager


def _exec_src() -> str:
    return inspect.getsource(op_worker._exec_mass_invite)


def test_resolve_self_user_id_exists_and_async():
    fn = getattr(account_manager, "resolve_self_user_id", None)
    assert fn is not None, "нужен хелпер resolve_self_user_id для backfill tg_user_id"
    assert asyncio.iscoroutinefunction(fn), "resolve_self_user_id должен быть async"


def test_promote_loop_backfills_missing_user_id():
    body = _exec_src()
    assert "resolve_self_user_id(" in body, (
        "промоут-шаг должен подтягивать user_id вживую, а не молча пропускать "
        "аккаунты без tg_user_id"
    )
    assert re.search(r"UPDATE tg_accounts SET tg_user_id", body), (
        "полученный user_id должен сохраняться в БД (backfill для будущих запусков)"
    )


def test_no_promoter_reason_surfaced_in_summary():
    body = _exec_src()
    assert "_promote_skipped_no_admin" in body, "нет флага отсутствия промоутера"
    # флаг должен участвовать в построении summary (а не только в логе шага)
    m = re.search(r"summary\s*=\s*\((.*?)\n    return \{", body, re.DOTALL)
    assert m, "summary block not found"
    assert "_promote_skipped_no_admin" in m.group(1), (
        "причина «ни один аккаунт не админ чата» должна попадать в ИТОГ операции, "
        "а не только в лог шага promote"
    )


def test_missing_uid_skip_is_counted_not_silent():
    body = _exec_src()
    assert "_promote_no_uid" in body, (
        "пропуски по отсутствующему user_id должны считаться и объясняться, "
        "а не теряться молча"
    )
