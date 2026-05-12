"""CRM and business data tools for BASIC.FOOD AI agents (Supabase-backed)."""
from __future__ import annotations
from datetime import datetime, timedelta
from typing import Any

from database.models import get_client


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

def get_customer_by_email(email: str) -> dict | None:
    res = get_client().table("customers").select("*").eq("email", email).single().execute()
    return res.data


def get_customer_by_id(customer_id: str) -> dict | None:
    res = get_client().table("customers").select("*").eq("id", customer_id).single().execute()
    return res.data


def get_customer_by_telegram(chat_id: int) -> dict | None:
    res = (
        get_client()
        .table("customers")
        .select("*")
        .eq("telegram_chat_id", chat_id)
        .single()
        .execute()
    )
    return res.data


def search_customers(query: str, limit: int = 10) -> list[dict]:
    res = (
        get_client()
        .table("customers")
        .select("*")
        .or_(f"name.ilike.%{query}%,email.ilike.%{query}%,phone.ilike.%{query}%")
        .order("total_spent", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


def update_customer(customer_id: str, **fields) -> bool:
    allowed = {"name", "phone", "address", "tags", "notes", "telegram_chat_id", "lifecycle_stage"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    updates["updated_at"] = datetime.now().isoformat()
    get_client().table("customers").update(updates).eq("id", customer_id).execute()
    return True


def add_customer_note(customer_id: str, note: str, author: str = "pablo-ai") -> dict:
    res = get_client().table("customer_notes").insert({
        "customer_id": customer_id,
        "note": note,
        "author": author,
    }).execute()
    return res.data[0] if res.data else {}


def get_customer_notes(customer_id: str) -> list[dict]:
    res = (
        get_client()
        .table("customer_notes")
        .select("*")
        .eq("customer_id", customer_id)
        .order("created_at", desc=True)
        .execute()
    )
    return res.data or []


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def get_order(order_number: str) -> dict | None:
    res = (
        get_client()
        .table("orders")
        .select("*, order_items(*), customers(name, email, phone, telegram_chat_id)")
        .eq("order_number", order_number)
        .single()
        .execute()
    )
    return res.data


def get_orders_by_status(status: str, limit: int = 20) -> list[dict]:
    res = (
        get_client()
        .table("orders")
        .select("*, customers(name, email, phone)")
        .eq("status", status)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


def get_customer_orders(customer_id: str, limit: int = 20) -> list[dict]:
    res = (
        get_client()
        .table("orders")
        .select("*, order_items(*)")
        .eq("user_id", customer_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


def update_order_status(order_id: str, status: str, notes: str = "") -> bool:
    valid = {"new", "confirmed", "processing", "shipped", "delivered", "cancelled", "refunded"}
    if status not in valid:
        return False
    update: dict[str, Any] = {"status": status, "updated_at": datetime.now().isoformat()}
    if notes:
        update["notes"] = notes
    get_client().table("orders").update(update).eq("id", order_id).execute()
    return True


def set_tracking_number(order_id: str, tracking: str) -> bool:
    get_client().table("orders").update({
        "tracking_number": tracking,
        "status": "shipped",
        "updated_at": datetime.now().isoformat(),
    }).eq("id", order_id).execute()
    return True


def get_recent_orders(days: int = 7, limit: int = 50) -> list[dict]:
    since = (datetime.now() - timedelta(days=days)).isoformat()
    res = (
        get_client()
        .table("orders")
        .select("*, customers(name, email, phone)")
        .gte("created_at", since)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

def get_product(product_id: str) -> dict | None:
    res = get_client().table("products").select("*").eq("id", product_id).single().execute()
    return res.data


def search_products(query: str, limit: int = 10) -> list[dict]:
    res = (
        get_client()
        .table("products")
        .select("*")
        .or_(f"name.ilike.%{query}%,description.ilike.%{query}%")
        .eq("is_active", True)
        .limit(limit)
        .execute()
    )
    return res.data or []


def get_low_stock_products(threshold: int = 10) -> list[dict]:
    res = (
        get_client()
        .table("products")
        .select("*")
        .lte("stock_quantity", threshold)
        .eq("is_active", True)
        .order("stock_quantity")
        .execute()
    )
    return res.data or []


def update_stock(product_id: str, new_quantity: int, reason: str = "") -> bool:
    get_client().table("products").update({
        "stock_quantity": new_quantity,
        "updated_at": datetime.now().isoformat(),
    }).eq("id", product_id).execute()

    get_client().table("stock_adjustments").insert({
        "product_id": product_id,
        "new_quantity": new_quantity,
        "reason": reason,
        "adjusted_by": "pablo-ai",
    }).execute()
    return True


def get_all_products(active_only: bool = True) -> list[dict]:
    q = get_client().table("products").select("id, name, price, stock_quantity, categories, weight, is_active, sold_count")
    if active_only:
        q = q.eq("is_active", True)
    res = q.order("sort_order").execute()
    return res.data or []


# ---------------------------------------------------------------------------
# Messages / Support
# ---------------------------------------------------------------------------

def save_message(
    channel: str,
    content: str,
    direction: str = "inbound",
    subject: str = "",
    customer_id: str | None = None,
    chat_id: int | None = None,
) -> str:
    payload: dict[str, Any] = {
        "channel": channel,
        "direction": direction,
        "content": content,
        "subject": subject,
    }
    if customer_id:
        payload["customer_id"] = customer_id
    if chat_id:
        payload["chat_id"] = chat_id

    res = get_client().table("pablo_messages").insert(payload).execute()
    return res.data[0]["id"] if res.data else ""


def get_unresolved_messages(channel: str | None = None, limit: int = 20) -> list[dict]:
    q = (
        get_client()
        .table("pablo_messages")
        .select("*")
        .eq("is_resolved", False)
        .eq("direction", "inbound")
        .order("created_at", desc=True)
        .limit(limit)
    )
    if channel:
        q = q.eq("channel", channel)
    res = q.execute()
    return res.data or []


def mark_message_resolved(message_id: str, agent_response: str = "") -> bool:
    get_client().table("pablo_messages").update({
        "is_resolved": True,
        "agent_response": agent_response,
    }).eq("id", message_id).execute()
    return True


# ---------------------------------------------------------------------------
# Telegram chat sessions
# ---------------------------------------------------------------------------

def get_telegram_chat_meta(chat_id: int) -> dict | None:
    res = (
        get_client()
        .table("telegram_customer_chats")
        .select("*")
        .eq("chat_id", chat_id)
        .single()
        .execute()
    )
    return res.data


def upsert_telegram_chat(chat_id: int, first_name: str = "", username: str = "") -> None:
    get_client().table("telegram_customer_chats").upsert({
        "chat_id": chat_id,
        "first_name": first_name,
        "username": username,
        "last_message_at": datetime.now().isoformat(),
    }, on_conflict="chat_id").execute()
