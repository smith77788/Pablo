"""Здоровье инфраструктуры: с оповещением можно что-то сделать.

Экран показывал находки детектора аномалий как текст и только. Типы и
детекторы вылезали латиницей — «error_spike», «queue_surge», «reassign»,
хотя владелец по-английски не читает. Снять разобранное оповещение можно
было лишь из бота (`bot/handlers/infra_health_center.py`): в мини-аппе оно
висело, пока детектор сам не перестанет его находить. И «затронуто: 812»
никуда не вело, хотя очередь — соседний экран.
"""
from __future__ import annotations

import ast
import functools
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "mini_app", "index.html")
API = os.path.join(ROOT, "services", "mini_app_api.py")
DET = os.path.join(ROOT, "services", "anomaly_detector.py")


@functools.lru_cache(maxsize=1)
def _html() -> str:
    with open(HTML, encoding="utf-8") as f:
        return f.read()


def _js_func(name: str) -> str:
    h = _html()
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", h)
    assert m, f"функция {name} не найдена"
    depth = 0
    for j in range(m.end() - 1, len(h)):
        if h[j] == "{":
            depth += 1
        elif h[j] == "}":
            depth -= 1
            if depth == 0:
                return h[m.start():j + 1]
    raise AssertionError(name)


def _py_func(path: str, name: str) -> str:
    with open(path, encoding="utf-8") as f:
        src = f.read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{name} не найдена в {path}")


def test_the_detector_is_still_the_source():
    """Антивакуумность: экран смотрит на anomaly_events, а не на мёртвую таблицу."""
    body = _py_func(API, "infra_health_overview")
    assert "get_active_anomalies" in body
    # Комментарии объясняют, почему старую таблицу не читают, — смотрим код.
    code = "\n".join(l for l in body.split("\n") if not l.strip().startswith("#"))
    assert "infrastructure_alerts" not in code, "вернулась таблица, которую никто не пишет"


def test_every_anomaly_type_is_translated():
    """Список типов задан в anomaly_detector — перевод должен покрывать его весь."""
    with open(DET, encoding="utf-8") as f:
        det = f.read()
    written = set(re.findall(r'anomaly_type="(\w+)"', det))
    assert written, "не нашли типов аномалий — проверка потеряла смысл"
    m = re.search(r"const IH_ANOMALY = \{(.*?)\n\};", _html(), re.S)
    assert m, "словаря типов аномалий нет"
    known = set(re.findall(r"^\s{2}(\w+):", m.group(1), re.M))
    assert written <= known, f"без перевода остались: {sorted(written - known)}"


def test_alert_leads_to_where_the_problem_is():
    body = _js_func("openInfraHealth")
    assert "ihAnomalyGo(" in body, "оповещение никуда не ведёт"
    src = _html()
    for target in ("openOps()", "openHealth()", "healthGoAccounts("):
        assert target in re.search(r"const IH_ANOMALY = \{(.*?)\n\};", src, re.S).group(1), \
            f"ни одна аномалия не ведёт через {target}"


def test_alert_can_be_dismissed_from_the_mini_app():
    body = _js_func("openInfraHealth")
    assert "ihResolve(" in body, "снять оповещение с экрана нечем"
    res = _js_func("ihResolve")
    assert "/infra_health/anomaly/" in res and "/resolve" in res
    assert "askConfirm" in res
    api_src = open(API, encoding="utf-8").read()
    assert 'add_post("/api/miniapp/infra_health/anomaly/{anomaly_id}/resolve"' in api_src


def test_resolve_is_owner_scoped():
    body = _py_func(API, "infra_health_resolve")
    assert "_get_uid(request)" in body and "401" in body
    assert "resolve_anomaly(pool, anom_id, uid)" in body, "чужое оповещение можно снять"
    assert "validate_integer" in body


def test_recovery_rows_speak_russian():
    body = _js_func("openInfraHealth")
    assert "ihRecoveryRu(" in body and "ihActionRu(" in body, (
        "тип восстановления и действие показываются сырыми идентификаторами"
    )


def test_row_click_does_not_swallow_the_button():
    """Строка ведёт в очередь, кнопка снимает — нажатие не должно делать оба."""
    body = _js_func("openInfraHealth")
    assert "event.stopPropagation()" in body


def test_error_has_a_way_out():
    assert "errHtml(errRu(e), 'openInfraHealth()')" in _js_func("openInfraHealth")


def test_empty_state_says_what_is_being_watched():
    body = _js_func("openInfraHealth")
    assert "каждые пять минут" in body, "пустота не объясняет, что вообще проверяется"
    assert "Система ничего не чинила" in body
