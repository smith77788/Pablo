"""Старт не молчит: bootstrap-сервер отдаёт фазу инициализации.

«starting» без деталей прятал, ГДЕ виснет холодный старт (пул/миграции/бот/веб).
Теперь фаза видна в теле health-ответа — при зависшем деплое сразу понятно, на
чём застряли, а не «приложение не грузится».
"""
from __future__ import annotations


def test_set_boot_phase_updates_global():
    import main
    main._set_boot_phase("db-pool+migrations")
    assert main._boot_phase == "db-pool+migrations"
    main._set_boot_phase("web-start")
    assert main._boot_phase == "web-start"


def test_phases_are_wired_in_startup():
    import os
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "main.py"), encoding="utf-8").read()
    # health-ответ несёт фазу, а не голое "starting"
    assert 'text=f"starting: {_boot_phase}"' in src
    for phase in ("db-pool+migrations", "bot-commands", "web-start"):
        assert f'_set_boot_phase("{phase}")' in src, phase
