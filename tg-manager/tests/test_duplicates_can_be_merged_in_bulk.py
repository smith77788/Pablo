"""Надёжные дубли контактов объединяются пакетом, а не по одной паре.

НАХОДКА (22.10.2026). Экран «Возможные дубликаты» объединял ровно одну пару
за нажатие (/api/miniapp/uch/merge). Пакетная ручка
/api/miniapp/uch/bulk/merge и движок bulk_ops_engine.bulk_merge были
написаны, проверяли владение обоими контактами (межарендный IDOR закрыт) и
возвращали {merged, skipped} — и не звались ниоткуда. При пятидесяти парах
владелец нажимал пятьдесят раз, а готовое решение лежало рядом.

ПОЧЕМУ ПАКЕТ ИДЁТ НЕ ПО ВСЕМУ СПИСКУ. Объединение необратимо: вторая запись
пары удаляется. Поэтому пакетом уходят только пары, в которых движок уверен
(confidence ≥ DUP_SURE), а остальные остаются в списке и объединяются по
одной — глазами. Кнопка показывается лишь когда таких пар больше одной:
иначе пакет не делает ничего, чего не делает обычная кнопка в строке.
"""
from __future__ import annotations

from tests.miniapp_source import js_function, miniapp_html


def test_the_batch_engine_is_reachable():
    assert "/api/miniapp/uch/bulk/merge" in miniapp_html(), (
        "пакетное объединение снова недостижимо: ручка есть, нажать негде")
    body = js_function("mergeAllDuplicates")
    assert "/api/miniapp/uch/bulk/merge" in body, "кнопка не зовёт пакетную ручку"
    assert "primary_id" in body and "secondary_id" in body, (
        "движок ждёт пары {primary_id, secondary_id} — экран их не собирает")


def test_only_confident_pairs_go_in_the_batch():
    """Необратимое действие пакетом — только там, где движок уверен."""
    body = js_function("mergeAllDuplicates")
    assert "DUP_SURE" in body, "порог уверенности не применяется"
    assert "confidence" in body, "пары не фильтруются по уверенности"
    threshold = js_function("detectDuplicates")
    assert "DUP_SURE" in threshold, (
        "кнопка пакета показывается без оглядки на уверенность")


def test_the_batch_says_what_it_will_destroy():
    body = js_function("mergeAllDuplicates")
    assert "askConfirm(" in body, "пакетное объединение уходит без подтверждения"
    i = body.index("askConfirm(")
    question = body[i:i + 900]
    assert "удалена" in question, (
        "вопрос не говорит, что вторая запись каждой пары удаляется")
    assert "Отменить это нельзя" in question, "вопрос не говорит о необратимости"
    assert "plural(" in question, "число пар в вопросе без согласованной формы"


def test_skipped_pairs_are_reported():
    """Движок пропускает пару (уже слита, чужой id) — молчать об этом нельзя."""
    body = js_function("mergeAllDuplicates")
    assert "d.skipped" in body, "экран не читает skipped"
    assert "пропущено" in body, "о пропущенных парах владельцу не говорят"
