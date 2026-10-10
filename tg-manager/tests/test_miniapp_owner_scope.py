"""Регресс owner-scope (мультитенантная изоляция) в mini_app_api.

Аудит IDOR по owned-таблицам (2026-07-12): чтения/мутации по core-таблицам и
bot-owned таблицам (funnels/broadcasts/auto_replies/experiments — без своей
колонки владельца, изоляция транзитивно через managed_bots.added_by) в целом
дисциплинированы. Найдена и закрыта одна дыра: привязка bot_id к presence_pack
не проверяла владельца бота (можно было залинковать чужой bot_id в свой пак).

Тест фиксирует: (1) фикс presence_pack (fetch username для линковки скоупится
added_by); (2) идиома изоляции bot-owned мутаций не регрессирует
(`bot_id IN (SELECT bot_id FROM managed_bots WHERE added_by=...)` либо JOIN на
managed_bots ... added_by в ТОМ ЖЕ операторе).
"""
from __future__ import annotations

import inspect
import re

from services import mini_app_api

_SRC = inspect.getsource(mini_app_api)


def test_presence_pack_bot_link_is_owner_scoped():
    # Fetch имени бота для привязки к паку обязан скоупиться added_by (иначе можно
    # залинковать чужой bot_id в свой пак).
    assert re.search(
        r"SELECT\s+username\s+FROM\s+managed_bots\s+WHERE\s+bot_id=\$1\s+AND\s+added_by=\$2",
        _SRC,
    ), "линковка bot_id к presence_pack должна проверять added_by=uid"


def test_bot_owned_mutations_keep_ownership_join():
    """Мутации по таблицам без своей owner-колонки (изоляция транзитивно через
    managed_bots.added_by) не должны терять фильтр владельца в операторе."""
    tables = ("funnels", "auto_replies", "experiments")
    for m in re.finditer(r"\b(UPDATE|DELETE FROM)\s+(" + "|".join(tables) + r")\b", _SRC):
        stmt = _SRC[m.start(): m.start() + 400]
        assert "managed_bots" in stmt and "added_by" in stmt, (
            f"{m.group(1)} {m.group(2)} должен скоупиться через managed_bots.added_by: "
            f"{re.sub(chr(10), ' ', stmt[:120])}"
        )


def test_admin_only_endpoints_gate_on_is_admin():
    """Межтенантные (админские) выборки без owner-скоупа должны стоять за
    server-derived _is_admin, а не за параметром запроса."""
    for handler in ("admin_stats", "admin_ops_stats", "admin_users"):
        m = re.search(rf"async def {handler}\(.*?\n(.*?)\n    async def ", _SRC, re.DOTALL)
        assert m, f"handler {handler} not found"
        assert "_is_admin(uid)" in m.group(1), (
            f"{handler} должен гейтиться _is_admin(uid) (не параметром запроса)"
        )
