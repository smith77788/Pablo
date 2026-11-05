"""Живой обход модулей с вкладками в настоящем браузере.

Разметку и реестр SCREEN_MODULES проверяет `test_merged_modules.py`, но
вкладка ломается и там, где текстом не видно: экран, собранный из JS, не
нарисовал полоску; open-функция открыла не тот экран; переключение
положило вкладку поверх, и «назад» перебирает их по одной. Скрипт
`deploy/scripts/screen_modules_walk.mjs` нажимает каждую вкладку каждого
модуля и сверяет стек, шапку и подсветку.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "scripts" / "screen_modules_walk.mjs"
_CHROME = (os.getenv("CHROME_BIN", ""), "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
           "/opt/pw-browsers/chromium/chrome-linux/chrome")
_PW = (os.getenv("PLAYWRIGHT_JS", ""), "/opt/node22/lib/node_modules/playwright/index.mjs")


def _missing() -> str | None:
    if not shutil.which("node"):
        return "нет node"
    if not any(p and pathlib.Path(p).exists() for p in _CHROME):
        return "нет Chromium (задайте CHROME_BIN)"
    if not any(p and pathlib.Path(p).exists() for p in _PW):
        return "нет Playwright (задайте PLAYWRIGHT_JS)"
    return None


pytestmark = pytest.mark.skipif(_missing() is not None, reason=str(_missing()))


def _first(paths: tuple[str, ...]) -> str:
    return next(p for p in paths if p and pathlib.Path(p).exists())


def test_every_module_tab_opens_its_screen_and_does_not_stack():
    # Скрипту передаём найденные пути: иначе без экспортированных
    # PLAYWRIGHT_JS/CHROME_BIN проверка «не пропускать» прошла бы, а сам
    # обход упал бы на импорте playwright.
    env = {**os.environ, "PLAYWRIGHT_JS": _first(_PW), "CHROME_BIN": _first(_CHROME)}
    proc = subprocess.run(["node", str(SCRIPT)], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, timeout=300)
    out = proc.stdout.strip()
    assert out, f"обход ничего не вернул: {proc.stderr[-800:]}"
    res = json.loads(out.splitlines()[-1])
    assert not res["errors"], f"сбои JS при переключении вкладок: {res['errors'][:5]}"
    assert not res["bad"], "вкладки ведут себя не так:\n" + json.dumps(res["bad"], ensure_ascii=False, indent=1)
