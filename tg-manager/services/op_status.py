"""Модель состояний операции — один источник правды.

Зачем отдельный модуль. Статусы операции были россыпью строковых литералов по
файлу op_worker.py и по хендлерам, и из-за этого жил класс ошибок, который
пользователь видел как ЛОЖЬ системы: операция, отработавшая 203 цели из 380 со
177 ошибками, закрывалась статусом `done` и уходила владельцу с зелёной
галочкой «✅ завершена». Формально исполнитель вернул сводку — значит `done`;
фактически работа не доведена. Тот же разрыв уже описан в
`services/operation_retry.py`: там пришлось ОТДЕЛЬНО восстанавливать правду по
счётчикам (done_items < total_items, err_cnt > 0), потому что самому статусу
верить было нельзя.

Здесь статус перестаёт врать: у недоведённой работы появляется собственное
терминальное состояние `partial`.

Жизненный цикл:

    pending ──> running ──┬──> done       все цели взяты
                          ├──> partial    часть целей взята, часть провалена
                          ├──> failed     не взято ничего
                          └──> cancelled  остановлена владельцем

    pending/running также могут вернуться в pending (повтор с backoff,
    отложенный запуск, сброс зависшей операции) — см. op_worker._maybe_requeue
    и _watchdog_stale.

Модуль намеренно без зависимостей (ни БД, ни сети, ни telethon): решение о
статусе — чистая функция от счётчиков, её можно вызвать и проверить где угодно.
"""

from __future__ import annotations

# ── Состояния ────────────────────────────────────────────────────────────────
PENDING = "pending"
RUNNING = "running"
DONE = "done"
PARTIAL = "partial"
FAILED = "failed"
CANCELLED = "cancelled"

# Операция ещё живёт: её нельзя ни повторять, ни считать завершённой.
# paused/scheduled/waiting_approval — состояния ожидания, которые ставят
# отдельные подсистемы (пауза владельцем, отложенный старт, апрув).
IN_FLIGHT = frozenset({PENDING, RUNNING, "paused", "scheduled", "waiting_approval"})

# Операция закончилась: перезаписывать её статус исполнителю уже нельзя.
# `partial` обязан быть здесь — иначе частично выполненная операция выглядит
# для всех guard'ов `status NOT IN (...)` как живая и её статус затирается
# задним числом.
TERMINAL = frozenset({DONE, PARTIAL, FAILED, CANCELLED})

# Работа доведена полностью. Только это — безоговорочный успех.
SUCCESSFUL = frozenset({DONE})

# Работа хотя бы частично выполнена: Telegram нас не отвергал целиком.
# Предохранитель (circuit breaker) и ML-пейсинг смотрят именно сюда: `partial` —
# не повод открывать цепь, реальные цели были взяты.
PRODUCTIVE = frozenset({DONE, PARTIAL})

ICONS: dict[str, str] = {
    PENDING: "⏳",
    RUNNING: "🔄",
    DONE: "✅",
    PARTIAL: "⚠️",
    FAILED: "❌",
    CANCELLED: "🚫",
}

LABELS: dict[str, str] = {
    PENDING: "ожидает",
    RUNNING: "выполняется",
    DONE: "завершена",
    PARTIAL: "завершена частично",
    FAILED: "завершилась с ошибкой",
    CANCELLED: "отменена",
}


def normalize(status) -> str:
    """Привести статус к каноническому виду (строка, нижний регистр, без полей)."""
    return (str(status or "")).strip().lower()


def is_terminal(status) -> bool:
    """Операция завершена — любым исходом."""
    return normalize(status) in TERMINAL


def is_in_flight(status) -> bool:
    """Операция ещё в работе или ждёт своей очереди."""
    return normalize(status) in IN_FLIGHT


def is_productive(status) -> bool:
    """Исполнитель взял хотя бы одну цель (успех или частичный успех)."""
    return normalize(status) in PRODUCTIVE


def icon(status) -> str:
    return ICONS.get(normalize(status), "❓")


def label(status) -> str:
    return LABELS.get(normalize(status), normalize(status) or "неизвестно")


def sql_terminal_list() -> str:
    """Литерал списка терминальных статусов для SQL-guard'ов.

    Нужен, чтобы `status NOT IN (...)` больше нигде не выписывался руками:
    именно рукописные списки забывали про `partial` и затирали его задним числом.
    Порядок фиксирован — строка попадает в тексты запросов и в тесты.
    """
    return "('" + "','".join((DONE, PARTIAL, FAILED, CANCELLED)) + "')"


def sql_in_flight_list() -> str:
    """Литерал списка ЖИВЫХ статусов для SQL-guard'ов.

    Пара к `sql_terminal_list`. Рукописный список живых статусов забывает
    `paused` ровно так же, как рукописный список терминальных забывал
    `partial`: приостановленная операция выглядит «не живой», и её запросто
    сносит уборка. Порядок фиксирован — строка попадает в тексты запросов.
    """
    return "('" + "','".join(sorted(IN_FLIGHT)) + "')"


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def classify_final(
    handler_status=None,
    ok=0,
    failed=0,
    done_items=None,
    total_items=None,
) -> str:
    """Финальный статус операции по тому, что РЕАЛЬНО сделал исполнитель.

    Решение принимается по счётчикам, а не по слову исполнителя: `status`,
    который вернул обработчик, — это его намерение, а ok/failed — факт. Раньше
    приоритет был обратным, поэтому «я отработал» перевешивало «177 целей не
    взято».

    Аргументы:
        handler_status: что вернул исполнитель в result["status"] (может быть
            None — тогда судим только по счётчикам).
        ok:      сколько целей обработано успешно.
        failed:  сколько целей провалено.
        done_items / total_items: счётчики прогресса из operation_queue. Ловят
            случай, когда исполнитель остановился раньше времени и НЕ записал
            остаток в failed — снаружи это выглядело как честный `done` с
            недобранной целью.

    Возвращает один из DONE / PARTIAL / FAILED.
    """
    ok_n, failed_n = _as_int(ok), _as_int(failed)
    said = normalize(handler_status)

    # Отмена и повтор решаются выше по стеку — сюда они доходить не должны,
    # но если дошли, уважаем: затирать их счётчиками нельзя.
    if said in (CANCELLED, "requeue"):
        return said

    if ok_n > 0 and failed_n > 0:
        return PARTIAL

    if ok_n == 0:
        # Ни одной взятой цели. Провал — и когда цели падали (failed>0), и
        # когда исполнитель отказался до работы (ok=0, failed=0, status=failed):
        # «отказ до начала» отличает не статус, а то, что счётчики пусты, и эту
        # разницу разбирает уже вызывающий (предохранитель её не считает сбоем).
        if failed_n > 0 or said == FAILED:
            return FAILED
        # ok=0, failed=0 и исполнитель не жаловался: обслуживающая операция без
        # целей (нечего было делать) — это честное «выполнено».
        return DONE

    # ok > 0, failed == 0.
    if said == FAILED:
        # Исполнитель взял часть целей и оборвался (нет аккаунтов, флуд,
        # исчерпан лимит). Не `failed` — работа была; и не `done` — не доведена.
        return PARTIAL

    # Недобор по прогрессу: цель ставили на total_items, а дошли до done_items.
    # Именно этот случай закрывался зелёным `done` на 203 из 380.
    total_n, done_n = _as_int(total_items), _as_int(done_items)
    if total_n > 0 and done_n > 0 and done_n < total_n:
        return PARTIAL

    return DONE
