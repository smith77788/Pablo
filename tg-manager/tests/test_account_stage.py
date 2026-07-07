"""Регрессия: CRM-статусы аккаунта (stage) — паритет бэкенд↔UI и проводка.

mini_app_api не импортируется в песочнице (нет asyncpg), поэтому проверяем на
уровне исходников: (1) whitelist ACCOUNT_STAGES на бэке и карта ACC_STAGES в UI
совпадают ключ-в-ключ — иначе UI предложит статус, который бэк отвергнет 400;
(2) set_stage реально пишет в БД со скоупом owner_id; (3) meta валидирует stage.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXPECTED = {"new", "warming", "ready", "in_work", "resting", "frozen", "reserve"}


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_backend_stage_whitelist_exact():
    src = _read("services/mini_app_api.py")
    m = re.search(r"ACCOUNT_STAGES\s*=\s*\{([^}]*)\}", src)
    assert m, "ACCOUNT_STAGES не найден"
    keys = set(re.findall(r'"([a-z_]+)"', m.group(1)))
    assert keys == EXPECTED, keys


def test_ui_stage_map_matches_backend():
    ui = _read("mini_app/index.html")
    m = re.search(r"const ACC_STAGES\s*=\s*\{(.*?)\n\};", ui, re.DOTALL)
    assert m, "ACC_STAGES в UI не найден"
    keys = set(re.findall(r"\n\s*([a-z_]+):\s*\{emoji", m.group(1)))
    assert keys == EXPECTED, keys


def test_set_stage_mass_scoped_write():
    src = _read("services/mini_app_api.py")
    assert 'op == "set_stage"' in src
    # запись должна быть скоупнута по owner_id и множеству id (не глобальный UPDATE)
    assert re.search(
        r"UPDATE tg_accounts SET stage=\$1 WHERE owner_id=\$2 AND id=ANY", src
    ), "set_stage пишет без скоупа owner_id/id"


def test_meta_validates_stage():
    src = _read("services/mini_app_api.py")
    assert 'if "stage" in body' in src
    assert "not in ACCOUNT_STAGES" in src, "meta не валидирует stage по whitelist"


def test_stage_stats_scoped_and_whitelisted():
    src = _read("services/mini_app_api.py")
    # разбивка by_stage должна считаться по owner_id и фильтроваться whitelist'ом
    assert '"by_stage"' in src, "нет разбивки by_stage в stats"
    assert re.search(
        r"FROM tg_accounts\s+\"?\s*\n?\s*\"?WHERE owner_id=\$1 AND stage IS NOT NULL GROUP BY stage",
        src,
    ) or "WHERE owner_id=$1 AND stage IS NOT NULL GROUP BY stage" in src, (
        "by_stage считается без скоупа owner_id"
    )
    assert 'r.get("stage") in ACCOUNT_STAGES' in src, "by_stage не фильтрует по whitelist"


def test_ui_stage_filter_composes_with_health():
    ui = _read("mini_app/index.html")
    # срез по статусу должен КОМБИНИРОВАТЬСЯ с фильтром здоровья внутри _accFiltered
    assert "if (ACC_STAGE_FILTER) list = list.filter(a=>a.stage===ACC_STAGE_FILTER)" in ui, (
        "stage-фильтр не встроен в _accFiltered — не скомбинируется с health-фильтром"
    )
    assert "renderStageChips" in ui
