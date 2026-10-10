"""Сенсор намерений (Vault → CRM): фраза во входящем ЛС → стадия + тег + алерт.

Проверяем ядро scan_incoming через фейковый pool: матч правил, движение стадии,
добавление тегов, уведомление только при реальном изменении, идемпотентность.
"""
from __future__ import annotations

import asyncio

from services import intent_sensor as ins


class _FakePool:
    """Мини-эмуляция: контакт по tg_id, его теги и CRM-стадия."""
    def __init__(self, rules, contact_id="c-1", tags=None, stage=None):
        self._rules = rules
        self._cid = contact_id
        self._tags = list(tags or [])
        self._stage = stage
        self.crm_upserts = []
        self.tag_updates = []

    async def fetch(self, sql, *a):
        if "vault_intent_rules" in sql:
            return [dict(r) for r in self._rules]
        return []

    async def fetchrow(self, sql, *a):
        if "unified_contacts WHERE owner_id" in sql:
            return {"id": self._cid}
        if "SELECT tags FROM unified_contacts" in sql:
            return {"tags": self._tags}
        return None

    async def fetchval(self, sql, *a):
        if "stage FROM contact_crm" in sql:
            return self._stage
        return None

    async def execute(self, sql, *a):
        if "UPDATE unified_contacts SET tags" in sql:
            self.tag_updates.append(a)
            for t in a[1]:
                if t not in self._tags:
                    self._tags.append(t)
        return "UPDATE 1"


def _rule(phrase, stage=None, tag=None, notify=True, rid=1):
    return {"id": rid, "phrase": phrase, "stage": stage, "tag": tag, "notify": notify}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _peer():
    return {"peer_user_id": 555, "peer_name": "Иван", "peer_username": "ivan"}


def test_match_moves_stage_and_tags_and_notifies():
    pool = _FakePool([_rule("цена", stage="proposal", tag="интерес")], stage="lead")
    sent = []
    async def notifier(owner, msg): sent.append(msg)
    res = _run(ins.scan_incoming(pool, None, 1, _peer(), "а какая цена?", notifier=notifier))
    assert res["matched"] == 1
    assert res["stage"] == "proposal"
    assert res["tags"] == ["интерес"]
    assert res["notified"] is True and sent


def test_no_rules_no_action():
    pool = _FakePool([])
    res = _run(ins.scan_incoming(pool, None, 1, _peer(), "какая цена?"))
    assert res["matched"] == 0


def test_no_match_no_action():
    pool = _FakePool([_rule("купить", stage="won")])
    res = _run(ins.scan_incoming(pool, None, 1, _peer(), "привет, как дела"))
    assert res["matched"] == 0


def test_idempotent_no_renotify_when_already_applied():
    # уже в целевой стадии и с тегом → не уведомляем повторно
    pool = _FakePool([_rule("цена", stage="proposal", tag="интерес")],
                     tags=["интерес"], stage="proposal")
    sent = []
    async def notifier(owner, msg): sent.append(msg)
    res = _run(ins.scan_incoming(pool, None, 1, _peer(), "цена?", notifier=notifier))
    assert res["matched"] == 1
    assert res["tags"] == [] and res["stage"] is None
    assert res["notified"] is False and not sent


def test_most_advanced_stage_wins():
    rules = [_rule("цена", stage="proposal", tag="a", rid=1),
             _rule("готов", stage="negotiation", tag="b", rid=2)]
    pool = _FakePool(rules, stage="lead")
    res = _run(ins.scan_incoming(pool, None, 1, _peer(), "цена ок, готов купить", notifier=lambda *a: None))
    # negotiation «продвинутее» proposal
    assert res["stage"] == "negotiation"
    assert set(res["tags"]) == {"a", "b"}


def test_add_rule_rejects_empty_action():
    class _P:
        async def fetchval(self, *a): return 1
    try:
        _run(ins.add_rule(_P(), 1, "цена", None, None))
        assert False, "должно упасть — правило без действия"
    except ValueError:
        pass
