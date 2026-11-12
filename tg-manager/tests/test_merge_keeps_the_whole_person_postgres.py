"""Слияние дублей сохраняет человека ЦЕЛИКОМ, а не только поля карточки.

НАХОДКИ (12.11.2026). Слияние переносило поля (merge_engine.absorbed_fields)
и перецепляло источники с историей, но у второй записи ещё девять таблиц на
`ON DELETE CASCADE`, и удаление второй стирало их молча:

* СДЕЛКУ (contact_crm): клиент в «Переговорах» с суммой пропадал с доски;
* журнал касаний (contact_crm_activity);
* членство в группах — человек выпадал из сегмента рассылки;
* авто-теги, граф личности, связи, расхождения.

Поля личности, которые пишет синхронизация, а не экран, не переносились
вовсе: основная запись без Telegram ID поглощала запись с ним и теряла его
(в ЛС больше не написать, а синхронизация заводила дубль заново). Отметка
«не писать» (excluded) второй записи пропадала — человек, попросивший не
писать, снова попадал в рассылку. «Избранное» и оценка 5/5 пропадали, если у
основной стояли значения по умолчанию (False и 0 — не «пусто»).

Если оба контакта пришли из ОДНОГО аккаунта, перецепка источников падала на
UNIQUE(contact_id, account_id) — пара не сливалась никогда, владелец видел
«Внутренняя ошибка».

Два разных Telegram ID поиск дублей предлагал объединить (общий телефон +
имя = 0.55), а слияние оставляло один ID из двух.

Цепочка из трёх записей (А~Б по телефону, Б~В по почте) пакетом не
сливалась: после (А,Б) пара (Б,В) не находила Б и «пропускалась».

ОБРАТНЫЙ КОНТРОЛЬ. Вернуть в absorb_contact прежние две перецепки вместо
rehome_dependents — краснеют проверки сделки, касаний, групп и общего
аккаунта; убрать _absorb_identity — Telegram ID и «не писать»; убрать
отслеживание `went_to` в bulk_merge — цепочка.

Запуск: INFRAGRAM_TEST_DSN, как в tests/test_invite_e2e_postgres.py.
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
OWNER = 991861
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
    _wipe(p)
    _run(p.close())


def _wipe(p):
    _run(p.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER))
    _run(p.execute("DELETE FROM contact_groups WHERE owner_id=$1", OWNER))


@pytest.fixture(autouse=True)
def _clean(pool):
    _wipe(pool)
    yield


def _mk(pool, **fields):
    cols, vals, place = ["owner_id"], [OWNER], ["$1"]
    for i, (k, v) in enumerate(fields.items(), start=2):
        cols.append(k)
        if k in ("phones", "emails", "websites", "addresses", "custom_fields"):
            place.append(f"${i}::jsonb")
            v = json.dumps(v)
        elif k == "tags":
            place.append(f"${i}::text[]")
        else:
            place.append(f"${i}")
        vals.append(v)
    return _run(pool.fetchval(
        f"INSERT INTO unified_contacts ({', '.join(cols)}) "
        f"VALUES ({', '.join(place)}) RETURNING id", *vals))


def _merge(pool, a, b):
    from services.contacts_hub.merge_engine import manual_merge
    return _run(manual_merge(pool, a, b, OWNER))


def _row(pool, cid):
    return _run(pool.fetchrow("SELECT * FROM unified_contacts WHERE id=$1", cid))


def test_the_deal_of_the_absorbed_contact_survives(pool):
    a = _mk(pool, first_name="Иван", phones=["+79990000001"])
    b = _mk(pool, first_name="Иван", phones=["89990000001"])
    _run(pool.execute(
        "INSERT INTO contact_crm (owner_id, contact_id, stage, deal_value, currency, "
        "next_reminder_text) VALUES ($1,$2,'negotiation',300000,'RUB','перезвонить')",
        OWNER, b))
    r = _merge(pool, a, b)
    assert r["status"] == "merged", r
    deal = _run(pool.fetchrow(
        "SELECT * FROM contact_crm WHERE owner_id=$1 AND contact_id=$2", OWNER, a))
    assert deal is not None, "сделка второй записи удалена слиянием"
    assert (deal["stage"], float(deal["deal_value"]), deal["currency"]) == (
        "negotiation", 300000.0, "RUB")
    assert deal["next_reminder_text"] == "перезвонить"


def test_a_default_deal_of_the_primary_takes_the_real_one(pool):
    a = _mk(pool, first_name="Иван", phones=["+79990000002"])
    b = _mk(pool, first_name="Иван", phones=["+79990000002"])
    _run(pool.execute(
        "INSERT INTO contact_crm (owner_id, contact_id, stage, deal_value, currency) "
        "VALUES ($1,$2,'lead',0,'USD'), ($1,$3,'proposal',500,'EUR')", OWNER, a, b))
    _merge(pool, a, b)
    deal = _run(pool.fetchrow(
        "SELECT * FROM contact_crm WHERE owner_id=$1 AND contact_id=$2", OWNER, a))
    assert (deal["stage"], float(deal["deal_value"]), deal["currency"]) == (
        "proposal", 500.0, "EUR"), "сумма обязана переехать ВМЕСТЕ со своей валютой"


def test_a_filled_deal_of_the_primary_is_not_overwritten(pool):
    a = _mk(pool, first_name="Иван", phones=["+79990000003"])
    b = _mk(pool, first_name="Иван", phones=["+79990000003"])
    _run(pool.execute(
        "INSERT INTO contact_crm (owner_id, contact_id, stage, deal_value, currency) "
        "VALUES ($1,$2,'won',100,'RUB'), ($1,$3,'lost',999,'USD')", OWNER, a, b))
    _merge(pool, a, b)
    deal = _run(pool.fetchrow(
        "SELECT * FROM contact_crm WHERE owner_id=$1 AND contact_id=$2", OWNER, a))
    assert (deal["stage"], float(deal["deal_value"]), deal["currency"]) == ("won", 100.0, "RUB")


def test_touches_and_groups_survive(pool):
    a = _mk(pool, first_name="Иван", phones=["+79990000004"])
    b = _mk(pool, first_name="Иван", phones=["+79990000004"])
    _run(pool.execute(
        "INSERT INTO contact_crm_activity (owner_id, contact_id, activity_type, description) "
        "VALUES ($1,$2,'call','обсудили'), ($1,$2,'meeting','встреча')", OWNER, b))
    gid = _run(pool.fetchval(
        "INSERT INTO contact_groups (owner_id, name) VALUES ($1,'VIP') RETURNING id", OWNER))
    _run(pool.execute(
        "INSERT INTO contact_group_members (group_id, contact_id) VALUES ($1,$2)", gid, b))
    r = _merge(pool, a, b)
    assert r["moved"]["touches"] == 2 and r["moved"]["groups"] == 1, r
    n = _run(pool.fetchval(
        "SELECT COUNT(*) FROM contact_crm_activity WHERE contact_id=$1", a))
    assert n == 2, "журнал касаний второй записи удалён слиянием"
    assert _run(pool.fetchval(
        "SELECT COUNT(*) FROM contact_group_members WHERE contact_id=$1 AND group_id=$2",
        a, gid)) == 1, "человек выпал из группы после слияния"


def test_both_records_from_one_account_merge_at_all(pool):
    a = _mk(pool, first_name="Иван", phones=["+79990000005"])
    b = _mk(pool, username="ivan5", phones=["+79990000005"])
    _run(pool.execute(
        "INSERT INTO contact_sources (contact_id, account_id) VALUES ($1, 777), ($2, 777), ($2, 778)",
        a, b))
    r = _merge(pool, a, b)
    assert r["status"] == "merged", r
    accs = sorted(x["account_id"] for x in _run(pool.fetch(
        "SELECT account_id FROM contact_sources WHERE contact_id=$1", a)))
    assert accs == [777, 778]


def test_telegram_id_and_do_not_write_survive(pool):
    a = _mk(pool, first_name="Иван", phones=["+79990000006"])
    b = _mk(pool, telegram_user_id=5550006, phones=["+79990000006"],
            excluded=True, is_favorite=True, user_rating=5)
    _merge(pool, a, b)
    row = _row(pool, a)
    assert row["telegram_user_id"] == 5550006, "Telegram ID поглощённой записи потерян"
    assert row["excluded"] is True, "отметка «не писать» снята слиянием"
    assert row["is_favorite"] is True and row["user_rating"] == 5


def test_two_telegram_accounts_are_not_merged_and_not_offered(pool):
    from services.contacts_hub.trust_engine import detect_smart_duplicates
    a = _mk(pool, telegram_user_id=5550007, first_name="Иван", last_name="Петров",
            phones=["+79990000007"])
    b = _mk(pool, telegram_user_id=5550008, first_name="Иван", last_name="Петров",
            phones=["+79990000007"])
    pairs = _run(detect_smart_duplicates(pool, OWNER))["duplicates"]
    assert not pairs, f"два разных аккаунта Telegram предложены как дубль: {pairs}"
    r = _merge(pool, a, b)
    assert r["status"] == "error" and r["error"] == "different_telegram_accounts"
    assert _row(pool, a) is not None and _row(pool, b) is not None


def test_a_chain_of_three_merges_in_one_pass(pool):
    from services.contacts_hub.bulk_ops_engine import bulk_merge
    a = _mk(pool, first_name="Иван", phones=["+79990000009"])
    b = _mk(pool, first_name="Иван", phones=["+79990000009"], emails=["ivan9@mail.ru"])
    c = _mk(pool, first_name="Иван", emails=["IVAN9@mail.ru"], company="Ромашка")
    r = _run(bulk_merge(pool, OWNER, [
        {"primary_id": str(a), "secondary_id": str(b)},
        {"primary_id": str(b), "secondary_id": str(c)},
    ]))
    assert r == {"merged": 2, "skipped": 0}, r
    left = _run(pool.fetchval(
        "SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1", OWNER))
    assert left == 1
    assert _row(pool, a)["company"] == "Ромашка"


def test_latin_and_cyrillic_spelling_is_one_name(pool):
    from services.contacts_hub.trust_engine import detect_smart_duplicates
    _mk(pool, first_name="Иван", last_name="Петров", phones=["+79990000010"])
    _mk(pool, first_name="Petrov", last_name="Ivan", phones=["89990000010"])
    pairs = _run(detect_smart_duplicates(pool, OWNER))["duplicates"]
    assert len(pairs) == 1 and "same_name" in pairs[0]["reasons"], pairs


def test_preview_names_what_hangs_on_the_second_record(pool):
    from services.contacts_hub.merge_engine import merge_preview
    a = _mk(pool, first_name="Иван", phones=["+79990000011"])
    b = _mk(pool, telegram_user_id=5550011, phones=["+79990000011"])
    _run(pool.execute(
        "INSERT INTO contact_crm (owner_id, contact_id, stage) VALUES ($1,$2,'contact')",
        OWNER, b))
    p = _run(merge_preview(pool, a, b, OWNER))
    labels = [t["label"] for t in p["taken"]]
    assert "аккаунт Telegram (ID)" in labels, labels
    assert p["attached"]["deal"] == 1
    json.dumps(p)  # ответ экрану обязан сериализоваться
