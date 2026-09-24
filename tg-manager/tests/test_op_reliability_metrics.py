"""Срабатывания механизмов надёжности обязаны быть видны снаружи, а не в логах.

Очередь обзавелась тремя способами прекратить операцию помимо обычного провала:
потолок прогона (операция прервана по времени), бюджет живучести (операция
остановлена как ядовитая после N падений воркера) и отсрочка по длинной
флуд-паузе. Все три писали ТОЛЬКО в лог.

Снаружи это выглядит одинаково: операция «не доехала». Понять, прервана она по
времени, отложена платформой или остановлена как ядовитая, можно было лишь
чтением логов постфактум — то есть узнать о деградации получалось из жалобы
владельца, а не с графика. Ровно та причина, по которой в воркере уже есть
`infragram_operations_total` и `infragram_operation_seconds`.

Метрика не должна ронять работу: счётчик — вспомогательная вещь, его сбой не
повод терять операцию.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Счётчики, которые обязаны существовать и быть описаны в реестре метрик.
_EXPECTED = (
    "infragram_op_timeouts_total",
    "infragram_op_revives_total",
    "infragram_op_poisoned_total",
    "infragram_op_flood_defers_total",
    "infragram_flood_sleep_truncated_total",
)


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _fn(src: str, name: str) -> str:
    start = src.index(f"def {name}")
    m = re.search(r"\n(?:async )?def ", src[start + 10:])
    return src[start:start + 10 + m.start()] if m else src[start:]


def test_every_counter_is_documented_in_the_registry():
    """Метрика без описания — строка, которую некому прочитать на графике."""
    from services.metrics import _HELP

    for name in _EXPECTED:
        assert name in _HELP, f"{name} не описан в реестре метрик"
        assert re.search(r"[а-яА-Я]", _HELP[name]), (
            f"{name}: описание читает владелец, оно должно быть на русском"
        )


def test_every_counter_is_actually_emitted():
    ow = _read("services/op_worker.py")
    for name in _EXPECTED:
        assert f'"{name}"' in ow, f"{name} объявлен, но нигде не пишется"


def test_metric_helper_never_breaks_the_operation():
    """Сбой счётчика не повод терять операцию — он вспомогателен."""
    code = _fn(_read("services/op_worker.py"), "_reliability_metric")
    assert "try:" in code and "except Exception:" in code
    assert "pass" in code


def test_counters_count_operations_not_watchdog_ticks():
    """Один тик сторожа поднимает несколько операций.

    Инкремент на единицу показал бы «1» там, где операций было десять, и
    масштаб проблемы на графике бы потерялся.
    """
    ow = _read("services/op_worker.py")
    for call, counter in (
        ("infragram_op_revives_total", "count"),
        ("infragram_op_poisoned_total", "poisoned_n"),
    ):
        for m in re.finditer(rf'_reliability_metric\("{call}",\s*([^,)]+)', ow):
            assert m.group(1).strip() == counter, (
                f"{call} обязан инкрементироваться числом операций ({counter}), "
                f"а не {m.group(1).strip()}"
            )


def test_timeout_metric_is_labelled_by_op_type():
    """Без типа операции счётчик не отвечает на главный вопрос: ЧТО зависает."""
    ow = _read("services/op_worker.py")
    m = re.search(r'_reliability_metric\(\s*"infragram_op_timeouts_total",\s*op_type=op_type\s*\)', ow)
    assert m, "таймаут обязан быть размечен типом операции"


def test_revive_and_poison_metrics_say_where_they_fired():
    """Сброс на старте и сторож зависших — разные симптомы, путать их нельзя."""
    ow = _read("services/op_worker.py")
    for source in ("startup", "watchdog"):
        assert f'source="{source}"' in ow, f"нет разметки source={source}"


def test_stuck_alert_text_matches_actual_behaviour():
    """Алерт обещал безусловный авто-сброс — после бюджета живучести это неправда.

    Владелец читает этот текст и принимает по нему решение: ждать или вмешаться.
    Устаревшее обещание здесь хуже отсутствия текста.
    """
    body = _fn(_read("services/op_worker.py"), "_watchdog_alerts")
    # Именно текст, который уходит владельцу, а не комментарий рядом с ним:
    # выше по функции старая формулировка цитируется в пояснении к другому фиксу.
    tail = body[body.index('lines.append(\n        "\\n<i>pending не разбирается'):]
    tail = tail[:400]
    assert "_MAX_REVIVES" in tail, (
        "текст обязан называть предел воскрешений, а не обещать вечный авто-сброс"
    )
    assert "помечается ошибкой" in tail


def test_metrics_module_accepts_the_value_argument():
    """Поведение проверяем напрямую: подпись inc() могла разойтись с вызовом."""
    from services import metrics

    metrics.reset()
    metrics.inc("infragram_op_revives_total", {"source": "startup"}, 7)
    snap = metrics.snapshot()
    assert any(
        "infragram_op_revives_total" in str(k) and v == 7
        for k, v in snap.get("counters", snap).items()
    ), snap
    metrics.reset()
