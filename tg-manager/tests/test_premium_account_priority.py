"""Регресс: Premium-аккаунты — мягкий приоритет в выборе, не переопределение trust.

Первопричина: tg_accounts.is_premium уже существовал (schema_v160, заполняется
при обычной health-check — get_me() уже вызывается, лишней нагрузки на
Telegram нет) для риск-движка, но нигде не читался при ВЫБОРЕ аккаунтов —
классический разрыв «записано, но не подключено».

Осознанно НЕ заменяет trust_score как первичный сигнал: is_premium —
неподтверждённый платформой сигнал (популярное наблюдение, не задокументированное
поведение Telegram), тогда как trust_score считается по РЕАЛЬНЫМ исходам
(флуд/баны). Поэтому premium — второй ключ сортировки / малый бонус (по
величине как _recency_penalty, вдвое меньше), сдвигающий выбор только среди
близких по trust аккаунтов, а не переопределяющий существенную разницу.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_is_premium_column_predates_this_change():
    """Колонка уже существовала (schema_v160) — эта работа не добавляет новый
    сбор данных, только подключает уже собранный сигнал к выбору."""
    sql = _read("schema_v160.sql")
    assert "is_premium" in sql


def test_select_all_active_orders_by_premium_after_trust():
    src = _read("services/resource_selector.py")
    assert "a.is_premium" in src
    # is_premium — ВТОРОЙ ключ, после trust_score, не вместо него
    order_line = [ln for ln in src.splitlines() if "ORDER BY a.trust_score" in ln][0]
    assert "a.trust_score DESC" in order_line
    assert order_line.index("a.trust_score") < order_line.index("a.is_premium")


def test_get_best_account_selects_is_premium_column():
    src = _read("services/flood_engine.py")
    seg = src[src.index("async def get_best_account"):]
    assert "a.is_premium" in seg[:seg.index("ORDER BY")]


def test_premium_bonus_magnitude_bounded_like_recency_penalty():
    """Бонус не должен быть способен в одиночку перебить существенную разницу
    trust_score — сверяем порядок величины с уже принятым _recency_penalty
    (max_penalty=0.08, из его же докстринга: 'мала относительно разницы
    trust/risk')."""
    src = _read("services/flood_engine.py")
    seg = src[src.index("for row in pool_rows:"):]
    seg = seg[:seg.index("best = best or")]
    assert "is_premium" in seg
    assert "combined -= 0.04" in seg, "бонус должен быть небольшой константой (см. докстринг рядом)"


def test_premium_bonus_applied_only_when_true_null_safe():
    """is_premium NULL ('не проверяли', см. schema_v160) не должен давать бонус
    и не должен падать на None."""
    src = _read("services/flood_engine.py")
    seg = src[src.index("for row in pool_rows:"):]
    seg = seg[:seg.index("best = best or")]
    assert 'if row.get("is_premium"):' in seg  # .get() → None не выбрасывает, falsy не даёт бонус
