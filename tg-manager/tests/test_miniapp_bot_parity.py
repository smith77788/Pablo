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

# Факт на 2026-07-25 после подключения bot_factory + bulk_create_channels.
# Двигать ВНИЗ по мере закрытия.
#
# Первый замер дал 17, но был ЗАВЫШЕН: global_presence_channel/_group/_bot
# собираются динамически (f"global_presence_{asset_type}") и давно доступны из
# мини-аппа — детектор литералов их не видел. Едва не начал реализовывать уже
# работающее; отсюда учёт динамических префиксов ниже.
BASELINE_GAP = 10


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


def _dynamic_prefixes(src: str) -> list[str]:
    """Префиксы op_type, собираемых динамически: f"global_presence_{asset_type}".

    Без этого детектор литералов считает такие операции «отсутствующими» и
    завышает разрыв — я на этом уже ошибся, чуть не начав реализовывать то, что
    в мини-аппе давно работает.
    """
    out = []
    for m in re.finditer(r'op_type\s*=\s*f["\']([a-z_]+?)\{', src):
        out.append(m.group(1))
    for m in re.finditer(r'submit\(\s*[^,]+,\s*[^,]+,\s*f["\']([a-z_]+?)\{', src):
        out.append(m.group(1))
    return out


def _reachable_from_mini(op: str, mini: str, prefixes: list[str]) -> bool:
    if re.search(r'["\']' + re.escape(op) + r'["\']', mini):
        return True
    return any(op.startswith(p) and op != p for p in prefixes)


def _gap() -> list[str]:
    bot, mini = _bot_src(), _mini_src()
    prefixes = _dynamic_prefixes(mini)
    gap = []
    for op in _worker_ops():
        if re.search(r'["\']' + re.escape(op) + r'["\']', bot) and not _reachable_from_mini(op, mini, prefixes):
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


def test_creation_operations_reached_parity():
    """Создание сущностей — то, что владелец назвал в первую очередь."""
    gap = _gap()
    for op in ("bot_factory", "bulk_create_channels",
               "global_presence_channel", "global_presence_group", "global_presence_bot"):
        assert op not in gap, f"{op}: создание должно быть доступно из мини-аппа"


def test_dynamic_optypes_counted_as_reachable():
    """Регресс на ошибку замера: op_type, собираемый f-строкой, — тоже доступен.

    Без этого разрыв завышается и можно начать реализовывать уже работающее.
    """
    mini = _mini_src()
    prefixes = _dynamic_prefixes(mini)
    assert "global_presence_" in prefixes, (
        "динамический префикс global_presence_ должен распознаваться"
    )
    assert _reachable_from_mini("global_presence_channel", mini, prefixes)
