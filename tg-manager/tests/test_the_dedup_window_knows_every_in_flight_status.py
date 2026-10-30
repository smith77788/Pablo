"""Дедуп постановки не держит рукописный список статусов.

ЧЕМ ОПАСЕН РУКОПИСНЫЙ СПИСОК ИМЕННО ЗДЕСЬ. `operation_bus.submit` — та самая
единственная дверь, через которую операция попадает в очередь, и окно
идемпотентности в ней существует ради одного: двойной тап кнопки (или повтор
клиента после таймаута) не должен давать ДВЕ операции. Чтобы узнать «такая уже
есть», дедуп спрашивает очередь по статусам. Список статусов лежал в запросе
строками — две копии, обе набраны руками.

Эта мина в продукте уже срабатывала, только в другом месте: копии списков
статусов в `bot/handlers` не знали про 'partial', 'paused' и 'waiting_approval',
появившиеся позже. Там ценой был неверный экран. Здесь цена другая: не узнает
дедуп про новый «в полёте» статус — двойной тап пройдёт насквозь, и операция
исполнится дважды. На инвайте это двойной расход аккаунтов и удвоенный риск
бана.

ЧТО ЗАКРЕПЛЕНО. Набор статусов дедупа выводится из единственного источника
(`services/op_status.IN_FLIGHT` плюс `DONE`), а не переписывается в SQL. Проверка
смотрит на оба: что рукописного списка в запросе нет и что вывод живой —
добавленный в IN_FLIGHT статус появляется в наборе сам.

ПОЧЕМУ `done` ВНУТРИ, А `partial` СНАРУЖИ. Окно закрывает гонку «первый запрос
уже успел завершиться успешно»: повторный тап в эти секунды значит «я не увидел
ответа». Операция, которая НЕ доехала (partial/failed/cancelled), повторного
запуска владельцу не запрещает — иначе владелец не смог бы дозапустить то, что
оборвалось.
"""

import ast
import pathlib
import re

from services import op_status
from services import operation_bus

BUS = pathlib.Path(operation_bus.__file__)
# Список литералов сразу за `status IN (`: ровно то, чего здесь быть не должно.
HANDWRITTEN = re.compile(r"status\s+IN\s*\(\s*(?:--[^\n]*\n\s*)?'")


def _sql_strings_of(name: str) -> list[str]:
    """Строковые константы внутри функции (включая f-строки)."""
    tree = ast.parse(BUS.read_text(encoding="utf-8"))
    out: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name != name:
            continue
        for n in ast.walk(fn):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                out.append(n.value)
        return out
    raise AssertionError(f"{name} не найдена в {BUS.name} — проверку перечитать")


def test_the_dedup_query_has_no_handwritten_status_list():
    bad = [s for s in _sql_strings_of("submit") if HANDWRITTEN.search(s)]
    assert not bad, (
        "в постановке операции статусы дедупа набраны руками: "
        f"{[s.strip()[:80] for s in bad]}. Такой список отстаёт от "
        "services/op_status.py молча, а цена отставания здесь — двойное "
        "исполнение операции по двойному тапу")


def test_the_derivation_is_alive_not_a_copy():
    """Новый «в полёте» статус попадает в набор сам, без правки SQL."""
    original = op_status.IN_FLIGHT
    try:
        op_status.IN_FLIGHT = frozenset(original | {"ждёт_чего_то_нового"})
        got = operation_bus._dedup_status_sql()
    finally:
        op_status.IN_FLIGHT = original
    assert "ждёт_чего_то_нового" in got, (
        "набор статусов дедупа не следует за op_status.IN_FLIGHT — значит "
        "это копия, а не вывод")


def test_todays_set_is_unchanged():
    """Правка ничего не поменяла в поведении: набор тот же, что был в SQL."""
    got = operation_bus._dedup_status_sql()
    assert got == (
        "('done','paused','pending','running','scheduled','waiting_approval')"
    ), got


def test_a_finished_but_incomplete_operation_can_be_relaunched():
    """Недоехавшие исходы в окно не входят — иначе не дозапустить оборванное."""
    got = operation_bus._dedup_status_sql()
    for word in (op_status.PARTIAL, op_status.FAILED, op_status.CANCELLED):
        assert f"'{word}'" not in got, (
            f"исход {word!r} попал в окно дедупа: повторный запуск оборванной "
            "операции владелец получит как «уже есть такая»")


def test_the_census_bites():
    """Перепись обязана ловить рукописный список, если он вернётся."""
    assert HANDWRITTEN.search(
        "SELECT 1 FROM q WHERE status IN (\n  'pending', 'running'\n)")
    assert not HANDWRITTEN.search("SELECT 1 FROM q WHERE status IN ('a','b')".replace(
        "status IN ('a','b')", "status IN {_dedup_statuses}"))
