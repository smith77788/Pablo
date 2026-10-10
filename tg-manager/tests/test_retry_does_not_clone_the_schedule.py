"""Кнопка «Повторить» повторяет работу, а не заводит второе расписание.

ЧТО ЛОМАЛОСЬ. Повтор операции копировал `params` целиком — вместе с
`repeat_interval_min`, ключом повторяемости. Для обычной операции это
безобидно, для автопостинга — нет: круг расписания и так продолжается сам
(неудачный круг его больше не обрывает, `op_worker._reschedule_recurring`), а
повтор запускал ВТОРУЮ цепочку с тем же интервалом. Каналы начинали получать
посты вдвое чаще, следующее нажатие — вчетверо, и остановить это можно было
только вручную, отменяя операции по одной.

Хуже всего массовый повтор «перезапустить все недоведённые»: он берёт до 25
операций за раз. У автопостинга с часовым интервалом за неделю набирается
больше сотни кругов, и одно нажатие могло размножить расписание в 25
параллельных цепочек. Для флота аккаунтов это прямой путь в бан — ровно та
частота публикаций, от которой оберегает весь пейсинг продукта.

Ещё один перенос того же рода: следующий круг расписания наследовал
`retry_of_op`, если текущий был повтором. По этой ссылке исполнитель читает
журнал предка, то есть считает уже опубликованные каналы сделанными и
пропускает их. Канал, однажды получивший пост, не получал больше НИ ОДНОГО, и
дырка в наполнении держалась бы вечно.

ЧТО ТЕПЕРЬ. Все пути повтора собирают params одной функцией
`operation_bus.params_for_retry`: она ставит ссылку на журнал предка и
выбрасывает ключи расписания. Смысл кнопки — «сделай эту работу ещё раз», а не
«заведи новое расписание»; живое расписание в повторе не нуждается, а
отменённое владелец возвращает там, где заводил.
"""
from __future__ import annotations

import ast
import inspect

import pytest


def _func_body(module, name: str) -> str:
    """Тело функции по границам AST, а не окном фиксированной длины.

    Срез «start:start+N» живёт до следующей правки кода; отрицательное
    утверждение о промахнувшемся окне зеленеет навсегда
    (tests/test_no_silently_disabled_guards.py).
    """
    src = inspect.getsource(module)
    tree = ast.parse(src)
    lines = src.split("\n")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"функция {name} не найдена в {module.__name__}")


# ── сам сборщик params ───────────────────────────────────────────────────────

def test_repeat_interval_does_not_survive_a_retry():
    from services import operation_bus

    out = operation_bus.params_for_retry(41, {"text": "пост", "repeat_interval_min": 60})

    assert "repeat_interval_min" not in out, (
        "повтор унёс интервал расписания — у владельца появилась вторая цепочка "
        "автопостинга, и каналы получают посты вдвое чаще"
    )


def test_repeat_count_does_not_survive_a_retry():
    from services import operation_bus

    out = operation_bus.params_for_retry(41, {"repeat_interval_min": 30, "repeat_count": 5})

    assert "repeat_count" not in out


def test_failure_streak_does_not_survive_a_retry():
    """Счётчик неудач подряд описывает ТУ цепочку, а не эту работу."""
    from services import operation_bus
    from services import op_worker

    out = operation_bus.params_for_retry(41, {op_worker._RECURRING_STREAK_KEY: 2})

    assert op_worker._RECURRING_STREAK_KEY not in out
    assert op_worker._RECURRING_STREAK_KEY in operation_bus.RECURRENCE_KEYS


def test_retry_keeps_everything_that_describes_the_work():
    from services import operation_bus

    out = operation_bus.params_for_retry(
        41,
        {"text": "пост", "channel_ids": [1, 2], "account_ids": [7],
         "delay_seconds": 30, "repeat_interval_min": 60},
    )

    assert out["text"] == "пост"
    assert out["channel_ids"] == [1, 2]
    assert out["account_ids"] == [7]
    assert out["delay_seconds"] == 30, "повтор обязан повторять ТУ ЖЕ операцию"


def test_retry_links_to_the_journal_of_the_original():
    from services import operation_bus

    out = operation_bus.params_for_retry(41, {})

    assert out["retry_of_op"] == 41, (
        "без ссылки на предка журнал у повтора пустой и вся уже сделанная "
        "работа будет сделана второй раз"
    )


