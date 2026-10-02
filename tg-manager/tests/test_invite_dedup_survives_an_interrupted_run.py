"""Прерванный инвайт не приглашает тех же людей второй раз.

ЧТО БЫЛО. Дедуп инвайта («кого уже приглашали в эту группу») живёт в таблице
`invite_target_log` по ключу (owner_id, group_key) — именно так, чтобы работал и
для НОВОЙ операции-повтора, а не только для той же строки очереди.

Но исполнитель копил обработанные цели в памяти процесса (`invited_this_run`)
весь прогон — а прогон инвайта идёт часами: это пейсинг против банов. Запись
шла ОДНИМ запросом в самом конце. Значит любой обрыв до финала терял дедуп
ЦЕЛИКОМ:

  * рестарт воркера или сброс контейнера;
  * потолок прогона (6 часов) и сторож застоя;
  * длинная флуд-пауза — операция уходит в очередь из середины прогона;
  * отмена владельцем;
  * падение исполнителя исключением.

Следующая попытка читала пустой дедуп и приглашала тех же людей ВТОРОЙ раз. Для
самой баноопасной операции продукта это худший исход, а обещание повтора
«взятые цели сохранены — повтор продолжит с того же места» было неправдой.

ФИКС: цели пишутся в дедуп по ходу прогона, пачка за пачкой
(`_remember_invited`). Запись идемпотентна (ON CONFLICT DO NOTHING), пачки идут
минутами друг от друга — цена незаметна.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from services import op_worker

ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


def _func(name: str, within: str | None = None) -> str:
    """Исходник функции по границам AST (вложенной — внутри указанной)."""
    tree = ast.parse(_SRC if within is None else _func(within))
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            seg = ast.get_source_segment(_SRC if within is None else _func(within), node)
            if seg:
                return seg
    raise AssertionError(f"нет функции {name}"
                         + (f" внутри {within}" if within else ""))


class _Pool:
    """Пул, повторяющий ON CONFLICT DO NOTHING по (owner_id, group_key, target)."""

    def __init__(self):
        self.stored: set[tuple] = set()
        self.queries: list[str] = []

    async def execute(self, query, *args):
        self.queries.append(query)
        if "INSERT INTO invite_target_log" not in query:
            return "OK"
        owner_id, group_key, targets = args[0], args[1], args[2]
        before = len(self.stored)
        for t in targets:
            self.stored.add((owner_id, group_key, str(t)))
        return f"INSERT 0 {len(self.stored) - before}"


def test_recording_is_idempotent_and_one_query_per_batch():
    """Запись по ходу прогона обязана быть дешёвой и повторяемой."""
    pool = _Pool()
    asyncio.run(op_worker._record_invited_targets(pool, 555, "chat:1", 7, ["a", "b"]))
    asyncio.run(op_worker._record_invited_targets(pool, 555, "chat:1", 7, ["b", "c"]))

    assert pool.stored == {(555, "chat:1", x) for x in ("a", "b", "c")}
    inserts = [q for q in pool.queries if "INSERT INTO invite_target_log" in q]
    assert len(inserts) == 2, (
        "на пачку должен уходить ОДИН запрос, иначе прогон на 2000 целей даст "
        f"2000 round-trip'ов: {len(inserts)}")
    assert "ON CONFLICT DO NOTHING" in inserts[0], (
        "повторная запись той же цели обязана быть безвредной: пачки "
        "пересекаются после возврата операции в очередь")


def test_recording_never_breaks_the_operation():
    """Дедуп — best-effort: сбой записи не должен ронять инвайт."""

    class _Broken:
        async def execute(self, query, *args):
            raise RuntimeError("БД недоступна")

    asyncio.run(op_worker._record_invited_targets(_Broken(), 555, "g", 1, ["a"]))


def test_dedup_is_written_during_the_run_not_at_the_end():
    """Храповик: набор целей не пополняется без записи в дедуп.

    Границы — тело исполнителя и тело вложенного помощника (AST), а не окно
    фиксированной длины: сдвинулся бы код, и проверка замолчала бы сама.
    """
    body = _func("_exec_mass_invite")
    helper = _func("_remember_invited", within="_exec_mass_invite")

    assert "_record_invited_targets(" in helper, (
        "помощник пополняет набор в памяти, но не пишет в дедуп — обрыв прогона "
        "снова потеряет всё")

    outside = body.replace(helper, "")
    assert "invited_this_run.update(" not in outside, (
        "цели добавляются в набор мимо _remember_invited: эти цели не попадут в "
        "дедуп до самого конца прогона, а прогон инвайта идёт часами")
    assert outside.count("await _remember_invited(") >= 3, (
        "не все пути инвайта (пачка, промоут-трюк, ссылка в ЛС) пишут дедуп "
        "по ходу прогона")


def test_dedup_key_is_not_the_operation_id():
    """Дедуп обязан работать для НОВОЙ операции-повтора, а не только для той же.

    Журнал операции (`operation_log`) привязан к номеру операции, поэтому
    повтор, поставленный шиной как новая операция, по нему бы ничего не пропустил.
    """
    loader = _func("_load_invited_targets")
    assert "owner_id=$1 AND group_key=$2" in loader, (
        "дедуп инвайта перестал быть сквозным по (owner_id, group_key)")
    assert "op_id" not in loader.split('"""')[-1], (
        "в выборке дедупа появился номер операции — повтор снова будет "
        "приглашать уже приглашённых")
