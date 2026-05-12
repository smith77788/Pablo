"""Analytics and reporting tools for BASIC.FOOD AI agents (Supabase-backed)."""
from __future__ import annotations
from datetime import datetime, timedelta

from database.models import get_client


def _since(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat()


def get_sales_summary(days: int = 30) -> dict:
    """Revenue, order count, and average order value for the last N days."""
    since = _since(days)
    res = get_client().rpc("get_investor_monthly_revenue", {}).execute()

    # Fallback: direct query
    orders = (
        get_client()
        .table("orders")
        .select("total, status, payment_method")
        .gte("created_at", since)
        .execute()
    ).data or []

    total_revenue = sum(o["total"] for o in orders if o["status"] != "cancelled")
    cancelled = sum(1 for o in orders if o["status"] == "cancelled")

    return {
        "period_days": days,
        "order_count": len(orders),
        "revenue_kopecks": total_revenue,
        "revenue_uah": round(total_revenue / 100, 2),
        "avg_order_uah": round(total_revenue / max(len(orders) - cancelled, 1) / 100, 2),
        "cancelled_count": cancelled,
    }


def get_orders_by_status_count(days: int = 30) -> list[dict]:
    """Order count grouped by status."""
    since = _since(days)
    orders = (
        get_client()
        .table("orders")
        .select("status, total")
        .gte("created_at", since)
        .execute()
    ).data or []

    from collections import defaultdict
    counts: dict[str, dict] = defaultdict(lambda: {"count": 0, "total_kopecks": 0})
    for o in orders:
        counts[o["status"]]["count"] += 1
        counts[o["status"]]["total_kopecks"] += o["total"]

    return [
        {"status": s, **v, "total_uah": round(v["total_kopecks"] / 100, 2)}
        for s, v in sorted(counts.items(), key=lambda x: -x[1]["count"])
    ]


def get_top_products(days: int = 30, limit: int = 10) -> list[dict]:
    """Best-selling products by revenue in the last N days."""
    since = _since(days)
    items = (
        get_client()
        .table("order_items")
        .select("product_id, product_name, product_price, quantity, orders(status, created_at)")
        .gte("orders.created_at", since)
        .execute()
    ).data or []

    from collections import defaultdict
    stats: dict = defaultdict(lambda: {"name": "", "units": 0, "revenue_kopecks": 0})
    for item in items:
        order = item.get("orders") or {}
        if order.get("status") == "cancelled":
            continue
        pid = item["product_id"]
        stats[pid]["name"] = item.get("product_name", pid)
        stats[pid]["units"] += item.get("quantity", 0)
        stats[pid]["revenue_kopecks"] += item.get("product_price", 0) * item.get("quantity", 0)

    sorted_products = sorted(stats.items(), key=lambda x: -x[1]["revenue_kopecks"])[:limit]
    return [
        {"product_id": pid, **v, "revenue_uah": round(v["revenue_kopecks"] / 100, 2)}
        for pid, v in sorted_products
    ]


def get_top_customers(days: int = 30, limit: int = 10) -> list[dict]:
    """Customers ranked by spend in the last N days."""
    since = _since(days)
    orders = (
        get_client()
        .table("orders")
        .select("user_id, total, customers(name, email, phone)")
        .gte("created_at", since)
        .neq("status", "cancelled")
        .execute()
    ).data or []

    from collections import defaultdict
    stats: dict = defaultdict(lambda: {"customer": {}, "orders": 0, "revenue_kopecks": 0})
    for o in orders:
        uid = o.get("user_id") or "guest"
        stats[uid]["customer"] = o.get("customers") or {}
        stats[uid]["orders"] += 1
        stats[uid]["revenue_kopecks"] += o["total"]

    sorted_customers = sorted(stats.items(), key=lambda x: -x[1]["revenue_kopecks"])[:limit]
    return [
        {
            "user_id": uid,
            "name": v["customer"].get("name", "Гость"),
            "email": v["customer"].get("email"),
            "order_count": v["orders"],
            "revenue_uah": round(v["revenue_kopecks"] / 100, 2),
        }
        for uid, v in sorted_customers
    ]


def get_inventory_snapshot() -> dict:
    """Current inventory: total units, estimated retail value, low-stock count."""
    products = (
        get_client()
        .table("products")
        .select("name, price, stock_quantity, is_active")
        .eq("is_active", True)
        .execute()
    ).data or []

    total_units = sum(p["stock_quantity"] for p in products)
    retail_value = sum(p["price"] * p["stock_quantity"] for p in products)
    low_stock = [p for p in products if p["stock_quantity"] <= 10]

    return {
        "product_count": len(products),
        "total_units": total_units,
        "retail_value_uah": round(retail_value / 100, 2),
        "low_stock_count": len(low_stock),
        "out_of_stock_count": sum(1 for p in products if p["stock_quantity"] == 0),
        "low_stock_products": [
            {"name": p["name"], "stock": p["stock_quantity"]} for p in low_stock
        ],
    }


def get_customer_lifecycle_breakdown() -> list[dict]:
    """Count customers per lifecycle stage."""
    res = (
        get_client()
        .table("customers")
        .select("lifecycle_stage")
        .execute()
    )
    from collections import Counter
    counts = Counter(c["lifecycle_stage"] for c in (res.data or []))
    return [{"stage": stage, "count": cnt} for stage, cnt in counts.most_common()]


def get_daily_revenue(days: int = 14) -> list[dict]:
    """Revenue per day for the last N days."""
    since = _since(days)
    orders = (
        get_client()
        .table("orders")
        .select("created_at, total, status")
        .gte("created_at", since)
        .execute()
    ).data or []

    from collections import defaultdict
    daily: dict = defaultdict(lambda: {"orders": 0, "revenue_kopecks": 0})
    for o in orders:
        if o["status"] == "cancelled":
            continue
        day = o["created_at"][:10]
        daily[day]["orders"] += 1
        daily[day]["revenue_kopecks"] += o["total"]

    return [
        {"date": d, **v, "revenue_uah": round(v["revenue_kopecks"] / 100, 2)}
        for d, v in sorted(daily.items())
    ]
