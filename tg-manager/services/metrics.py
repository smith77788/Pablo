"""Метрики процесса в формате Prometheus — без новых зависимостей.

Находка аудита №6: в проекте не было ни метрик, ни трассировки, ни агрегации
ошибок — единственным инструментом наблюдения оставались логи. Для платформы,
выполняющей необратимые массовые действия чужими аккаунтами, это означает, что о
деградации узнаёшь из жалобы клиента. Симптом виден в истории самого продукта:
диагноз «работали 0 из 28» ставился по скриншотам отчёта, а не по графику.

Почему свой модуль, а не prometheus_client: зависимости в проекте курируются
(requirements.txt со сроком ревизии), а нужного здесь — четыре счётчика и
текстовый вывод — на 100 строк. Формат совместим с любым Prometheus-совместимым
сборщиком, так что переезд на библиотеку потом ничего не сломает.

Всё считается В ПАМЯТИ ПРОЦЕССА и это ОСОЗНАННО: метрики per-process — норма для
Prometheus (сборщик сам агрегирует по инстансам). Важное следствие для ROLE:
у процесса-воркера своего HTTP нет, его метрики отдаёт только собственный
endpoint, если его поднимут; пока воркер не выделен, всё видно на web.
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()

# {(имя, (метки...)) -> число}
_counters: dict[tuple, float] = {}
# {(имя, (метки...)) -> [сумма, количество]} — для средних (задержка операций)
_sums: dict[tuple, list] = {}
# {имя -> значение} — мгновенные показатели (насыщение пула)
_gauges: dict[tuple, float] = {}

_HELP = {
    "infragram_operations_total": "Операции по типу и исходу",
    "infragram_operation_seconds_sum": "Суммарная длительность операций, сек",
    "infragram_operation_seconds_count": "Число завершённых операций",
    "infragram_flood_events_total": "События FloodWait/PeerFlood",
    "infragram_session_deaths_total": "Смерти сессий (auth key убит)",
    "infragram_db_pool_connections": "Соединения пула БД",
}


def _key(name: str, labels: dict | None) -> tuple:
    if not labels:
        return (name, ())
    return (name, tuple(sorted((str(k), str(v)) for k, v in labels.items())))


def inc(name: str, labels: dict | None = None, value: float = 1.0) -> None:
    """Увеличить счётчик. Никогда не бросает — метрика не должна ронять работу."""
    try:
        with _lock:
            k = _key(name, labels)
            _counters[k] = _counters.get(k, 0.0) + float(value)
    except Exception:
        pass


def observe(name: str, seconds: float, labels: dict | None = None) -> None:
    """Записать длительность (сумма+количество → среднее на стороне сборщика)."""
    try:
        with _lock:
            k = _key(name, labels)
            cur = _sums.setdefault(k, [0.0, 0.0])
            cur[0] += float(seconds)
            cur[1] += 1.0
    except Exception:
        pass


def gauge(name: str, value: float, labels: dict | None = None) -> None:
    """Мгновенный показатель (перезаписывается)."""
    try:
        with _lock:
            _gauges[_key(name, labels)] = float(value)
    except Exception:
        pass


def _fmt_labels(labels: tuple) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
    return "{" + inner + "}"


def _escape(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render() -> str:
    """Текст в экспозиционном формате Prometheus."""
    with _lock:
        counters = dict(_counters)
        sums = dict(_sums)
        gauges = dict(_gauges)

    lines: list[str] = []
    seen_help: set[str] = set()

    def _emit(name: str, labels: tuple, value: float, kind: str) -> None:
        if name not in seen_help:
            seen_help.add(name)
            if name in _HELP:
                lines.append(f"# HELP {name} {_HELP[name]}")
            lines.append(f"# TYPE {name} {kind}")
        lines.append(f"{name}{_fmt_labels(labels)} {value:g}")

    for (name, labels), v in sorted(counters.items()):
        _emit(name, labels, v, "counter")
    for (name, labels), (s, c) in sorted(sums.items()):
        _emit(f"{name}_sum", labels, s, "counter")
        _emit(f"{name}_count", labels, c, "counter")
    for (name, labels), v in sorted(gauges.items()):
        _emit(name, labels, v, "gauge")

    lines.append(f"infragram_metrics_scrape_timestamp {time.time():.0f}")
    return "\n".join(lines) + "\n"


def snapshot() -> dict:
    """Срез для тестов и отладки."""
    with _lock:
        return {
            "counters": {f"{n}{_fmt_labels(l)}": v for (n, l), v in _counters.items()},
            "sums": {f"{n}{_fmt_labels(l)}": tuple(v) for (n, l), v in _sums.items()},
            "gauges": {f"{n}{_fmt_labels(l)}": v for (n, l), v in _gauges.items()},
        }


def reset() -> None:
    """Только для тестов."""
    with _lock:
        _counters.clear()
        _sums.clear()
        _gauges.clear()
