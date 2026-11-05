"""Рабочее место виртуального админа в настоящем браузере.

`deploy/scripts/check_va_workspace.mjs` проходит самые хрупкие места: поиск
каналов и пагинация (страница по 30), черновик проекта, который не должен
теряться при переключении вкладки и обновлении экрана, сохранение города и
границ услуг, предпросмотр и отключение фото, защита от УСТАРЕВШЕГО ответа
(поздний ответ сервера не должен затирать выбранный канал) и отсутствие
горизонтальной прокрутки на телефоне и на десктопе.

Его не звал НИКТО: ни тест, ни workflow, ни строчка в CLAUDE.md. Больше того,
локально он и не мог запуститься — брал Playwright только из переменной
`PLAYWRIGHT_JS`, а без неё шёл в `import('playwright')` и падал на импорте.
Приговор инструмента, который никто не запускает, ничего не значит.

ЧЕГО ЭТОТ ТЕСТ НЕ ДАЁТ. В CI он ПРОПУСКАЕТСЯ: в `.github/workflows/tests.yml`
нет ни Chromium, ни установки Playwright. Защита локальная — она держит руки
того, кто правит рабочее место ВА у себя. Так же устроены соседи
`test_screen_modules_walk.py` и `test_miniapp_tap_targets_live.py`.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "scripts" / "check_va_workspace.mjs"
_CHROME = (os.getenv("CHROME_BIN", ""),
           "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
           "/opt/pw-browsers/chromium/chrome-linux/chrome")
_PW = (os.getenv("PLAYWRIGHT_JS", ""),
       "/opt/node22/lib/node_modules/playwright/index.mjs")

_DONE = "Готово: поиск, пагинация"


def _missing() -> str | None:
    if not shutil.which("node"):
        return "нет node"
    if not any(p and pathlib.Path(p).exists() for p in _CHROME):
        return "нет Chromium (задайте CHROME_BIN)"
    if not any(p and pathlib.Path(p).exists() for p in _PW):
        return "нет Playwright (задайте PLAYWRIGHT_JS)"
    return None


pytestmark = pytest.mark.skipif(_missing() is not None, reason=str(_missing()))


def test_the_va_workspace_survives_its_fragile_flows():
    proc = subprocess.run(["node", str(SCRIPT)], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=900)
    assert proc.returncode == 0, (
        "проверка рабочего места ВА упала:\n"
        + (proc.stdout[-800:] or "") + "\n" + (proc.stderr[-1200:] or ""))
    assert _DONE in proc.stdout, (
        "проверка дошла до конца, но не сказала этого — значит прошла не она:\n"
        + proc.stdout[-500:])


def test_the_check_still_checks_something():
    """Знаменатель: сколько утверждений осталось в самой проверке.

    «Зелено» у скрипта, из которого вынули проверки, выглядит точно так же,
    как «зелено» у работающего. Число держим снизу, а не точным: добавлять
    утверждения можно, молча убирать — нет.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    checks = src.count("throw new Error")
    assert checks >= 14, (
        f"в проверке рабочего места ВА осталось {checks} утверждений — "
        "часть молча убрали, и её «готово» больше ничего не значит")
