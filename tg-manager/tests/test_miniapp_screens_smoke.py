"""Каждый экран мини-аппа открывается дважды и не падает.

Почему запуск, а не разбор текста.

Самый частый способ сломать экран здесь — обратиться к элементу, который лежит
внутри контейнера, перерисовываемого этой же функцией. Первое открытие проходит,
второе падает на null: «Cannot set properties of null». Ровно так 2026-09-05 лёг
экран инвайтинга, причём падало ДО первого await — то есть со второго открытия
не грузилось вообще ничего.

Статически класс не ловится: область видимости переменных JS регулярками не
разрешается. Две попытки это сделать дали ложные находки — одноимённые
переменные в разных функциях указывают на разные элементы (`more` — то `s-more`,
то `vchatMore`). Поэтому здесь настоящий Chromium: экран открывается дважды, и
любое необработанное исключение — падение теста.

Стенд проверен на известном баге: с возвращённой поломкой инвайтинга он
сообщает ровно «openMassInvite — открытие №2, Cannot set properties of null».

Без Playwright/Chromium тест пропускается (как e2e на живом Postgres):
  node deploy/scripts/screens_smoke.mjs           # весь список
  node deploy/scripts/screens_smoke.mjs --only=openMassInvite
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "scripts" / "screens_smoke.mjs"

_CHROME_CANDIDATES = (
    os.getenv("CHROME_BIN", ""),
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/opt/pw-browsers/chromium/chrome-linux/chrome",
)
_PW_CANDIDATES = (
    os.getenv("PLAYWRIGHT_JS", ""),
    "/opt/node22/lib/node_modules/playwright/index.mjs",
)


def _missing() -> str | None:
    if not shutil.which("node"):
        return "нет node"
    if not any(p and pathlib.Path(p).exists() for p in _CHROME_CANDIDATES):
        return "нет Chromium (задайте CHROME_BIN)"
    if not any(p and pathlib.Path(p).exists() for p in _PW_CANDIDATES):
        return "нет Playwright (задайте PLAYWRIGHT_JS)"
    return None


pytestmark = pytest.mark.skipif(_missing() is not None, reason=str(_missing()))


@pytest.fixture(scope="module")
def smoke() -> dict:
    """Один запуск браузера на весь модуль: подъём Chromium дорогой."""
    proc = subprocess.run(
        ["node", str(SCRIPT), "--json"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=900,
    )
    out = proc.stdout.strip()
    assert out, f"смоук ничего не вернул: {proc.stderr[-800:]}"
    return json.loads(out)


def test_screens_were_actually_checked(smoke):
    """Пустой список экранов означал бы сломанный стенд, а не здоровый мини-апп."""
    assert smoke["checked"] > 50, (
        f"проверено всего {smoke['checked']} экранов — похоже, стенд не нашёл "
        "функции открытия, а не в приложении их нет"
    )


def test_no_screen_breaks_on_reopen(smoke):
    """Ни один экран не должен падать ни на первом, ни на втором открытии."""
    fails = smoke["failures"]
    report = "\n".join(
        f"  {f['screen']} — открытие №{f['pass']}: " + "; ".join(f["errors"])
        for f in fails
    )
    assert not fails, f"экраны падают при открытии:\n{report}"
