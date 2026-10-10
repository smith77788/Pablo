"""Гейт: каждый полностью статический вызов api('/api/miniapp/…') во фронте
имеет зарегистрированный маршрут на бэке — иначе экран получает 404.

Самый широкий контракт «живого продукта»: мини-апп зовёт сотни эндпоинтов; при
переименовании пути на бэке (или опечатке во фронте) экран молча ломается
ошибкой. Проверяем статически — без БД и сервера.

ОБЛАСТЬ: только ПОЛНОСТЬЮ статические пути. Динамические вызовы
(`'/api/.../'+id+'/check'` или шаблоны с ${…}) пропускаем — им на бэке
соответствуют параметрические маршруты `{acc_id}`, которые статически не
сопоставить без ложных срабатываний. Опечатки в статических путях — самый
частый класс, и он покрыт полностью.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def _backend_static_routes() -> set[str]:
    """Маршруты СОБРАННОГО приложения, а не строки в тексте одного файла:
    обработчики режутся по модулям `services/mini_app_*.py`, и текстовый поиск
    объявлял живые маршруты несуществующими."""
    from tests.miniapp_routes import registered_routes

    entries = registered_routes()
    assert len(entries) > 800, "инвентарь маршрутов пуст — проверка измеряет не то"
    # OPTIONS /api/miniapp/{path} — общий ответ на preflight, он матчит
    # ЛЮБОЙ путь и делал бы проверку слепой: мёртвая кнопка «нашла бы»
    # себе маршрут. Preflight — не эндпоинт, его не считаем.
    routes = {e.split(" ", 1)[1] for e in entries
              if not e.startswith("OPTIONS ")}
    return {r for r in routes if "{" not in r and r.startswith("/api/miniapp/")}


def _frontend_static_calls() -> set[str]:
    calls = re.findall(
        r"""(?:api|fetchT)\(\s*[`'"](/api/miniapp/[^`'"]+)[`'"]""", HTML)
    out: set[str] = set()
    for c in calls:
        path = c.split("?")[0]
        # ${…}/{…} — шаблон; хвост '/' — префикс конкатенации ('/api/…/'+id)
        if "$" in path or "{" in path or path.endswith("/"):
            continue
        out.add(path)
    return out


def test_all_static_frontend_paths_have_backend_route():
    static_routes = _backend_static_routes()
    static_calls = _frontend_static_calls()
    assert len(static_calls) > 150, (
        f"извлекли подозрительно мало путей ({len(static_calls)}) — сломался парсер"
    )
    missing = sorted(p for p in static_calls if p not in static_routes)
    assert not missing, (
        "фронт зовёт эндпоинты, которых нет на бэке (будет 404):\n  "
        + "\n  ".join(missing)
    )
