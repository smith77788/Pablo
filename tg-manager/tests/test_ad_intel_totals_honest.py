"""Регресс: KPI «Рекламодателей» и «Размещений» в Разведке рекламы — честные.

`openAdIntel` рисует три KPI-плитки из ответа `/api/miniapp/ad_intel`:
Каналов (`total_channels`), Рекламодателей (`total_advertisers`), Размещений
(`total_placements`). Бэкенд отдавал ТОЛЬКО `total_channels` → две плитки из трёх
всегда показывали 0 даже при реальных данных, и плитка в «Ещё» вечно писала
«· 0 размещений». Это лгущая статистика (класс 4: счётчик не отражает факт).

Итоги считаются по ВСЕМ рекламодателям владельца (не по top-10, который идёт в
список): COUNT(*) и SUM(placements_count) из ad_advertisers.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "services" / "mini_app_api.py"
INDEX = ROOT / "mini_app" / "index.html"


def _handler_src() -> str:
    src = API.read_text(encoding="utf-8")
    m = re.search(r"    async def ad_intel_overview.*?(?=\n    async def )", src, re.DOTALL)
    assert m, "ad_intel_overview не найден"
    return m.group(0)


def test_backend_returns_keys_frontend_renders():
    h = _handler_src()
    for key in ("total_channels", "total_advertisers", "total_placements"):
        assert f'"{key}"' in h, (
            f"фронт рисует KPI из {key}; без него плитка вечно показывает 0"
        )


def test_totals_computed_over_all_advertisers_not_top10():
    h = _handler_src()
    assert "COUNT(*)" in h and "SUM(placements_count)" in h, (
        "итоги должны считаться по всем рекламодателям владельца, а не по top-10"
    )
    assert "FROM ad_advertisers WHERE owner_id=$1" in h, "итоги обязаны быть owner-скоуплены"


def test_frontend_still_reads_those_keys():
    # если фронт переименуют — тест должен заставить синхронизировать обе стороны
    html = INDEX.read_text(encoding="utf-8")
    i = html.find("api('/api/miniapp/ad_intel'")
    assert i != -1, "вызов ad_intel не найден"
    seg = html[i:i + 900]
    assert "total_advertisers" in seg and "total_placements" in seg
