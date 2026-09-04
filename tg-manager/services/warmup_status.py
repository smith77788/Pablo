"""Честный статус прогрева: почему план на самом деле не двигается.

Разрыв, который это закрывает. Экран прогрева показывал ровно две вещи —
`status` и «День X/Y». Но движок прогрева (`account_warmer`) МОЛЧА пропускает
день в шести разных случаях: аккаунт занят операцией, выключен, забанен, в
спам-блоке, с протухшей сессией, без session_str. План при этом остаётся
`active`, день не растёт, и пользователь неделями видит бодрое
«🟢 Активен · День 3/21» на аккаунте, который умер на второй день.

Хуже того: движок сам ставит план на паузу при бане, спам-ограничении и
длинном FloodWait — и это выглядело В ТОЧНОСТИ как пауза, поставленная
человеком. Пользователь видел «⏸ Пауза», жал «▶ Возобновить» и снова гнал
действия по флагнутому аккаунту — то есть добивал его своими руками.

Здесь живут решения, а не запросы: по строке плана и текущему времени
сказать, что с ним происходит на самом деле, и можно ли его возобновлять.
Чистые функции — проверяются без базы и без Telegram.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Цикл прогрева ходит раз в час и берёт план, если последнее действие было
# больше 20 часов назад. Плюс план откладывается на локальную ночь аккаунта
# (до ~8 часов). Значит здоровый план обязан отмечаться минимум раз в ~28 часов;
# 40 берём с запасом, чтобы не пугать человека ложной тревогой.
STALL_AFTER_H = 40

# Статусы аккаунта, при которых прогрев физически невозможен: коннект либо
# не пройдёт, либо только навредит.
DEAD_STATUSES = ("banned", "deactivated", "session_expired")
RESTRICTED_STATUSES = ("spamblock",)

# ── Причины паузы ──────────────────────────────────────────────────────────
# 'user' ставит человек, остальные — движок. Разделение существует ровно для
# того, чтобы не предлагать «Возобновить» там, где возобновление вредит.
PAUSE_USER = "user"
AUTO_PAUSE_REASONS = ("banned", "restricted", "flood")

_PAUSE_LABEL = {
    PAUSE_USER: "⏸ Пауза (вами)",
    "banned": "🚫 Аккаунт забанен",
    "restricted": "⚠️ Аккаунт ограничен",
    "flood": "🕒 Длинный FloodWait",
}
_PAUSE_HINT = {
    PAUSE_USER: "Прогрев остановлен вручную. Можно возобновить в любой момент.",
    "banned": "Telegram заблокировал аккаунт — прогрев остановлен, чтобы не тратить"
              " на него циклы. Возобновление ничего не даст: нужен другой аккаунт.",
    "restricted": "Telegram ограничил аккаунт (спам-блок). Прогрев остановлен,"
                  " чтобы не добивать его. Дождитесь снятия ограничения —"
                  " реабилитация делает это сама — и возобновите.",
    "flood": "Telegram попросил очень долгую паузу. Это не поломка: подождите"
             " и возобновите прогрев вручную.",
}

# ── Причины молчаливого пропуска дня ───────────────────────────────────────
_SKIP_LABEL = {
    "busy": "Аккаунт занят операцией",
    "inactive": "Аккаунт выключен",
    "no_session": "У аккаунта нет сессии",
    "not_found": "Аккаунт недоступен",
    "banned": "Аккаунт забанен",
    "spamblock": "Аккаунт в спам-блоке",
    "deactivated": "Аккаунт удалён в Telegram",
    "session_expired": "Сессия аккаунта протухла",
    "all_failed": "Все действия дня провалились",
}
_SKIP_HINT = {
    "busy": "Прогрев не запускается на аккаунте, занятом операцией — две"
            " одновременные сессии одного аккаунта ведут к бану. Он догреется,"
            " когда операции освободят аккаунт.",
    "inactive": "Включите аккаунт в списке аккаунтов — прогрев продолжится сам.",
    "no_session": "Аккаунт без сессии греть нечем. Переимпортируйте его.",
    "not_found": "Аккаунт не читается из базы. Проверьте его в списке аккаунтов.",
    "banned": "Аккаунт заблокирован Telegram. Прогрев на нём бессмыслен —"
              " отмените план и грейте другой аккаунт.",
    "spamblock": "Аккаунт в спам-блоке. Прогрев не поможет, пока ограничение"
                 " не снято — этим занимается реабилитация.",
    "deactivated": "Аккаунт удалён на стороне Telegram. Отмените план.",
    "session_expired": "Сессия недействительна. Переимпортируйте аккаунт.",
    "all_failed": "Ни одно действие не прошло — обычно это мёртвый прокси или"
                  " ограничение аккаунта. Проверьте прокси аккаунта.",
}

# Пропуски, которые сами не пройдут: пока человек не вмешается, план стоит.
_TERMINAL_SKIPS = ("no_session", "not_found", "banned", "deactivated",
                   "session_expired")


def skip_label(reason: str | None) -> str:
    if not reason:
        return ""
    return _SKIP_LABEL.get(reason, reason)


def _hours_since(ts, now: datetime) -> float | None:
    if ts is None:
        return None
    try:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds() / 3600.0
    except (AttributeError, TypeError, ValueError):
        return None


def classify(plan: dict, now: datetime | None = None) -> dict:
    """Что на самом деле происходит с планом прогрева.

    Возвращает {state, label, hint, resumable, attention}:
      • state — running / stalled / blocked / paused / completed / cancelled;
      • label — короткая строка для списка;
      • hint — человеческое объяснение, что делать (пусто, если всё хорошо);
      • resumable — показывать ли кнопку «Возобновить»;
      • attention — план требует вмешательства человека (для счётчика/баннера).

    `stalled` — главный вклад этой функции: план числится активным, но день не
    двигается дольше, чем позволяет расписание цикла. Раньше это состояние
    было неотличимо от нормальной работы.
    """
    now = now or datetime.now(timezone.utc)
    status = (plan.get("status") or "active").lower()

    if status == "completed":
        return {"state": "completed", "label": "✅ Завершён", "hint": "",
                "resumable": False, "attention": False}
    if status == "cancelled":
        return {"state": "cancelled", "label": "🚫 Отменён", "hint": "",
                "resumable": False, "attention": False}
    if status == "failed":
        return {"state": "blocked", "label": "❌ Ошибка",
                "hint": plan.get("pause_detail") or "Прогрев остановлен ошибкой.",
                "resumable": False, "attention": True}

    if status == "paused":
        reason = (plan.get("pause_reason") or PAUSE_USER).lower()
        if reason not in _PAUSE_LABEL:
            reason = PAUSE_USER
        auto = reason in AUTO_PAUSE_REASONS
        return {
            "state": "blocked" if auto else "paused",
            "label": _PAUSE_LABEL[reason],
            "hint": _PAUSE_HINT[reason],
            # Возобновлять забаненный аккаунт нечего: это не пауза, это конец.
            "resumable": reason != "banned",
            "attention": auto,
        }

    # status == 'active'
    skip = (plan.get("last_skip_reason") or "").lower() or None
    ref = plan.get("last_action_at") or plan.get("started_at")
    idle_h = _hours_since(ref, now)
    stalled = idle_h is not None and idle_h >= STALL_AFTER_H

    if stalled or (skip and skip in _TERMINAL_SKIPS):
        label = "⏳ Стоит на месте"
        hint = (_SKIP_HINT.get(skip)
                or "План числится активным, но за последние "
                   f"{int(idle_h) if idle_h else STALL_AFTER_H} ч ни одно действие не"
                   " выполнилось. Обычно это мёртвый прокси, выключенный аккаунт"
                   " или занятость операциями.")
        if skip:
            label = "⏳ " + skip_label(skip)
        return {"state": "stalled", "label": label, "hint": hint,
                "resumable": False, "attention": True}

    label = "🟢 Активен"
    hint = ""
    if skip:
        # Пропуск был, но недавний и проходящий: показываем честно, тревогу не
        # поднимаем — само рассосётся.
        label = "🟢 Активен · " + skip_label(skip)
        hint = _SKIP_HINT.get(skip, "")
    return {"state": "running", "label": label, "hint": hint,
            "resumable": False, "attention": False}


def can_resume(pause_reason: str | None, acc_status: str | None,
               is_active) -> tuple[bool, str]:
    """Можно ли возобновлять прогрев. (можно, причина отказа).

    Возобновление прогрева на забаненном или ограниченном аккаунте — не
    нейтральное действие: оно снова гонит по нему действия и усугубляет
    положение. Поэтому отказ, а не «попробуем и посмотрим».
    """
    st = (acc_status or "active").lower()
    if is_active is False:
        return False, ("Аккаунт выключен. Включите его в списке аккаунтов —"
                       " после этого прогрев можно возобновить.")
    if st in DEAD_STATUSES:
        return False, (f"Аккаунт в состоянии «{st}» — прогрев на нём невозможен."
                       " Отмените план и грейте другой аккаунт.")
    if st in RESTRICTED_STATUSES:
        return False, ("Аккаунт в спам-блоке. Прогрев сейчас только навредит:"
                       " дождитесь снятия ограничения (этим занимается"
                       " реабилитация) и возобновите.")
    if (pause_reason or "").lower() == "banned":
        return False, ("План остановлен из-за бана аккаунта. Возобновление"
                       " ничего не изменит — отмените план.")
    return True, ""


def build_pause_alert(name: str, reason: str, day: int, target: int,
                      detail: str | None = None) -> str:
    """Уведомление о том, что движок сам остановил прогрев.

    Раньше об этом не сообщалось вообще: аккаунт ловил бан на 4-й день из 21,
    план вставал, и человек узнавал об этом, только если сам открывал экран —
    и то видел лишь «Пауза».
    """
    head = {
        "banned": "🚫 <b>Прогрев остановлен: аккаунт забанен</b>",
        "restricted": "⚠️ <b>Прогрев остановлен: аккаунт ограничен</b>",
        "flood": "🕒 <b>Прогрев остановлен: длинная пауза от Telegram</b>",
    }.get(reason, "⏸ <b>Прогрев остановлен</b>")
    tail = _PAUSE_HINT.get(reason, "")
    body = f"\n\n<b>{name}</b> — день {int(day or 0)} из {int(target or 0)}."
    if detail:
        body += f"\n<code>{str(detail)[:120]}</code>"
    return f"{head}{body}\n\n{tail}"
