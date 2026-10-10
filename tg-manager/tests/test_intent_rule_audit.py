"""Правила намерений должны быть отлаживаемыми и правимыми.

Два разрыва:
  • у правила был только счётчик hits — голое число. Понять, НА ЧТО именно оно
    сработало, было негде, поэтому ложные срабатывания (классика — фраза
    «готов», ловившая «не готов») оставались невидимыми: контакт молча уезжал
    не в ту стадию, а оператор видел лишь растущий счётчик;
  • правки правила не было вовсе: чтобы исправить опечатку во фразе, правило
    удаляли и создавали заново — вместе со счётчиком и журналом, то есть теряя
    ровно ту историю, по которой правило и настраивают.
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest

from services import intent_sensor as S

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_API = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
_UI = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


class _Pool:
    def __init__(self, row=None, result="UPDATE 1"):
        self._row = row
        self._result = result
        self.executed = []

    async def fetchrow(self, q, *a):
        return self._row

    async def execute(self, q, *a):
        self.executed.append((q, a))
        return self._result

    async def fetch(self, q, *a):
        return []


def _cur(stage="lead", tag="тег"):
    return {"stage": stage, "tag": tag}


# ── Правка правила ────────────────────────────────────────────────────────────

def test_update_changes_phrase():
    pool = _Pool(_cur())
    ok = asyncio.run(S.update_rule(pool, 1, 5, phrase="Новая Фраза"))
    assert ok
    q, args = pool.executed[0]
    assert "phrase=$1" in q
    assert args[0] == "новая фраза", "обычная фраза приводится к нижнему регистру"


def test_update_keeps_regexp_case():
    """Понижение регистра ломает экранированные классы (\\B → \\b)."""
    pool = _Pool(_cur())
    asyncio.run(S.update_rule(pool, 1, 5, phrase=r"re:\B\d+"))
    assert pool.executed[0][1][0] == r"re:\B\d+"


def test_update_rejects_broken_regexp():
    with pytest.raises(ValueError):
        asyncio.run(S.update_rule(_Pool(_cur()), 1, 5, phrase="re:[unclosed"))


def test_update_rejects_empty_phrase():
    with pytest.raises(ValueError):
        asyncio.run(S.update_rule(_Pool(_cur()), 1, 5, phrase="   "))


def test_update_rejects_nothing_to_change():
    with pytest.raises(ValueError):
        asyncio.run(S.update_rule(_Pool(_cur()), 1, 5))


def test_cannot_strip_rule_of_all_actions():
    """Правило без стадии и тега ничего не делает — это молчаливая поломка."""
    with pytest.raises(ValueError):
        asyncio.run(S.update_rule(_Pool(_cur(stage="lead", tag=None)), 1, 5, stage=None))


def test_final_state_is_validated_not_just_the_payload():
    """Снять тег можно, если стадия остаётся, — проверяется ИТОГ, а не поле."""
    ok = asyncio.run(S.update_rule(_Pool(_cur(stage="proposal", tag="t")), 1, 5, tag=None))
    assert ok


def test_unknown_stage_is_normalised_to_none():
    pool = _Pool(_cur(stage="lead", tag="есть-тег"))
    asyncio.run(S.update_rule(pool, 1, 5, stage="не-стадия"))
    assert pool.executed[0][1][0] is None


def test_update_is_owner_scoped():
    pool = _Pool(_cur())
    asyncio.run(S.update_rule(pool, 42, 5, notify=False))
    q, args = pool.executed[0]
    assert "owner_id=$" in q and args[-1] == 42 and args[-2] == 5


def test_missing_rule_returns_false():
    assert asyncio.run(S.update_rule(_Pool(None), 1, 5, notify=True)) is False


# ── Журнал срабатываний ───────────────────────────────────────────────────────

def test_hits_journal_fails_open_when_table_is_missing():
    """Журнал появился позже правил — лаг миграции не должен ронять экран."""
    class _Broken(_Pool):
        async def fetch(self, q, *a):
            raise RuntimeError('relation "vault_intent_hits" does not exist')

    assert asyncio.run(S.recent_hits(_Broken(), 1)) == []


def test_scan_writes_the_journal():
    import ast
    src = (_ROOT / "services" / "intent_sensor.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "scan_incoming":
            body = ast.get_source_segment(src, node)
            assert "vault_intent_hits" in body
            # Сбой журнала не должен ломать саму обработку намерения.
            assert "except Exception" in body
            return
    raise AssertionError("scan_incoming не найдена")


def test_schema_file_exists_with_cascade():
    sql = (_ROOT / "schema_v194_intent_hits.sql").read_text(encoding="utf-8")
    assert "vault_intent_hits" in sql
    assert "ON DELETE CASCADE" in sql, "журнал удалённого правила не должен оставаться сиротой"
    assert "idx_intent_hits_owner_time" in sql, "выборка последних без индекса — скан таблицы"


# ── Доведено до пользователя ──────────────────────────────────────────────────

def test_endpoints_registered():
    assert 'add_patch("/api/miniapp/uch/intent/rule/{rule_id}"' in _API
    assert '"/api/miniapp/uch/intent/hits"' in _API


def test_update_endpoint_distinguishes_absent_field_from_empty():
    """None для stage/tag — осмысленное «снять», а не «не прислали»."""
    start = _API.index("async def uch_intent_rule_update")
    body = _API[start:start + 2200]
    assert '"stage" in body' in body and '"tag" in body' in body


def test_ui_has_edit_and_journal():
    assert "editIntentRule(" in _UI and "cancelIntentEdit" in _UI
    assert "openIntentHits(" in _UI
    assert "method:'PATCH'" in _UI


def test_ui_resolves_rule_from_cache_not_from_attribute():
    assert "CUR_INTENT_RULES" in _UI
    assert "editIntentRule(${JSON.stringify" not in _UI
