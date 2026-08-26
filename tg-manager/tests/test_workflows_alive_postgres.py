"""Раздел «Воркфлоу» обязан существовать в БД и работать целиком.

ЧТО БЫЛО СЛОМАНО. Экран мини-аппа и восемь маршрутов API работают с таблицами
`workflow_definitions` и `workflow_runs`. Заводила их только функция
`init_workflow_tables`, которую НИКТО не вызывает, и ни одна миграция их не
создавала. После полного накатывания схемы таблиц в базе не было:
  • список — вечно пустой (ошибка запроса проглатывалась);
  • создание — 500 «relation ... does not exist»;
  • пауза/возобновление/удаление — 500 «cannot import name», потому что
    `pause_workflow`, `resume_workflow` и `delete_workflow` не были написаны
    вовсе, хотя маршруты на них зарегистрированы.
Снаружи раздел выглядел работающим и не работал ни в одной своей части.

Проверяется на НАСТОЯЩЕЙ базе: ровно эта поломка — отсутствие таблиц — на
заглушке пула невидима в принципе. Запуск: INFRAGRAM_TEST_DSN=... pytest ...
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 555777
OTHER = 555778

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _mk():
        setup = await asyncpg.connect(DSN)
        try:
            await setup.execute("CREATE SCHEMA IF NOT EXISTS wftest")
        finally:
            await setup.close()
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4,
                                      server_settings={"search_path": "wftest"})
        # Ровно та миграция, что уезжает в прод. Комментарии стоят ПЕРЕД первым
        # ';', поэтому чанк целиком отбрасывать нельзя — чистим построчно.
        with open(os.path.join(ROOT, "schema_v183.sql"), encoding="utf-8") as f:
            for chunk in f.read().split(";"):
                body = "\n".join(ln for ln in chunk.split("\n")
                                 if not ln.strip().startswith("--")).strip()
                if body:
                    await p.execute(body)
        return p

    p = _run(_mk())
    yield p
    _run(p.execute("DROP TABLE IF EXISTS workflow_runs"))
    _run(p.execute("DROP TABLE IF EXISTS workflow_definitions"))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(pool.execute("DELETE FROM workflow_runs"))
    _run(pool.execute("DELETE FROM workflow_definitions"))
    yield


def _mk(pool, name="wf", steps=None, owner=OWNER):
    from services import workflow_engine as we
    res = _run(we.create_workflow(pool, owner, name, "описание", steps or [{"action": "a"}]))
    assert res.get("ok"), res
    return int(res["id"])


# ── таблицы вообще есть ──────────────────────────────────────────────────────

def test_migration_creates_the_tables_the_screen_needs():
    """Миграция обязана заводить обе таблицы: без них раздел мёртв целиком."""
    sql = open(os.path.join(ROOT, "schema_v183.sql"), encoding="utf-8").read()
    assert "CREATE TABLE IF NOT EXISTS workflow_definitions" in sql
    assert "CREATE TABLE IF NOT EXISTS workflow_runs" in sql


def test_list_and_create_work_on_real_schema(pool):
    from services import workflow_engine as we

    wid = _mk(pool, "мой воркфлоу")
    rows = _run(we.get_workflows(pool, OWNER))
    assert [r["id"] for r in rows] == [wid], (
        "созданный воркфлоу не виден в списке — экран останется пустым")
    assert rows[0]["name"] == "мой воркфлоу"


# ── три отсутствовавшие функции ──────────────────────────────────────────────

def test_pause_and_resume_flip_the_same_flag_as_the_screen(pool):
    """Пауза — это is_active, ровно как в PATCH /workflows/{id}.

    Два способа нажать одну кнопку не должны означать разное.
    """
    from services import workflow_engine as we

    wid = _mk(pool)
    _run(we.pause_workflow(pool, OWNER, wid))
    assert _run(pool.fetchval(
        "SELECT is_active FROM workflow_definitions WHERE id=$1", wid)) is False

    _run(we.resume_workflow(pool, OWNER, wid))
    assert _run(pool.fetchval(
        "SELECT is_active FROM workflow_definitions WHERE id=$1", wid)) is True


def test_pause_of_someone_elses_workflow_is_not_found(pool):
    """Чужой воркфлоу — «не найден», а не «не разрешено».

    Разный ответ на «нет такого» и «есть, но чужой» позволяет перебором узнать
    чужие id.
    """
    from services import workflow_engine as we

    wid = _mk(pool, owner=OTHER)
    with pytest.raises(LookupError):
        _run(we.pause_workflow(pool, OWNER, wid))
    with pytest.raises(LookupError):
        _run(we.pause_workflow(pool, OWNER, 10 ** 8))     # несуществующий
    assert _run(pool.fetchval(
        "SELECT is_active FROM workflow_definitions WHERE id=$1", wid)) is True


def test_delete_removes_workflow_and_reports_missing(pool):
    from services import workflow_engine as we

    wid = _mk(pool)
    assert _run(we.delete_workflow(pool, OWNER, wid)) is True
    assert _run(pool.fetchval(
        "SELECT count(*) FROM workflow_definitions WHERE id=$1", wid)) == 0
    assert _run(we.delete_workflow(pool, OWNER, wid)) is False, (
        "повторное удаление обязано честно сказать «нет такого» (обработчик даст 404)")


def test_delete_does_not_break_on_run_history(pool):
    """Удаление воркфлоу с историей прогонов не должно падать на внешнем ключе.

    И «выполняющийся» прогон удалённого воркфлоу висел бы вечно — отменяем.
    """
    from services import workflow_engine as we

    wid = _mk(pool)
    run = _run(we.execute_workflow(pool, OWNER, wid))
    assert run.get("ok"), run

    assert _run(we.delete_workflow(pool, OWNER, wid)) is True
    st = _run(pool.fetchrow(
        "SELECT status FROM workflow_runs WHERE id=$1", int(run["run_id"])))
    assert st["status"] == "cancelled", (
        f"прогон остался в статусе {st['status']} у несуществующего воркфлоу")


def test_delete_of_someone_elses_workflow_does_nothing(pool):
    from services import workflow_engine as we

    wid = _mk(pool, owner=OTHER)
    assert _run(we.delete_workflow(pool, OWNER, wid)) is False
    assert _run(pool.fetchval(
        "SELECT count(*) FROM workflow_definitions WHERE id=$1", wid)) == 1


# ── шаги не должны теряться ──────────────────────────────────────────────────

def test_steps_survive_creation_and_are_counted(pool):
    """Шаги обязаны сохраниться, а total_steps — считать шаги, а не символы.

    В обработчике `steps` уходил ПОЗИЦИОННО и попадал в description: шаги,
    которые только что провалидировали, молча терялись. А `total_steps`
    считался как len() от jsonb, который asyncpg отдаёт строкой, — прогресс на
    экране показывал десятки шагов вместо двух.
    """
    from services import workflow_engine as we

    steps = [{"action": "parse"}, {"action": "invite"}]
    wid = _mk(pool, "с шагами", steps=steps)

    saved = _run(pool.fetchval(
        "SELECT steps FROM workflow_definitions WHERE id=$1", wid))
    if isinstance(saved, str):
        saved = json.loads(saved)
    assert saved == steps, f"шаги не сохранились: {saved!r}"

    run = _run(we.execute_workflow(pool, OWNER, wid))
    assert run["total_steps"] == 2, (
        f"total_steps={run['total_steps']} — считаются символы, а не шаги")


def test_handler_passes_steps_by_keyword():
    """Храповик на ту самую позиционную ошибку в обработчике."""
    import ast

    src = open(os.path.join(ROOT, "services", "mini_app_api.py"),
               encoding="utf-8").read()
    lines = src.split("\n")
    body = None
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "workflow_create":
            body = "\n".join(lines[node.lineno - 1:node.end_lineno])
    assert body, "обработчик workflow_create не найден"
    assert "steps=steps" in body, (
        "steps снова уходит позиционно — попадёт в description, и шаги потеряются")
