"""Регрессия: кнопка «Открыть» обещала открыть и не открывала.

Что было сломано.

1. `runPulseAction` на незнакомый раздел говорила «Открываю…» и не открывала
   ничего. Человек нажимал «Открыть» в подсказке или в нудже бота, читал, что
   раздел открывается, и оставался на месте — без единого признака поломки.

2. Одно и то же событие вело в разные места. Нудж бота про упавшую операцию
   собирает ссылку `operation:<op_id>` (services/organism/runner.py), и экран
   открывается прямо на ней. Карточка Пульса в приложении отдаёт словарь бэкенда
   как есть — с ключом `op_id` (services/organism/brain.py), а фронт читал
   только `param`: из приложения тот же самый сбой вёл в общий список, где номер
   операции уже потерян.

3. `runPulseActionById` на потерянный ключ не делала ВООБЩЕ ничего: нажатие без
   отклика. Так бывает после перерисовки Пульса.

4. `onboardStep` молчала так же — а это первый экран нового владельца, где
   кнопка без отклика читается как «приложение сломано».

5. Переход по ссылке из уведомления был обёрнут в пустой `catch(_){}`: сломанная
   ссылка роняла переход молча.
"""
from __future__ import annotations

import re

import pytest

from tests.miniapp_source import miniapp_html, source_of


def _nocomments(js: str) -> str:
    """Тело без комментариев: иначе проверка «этой строки в коде нет» падает на
    комментарии, который эту же строку цитирует, объясняя, что было сломано."""
    return re.sub(r"//[^\n]*", "", js)


def _fn(name: str) -> str:
    src = source_of(name)
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\s*\([^)]*\)\s*\{", src, re.M)
    assert m, f"функция {name} не найдена"
    i = m.end() - 1
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    pytest.fail(f"не закрылось тело {name}")


def test_unknown_section_says_so_instead_of_promising():
    body = _nocomments(_fn("runPulseAction"))
    assert "Открываю…" not in body, (
        "«Открываю…» — обещание, за которым ничего не следует: раздел не "
        "открывается, а человек остаётся на месте и считает, что открывается")
    assert "Не знаю раздела" in body, (
        "незнакомый раздел — это поломка ссылки, и сказать надо именно это")


def test_pulse_card_and_bot_nudge_lead_to_the_same_place():
    """brain.py кладёт op_id, runner.py — param; читать надо оба."""
    body = _fn("runPulseAction")
    assert "_pulseParam(" in body, (
        "параметр действия читается напрямую, значит одно из двух имён "
        "(param из ссылки, op_id из карточки) теряется")
    helper = _fn("_pulseParam")
    assert "a.param" in helper and "a.op_id" in helper, (
        "карточка Пульса про упавшую операцию вела в общий список вместо самой "
        "операции: фронт читал param, а бэкенд присылал op_id")


def test_operation_action_passes_the_id_through():
    body = _fn("runPulseAction")
    m = re.search(r"k==='operation'\)\s*return\s+openMassOps\(([^)]*)\)", body)
    assert m, "действие «операция» больше не ведёт в операции"
    assert "prm" in m.group(1), (
        "номер операции не доезжает до экрана — открывается общий список")


def test_stale_pulse_card_says_it_is_stale():
    body = _fn("runPulseActionById")
    assert "if (!a)" in body, "потерянный ключ по-прежнему даёт полную тишину"
    assert "toast(" in body and "loadOrganismPulse()" in body, (
        "нажатие без единого отклика читается как сломанное приложение; "
        "карточку надо признать устаревшей и обновить подсказки")


def test_onboarding_step_never_fails_silently():
    body = _fn("onboardStep")
    assert "typeof f !== 'function'" in body, (
        "первый экран нового владельца: кнопка без отклика читается как "
        "«приложение сломано»")
    assert "toast(" in body, "неизвестный шаг обязан сказать о себе"


def test_deep_link_failure_is_not_swallowed():
    """Границы берём по структуре setTimeout(...), а не окном в N символов:
    окно фиксированной длины — ровно то, что запрещает храповик
    test_no_silently_disabled_guards (сдвинулся код — защита выключилась)."""
    html = miniapp_html()
    i = html.index("runPulseAction({kind: _screen")
    start = html.rindex("setTimeout(", 0, i)
    depth = 0
    end = None
    for j in range(html.index("(", start), len(html)):
        if html[j] == "(":
            depth += 1
        elif html[j] == ")":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    assert end, "не нашли границы перехода по ссылке"
    call = html[start:end]
    assert "catch(_){}" not in call and "catch (_) {}" not in call, (
        "пустой catch: сломанная ссылка из уведомления роняла переход молча")
    assert "Не удалось открыть раздел из ссылки" in call, (
        "человек должен узнать, что ссылка не сработала")
