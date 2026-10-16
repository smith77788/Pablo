"""Централизованный мутатор статуса аккаунта — единый источник правды смены
`tg_accounts.acc_status` (Ban Weather, Фаза 1).

Захват события делает триггер БД (trg_immunity_capture_status) — он ловит ЛЮБОЙ
путь смены статуса. Эта функция — предпочтительный путь: помимо смены статуса она
ОБОГАЩАЕТ только что записанное триггером событие человекочитаемой причиной,
источником и контекстом. Обогащение опционально для захвата, но повышает качество
автопсии. Сайты смены статуса мигрируют на неё постепенно (не критично для захвата).

См. docs/BAN_WEATHER_MODULE.md.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


# ── Словарь статусов: что считается «мёртвым» ───────────────────────────────
#
# Один набор на весь продукт. Раньше его выписывал каждый читатель сам, и
# наборы разошлись: единая дверь выбора аккаунтов (`resource_selector`) и экран
# флота считали мёртвым и `spamblock`, а риск-пульс
# (`infra_memory.get_account_health`) — нет. Аккаунт, получивший спам-блок
# после PEER_FLOOD, показывался владельцу ЗДОРОВЫМ ровно в том месте, которое
# для этого и сделано: операции его уже не брали, экран флота называл мёртвым,
# а приборный щиток — «здоров». Расхождение читается как «из 52 аккаунтов
# работают 20, а почему — непонятно».
#
# Набор — объединение всех прежних: исключать из работы аккаунт, помеченный
# `deleted` или `frozen`, тоже верно, и ослаблять защиту при сведении нельзя.
DEAD_STATUSES = frozenset({
    "banned", "deactivated", "session_expired", "spamblock", "deleted",
    "frozen",
})


def is_dead(acc_status: Optional[str]) -> bool:
    """Статус означает, что аккаунт для действий не годится (и сам не вернётся)."""
    return str(acc_status or "active").strip().lower() in DEAD_STATUSES


# Подмножества словаря по тому, ЧТО с аккаунтом делать. Владельцу нельзя
# советовать «очистите их» одним списком: забаненный аккаунт потерян, а
# спам-блок снимается и сессия переподключается — по общему совету он
# выбрасывает рабочие аккаунты. Разбиение полное и без пересечений (стережёт
# test_dead_status_is_one_vocabulary), поэтому седьмой статус в словаре
# заставит решить, к какой группе он относится, а не тихо выпадет из советов.
LOST_STATUSES = frozenset({"banned", "deactivated", "deleted"})
RESTRICTED_STATUSES = frozenset({"spamblock", "frozen"})
SESSION_STATUSES = frozenset({"session_expired"})

# Эффективный статус (`account_manager.effective_account_status`) добавляет к
# словарю два значения, которых в базе нет: `archived` — аккаунт выключен
# владельцем, `no_session` — строки сессии нет. Гейты, судящие по ЭФФЕКТИВНОМУ
# статусу, обязаны брать этот набор: свои копии из трёх значений пропускали
# `deleted` и `frozen`, и самый баноопасный путь продукта (инвайт) брал такой
# аккаунт в работу. `session_expired` эффективный статус отдаёт как `active`,
# когда строка сессии на месте, — в наборе он остаётся, чтобы набор был
# надмножеством словаря, а не отдельным третьим списком.
EFFECTIVE_EXTRA_STATUSES = frozenset({"archived", "no_session"})
EFFECTIVE_DEAD_STATUSES = frozenset(DEAD_STATUSES | EFFECTIVE_EXTRA_STATUSES)


def is_effectively_dead(effective_status: Optional[str]) -> bool:
    """То же, но для значения из `effective_account_status`."""
    return (str(effective_status or "active").strip().lower()
            in EFFECTIVE_DEAD_STATUSES)


# Как статус читается владельцу. В базе лежит английский код, а владелец
# английского не понимает: подписи писал каждый экран свои, а где не писал —
# код попадал в текст как есть («Аккаунт в состоянии «banned»»). Подписи тоже
# одни на продукт, и полнота проверяется тестом словаря.
RU_LABEL = {
    "banned": "забанен",
    "deactivated": "аккаунт удалён",
    "deleted": "аккаунт удалён в Telegram",
    "frozen": "аккаунт заморожен",
    "session_expired": "сессия отозвана",
    "spamblock": "спам-блок",
    "archived": "выключен владельцем",
    "no_session": "нет сессии",
}


def ru_label(acc_status: Optional[str]) -> str:
    """Человеческая подпись статуса; неизвестный код отдаём как есть."""
    code = str(acc_status or "active").strip().lower()
    return RU_LABEL.get(code, code)


def sql_status_list(statuses) -> str:
    """Готовый список значений для SQL из любого набора словаря.

    Нужен там, где условие берёт не весь набор, а осознанное подмножество
    (например, только безвозвратно потерянные: `sql_status_list(LOST_STATUSES)`).
    Значения — литералы словаря, не пользовательский ввод.
    """
    values = {str(s).strip().lower() for s in statuses if str(s).strip()}
    unknown = values - (DEAD_STATUSES | EFFECTIVE_EXTRA_STATUSES)
    if unknown:
        raise ValueError(f"нет в словаре статусов: {sorted(unknown)}")
    return ", ".join(f"'{status}'" for status in sorted(values))


def sql_dead_list(*extra: str) -> str:
    """Готовый список для SQL: `NOT IN (sql_dead_list())`.

    Подставляется в текст запроса, поэтому значения — только из набора выше
    (литералы в коде, не пользовательский ввод).

    `extra` — дополнительные статусы, которые СТРОЖЕ набора: например, экран
    виртуального админа не трогает ещё и греющийся аккаунт (`warming`). Это
    всегда расширение набора, не замена: ослабить защиту так нельзя.
    """
    bad = {str(s).strip().lower() for s in extra if str(s).strip()}
    return ", ".join(f"'{status}'" for status in sorted(DEAD_STATUSES | bad))


def sql_not_dead(column: str = "acc_status", *, default: str = "active",
                 extra: tuple[str, ...] = ()) -> str:
    """Готовое условие «статус аккаунта не мёртвый» для текста запроса.

    Одна дверь на все выборки аккаунтов под действие. Раньше это условие
    выписывал каждый вызов сам, и в репозитории жило больше двадцати копий
    набора из трёх-четырёх статусов. Из-за этого массовая дверь аккаунт уже не
    брала, а одиночная операция брала и работала мёртвой сессией; `spamblock`
    и вовсе числился рабочим в большинстве копий.

    `column` — имя колонки с алиасом таблицы (`a.acc_status`), `default` —
    значение для NULL (для проверки неважно: и 'active', и 'ok' не мёртвые),
    `extra` — статусы строже набора (см. sql_dead_list).
    """
    return (f"COALESCE({column}, '{default}') NOT IN ("
            + sql_dead_list(*extra) + ")")


async def set_status(
    pool,
    acc_id: int,
    new_status: str,
    *,
    reason: Optional[str] = None,
    source: Optional[str] = None,
    context: Optional[dict[str, Any]] = None,
) -> bool:
    """Сменить acc_status аккаунта и обогатить событие.

    Возвращает True, если статус реально изменился (событие записано триггером).
    Fail-soft: обогащение не критично — при сбое смена статуса всё равно в силе.
    """
    from services.logger import log_exc_swallow

    try:
        row = await pool.fetchrow(
            """UPDATE tg_accounts
               SET acc_status = $2
               WHERE id = $1 AND acc_status IS DISTINCT FROM $2
               RETURNING id""",
            acc_id, new_status,
        )
    except Exception:
        log_exc_swallow(log, f"account_status: смена статуса acc={acc_id} упала")
        return False

    if not row:
        return False  # статус не изменился — триггер не сработал, обогащать нечего

    if reason is None and source is None and context is None:
        return True

    # Обогащаем последнее (только что созданное триггером) событие для этого acc.
    try:
        await pool.execute(
            """UPDATE account_status_events
               SET reason = COALESCE($2, reason),
                   source = COALESCE($3, source),
                   context = COALESCE($4::jsonb, context)
               WHERE id = (
                   SELECT id FROM account_status_events
                   WHERE acc_id = $1 AND new_status = $5 AND processed_at IS NULL
                   ORDER BY id DESC LIMIT 1
               )""",
            acc_id, reason, source,
            json.dumps(context, ensure_ascii=False) if context is not None else None,
            new_status,
        )
    except Exception:
        log_exc_swallow(log, f"account_status: обогащение события acc={acc_id} упало")

    return True
