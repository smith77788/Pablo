"""Причина, по которой операция не идёт, видна на обеих поверхностях.

ЧТО ЛОМАЛОСЬ. У operation_queue ДВЕ колонки причины, и пишут в них разные пути:

  * `error_msg` — терминальный провал: исполнитель вернул отказ, тип операции
    неизвестен, сторож признал операцию ядовитой;
  * `last_error` — почему операция ЖДЁТ: отложена флуд-паузой Telegram,
    поставлена на повторную попытку после ошибки, возвращена в очередь при
    рестарте воркера.

Читатели разошлись: мини-апп брал только `error_msg`, бот — только
`last_error`. Каждая поверхность была слепа ровно на половину причин, причём на
разную половину. В мини-аппе операция, отложенная Telegram на час, выглядела
как «ожидает» без единого слова объяснения — а код флуд-паузы прямо пишет, что
молчание здесь обходится дороже сообщения: владелец начинает отменять и
запускать заново, добирая новых ограничений. В боте зеркально: у провалившейся
операции причина лежала в `error_msg`, и экран деталей не показывал её вообще.

ЧТО ТЕПЕРЬ. Выражение причины одно на всех — `op_status.sql_error_reason()`.
Порядок: терминальная причина, затем причина ожидания, затем `reason` из result
(старые «мягкие» провалы писали его туда, а не в колонку).
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_the_expression_covers_both_columns():
    from services import op_status

    sql = op_status.sql_error_reason()

    assert "error_msg" in sql and "last_error" in sql, (
        "выражение причины смотрит только в одну колонку — половина причин "
        "останется невидимой"
    )
    assert sql.index("error_msg") < sql.index("last_error"), (
        "терминальная причина должна идти первой: у завершённой операции "
        "причина ожидания уже не актуальна"
    )
    assert "result->>'reason'" in sql, "потерян запасной источник причины"


def test_an_empty_string_is_not_a_reason():
    """Пустая строка в колонке — не причина, иначе экран показал бы пустоту."""
    from services import op_status

    assert op_status.sql_error_reason().count("NULLIF") == 2


def test_the_alias_is_applied_to_every_column():
    from services import op_status

    sql = op_status.sql_error_reason("oq.")

    assert "oq.error_msg" in sql
    assert "oq.last_error" in sql
    assert "oq.result" in sql


@pytest.mark.parametrize(
    "rel,name",
    [
        ("services/mini_app_api.py", "operations"),
        ("services/mini_app_api.py", "operation_status"),
    ],
)
def test_mini_app_readers_use_the_shared_expression(rel, name):
    src = (ROOT / rel).read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.split("\n")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            body = "\n".join(lines[node.lineno - 1:node.end_lineno])
            assert "sql_error_reason(" in body, (
                f"{name} собирает причину сам — так и разошлись бот с мини-аппом"
            )
            return
    pytest.fail(f"функция {name} не найдена в {rel}")


def test_bot_detail_screen_uses_the_shared_expression():
    src = (ROOT / "bot/handlers/mass_ops.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.split("\n")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "cb_op_detail":
            body = "\n".join(lines[node.lineno - 1:node.end_lineno])
            assert "sql_error_reason(" in body, (
                "экран деталей в боте читает одну колонку — у упавшей операции "
                "он не покажет причину"
            )
            return
    pytest.fail("cb_op_detail не найден")


def test_no_reader_selects_a_single_reason_column():
    """Никто не выбирает одну колонку причины из очереди в обход выражения."""
    offenders = []
    for base in ("bot", "services"):
        for path in sorted((ROOT / base).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel == "services/op_status.py":
                continue
            src = path.read_text(encoding="utf-8")
            for i, line in enumerate(src.split("\n"), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # Интересует только ЧТЕНИЕ: в UPDATE колонки пишут по одной,
                # и это правильно — пути ожидания и провала разные.
                if "SELECT" not in line.upper() and "AS error_msg" not in line:
                    continue
                # Таблица ищется ВПЕРЁД от строки выборки (FROM идёт после
                # SELECT): окно в обе стороны цепляло соседнюю функцию, и
                # last_error чужой таблицы (va_channel_admin) считался причиной
                # операции.
                at = src.find(line)
                if "operation_queue" not in src[at:at + 600]:
                    continue
                has_msg = "error_msg" in line and "AS error_msg" not in line
                has_last = "last_error" in line
                if (has_msg or has_last) and "sql_error_reason" not in line:
                    offenders.append(f"{rel}:{i}: {stripped[:90]}")
    assert not offenders, (
        "причина операции читается в обход op_status.sql_error_reason — "
        "поверхность снова ослепнет на половину причин:\n  " + "\n  ".join(offenders)
    )


def test_helper_is_documented_where_it_lives():
    from services import op_status

    doc = inspect.getdoc(op_status.sql_error_reason) or ""
    assert "last_error" in doc and "error_msg" in doc
