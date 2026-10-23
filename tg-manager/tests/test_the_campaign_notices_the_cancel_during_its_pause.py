"""«Отменить» во время паузы рассылки обязана сработать, а не дождаться конца.

Кампания ЛС спит в своём цикле: пейсинг между целями, тихие часы, флуд-паузы
до пяти минут. Отмена доходит до исполнителя опросом на витке цикла, а сон был
один и целиком — поэтому владелец, нажавший «Отменить», всю паузу видел
рассылку работающей, а аккаунты оставались арендованными под отменённую
операцию. Ровно тот разрыв, который для пауз ВНУТРИ прогона уже закрыт
`_pause_watching_for_cancel` (см. его разбор), только между целями.

Вторая половина — сторож застоя. Дробная пауза снимает объявление за собой, а
`done_items` после паузы двинется только по завершении следующей цели. Тик
сторожа, попавший в этот зазор, оборвал бы здоровый прогон, поэтому по
возвращении объявляется буфер на один виток опроса.
"""
from __future__ import annotations

import ast
import asyncio
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _func_src(rel: str, name: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        src = f.read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{name} пропал из {rel}")


@pytest.fixture
def _instant(monkeypatch):
    """Шаги паузы — 15 секунд: тест не должен их пересиживать по-настоящему."""
    slept: list[float] = []

    async def _fake(sec):
        slept.append(float(sec))

    monkeypatch.setattr(asyncio, "sleep", _fake)
    return slept


@pytest.fixture
def _declared(monkeypatch):
    """Что именно объявлено сторожу застоя и в каком порядке."""
    from services import op_worker

    calls: list[float] = []
    _real = op_worker.declare_op_pause

    def _spy(op_id, seconds: float):
        calls.append(float(seconds))
        return _real(op_id, seconds)

    monkeypatch.setattr(op_worker, "declare_op_pause", _spy)
    return calls


# ── Пауза исполнителя ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_cancelled_operation_does_not_sit_out_its_pause(_instant, monkeypatch):
    from services import op_worker

    async def _cancelled(_pool, _op_id):
        return True

    monkeypatch.setattr(op_worker, "_is_cancelled", _cancelled)
    waited = await op_worker.cancellable_pause(object(), 77, 300.0, "test")
    assert waited < 300.0, (
        "пауза пересижена целиком: владелец ждёт отмены до пяти минут, и всё "
        "это время аккаунты числятся занятыми")


@pytest.mark.asyncio
async def test_a_live_operation_waits_the_whole_pause(_instant, monkeypatch):
    """Укоротить паузу живой операции нельзя: на флуде она и есть защита."""
    from services import op_worker

    async def _not_cancelled(_pool, _op_id):
        return False

    monkeypatch.setattr(op_worker, "_is_cancelled", _not_cancelled)
    waited = await op_worker.cancellable_pause(object(), 78, 45.0, "test")
    assert waited == pytest.approx(45.0)
    assert sum(_instant) == pytest.approx(45.0)


@pytest.mark.asyncio
async def test_the_watchdog_is_told_about_the_pause_and_about_the_gap_after_it(
        _instant, _declared, monkeypatch):
    from services import op_worker

    async def _not_cancelled(_pool, _op_id):
        return False

    monkeypatch.setattr(op_worker, "_is_cancelled", _not_cancelled)
    await op_worker.cancellable_pause(object(), 79, 120.0, "test")

    assert _declared, "пауза не объявлена сторожу — прогон будет обрублен как застой"
    assert _declared[0] > 120.0, (
        "объявлено ровно по длине сна: тик сторожа в зазоре после паузы "
        "оборвёт здоровый прогон")
    assert _declared[-1] > 0, "буфер после паузы не объявлен заново"
    assert op_worker._declared_pause_until.get(79, 0.0) > 0, (
        "по возвращении объявления нет вовсе — сторож судит по done_items, "
        "который двинется только после следующей цели")
    op_worker._declared_pause_until.pop(79, None)


@pytest.mark.asyncio
async def test_a_pause_without_an_operation_is_not_an_error(_instant):
    from services import op_worker

    assert await op_worker.cancellable_pause(object(), None, 10.0) == 0.0
    assert await op_worker.cancellable_pause(object(), 80, 0) == 0.0


# ── Проводка в кампании ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_campaign_pause_goes_through_the_cancellable_one(_instant, monkeypatch):
    from services import dm_engine, op_worker

    seen: list[tuple] = []

    async def _spy(pool, op_id, seconds, where=""):
        seen.append((op_id, seconds, where))
        return float(seconds)

    monkeypatch.setattr(op_worker, "cancellable_pause", _spy)
    await dm_engine._guarded_sleep(42, 7.5, pool=object())
    assert seen == [(42, 7.5, "dm_campaign")], seen
    assert not _instant, "пауза прошла ещё и обычным сном — ждали дважды"


@pytest.mark.asyncio
async def test_without_a_pool_the_pause_still_declares_itself(_instant, _declared):
    """Fail-open: нет pool — спим как раньше, но сторож всё равно предупреждён."""
    from services import dm_engine

    await dm_engine._guarded_sleep(43, 5.0)
    assert _instant == [5.0]
    assert _declared and _declared[0] > 5.0


def test_every_pause_in_the_campaign_is_cancellable():
    """Храповик: новая пауза без pool вернёт прежнее поведение молча."""
    body = _func_src("services/dm_engine.py", "run_campaign")
    calls = [m for m in ast.walk(ast.parse(body.strip()))
             if isinstance(m, ast.Call)
             and getattr(m.func, "id", "") == "_guarded_sleep"]
    assert len(calls) >= 5, f"найдено пауз: {len(calls)} — разбор сломан"
    bad = [c.lineno for c in calls
           if not any(k.arg == "pool" for k in c.keywords)]
    assert not bad, (
        "пауза без pool не замечает отмену (строки внутри run_campaign: "
        f"{bad})")
