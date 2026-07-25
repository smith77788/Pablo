"""Гейт паритета: мини-апп догоняет бот по операциям и не отстаёт сильнее.

Продуктовое требование: мини-апп — это визуальный софт вместо тысячи кнопок в
боте, поэтому в нём должно быть ВСЁ, что умеет бот: реальное создание каналов/
чатов/ботов, авторегистрация, массовые операции.

Разрыв закрывается постепенно, поэтому гейт — храповик (как
test_operation_bus_ratchet): он не требует нулевого разрыва сразу, но НЕ ДАЁТ
ему вырасти. Закрыли ещё одну операцию — уменьшите BASELINE_GAP (вниз двигать
можно и нужно; вверх — только осознанно, с объяснением).

Сверка идёт по op_type, которые реально диспетчеризует воркер: строки в
комментариях не считаются (иначе упоминание операции в пояснении «закрывало» бы
разрыв, ничего не реализовав — на этом я уже один раз обжёгся).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Факт на 2026-07-25 после подключения bot_factory: 16 операций есть в боте и
# ещё не выведены в мини-апп. Двигать ВНИЗ по мере закрытия.
BASELINE_GAP = 16


def _strip_comments(src: str) -> str:
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in src.splitlines())


def _worker_ops() -> list[str]:
    ow = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
    return sorted(set(re.findall(r'op_type == ["\'](\w+)["\']', ow)))


def _bot_src() -> str:
    out = []
    for p in (ROOT / "bot").rglob("*.py"):
        out.append(_strip_comments(p.read_text(encoding="utf-8", errors="ignore")))
    return "\n".join(out)


def _mini_src() -> str:
    return _strip_comments((ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8"))


def _gap() -> list[str]:
    bot, mini = _bot_src(), _mini_src()
    gap = []
    for op in _worker_ops():
        lit = r'["\']' + re.escape(op) + r'["\']'
        if re.search(lit, bot) and not re.search(lit, mini):
            gap.append(op)
    return gap


def test_parity_gap_does_not_grow():
    gap = _gap()
    assert len(gap) <= BASELINE_GAP, (
        f"разрыв бот→мини-апп вырос до {len(gap)} (baseline {BASELINE_GAP}). "
        "Мини-апп должен уметь всё, что умеет бот. Новые операции выводите в "
        f"мини-апп сразу. Сейчас не выведены: {gap}"
    )


def test_baseline_not_stale():
    """Baseline не должен быть завышен: закрыли операцию — опустите число,
    иначе храповик перестаёт защищать."""
    gap = _gap()
    assert len(gap) >= BASELINE_GAP - 3, (
        f"фактический разрыв {len(gap)} заметно меньше baseline {BASELINE_GAP} — "
        "опустите BASELINE_GAP до факта, чтобы гейт снова держал планку"
    )


def test_bot_factory_reached_parity():
    """Реальное создание ботов через @BotFather — было первым закрытым пунктом."""
    assert "bot_factory" not in _gap(), (
        "создание ботов через BotFather обязано быть доступно из мини-аппа"
    )
