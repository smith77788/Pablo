"""Экраны мини-аппа на записях без необязательных полей.

Старая запись в базе, которой колонку добавили позже, приходит на экран без
части полей. Клиент вставлял их в подпись как есть, и человек читал:

    «undefined ботов · 0 кластеров»      (экран «Ещё»)
    «undefined подписчиков»              (топология)
    «NaN»                                (центр кольца в командном центре)
    «⚠️ Ошибка Cannot read properties of undefined (reading 'slice')»

Для владельца, который не читает по-английски, последнее — просто мусор, а
«undefined» и «NaN» неотличимы от поломки продукта: это не читается как «нет
данных».

Здесь мини-апп поднимается в настоящем браузере, сервер отвечает записями, у
которых ЕСТЬ первичные ключи (в базе они обязательны) и НЕТ необязательных
полей, вызываются все загрузчики экранов, и в видимом тексте не должно быть ни
«undefined», ни «null», ни «NaN».

Почему ключи подставляются: без них подпись «Бот #undefined» — артефакт замера,
а не дефект продукта, и проверка начала бы требовать правок там, где всё верно.

Нужен Chromium и Playwright; без них тест пропускается (в CI браузера нет).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve().parent / "js" / "miniapp_sparse_data.mjs"
CHROME = [
    os.environ.get("CHROME_BIN", ""),
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/opt/pw-browsers/chromium/chrome-linux/chrome",
]
PLAYWRIGHT = [
    os.environ.get("PLAYWRIGHT_JS", ""),
    "/opt/node22/lib/node_modules/playwright/index.mjs",
]


def _has(paths: list[str]) -> bool:
    return any(p and Path(p).exists() for p in paths)


pytestmark = pytest.mark.skipif(
    not (shutil.which("node") and _has(CHROME) and _has(PLAYWRIGHT)),
    reason="нет node, Chromium или Playwright — проверка требует настоящего браузера")


@pytest.fixture(scope="module")
def report() -> dict:
    res = subprocess.run(["node", str(SCRIPT)], cwd=ROOT, capture_output=True,
                         text=True, timeout=300)
    assert res.returncode == 0, res.stderr[-1500:]
    return json.loads(res.stdout)


def test_the_probe_really_opened_the_screens(report):
    """Страховка измерителя: ничего не вызвав, он всегда «зелёный»."""
    assert report["screens"] > 100, f"экранов найдено {report['screens']}"
    assert report["called"]["tried"] > 150, (
        f"загрузчиков вызвано {report['called']['tried']} — проверка измеряет не то")


def test_no_screen_loader_throws_on_sparse_data(report):
    assert not report["called"]["failed"], (
        "загрузчик экрана упал на записи без необязательных полей:\n"
        + "\n".join(report["called"]["failed"]))
    assert not report["errors"], "\n".join(report["errors"])


def test_no_screen_shows_undefined_or_nan(report):
    assert not report["bad"], (
        "в видимом тексте экрана оказалось значение, которое человек читает как "
        "поломку:\n" + "\n".join(report["bad"]))
