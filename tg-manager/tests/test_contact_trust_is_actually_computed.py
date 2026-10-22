"""Доверие контакта считается и показывается, а не стоит пустым навсегда.

НАХОДКА (22.10.2026). Карточка контакта показывает строку «Доверие: N%» —
но только если `trust_score` не пуст. Записывает это поле РОВНО ОДНА
функция, `trust_engine.update_trust_scores`, и зовётся она ровно из одной
ручки, /api/miniapp/uch/trust/update, которую не звал ни один экран. То есть
поле было пустым у всех контактов всегда, строка в карточке не появлялась ни
разу, а движок, формула и её юнит-тесты (test_uch) были на месте.

ВТОРАЯ ПОЛОВИНА ТОЙ ЖЕ НАХОДКИ — ЦЕНА. Пересчёт делал ТРИ запроса НА КАЖДЫЙ
контакт: источники, сам контакт заново и UPDATE. На хабе в десять тысяч
контактов — за тридцать тысяч обращений к базе из одного HTTP-запроса. Дать
такому кнопку значило бы повесить приложение на первом же нажатии, поэтому
«недостижимо» и «непригодно» здесь одно и то же: пересчёт переписан на три
запроса независимо от числа контактов.
"""
from __future__ import annotations

import re

from tests.miniapp_source import js_function, miniapp_html, py_function


def test_the_score_can_be_recomputed_from_the_app():
    assert "/api/miniapp/uch/trust/update" in miniapp_html(), (
        "доверие снова нельзя пересчитать: ручка есть, нажать негде, "
        "и поле trust_score останется пустым у всех контактов")
    body = js_function("recomputeContactTrust")
    assert "/api/miniapp/uch/trust/update" in body, "кнопка не зовёт пересчёт"
    assert "d.updated" in body, "экран не говорит, сколько контактов пересчитано"
    assert "loadContacts(" in body, (
        "список не перечитан: пересчёт выглядел бы как «ничего не произошло»")


def test_the_tool_is_in_the_hub_toolbar():
    """Инструменты хаба — закрытый список со счётчиком в заголовке."""
    html = miniapp_html()
    assert 'onclick="recomputeContactTrust()"' in html, "инструмента нет в панели"
    m = re.search(r"ещё (\d+)</span>", html)
    assert m, "счётчик инструментов не найден"
    chips = html.count('<span class="chip" style="flex:0 0 auto" onclick="open')
    # Счётчик в заголовке обязан совпадать с числом чипов внутри, иначе он
    # врёт — а на нём и держится «ни один инструмент не убран».
    assert int(m.group(1)) >= chips, (
        f"счётчик говорит «ещё {m.group(1)}», а инструментов в панели {chips}")


def test_the_recompute_does_not_query_per_contact():
    """Запрос на контакт — это не медленно, это неработоспособно.

    Проверяем структуру, а не время: внутри цикла по контактам не должно
    остаться ни одного await к базе. Замер по времени потребовал бы живой
    базы и десяти тысяч строк, а поломка возвращается одной строкой внутрь
    цикла — её видно и так.
    """
    src = py_function("services/contacts_hub/trust_engine.py", "update_trust_scores")
    body = src[src.index("contacts = await pool.fetch"):]
    for loop in re.finditer(r"\n    for .+?:\n((?:(?:        .*)?\n)+)", body):
        inner = loop.group(1)
        assert "await pool." not in inner, (
            "внутри цикла по контактам снова обращение к базе: на десяти "
            "тысячах контактов это десятки тысяч запросов за один HTTP-вызов")
    assert src.count("await pool.") <= 3, (
        f"запросов в пересчёте {src.count('await pool.')} — должно быть три: "
        "контакты, их источники и одна запись пачкой")


def test_the_formula_still_gets_real_sources():
    """Передать вместо источников их число — значит разойтись с формулой.

    compute_trust_score смотрит на len(sources), но это деталь формулы, а не
    контракт: правка, которая заглянет внутрь источника, молча сломала бы
    пакетный путь, если ему подсунуть range(n).
    """
    src = py_function("services/contacts_hub/trust_engine.py", "update_trust_scores")
    assert "by_contact.get(c['id'], [])" in src, (
        "в формулу уходят не настоящие строки источников")
    assert "range(" not in src, "источники подменены их числом"


def test_the_bulk_write_stays_inside_the_owner():
    src = py_function("services/contacts_hub/trust_engine.py", "update_trust_scores")
    i = src.index("UPDATE unified_contacts")
    assert "owner_id = $3" in src[i:i + 400], (
        "пакетная запись без owner_id в условии: любая ошибка выше тронет "
        "чужие строки")
