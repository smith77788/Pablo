"""Главный экран: девять чисел, ни одно не открывалось.

Панель — первое, что владелец видит, и там же самые важные числа продукта:
сколько аккаунтов в строю, сколько в бане, сколько операций упало за сутки,
сколько аккаунтов под риском. Все девять были просто текстом: увидеть, КАКИЕ
именно аккаунты в бане или КАКИЕ операции упали, с панели было нечем — нужно
было уйти в другой раздел и там повторить срез руками.

Переходы уже существовали (`healthGoAccounts`, `opsGo`, `openHealth`,
`openNewUsers`) — панель ими просто не пользовалась.

Отдельно здесь закреплено разделение «Бан и спамблок» на две плитки: одна
цифра на два разных среза никуда не ведёт честно, потому что открыть можно
только один из них.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _html() -> str:
    with open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8") as f:
        return f.read()


def _dashboard(html: str) -> str:
    i = html.index("async function loadDashboard()")
    return html[i:html.index("function dashSetMetric", i)]


def _silent(calls):
    """Вызовы card(...) без перехода: четвёртый аргумент — "имяФункции(...)"."""
    return [c for c in calls if not re.search(r'"\w+\(', c)]


def test_probe_sees_the_tiles_and_a_silent_one():
    """Проба, которая ничего не способна найти, вечно зелёная и потому лживая."""
    body = _dashboard(_html())
    assert len(re.findall(r"card\(", body)) >= 6, "плитки панели перестали находиться"
    assert _silent(["num(a.total||0), 'Аккаунты в строю', ''"]), (
        "проба не видит плитку без перехода")
    assert not _silent(['num(o.failed), \'С ошибкой\', \'\', "opsGo(\'failed\')"']), "проба ругается на здоровую плитку"


def test_every_dashboard_number_opens_what_it_counts():
    body = _dashboard(_html())
    # Каждый вызов card(...) обязан нести переход: иначе число снова немое.
    calls = re.findall(r"card\((.*?)\);?\n", body)
    silent = _silent(calls)
    assert not silent, (
        f"числа на панели, которые никуда не ведут: {silent}")


def test_destinations_exist():
    html = _html()
    body = _dashboard(html)
    for fn in re.findall(r"onclick=\\?\"(\w+)\(", body):
        assert f"function {fn}(" in html, f"переход {fn} с панели ведёт в никуда"


def test_ban_and_spamblock_are_two_tiles_now():
    body = _dashboard(_html())
    assert "healthGoAccounts('banned')" in body
    assert "healthGoAccounts('spamblock')" in body
    assert "'Бан и спамблок'" not in body, (
        "одна цифра на два среза: открыть можно только один, значит она врёт")


def test_failed_operations_open_the_failed_ones():
    body = _dashboard(_html())
    assert "opsGo('failed')" in body
    assert "opsGo('running')" in body
