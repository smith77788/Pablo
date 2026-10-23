"""Откат карточки контакта называет, что именно затрёт.

НАХОДКА (22.10.2026). Кнопка «Откат» на экране версий перезаписывает карточку
целиком и спрашивала ровно одно: «Откатить к версии 3?». Ни одного поля, ни
одного значения — владельцу предлагалось подписать не глядя необратимую (до
этого дня — буквально необратимую, см.
tests/test_contact_versions_are_actually_created.py) перезапись имени,
телефонов, заметок и тегов.

Движок умел считать разницу и раньше: `get_version_diff` лежал написанным и
непозванным. Но он отвечает на ДРУГОЙ вопрос — что изменилось в тот момент
(версия N против N-1). Перед перезаписью нужен не он, а «что я сейчас
потеряю»: версия N против ТЕКУЩЕЙ карточки. Это `rollback_preview`.

ОБРАТНЫЙ КОНТРОЛЬ (каждая проверка краснела на своей поломке): сравнил
снимок без приведения — «изменением» стали все поля подряд, включая
нетронутые телефоны; вернул фронту вопрос без списка полей — проверка
подтверждения покраснела; разрешил откат при пустом списке изменений —
покраснела проверка, что при совпадении запрос не уходит вовсе.
"""
from __future__ import annotations

import asyncio

from services.contacts_hub import versioning_engine as ve
from tests.miniapp_source import js_function, py_function
from tests.miniapp_routes import registered_routes


class _Pool:
    """Отдаёт версию и текущую карточку, различая их по самому запросу."""

    def __init__(self, snapshot, current):
        self._snapshot = snapshot
        self._current = current

    async def fetchrow(self, sql, *params):
        if 'contact_versions' in sql:
            return {'snapshot': self._snapshot} if self._snapshot is not None else None
        return self._current


CURRENT = {
    'first_name': 'Пётр',
    'phones': '["+79990000000"]',       # jsonb из базы приходит строкой
    'notes': None,
    'tags': ['vip'],
    'birthday': '1990-05-01',
}
OLD = {
    'first_name': 'Иван',               # отличается
    'phones': ['+79990000000'],         # то же, но списком
    'notes': '',                        # пусто было, пусто и осталось
    'tags': ['vip'],
    'birthday': '1990-05-01',
}


def _preview(snapshot, current=CURRENT, vnum=3):
    return asyncio.run(ve.rollback_preview(_Pool(snapshot, current), 'c-1', 7, vnum))


def test_only_the_fields_that_really_change_are_named():
    out = _preview(OLD)
    assert [c['field'] for c in out['changes']] == ['first_name'], out['changes']
    assert out['total'] == 1
    c = out['changes'][0]
    assert c['from'] == 'Пётр' and c['to'] == 'Иван', c
    assert c['label'] == 'имя', "владелец читает подпись, а не имя столбца"


def test_a_version_equal_to_the_card_changes_nothing():
    """Честный пустой ответ лучше молчаливой перезаписи той же строки."""
    same = dict(CURRENT)
    same['phones'] = ['+79990000000']
    out = _preview(same)
    assert out['changes'] == [] and out['total'] == 0, out


def test_a_missing_version_is_not_an_empty_diff():
    assert _preview(None) is None, "отсутствие версии нельзя путать с «ничего не изменится»"


def test_the_screen_asks_the_server_before_asking_the_owner():
    body = js_function('rollbackVersion')
    i = body.index("'/api/miniapp/uch/contacts/'")
    assert body.index('askConfirm') > i, "вопрос задаётся раньше, чем известно, что изменится"
    # Вопрос перечисляет поля и значения, а не только номер версии.
    assert 'c.label' in body, "подтверждение снова не называет поля"
    assert 'verVal(c.from)' in body and 'verVal(c.to)' in body, (
        "подтверждение не показывает, что на что меняется")
    assert "plural(" in body and "num(" in body, "«изменит 2 поле» — так не говорят"


def test_refusal_and_an_empty_diff_both_stop_the_request():
    body = js_function('rollbackVersion')
    import re
    assert re.search(r"if\s*\(\s*!\s*await askConfirm\(", body), (
        "«Отмена» не останавливает перезапись")
    # Совпадение с текущей карточкой — выход до запроса, а не «успешный» откат.
    assert 'if (!ch.length)' in body, (
        'при совпадении версии с карточкой откат уходит молча')
    i = body.index('if (!ch.length)')
    j = body.index("{method:'POST'}")
    assert i < j and 'return' in body[i:i + 200], (
        "при совпадении версии с карточкой запрос всё равно уходит")


def test_the_preview_and_the_rollback_share_one_path():
    routes = set(registered_routes())
    path = '/api/miniapp/uch/contacts/{contact_id}/rollback/{version_num}'
    assert f'GET {path}' in routes, "предпросмотр отката некому ответить"
    assert f'POST {path}' in routes, "сам откат пропал"


def test_the_preview_compares_with_the_card_not_with_the_previous_version():
    """`get_version_diff` отвечает на другой вопрос и для этого не годится."""
    body = py_function('services/contacts_hub/versioning_engine.py', 'rollback_preview')
    assert 'unified_contacts' in body, (
        "предпросмотр смотрит только в историю — он не знает, что в карточке сейчас")
    assert '_norm(' in body, (
        "сравнение без приведения: jsonb из базы — строка, и «изменится» будет всё")
