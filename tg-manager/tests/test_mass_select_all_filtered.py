"""Регрессия: масс-действия по всему серверному срезу (не только загруженной странице).

mini_app_api не импортируется в песочнице — проверяем на уровне исходников, что
accounts_mass резолвит весь набор через тот же owner/admin-скоуп WHERE
(_accounts_where) с лимитом-предохранителем, и что UI отправляет select_all_filtered
с текущим фильтром вместо списка id.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_backend_resolves_whole_filter_scoped():
    src = _read("services/mini_app_api.py")
    mass = src[src.index("async def accounts_mass"):]
    mass = mass[:mass.index("n = len(ids)") + 20]
    assert 'body.get("select_all_filtered")' in mass, "нет режима select_all_filtered"
    # использует тот же билдер WHERE со скоупом owner/admin
    assert "_accounts_where(uid, flt, stage, qterm, admin=admin)" in mass
    # предохранитель от неограниченного enqueue
    assert "LIMIT 5000" in mass


def test_ui_sends_select_all_filtered():
    ui = _read("mini_app/index.html")
    m = re.search(r"function _massSel\(\)\s*\{(.*?)\n\}", ui, re.DOTALL)
    assert m, "_massSel не найден"
    body = m.group(1)
    assert "select_all_filtered: true" in body
    assert "filter: ACC_FILTER" in body and "stage: ACC_STAGE_FILTER" in body and "q: ACC_SEARCH" in body
    # все масс-пути идут через _massSel (не жёстко account_ids)
    assert "op, ..._massSel()" in ui        # runAccMass
    assert "op:'set_stage', stage, ..._massSel()" in ui  # submitMassStage
    assert "Object.assign(payload, _massSel())" in ui    # submitAccProfile
