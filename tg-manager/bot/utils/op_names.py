"""Подписи операций и их статусов для ответов бота.

Экраны бота печатали код типа («mass_invite») и статус («failed») как есть.
Описание берётся из реестра operation_bus.OP_REGISTRY, статус — из
services.op_status.LABELS: один источник для бота и мини-аппа.
"""
from __future__ import annotations


def op_name(op_type, fallback: str | None = "Операция") -> str:
    """Название операции по-русски. Незнакомый тип — `fallback`
    (None — вернуть сам код: для сводок, где важно различать строки)."""
    from services import operation_bus as _ob
    code = str(op_type or "")
    desc = _ob.OP_REGISTRY.get(code, {}).get("description")
    if desc:
        return desc
    return code if fallback is None else fallback


def status_label(status) -> str:
    from services import op_status as _os
    return _os.label(status)


# Статусы рассылок, сессий прогрева и планов. Помимо статусов операции у них
# свои слова (active, completed, stopped, sending, draft), и экраны печатали
# их как есть: «Статус: active». Род — по слову, к которому подпись стоит:
# «рассылка/сессия завершена», но «план завершён».
_RUN_STATUS_F = {
    "active": "идёт", "running": "идёт", "sending": "идёт отправка",
    "completed": "завершена", "done": "завершена", "finished": "завершена",
    "stopped": "остановлена", "draft": "черновик", "scheduled": "запланирована",
    "ready": "готова к запуску", "executing": "выполняется",
}
_RUN_STATUS_M = {
    "active": "идёт", "running": "выполняется", "pending": "ожидает",
    "completed": "завершён", "done": "завершён", "finished": "завершён",
    "partial": "завершён частично", "failed": "завершился с ошибкой",
    "cancelled": "отменён", "stopped": "остановлен", "paused": "на паузе",
    "draft": "черновик", "scheduled": "запланирован",
}


def run_status(status, masculine: bool = False) -> str:
    """Статус рассылки/сессии/плана по-русски; незнакомый — «неизвестно»,
    а не английское слово из базы."""
    from services import op_status as _os
    code = str(status or "").strip().lower()
    if masculine:
        return _RUN_STATUS_M.get(code, "неизвестно")
    return _RUN_STATUS_F.get(code) or _os.LABELS.get(code) or "неизвестно"


# Короткие подписи состояния аккаунта для кнопок (там, где нет места под
# полную подпись из карточки аккаунта).
_ACC_SHORT = {
    "active": "✅", "cooldown": "⏳ пауза", "spamblock": "⚠️ спам-блок",
    "banned": "❌ бан", "deactivated": "💀 удалён", "deleted": "💀 удалён",
    "frozen": "🧊 заморожен", "session_expired": "🔑 нет сессии",
    "archived": "⏸ отключён", "restricted": "⚠️ ограничен",
}


def acc_status_short(status) -> str:
    code = str(status or "active").strip().lower()
    return _ACC_SHORT.get(code, "❓")
