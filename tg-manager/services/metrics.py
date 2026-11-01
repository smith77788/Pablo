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

import logging
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
    "infragram_pacing_sync_errors_total": (
        "Сбои обмена темпом флота с базой (services/pacing_store): пока они "
        "идут, процесс решает по своим исходам, а соседи его флудов не видят"),
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
    "infragram_anti_storm_arm_write_failures_total":
        "Незаписанные включения защиты от волны банов (флот продолжает работать в полном темпе во время чистки)",
    "infragram_anomaly_check_failures_total":
        "Сбои проверок разбора аномалий по метке detector (ослепший глаз неотличим от здорового продукта)",
    "infragram_queue_frozen_check_failures_total":
        "Сбои проверки «очередь не разбирается» (детектор остановки исполнителя ослеп, "
        "и остановка выглядит обычным ожиданием)",
    "infragram_executor_standdown_tasks_left_total":
        "Задачи операций, не свернувшиеся за отведённое время при передаче "
        "очереди другому процессу (их сессии могут достаться двум процессам "
        "сразу)",
    "infragram_unplanned_write_cancel_failures_total":
        "Защитные записи, потерянные НЕ на плановой остановке: передача "
        "очереди другому процессу или снятие зависшего прогона",
    "infragram_protective_write_cancelled_total":
        "Защитные записи, не состоявшиеся из-за остановки процесса (деплой, "
        "передача очереди): потеря настоящая, но это не отказ базы",
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
    "infragram_network_node_mark_write_failures_total":
        "Связка: сколько раз не удалось запомнить созданный узел. Операция "
        "закрывалась как частично выполненная: без отметки следующий прогон "
        "создал бы второй такой же канал",
    "infragram_gp_asset_mark_write_failures_total":
        "Global Presence: сколько раз не удалось записать id созданного канала "
        "или бота. Цель закрывалась на месте: без отметки разбор брошенных "
        "целей вернул бы её в очередь и аккаунт создал бы второй ресурс",
    "infragram_network_broadcast_mark_write_failures_total":
        "Сетевая рассылка: сколько раз не удалось закрепить бота за операцией. "
        "Рассылка по нему не запускалась: без отметки повтор прогона отправил бы "
        "сообщение всей аудитории бота второй раз",
    "infragram_broadcast_subset_write_failures_total":
        "Несохранённые списки получателей рассылок (рассылка не запущена и "
        "снята, чтобы докатка не отправила её всей аудитории бота)",
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



# Плановая ли остановка процесса. Ставится один раз из `op_worker.shutdown`
# (SIGTERM на деплое) и больше не снимается: процесс после этого умирает.
# Нужен, чтобы отличить потерю защитной записи на штатном выпуске от потери
# при передаче очереди или снятии зависшего прогона — первое случается на
# каждый пуш и тревогой быть не может, второе редко и обязано ею быть.
_shutting_down = False


def set_shutting_down(value: bool = True) -> None:
    """Отметить плановую остановку процесса. Зовётся из `op_worker.shutdown`."""
    global _shutting_down
    _shutting_down = bool(value)

def note_write_cancelled(
    module: str, where: str, ctx: str = "", lost: str = "",
) -> None:
    """Защитная запись не состоялась из-за ОТМЕНЫ задачи, а не отказа БД.

    Писатели защиты по всему проекту построены одинаково: попытка, пауза,
    вторая попытка, затем `log.error` и счётчик потери. Ловят они
    `except Exception`, а `asyncio.CancelledError` с Python 3.8 не
    `Exception` — то есть отмена проходила сквозь них без лога и без счётчика,
    и слепы они оказывались ровно к нашим собственным остановкам: SIGTERM на
    деплое и передача очереди другому процессу. Дороже всего это для отметок,
    которые пишутся ПОСЛЕ действия: остановка возвращает операции в очередь,
    то есть повторный прогон не «возможен», а гарантирован, и он сделает цель
    второй раз.

    Дверь одна: иначе у каждого писателя завёлся бы свой текст и свой счётчик,
    и общая картина «сколько защиты съели наши рестарты» не собиралась бы
    нигде.

    ОТМЕНУ ВЫЗЫВАЮЩИЙ ОБЯЗАН ПРОБРОСИТЬ (`raise` сразу после этого вызова):
    задача, не заметившая отмены, продолжает работать сессией, аренда которой
    уже снята, — это AUTH_KEY_DUPLICATED.

    Счётчик намеренно НЕ `*_failures_total` и НЕ в
    `op_worker._PROTECTIVE_FAILURE_COUNTERS`: деплой — штатное и частое
    событие, а тревога, приходящая владельцу на каждый выпуск, перестанет
    читаться, и в этом шуме потеряется настоящий сбой защиты.
    """
    try:
        logging.getLogger(module).warning(
            "%s: защитная запись не состоялась — задачу отменили%s (%s). "
            "Это остановка процесса, а не отказ базы, но потеря настоящая",
            str(module).rsplit(".", 1)[-1], ctx or "", lost or "неизвестно",
        )
    except Exception:
        pass
    planned = bool(_shutting_down)
    try:
        inc("infragram_protective_write_cancelled_total",
            {"where": str(where) or "unknown",
             "planned": "1" if planned else "0"})
    except Exception:
        pass
    if not planned:
        # НЕ плановая остановка — значит это не деплой, а передача очереди
        # другому процессу или снятие зависшего прогона сторожем живучести.
        # Такое случается редко, поэтому тревога владельцу здесь шума не даёт,
        # а потеря ровно та же: повторный прогон сделает цель второй раз.
        try:
            inc("infragram_unplanned_write_cancel_failures_total",
                {"where": str(where) or "unknown"})
        except Exception:
            pass
