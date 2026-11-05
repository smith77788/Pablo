"""Уборка фоновых циклов обязана идти по ЖИВОМУ пулу, а не после его закрытия.

РАЗРЫВ. Все ~60 фоновых циклов живут в `main._BG_TASKS`, и до сих пор их никто
не отменял: они умирали вместе с процессом, когда `asyncio.run` в самом конце
гасит оставшиеся задачи. К этому моменту `pool.close()` в блоке `finally` уже
прошёл — то есть уборка на отмене выполнялась по ЗАКРЫТОМУ пулу и падала
молча, внутри собственного `except`.

А уборка эта у трёх циклов есть, и ровно про деплой:

* `infra_memory.run_flush_loop` — финальная запись выученной осторожности
  аккаунтов и прокси. Интервал 60 секунд, значит на каждом выпуске терялось до
  минуты данных, по которым продукт выбирает аккаунты;
* `pacing_store.run_sync_loop` — последняя запись исходов и состояния темпа; в
  её докстринге прямо сказано «чтобы деплой не терял исходы и состояние,
  накопленные за последние секунды»;
* `payment_webhook.run` — закрытие своего сервера.

Автодеплой означает рестарт на каждый пуш, так что терялось это постоянно.

ПОЧЕМУ ПРОВЕРКА ПО ТЕКСТУ `main.py`. Последовательность остановки живёт в
блоке `finally` функции `main`, и выполнить её в тесте нельзя, не поднимая
процесс целиком. В проекте для этого уже есть прецедент — соседний тест
`test_main_calls_shutdown_before_closing_the_pool`. Поэтому проверяем разбором
AST: шаг есть, он отменяет задачи, ждёт их с РАМКОЙ и стоит до `pool.close()`.
"""
from __future__ import annotations

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "main.py")


def _src() -> str:
    with open(MAIN, encoding="utf-8") as f:
        return f.read()


def _fn(name: str) -> ast.AST:
    tree = ast.parse(_src())
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"в main.py нет функции {name}")


# ── Главное: шаг есть и стоит до закрытия пула ───────────────────────────────

def test_the_stop_step_runs_before_the_pool_is_closed():
    """После закрытия пула любая финальная запись — это запись в никуда."""
    src = _src()
    call = "_stop_background_loops()"
    assert call in src, (
        "фоновые циклы никто не останавливает до закрытия пула — их финальные "
        "записи (память аккаунтов, темп) уйдут в закрытый пул"
    )
    assert src.index(call) < src.index("await pool.close()"), (
        "остановка фоновых циклов стоит ПОСЛЕ закрытия пула — то есть не даёт "
        "ничего"
    )


def test_the_worker_cleanup_still_goes_first():
    """Возврат операций и снятие аренд дороже: без них флот стоит до TTL."""
    src = _src()
    assert src.index("op_worker.shutdown(pool") < src.index("_stop_background_loops()"), (
        "остановка фоновых циклов обогнала уборку воркера — платформа может "
        "убить контейнер до возврата операций и снятия аренд аккаунтов"
    )


# ── Сам шаг: отменяет, ждёт и ждёт С РАМКОЙ ──────────────────────────────────

def test_the_step_cancels_the_loops():
    node = _fn("_stop_background_loops")
    body = ast.unparse(node)
    assert ".cancel()" in body, "шаг не отменяет задачи — ждать будет нечего"
    assert "_BG_TASKS" in body, (
        "шаг не смотрит в _BG_TASKS — значит отменяет не фоновые циклы"
    )


def test_the_wait_is_bounded():
    """Без рамки зависший цикл забрал бы закрытие пула и сессии бота."""
    node = _fn("_stop_background_loops")
    waits = [n for n in ast.walk(node)
             if isinstance(n, ast.Call)
             and ast.unparse(n.func).endswith(("asyncio.wait", "asyncio.wait_for"))]
    assert waits, "шаг не ждёт сворачивания — уборка не успеет ничего записать"
    assert any(any(k.arg in ("timeout",) for k in w.keywords) for w in waits), (
        "ожидание без timeout: один не сворачивающийся цикл задержит остановку "
        "до того, как платформа убьёт контейнер"
    )


# ── Самопроверка: шаг защищает что-то настоящее ──────────────────────────────

def test_there_is_cleanup_worth_protecting():
    """Если финальные записи исчезнут, этот тест обязан это заметить.

    Иначе шаг останется охранять пустоту, и следующий читатель не поймёт,
    зачем он нужен.
    """
    found = []
    for rel, fname in (("services/infra_memory.py", "run_flush_loop"),
                       ("services/pacing_store.py", "run_sync_loop")):
        path = os.path.join(ROOT, rel)
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if node.name != fname:
                continue
            for t in ast.walk(node):
                if not isinstance(t, ast.Try):
                    continue
                for h in t.handlers:
                    names = ([] if h.type is None else
                             [ast.unparse(i) for i in
                              (h.type.elts if isinstance(h.type, ast.Tuple) else [h.type])])
                    if any("CancelledError" in n for n in names) and any(
                            isinstance(x, ast.Await) for x in ast.walk(h)):
                        found.append(f"{rel}:{fname}")
    assert len(set(found)) == 2, (
        "финальных записей на отмене больше нет в "
        f"{{infra_memory, pacing_store}} (нашлось: {sorted(set(found))}) — "
        "либо их убрали, либо переписали; тогда и шаг остановки надо "
        "пересмотреть, а не оставлять охранять пустоту"
    )
