"""Ни один статус операции не рисуется знаком вопроса.

ЧТО БЫЛО. `services/op_status` — один источник правды о статусах, у него есть и
иконки, и русские подписи. Но экраны очереди в боте держали ТРИ собственные копии
карты иконок, и ни одна не знала про `partial`, `paused` и `waiting_approval`:
каждый такой статус рисовался «❓».

Цена выросла вместе с честностью статусов. `partial` — это терминальное
состояние недоведённой работы, и им теперь закрывается всё, что оборвалось на
половине: не дождалась свободных аккаунтов, исчерпала бюджет перезапусков,
упала до старта исполнителя, зависла и была снята сторожем. То есть владелец
видел «❓» ровно в тех случаях, где важнее всего понять, что произошло.

Сам `op_status` при этом тоже не знал про состояния ожидания: `IN_FLIGHT`
перечисляет 'paused', 'scheduled' и 'waiting_approval', а в ICONS/LABELS их не
было.
"""
from __future__ import annotations

import os
import re

from services import op_status

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ── Источник правды знает все состояния ──────────────────────────────────────

def test_every_known_status_has_an_icon_and_a_label():
    known = set(op_status.TERMINAL) | set(op_status.IN_FLIGHT)
    for status in sorted(known):
        assert op_status.icon(status) != "❓", (
            f"статус «{status}» рисуется знаком вопроса"
        )
        assert op_status.label(status) != status, (
            f"у статуса «{status}» нет русской подписи — владелец увидит "
            f"английский идентификатор"
        )


def test_partial_is_not_mistaken_for_success_or_failure():
    """Недоведённая работа обязана выглядеть иначе, чем успех и чем провал."""
    assert op_status.icon(op_status.PARTIAL) not in (
        op_status.icon(op_status.DONE), op_status.icon(op_status.FAILED))
    assert "частич" in op_status.label(op_status.PARTIAL)


def test_an_unknown_status_still_falls_back_safely():
    """Падать на незнакомом статусе нельзя: экран важнее точности иконки."""
    assert op_status.icon("нечто") == "❓"
    assert op_status.label("") == "неизвестно"


# ── Храповик: экраны очереди не держат своих карт ────────────────────────────

def test_the_queue_screens_do_not_keep_their_own_icon_maps():
    src = _read("bot/handlers/botmother_menu.py")
    offenders = []
    for m in re.finditer(r'\{[^{}]*"done"\s*:\s*"[^"]+"[^{}]*\}', src):
        seg = m.group(0)
        if '"failed"' not in seg:
            continue                     # не карта статусов операции
        if '"partial"' in seg:
            continue                     # полная карта — вреда нет
        offenders.append(f"  строка {src[:m.start()].count(chr(10)) + 1}")
    assert not offenders, (
        "экран очереди снова держит свою карту иконок без 'partial' — "
        "недоведённая операция будет рисоваться знаком вопроса:\n"
        + "\n".join(offenders)
    )


def test_the_queue_screens_ask_op_status():
    src = _read("bot/handlers/botmother_menu.py")
    assert src.count("_ost.icon(") >= 3, (
        "не все три экрана очереди берут иконку из общего источника"
    )
    assert '"❓"' not in src.split("_ost.icon(")[0][-400:], (
        "рядом с вызовом остался прежний запасной знак вопроса"
    )


def test_the_detector_sees_the_screens_at_all():
    """Детектор, который ничего не находит, зелёный всегда."""
    src = _read("bot/handlers/botmother_menu.py")
    assert 'op["status"]' in src, (
        "экраны очереди больше не показывают статус операции — тест устарел"
    )
