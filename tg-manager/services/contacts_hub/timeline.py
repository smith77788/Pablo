"""Таймлайн касаний контакта (карточка-360): все следы в одной ленте.

Контакт трогают разные подсистемы: правки полей (contact_history), источники
(каким аккаунтом найден), CRM-стадия, и события организма (intent из
intent_sensor несёт contact_id в payload). Здесь — чистое слияние этих
разнородных следов в одну хронологическую ленту. Без БД — тестируется отдельно.
"""
from __future__ import annotations

_EVENT_ICON = {
    "intent": "💡",
    "joined": "➕",
    "left": "➖",
    "op_done": "⚙️",
    "ban": "⛔",
}


def _hist_title(field: str) -> str:
    names = {
        "first_name": "Имя", "last_name": "Фамилия", "username": "Username",
        "display_name": "Отображаемое имя", "phones": "Телефоны",
        "is_premium": "Premium", "tags": "Теги", "notes": "Заметки",
    }
    return names.get(field or "", field or "поле")


def build_timeline(history=None, events=None, sources=None, crm=None,
                   limit: int = 60, received=None, replies=None,
                   orders=None) -> list[dict]:
    """Собрать единую ленту касаний контакта.

    Аргументы — уже выбранные из БД строки (dict-подобные):
      history: [{field, old_value, new_value, created_at}]
      events:  [{kind, payload(dict), created_at}]  (organism_events по контакту)
      sources: [{source_type|account_id, discovered_at}]
      crm:     {stage, updated_at}  или None
      received: [{campaign, status, at}]  что ему отправляли (рассылки)
      replies:  [{channel, text, at}]     что он написал в ответ
      orders:   [{total_cents, currency, status, items, at}]  что купил
    Возвращает список {ts, icon, kind, title, detail}, отсортированный по времени
    (новые сверху), обрезанный до limit. Записи без времени отбрасываются.
    """
    items: list[dict] = []

    for h in history or []:
        old, new = h.get("old_value"), h.get("new_value")
        detail = f"{old or '∅'} → {new or '∅'}" if (old or new) else ""
        items.append({"ts": h.get("created_at"), "icon": "✏️", "kind": "history",
                      "title": f"Изменено: {_hist_title(h.get('field'))}",
                      "detail": detail[:160]})

    for e in events or []:
        payload = e.get("payload") or {}
        kind = e.get("kind") or "event"
        icon = _EVENT_ICON.get(kind, "•")
        if kind == "intent":
            tags = payload.get("tags") or []
            stage = payload.get("stage")
            bits = []
            if payload.get("text"):
                bits.append(f"«{str(payload['text'])[:80]}»")
            if stage:
                bits.append(f"стадия → {stage}")
            if tags:
                bits.append("теги: " + ", ".join(map(str, tags)))
            title = "Сигнал намерения"
            detail = " · ".join(bits)
        else:
            title = kind
            detail = ""
        items.append({"ts": e.get("created_at"), "icon": icon, "kind": kind,
                      "title": title, "detail": detail[:160]})

    for s in sources or []:
        label = s.get("source_type") or (f"аккаунт #{s['account_id']}"
                                         if s.get("account_id") else "источник")
        items.append({"ts": s.get("discovered_at"), "icon": "🔗", "kind": "source",
                      "title": "Обнаружен", "detail": str(label)[:160]})

    if crm and crm.get("stage"):
        items.append({"ts": crm.get("updated_at"), "icon": "📊", "kind": "crm",
                      "title": "CRM-стадия", "detail": str(crm["stage"])})

    for r in received or []:
        items.append({"ts": r.get("at"), "icon": "📨", "kind": "received",
                      "title": "Получил рассылку",
                      "detail": str(r.get("campaign") or "")[:160]})

    for r in replies or []:
        txt = str(r.get("text") or "")
        items.append({"ts": r.get("at"), "icon": "💬", "kind": "reply",
                      "title": f"Написал · {r.get('channel') or 'Telegram'}",
                      "detail": (f"«{txt[:140]}»" if txt else "")})

    for o in orders or []:
        items.append({"ts": o.get("at"), "icon": "🛒", "kind": "order",
                      "title": f"Заказ · {ORDER_STATUS_LABEL.get(o.get('status'), o.get('status') or '')}",
                      "detail": _money(o.get("total_cents"), o.get("currency"))})

    items = [i for i in items if i.get("ts") is not None]
    items.sort(key=lambda i: i["ts"], reverse=True)
    return items[:limit]


# Заказ бота-продавца: какие статусы — это деньги. «new» и «handoff» — ещё
# разговор, «cancelled» — не покупка.
PAID_ORDER_STATUSES = ("confirmed", "done")
ORDER_STATUS_LABEL = {
    "new": "новый", "confirmed": "подтверждён", "handoff": "передан человеку",
    "cancelled": "отменён", "done": "выполнен",
}


def _money(cents, currency) -> str:
    try:
        v = int(cents or 0) / 100
    except (TypeError, ValueError):
        v = 0
    s = f"{v:,.2f}".replace(",", " ").replace(".00", "")
    return f"{s} {currency or ''}".strip()


def build_journey(sources=None, received=None, replies=None, orders=None,
                  funnel=None) -> dict:
    """Путь клиента одной сводкой — четыре вопроса карточки.

    «Откуда пришёл, что получил, что ответил, что купил». Каждая часть уже
    выбрана из своей таблицы; здесь только свод, без БД.

    sources:  [{label, at}]  — где найден (аккаунт, бот, импорт)
    received: [{campaign, status, at}]
    replies:  [{channel, text, at}]
    orders:   [{total_cents, currency, status, at}]
    funnel:   {value, label} | None — состояние на лестнице воронки
    """
    def _first(rows):
        rows = [r for r in rows or [] if r.get("at") is not None]
        return min(rows, key=lambda r: r["at"]) if rows else None

    def _last(rows):
        rows = [r for r in rows or [] if r.get("at") is not None]
        return max(rows, key=lambda r: r["at"]) if rows else None

    src = _first(sources)
    sent = [r for r in received or [] if (r.get("status") or "sent") == "sent"]
    paid = [o for o in orders or [] if o.get("status") in PAID_ORDER_STATUSES]
    revenue: dict[str, int] = {}
    for o in paid:
        cur = o.get("currency") or "USD"
        revenue[cur] = revenue.get(cur, 0) + int(o.get("total_cents") or 0)
    first_campaign = _first(sent)
    last_reply = _last(replies)
    return {
        "source": ({"label": src.get("label"), "at": src.get("at")} if src else None),
        "received": {"count": len(sent),
                     "campaigns": sorted({str(r.get("campaign")) for r in sent
                                          if r.get("campaign")}),
                     "first_campaign": (first_campaign or {}).get("campaign")},
        "replied": {"count": len(replies or []),
                    "last_at": (last_reply or {}).get("at"),
                    "last_text": str((last_reply or {}).get("text") or "")[:140]},
        "bought": {"orders": len(paid),
                   "revenue": [{"currency": c, "total_cents": v,
                                "text": _money(v, c)} for c, v in sorted(revenue.items())]},
        "funnel": funnel,
    }
