"""Регресс: панель аккаунтов честно разделяет спамблок (флот-здоровье).

mini_app_api не импортируется в песочнице — проверяем на уровне исходников:
1. Есть фильтр списка `spamblock` (раньше можно было только all/active/cooldown/banned).
2. Фильтр `active` и stats-счётчик `active` исключают спамблок — спамблокнутый
   аккаунт больше не протекает в «Активные».
3. stats отдаёт отдельный счётчик `spamblock`.
4. Фронт: KPI-чип «Спамблок» + updateAccKpi считает его отдельно и убирает из active.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_backend_has_spamblock_filter_and_active_excludes_it():
    api = _read("services/mini_app_api.py")
    where = api[api.index("def _accounts_where"):]
    where = where[:where.index("return ")]
    assert 'flt == "spamblock"' in where, "нет фильтра списка spamblock"
    assert "COALESCE(acc_status,'ok') = 'spamblock'" in where
    # active больше не тянет спамблок
    assert "NOT IN ('banned','spamblock')" in where, "active-фильтр не исключает спамблок"


def test_filter_whitelists_include_spamblock():
    api = _read("services/mini_app_api.py")
    # оба места валидации фильтра (список + select_all_filtered); "dead" добавлен
    # для массового удаления невоскрешаемых.
    assert api.count('("all", "active", "cooldown", "banned", "spamblock", "dead")') == 2


def test_stats_counts_spamblock_separately():
    api = _read("services/mini_app_api.py")
    assert "COALESCE(acc_status,'ok')='spamblock') AS spamblock" in api
    assert "NOT IN ('banned','spamblock')\n" in api or "NOT IN ('banned','spamblock')" in api
    # ключ выведен наружу в stats-словарь
    assert '"total", "banned", "spamblock", "cooldown", "dead", "active"' in api


def test_frontend_has_spamblock_kpi_and_honest_active():
    ui = _read("mini_app/index.html")
    assert "filterAcc('spamblock'" in ui, "нет KPI-чипа спамблока"
    assert 'id="kpi-spam"' in ui
    # честный active в клиентском фолбэке + отдельный счётчик спамблока
    assert "a.acc_status!=='spamblock'" in ui
    assert "stats.spamblock" in ui
    assert "el('kpi-spam', spam)" in ui
