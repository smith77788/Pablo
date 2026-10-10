"""Авто-блокировка виртуального администратора канала.

Раньше редактор был чисто советующим: показывал замечания на предпросмотре, но
опубликовать можно было что угодно, а автономная публикация (без человека) не
проверялась вовсе. Владелец включить блокировку не мог — режим автономности
жил в БД, но UI его не отдавал, не сохранял, а исполнитель массовой публикации
о нём не знал.

Здесь проверяется вся цепочка авто-блокировки:
  ядро решения (channel_brain.enforce_decision) →
  обёртка с БД fail-open (editorial_review.autonomous_block) →
  встройка в исполнитель (op_worker._exec_mass_publish) →
  проводка режима через API (to_public / validate_policy / save) и UI.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import channel_brain as cb  # noqa: E402
from services import channel_brain_store as store  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _verdict(reasons=(), brand_violations=()):
    return cb.EditorialVerdict(
        ok=not reasons,
        needs_review=bool(reasons),
        reasons=list(reasons),
        repetition={},
        brand={"violations": list(brand_violations)},
    )


# ── ядро решения: чистая функция ─────────────────────────────────────────────


def test_clean_post_is_never_blocked():
    for mode in cb.AUTONOMY_MODES + ("мусор", "", None):
        block, why = cb.enforce_decision(mode, _verdict())
        assert block is False and why == []


def test_manual_never_blocks_even_dirty():
    v = _verdict(reasons=["почти повторяет недавний пост"],
                 brand_violations=[{"code": "forbidden_word", "detail": "казино"}])
    block, why = cb.enforce_decision("manual", v)
    assert block is False and why == []


def test_autonomous_blocks_any_reason():
    v = _verdict(reasons=["начинается так же, как недавний пост"])
    block, why = cb.enforce_decision("autonomous", v)
    assert block is True
    assert why == ["начинается так же, как недавний пост"]


def test_semi_blocks_hard_brand_violation():
    v = _verdict(reasons=["запрещённое слово: казино"],
                 brand_violations=[{"code": "forbidden_word", "detail": "казино"}])
    block, why = cb.enforce_decision("semi", v)
    assert block is True and why


def test_semi_passes_a_mere_repeat():
    """semi блокирует явный брак, но повтор — это мягкое замечание, не блок."""
    v = _verdict(reasons=["почти повторяет недавний пост (совпадает на 88%)"])
    block, why = cb.enforce_decision("semi", v)
    assert block is False and why == []


def test_unknown_mode_falls_back_to_manual():
    """Опечатка в режиме не должна ВКЛЮЧИТЬ блокировку — только осознанный выбор."""
    v = _verdict(reasons=["что угодно"],
                 brand_violations=[{"code": "forbidden_word", "detail": "x"}])
    assert cb.enforce_decision("autonom", v) == (False, [])
    assert cb.enforce_decision(None, v) == (False, [])


def test_none_verdict_is_safe():
    assert cb.enforce_decision("autonomous", None) == (False, [])


# ── обёртка с БД: fail-open ──────────────────────────────────────────────────


class _Brain:
    def __init__(self, mode):
        self.autonomy_mode = mode
        self.brand_rules = cb.BrandRules(forbidden_words=("казино",))
        self.dup_threshold = 0.6
        self.pillars = []
        self.mix_weights = {}


def test_manual_mode_does_no_work(monkeypatch):
    """В manual-режиме обёртка не должна даже читать историю постов."""
    from services import editorial_review as er

    async def _prof(pool, owner, key):
        return _Brain("manual")

    monkeypatch.setattr(store, "get_profile", _prof)

    def _boom(*a, **k):
        raise AssertionError("история читаться не должна в manual")

    monkeypatch.setattr(er, "review_draft", _boom)
    block, why = asyncio.run(er.autonomous_block(None, 1, "текст"))
    assert block is False and why == []


def test_autonomous_blocks_forbidden_word_end_to_end(monkeypatch):
    from services import editorial_review as er

    async def _prof(pool, owner, key):
        return _Brain("autonomous")

    async def _recent(*a, **k):
        return []

    monkeypatch.setattr(store, "get_profile", _prof)
    monkeypatch.setattr(er.content_memory, "recent_texts_for_owner", _recent, raising=False)
    monkeypatch.setattr(er.content_memory, "recent_texts", _recent, raising=False)

    block, why = asyncio.run(er.autonomous_block(None, 1, "Заходите в казино прямо сейчас"))
    assert block is True and why


def test_db_failure_never_blocks(monkeypatch):
    """Блокировка рушит легитимную публикацию — сбой проверки блокировать не смеет."""
    from services import editorial_review as er

    async def _boom(pool, owner, key):
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(store, "get_profile", _boom)
    block, why = asyncio.run(er.autonomous_block(None, 1, "любой текст"))
    assert block is False and why == []


# ── встройка в исполнитель ───────────────────────────────────────────────────


def test_mass_publish_calls_the_gate():
    src = _read("services/op_worker.py")
    assert "autonomous_block(pool, owner_id, mp_text" in src, (
        "исполнитель массовой публикации не спрашивает редактора"
    )
    # блокировка возвращает failed с причиной, а не молча публикует
    i = src.index("autonomous_block(pool, owner_id, mp_text")
    seg = src[i:i + 700]
    assert '"status": "failed"' in seg
    assert "виртуальным администратором" in seg


def test_gate_runs_after_content_safety_not_before():
    """content_safety (запрещённый контент) — базовый предохранитель и должен
    стоять первым; редактор про качество, он ниже."""
    src = _read("services/op_worker.py")
    assert src.index("content_safety") < src.index("autonomous_block(pool")


# ── проводка режима: API и UI ────────────────────────────────────────────────


def test_public_policy_exposes_the_mode():
    assert store.to_public(None)["autonomy_mode"] == "manual"
    assert store.to_public(_Brain("autonomous"))["autonomy_mode"] == "autonomous"


def test_validate_accepts_known_modes_and_rejects_junk():
    clean, errors = store.validate_policy({"autonomy_mode": "semi"})
    assert clean.get("autonomy_mode") == "semi" and not errors
    clean2, errors2 = store.validate_policy({"autonomy_mode": "включи всё"})
    assert "autonomy_mode" not in clean2 and errors2


def test_validate_without_mode_leaves_it_untouched():
    """Запрос без режима политику режима не трогает (обрабатывает вызывающий)."""
    clean, errors = store.validate_policy({"max_emoji": 3})
    assert "autonomy_mode" not in clean and not errors


def test_save_endpoint_applies_the_new_mode():
    api = _read("services/mini_app_api.py")
    assert 'clean.get("autonomy_mode"' in api, (
        "save по-прежнему затирает режим прежним — включить блокировку нельзя"
    )


def test_ui_lets_the_owner_choose_the_mode():
    js = _read("mini_app/screens/editorial.js")
    assert "id=\"edMode\"" in js or "id='edMode'" in js
    assert "autonomy_mode" in js, "выбор режима не отправляется на сервер"
    for val in ("manual", "semi", "autonomous"):
        assert val in js, f"в UI нет режима {val}"
