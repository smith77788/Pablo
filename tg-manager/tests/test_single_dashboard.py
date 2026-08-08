"""Гейт: в продукте РОВНО ОДИН дашборд (openUnifiedDashboard).

Пользователь многократно требовал: один единый всесторонний дашборд, а не
несколько. Этот гейт запрещает повторное появление вторых дашбордов —
open*Dashboard-открывашек, плиток/пунктов с ярлыком «Дашборд…» помимо главного,
дубль-метрик-экранов.
"""
from __future__ import annotations

import glob
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _mini_sources() -> dict[str, str]:
    out = {}
    idx = ROOT / "mini_app" / "index.html"
    out[str(idx)] = idx.read_text(encoding="utf-8")
    for f in glob.glob(str(ROOT / "mini_app" / "screens" / "*.js")):
        out[f] = Path(f).read_text(encoding="utf-8")
    return out


# Разрешённая единственная дашборд-открывашка.
_ALLOWED_DASH_OPENERS = {"openUnifiedDashboard"}
# Известные детальные экраны — это НЕ дашборды (здоровье/стата/аналитика).
# Их имена не содержат "Dashboard", поэтому под запрет не попадают.


def test_only_one_dashboard_opener_defined():
    """Определена ровно одна функция-открывашка дашборда: openUnifiedDashboard."""
    src = "\n".join(_mini_sources().values())
    openers = set(re.findall(r"(?:async\s+)?function\s+(open\w*Dashboard)\s*\(", src))
    openers |= set(re.findall(r"\b(open\w*Dashboard)\s*=\s*(?:async\s*)?(?:function|\()", src))
    extra = openers - _ALLOWED_DASH_OPENERS
    assert not extra, (
        "Заведён второй дашборд (запрещено — должен быть один openUnifiedDashboard): "
        + ", ".join(sorted(extra))
    )


def test_no_second_dashboard_opener_called():
    """Нигде не вызывается open*Dashboard, кроме единственного разрешённого."""
    src = "\n".join(_mini_sources().values())
    called = set(re.findall(r"\b(open\w*Dashboard)\s*\(", src))
    extra = called - _ALLOWED_DASH_OPENERS
    assert not extra, (
        "Вызов второго дашборда (запрещено): " + ", ".join(sorted(extra))
    )


def test_no_dashboard_labeled_tiles_besides_main():
    """Ни в mgmt-плитках, ни в лаунчерах нет пунктов с ярлыком «Дашборд…»,
    кроме единственной главной плитки «Дашборд» → openUnifiedDashboard."""
    srcs = _mini_sources()
    bad = []
    for path, src in srcs.items():
        # mgmt-tile: (onclick, label)
        for oc, lbl in re.findall(
            r'class="mgmt-tile"[^>]*onclick="([^"]*)"[^>]*>.*?mgmt-tile-lbl">([^<]*)<',
            src, re.S):
            if "ашборд" in lbl and oc.strip().rstrip("()") not in _ALLOWED_DASH_OPENERS:
                bad.append(f"{Path(path).name}: плитка «{lbl.strip()}» → {oc.strip()}")
        # JS-лаунчеры вида { fn: '...', lbl: 'Дашборд...' }
        for fn, lbl in re.findall(r"fn:\s*'([^']+)'[^}]*lbl:\s*'([^']*ашборд[^']*)'", src):
            if fn not in _ALLOWED_DASH_OPENERS:
                bad.append(f"{Path(path).name}: лаунчер «{lbl}» → {fn}")
    assert not bad, "Второй дашборд по ярлыку (запрещено):\n" + "\n".join(bad)