def test_source_params_are_not_touched():
    """Вызывающий часто держит эти params для ответа владельцу."""
    from services import operation_bus

    src = {"repeat_interval_min": 60, "text": "пост"}
    operation_bus.params_for_retry(41, src)

    assert src == {"repeat_interval_min": 60, "text": "пост"}


def test_missing_params_are_not_a_crash():
    from services import operation_bus

    assert operation_bus.params_for_retry(41, None)["retry_of_op"] == 41


def test_a_schedule_is_reported_so_the_owner_can_be_told():
    from services import operation_bus

    assert operation_bus.drops_recurrence({"repeat_interval_min": 60}) is True
    assert operation_bus.drops_recurrence({"repeat_interval_min": 0}) is False
    assert operation_bus.drops_recurrence({}) is False
    assert operation_bus.drops_recurrence({"repeat_interval_min": "каждый час"}) is False


# ── точечный повтор упавших целей ────────────────────────────────────────────

class _RetryPool:
    def __init__(self, params: dict):
        self.params = params

    async def fetchrow(self, query, *args):
        return {"owner_id": 5, "op_type": "bulk_leave", "params": dict(self.params)}

    async def fetch(self, query, *args):
        return [{"target": "@chan_a", "status": "error"}]

    async def execute(self, query, *args):
        return "UPDATE 1"


@pytest.mark.asyncio
async def test_retry_of_failed_targets_does_not_clone_the_schedule(monkeypatch):
    from services import operation_bus

    seen: dict = {}

    async def _fake_submit(pool, owner_id, op_type, params, **kw):
        seen["params"] = params
        return 99

    monkeypatch.setattr(operation_bus, "submit", _fake_submit)

    res = await operation_bus.submit_retry_failed(
        _RetryPool({"channels": ["@chan_a", "@chan_b"], "repeat_interval_min": 45}), 5, 41)

    assert res["ok"] is True
    assert seen["params"]["channels"] == ["@chan_a"], "повтор пошёл не по упавшим целям"
    assert seen["params"]["retry_of_op"] == 41
    assert "repeat_interval_min" not in seen["params"], (
        "точечный повтор унёс расписание исходной операции"
    )


# ── следующий круг расписания ────────────────────────────────────────────────

class _RoundPool:
    async def fetchrow(self, query, *args):
        return {"label": "Пост ↻", "total_items": 3}

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        return "UPDATE 1"


@pytest.mark.asyncio
async def test_next_round_does_not_inherit_a_foreign_journal(monkeypatch):
    from services import op_worker
    from services import operation_bus

    seen: dict = {}

    async def _fake_submit(pool, owner_id, op_type, params, **kw):
        seen["params"] = params
        return 100

    monkeypatch.setattr(operation_bus, "submit", _fake_submit)

    await op_worker._reschedule_recurring(
        _RoundPool(), None, 41, 5, "mass_publish",
        {"text": "пост", "repeat_interval_min": 60, "retry_of_op": 40},
        productive=True,
    )

    assert "params" in seen, "следующий круг расписания не поставлен"
    assert "retry_of_op" not in seen["params"], (
        "круг унаследовал журнал ЧУЖОЙ операции — каналы, однажды получившие "
        "пост, считаются сделанными и больше не получат ни одного"
    )
    assert seen["params"]["repeat_interval_min"] == 60, "расписание само себя потеряло"


# ── все пути повтора идут через один сборщик ────────────────────────────────

def test_single_retry_in_the_mini_app_uses_the_shared_builder():
    from services import mini_app_api

    body = _func_body(mini_app_api, "retry_operation")

    assert body.count("params_for_retry(") == 2, (
        "повтор в мини-аппе собирает params сам: оба пути (точечный повтор "
        "публикации и повтор целиком) обязаны идти через "
        "operation_bus.params_for_retry, иначе снова унесут расписание или "
        "потеряют ссылку на журнал предка"
    )


def test_bulk_retry_in_the_mini_app_uses_the_shared_builder():
    from services import mini_app_api

    body = _func_body(mini_app_api, "operations_retry_failed")

    assert "params_for_retry(" in body, (
        "массовый повтор берёт до 25 операций за раз — собирая params сам, он "
        "размножает расписание автопостинга в 25 цепочек и заново делает всю "
        "уже сделанную работу"
    )


def test_retry_of_failed_targets_uses_the_shared_builder():
    from services import operation_bus

    body = _func_body(operation_bus, "submit_retry_failed")

    assert "params_for_retry(" in body
