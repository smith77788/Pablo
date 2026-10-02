"""Четвёртый путь закрытия операции не врёт и не исчезает из статистики.

ЧТО БЫЛО. Зависшую операцию, исчерпавшую повторы, закрывал
`recovery_engine._queue_recovery` — четвёртый путь закрытия в продукте (после
двух сторожей `op_worker` и голодания флота). В отличие от остальных трёх он:

  * писал `status='failed'` ВСЛЕПУЮ: операция взяла 203 цели из 380, зависла —
    владелец всё равно читал «❌ Не запустилась»;
  * не ходил через единственную дверь `_announce_op_outcome`, поэтому у операции
    не было ни метрики `infragram_operations_total`, ни события шины `op_done`,
    ни записи в журнале соответствия: в статистике её просто не существовало;
  * писал владельцу `retry_count=3/3 — помечена как failed (dead letter)` и
    `backoff=600s` — владелец НЕ понимает английский, а это текст карточки.

ФИКС: ветка dead letter закрывается через `_dead_letter_op` — исход по
счётчикам журнала (`op_status.classify_final`), объявление через ту же дверь,
что у остальных путей, и русский текст причины.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from services import op_status, recovery_engine


class _StuckPool:
    """Очередь с одной зависшей операцией и журналом её целей."""

    def __init__(self, *, ok_n=203, failed_n=12, retry=3, max_retries=3,
                 total_items=380):
        self.ok_n, self.failed_n = ok_n, failed_n
        self.row = {
            "id": 7001,
            "op_type": "mass_invite",
            "status": "running",
            "params": {"chat": "@target"},
            "retry_count": retry,
            "max_retries": max_retries,
            "done_items": ok_n,
            "total_items": total_items,
            "stuck_minutes": 420.0,
        }
        self.writes: list[tuple[str, tuple]] = []

    async def fetch(self, query, *args):
        if "FROM operation_queue" in query and "status='running'" in query:
            return [dict(self.row)] if self.row["status"] == "running" else []
        return []

    async def fetchrow(self, query, *args):
        if "operation_log" in query:
            return {"ok_n": self.ok_n, "failed_n": self.failed_n}
        if query.strip().upper().startswith("UPDATE"):
            return await self._update(query, args)
        if "FROM operation_queue" in query:
            return dict(self.row)
        return None

    async def fetchval(self, query, *args):
        row = await self.fetchrow(query, *args)
        return next(iter(row.values())) if row else None

    async def execute(self, query, *args):
        if query.strip().upper().startswith("UPDATE") and "operation_queue" in query:
            await self._update(query, args)
            return "UPDATE 1"
        return "OK"

    async def _update(self, query, args):
        """Применяет предикат статуса честно: иначе тест не отличил бы фикс."""
        self.writes.append((query, args))
        if "WHERE id=$1 AND status='running'" in query:
            if self.row["status"] != "running":
                return None
        elif "AND status='failed'" in query:
            if self.row["status"] != "failed":
                return None
        # новый статус передан параметром $2 во всех путях закрытия
        if len(args) >= 2 and isinstance(args[1], str) and args[1] in {
                op_status.FAILED, op_status.PARTIAL, op_status.DONE}:
            self.row["status"] = args[1]
        m = re.search(r"SET\s+status\s*=\s*'(\w+)'", query)
        if m:
            self.row["status"] = m.group(1)
        out = dict(self.row)
        out["dur_s"] = 25200.0
        return out


def _run(pool, monkeypatch, *, announced):
    async def _fake_announce(p, op_id, owner_id, op_type, params, status, ok,
                             failed, summary="", duration_s=None):
        announced.append({"op_id": op_id, "status": status, "ok": ok,
                          "failed": failed, "summary": summary,
                          "duration_s": duration_s})

    from services import op_worker as _ow

    monkeypatch.setattr(_ow, "_announce_op_outcome", _fake_announce)
    monkeypatch.setattr(recovery_engine, "_log_recovery_event",
                        lambda *a, **kw: asyncio.sleep(0))
    return asyncio.run(recovery_engine._queue_recovery(pool, None, 555))


def test_dead_letter_of_partly_done_operation_is_partial(monkeypatch):
    pool = _StuckPool()
    announced: list[dict] = []
    actions = _run(pool, monkeypatch, announced=announced)

    assert actions and actions[0].action == "fail"
    assert pool.row["status"] == op_status.PARTIAL, (
        "операция обработала 203 цели из 380 и закрыта как «не запустилась»: "
        f"статус {pool.row['status']!r}")


def test_dead_letter_without_any_success_stays_failed(monkeypatch):
    """Обратная сторона: ничего не сделала — честный исход именно 'failed'."""
    pool = _StuckPool(ok_n=0, failed_n=0)
    announced: list[dict] = []
    _run(pool, monkeypatch, announced=announced)
    assert pool.row["status"] == op_status.FAILED


def test_dead_letter_goes_through_the_single_door(monkeypatch):
    """Метрика, событие шины и журнал соответствия — через одну дверь."""
    pool = _StuckPool()
    announced: list[dict] = []
    _run(pool, monkeypatch, announced=announced)

    assert len(announced) == 1, (
        "закрытие по dead letter не объявлено: операция исчезает из метрик, "
        f"шины и журнала соответствия ({announced})")
    a = announced[0]
    assert a["op_id"] == 7001 and a["status"] == op_status.PARTIAL
    assert (a["ok"], a["failed"]) == (203, 12)
    assert a["duration_s"] == 25200.0, (
        "длительность потеряна: started_at обнулён на терминальной строке")


def test_owner_text_has_no_english_jargon(monkeypatch):
    """Владелец не понимает английский — в причине его быть не должно."""
    pool = _StuckPool()
    announced: list[dict] = []
    _run(pool, monkeypatch, announced=announced)

    texts = [str(a) for q, a in pool.writes if "error_msg" in q]
    assert texts, "причина закрытия владельцу не записана"
    blob = " ".join(texts) + " " + announced[0]["summary"]
    for bad in ("retry_count", "dead letter", "backoff", "failed)"):
        assert bad not in blob, f"в тексте владельцу осталось «{bad}»: {blob}"
    assert re.search(r"[а-яА-Я]", blob), f"текст владельцу не на русском: {blob}"


def test_resume_text_is_russian_minutes(monkeypatch):
    """Возврат в очередь тоже объясняется по-русски, а не «backoff=600s»."""
    pool = _StuckPool(retry=0, max_retries=3)
    announced: list[dict] = []
    actions = _run(pool, monkeypatch, announced=announced)

    assert actions and actions[0].action == "resume"
    msg = actions[0].outcome["error_msg"]
    assert "backoff" not in msg and "мин" in msg, msg
    assert announced == [], "возврат в очередь — не закрытие, объявлять нечего"


def test_dead_letter_respects_a_row_closed_by_someone_else(monkeypatch):
    """Строку уже закрыл другой сторож — второй раз её не закрываем."""
    pool = _StuckPool()

    async def _taken(query, *args):
        if "operation_log" in query:
            return {"ok_n": 203, "failed_n": 12}
        return None          # UPDATE ... WHERE status='running' не нашёл строки

    announced: list[dict] = []
    monkeypatch.setattr(pool, "fetchrow", _taken)
    actions = _run(pool, monkeypatch, announced=announced)

    assert actions == [], "закрытие посчитано дважды"
    assert announced == [], "итог объявлен по строке, которую мы не закрывали"
