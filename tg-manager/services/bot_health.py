"""Здоровье управляемых ботов: почему бот молчит и что с этим делать.

Разрыв, который это закрывает. Опрос ботов (`auto_responder`) уже умеет
отличать отозванный токен от сетевой ошибки и даже ставит такому боту
экспоненциальный отступ — но знает об этом ТОЛЬКО серверный лог. Снаружи бот
остаётся в списке как ни в чём не бывало: «активен», подписчики ему пишут,
он не отвечает, и никто не говорит владельцу ни слова. Это хуже, чем молчащий
аккаунт: молчит не внутренний инструмент, а лицо, которым владелец повёрнут к
своей аудитории.

Хуже того, восстановиться было почти невозможно. Заменить токен в продукте
нельзя: `db.add_bot` при существующем bot_id возвращает «уже добавлен» и токен
НЕ обновляет. Оставался единственный путь — удалить бота и добавить заново, а
`bot_users` висит на `managed_bots` с `ON DELETE CASCADE`, то есть вместе с
ботом стиралась вся его аудитория. Продукт предлагал вылечить молчание ценой
базы подписчиков.

Здесь — только решения: классификация ошибки, человеческое объяснение и
правило «когда сказать». Без сети и без базы.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Отозванный токен — не «моргание»: он не чинится сам. Но одна ошибка может
# быть и случайной (перезапуск, сеть), поэтому тревожим после короткой серии.
FAIL_STREAK_TO_ALERT = 3

UNAUTHORIZED = "unauthorized"
CONFLICT = "conflict"
FLOOD = "flood"
NETWORK = "network"
OTHER = "other"

_LABEL = {
    UNAUTHORIZED: "🔴 Токен отозван",
    CONFLICT: "🟠 Конфликт получения обновлений",
    FLOOD: "🟡 Telegram просит подождать",
    NETWORK: "🟡 Нет связи с Telegram",
    OTHER: "🟠 Ошибка",
}
_HINT = {
    UNAUTHORIZED: "Telegram больше не принимает токен этого бота — обычно его"
                  " отозвали или перевыпустили в @BotFather. Бот не отвечает"
                  " подписчикам. Получите новый токен у @BotFather и замените"
                  " его здесь: подписчики, воронки и правила сохранятся.",
    CONFLICT: "Обновления забирает кто-то ещё — вебхук или другая копия бота."
              " Пока это так, часть сообщений проходит мимо.",
    FLOOD: "Telegram временно ограничил частоту запросов. Это пройдёт само;"
           " делать ничего не нужно.",
    NETWORK: "Не удалось связаться с Telegram. Обычно это временно —"
             " следующая попытка всё исправит.",
    OTHER: "Telegram вернул ошибку при получении обновлений.",
}
# Само не пройдёт — нужен человек.
_NEEDS_HUMAN = (UNAUTHORIZED, CONFLICT)


def classify_error(description: str = "", error_code=None) -> str:
    """Что за ошибка пришла от Telegram. Чистая функция.

    Классифицируем по смыслу, а не по тексту целиком: описания Telegram
    меняются, а нам важно отличить «токен мёртв, зовите человека» от
    «подождите, само пройдёт».
    """
    d = (description or "").lower()
    try:
        code = int(error_code) if error_code is not None else None
    except (TypeError, ValueError):
        code = None
    if code == 401 or "unauthorized" in d:
        return UNAUTHORIZED
    if code == 409 or "conflict" in d or "terminated by other" in d or "webhook" in d:
        return CONFLICT
    if code == 429 or "too many requests" in d or "flood" in d:
        return FLOOD
    if not d and code is None:
        return OTHER
    if ("timeout" in d or "connection" in d or "network" in d
            or "temporarily unavailable" in d or code in (502, 503, 504)):
        return NETWORK
    return OTHER


def decide_alert(kind: str, fail_streak: int, already_notified: bool) -> bool:
    """Пора ли сказать владельцу.

    Говорим только о том, что не пройдёт само и требует его рук, и только один
    раз на поломку — иначе уведомления превращаются в шум и их перестают
    читать (тот же приём, что у сторожа прокси и у прогрева).
    """
    if already_notified:
        return False
    if kind not in _NEEDS_HUMAN:
        return False
    return int(fail_streak or 0) >= FAIL_STREAK_TO_ALERT


def describe(row: dict, now: datetime | None = None) -> dict:
    """Состояние бота для интерфейса.

    Возвращает {state, label, hint, attention, kind}. `attention` — бот сам не
    поправится, нужен человек; именно по нему считается счётчик на экране.
    """
    now = now or datetime.now(timezone.utc)
    if row.get("is_active") is False:
        return {"state": "off", "kind": "", "label": "⏸ Выключен",
                "hint": "Бот выключен вами — обновления не забираются.",
                "attention": False}
    streak = int(row.get("fail_streak") or 0)
    if streak <= 0:
        return {"state": "ok", "kind": "", "label": "🟢 Работает", "hint": "",
                "attention": False}
    kind = (row.get("last_error") or OTHER).lower()
    if kind not in _LABEL:
        kind = OTHER
    needs_human = kind in _NEEDS_HUMAN and streak >= FAIL_STREAK_TO_ALERT
    return {
        "state": "broken" if needs_human else "degraded",
        "kind": kind,
        "label": _LABEL[kind],
        "hint": _HINT[kind],
        "attention": needs_human,
    }


def build_alert(name: str, kind: str, fail_streak: int) -> str:
    """Уведомление владельцу. Называем цену молчания: боту пишут люди."""
    head = ("🔴 <b>Бот не отвечает: токен отозван</b>"
            if kind == UNAUTHORIZED else
            "🟠 <b>Бот получает обновления не полностью</b>")
    return (
        f"{head}\n\n"
        f"<b>{name}</b> — {int(fail_streak or 0)} неудачных попыток подряд.\n"
        f"Пока это так, подписчики пишут боту и не получают ответа.\n\n"
        f"{_HINT.get(kind, _HINT[OTHER])}"
    )
