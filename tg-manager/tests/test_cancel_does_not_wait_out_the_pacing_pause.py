"""Отмена не ждёт конца ПЕЙСИНГОВОЙ паузы — иначе флот стоит до двадцати минут.

РАЗРЫВ. Флуд-паузы отмену уже слышат (`bounded_flood_sleep` +
`tests/test_cancel_does_not_wait_out_the_flood_pause.py`). А собственный темп
исполнителей — пауза между группами, между ботами, перед постом — оставался
одним `asyncio.sleep` на всю длину. Длины там не секунды:

* `_exec_niche_growth_post` — 10–20 минут между группами (и это ещё умножается
  губернатором, множитель ≥1), 3–8 минут между вступлением и постом, 5–10 минут
  после флуда;
* `_exec_global_presence_bot`, `_exec_bot_factory`, `_exec_gp_bulk_apply` —
  от получаса секунд до трёх минут между реальными действиями.

Всё это время отмена не замечается: владелец нажал «Отменить», операция
показана работающей, арендованные аккаунты заняты и слот параллельности (один
из восьми) занят. Это ровно то поведение, из-за которого владелец «начинает
отменять и запускать заново, добирая новых ограничений».

ЧТО ПРОВЕРЯЕМ. Длинная пауза дробится и опрашивает отмену — но ранний выход
допускается ТОЛЬКО там, где следом идёт либо следующий виток цикла (а он
начинается с проверки отмены), либо явная проверка на месте. Там, где за
паузой сразу идёт реальное действие, пауза остаётся целой: укоротить её
значит сделать действие раньше, чем позволяет анти-бан-темп, а это дороже
простоя.
"""
from __future__ import annotations

import ast
import asyncio
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "services", "op_worker.py")

# Ниже какой длины пауза простоем флота не считается: отмену владелец и так
# заметит не мгновенно, а дробить секундные паузы — это лишние запросы к БД.
_LONG_S = 30.0

# Паузы, которые обязаны остаться целыми, с причиной для каждой. Ключ —
# (функция, выражение длины): он не ломается от сдвига строк.
#
# Первые три: пауза стоит ВПЛОТНУЮ перед реальным действием (поставить
# username, опубликовать пост), и между ними нет проверки отмены. Ранний
# выход сделал бы действие раньше, чем разрешает темп, — то есть обменял бы
# простой флота на риск ограничения, а это не обмен.
#
# Две последних — инвайт: там длину назначает governor чата (окно частоты), и
# выход из неё раньше срока разбирается отдельно, вместе со всей цепочкой
# `_iter_round`. Инвайт — самая баноопасная операция продукта, и трогать его
# темп заодно с остальными нельзя.
_MUST_STAY_WHOLE = {
    ("_exec_global_presence_channel", "pause"),
    ("_exec_global_presence_channel", "_post_delay"),
    ("_exec_bulk_create_channels", "_pub_delay"),
    ("_exec_mass_invite", "_wait"),
    ("_iter_round", "_wait"),
    ("_exec_mass_invite", "min(_d2.next_delay_sec, 120.0)"),
}


