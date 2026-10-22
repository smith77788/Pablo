"""Правила переноса при слиянии дублей — и ровно одна их реализация.

Поведение на живой базе стережёт
tests/test_merge_keeps_what_it_absorbs_postgres.py; здесь — сами правила
(они чистые, база для них не нужна) и то, что реализация слияния одна.

ПОЧЕМУ «ОДНА» ВАЖНО. Их было три: merge_engine.manual_merge,
merge_engine.merge_contacts_improved и bulk_ops_engine.bulk_merge. Все три
делали одно и то же и содержали одну и ту же ошибку — перецепляли источники
и удаляли второй контакт, не перенося ни одного поля. Три копии одной
ошибки чинятся по одной и расходятся при первой же правке.

ОБРАТНЫЙ КОНТРОЛЬ: заставил absorbed_fields возвращать пустой словарь —
покраснели три проверки на живой базе (данные поглощённого контакта,
версия перед слиянием, пакетное слияние) и две здешние.
"""
from __future__ import annotations

from services.contacts_hub.merge_engine import absorbed_fields
from tests.miniapp_source import js_function, py_function


def test_lists_are_joined_not_chosen_between():
    """Два телефона одного человека — это два телефона."""
    out = absorbed_fields({'phones': ['+7001']}, {'phones': ['+7002', '+7001']})
    assert out['phones'] == ['+7001', '+7002'], out
    # jsonb приходит из базы строкой — это не повод считать поле пустым.
    out = absorbed_fields({'phones': '["+7001"]'}, {'phones': '["+7002"]'})
    assert out['phones'] == ['+7001', '+7002'], out


def test_nothing_moves_when_there_is_nothing_new():
    assert absorbed_fields({'phones': ['+7001']}, {'phones': ['+7001']}) == {}
    assert absorbed_fields({'company': 'А'}, {'company': 'А'}) == {}


def test_a_filled_field_is_never_overwritten():
    """Слияние вправе дополнить основной контакт, но не ухудшить его."""
    out = absorbed_fields({'company': 'Основная'}, {'company': 'Другая'})
    assert 'company' not in out, out
    # А пустое — заполняется.
    assert absorbed_fields({'company': None}, {'company': 'Другая'})['company'] == 'Другая'
    assert absorbed_fields({'company': ''}, {'company': 'Другая'})['company'] == 'Другая'


def test_custom_fields_merge_key_by_key():
    out = absorbed_fields({'custom_fields': {'a': 1}}, {'custom_fields': {'a': 2, 'b': 3}})
    assert out['custom_fields'] == {'a': 1, 'b': 3}, out


def test_every_mergeable_path_goes_through_one_implementation():
    for module, fn in (('services/contacts_hub/merge_engine.py', 'manual_merge'),
                       ('services/contacts_hub/merge_engine.py', 'merge_contacts_improved'),
                       ('services/contacts_hub/merge_engine.py', 'auto_merge'),
                       ('services/contacts_hub/bulk_ops_engine.py', 'bulk_merge')):
        body = py_function(module, fn)
        assert 'absorb_contact(' in body, (
            f"{fn} снова сливает контакты по-своему — поля потеряются именно там")
        assert 'DELETE FROM unified_contacts' not in body, (
            f"{fn} удаляет контакт мимо общего пути: без переноса полей и без версии")


def test_the_screen_promise_matches_what_the_engine_does():
    """Текст подтверждения обещал перенос данных и ТОГДА, когда переноса не было.

    Это худший вид неправды в интерфейсе: человек соглашается именно потому,
    что ему пообещали ничего не потерять.
    """
    for fn in ('mergeContacts', 'mergeAllDuplicates'):
        body = js_function(fn)
        assert 'перенесутся' in body, f"{fn}: подтверждение больше не говорит о переносе"
        assert 'Версиях' in body, (
            f"{fn}: не сказано, что слияние можно откатить — а это единственный путь назад")
    absorb = py_function('services/contacts_hub/merge_engine.py', 'absorb_contact')
    assert 'absorbed_fields(' in absorb and 'UPDATE unified_contacts SET' in absorb, (
        "движок снова ничего не переносит, а экран продолжает это обещать")


def test_the_preview_is_computed_by_the_merge_itself():
    """Предпросмотр, считающий по своим правилам, врёт точнее, чем молчание.

    Здесь уже лежал `get_merge_preview`: написанный, непозванный и со своим
    набором из восьми полей и правилом «a или b». Правила самого слияния были
    другими. Подключить такой предпросмотр значило бы показать одно, а
    сделать другое.
    """
    body = py_function('services/contacts_hub/merge_engine.py', 'merge_preview')
    assert 'absorbed_fields(' in body, (
        "предпросмотр снова считает по своим правилам — он разойдётся со слиянием")
    import services.contacts_hub.merge_engine as me
    assert not hasattr(me, 'get_merge_preview'), (
        "второй предпросмотр со своими правилами вернулся в модуль")


def test_the_screen_asks_what_moves_before_asking_to_merge():
    body = js_function('mergeContacts')
    i = body.index("'/api/miniapp/uch/merge?")
    assert body.index('askConfirm') > i, (
        "вопрос задаётся раньше, чем известно, что переедет")
    assert 'c.label' in body, "подтверждение не перечисляет поля"
    assert 'Переносить нечего' in body, (
        "когда переносить нечего, об этом надо сказать, а не обещать перенос")
