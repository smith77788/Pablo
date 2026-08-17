"""Ретеншен инвайта: сколько приглашённых осталось, сколько отвалилось.

«Пригласить» — половина задачи; вторая — удержать. Приток берём из
operation_log (успешные вступления по инвайт-операциям), отток — из событий
организма «left» (chat_guard эмитит уход из модерируемого чата). Здесь — чистый
расчёт сводки; выборки делает эндпоинт. Отток виден только для чатов под
модерацией бота — это честное ограничение, отражаем его в ответе.
"""
from __future__ import annotations


def summarize(joined: int, left: int) -> dict:
    """Сводка ретеншена из чисел притока/оттока.

    retention_pct — доля оставшихся от вступивших (0..100). Если притока нет —
    None (делить не на что). churn_pct — обратное.
    """
    joined = max(0, int(joined or 0))
    left = max(0, int(left or 0))
    if joined <= 0:
        return {"joined": joined, "left": left, "retained": None,
                "retention_pct": None, "churn_pct": None}
    retained = max(0, joined - left)
    retention = round(retained / joined * 100, 1)
    return {"joined": joined, "left": left, "retained": retained,
            "retention_pct": retention, "churn_pct": round(100 - retention, 1)}


def health(retention_pct) -> str:
    """Светофор ретеншена для UI/мозга. None → 'unknown'."""
    if retention_pct is None:
        return "unknown"
    if retention_pct >= 80:
        return "green"
    if retention_pct >= 50:
        return "amber"
    return "red"