def _lower_bound(node: ast.AST, known: dict[str, float]) -> float | None:
    """Наименьшая длина сна, если её видно из исходника (иначе None).

    Считаем только то, что действительно видно: литерал, `random.uniform`
    (берём нижнюю границу), сумму, `max`/`min`, губернатор темпа (его
    множитель ≥1 по построению) и умножение на вызов-джиттер
    (`chaos_factor()` тоже ≥1). Всё остальное — неизвестность, а не ноль:
    такой сон храповик просто не судит.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Name):
        return known.get(node.id)
    if isinstance(node, ast.Await):
        return _lower_bound(node.value, known)
    if isinstance(node, ast.Call):
        name = ast.unparse(node.func)
        if name.endswith("random.uniform") and len(node.args) == 2:
            return _lower_bound(node.args[0], known)
        if name.endswith("_governed_delay") and node.args:
            return _lower_bound(node.args[-1], known)
        if name == "max":
            seen = [v for v in (_lower_bound(a, known) for a in node.args) if v is not None]
            return max(seen) if seen else None
        if name == "min" and node.args:
            seen = [v for v in (_lower_bound(a, known) for a in node.args) if v is not None]
            return min(seen) if seen else None
        return None
    if isinstance(node, ast.BinOp):
        left = _lower_bound(node.left, known)
        right = _lower_bound(node.right, known)
        if isinstance(node.op, ast.Add) and left is not None and right is not None:
            return left + right
        if isinstance(node.op, ast.Mult):
            if left is not None and isinstance(node.right, ast.Call):
                return left
            if right is not None and isinstance(node.left, ast.Call):
                return right
    return None


def _long_raw_sleeps(src: str) -> list[tuple[str, str, int, float]]:
    """Сырые asyncio.sleep длиной от _LONG_S: (функция, выражение, строка, сек)."""
    tree = ast.parse(src)
    found: list[tuple[str, str, int, float]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        known: dict[str, float] = {}
        for node in ast.walk(fn):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                value = _lower_bound(node.value, known)
                if value is not None:
                    known[node.targets[0].id] = value
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call)
                    and ast.unparse(node.func).endswith("asyncio.sleep")
                    and node.args):
                continue
            low = _lower_bound(node.args[0], known)
            if low is not None and low >= _LONG_S:
                found.append((fn.name, ast.unparse(node.args[0]), node.lineno, low))
    return found


# ── Перепись длинных пауз ───────────────────────────────────────────────────

def test_every_long_pause_hears_the_cancel():
    with open(SRC, encoding="utf-8") as fh:
        src = fh.read()
    found = _long_raw_sleeps(src)
    deaf = [f for f in found if (f[0], f[1]) not in _MUST_STAY_WHOLE]
    assert not deaf, (
        "эти паузы глухи к отмене — операция держит слот и арендованные "
        "аккаунты до их конца:\n"
        + "\n".join(f"  {fn} стр.{line}: {expr} (от {low:.0f}с)"
                    for fn, expr, line, low in deaf)
        + "\nПропустите сон через _pause_watching_for_cancel(pool, op_id, …), "
        "если следом идёт виток цикла с проверкой отмены или такая проверка "
        "на месте. Если следом сразу реальное действие — оставьте паузу целой "
        "и внесите её в _MUST_STAY_WHOLE с причиной."
    )


def test_the_whole_pauses_are_still_there():
    """Самопроверка списка: исчезнувшая пауза — повод обновить причину, а не
    держать в храповике мёртвую запись (она маскирует новую находку)."""
    with open(SRC, encoding="utf-8") as fh:
        keys = {(fn, expr) for fn, expr, _, _ in _long_raw_sleeps(fh.read())}
    stale = sorted(_MUST_STAY_WHOLE - keys)
    assert not stale, (
        f"этих пауз в коде больше нет: {stale} — уберите их из _MUST_STAY_WHOLE")


def test_the_detector_bites():
    """Пауза в десять минут без отмены обязана быть находкой."""
    sick = (
        "async def _exec_demo(pool, op_id):\n"
        "    between = await _governed_delay(pool, 1, random.uniform(600, 1200))\n"
        "    await asyncio.sleep(between)\n"
    )
    assert _long_raw_sleeps(sick) == [("_exec_demo", "between", 3, 600.0)]


def test_the_detector_spares_short_pauses():
    """Иначе храповик потребовал бы дробить секундные паузы натуральности."""
    healthy = (
        "async def _exec_demo(pool, op_id):\n"
        "    await asyncio.sleep(2)\n"
        "    await asyncio.sleep(random.uniform(1.5, 3.5))\n"
        "    await _pause_watching_for_cancel(pool, op_id, 600, 'demo')\n"
    )
    assert _long_raw_sleeps(healthy) == []


# ── Поведение самой паузы ───────────────────────────────────────────────────

class _Pool:
    def __init__(self, status: str | None = "running"):
        self._status = status

    async def fetchrow(self, sql, *a):
        return None if self._status is None else {"status": self._status}


@pytest.fixture
def ow(monkeypatch):
    from services import op_worker

    op_worker._cancel_cache.clear()
    monkeypatch.setattr(op_worker, "_CANCEL_PAUSE_STEP_S", 0.01)
    yield op_worker
    op_worker._cancel_cache.clear()


def test_a_cancelled_operation_leaves_a_long_pacing_pause_early(ow):
    slept = asyncio.run(ow._pause_watching_for_cancel(
        _Pool("cancelled"), 42, 1.0, "проверка"))
    assert slept < 1.0, (
        "пауза темпа не прервалась: отменённая операция держит флот до её конца")


def test_a_live_operation_keeps_the_whole_pacing_pause(ow):
    """Темп — анти-бан, а не вежливость: без отмены он выдерживается целиком."""
    slept = asyncio.run(ow._pause_watching_for_cancel(
        _Pool("running"), 42, 0.05, "проверка"))
    assert slept == pytest.approx(0.05, abs=1e-6)
