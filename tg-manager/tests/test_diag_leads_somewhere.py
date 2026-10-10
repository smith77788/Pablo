"""Диагностика: каждая строка, называющая проблему, ведёт туда, где её чинят.

Экран печатал стену чисел и галочек, и ни одна строка никуда не вела.
«Ошибок за 24ч: 7» — а каких именно, смотреть негде; «Всего / активны 28 / 3»
— а что с остальными двадцатью пятью; «CF relay выкл» — английским по-русски
человеку, который по-английски не читает. Внизу текст «устраните причину
(релог аккаунта, прокси, FloodWait)» — и ни одной кнопки.

Отдельно держим недоведённые операции. Бэкенд отдаёт partial_24h с самого
начала, экран это поле просто не читал: операция, оборвавшаяся на половине,
не попадала ни в «успешно», ни в «ошибок», то есть исчезала из сводки совсем.
Статус partial лежит в op_status.TERMINAL, значит срез по нему принимается —
не хватало только подписи и кнопки.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
API = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()


def _fn(name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", HTML)
    assert m, f"функция {name} не найдена"
    i = HTML.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(HTML)):
        if HTML[j] == "{":
            depth += 1
        elif HTML[j] == "}":
            depth -= 1
            if depth == 0:
                return HTML[i:j + 1]
    raise AssertionError(name)


def test_backend_still_reports_partial():
    """Самопроверка: поле, ради которого всё затевалось, бэкенд правда отдаёт."""
    assert "AS partial_24h" in API, (
        "бэкенд больше не считает недоведённые — экрану нечего показывать")


def test_partial_is_a_real_filter_value():
    """Срез по partial принимается: статус лежит в TERMINAL."""
    st = open(os.path.join(ROOT, "services", "op_status.py"), encoding="utf-8").read()
    assert re.search(r"TERMINAL = frozenset\(\{[^}]*PARTIAL", st), (
        "partial выпал из TERMINAL — ссылка на срез начнёт отдавать 400")


def test_numbers_lead_to_the_rows_behind_them():
    f = _fn("runDiag")
    for target in ("opsGo('pending')", "opsGo('running')",
                   "opsGo('failed')", "opsGo('partial')"):
        assert target in f, f"число не ведёт к своим строкам: {target}"
    assert "goTab('accounts')" in f, "аккаунты из диагностики не открыть"
    assert "openProxies()" in f, "прокси из диагностики не открыть"


def test_partial_is_on_the_screen_at_all():
    f = _fn("runDiag")
    assert "q.partial_24h" in f, (
        "недоведённые операции не показаны — они не попадают ни в успешные, "
        "ни в упавшие и исчезают из сводки")


def test_partial_has_a_russian_label_and_a_button():
    i = HTML.index("const OPS_STATUS_LABELS")
    assert "partial:" in HTML[i:i + 400], (
        "срез подписывается владельцу сырым ключом «partial»")
    assert "filterOps('partial'" in HTML, "среза «недоведены» нет в списке операций"


def test_verdict_is_stated_before_the_numbers():
    f = _fn("runDiag")
    assert "Операции пойдут" in f and "Операции не пойдут" in f, (
        "экран не говорит главного: заработают операции или нет")


def test_no_english_left_on_the_screen():
    f = _fn("runDiag")
    assert "CF relay" not in f, "вернулась английская подпись CF relay"
    assert "accRu(lt.status)" in f, (
        "статус аккаунта уезжает владельцу английским ключом")


def test_account_statuses_from_the_live_check_are_translated():
    """cooldown и no_session возвращает check_account_status_full."""
    i = HTML.index("function accRu(")
    block = HTML[i:i + 900]
    for key in ("cooldown:", "no_session:"):
        assert key in block, f"нет русской подписи для статуса {key}"


def test_failed_live_check_offers_a_way_out():
    f = _fn("runDiag")
    i = f.index("Подключение не работает")
    tail = f[i:]
    assert "К аккаунтам" in tail and "К прокси" in tail, (
        "живая проверка упала, а кнопок для починки нет")


def test_error_state_has_retry():
    f = _fn("runDiag")
    assert "errHtml(errRu(e), 'runDiag()')" in f, "ошибка диагностики — тупик"
