"""Мёртвый статус аккаунта не прячется под паузой и не считается доступным.

Набор «мёртвых» статусов живёт в одном месте — `account_status.DEAD_STATUSES`
(шесть: banned, deactivated, deleted, frozen, session_expired, spamblock).
Выборки «под действие» уже сведены на этот словарь, но два места того же смысла
держали литерал из трёх статусов, и оба литерала врали владельцу:

* ЗАПИСЬ ПАУЗЫ (`flood_engine`, два UPDATE). `acc_status = CASE WHEN ... IN
  ('spamblock','banned','deactivated') THEN acc_status ... ELSE 'cooldown'` —
  условие «не затирать терминальный статус». Аккаунт в `session_expired`,
  `deleted` или `frozen` под это условие не попадал, и флуд-пауза переписывала
  его статус на `cooldown`: мёртвый аккаунт выглядел «просто на паузе» и после
  неё снова шёл в работу, а причина смерти терялась.
* СЧЁТЧИК «ДОСТУПНО» (`intelligence_engine`). `COUNT(*) FILTER (... NOT IN
  ('spamblock','banned','deactivated'))` — число, которое владелец видит перед
  запуском операции. Мёртвые по трём остальным статусам считались доступными,
  то есть продукт обещал аккаунты, которых нет.

Проверка сверяет происхождение набора, а не текст: дословные имена статусов в
этих местах больше не пишутся, и именно на это попались три предыдущие
проверки — они краснели на верной правке.
"""
from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_the_vocabulary_has_all_six_statuses():
    """Антивакуумность: проверки ниже про словарь, он обязан быть непустым."""
    from services.account_status import DEAD_STATUSES

    assert {"banned", "deactivated", "deleted", "frozen", "session_expired",
            "spamblock"} <= set(DEAD_STATUSES), (
        "словарь мёртвых статусов сократился — сначала объясните, какой статус "
        "снова считается пригодным для работы")


def test_the_pause_writer_keeps_a_terminal_status():
    """Оба UPDATE паузы берут набор из словаря, а не литералом."""
    src = _src("services/flood_engine.py")
    # Окно до ближайшего END, а не срез фиксированной длины: срез сдвигается
    # вместе с кодом и однажды уже выключил такую же проверку молча.
    cases = re.findall(r"acc_status = CASE(.*?)\bEND", src, re.DOTALL)
    assert len(cases) >= 2, (
        f"найдено {len(cases)} записей статуса вместо двух — проверка смотрит "
        "не туда")
    for i, body in enumerate(cases, 1):
        assert "sql_dead_list()" in body, (
            f"запись паузы №{i} решает, что считать терминальным статусом, по "
            "своему литералу: session_expired, deleted и frozen она затрёт на "
            "'cooldown', и мёртвый аккаунт вернётся в работу после паузы")


def test_the_available_counter_counts_only_usable_accounts():
    """Счётчик «доступно» берёт набор из словаря."""
    src = _src("services/intelligence_engine.py")
    m = re.search(r"AS available", src)
    assert m, "счётчик доступных аккаунтов пропал"
    # Окно — от начала COUNT(*) FILTER до самой подписи, а не срез на глазок.
    start = src.rfind("COUNT(*) FILTER", 0, m.start())
    assert start > 0, "счётчик доступных больше не считается через FILTER"
    window = src[start:m.start()]
    # Годится любая из двух дверей словаря: `sql_not_dead()` — то же условие
    # целиком, `sql_dead_list()` — только список значений. Важно, что набор
    # приходит из словаря, а не выписан здесь руками.
    assert ("sql_not_dead()" in window or "sql_dead_list()" in window), (
        "счётчик доступных аккаунтов отсеивает мёртвых по своему литералу — "
        "владельцу обещаны аккаунты, которых нет")
