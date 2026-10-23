"""Пачка итогов при переподключении показывается как пачка, а не как последний.

Разрыв, который это закрывает. Счётчик уже показанных итогов (`_seen_completed`)
живёт в ПОДКЛЮЧЕНИИ SSE, а не у пользователя: на каждом новом подключении сервер
заново присылает всё, что завершилось за последние полчаса, — `fetch_completed_ops`
отдаёт до двадцати событий, и они приходят одним куском.

Каждое такое событие звало `showOpComplete`, а та переписывала плашку `#opDone`
и сбрасывала её таймер. Из пяти завершившихся операций человек видел итог ровно
ОДНОЙ, последней; остальные четыре пролетали за доли секунды, и узнать, что там
получилось, было уже негде. Заодно приходило пять вибраций подряд.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_source


def _fn(name: str) -> str:
    src = miniapp_source()
    m = re.search(r"^function " + re.escape(name) + r"\s*\(", src, re.M)
    assert m, f"функция {name} не найдена"
    i = src.index("{", m.end() - 1)
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return src[i:j + 1]


def test_sse_goes_through_the_queue_not_straight_to_the_banner():
    src = miniapp_source()
    m = re.search(r"addEventListener\('op_complete'.{0,200}", src, re.S)
    assert m, "подписка на op_complete не найдена"
    assert "queueOpComplete(" in m.group(0), (
        "итог идёт прямо в плашку: пачка при переподключении затрёт сама себя")


def test_batch_of_outcomes_is_shown_as_one_summary():
    body = _fn("_flushOpComplete")
    assert "showOpCompleteBatch(" in body, "пачка итогов показывается не сводкой"
    assert "length === 1" in body, "одиночный итог должен остаться подробным"


def test_batch_summary_counts_both_outcomes_and_names_them():
    body = _fn("showOpCompleteBatch")
    assert "успешно" in body and "с ошибкой" in body, (
        "сводка обязана разделять успехи и ошибки")
    assert "o.label" in body, (
        "сводка без имён операций не даёт узнать свою среди чужих")
    assert "openOps()" in body, "из сводки должен быть вход в список операций"
    # Одна вибрация на пачку, а не по одной на операцию.
    assert body.count("notificationOccurred") == 1


def test_duplicate_outcome_does_not_double_count():
    body = _fn("queueOpComplete")
    assert "x.id === op.id" in body, (
        "переподключение во время пачки принесёт тот же итог второй раз")
