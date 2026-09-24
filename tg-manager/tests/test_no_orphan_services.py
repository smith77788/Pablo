"""Мёртвые сервисы не должны множиться и не должны выглядеть рабочими.

Опасность неподключённого модуля — не его вес. Он выглядит как работающая
функциональность: следующая сессия начнёт «улучшать» то, что никогда не
исполнялось, либо напишет третью копию рядом. На `invite_engine` это едва не
случилось — параллельно шла работа по инвайтингу.

Поэтому два правила:
  1. список сирот не растёт (храповик);
  2. каждая сирота несёт явный маркер в докстринге — чтобы её нельзя было
     принять за рабочий код, не открывая grep.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "services"

MARKER = "СТАТУС: НЕ ПОДКЛЮЧЁН"

# Проверено 2026-07-27. Список может только СОКРАЩАТЬСЯ: сервис либо
# подключается к сценарию, либо удаляется осознанным решением.
KNOWN_ORPHANS = {
    "error_recovery",      # дублирует circuit breaker в op_worker + retry в operation_bus
    "session_converter",   # дублирует account_manager.import_from_*
    "session_pool",        # смена модели жизненного цикла сессий — отдельная задача
    "invite_engine",       # сценарий раздачи ссылок в продукте не реализован
    "ai_action_router",    # ассистент использует intent_planner
    "perf",                # инструмент разработки
    "channel_brain",       # quality gate готов, но пайплайну публикации негде
                            # брать историю постов канала — нужна отдельная таблица
}


def _module_files() -> dict[str, Path]:
    mods = {p.stem: p for p in SERVICES.glob("*.py") if p.name != "__init__.py"}
    mods.update({p.parent.name: p for p in SERVICES.glob("*/__init__.py")})
    return mods


def _prod_sources() -> dict[Path, str]:
    out = {}
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", ".git/")):
            continue
        out[p] = p.read_text(encoding="utf-8", errors="ignore")
    return out


def _is_used(name: str, path: Path, sources: dict[Path, str]) -> bool:
    """Ссылка из прод-кода в любой форме импорта, принятой в проекте.

    Формы важны все: `from services import (\\n  x,` встречается в хендлерах, и
    без него детектор объявит живой сервис мёртвым — на этом уже спотыкались.
    """
    patterns = [
        rf"from services\.{name}\b",
        rf"\bservices\.{name}\b",
        rf"from services import [^\n(]*\b{name}\b",
        rf"from services import \([^)]*\b{name}\b",
        rf"from \.{name}\b",
        rf"^import {name}\b",
    ]
    rx = [re.compile(p, re.S | re.M) for p in patterns]
    return any(q != path and any(r.search(s) for r in rx) for q, s in sources.items())


def _orphans() -> set[str]:
    sources = _prod_sources()
    return {n for n, p in _module_files().items() if not _is_used(n, p, sources)}


def test_orphan_list_does_not_grow():
    found = _orphans()
    new = found - KNOWN_ORPHANS
    assert not new, (
        f"появились неподключённые сервисы: {sorted(new)}.\n"
        "Либо подключите к сценарию, либо не добавляйте: мёртвый модуль выглядит "
        "как работающая функциональность и провоцирует писать копию рядом."
    )


def test_known_orphans_are_still_orphans():
    """Подключили — уберите из списка, иначе храповик перестаёт защищать."""
    found = _orphans()
    revived = KNOWN_ORPHANS - found
    assert not revived, (
        f"эти сервисы больше не сироты: {sorted(revived)} — уберите их из KNOWN_ORPHANS"
    )


def test_every_orphan_is_marked():
    """Маркер в докстринге — чтобы мёртвый код нельзя было принять за рабочий."""
    unmarked = []
    for name in sorted(KNOWN_ORPHANS):
        path = _module_files().get(name)
        assert path is not None, f"{name} отсутствует — обновите KNOWN_ORPHANS"
        head = path.read_text(encoding="utf-8")[:2500]
        if MARKER not in head:
            unmarked.append(name)
    assert not unmarked, (
        f"сироты без маркера «{MARKER}» в шапке: {unmarked}. "
        "Без него следующий разработчик примет модуль за рабочий."
    )


def test_orphan_marker_explains_why():
    """Маркер без объяснения бесполезен: непонятно, подключать или удалять."""
    thin = []
    for name in sorted(KNOWN_ORPHANS):
        src = _module_files()[name].read_text(encoding="utf-8")
        idx = src.find(MARKER)
        if idx < 0:
            continue
        explanation = src[idx : idx + 400]
        # Достаточно пары предложений о причине — не просто «не подключён».
        if len(explanation.split(".")) < 3:
            thin.append(name)
    assert not thin, f"маркер без объяснения причины: {thin}"


# ── Дубли темпа (P3) ────────────────────────────────────────────────────────

def test_dead_pacing_duplicate_is_marked():
    """Два расчёта задержки: рабочий в pacing_engine, мёртвый в session_simulator.

    Опасность не в лишнем коде, а в том, что при правке ТЕМПА массовых операций
    легко починить не тот механизм — и изменение не доедет до реальных задержек.
    """
    src = (SERVICES / "session_simulator.py").read_text(encoding="utf-8")
    idx = src.find("def get_adaptive_delay(")
    assert idx > 0
    doc = src[idx : idx + 900]
    assert "НЕ ИСПОЛЬЗУЕТСЯ" in doc
    assert "pacing_engine" in doc, "маркер обязан указывать на рабочий механизм"


def test_real_pacing_loop_is_closed():
    """Рабочий контур обучения должен остаться замкнутым: пишем и читаем.

    Запись исходов живёт в op_worker (по итогу операции), а чтение множителя —
    в op_pacing (расчёт задержки вынесен туда при распиле монолита); op_worker
    использует эту задержку через op_worker.get_adaptive_delay (re-export).
    """
    worker = (SERVICES / "op_worker.py").read_text(encoding="utf-8")
    pacing = (SERVICES / "op_pacing.py").read_text(encoding="utf-8")
    assert "get_pacing_engine().record_result(" in worker, "результаты не записываются"
    assert "get_pacing_engine().get_multiplier(" in pacing, "множитель не читается"
