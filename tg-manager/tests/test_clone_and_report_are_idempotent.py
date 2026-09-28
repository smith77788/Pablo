"""Клонирование контента и репортинг не повторяют сделанное на втором прогоне.

ЧТО БЫЛО. Два исполнителя шли по списку целей и не вели журнала целей вообще.

`content_clone` пересылает или копирует сообщения из канала-источника в список
каналов-целей. Повтор начинал список сначала и клонировал содержимое ВТОРОЙ раз
в каналы, которые его уже получили. Повтор здесь не редкость, а штатный ход:
поймав FloodWait, исполнитель сам возвращает операцию в очередь с задержкой —
значит на любой длинной серии половина каналов гарантированно получала дубль
постов.

`report_peer` жалуется на одну цель с нескольких аккаунтов. Повтор заставлял
УЖЕ отрепортивший аккаунт жаловаться второй раз. Охвата это не добавляет
(Telegram считает повторную жалобу с того же аккаунта тем же голосом), зато это
ровно то действие, за которое аккаунт получает ограничения, — а репорт и так
самое рискованное действие в списке.

ЧТО ТЕПЕРЬ. Оба ведут журнал по целям и пропускают уже отработанные, считая их
успехом (счётчик не должен проседать из-за того, что работу сделал прошлый
прогон) и называя число пропущенных в сводке — иначе повтор выглядит мгновенным
успехом, и непонятно, ушло что-то заново или нет.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "services" / "op_worker.py"
_TEXT = _SRC.read_text(encoding="utf-8")
_TREE = ast.parse(_TEXT)


def _body(name: str) -> str:
    fn = next(
        (n for n in ast.walk(_TREE)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == name), None)
    assert fn is not None, f"исполнитель {name} не найден"
    end = getattr(fn, "end_lineno", fn.lineno)
    return "\n".join(_TEXT.split("\n")[fn.lineno - 1:end])


@pytest.mark.parametrize("executor", ["_exec_content_clone", "_exec_report_peer"])
def test_executor_reads_the_journal_before_acting(executor):
    body = _body(executor)
    assert "completed_targets(pool, op_id)" in body, (
        f"{executor} не смотрит, что уже сделано: повтор переделает всё заново"
    )


@pytest.mark.parametrize("executor", ["_exec_content_clone", "_exec_report_peer"])
def test_executor_writes_the_journal_for_both_outcomes(executor):
    body = _body(executor)
    assert "INSERT INTO operation_log" in body, (
        f"{executor} ничего не пишет в журнал — читать на повторе будет нечего"
    )
    assert '"ok" if ok else "error"' in body, (
        f"{executor} пишет журнал только для одного исхода: либо неудачные цели "
        f"попадут в пропуск, либо успешные не попадут"
    )


@pytest.mark.parametrize("executor", ["_exec_content_clone", "_exec_report_peer"])
def test_skipped_target_still_moves_the_progress_counter(executor):
    """Пропуск — это сделанная работа: без инкремента прогресс врёт вниз."""
    body = _body(executor)
    # Якорь — сама ветка пропуска (`... in _already:`), а не строка лога выше.
    i = body.index("in _already:")
    window = body[i:i + 700]
    assert "done_items=done_items+1" in window, (
        f"{executor}: пропущенная цель не двигает счётчик — операция на 380 целей "
        f"покажет 177 из 380 при полностью сделанной работе"
    )
    assert "continue" in window, f"{executor}: после пропуска работа всё равно делается"


@pytest.mark.parametrize("executor", ["_exec_content_clone", "_exec_report_peer"])
def test_skipped_targets_are_named_in_the_summary(executor):
    """Мгновенный «успех» без объяснения владелец прочитает как «ничего не ушло»."""
    body = _body(executor)
    assert "skip_count" in body
    assert "в прошлый раз" in body, (
        f"{executor}: сводка не говорит, что часть целей уже была обработана"
    )


def test_clone_skips_by_target_and_report_skips_by_account():
    """Ключ журнала должен быть тем, что повторять нельзя."""
    clone = _body("_exec_content_clone")
    assert "str(target_ref) in _already" in clone, (
        "клонирование пропускает не по каналу-цели — ключ обязан быть каналом"
    )
    report = _body("_exec_report_peer")
    assert '_key = str(acc["id"])' in report and "_key in _already" in report, (
        "репортинг пропускает не по аккаунту — цель у операции одна, "
        "повторять нельзя именно голос конкретного аккаунта"
    )
