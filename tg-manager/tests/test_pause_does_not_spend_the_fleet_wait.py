"""Пауза владельца не расходует предел ожидания свободных аккаунтов.

ЧТО БЫЛО. `operation_queue.acct_wait_since` — отметка начала ожидания свободного
флота; по ней `op_worker._requeue_op_no_accounts` проваливает операцию, если та
ждёт аккаунты дольше `_ACCT_WAIT_MAX_MIN`. Пауза и возобновление этой отметки не
трогали, то есть время под паузой шло в предел ожидания флота.

Последствие ровно то, которое однажды уже чинили для отложенного старта:
операция, постоявшая на паузе дольше двадцати минут, проваливалась на ПЕРВОЙ же
встрече с занятым флотом после возобновления — с текстом «не дождались свободных
аккаунтов за N мин», хотя не ждала нисколько. Владелец нажимал «Пауза», уходил и
возвращался к упавшей операции.

Пауза — это решение владельца, а не дефицит аккаунтов, поэтому отметка снимается
и на паузе (счётчик останавливается), и на возобновлении (на случай отметки,
поставленной до этой правки).
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _queries(src: str, pattern: str) -> list[str]:
    """Все запросы к operation_queue, у которых в SET есть заданный статус."""
    out = []
    for m in re.finditer(r"UPDATE operation_queue", src):
        seg = src[m.start():m.start() + 320]
        nxt = seg.find("UPDATE operation_queue", 1)
        if nxt > 0:
            seg = seg[:nxt]
        head = seg.split("WHERE")[0]
        if pattern in head:
            out.append(seg)
    return out


def test_pausing_stops_the_fleet_wait_clock():
    src = _read("services/mini_app_api.py")
    paused = _queries(src, "status='paused'")
    paused = [q for q in paused if "operation_queue" in q and "warmup" not in q]
    assert paused, "пауза операций не найдена — тест устарел"
    for q in paused:
        assert "acct_wait_since=NULL" in q, (
            "время под паузой идёт в предел ожидания свободного флота: "
            "возобновлённая операция провалится на первом же занятом флоте, "
            "не прождав ни секунды\n" + q.split("RETURNING")[0]
        )


def test_resuming_clears_a_stale_mark():
    src = _read("services/mini_app_api.py")
    resumed = [q for q in _queries(src, "status='pending'")
               if "status='paused'" in q]
    assert resumed, "возобновление приостановленных операций не найдено"
    for q in resumed:
        assert "acct_wait_since=NULL" in q, (
            "отметку, поставленную до этой правки, возобновление не снимает — "
            "операция провалится за «ждёт слишком долго»\n" + q
        )


def test_both_scopes_are_covered():
    """Пауза есть и для одной операции, и для всей очереди — паритет обязателен."""
    src = _read("services/mini_app_api.py")
    single = [q for q in _queries(src, "status='paused'") if "WHERE id=$1" in q]
    whole = [q for q in _queries(src, "status='paused'")
             if "WHERE owner_id=$1" in q]
    assert single, "пауза одной операции потерялась"
    assert whole, "пауза всей очереди потерялась"


def test_the_limit_itself_still_exists():
    """Если предел убрали, этот тест бессмысленен — пусть он об этом скажет."""
    ow = _read("services/op_worker.py")
    assert "_ACCT_WAIT_MAX_MIN" in ow and "acct_wait_since" in ow, (
        "предел ожидания флота исчез — проверка паузы потеряла смысл"
    )
