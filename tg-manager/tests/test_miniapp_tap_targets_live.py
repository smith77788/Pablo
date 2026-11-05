"""Эргономика мини-аппа в настоящем браузере: зона нажатия и переполнение.

`tests/test_miniapp_tap_targets.py` читает таблицу стилей и ловит объявленный
размер. Но размер чаще получается из ВЁРСТКИ: строка легенды кольца в командном
центре — это `display:flex` с шрифтом 12px и отступом снаружи, и её высота
выходила 23 пикселя, хотя клик по ней открывает срез списка аккаунтов. В
объявленных стилях такого не видно.

Измеритель `deploy/scripts/check_tap_targets.mjs` это и мерит — и его не звал
НИКТО: ни тест, ни workflow. Поэтому девять зон ниже тридцати пикселей
(восемь строк командного центра и «Вставить шаблон» в массовой публикации)
прожили незамеченными. Приговор инструмента, который никто не запускает,
ничего не значит, поэтому он теперь под тестом.

ЧЕГО ЭТОТ ТЕСТ НЕ ДАЁТ. В CI он ПРОПУСКАЕТСЯ: в `.github/workflows/tests.yml`
нет ни Chromium, ни установки Playwright, и `skipif` ниже честно это
отрабатывает — ровно так же, как сосед `test_screen_modules_walk.py`. То есть
защита тут локальная: она держит руки того, кто правит мини-апп у себя, а не
рубеж в прогоне. Добавлять браузер в четыре шарда CI — решение с ценой за
пределами этой правки; пока его нет, запуск браузерных проверок перед пушем
записан в CLAUDE.md как часть цепочки.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "scripts" / "check_tap_targets.mjs"
_CHROME = (os.getenv("CHROME_BIN", ""),
           "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
           "/opt/pw-browsers/chromium/chrome-linux/chrome")
_PW = (os.getenv("PLAYWRIGHT_JS", ""),
       "/opt/node22/lib/node_modules/playwright/index.mjs")


def _missing() -> str | None:
    if not shutil.which("node"):
        return "нет node"
    if not any(p and pathlib.Path(p).exists() for p in _CHROME):
        return "нет Chromium (задайте CHROME_BIN)"
    if not any(p and pathlib.Path(p).exists() for p in _PW):
        return "нет Playwright (задайте PLAYWRIGHT_JS)"
    return None


pytestmark = pytest.mark.skipif(_missing() is not None, reason=str(_missing()))


def _run() -> dict:
    proc = subprocess.run(["node", str(SCRIPT)], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=900)
    line = next((ln for ln in reversed(proc.stdout.splitlines())
                 if ln.startswith("JSON ")), None)
    assert line, f"измеритель не отдал итог: {proc.stdout[-500:]} {proc.stderr[-500:]}"
    return json.loads(line[5:])


def test_no_tap_zone_is_too_small_and_nothing_overflows():
    res = _run()
    # Знаменатель: без него «находок нет» не отличить от «ни один экран не
    # открылся». Открывашек без аргументов в приложении больше двухсот.
    assert res["ran"] >= 200, (
        f"обойдено всего {res['ran']} экранов из {res['opens']} — "
        "измеритель не доехал, и «находок нет» ничего не означает"
    )
    assert not res["errors"], f"сбои JS при обходе экранов: {res['errors']}"
    assert res["over"] == 0, f"элементы вылезли за 360px: {res['worst'][:6]}"
    assert res["taps"] == 0, (
        f"зоны нажатия ниже 30 пикселей (обойдено {res['ran']}): {res['worst'][:8]}"
    )
