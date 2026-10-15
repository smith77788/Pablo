"""Регрессия: каждый хендлер-функция в mini_app_api.py обязана быть
зарегистрирована как маршрут (app.router.add_*).

Найденный баг (аудит правила №1 CLAUDE.md — «трассировка полной цепочки»):
коммит 3c4c4092 добавил 4 хендлера (uch_bulk_merge/export/importance/rating,
контакт-менеджер) с реальной логикой внутри, но НИ ОДИН из них не был
зарегистрирован через app.router.add_*. Обращение к этим «маршрутам» всегда
давало 404 — обратный класс бага прошлым дохлым кнопкам (там UI звал
несуществующий маршрут; здесь маршрут не существовал вообще, хотя код для
него был написан и мог быть вызван UI в любой момент).

Этот тест ловит ЛЮБОЙ будущий хендлер, оставленный незарегистрированным, не
только эти четыре — статический скан всего файла.
"""
from __future__ import annotations

import re


def _load_source() -> str:
    """Весь API-слой мини-аппа, а не один файл.

    Ручки переезжают из 25-тысячестрочного mini_app_api.py в соседние модули
    services/mini_app_*.py (контакты, прокси, сценарии…). Скан по одному файлу
    после каждого выноса объявлял живые маршруты незарегистрированными.
    """
    from tests.miniapp_source import api_source

    return api_source()


def test_no_orphaned_route_handlers():
    src = _load_source()

    # Хендлер-подобные функции: async def name(request: web.Request) -> web.Response
    handler_defs = re.findall(r"async def (\w+)\(request: web\.Request\)", src)
    assert handler_defs, "не нашли ни одного хендлера — сломан сам скан"

    # Всё, что зарегистрировано через app.router.add_get/post/put/delete(..., name)
    registered = set(re.findall(r"app\.router\.add_\w+\([^)]*?,\s*(\w+)\s*[,)]", src, re.S))

    # Приватные хелперы (ведущее подчёркивание) — намеренно не маршруты,
    # вызываются напрямую другими хендлерами (напр. _admin_target).
    orphans = [h for h in handler_defs if h not in registered and not h.startswith("_")]

    assert not orphans, (
        "хендлеры определены, но ни разу не зарегистрированы как маршрут "
        f"(мёртвый код, обращение к ним даёт 404): {orphans}"
    )


def test_uch_bulk_handlers_specifically_registered():
    # Явная регрессия на конкретно найденный баг — если кто-то случайно уберёт
    # регистрацию обратно, тест упадёт даже если общий скан выше сломается.
    src = _load_source()
    for path, handler in (
        ("/api/miniapp/uch/bulk/merge", "uch_bulk_merge"),
        ("/api/miniapp/uch/bulk/export", "uch_bulk_export"),
        ("/api/miniapp/uch/bulk/importance", "uch_bulk_importance"),
        ("/api/miniapp/uch/bulk/rating", "uch_bulk_rating"),
    ):
        assert f'app.router.add_post("{path}", {handler})' in src, (
            f"маршрут {path} -> {handler} не зарегистрирован"
        )
