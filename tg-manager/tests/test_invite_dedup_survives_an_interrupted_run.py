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


# ── Вторая половина того же класса: запись НЕ УДАЛАСЬ ────────────────────────
#
# Писать по ходу прогона мало: сама запись может не пройти (занятый пул,
# блокировка на DDL, обрыв соединения). Такая потеря выглядела как успех —
# функция возвращала None и глотала исключение в debug-лог. Внутри прогона это
# не видно (цели держит набор в памяти), а СЛЕДУЮЩИЙ прогон приглашал тех же
# людей второй раз, и узнать об этом было негде: ни в метриках, ни в ошибке
# операции. Для самой баноопасной операции продукта это прямой путь к PeerFlood.


class _FlakyPool(_Pool):
    """Пул, у которого первые `fail_first` записей дедупа не проходят."""

    def __init__(self, fail_first=1):
        super().__init__()
        self.fail_left = fail_first
        self.attempts = 0

    async def execute(self, query, *args):
        if "INSERT INTO invite_target_log" in query:
            self.attempts += 1
            if self.fail_left > 0:
                self.fail_left -= 1
                raise RuntimeError("пул соединений занят")
        return await super().execute(query, *args)


def test_a_transient_failure_is_retried_on_the_spot():
    """Типовая причина мгновенная — одна повторная попытка её закрывает."""
    pool = _FlakyPool(fail_first=1)
    ok = asyncio.run(
        op_worker._record_invited_targets(pool, 555, "chat:1", 7, ["a", "b"]))
    assert ok is True, "повторная попытка записи не сделана"
    assert pool.stored == {(555, "chat:1", "a"), (555, "chat:1", "b")}
    assert pool.attempts == 2


def test_a_lost_record_is_reported_not_swallowed():
    """Потеря дедупа обязана быть видна снаружи, а не только в логе."""
    from services import metrics

    metrics.reset()
    pool = _FlakyPool(fail_first=99)
    ok = asyncio.run(
        op_worker._record_invited_targets(pool, 555, "chat:1", 7, ["a"]))
    assert ok is False, (
        "потерянная запись дедупа возвращает «всё хорошо»: вызывающий не узнает, "
        "что следующий прогон пригласит тех же людей повторно")
    counters = metrics.snapshot().get("counters") or {}
    assert any("infragram_invite_dedup_write_failures_total" in str(k)
               for k in counters), (
        f"потеря дедупа не попала в метрики: снаружи она невидима — {counters}")
    assert ("infragram_invite_dedup_write_failures_total"
            in metrics._HELP), "у метрики нет описания — она не попадёт в выдачу"


def test_nothing_to_write_is_not_a_failure():
    """Пустая пачка — не потеря: вызывающему нечего переносить."""
    assert asyncio.run(
        op_worker._record_invited_targets(_Pool(), 555, "g", 1, [])) is True


def test_unconfirmed_targets_are_carried_to_the_next_batch():
    """Храповик: неподтверждённая пачка переносится в следующую запись.

    Сама запись идемпотентна, пачки идут минутами друг от друга — повторить
    предыдущую пачку вместе с новой ничего не стоит, зато неудача одной записи
    перестаёт быть безвозвратной потерей.
    """
    helper = _func("_remember_invited", within="_exec_mass_invite")
    flat = " ".join(helper.split())
    assert "_dedup_unconfirmed" in flat, (
        "неудачная запись пачки по-прежнему теряется безвозвратно: следующая "
        "запись о ней не знает")
    assert "_record_invited_targets(\n" in helper or "_record_invited_targets(" in flat
    assert "_dedup_unconfirmed)" in flat or "_dedup_unconfirmed," in flat, (
        "в дедуп пишется только новая пачка, а не вместе с неподтверждёнными")
    assert "if await _record_invited_targets(" in flat, (
        "результат записи не проверяется — перенос не состоится")
    assert "_dedup_unconfirmed.clear()" in flat, (
        "подтверждённые цели не снимаются с переноса: пачка будет расти весь "
        "прогон")
