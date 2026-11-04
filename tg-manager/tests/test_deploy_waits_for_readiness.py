"""Платформа переключает трафик на новый экземпляр только когда он готов.

Без проверки здоровья в railway.json Railway считал новый деплой живым сразу
после старта контейнера и переводил на него трафик, пока тот ещё накатывал
миграции: всё это время мини-апп у всех клиентов отвечал «Сервис
перезапускается», хотя старый экземпляр мог обслуживать дальше.

Теперь проверка — `/health/ready` (база отвечает, цикл событий не стоит):
стартовая заглушка отвечает на неё 503 до конца инициализации, и старый
экземпляр работает, пока новый не готов. Не стал готов за `healthcheckTimeout`
— деплой не переключается, прод остаётся на прежней версии.

Что готовность в настоящем процессе действительно становится 200 —
tests/test_startup_survives_telegram_outage.py::test_readiness_turns_green_in_a_real_process.
"""
from __future__ import annotations

import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _deploy(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["deploy"]


def test_both_railway_configs_gate_on_readiness():
    for rel in ("railway.json", os.path.join("tg-manager", "railway.json")):
        d = _deploy(os.path.join(REPO, rel))
        assert d.get("healthcheckPath") == "/health/ready", rel
        # Миграции на большой базе идут минутами: короткий предел ронял бы
        # деплой, который просто ещё не закончил старт.
        assert int(d.get("healthcheckTimeout") or 0) >= 300, rel


def test_bootstrap_answers_not_ready_on_that_path():
    import sys
    sys.path.insert(0, os.path.join(REPO, "tg-manager"))
    from aiohttp import web

    import main
    assert main._bootstrap_response(web, "/health/ready").status == 503
