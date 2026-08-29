"""Аналитика диалогов Хранилища: чистая свёртка над сохранёнными сообщениями.

Хранилище уже архивирует бизнес-переписку (vault_messages: direction, msg_date,
chat_id). Раньше это был только архив — без разреза «как идёт общение». Здесь —
детерминированная аналитика без БД/сети: объёмы, охват собеседников, скорость
ответа владельца, часы активности. Выборку делает эндпоинт, сюда передаёт строки.
"""
from __future__ import annotations

from statistics import median


def _ts(v) -> float | None:
    """Приводит datetime | epoch-число | None к секундам (float) или None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return v.timestamp()  # datetime
    except Exception:
        return None


def analyze_dialogs(rows) -> dict:
    """rows: список {chat_id, direction('in'|'out'), msg_date}. Возвращает сводку.

    reply-time: для каждого диалога берём первое входящее, на которое владелец
    ответил (следующее 'out' позже по времени), и считаем задержку. Медиана по
    диалогам устойчивее среднего к выбросам (один ночной ответ не портит картину).
    """
    total = incoming = outgoing = 0
    by_chat: dict = {}
    hour_hist = [0] * 24
    for r in rows or []:
        d = (r.get("direction") or "in").lower()
        ts = _ts(r.get("msg_date"))
        cid = r.get("chat_id")
        total += 1
        if d == "out":
            outgoing += 1
        else:
            incoming += 1
        if ts is not None:
            # локальный час не знаем — используем UTC-час; для «когда пишут» хватает
            import datetime as _dt
            hour_hist[_dt.datetime.utcfromtimestamp(ts).hour] += 1
        by_chat.setdefault(cid, []).append((ts, d))

    dialogs = len(by_chat)
    replied = 0
    response_secs: list[float] = []
    for cid, msgs in by_chat.items():
        seq = sorted((t, d) for t, d in msgs if t is not None)
        # первое входящее, за которым позже есть исходящее
        first_in = next((t for t, d in seq if d == "in"), None)
        if first_in is None:
            continue
        first_out_after = next((t for t, d in seq if d == "out" and t >= first_in), None)
        if first_out_after is not None:
            replied += 1
            response_secs.append(first_out_after - first_in)

    dialogs_with_in = sum(
        1 for msgs in by_chat.values() if any(d == "in" for _, d in msgs))
    unanswered = max(0, dialogs_with_in - replied)
    reply_rate = round(replied / dialogs_with_in * 100, 1) if dialogs_with_in else None
    avg_resp_min = round(median(response_secs) / 60, 1) if response_secs else None
    busiest_hour = max(range(24), key=lambda h: hour_hist[h]) if total else None

    return {
        "total": total,
        "incoming": incoming,
        "outgoing": outgoing,
        "dialogs": dialogs,
        "replied_dialogs": replied,
        "unanswered_dialogs": unanswered,
        "reply_rate_pct": reply_rate,
        "median_response_min": avg_resp_min,
        "busiest_hour": busiest_hour,
    }
