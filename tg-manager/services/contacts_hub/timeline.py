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
                   limit: int = 60) -> list[dict]:
    """Собрать единую ленту касаний контакта.

    Аргументы — уже выбранные из БД строки (dict-подобные):
      history: [{field, old_value, new_value, created_at}]
      events:  [{kind, payload(dict), created_at}]  (organism_events по контакту)
      sources: [{source_type|account_id, discovered_at}]
      crm:     {stage, updated_at}  или None
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

    items = [i for i in items if i.get("ts") is not None]
    items.sort(key=lambda i: i["ts"], reverse=True)
    return items[:limit]
