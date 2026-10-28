"""Регресс: долгоживущие фоновые циклы не запускаются fire-and-forget без ссылки.

Event loop держит на задачу ТОЛЬКО слабую ссылку (asyncio docs). Длинный фоновый
цикл, запущенный несохранённым `create_task`/`get_event_loop().create_task`, может
быть собран GC до завершения → фича молча умирает. Такие задачи обязаны иметь
удержанную ссылку (локальную в невозвращающемся фрейме, модульную или через
task_registry). Здесь стережём конкретные известные точки старта фоновых циклов.
"""
from __future__ import annotations

import inspect

from services import scheduler, auto_responder, op_worker


def test_scheduler_ab_sweep_not_unreferenced():
    src = inspect.getsource(scheduler.run)
    assert "get_event_loop().create_task(declare_ab_winners" not in src
    assert "await declare_ab_winners(pool)" in src


def test_auto_responder_inactivity_sweep_has_ref():
    src = inspect.getsource(auto_responder.run)
    assert "get_event_loop().create_task(run_inactivity_sweep" not in src, (
        "fire-and-forget без ссылки → GC-риск для фонового sweep"
    )
    # ссылка удержана на уровне модуля (или иным способом), а не потеряна
    assert "_inactivity_sweep_task = asyncio.create_task(run_inactivity_sweep" in src, (
        "sweep должен запускаться с удержанием ссылки"
    )
    assert "_inactivity_sweep_task" in inspect.getsource(auto_responder), (
        "ссылка на sweep должна существовать на уровне модуля"
    )


def test_op_worker_holds_op_task_refs():
    # Самый критичный путь: каждая операция = отдельная задача. Без удержания
    # ссылки GC мог бы собрать выполняющуюся операцию до завершения.
    mod = inspect.getsource(op_worker)
    assert "_active_op_tasks" in mod, "нужен набор strong-ссылок на задачи операций"
    assert "add_done_callback(_active_op_tasks.discard)" in mod, (
        "задача операции должна сниматься по завершении, а до этого держаться ссылкой"
    )


# ── Свип #14: фоновые джобы из хендлеров запускаются через spawn (strong-ссылка) ──

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]

# (file, коротко что за задача) — эти fire-and-forget джобы обязаны идти через spawn,
# а не через голый create_task (иначе GC может собрать их до завершения).
_SPAWNED_JOBS = [
    ("bot/handlers/self_promo.py", "_post_to_channels_bg"),
    ("bot/handlers/accounts.py", "_qr_wait_task"),
    ("bot/handlers/admin.py", "_gate_notify_all_task"),
    ("bot/handlers/auto_registrar.py", "_do_batch_register"),
    ("bot/handlers/auto_registrar.py", "_wait_and_confirm"),
    ("bot/handlers/account_warmup.py", "_create_bg"),
    ("bot/handlers/gift_transfer.py", "_scan_bg"),
    ("bot/handlers/reg_checker.py", "_enrich_metadata"),
    ("bot/handlers/ai_assistant.py", "_do_action"),
]


def test_known_bg_jobs_use_spawn_not_bare_create_task():
    for rel, coro in _SPAWNED_JOBS:
        src = (_ROOT / rel).read_text(encoding="utf-8")
        # spawn(...) оборачивает корутину (допускаем перенос/отступ: \s*)
        assert re.search(rf"spawn\(\s*{re.escape(coro)}\(", src), (
            f"{rel}: {coro} должен запускаться через spawn()"
        )
        # и не оставаться голым create_task для этой же корутины
        assert not re.search(rf"create_task\(\s*{re.escape(coro)}\(", src), (
            f"{rel}: {coro} всё ещё запускается голым create_task"
        )


def test_no_bare_unreferenced_create_task_in_swept_files():
    """В затронутых свипом файлах не осталось голого несохранённого create_task.

    Разрешено: присваивание (task=/tasks=/_task=) и comprehension (… for …).
    """
    swept = [rel for rel, _ in _SPAWNED_JOBS] + [
        "bot/handlers/botmother_menu.py", "bot/handlers/topology.py",
        "bot/handlers/competitors.py", "bot/handlers/proxy_manager.py",
        "bot/handlers/start.py",
    ]
    offenders = []
    for rel in set(swept):
        for i, line in enumerate((_ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
            st = line.strip()
            if not re.match(r"^(asyncio|_asyncio)\.create_task\(", st):
                continue
            # присваивание слева?
            if re.search(r"=\s*(asyncio|_asyncio)\.create_task\(", line):
                continue
            # comprehension (в той же строке for) — держится списком
            if " for " in line:
                continue
            offenders.append(f"{rel}:{i}: {st}")
    assert not offenders, "Голый несохранённый create_task:\n" + "\n".join(offenders)
