"""Инвайт: не терять цель при перегреве и не терять аккаунты из-за прав.

Оба разрыва найдены на живом прогоне (203/380 за 2ч 7м, флот 32 аккаунта):

1. «🛑 Флот перегрет (5 флудов подряд) — операция остановлена» И при этом
   продолжение НЕ планировалось: остаток 177 целей исчезал вместе с операцией,
   хотя отчёт сам советовал «дайте отдохнуть и повторите позже». PeerFlood и
   FloodWait — временные лимиты, они снимаются отдыхом, в отличие от закрытой
   группы или поголовно мёртвых сессий.

2. «🚫 3 аккаунтов без прав админа выведены из круга» — выдача прав «на лету»
   допускалась ровно ОДИН раз на аккаунт (анти-цикл был множеством). Одна
   осечка по таймауту/флуду промоутера стоила аккаунта на весь прогон.
"""
from __future__ import annotations

import ast
import pathlib

from services import invite_recovery as IR

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_OPW_SRC = (_ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


def _func_src(name: str) -> str:
    tree = ast.parse(_OPW_SRC)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(_OPW_SRC, node) or ""
    raise AssertionError(f"функция {name} не найдена")


def _cont(**kw):
    base = dict(left=100, group_broken=False, all_failed_connect=False,
                chain=0, max_chain=5, flood_storm=False, ok_count=203)
    base.update(kw)
    return IR.should_schedule_continuation(**base)


# ── 1. Перегрев больше не отменяет продолжение ─────────────────────────────

def test_flood_storm_after_progress_no_longer_cancels_continuation():
    """Главный регресс живого прогона: 203 добавлено, 5 флудов — флот упёрся в
    потолок, а остаток 177 целей исчезал вместе с операцией."""
    ok, why = _cont(flood_storm=True, ok_count=203)
    assert ok is True
    assert "отдохн" in why


def test_flood_storm_without_any_success_still_blocks():
    """Встали сразу, никого не добавив — это флагнутый чат/аудитория, а не
    усталость: повтор жёг бы аккаунты. Граница сохранена намеренно."""
    ok, why = _cont(flood_storm=True, ok_count=0)
    assert ok is False
    assert "сожжёт" in why or "флагнут" in why


def test_normal_run_schedules_continuation():
    assert _cont()[0] is True


def test_closed_group_still_blocks_continuation():
    """Здесь повтор бесполезен: завтра группа будет так же недоступна."""
    ok, why = _cont(group_broken=True)
    assert ok is False and "групп" in why.lower()


def test_all_dead_sessions_still_block_continuation():
    ok, why = _cont(all_failed_connect=True)
    assert ok is False and "подключ" in why


def test_chain_limit_still_blocks():
    ok, why = _cont(chain=5, max_chain=5)
    assert ok is False and "продолжен" in why


def test_nothing_left_means_nothing_to_schedule():
    assert _cont(left=0)[0] is False


def test_storm_does_not_override_a_real_blocker():
    """Перегрев + закрытая группа: продолжать всё равно нельзя."""
    assert _cont(flood_storm=True, group_broken=True)[0] is False
    assert _cont(flood_storm=True, all_failed_connect=True)[0] is False


# ── 2. Права выдаются не с одной попытки ───────────────────────────────────

def test_more_than_one_promote_attempt_allowed():
    """Раньше попытка была одна — сетевая осечка стоила аккаунта на весь прогон."""
    assert IR.promote_retry_allowed(0) is True
    assert IR.promote_retry_allowed(1) is True
    assert IR.MAX_PROMOTE_ATTEMPTS >= 2


def test_promote_attempts_are_capped():
    """Потолок обязателен: аккаунту, которому права выдать нельзя в принципе,
    нельзя долбиться бесконечно."""
    assert IR.promote_retry_allowed(IR.MAX_PROMOTE_ATTEMPTS) is False
    assert IR.promote_retry_allowed(99) is False


def test_promote_retry_handles_garbage_counter():
    assert IR.promote_retry_allowed(None) is True
    assert IR.promote_retry_allowed("x") is True


# ── Проводка в исполнителе ─────────────────────────────────────────────────

def test_executor_uses_the_continuation_decision():
    src = _func_src("_exec_mass_invite")
    assert "should_schedule_continuation" in src
    # Старое жёсткое условие «не продолжать при перегреве» должно исчезнуть.
    assert "not flood_storm\n            and not _all_failed_connect" not in src


def test_executor_counts_promote_attempts_not_just_membership():
    src = _func_src("_exec_mass_invite")
    assert "_irec_promote_retry_allowed" in src
    # Счётчик, а не множество: dict.get(...) + инкремент.
    assert "_no_rights_on_demand.get(acc_id, 0) + 1" in src
    assert "_no_rights_on_demand.add(" not in src


def test_storm_message_no_longer_advises_a_manual_repeat():
    """Продолжение теперь планируется само — совет «повторите позже» был бы
    ложным указанием пользователю делать то, что делает система."""
    src = _func_src("_exec_mass_invite")
    # Точная пользовательская строка старого совета (в комментариях рядом она
    # цитируется другими словами — проверяем именно выдаваемый текст).
    assert "Дайте им отдохнуть и повторите позже" not in src
    assert "снимается отдыхом" in src
