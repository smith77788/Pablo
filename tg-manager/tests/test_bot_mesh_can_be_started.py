"""Цепочку между ботами можно запустить, а не только смотреть на пустой экран.

Экран «🔗 Задачи между ботами» владелец прислал как пример: он показывал
«Задач между ботами нет» и обещал, что они «появятся, когда боты начнут
передавать работу по маршруту». Появиться они не могли никогда.

Приёмная половина Bot Mesh написана целиком: `auto_responder._maybe_handle_mesh`
разбирает конверт, гасит петли уникальным индексом, двигает шаг и пересылает
дальше. А НАЧАТЬ цепочку было нечем: `bot_mesh.make_envelope` и
`bot_mesh.create_task` не вызывал никто — ни бот, ни мини-апп, ни воркер.
Таблица `bot_mesh_tasks` заполнялась только при входящем конверте, которого
без первого отправления не бывает.

Здесь проверяется: у движка появился вызывающий; маршрут скоупится по
владельцу; неудачную отправку не выдают за запущенную цепочку; на экране есть
чем запустить и чем прервать.
"""
from __future__ import annotations

import ast
import functools
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "mini_app", "index.html")
API = os.path.join(ROOT, "services", "mini_app_api.py")
MESH = os.path.join(ROOT, "services", "bot_mesh.py")


@functools.lru_cache(maxsize=1)
def _html() -> str:
    with open(HTML, encoding="utf-8") as f:
        return f.read()


@functools.lru_cache(maxsize=1)
def _api() -> str:
    with open(API, encoding="utf-8") as f:
        return f.read()


def _js_func(name: str) -> str:
    h = _html()
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", h)
    assert m, f"функция {name} в мини-аппе не найдена"
    depth = 0
    for j in range(m.end() - 1, len(h)):
        if h[j] == "{":
            depth += 1
        elif h[j] == "}":
            depth -= 1
            if depth == 0:
                return h[m.start():j + 1]
    raise AssertionError(f"не удалось найти конец функции {name}")


def _py_func(src: str, name: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"функция {name} не найдена")


# ─── движок ───────────────────────────────────────────────────────────────────


def test_the_engine_is_still_there():
    """Антивакуумность: если движок убрали, проверки ниже ничего не стерегут."""
    with open(MESH, encoding="utf-8") as f:
        src = f.read()
    for fn in ("make_envelope", "create_task", "encode_message", "drop_task"):
        assert f"def {fn}(" in src, f"в bot_mesh нет {fn}"


def test_someone_finally_starts_a_chain():
    """Главная дыра: у make_envelope/create_task не было ни одного вызывающего."""
    callers = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in {"tests", "__pycache__", ".git", "node_modules"}]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            if os.path.samefile(os.path.dirname(path), os.path.dirname(MESH)) and fn == "bot_mesh.py":
                continue
            with open(path, encoding="utf-8", errors="ignore") as f:
                body = f.read()
            if "make_envelope" in body and "create_task(" in body:
                callers.append(os.path.relpath(path, ROOT))
    assert callers, (
        "цепочку между ботами по-прежнему никто не начинает — "
        "экран Bot Mesh будет пустым всегда"
    )


# ─── маршрут ──────────────────────────────────────────────────────────────────


def test_route_is_registered():
    src = _api()
    assert 'add_post("/api/miniapp/mesh/task", mesh_task_create)' in src
    assert 'add_post("/api/miniapp/mesh/task/{task_id}/drop", mesh_task_drop)' in src


def test_every_bot_in_the_route_belongs_to_the_owner():
    """Иначе цепочку можно направить в чужого бота."""
    body = _py_func(_api(), "mesh_task_create")
    assert "_get_uid(request)" in body and "401" in body
    assert "added_by=$1" in body, "боты маршрута не скоупятся по владельцу"
    assert "is_active=TRUE" in body
    assert "не ваши" in body, "чужой бот в маршруте проходит молча"


def test_bot_ids_are_not_cut_at_two_billion():
    """bot_id — BIGINT: дефолтный потолок validate_integer (2**31-1) режет
    современные id ботов, и свой же бот выглядел бы чужим."""
    body = _py_func(_api(), "mesh_task_create")
    assert body.count("max_val=2**63 - 1") >= 2, "id бота обрезается по 2**31"


def test_failed_send_is_not_sold_as_a_running_chain():
    """Передача bot→bot требует включённого режима у обоих. Если Telegram
    отказал, задача не должна висеть «выполняется» без единого шага."""
    body = _py_func(_api(), "mesh_task_create")
    assert "drop_task" in body, "неудачная отправка оставляет задачу запущенной"
    assert '"sent": False' in body
    assert "@BotFather" in body, "владельцу не сказано, что включить"


def test_stop_is_owner_scoped_and_not_a_no_op():
    body = _py_func(_api(), "mesh_task_drop")
    assert "owner_id=$2" in body, "прервать можно чужую цепочку"
    assert "drop_task" in body


# ─── экран ────────────────────────────────────────────────────────────────────


def test_screen_has_a_way_to_start():
    h = _html()
    assert 'onclick="meshNewTask()"' in h, "запустить цепочку с экрана нечем"
    body = _js_func("openBotMesh")
    assert "meshNewTask()" in body, "в пустом состоянии нет кнопки запуска"


def test_empty_state_no_longer_promises_the_impossible():
    """Старый текст обещал, что задачи появятся сами. Без запуска — не появятся."""
    body = _js_func("openBotMesh")
    assert "Появятся, когда боты начнут передавать работу" not in body
    assert "Запустите первую" in body


def test_builder_asks_for_origin_route_and_text():
    body = _js_func("meshNewTask")
    assert "askChoice" in body
    assert "/api/miniapp/mesh/task" in body
    assert "route:" in body and "origin_bot:" in body
    assert "@BotFather" in body, "владельца не предупредили про режим bot-to-bot"
    assert "два подключённых бота" in body, "цепочка из одного бота не бывает"


def test_failed_send_is_visible_to_the_owner():
    body = _js_func("meshNewTask")
    assert "r.sent === false" in body, "отказ Telegram показывается как успех"


def test_running_chain_can_be_stopped():
    body = _js_func("openBotMeshTask")
    assert "botmeshStop" in body and "meshStopTask()" in body
    assert "d.status === 'running'" in body, "кнопка «Прервать» висит и на завершённых"
    stop = _js_func("meshStopTask")
    assert "/drop" in stop and "askConfirm" in stop


def test_new_drop_reasons_are_translated():
    """Свои же причины не должны вылезти на экран латиницей."""
    m = re.search(r"const MESH_DROP = \{(.*?)\};", _html(), re.S)
    assert m
    for reason in ("send_failed", "stopped_by_owner"):
        assert reason in m.group(1), f"причина {reason} без перевода"
