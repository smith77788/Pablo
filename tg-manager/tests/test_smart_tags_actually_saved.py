"""Умные теги реально сохраняются, а счётчик не врёт.

Фича не работала НИКОГДА, и это доказано на живом Postgres (28.09.2026):

1. `rule_id` в `contact_smart_tags` — INTEGER, а движок писал туда строку
   («builtin_Premium» у встроенного правила, «5» у своего). asyncpg такое
   связывание отвергает, ошибку глотал `except Exception` — ни одна метка не
   доходила до базы, экран вечно показывал «Нет тегов».
2. `conditions` приходят из JSONB строкой. `apply_smart_tags` их не разбирал,
   и первое же СВОЁ правило роняло применение целиком: AttributeError, 500.
3. `phones`/`emails` — тоже JSONB, то есть строка «[]». Проверка «не пусто»
   видела непустую строку и вешала метку «есть телефон» на каждый контакт.
4. `source_accounts_count` в таблице нет — правило «есть у нескольких
   аккаунтов» не совпадало ни с чем.
5. `applied` считал попытки вставки, а не строки: повторный запуск сообщал
   то же число, хотя не добавилось ничего.

Заглушка пула в проекте типы НЕ проверяет (см. CLAUDE.md), поэтому здесь
пул специально строгий — он падает на тех же связываниях, что и asyncpg.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from services.contacts_hub.smart_tags_engine import (
    BUILTIN_RULES,
    apply_smart_tags,
    get_smart_tag_rules,
)

OWNER = 77
CID_A = uuid.uuid4()   # premium, есть телефон, два аккаунта-источника
CID_B = uuid.uuid4()   # обычный, без телефона


class StrictPool:
    """Пул, повторяющий строгость asyncpg к типам параметров."""

    def __init__(self, rules):
        self._rules = rules
        self.saved = []          # (contact_id, tag, source, rule_id)
        self.insert_calls = 0

    # -- то, что проверял бы драйвер ---------------------------------
    @staticmethod
    def _check_int_or_none(value, where):
        if value is None:
            return
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(
                f"invalid input for query argument {where}: {value!r} "
                "('str' object cannot be interpreted as an integer)")

    async def fetch(self, sql, *args):
        low = " ".join(sql.split()).lower()
        if "from smart_tag_rules" in low:
            return list(self._rules)
        if "from unified_contacts" in low:
            has_cnt = "source_accounts_count" in low
            rows = [
                {"id": CID_A, "owner_id": OWNER, "is_premium": True,
                 "is_favorite": False, "phones": '["+79990000001"]',
                 "emails": "[]", "notes": "", "company": None,
                 "first_name": "Пётр"},
                {"id": CID_B, "owner_id": OWNER, "is_premium": False,
                 "is_favorite": False, "phones": "[]", "emails": "[]",
                 "notes": "", "company": None, "first_name": "Анна"},
            ]
            if has_cnt:
                rows[0]["source_accounts_count"] = 2
                rows[1]["source_accounts_count"] = 1
            return rows
        if "insert into contact_smart_tags" in low:
            self.insert_calls += 1
            _owner, cids, tags, srcs, rids = args
            added = []
            for cid, tag, src, rid in zip(cids, tags, srcs, rids):
                self._check_int_or_none(rid, "$5")
                if (cid, tag) in {(s[0], s[1]) for s in self.saved}:
                    continue            # ON CONFLICT DO NOTHING
                self.saved.append((cid, tag, src, rid))
                added.append({"?column?": 1})
            return added
        return []

    async def execute(self, sql, *args):
        low = " ".join(sql.split()).lower()
        if "insert into contact_smart_tags" in low:
            self.insert_calls += 1
            _owner, cid, tag, src, _conf, rid = args
            self._check_int_or_none(rid, "$6")
            if (cid, tag) not in {(s[0], s[1]) for s in self.saved}:
                self.saved.append((cid, tag, src, rid))
                return "INSERT 0 1"
            return "INSERT 0 0"
        return "OK"

    async def fetchval(self, sql, *args):
        return None

    async def fetchrow(self, sql, *args):
        return None


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _custom_rule(**over):
    row = {"id": 5, "owner_id": OWNER, "name": "Мой", "rule_type": "field_match",
           "conditions": json.dumps({"field": "first_name", "op": "contains",
                                     "value": "Ан"}),
           "tag": "анна", "is_active": True}
    row.update(over)
    return row


def test_tags_reach_the_database():
    """Главное: после применения метки ЕСТЬ в базе, а не только в ответе."""
    pool = StrictPool([_custom_rule()])
    res = _run(apply_smart_tags(pool, OWNER))
    assert pool.saved, "ни одна метка не сохранилась — фича мертва"
    assert res["applied"] == len(pool.saved), (
        "счётчик разошёлся с базой: %s против %s" % (res["applied"], len(pool.saved)))


def test_builtin_rule_id_is_not_a_string():
    """У встроенного правила нет числового id — в INTEGER идёт NULL."""
    pool = StrictPool([])
    _run(apply_smart_tags(pool, OWNER))
    assert pool.saved, "нечего проверять: встроенные правила ничего не записали"
    for _cid, tag, src, rid in pool.saved:
        assert rid is None or isinstance(rid, int), (tag, rid)
        assert src in ("builtin", "rule")


def test_own_rule_does_not_break_everything():
    """conditions приходят строкой; своё правило должно работать, а не ронять всё."""
    pool = StrictPool([_custom_rule()])
    _run(apply_smart_tags(pool, OWNER))
    tags = {t for _c, t, _s, _r in pool.saved}
    assert "анна" in tags, "своё правило не сработало"


def test_empty_json_list_is_empty():
    """«[]» из JSONB — это пусто. Иначе «есть телефон» у всех подряд."""
    pool = StrictPool([])
    _run(apply_smart_tags(pool, OWNER))
    with_phone = {c for c, t, _s, _r in pool.saved if t == "has_phone"}
    assert with_phone == {CID_A}, (
        "метка «есть телефон» проставлена не тем: %s" % (with_phone,))


def test_multi_account_rule_is_not_dead():
    """source_accounts_count нужно посчитать запросом, иначе правило пустое."""
    pool = StrictPool([])
    _run(apply_smart_tags(pool, OWNER))
    multi = {c for c, t, _s, _r in pool.saved if t == "multi_account"}
    assert multi == {CID_A}, "правило «у нескольких аккаунтов» не сработало"


def test_second_run_reports_zero_new():
    """Повторный запуск ничего не добавляет и честно говорит об этом."""
    pool = StrictPool([_custom_rule()])
    first = _run(apply_smart_tags(pool, OWNER))
    saved_after_first = len(pool.saved)
    second = _run(apply_smart_tags(pool, OWNER))
    assert first["applied"] > 0
    assert second["applied"] == 0, "повторный запуск приписал себе чужую работу"
    assert second["matched"] == first["matched"]
    assert len(pool.saved) == saved_after_first


def test_writes_are_batched():
    """Вставка пачками: экран не должен ждать по запросу на каждое совпадение."""
    pool = StrictPool([])
    res = _run(apply_smart_tags(pool, OWNER))
    assert res["matched"] >= 3
    assert pool.insert_calls <= 2, (
        "на %s совпадений ушло %s обращений к базе" % (res["matched"], pool.insert_calls))


def test_builtin_rules_are_named_in_russian():
    """Владелец не читает по-английски — встроенные правила подписаны по-русски."""
    for r in BUILTIN_RULES:
        assert r.get("code"), "у встроенного правила нет стабильного кода"
        assert any("а" <= ch.lower() <= "я" for ch in r["name"]), (
            "английское имя правила в интерфейсе: %s" % r["name"])


def test_rule_id_key_survives_rename():
    """id встроенного правила строится по code, а не по показываемому имени."""
    pool = StrictPool([])
    rules = _run(get_smart_tag_rules(pool, OWNER))
    ids = {r["id"] for r in rules if r.get("source") == "builtin"}
    assert "builtin_premium" in ids, ids
