"""Фоновая задача не может тихо исчезнуть, и её падение не проходит молча.

Event loop держит задачу только СЛАБОЙ ссылкой — это прямо оговорено в
документации `asyncio.create_task`. Задача, на которую больше никто не
ссылается, может быть собрана сборщиком мусора ПОСРЕДИ работы, и снаружи это
выглядит как «сервис молча перестал работать», причём без единой строчки в
логе. В op_worker этот же класс уже закрыт для задач операций (`_active_op_tasks`,
там он описан как «тихая смерть на самом критичном пути»), а все ~59 фоновых
циклов процесса запускались ровно так — без ссылки.

Дороже всего это стоило одноразовым задачам. Возобновление прерванных рассылок
(`broadcaster.resume_interrupted`) запускается один раз при старте: потеря этой
задачи означает, что рассылка, оборванная деплоем, не догонится НИКОГДА.

Второе следствие — невидимость. `create_task` без обработчика прячет исключение
до сборки мусора: максимум, что появлялось, — «Task exception was never
retrieved» в неизвестный момент и без имени сервиса.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_MAIN = _ROOT / "main.py"


def _create_task_calls(src: str):
    tree = ast.parse(src)
    out = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "create_task"
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "asyncio"):
            out.append(n.lineno)
    return out


def _spawn_body(src: str) -> str:
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_spawn":
            return ast.get_source_segment(src, n) or ""
    raise AssertionError("в main.py нет помощника _spawn")


def test_main_starts_background_work_only_through_spawn():
    src = _MAIN.read_text(encoding="utf-8")
    inside_spawn = set(_create_task_calls(_spawn_body(src)))
    raw = [ln for ln in _create_task_calls(src)]
    # Единственный законный create_task — внутри самого _spawn.
    body_lines = len(inside_spawn)
    assert body_lines == 1, "внутри _spawn ожидается ровно один create_task"
    assert len(raw) == 1, (
        "фоновая задача запущена мимо _spawn: на неё не останется ссылки, и "
        f"сборщик мусора может убрать её посреди работы (строки {raw})"
    )


def test_spawn_holds_a_reference_and_reports_failures():
    body = _spawn_body(_MAIN.read_text(encoding="utf-8"))
    assert "_BG_TASKS.add(task)" in body, "ссылку на задачу обязаны удержать"
    assert "add_done_callback" in body, "завершение задачи обязано разбираться"


def test_finished_task_releases_its_reference():
    """Множество ссылок не должно превращаться в утечку."""
    src = _MAIN.read_text(encoding="utf-8")
    tree = ast.parse(src)
    body = ""
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_on_bg_task_done":
            body = ast.get_source_segment(src, n) or ""
    assert "_BG_TASKS.discard(task)" in body, "завершённая задача обязана уходить из множества"
    assert "log.error" in body, "падение фоновой задачи обязано попадать в лог"
    assert "task.cancelled()" in body, (
        "остановка процесса — не сбой, отменённую задачу в ошибки писать нельзя"
    )


@pytest.mark.asyncio
async def test_supervisor_names_its_task():
    """Без имени в логе видно «Task-37», и непонятно, какой сервис отвалился."""
    from services import service_supervisor

    seen = {}

    async def _svc():
        task = asyncio.current_task()
        seen["name"] = task.get_name() if task else None

    await service_supervisor.supervise("scheduler", _svc, restart_on_return=False)
    assert seen["name"] == "svc:scheduler"
