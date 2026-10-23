"""Регресс-тест: каждый op_type, который ставит mini app / handlers в очередь,
ДОЛЖЕН диспатчиться воркером. Иначе операция зависает навсегда (класс бага
«pending 29 дней»).

Сверяем с диспетчером op_worker два источника: типы из вызовов
`operation_bus.submit(op_type=...)` и типы из прямых `INSERT INTO
operation_queue` (их стережёт `test_no_raw_queue_inserts`, но если такой
всё-таки появится, он тоже обязан диспатчиться).

**Проба проверяется на заведомо здоровом примере** (`test_the_probe_sees_real_op_types`):
раньше проба искала только прямые INSERT, а их в mini app давно нет — она
перестала видеть ЧТО-ЛИБО и тест проходил вхолостую, ничего не охраняя.
"""
from __future__ import annotations

import ast
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


# Где ставят операции: mini app и обработчики бота.
_SUBMIT_SOURCES = tuple(
    ["services/mini_app_api.py", "services/operation_bus.py"]
    + sorted(
        os.path.relpath(os.path.join(_d, _f), _ROOT)
        for _d, _, _fs in os.walk(os.path.join(_ROOT, "bot"))
        for _f in _fs
        if _f.endswith(".py")
    )
)

# Не op_type очереди, а kind/asset_type — ложные срабатывания прежней пробы.
_NOT_OP_TYPES = {"note", "post"}


def _submitted_op_types() -> set[str]:
    types: set[str] = set()
    for rel in _SUBMIT_SOURCES:
        src = _read(rel)
        # 1. operation_bus.submit(..., op_type="...") — основной и единственный
        #    разрешённый путь постановки (см. tests/test_operation_bus_ratchet.py).
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name != "submit":
                continue
            # op_type — третий позиционный (pool, owner_id, op_type, params),
            # но зовут и по имени. Берём оба написания.
            cand = None
            if len(node.args) >= 3:
                cand = node.args[2]
            for kw in node.keywords:
                if kw.arg == "op_type":
                    cand = kw.value
            if isinstance(cand, ast.Constant) and isinstance(cand.value, str):
                types.add(cand.value)
        # 2. Прямой INSERT INTO operation_queue(...) VALUES($1,'op_type',...
        #    Якорь именно на operation_queue: без него ловилась ЛЮБАЯ вставка
        #    вида VALUES($1,'что-то') — например служебная отметка в
        #    schema_migrations со статусом 'ok'.
        for m in re.finditer(r"INSERT\s+INTO\s+operation_queue\b", src, re.I):
            chunk = src[m.end(): m.end() + 600]
            hit = re.search(r"VALUES\s*\(\s*\$1,\s*'([a-z_]+)'", chunk)
            if hit:
                types.add(hit.group(1))
    return types - _NOT_OP_TYPES


def _dispatched_op_types() -> set[str]:
    """Спрашиваем воркер напрямую: соответствие «тип → исполнитель» это словарь,
    а не форма кода, и скан регэкспом по исходнику здесь больше не нужен."""
    from services import op_worker

    return set(op_worker.dispatch_table())


def test_all_submitted_op_types_are_dispatched():
    submitted = _submitted_op_types()
    dispatched = _dispatched_op_types()
    missing = submitted - dispatched
    assert not missing, (
        f"op_types поставлены mini app, но НЕ диспатчатся воркером "
        f"(зависнут навсегда): {sorted(missing)}"
    )


def test_registry_matches_dispatch():
    """OP_REGISTRY (operation_bus) op_types тоже должны диспатчиться."""
    reg_src = _read("services/operation_bus.py")
    registry = set(re.findall(r'^\s{4}"([a-z_]+)":\s*\{', reg_src, re.M))
    dispatched = _dispatched_op_types()
    missing = registry - dispatched
    assert not missing, f"OP_REGISTRY op_types не диспатчатся: {sorted(missing)}"


def test_the_probe_sees_real_op_types():
    """Проба обязана что-то находить.

    Пустой результат означает не «всё хорошо», а «детектор ослеп»: именно так
    этот тест и жил — искал прямые INSERT, которых в mini app давно нет.
    """
    found = _submitted_op_types()
    assert len(found) >= 5, (
        f"проба нашла всего {len(found)} op_type ({sorted(found)}) — она "
        f"перестала видеть постановку операций и охраняет пустоту")
    assert "mass_invite" in found, (
        "самой баноопасной операции продукта проба не видит — значит не видит "
        "и остальных")
