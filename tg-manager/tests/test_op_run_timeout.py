"""Зависшая операция обязана отпускать флот, а не держать его до рестарта.

Разрыв. Вызов исполнителя не имел НИКАКОГО потолка. Исполнитель, зависший на
ответе Telegram (сеть легла, прокси молчит, вызов без собственного timeout),
держал слот параллельности — один из восьми — и арендованные аккаунты
бесконечно. Причём невидимо:

  * `_watchdog_stale` пропускает операции, числящиеся активными в этом процессе
    (`_active_op_ids`), — а зависшая там и числится;
  * `_watchdog_alerts` отсекает их по тому же критерию, так что и алерта нет.

То есть операция просто исчезала из всех механизмов наблюдения, и вылечить это
можно было только рестартом контейнера.

Потолок намеренно щедрый: массовым операциям положено идти часами (пейсинг
против банов), и убить здоровую работу хуже, чем поздно поймать зависшую.
Таймаут считается временной ошибкой, поэтому операция уходит в обычный повтор с
backoff и, исчерпав max_retries, честно падает в failed.

op_worker импортирует telethon и в тестовой среде не поднимается — связку
проверяем по исходнику, как и соседние тесты очереди.
"""
from __future__ import annotations

import os
import re

from services.operation_bus import OP_REGISTRY, timeout_for

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _fn(src: str, name: str) -> str:
    start = src.index(f"async def {name}")
    m = re.search(r"\n(?:async )?def ", src[start + 10:])
    return src[start:start + 10 + m.start()] if m else src[start:]


# ── Потолок из реестра ───────────────────────────────────────────────────────

def test_unknown_op_type_falls_back_to_default():
    assert timeout_for("нет такого типа", 1234) == 1234


def test_registry_types_without_override_use_default():
    assert timeout_for("mass_publish", 1234) == 1234


def test_declared_override_wins(monkeypatch):
    OP_REGISTRY.setdefault("__probe__", {})["timeout_sec"] = 60
    try:
        assert timeout_for("__probe__", 1234) == 60
    finally:
        OP_REGISTRY.pop("__probe__", None)


def test_garbage_override_does_not_disable_the_cap():
    """Мусор в реестре не должен снимать защиту — иначе опечатка = снова вечность."""
    for junk in ("", None, 0, -5, "много", [], {}):
        OP_REGISTRY.setdefault("__probe__", {})["timeout_sec"] = junk
        try:
            assert timeout_for("__probe__", 1234) == 1234, junk
        finally:
            OP_REGISTRY.pop("__probe__", None)


def test_every_declared_override_is_sane():
    """Если тип объявил свой потолок, он обязан быть осмысленным числом секунд."""
    for op_type, meta in OP_REGISTRY.items():
        if "timeout_sec" not in meta:
            continue
        value = meta["timeout_sec"]
        assert isinstance(value, int) and value > 0, (op_type, value)
        assert value <= 48 * 3600, f"{op_type}: потолок больше двух суток бессмыслен"


# ── Связка с воркером ────────────────────────────────────────────────────────

def test_handler_call_is_bounded():
    body = _fn(_read("services/op_worker.py"), "_run_op_task")
    assert "asyncio.wait_for(" in body, "вызов исполнителя обязан иметь потолок"
    call = body[body.index("_handler = handler_for(op_type)"):]
    assert "_handler(pool, bot, op_id, owner_id, params)" in call
    wait = call.index("asyncio.wait_for(")
    handler = call.index("_handler(pool, bot, op_id, owner_id, params)", wait)
    assert handler - wait < 200, "потолок должен оборачивать именно вызов исполнителя"


def test_timeout_is_configurable_and_bounded():
    ow = _read("services/op_worker.py")
    m = re.search(
        r'_OP_TIMEOUT_DEFAULT_S = _int_env\("OP_TIMEOUT_SEC", ([^,]+), ([^,]+), ([^)]+)\)', ow)
    assert m, "общий потолок не найден"
    default, lo, hi = (eval(g.strip()) for g in m.groups())  # noqa: S307 - свои же литералы
    assert lo <= default <= hi
    assert lo >= 60, "слишком маленький нижний предел убьёт здоровую массовую операцию"
    assert hi <= 48 * 3600, "верхний предел обязан быть конечным"
    assert default >= 3600, (
        "дефолт обязан быть щедрым: массовым операциям положено идти часами, "
        "и убить здоровую работу хуже, чем поздно поймать зависшую"
    )


def test_timeout_uses_per_type_ceiling():
    body = _fn(_read("services/op_worker.py"), "_run_op_task")
    assert "timeout_for(op_type, _OP_TIMEOUT_DEFAULT_S)" in body, (
        "потолок обязан уважать объявленный типом timeout_sec"
    )


def test_timeout_is_loud():
    """Раньше зависшая операция не оставляла следа вообще."""
    body = _fn(_read("services/op_worker.py"), "_run_op_task")
    seg = body[body.index("except asyncio.TimeoutError"):]
    seg = seg[:400]
    assert "log.error" in seg, "прерывание по таймауту обязано попасть в логи"
    assert "op_id" in seg and "op_type" in seg


def test_timeout_reaches_the_normal_failure_path():
    """Таймаут — временная ошибка: повтор с backoff, затем честный failed.

    Отдельной ветки быть не должно: она бы обошла освобождение аккаунтов,
    _maybe_requeue и запись error_msg.
    """
    ow = _read("services/op_worker.py")
    body = _fn(ow, "_run_op_task")
    seg = body[body.index("except asyncio.TimeoutError"):]
    seg = seg[:seg.index("\n            # Флот временно занят")] if "\n            # Флот временно занят" in seg else seg[:800]
    assert "raise TimeoutError(" in seg, (
        "таймаут обязан подниматься как ошибка, а не подменять результат"
    )
    # TimeoutError уже числится временной ошибкой — значит уйдёт в повтор.
    from services.op_errors import _RETRYABLE_ERRORS
    assert "TimeoutError" in _RETRYABLE_ERRORS


def test_timeout_message_is_russian_and_actionable():
    """Владелец не понимает английский (CLAUDE.md) — причина попадает в error_msg."""
    body = _fn(_read("services/op_worker.py"), "_run_op_task")
    seg = body[body.index("raise TimeoutError("):]
    seg = seg[:300]
    assert re.search(r"[а-яА-Я]", seg), seg
    assert "прервана" in seg


def test_accounts_are_released_after_timeout():
    """Смысл потолка — отпустить флот; без finally он бы не отпускался."""
    body = _fn(_read("services/op_worker.py"), "_run_op_task")
    fin = body[body.rindex("finally:"):]
    assert "release_operation_accounts(op_id)" in fin
    assert "_active_op_ids.discard(op_id)" in fin
