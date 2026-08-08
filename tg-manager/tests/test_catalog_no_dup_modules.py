"""Регрессия по скриншотам: «много дублей, несколько дашбордов, нерабочих модулей».

Каталог «Все функции» (mgmt-tile) содержал ОДНУ функцию под РАЗНЫМИ именами
(openMassPub → «Массопубликация»/«Массовая публикация»; openBotFactory →
«Фабрика ботов»/«Создать ботов»; openChannelFactory → «Фабрика каналов»/«Создать
каналы») — пользователь принимал это за разные модули. Плюс две плитки «Дашборд»
вели в РАЗНЫЕ экраны (unified vs метрики) — «несколько дашбордов».

Эти проверки фиксируют:
  1. один обработчик плитки = одно имя (нет «двух названий у одной функции»);
  2. каждый обработчик плитки реально определён (нет «нерабочих модулей»);
  3. два дашборда имеют РАЗНЫЕ имена (не два одинаковых «Дашборд»).
"""
from __future__ import annotations

import glob
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _index() -> str:
    return (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def _all_js() -> str:
    src = _index()
    for f in glob.glob(str(ROOT / "mini_app" / "screens" / "*.js")):
        src += "\n" + Path(f).read_text(encoding="utf-8")
    return src


def _tiles(html: str):
    return re.findall(
        r'class="mgmt-tile"[^>]*onclick="([^"]*)"[^>]*>.*?mgmt-tile-lbl">([^<]*)<',
        html, re.S)


def test_no_same_handler_multiple_labels():
    tiles = _tiles(_index())
    by_handler = defaultdict(set)
    for oc, lbl in tiles:
        by_handler[oc.strip()].add(lbl.strip())
    bad = {oc: labs for oc, labs in by_handler.items() if len(labs) > 1}
    assert not bad, (
        "Одна функция под разными именами (пользователь видит «дубли модулей»): "
        + "; ".join(f"{oc} → {sorted(labs)}" for oc, labs in bad.items())
    )


def test_all_tile_handlers_defined():
    html = _index()
    allsrc = _all_js()
    defined = set(re.findall(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", allsrc))
    defined |= set(re.findall(
        r"(?:const|let|var|window\.)\s*([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()",
        allsrc))
    allowed = {"goTab", "navGo", "push"}
    fns = set()
    for oc in re.findall(r'class="mgmt-tile"[^>]*onclick="([^"]+)"', html):
        m = re.match(r"\s*([A-Za-z_$][\w$]*)\s*\(", oc)
        if m:
            fns.add(m.group(1))
    dead = sorted(f for f in fns if f not in defined and f not in allowed)
    assert not dead, f"Нерабочие плитки (обработчик не определён): {dead}"


def test_dashboard_is_one_destination_with_one_name():
    """Раньше здесь требовались ДВА дашборда с разными именами — это отражало
    прежнее устройство, когда «Дашборд» и «Дашборд метрик» были разными экранами.

    После сведения всего в единый хаб (`openUnifiedDashboard`, жалоба владельца
    «остался лишь один единый полноценный дашборд») требование перестало быть
    верным и начало ПРОТИВОРЕЧИТЬ `test_no_same_handler_multiple_labels` в этом
    же файле: три плитки вели на один экран под тремя разными именами, и один
    гейт требовал ровно то, что запрещал другой. Ветка стояла красной.

    Правило теперь одно: у одного назначения одно имя. Детальные дашборды никуда
    не делись — их запускает вкладка «Все дашборды» хаба, это стережёт
    `test_single_dashboard_entry.test_detail_dashboards_still_reachable_from_hub`.
    """
    tiles = _tiles(_index())
    dash_tiles = [(oc.strip(), lbl.strip()) for oc, lbl in tiles if "ашборд" in lbl]
    assert dash_tiles, "вход в дашборд должен быть в каталоге"
    assert {oc for oc, _ in dash_tiles} == {"openUnifiedDashboard()"}, (
        "плитка с именем «Дашборд» обязана вести в единый хаб, а не в старый экран"
    )
    assert {lbl for _, lbl in dash_tiles} == {"Дашборд"}, (
        f"одно назначение — одно имя, иначе пользователь видит дубли модулей: "
        f"{sorted({lbl for _, lbl in dash_tiles})}"
    )
