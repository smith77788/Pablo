"""Метрики процесса (находка аудита №6).

БЫЛО: ни метрик, ни трассировки, ни агрегации ошибок — только логи. Для
платформы, выполняющей необратимые массовые действия чужими аккаунтами, это
значит, что о деградации узнаёшь из жалобы клиента. Симптом виден в истории
самого продукта: диагноз «работали 0 из 28» ставился по скриншотам отчёта.

Аудит называл четыре сигнала — их и проверяем: исход+длительность операций,
события флуда, смерти сессий, насыщение пула БД.
"""
from __future__ import annotations

import ast
import os

import pytest

from services import metrics as m

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _clean():
    m.reset()
    yield
    m.reset()


def _func_src(rel: str, name: str) -> str:
    src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(src.split("\n")[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{name} не найдена в {rel}")


# ── формат и поведение ────────────────────────────────────────────────────────

def test_render_is_valid_prometheus_exposition():
    m.inc("infragram_operations_total", {"op_type": "mass_invite", "status": "done"})
    m.gauge("infragram_db_pool_connections", 18, {"state": "active"})
    out = m.render()
    assert "# TYPE infragram_operations_total counter" in out
    assert 'infragram_operations_total{op_type="mass_invite",status="done"} 1' in out
    assert "# TYPE infragram_db_pool_connections gauge" in out
    # каждая строка данных — «имя[{метки}] число»
    for line in out.strip().split("\n"):
        if line.startswith("#") or not line:
            continue
        assert len(line.rsplit(" ", 1)) == 2, f"кривая строка экспозиции: {line!r}"
        float(line.rsplit(" ", 1)[1])


def test_labels_are_escaped_not_breaking_format():
    """Метка с кавычкой/переводом строки не должна ломать весь ответ."""
    m.inc("infragram_operations_total", {"op_type": 'ху"дой\nтип', "status": "failed"})
    out = m.render()
    data = [l for l in out.split("\n") if l and not l.startswith("#")]
    assert all(l.count("\n") == 0 for l in data)
    assert '\\"' in out


def test_observe_gives_sum_and_count_for_average():
    m.observe("infragram_operation_seconds", 10.0, {"op_type": "x"})
    m.observe("infragram_operation_seconds", 20.0, {"op_type": "x"})
    out = m.render()
    assert 'infragram_operation_seconds_sum{op_type="x"} 30' in out
    assert 'infragram_operation_seconds_count{op_type="x"} 2' in out


def test_metrics_never_raise_on_bad_input():
    """Метрика не имеет права уронить работу — она наблюдатель, а не участник."""
    m.inc("x", {"k": object()})            # неконвертируемая метка
    m.observe("y", float("nan"))
    m.gauge("z", "не-число")               # мусор вместо значения
    m.render()                              # не должно бросить


# ── четыре сигнала из аудита реально подключены ───────────────────────────────

def test_operation_outcome_and_duration_are_instrumented():
    body = _func_src("services/op_worker.py", "_run_op_task")
    assert "infragram_operations_total" in body, "исход операции не считается"
    assert "infragram_operation_seconds" in body, "длительность операции не меряется"
    assert "_t_started" in body


def test_flood_events_are_instrumented():
    for fn in ("record_flood", "record_peer_flood"):
        body = _func_src("services/flood_engine.py", fn)
        assert "infragram_flood_events_total" in body, f"{fn}: флуд не считается"


def test_session_deaths_are_instrumented():
    # Классификация ошибок вынесена в services/op_errors.py (распил монолита);
    # op_worker импортирует _is_dead_session_error обратно.
    body = _func_src("services/op_errors.py", "_is_dead_session_error")
    assert "infragram_session_deaths_total" in body, "смерть сессии не считается"
    # конфликт auth-key считаем ОТДЕЛЬНО: это сигнал нарушенного захвата (№1),
    # а не гибель сессии
    assert "auth_key_conflict" in body and "dead_session" in body


def test_db_pool_saturation_is_instrumented():
    body = _func_src("database/db.py", "monitor_pool_health")
    assert "infragram_db_pool_connections" in body, "насыщение пула не видно"
    for state in ("total", "active", "idle"):
        assert f'"{state}"' in body


def test_metrics_endpoint_registered():
    src = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    assert 'app.router.add_get("/metrics", prom_metrics)' in src, (
        "endpoint /metrics не зарегистрирован — собирать нечем")


def test_no_new_dependency_added():
    """Зависимости в проекте курируются: метрики не должны тянуть библиотеку."""
    reqs = open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8").read().lower()
    assert "prometheus" not in reqs
    src = open(os.path.join(ROOT, "services", "metrics.py"), encoding="utf-8").read()
    assert "import prometheus" not in src
