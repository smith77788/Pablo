"""Храповик: «Отменить» обязана доходить до работающего цикла исполнителя.

Отмена в этом продукте — не кнопка «закрыть окно». Массовый прогон тратит
невосполнимое: дневные лимиты аккаунтов, приглашения, доверие Telegram. Если
исполнитель спрашивает про отмену только ДО цикла, нажатие «Отменить» ничего не
останавливает: прогон идёт до конца по всем целям, и останавливает его лишь
потолок прогона.

Проверено 2026-10-01: все 32 исполнителя с реальной работой в цикле отмену
уважают, трое — через делегата (`_exec_dm_campaign` → `dm_engine.run_campaign`
опрашивает `operation_queue.status` в своём цикле, `_exec_auto_register` →
`progress_cb` возвращает «продолжать ли», `_exec_find_contact` →
`contact_finder.hunt(is_cancelled=...)`). То есть здесь не исправление, а
заморозка достигнутого: новый исполнитель не должен уехать в прод без этого.

Правило, которое сторожим: `_is_cancelled` вызывается ВНУТРИ цикла либо внутри
вложенной функции исполнителя (её он и передаёт делегату). Вызов только в
прологе правилу не удовлетворяет.
"""
from __future__ import annotations

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Модули, обращение к которым в цикле означает «здесь идёт реальная работа»:
# сетевой вызов к Telegram или движок, который его делает.
_WORKERS = ("account_manager", "strike_engine", "dm_engine", "mass_inviter_engine",
            "content_mesh", "ghost_engine", "account_warmer", "session_simulator")

_LOOPS = (ast.For, ast.AsyncFor, ast.While)


def _calls(node) -> list[str]:
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                v = f.value
                out.append(f"{v.id}.{f.attr}" if isinstance(v, ast.Name) else f".{f.attr}")
            elif isinstance(f, ast.Name):
                out.append(f.id)
    return out


def _nested_funcs(fn):
    return [n for n in ast.walk(fn)
            if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n is not fn]


def _classify(tree) -> tuple[list[str], list[str]]:
    good: list[str] = []
    bad: list[str] = []
    for fn in [n for n in tree.body
               if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
               and n.name.startswith("_exec_")]:
        loops = [n for n in ast.walk(fn) if isinstance(n, _LOOPS)]
        if not any(c.startswith(w + ".") for lp in loops for c in _calls(lp)
                   for w in _WORKERS):
            continue                      # в цикле нет работы по сети
        in_loop = any("_is_cancelled" in _calls(lp) for lp in loops)
        # Делегирование: исполнитель отдаёт проверку наружу вложенной функцией
        # (колбэк прогресса, is_cancelled=...). Это такая же отмена в цикле,
        # просто цикл чужой.
        in_callback = any("_is_cancelled" in _calls(nf) for nf in _nested_funcs(fn))
        (good if (in_loop or in_callback) else bad).append(fn.name)
    return good, bad


def _tree():
    with open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8") as f:
        return ast.parse(f.read())


def test_every_working_loop_can_be_stopped():
    good, bad = _classify(_tree())
    assert not bad, (
        "исполнитель делает сетевую работу в цикле, но отмену спрашивает только "
        "до него — «Отменить» не останавливает прогон:\n  "
        + "\n  ".join(sorted(bad)))
    assert len(good) >= 30, (
        f"детектор видит всего {len(good)} рабочих циклов — он сломался, "
        "а не код стал лучше")


def test_the_detector_flags_a_broken_executor():
    """Детектор, который ничего не ловит, зелёный всегда — правило тогда мертво."""
    broken = ast.parse(
        "async def _exec_demo(pool, bot, op_id, owner_id, params):\n"
        "    if await _is_cancelled(pool, op_id):\n"
        "        return {}\n"
        "    for t in params['targets']:\n"
        "        await account_manager.post_to_channel(t)\n")
    good, bad = _classify(broken)
    assert bad == ["_exec_demo"] and not good


def test_the_detector_accepts_a_check_inside_the_loop():
    fixed = ast.parse(
        "async def _exec_demo(pool, bot, op_id, owner_id, params):\n"
        "    for t in params['targets']:\n"
        "        if await _is_cancelled(pool, op_id):\n"
        "            break\n"
        "        await account_manager.post_to_channel(t)\n")
    good, bad = _classify(fixed)
    assert good == ["_exec_demo"] and not bad


def test_the_detector_accepts_delegation_through_a_callback():
    """Так устроены dm_campaign, auto_register и find_contact."""
    delegated = ast.parse(
        "async def _exec_demo(pool, bot, op_id, owner_id, params):\n"
        "    async def _stop():\n"
        "        return await _is_cancelled(pool, op_id)\n"
        "    for chunk in params['chunks']:\n"
        "        await account_manager.hunt(chunk, is_cancelled=_stop)\n")
    good, bad = _classify(delegated)
    assert good == ["_exec_demo"] and not bad


def test_the_detector_ignores_loops_without_network_work():
    """Иначе под правило попадёт любой подсчёт в списке — это ложные находки."""
    pure = ast.parse(
        "async def _exec_demo(pool, bot, op_id, owner_id, params):\n"
        "    total = 0\n"
        "    for t in params['targets']:\n"
        "        total += 1\n"
        "    return {'ok': total}\n")
    good, bad = _classify(pure)
    assert not good and not bad
