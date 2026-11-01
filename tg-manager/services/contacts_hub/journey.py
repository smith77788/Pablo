"""Следы человека вне контакт-хаба: рассылки, инбокс, заказы, подписки, воронка.

Карточка клиента отвечает на четыре вопроса — откуда пришёл, что получил,
что ответил, что купил, — а данные для них лежат в таблицах, которые
ключуются Telegram-id человека, а не id контакта. Здесь они выбираются;
свод и лента — чистые функции в contacts_hub/timeline.py.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def _rows(pool, q, *a):
    try:
        return list(await pool.fetch(q, *a) or [])
    except Exception:
        log.debug("journey: источник недоступен: %s", q[:60], exc_info=True)
        return []


async def _one(pool, q, *a):
    rows = await _rows(pool, q, *a)
    return rows[0] if rows else None


async def journey_parts(pool, uid: int, tg_id) -> dict:
    """Следы человека в рассылках, инбоксе, заказах и воронке.

    Каждая выборка — через _rows: карточка не должна падать оттого,
    что у владельца нет ботов-продавцов или миграция инбокса ещё не легла.
    """
    parts = {"received": [], "replies": [], "orders": [],
             "bot_sources": [], "funnel": None}
    if not tg_id:
        return parts
    tg_id = int(tg_id)
    rows = await _rows(pool,
        """SELECT c.name, l.status, l.sent_at FROM dm_campaign_log l
             JOIN dm_campaigns c ON c.id = l.campaign_id
            WHERE c.owner_id=$1 AND l.tg_user_id=$2
            ORDER BY l.sent_at DESC LIMIT 60""", uid, tg_id)
    parts["received"] = [{"campaign": r["name"], "status": r["status"] or "sent",
                          "at": r["sent_at"]} for r in (rows or [])]
    rows = await _rows(pool,
        """SELECT LEFT(b.message_text, 40) AS title, l.delivered_at FROM broadcast_delivery_log l
             JOIN broadcasts b ON b.id = l.broadcast_id
             JOIN managed_bots mb ON mb.bot_id = b.bot_id
            WHERE mb.added_by=$1 AND l.user_id=$2
            ORDER BY l.delivered_at DESC LIMIT 60""", uid, tg_id)
    parts["received"] += [{"campaign": ("Рассылка бота: «%s»" % r["title"]) if r["title"]
                           else "Рассылка бота",
                           "status": "sent", "at": r["delivered_at"]}
                          for r in (rows or [])]
    from services.unified_inbox import _decrypt
    rows = await _rows(pool,
        """SELECT account_id, text_enc, msg_date FROM inbox_messages
            WHERE owner_id=$1 AND peer_user_id=$2
            ORDER BY msg_date DESC LIMIT 60""", uid, tg_id)
    parts["replies"] = [{"channel": f"аккаунт #{r['account_id']}",
                         "text": _decrypt(r["text_enc"]), "at": r["msg_date"]}
                        for r in (rows or [])]
    rows = await _rows(pool,
        """SELECT text_enc, COALESCE(msg_date, created_at) AS at FROM vault_messages
            WHERE owner_id=$1 AND chat_id=$2 AND direction='in' AND NOT is_deleted
            ORDER BY COALESCE(msg_date, created_at) DESC LIMIT 60""", uid, tg_id)
    parts["replies"] += [{"channel": "Хранилище", "text": _decrypt(r["text_enc"]),
                          "at": r["at"]} for r in (rows or [])]
    rows = await _rows(pool,
        """SELECT total_cents, currency, status, created_at FROM bot_sales_orders
            WHERE owner_id=$1 AND customer_chat_id=$2
            ORDER BY created_at DESC LIMIT 60""", uid, tg_id)
    parts["orders"] = [{"total_cents": r["total_cents"], "currency": r["currency"],
                        "status": r["status"], "at": r["created_at"]}
                       for r in (rows or [])]
    rows = await _rows(pool,
        """SELECT mb.username, bu.first_seen FROM bot_users bu
             JOIN managed_bots mb ON mb.bot_id = bu.bot_id
            WHERE mb.added_by=$1 AND bu.user_id=$2
            ORDER BY bu.first_seen LIMIT 10""", uid, tg_id)
    parts["bot_sources"] = [{"label": f"бот @{r['username']}" if r["username"] else "бот",
                             "at": r["first_seen"]} for r in (rows or [])]
    st = await _one(pool,
        """SELECT vs.value FROM virtual_states vs
             JOIN unified_contacts uc ON uc.id::text = vs.entity_id
            WHERE vs.owner_id=$1 AND vs.entity_type='user' AND vs.state_key='funnel'
              AND uc.owner_id=$1 AND uc.telegram_user_id=$2 LIMIT 1""", uid, tg_id)
    if st:
        from services.virtual_layer import VALUE_LABEL
        parts["funnel"] = {"value": st["value"],
                           "label": VALUE_LABEL.get(st["value"], st["value"])}
    return parts
