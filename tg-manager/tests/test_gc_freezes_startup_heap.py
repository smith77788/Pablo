"""Стартовая куча заморожена: полная сборка мусора не обходит её каждый раз.

Без заморозки каждая полная сборка останавливала весь процесс (бот, мини-апп,
исполнитель операций) на 150–250 мс — см. services/gc_tuning.py.
"""
import gc
import re
from pathlib import Path

from services import gc_tuning

ROOT = Path(__file__).resolve().parent.parent


def test_freeze_moves_startup_objects_out_of_collection():
    gc.unfreeze()
    try:
        keep = [[i] for i in range(1000)]  # живые объекты «старта»
        n = gc_tuning.freeze_startup_heap()
        assert n >= len(keep)
        assert gc.get_freeze_count() == n
    finally:
        gc.unfreeze()


def test_freeze_collects_garbage_before_freezing():
    """Мусор старта не должен уехать в постоянное поколение навсегда."""
    gc.unfreeze()
    try:
        import weakref

        class Node:
            pass

        a, b = Node(), Node()
        a.other, b.other = b, a          # цикл: без сборки не освободится
        ref = weakref.ref(a)
        del a, b
        gc_tuning.freeze_startup_heap()
        assert ref() is None
    finally:
        gc.unfreeze()


def test_freeze_failure_does_not_break_startup(monkeypatch):
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(gc_tuning.gc, "freeze", boom)
    assert gc_tuning.freeze_startup_heap() == 0


def test_main_freezes_before_serving_updates():
    """Заморозка на настоящем пути старта и ДО поллинга/вебхука."""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    body = src[src.index("async def main()"):]
    call = body.find("freeze_startup_heap()")
    assert call != -1, "main() не замораживает стартовую кучу"
    serve = re.search(r"await bot\.set_webhook\(|await dp\.start_polling\(", body)
    assert serve and call < serve.start()
