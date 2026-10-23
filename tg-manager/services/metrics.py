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
    # Механизмы надёжности очереди. Без этих счётчиков их срабатывания видны
    # только в логах: снаружи операция просто «не доехала», и понять, почему
    # именно — прервана по времени, отложена платформой или остановлена как
    # ядовитая — можно было лишь чтением логов постфактум.
    "infragram_op_timeouts_total": "Операции, прерванные по потолку прогона",
    "infragram_op_revives_total": "Возвраты операции в очередь после падения/зависания воркера",
    "infragram_op_poisoned_total": "Операции, остановленные по исчерпании бюджета живучести",
    "infragram_op_flood_defers_total": "Операции, отложенные до конца длинной флуд-паузы",
    "infragram_op_ceiling_resumes_total": "Здоровые длинные операции, продолженные повтором после потолка прогона",
    "infragram_flood_sleep_truncated_total": "Флуд-паузы, урезанные внутри прогона",
    "infragram_journal_read_failures_total":
        "Отказов прочитать журнал целей: операция отложена, чтобы не повторить сделанное",
    "infragram_account_lease_renew_drift_total":
        "Аккаунтов, у которых аренду снял кто-то другой, пока операция ими работала",
    "infragram_account_lease_regained_total":
        "Аккаунтов, чья пропавшая аренда взята заново до истечения TTL",
    "infragram_op_close_write_failures_total":
        "Сбои записи финала операции (исход не сохранён, строка осталась running)",
    "infragram_maintenance_step_failures_total":
        "Сбои шагов обслуживания воркера (сторожа, уборка, оповещения)",
    "infragram_broadcast_stop_write_failures_total":
        "Сбои записи остановки рассылки (риск воскрешения после рестарта)",
    "infragram_long_flood_stops_total":
        "Аккаунт закончил прогон из-за паузы Telegram длиннее допустимой",
    "infragram_recurring_chains_repaired_total":
        "Продолженные расписания, оборвавшиеся на закрытии круга",
    "infragram_invite_dedup_read_failures_total":
        "Сбои чтения журнала приглашений (дедуп на прогоне не действует)",
    "infragram_opt_out_read_failures_total":
        "Сбои чтения реестра «не писать» (фильтр на прогоне не действует)",
    "infragram_daily_limit_read_failures_total":
        "Сбои чтения суточного счёта действий (лимит на прогон не сработает)",
    "infragram_flood_pause_cut_by_cancel_total":
        "Флуд-паузы, прерванные отменой владельца (флот освободился раньше)",
    "infragram_op_fleet_starved_total":
        "Операции, закрытые из-за того, что свободных аккаунтов так и не стало",
    "infragram_executor_lease_errors_total":
        "Сбоев запроса аренды исполнителя (пауза очереди не включена, fail-open)",
    "infragram_executor_lease_denied_total":
        "Кругов цикла, на которых процесс НЕ разбирал очередь: аренда у другого",
    "infragram_executor_standdowns_total":
        "Сдач аренды на ходу: процесс гасил идущие операции и отпускал аккаунты",
    "infragram_op_cancel_orphans_closed_total":
        "Отменённых операций, итог которым дописал сторож (воркер умер между "
        "отменой и сворачиванием)",
    "infragram_notifications_undelivered_total":
        "Недоставленные уведомления владельцу (отчёт об операции до него не дошёл)",
    "infragram_budget_write_failures_total":
        "Потерянные записи суточного счёта действий (аккаунт могут взять сверх лимита)",
    "infragram_strike_history_write_failures_total":
        "Потерянные записи истории ударов (интервал повтора по цели перестаёт действовать)",
    "infragram_restriction_write_failures_total":
        "Потерянные сигналы об ограничении аккаунта (карантин и защита от волны банов его не увидят)",
    "infragram_op_report_after_close_total":
        "Падения отчёта об УЖЕ закрытой операции (исход в БД верный, сообщение или график могли не уйти)",
    "infragram_requeue_write_failures_total":
        "Сбои записи решения о повторе операции (операция осталась закрытой, хотя ошибка была ретраевой)",
    "infragram_journal_write_failures_total":
        "Потерянные строки успеха в журнале целей (повтор сделает цель второй раз)",
    "infragram_account_cooldown_write_failures_total":
        "Потерянные записи паузы аккаунта (аккаунт останется доступным сразу после флуда)",
    "infragram_account_rest_write_failures_total":
        "Потерянные записи отдыха аккаунта после тяжёлого прогона (после рестарта "
        "он снова выглядит отдохнувшим)",
    "infragram_broadcast_pin_write_failures_total":
        "Незакреплённые id рассылок (рассылка не запущена и снята, чтобы "
        "повтор операции не отправил сообщение всей аудитории второй раз)",
    "infragram_broadcast_dedup_read_failures_total":
        "Непрочитанные списки доставленных у рассылок (рассылка отложена, "
        "чтобы не отправить сообщение всей аудитории второй раз)",
    "infragram_dm_dedup_write_failures_total":
        "Непоставленные метки дедупа ЛС (сообщение не отправлено, чтобы не "
        "написать человеку второй раз)",
    "infragram_dm_unmark_failures_total":
        "Не снятые предзаписи ЛС (человек числится получившим сообщение, "
        "хотя Telegram его не рассматривал)",
    "infragram_dm_settle_write_failures_total":
        "Незаписанные исходы ЛС (счёт по статусам кампании расходится с тем, "
        "что реально произошло)",
    "infragram_invite_dedup_write_failures_total":
        "Потерянные записи дедупа инвайта (следующий прогон может пригласить повторно)",
    "infragram_invite_unmark_failures_total":
        "Не снятые предзаписи инвайта (цели помечены приглашёнными, хотя их "
        "Telegram не рассматривал — больше их не позовут)",
    "infragram_growth_dedup_read_failures_total":
        "Непрочитанные списки отработанных групп Growth Agent (операция отложена "
        "на повтор, чтобы промо не ушло в те же группы второй раз)",
    "infragram_background_writes_dropped_total":
        "Фоновые записи (телеметрия, журнал соответствия, конверсии), не "
        "успевшие лечь до остановки процесса и отменённые — они потеряны",
    "infragram_protective_alerts_no_recipient_total":
        "Оповещения о сбое защитных записей, которые отправлять некому "
        "(ADMIN_IDS пуст) — о поломке защиты не узнаёт никто",
    "infragram_growth_dedup_write_failures_total":
        "Незаписанные группы Growth Agent (поста в такую группу не было: иначе "
        "следующий запуск отправил бы туда промо второй раз)",
    "infragram_recurring_reschedule_failures_total":
        "Повторяющиеся операции, у которых не удалось поставить следующий запуск",
    "infragram_op_owner_sem_defers_total":
        "Операции, возвращённые в очередь: место на владельца не освободилось",
    "infragram_op_stalls_total": "Операции, о застревании которых предупредили владельца",
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
