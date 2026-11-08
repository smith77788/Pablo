"""Массовое управление ресурсами в настоящем браузере.

deploy/scripts/check_fleet_tools.mjs открывает весь мини-апп с подставным API
и проверяет: на широком экране аккаунты — таблица, на телефоне — карточки;
клик по заголовку сортирует на сервере; сохранённый вид и чип тега уходят в
запрос; Shift+клик выделяет диапазон; выбранные прокси уходят одним запросом.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "scripts" / "check_fleet_tools.mjs"


def test_fleet_tools_in_a_real_browser():
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node")
    try:
        res = subprocess.run([node, str(SCRIPT)], cwd=ROOT, capture_output=True,
                             text=True, timeout=180, env=os.environ.copy())
    except subprocess.TimeoutExpired:
        pytest.fail("проверка в браузере не уложилась в 3 минуты")
    lines = [ln for ln in res.stdout.splitlines() if ln.startswith("{")]
    if not lines:
        err = (res.stderr or "")[-600:]
        if "Executable doesn't exist" in err or "Cannot find" in err or "ERR_MODULE_NOT_FOUND" in err:
            pytest.skip("нет Chromium или Playwright: " + err[-200:])
        pytest.fail("проверка упала:\n" + err)
    out = json.loads(lines[-1])
    assert out["errors"] == [], out["errors"]
    assert out["wide_rows"] == 3 and out["phone_cards"] == 3 and out["phone_table"] == 0
    assert out["tag_chip"] == "#прогрев 2" and out["view_chip"] and out["sort_select"] == 10
    assert "sort=trust" in out["header_sort"]
    assert "filter=dead" in out["view_query"] and "sort=trust&dir=asc" in out["view_query"]
    assert "tag=" in out["tag_query"]
    assert out["range_selected"] == 3 and out["pp_selected"] == 3
    assert out["pp_bulk"] == {"op": "backup_on", "proxy_ids": [11, 12, 13]}
