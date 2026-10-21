"""«Мёртвый аккаунт» определён в одном месте — копии не должны разъехаться.

fleet_maintenance.sweep_fleet удаляет аккаунты по DEAD_ACCOUNT_STATUSES, а
экранный фильтр `dead` (mini_app_api._accounts_where, им же пользуется кнопка
«Удалить мёртвые аккаунты») — по своему списку статусов в SQL. Это два места с
одним смыслом. Если они разойдутся, «сделать всё разом» начнёт удалять не то,
что показывает экран. Тест держит их равными: разошлись — чинить определение,
а не тест.
"""
from __future__ import annotations

import re

from services import fleet_maintenance
from tests.miniapp_source import api_source


def test_dead_account_statuses_match_the_dead_filter():
    src = api_source()
    # Ветка фильтра `dead` в _accounts_where: ... flt == "dead": ...
    #   COALESCE(acc_status,'ok') IN ('banned','deactivated','session_expired')
    m = re.search(r'flt\s*==\s*"dead".*?\bIN\s*\(([^)]*)\)', src, re.S)
    assert m, "не найден SQL фильтра `dead` в _accounts_where — проверка смотрит не туда"
    filter_statuses = set(re.findall(r"'([a-z_]+)'", m.group(1)))
    assert filter_statuses, "из фильтра `dead` не извлёкся ни один статус"
    assert set(fleet_maintenance.DEAD_ACCOUNT_STATUSES) == filter_statuses, (
        "определения «мёртвого аккаунта» разошлись: "
        f"fleet_maintenance={sorted(fleet_maintenance.DEAD_ACCOUNT_STATUSES)} "
        f"vs фильтр dead={sorted(filter_statuses)}"
    )
