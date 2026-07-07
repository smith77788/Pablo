"""Регрессия: серверная пагинация/фильтрация списка аккаунтов (снят 100-лимит).

mini_app_api не импортируется в песочнице (нет asyncpg), поэтому проверяем на
уровне исходников ключевые инварианты: WHERE всегда скоупнут owner_id; фильтр
здоровья/статуса/поиск встроены серверно; ответ несёт page.has_more/filtered_total;
UI ушёл на серверный путь (reload/loadMore/search), а не дофильтрует клиентом.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_where_builder_scoped_by_owner():
    src = _read("services/mini_app_api.py")
    m = re.search(r"def _accounts_where\(.*?\n(.*?)\n    async def accounts", src, re.DOTALL)
    assert m, "_accounts_where не найден"
    body = m.group(1)
    # owner_id=$1 — первый и обязательный клауз
    assert 'clauses = ["owner_id=$1"]' in body, "WHERE не начинается со скоупа owner_id"
    # фильтры здоровья
    for token in ("active", "cooldown", "banned"):
        assert f'flt == "{token}"' in body, f"нет ветки фильтра {token}"
    # статус — только из whitelist
    assert "stage in ACCOUNT_STAGES" in body
    # поиск по телефону/имени/username через ILIKE
    assert "ILIKE" in body and "phone" in body and "username" in body


def test_pagination_params_and_response():
    src = _read("services/mini_app_api.py")
    acc = src[src.index("async def accounts(request"):]
    acc = acc[:acc.index("async def account_detail")]
    # лимит клампится (не даём выкачать всё одним запросом), offset неотрицателен
    assert "min(200, max(1, int(qs.get(\"limit\", 100)))" in acc.replace("'", '"')
    assert "max(0, int(qs.get(\"offset\", 0))" in acc.replace("'", '"')
    # ответ несёт блок пагинации
    assert '"has_more"' in acc and '"filtered_total"' in acc
    # filtered_total считается по тому же WHERE (срез по всей таблице)
    assert "SELECT COUNT(*) FROM tg_accounts WHERE {where}" in acc


def test_ui_switched_to_server_side():
    ui = _read("mini_app/index.html")
    for fn in ("reloadAccounts", "loadMoreAccounts", "onAccSearch", "_accQuery", "renderLoadMore"):
        assert fn in ui, f"нет функции {fn} в UI"
    # клиентский дофильтр убран: _accFiltered отдаёт CUR_ACCS как есть
    m = re.search(r"function _accFiltered\(\)\s*\{(.*?)\}", ui, re.DOTALL)
    assert m and "return CUR_ACCS" in m.group(1), "_accFiltered всё ещё фильтрует клиентом"
    # фильтр/статус дергают серверную перезагрузку, а не renderAccounts(_accFiltered())
    assert re.search(r"function filterAcc\(mode, btn\)\s*\{[^}]*reloadAccounts\(\)", ui, re.DOTALL)
    assert re.search(r"function filterStage\(stage\)\s*\{.*?reloadAccounts\(\)", ui, re.DOTALL)
