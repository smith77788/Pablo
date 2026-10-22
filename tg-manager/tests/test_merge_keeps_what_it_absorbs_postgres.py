"""Слияние дублей переносит поля, а не выбрасывает их вместе с контактом.

НАХОДКА (22.10.2026). «Объединить дубли» во ВСЕХ трёх своих видах —
merge_engine.manual_merge (кнопка в карточке), merge_engine.auto_merge и
bulk_ops_engine.bulk_merge (пакетное слияние с экрана дублей) — делало
ровно одно: перецепляло contact_sources и contact_history на основной
контакт и УДАЛЯЛО второй. Ни одного поля никуда не переносилось. Телефон,
почта, компания, должность, заметки и теги, которые были только у второго,
исчезали навсегда: без версии, без следа, без возможности вернуть. Операция
называлась «объединить», а делала «удалить дубль».

Три копии одной ошибки лежали рядом и чинились бы по одной, поэтому слияние
теперь одно — merge_engine.absorb_contact, — и все три пути зовут его.

ПОЧЕМУ ЖИВОЙ POSTGRES. Проверка на заглушке видит только текст запросов, а
вопрос здесь ровно в том, что ОСТАЛОСЬ в базе после слияния. Ровно так же
сегодня уже обожглись на пересчёте доверия: все проверки текста были
зелёными, пока Postgres отбивал запрос целиком.

ОБРАТНЫЕ КОНТРОЛИ. (1) absorbed_fields, возвращающий пустой словарь, то есть
прежнее поведение, — краснеют три проверки: данные поглощённого контакта,
версия перед слиянием, пакетное слияние. (2) Предпросмотр со своими
правилами (как было у покойного get_merge_preview: company/position/notes и
ничего больше) — краснеют обе проверки предпросмотра: он умалчивает о
телефонах и тегах и обещает перенос туда, где слияние ничего не меняет.

КАК ЗАПУСТИТЬ — см. докстринг tests/test_invite_e2e_postgres.py; переменная
та же, INFRAGRAM_TEST_DSN.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 991844
STRANGER = 991845
_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _mk():
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        files = ["schema.sql"] + sorted(
            glob.glob(os.path.join(ROOT, "schema_v*.sql")),
            key=lambda f: int(re.search(r"schema_v(\d+)", f).group(1)))
        for f in files:
            path = f if os.path.isabs(f) else os.path.join(ROOT, f)
            if not os.path.exists(path):
                continue
            try:
                await p.execute(open(path, encoding="utf-8").read())
            except Exception:
                pass
        return p

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    for uid in (OWNER, STRANGER):
        _run(p.execute("DELETE FROM unified_contacts WHERE owner_id=$1", uid))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    for uid in (OWNER, STRANGER):
        _run(pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", uid))
    yield


def _mk_contact(pool, owner, **fields):
    cols = ['owner_id'] + list(fields)
    vals = [owner]
    place = ['$1']
    for i, (k, v) in enumerate(fields.items(), start=2):
        if k in ('phones', 'emails', 'websites', 'addresses', 'custom_fields'):
            place.append(f'${i}::jsonb')
            v = json.dumps(v)
        elif k == 'tags':
            place.append(f'${i}::text[]')
        else:
            place.append(f'${i}')
        vals.append(v)
    return _run(pool.fetchval(
        f"INSERT INTO unified_contacts ({', '.join(cols)}) "
        f"VALUES ({', '.join(place)}) RETURNING id", *vals))


def _row(pool, cid):
    return _run(pool.fetchrow("SELECT * FROM unified_contacts WHERE id=$1", cid))


def test_the_absorbed_contacts_data_survives(pool):
    """Телефон, который был только у второго, обязан остаться у первого."""
    from services.contacts_hub.merge_engine import manual_merge

    a = _mk_contact(pool, OWNER, first_name="Иван", phones=["+70000000001"])
    b = _mk_contact(pool, OWNER, first_name="Иван", phones=["+70000000002"],
                    company="ООО Ромашка", notes="познакомились на конференции",
                    tags=["vip"])
    res = _run(manual_merge(pool, str(a), str(b), OWNER))
    assert res.get('status') == 'merged', res

    row = _row(pool, a)
    phones = row['phones']
    if isinstance(phones, str):
        phones = json.loads(phones)
    assert sorted(phones) == ["+70000000001", "+70000000002"], (
        f"телефон поглощённого контакта потерян: {phones}")
    assert row['company'] == "ООО Ромашка", "компания потеряна"
    assert row['notes'] == "познакомились на конференции", "заметки потеряны"
    assert list(row['tags'] or []) == ["vip"], "теги потеряны"
    assert _row(pool, b) is None, "второй контакт остался — дубль не убран"


def test_a_filled_field_of_the_primary_is_never_overwritten(pool):
    """Слияние может дополнить основной контакт, но не ухудшить его."""
    from services.contacts_hub.merge_engine import manual_merge

    a = _mk_contact(pool, OWNER, first_name="Иван", company="Основная компания")
    b = _mk_contact(pool, OWNER, first_name="Ваня", company="Другая компания")
    _run(manual_merge(pool, str(a), str(b), OWNER))
    row = _row(pool, a)
    assert row['company'] == "Основная компания", "непустое поле перезаписано"
    assert row['first_name'] == "Иван", "имя основного контакта подменено"


def test_the_merge_leaves_a_version_to_come_back_to(pool):
    """Второй контакт удаляется навсегда — вернуть можно только через версию."""
    from services.contacts_hub.merge_engine import manual_merge

    a = _mk_contact(pool, OWNER, first_name="Иван")
    b = _mk_contact(pool, OWNER, first_name="Иван", company="ООО Ромашка")
    _run(manual_merge(pool, str(a), str(b), OWNER))

    vers = _run(pool.fetch(
        "SELECT snapshot, changed_by, change_summary FROM contact_versions "
        "WHERE contact_id=$1 AND owner_id=$2 ORDER BY version_num DESC", a, OWNER))
    assert vers, "слияние не оставило версии — откатывать нечем"
    v = vers[0]
    assert v['changed_by'] == 'merge', v['changed_by']
    snap = v['snapshot']
    if isinstance(snap, str):
        snap = json.loads(snap)
    assert not snap.get('company'), (
        "в версии лежит состояние ПОСЛЕ слияния — вернуться к прежнему нельзя")
    assert 'компания' in (v['change_summary'] or ''), (
        f"подпись версии не по-русски: {v['change_summary']!r}")


def test_a_pair_with_a_stranger_changes_nothing(pool):
    """Чужой контакт нельзя ни поглотить, ни тронуть его источники."""
    from services.contacts_hub.bulk_ops_engine import bulk_merge

    mine = _mk_contact(pool, OWNER, first_name="Мой")
    theirs = _mk_contact(pool, STRANGER, first_name="Чужой", company="Секрет")
    res = _run(bulk_merge(pool, OWNER, [
        {'primary_id': str(mine), 'secondary_id': str(theirs)}]))
    assert res['merged'] == 0 and res['skipped'] == 1, res
    assert _row(pool, theirs) is not None, "чужой контакт удалён"
    assert _row(pool, mine)['company'] is None, "в мой контакт утекло чужое поле"


def test_bulk_merge_absorbs_too(pool):
    """Пакетное слияние — тот же путь: у него была своя копия той же ошибки."""
    from services.contacts_hub.bulk_ops_engine import bulk_merge

    a = _mk_contact(pool, OWNER, first_name="Пётр", emails=["p@one.ru"])
    b = _mk_contact(pool, OWNER, first_name="Пётр", emails=["p@two.ru"])
    res = _run(bulk_merge(pool, OWNER, [
        {'primary_id': str(a), 'secondary_id': str(b)}]))
    assert res['merged'] == 1, res
    emails = _row(pool, a)['emails']
    if isinstance(emails, str):
        emails = json.loads(emails)
    assert sorted(emails) == ["p@one.ru", "p@two.ru"], (
        f"пакетное слияние снова теряет поля: {emails}")


def test_the_preview_promises_exactly_what_the_merge_does(pool):
    """Предпросмотр и слияние обязаны совпадать до значения.

    Не «похожи», а совпадают: предпросмотр показывает ровно то, что потом
    окажется в карточке. Проверяем это единственным честным способом — сначала
    спрашиваем предпросмотр, потом сливаем и сравниваем с базой.
    """
    from services.contacts_hub.merge_engine import manual_merge, merge_preview

    a = _mk_contact(pool, OWNER, first_name="Анна", phones=["+70000000011"])
    b = _mk_contact(pool, OWNER, first_name="Аня", phones=["+70000000012"],
                    company="ООО Вектор", position="директор", tags=["клиент"])
    prev = _run(merge_preview(pool, str(a), str(b), OWNER))
    assert 'error' not in prev, prev
    promised = {c['field']: c['to'] for c in prev['taken']}
    assert promised, "предпросмотр обещает, что не переедет ничего"
    assert prev['secondary'], "не сказано, какая именно запись исчезнет"
    # Полнота, а не только правдивость: умолчать о переезжающем поле — тот же
    # обман, что назвать несуществующее. Телефон второй записи переедет
    # обязательно, значит обязан быть и в обещании.
    assert 'phones' in promised, (
        f"предпросмотр умолчал о телефонах: {sorted(promised)}")
    assert 'tags' in promised, f"предпросмотр умолчал о тегах: {sorted(promised)}"

    _run(manual_merge(pool, str(a), str(b), OWNER))
    row = _row(pool, a)
    for field, value in promised.items():
        actual = row[field]
        if isinstance(actual, str) and field in ('phones', 'emails', 'websites',
                                                 'addresses', 'custom_fields'):
            actual = json.loads(actual)
        if isinstance(actual, (list, tuple)):
            actual = list(actual)
        assert actual == value, (
            f"предпросмотр обещал {field}={value!r}, а в карточке {actual!r}")


def test_the_preview_says_nothing_moves_when_nothing_does(pool):
    from services.contacts_hub.merge_engine import merge_preview

    a = _mk_contact(pool, OWNER, first_name="Анна", company="ООО Вектор")
    b = _mk_contact(pool, OWNER, first_name="Аня", company="Другая")
    prev = _run(merge_preview(pool, str(a), str(b), OWNER))
    assert prev['taken'] == [], (
        "предпросмотр обещает перенос в заполненные поля — слияние так не делает")


def test_the_preview_refuses_a_stranger(pool):
    from services.contacts_hub.merge_engine import merge_preview

    mine = _mk_contact(pool, OWNER, first_name="Мой")
    theirs = _mk_contact(pool, STRANGER, first_name="Чужой", company="Секрет")
    prev = _run(merge_preview(pool, str(mine), str(theirs), OWNER))
    assert prev.get('error') == 'secondary_not_found', (
        f"предпросмотр показал поля чужого контакта: {prev}")
