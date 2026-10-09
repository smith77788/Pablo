"""Регресс: панель аккаунтов честно разделяет спамблок (флот-здоровье).

mini_app_api не импортируется в песочнице — проверяем на уровне исходников:
1. Есть фильтр списка `spamblock` (раньше можно было только all/active/cooldown/banned).
2. Фильтр `active` и stats-счётчик `active` исключают спамблок — спамблокнутый
   аккаунт больше не протекает в «Активные». Проверяем это по СМЫСЛУ, а не по
   форме литерала: набор мёртвых статусов переехал в `account_status`, и срез,
   счётчик и клиентский фолбэк читают его оттуда. Пробник на точный текст
   `NOT IN ('banned','spamblock')` после этого стал ловить не защиту, а
   способ её записи — и краснел на усилении (в набор добавились `deleted`,
   `frozen`, отозванная сессия и аккаунты без сессии вообще).
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
    # active больше не тянет спамблок — и не тянет вообще никого мёртвого.
    # Срез берёт общее условие, а в нём набор из единственного места.
    from services import account_status
    i = api.index("_ACTIVE_ACC_SQL = (")
    active_sql = api[i:api.index(")\n", i)]
    assert "sql_dead_list()" in active_sql, (
        "срез «Активные» снова судит по своему списку статусов")
    assert "spamblock" in account_status.DEAD_STATUSES
    assert "clauses.append(_ACTIVE_ACC_SQL)" in where, (
        "срез списка перестал брать общее условие — он разъедется со счётчиком")


def test_filter_whitelists_include_spamblock():
    """Оба места валидации (список + select_all_filtered) берут ОДИН список.

    Раньше литерал был выписан в обоих местах, и тест считал его вхождения.
    ded9c360 вынес его в `ACCOUNT_FILTERS` — это ровно то, чего тест и хотел
    (списки не могут разъехаться, потому что список один), поэтому проверяем
    сам набор и то, что оба места сверяются с ним, а не форму литерала.
    """
    api = _read("services/mini_app_api.py")
    i = api.index("ACCOUNT_FILTERS = (")
    decl = api[i:api.index(")", i) + 1]
    for slice_name in ("all", "active", "cooldown", "banned", "spamblock", "dead"):
        assert f'"{slice_name}"' in decl, f"срез {slice_name} исчез из набора"
    assert api.count("not in ACCOUNT_FILTERS") == 2, (
        "валидация среза сверяется с набором не в двух местах — один из них "
        "снова молча подменит срез на «все»")


def test_stats_counts_spamblock_separately():
    api = _read("services/mini_app_api.py")
    assert "COALESCE(acc_status,'ok')='spamblock') AS spamblock" in api
    # Счётчик готовых — то же общее условие, что у среза списка (оба ветки,
    # владелец и админ), иначе чип обещает не то, что откроет клик по нему.
    assert api.count('_ACTIVE_ACC_SQL + """) AS active') == 2, (
        "счётчик готовых считает по своему условию хотя бы в одной ветке")
    # ключ выведен наружу в stats-словарь. Проверяем сам ключ, а не точный
    # список: набор счётчиков расширяется (добавился, например, proxy_down —
    # аккаунты, простаивающие из-за мёртвого прокси).
    assert '"spamblock"' in api and '"cooldown"' in api and '"active"' in api


def test_frontend_has_spamblock_kpi_and_honest_active():
    ui = _read("mini_app/index.html")
    assert "filterAcc('spamblock'" in ui, "нет KPI-чипа спамблока"
    assert 'id="kpi-spam"' in ui
    # Честный active в клиентском фолбэке: спамблок в него не протекает. Форму
    # записи не морозим — важно, что фолбэк судит по общему набору мёртвых
    # статусов (он совпадает с серверным, см. test_dead_status_is_one_vocabulary),
    # а не по двум статусам из шести.
    i = ui.index("const active = (stats && stats.active")
    fallback = ui[i:ui.index("\n", i)]
    assert "ACC_DEAD_STATUSES" in fallback, fallback
    assert "'spamblock'" in ui[ui.index("const ACC_DEAD_STATUSES"):
                               ui.index("const ACC_DEAD_STATUSES") + 300]
    assert "stats.spamblock" in ui
    assert "el('kpi-spam', spam)" in ui
