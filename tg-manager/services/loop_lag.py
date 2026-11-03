"""Сторож цикла событий: замер задержки и снимок стека, когда цикл встал.

ЗАЧЕМ. Бот, HTTP-API мини-аппа, исполнитель операций и ~50 фоновых циклов —
один процесс и один цикл событий. Любой синхронный кусок в корутине
(разбор файла, сборка Excel, тяжёлый цикл по строкам, синхронный сетевой
вызов) останавливает всё сразу: бот не отвечает, мини-апп висит на
спиннере, у операций не продлевается аренда. Снаружи это выглядит как
«тормозит», а в логах нет НИЧЕГО — блокирующий код не пишет о том, что он
блокирует. 03.11.2026 так нашлись четыре места, замораживавших процесс до
минуты (см. tests/test_no_heavy_cpu_on_event_loop.py), — чтением кода, а не по
сигналу из прода. Этот модуль даёт сигнал.

ДВЕ ЧАСТИ.

* Корутина-пульс (`run`): каждые `interval` секунд засыпает и меряет, на
  сколько позже запланированного проснулась. Это и есть задержка цикла —
  то, на сколько опаздывает любой другой ответ в процессе. Пишется в метрики
  (`infragram_event_loop_lag_seconds`, максимум за окно и счётчик замираний).

* Поток-сторож. Пока цикл стоит, корутина-пульс не может ничего сказать — она
  сама ждёт. Поэтому отдельный поток смотрит на время последнего пульса, и
  если цикл молчит дольше `stall_after` секунд, снимает СТЕК потока цикла
  (`sys._current_frames`) и пишет его в лог. Это ровно то место, которое
  держит процесс: файл, строка, функция. Локальные переменные не
  печатаются — только имена файлов, функций и строки кода, секретов там нет.
  Одно замирание — один снимок; снимков не больше одного в `dump_every` с.

Сторож ничего не чинит и ничего не прерывает: он наблюдатель. Сбой внутри
него не должен трогать продукт — всё обёрнуто, метрики не бросают.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback

from services import metrics

log = logging.getLogger(__name__)

LAG = "infragram_event_loop_lag_seconds"
LAG_MAX = "infragram_event_loop_lag_max_seconds"
STALLS = "infragram_event_loop_stalls_total"

# Состояние одного процесса: один цикл, один сторож. Доступ из двух потоков —
# под замком.
_lock = threading.Lock()
_state = {
    "last_beat": 0.0,        # time.monotonic() последнего пульса
    "lag": 0.0,              # последняя измеренная задержка
    "window_max": 0.0,       # максимум за текущее окно
    "window_started": 0.0,
    "loop_thread_id": None,
    "running": False,
}

# Окно для «максимума»: последнее значение задержки ничего не говорит о
# кратком замирании между сборами метрик, максимум за минуту — говорит.
_WINDOW_SEC = 60.0


def snapshot() -> dict:
    """Текущее состояние для проверки готовности и отладки."""
    with _lock:
        last = _state["last_beat"]
        return {
            "running": bool(_state["running"]),
            "lag_sec": round(float(_state["lag"]), 4),
            "max_lag_sec_1m": round(float(_state["window_max"]), 4),
            "since_beat_sec": (round(time.monotonic() - last, 3) if last else None),
        }


def _record(lag: float, now: float, stall_after: float) -> None:
    with _lock:
        _state["last_beat"] = now
        _state["lag"] = lag
        if now - _state["window_started"] >= _WINDOW_SEC:
            _state["window_started"] = now
            _state["window_max"] = 0.0
        if lag > _state["window_max"]:
            _state["window_max"] = lag
        window_max = _state["window_max"]
    metrics.gauge(LAG, lag)
    metrics.gauge(LAG_MAX, window_max)
    if lag >= stall_after:
        metrics.inc(STALLS)


def format_stack(thread_id: int | None, limit: int = 25) -> str:
    """Стек потока цикла: только места в коде, без значений переменных."""
    if thread_id is None:
        return "(поток цикла неизвестен)"
    frame = sys._current_frames().get(thread_id)
    if frame is None:
        return "(поток цикла не найден)"
    return "".join(traceback.format_stack(frame, limit=limit))


class _Watchdog(threading.Thread):
    def __init__(self, stall_after: float, dump_every: float, poll: float):
        super().__init__(name="loop-lag-watchdog", daemon=True)
        self.stall_after = stall_after
        self.dump_every = dump_every
        self.poll = poll
        self.stop_event = threading.Event()
        self._reported_beat = 0.0
        self._last_dump = 0.0

    def check_once(self, now: float | None = None) -> str | None:
        """Один шаг проверки; возвращает снимок стека, если он снят (для тестов)."""
        now = time.monotonic() if now is None else now
        with _lock:
            last = _state["last_beat"]
            tid = _state["loop_thread_id"]
        if not last:
            return None
        stalled = now - last
        if stalled < self.stall_after:
            return None
        # Одно замирание — один снимок (пульс за это время не обновился).
        if last == self._reported_beat or now - self._last_dump < self.dump_every:
            return None
        self._reported_beat = last
        self._last_dump = now
        stack = format_stack(tid)
        log.warning(
            "Цикл событий стоит уже %.1f с — весь процесс (бот, мини-апп, "
            "операции) не отвечает. Сейчас цикл держит:\n%s",
            stalled, stack,
        )
        return stack

    def run(self) -> None:  # pragma: no cover — бесконечный цикл потока
        while not self.stop_event.wait(self.poll):
            try:
                self.check_once()
            except Exception:
                # Наблюдатель не имеет права ронять что-либо, включая себя.
                pass


async def run(
    interval: float = 0.5,
    stall_after: float = 1.0,
    dump_after: float = 2.0,
    dump_every: float = 60.0,
) -> None:
    """Пульс цикла событий плюс поток-сторож. Работает до отмены.

    interval    — как часто меряем задержку;
    stall_after — с какой задержки пульс считается «замиранием» (счётчик);
    dump_after  — сколько цикл должен молчать, чтобы сторож снял стек;
    dump_every  — не чаще одного снимка за столько секунд.
    """
    loop = asyncio.get_running_loop()
    now = time.monotonic()
    with _lock:
        _state.update(last_beat=now, lag=0.0, window_max=0.0, window_started=now,
                      loop_thread_id=threading.get_ident(), running=True)
    dog = _Watchdog(stall_after=dump_after, dump_every=dump_every,
                    poll=min(0.25, dump_after / 4))
    dog.start()
    try:
        while True:
            t0 = loop.time()
            await asyncio.sleep(interval)
            lag = max(0.0, loop.time() - t0 - interval)
            _record(lag, time.monotonic(), stall_after)
            if lag >= stall_after:
                log.warning("Цикл событий опоздал на %.2f с (порог %.1f с)",
                            lag, stall_after)
    finally:
        dog.stop_event.set()
        with _lock:
            _state["running"] = False
