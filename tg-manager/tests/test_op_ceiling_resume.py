"""Длинная ЗДОРОВАЯ операция не падает по потолку прогона, а продолжается.

Жалоба владельца (скрин операции #605): DM-кампания на 2830 целей шла ровным
безопасным темпом (~0.9/мин, 12%, ETA ~44 ч) и была помечена ОШИБКОЙ по
достижении потолка прогона (360 мин). На этом темпе 2–3 тысячи целей физически
не укладываются в шесть часов — значит, ЛЮБАЯ крупная рассылка обречена падать
на ~12%, хотя ничего не сломано.

Ключ: сторож застоя (90 мин без движения done_items) срабатывает РАНЬШЕ потолка,
если операция встала. Поэтому, дойдя до потолка с ненулевым растущим прогрессом,
операция заведомо здорова — её нужно вернуть в очередь (продолжит с того же
места по operation_log), а не валить.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from services import op_worker


@pytest.mark.asyncio
async def test_healthy_long_op_hits_ceiling_resumes_not_fails(monkeypatch):
    """done_items растёт вплоть до потолка → _OpOverCeiling, а НЕ TimeoutError."""
    counter = {"n": 1}

    async def fake_fetchrow(pool, sql, *args, **kw):
        counter["n"] += 1
        return {"done_items": counter["n"]}

    monkeypatch.setattr(op_worker, "_safe_fetchrow", fake_fetchrow)
    monkeypatch.setattr(op_worker, "_OP_STALL_POLL_S", 0.01)

    async def never_finishes():
        await asyncio.sleep(30)

    with pytest.raises(op_worker._OpOverCeiling):
        await op_worker._run_with_stall_guard(
            None, never_finishes(), 605, "dm_campaign", timeout_s=0.05)


@pytest.mark.asyncio
async def test_truly_stuck_op_still_fails_as_timeout(monkeypatch):
    """done_items ЗАМЕР → настоящий застой: по-прежнему обычный таймаут."""
    async def fake_fetchrow(pool, sql, *args, **kw):
        return {"done_items": 5}  # не двигается

    monkeypatch.setattr(op_worker, "_safe_fetchrow", fake_fetchrow)
    monkeypatch.setattr(op_worker, "_OP_STALL_POLL_S", 0.01)
    monkeypatch.setattr(op_worker, "_OP_STALL_MIN", 0)  # застой считаем мгновенно

    async def never_finishes():
        await asyncio.sleep(30)

    with pytest.raises(asyncio.TimeoutError):
        await op_worker._run_with_stall_guard(
            None, never_finishes(), 606, "dm_campaign", timeout_s=5)


@pytest.mark.asyncio
async def test_ceiling_with_zero_progress_is_a_real_hang(monkeypatch):
    """done==0 за все часы — операция не взяла ни одной цели: это зависание."""
    async def fake_fetchrow(pool, sql, *args, **kw):
        return {"done_items": 0}

    monkeypatch.setattr(op_worker, "_safe_fetchrow", fake_fetchrow)
    monkeypatch.setattr(op_worker, "_OP_STALL_POLL_S", 0.01)

    async def never_finishes():
        await asyncio.sleep(30)

    with pytest.raises(asyncio.TimeoutError):
        await op_worker._run_with_stall_guard(
            None, never_finishes(), 607, "dm_campaign", timeout_s=0.05)


@pytest.mark.asyncio
async def test_resume_requeues_pending_and_does_not_fail(monkeypatch):
    """_resume_op_over_ceiling возвращает в очередь (pending), не помечает ошибкой."""
    executed = {}

    async def fake_execute(pool, sql, *args, log_ctx=""):
        executed["sql"] = sql
        executed["args"] = args
        return "UPDATE 1"

    notified = {}

    async def fake_notify(pool, bot, owner_id, text, *a, **k):
        notified["text"] = text

    metric = {}

    def fake_metric(name, value=1.0, **labels):
        metric["name"] = name

    monkeypatch.setattr(op_worker, "_safe_execute", fake_execute)
    monkeypatch.setattr(op_worker, "_notify_owner_about_op", fake_notify)
    monkeypatch.setattr(op_worker, "_reliability_metric", fake_metric)

    await op_worker._resume_op_over_ceiling(
        None, 605, bot=None, owner_id=7, done_so_far=337, total_items=2830)

    assert "status='pending'" in executed["sql"], "операция должна вернуться в очередь"
    # Провал ставит finished_at=now() и терминальный статус; продолжение — нет
    # (слово failed встречается лишь в WHERE-защите от перезаписи терминальных).
    assert "finished_at" not in executed["sql"], "продолжение не завершает операцию"
    assert "SET status='pending'" in executed["sql"]
    assert metric["name"] == "infragram_op_ceiling_resumes_total"
    # Текст владельцу: «не ошибка», «продолжится автоматически».
    assert "не ошибка" in notified["text"] and "продолж" in notified["text"].lower()


def test_caller_resumes_over_ceiling_instead_of_failing():
    """В _run_op_task потолок здоровой операции ведёт к продолжению, не к провалу.

    _OpOverCeiling и asyncio.TimeoutError — независимые типы исключений, порядок
    except-веток между ними значения не имеет (ни одна не перехватывает другую).
    Важно одно: ветка _OpOverCeiling существует и ведёт к возврату в очередь.
    """
    src = inspect.getsource(op_worker)
    assert "except _OpOverCeiling" in src, "потолок здоровой операции не перехвачен"
    # Тело ветки _OpOverCeiling ведёт к продолжению (_resume_op_over_ceiling),
    # а не к немедленному провалу.
    i_ceiling = src.index("except _OpOverCeiling")
    body = src[i_ceiling:i_ceiling + 3000]
    assert "_resume_op_over_ceiling" in body
