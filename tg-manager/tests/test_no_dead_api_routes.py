"""Гейт против «мёртвых кнопок»: каждый вызов api('/api/miniapp/...') во фронте
имеет зарегистрированный маршрут на бэке.

Класс багов #4 (мёртвая кнопка/404): фронт зовёт эндпоинт, которого нет — кнопка
молча падает. Тест извлекает ЛИТЕРАЛЬНЫЕ пути (template-literal и обычные строки без
конкатенации) и сверяет с router.add_*(...). Прокинутые через '+' префиксы (кончаются
на '/') не проверяем — путь собирается в рантайме, статически не восстановить.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")


def _route_patterns() -> list[re.Pattern]:
    """Маршруты бэка → регэкспы: {param} матчит любой непустой сегмент."""
    pats = []
    for m in re.finditer(r"""add_(?:get|post|put|delete|patch)\(\s*['"]([^'"]+)['"]""", API):
        route = m.group(1).split("?")[0]
        rx = "^" + re.sub(r"\{[^}]+\}", r"[^/]+", re.escape(route).replace(r"\{", "{").replace(r"\}", "}")) + "$"
        pats.append(re.compile(rx))
    return pats


def _frontend_example_paths() -> set[str]:
    """Каждый api('/api/miniapp/...') → конкретный путь-пример (динамика → '1')."""
    calls = set()
    for m in re.finditer(r"""\bapi\(\s*(['"`])(/api/miniapp/[^'"`]*)\1""", HTML):
        raw = m.group(2).split("?")[0]
        if raw.endswith("/"):
            continue  # префикс-конкатенация ('/api/miniapp/foo/' + id) — не восстановить статически
        example = re.sub(r"\$\{[^}]+\}", "1", raw)   # ${id} → 1 (пример значения)
        calls.add(example)
    return calls


def test_every_frontend_api_call_has_backend_route():
    pats = _route_patterns()
    missing = sorted(p for p in _frontend_example_paths()
                     if not any(rx.match(p) for rx in pats))
    assert not missing, "Фронт зовёт несуществующие маршруты (мёртвые кнопки):\n" + "\n".join(missing)
