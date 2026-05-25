"""Tool implementations for AI assistant — each tool is isolated per user_id."""
from __future__ import annotations
import asyncpg


async def get_my_bots(pool: asyncpg.Pool, user_id: int) -> dict:
    bots = await pool.fetch(
        """
        SELECT b.bot_id, b.username, b.first_name,
               COUNT(DISTINCT a.user_id) AS audience,
               b.swarm_enabled, b.bot_role, b.cluster
        FROM managed_bots b
        LEFT JOIN bot_audience a ON a.bot_id = b.bot_id
        WHERE b.added_by=$1
        GROUP BY b.bot_id, b.username, b.first_name, b.swarm_enabled, b.bot_role, b.cluster
        ORDER BY audience DESC
        """,
        user_id,
    )
    return {
        "total": len(bots),
        "bots": [
            {
                "id": b["bot_id"],
                "name": f"@{b['username']}" if b["username"] else b["first_name"],
                "audience": b["audience"],
                "swarm": b["swarm_enabled"],
                "role": b["bot_role"],
                "cluster": b["cluster"] or "default",
            }
            for b in bots
        ],
    }


async def get_bot_details(pool: asyncpg.Pool, user_id: int, bot_id: int) -> dict:
    row = await pool.fetchrow(
        "SELECT * FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, user_id
    )
    if not row:
        return {"error": "Bot not found"}

    audience = await pool.fetchval(
        "SELECT COUNT(*) FROM bot_audience WHERE bot_id=$1", bot_id
    )
    today = await pool.fetchval(
        "SELECT COUNT(*) FROM bot_audience WHERE bot_id=$1 AND joined_at > now() - INTERVAL '24 hours'",
        bot_id,
    )
    broadcasts = await pool.fetchval(
        "SELECT COUNT(*) FROM broadcasts WHERE bot_id=$1", bot_id
    )
    tags = await pool.fetchval(
        "SELECT COUNT(DISTINCT tag) FROM user_tags WHERE bot_id=$1", bot_id
    )
    return {
        "id": bot_id,
        "name": f"@{row['username']}" if row["username"] else row["first_name"],
        "audience_total": audience,
        "new_today": today,
        "broadcasts_total": broadcasts,
        "crm_tags": tags,
        "swarm": row["swarm_enabled"],
        "cluster": row["cluster"] or "default",
    }


async def get_network_stats(pool: asyncpg.Pool, user_id: int) -> dict:
    total_bots = await pool.fetchval(
        "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1", user_id
    )
    total_audience = await pool.fetchval(
        "SELECT COUNT(DISTINCT a.user_id) FROM bot_audience a "
        "JOIN managed_bots b ON b.bot_id=a.bot_id WHERE b.added_by=$1",
        user_id,
    )
    total_sent = await pool.fetchval(
        "SELECT COALESCE(SUM(sent_count),0) FROM broadcasts b2 "
        "JOIN managed_bots m ON m.bot_id=b2.bot_id WHERE m.added_by=$1",
        user_id,
    )
    swarm_bots = await pool.fetchval(
        "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1 AND swarm_enabled=true", user_id
    )
    return {
        "total_bots": total_bots,
        "unique_audience": total_audience,
        "messages_sent": total_sent,
        "swarm_bots": swarm_bots,
    }


async def get_audience_activity(pool: asyncpg.Pool, user_id: int, bot_id: int) -> dict:
    row = await pool.fetchrow(
        "SELECT bot_id FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, user_id
    )
    if not row:
        return {"error": "Bot not found"}
    hot = await pool.fetchval(
        "SELECT COUNT(*) FROM user_activity WHERE bot_id=$1 "
        "AND last_seen > now() - INTERVAL '24 hours'",
        bot_id,
    )
    warm = await pool.fetchval(
        "SELECT COUNT(*) FROM user_activity WHERE bot_id=$1 "
        "AND last_seen BETWEEN now() - INTERVAL '7 days' AND now() - INTERVAL '24 hours'",
        bot_id,
    )
    cold = await pool.fetchval(
        "SELECT COUNT(*) FROM user_activity WHERE bot_id=$1 "
        "AND last_seen BETWEEN now() - INTERVAL '30 days' AND now() - INTERVAL '7 days'",
        bot_id,
    )
    lost = await pool.fetchval(
        "SELECT COUNT(*) FROM user_activity WHERE bot_id=$1 "
        "AND last_seen < now() - INTERVAL '30 days'",
        bot_id,
    )
    return {"hot": hot, "warm": warm, "cold": cold, "lost": lost}


async def get_growth_trend(pool: asyncpg.Pool, user_id: int, bot_id: int, days: int = 7) -> dict:
    row = await pool.fetchrow(
        "SELECT bot_id FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, user_id
    )
    if not row:
        return {"error": "Bot not found"}
    rows = await pool.fetch(
        """
        SELECT DATE(joined_at) AS day, COUNT(*) AS new_users
        FROM bot_audience
        WHERE bot_id=$1 AND joined_at > now() - ($2 || ' days')::INTERVAL
        GROUP BY day ORDER BY day
        """,
        bot_id, str(days),
    )
    return {
        "period_days": days,
        "daily": [{"date": str(r["day"]), "new_users": r["new_users"]} for r in rows],
        "total_new": sum(r["new_users"] for r in rows),
    }


async def get_seo_recommendations(pool: asyncpg.Pool, user_id: int, bot_id: int) -> dict:
    row = await pool.fetchrow(
        "SELECT * FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, user_id
    )
    if not row:
        return {"error": "Bot not found"}
    tips = []
    score = 0
    name = row.get("first_name") or ""
    if len(name) >= 5:
        score += 20
    else:
        tips.append("Имя слишком короткое — добавьте ключевые слова (мин. 5 символов)")
    if row.get("username"):
        score += 15
        if len(row["username"]) <= 20:
            score += 5
    else:
        tips.append("Нет username — бот не будет индексироваться в поиске Telegram")
    if row.get("description"):
        score += 30
        if len(row.get("description", "")) >= 100:
            score += 10
        tips.append("Описание есть — убедитесь что в нём есть ключевые слова вашей тематики") if score < 60 else None
    else:
        tips.append("Нет описания — добавьте текст с ключевыми словами (≥100 символов)")
    if row.get("short_description"):
        score += 20
    else:
        tips.append("Нет краткого описания (about) — оно показывается в превью поиска")
    return {"seo_score": min(score, 100), "tips": tips[:5]}


TOOL_DEFINITIONS = [
    {
        "name": "get_my_bots",
        "description": "Get list of all user's bots with basic stats (audience, swarm status, cluster)",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_bot_details",
        "description": "Get detailed statistics for a specific bot by its numeric ID",
        "input_schema": {
            "type": "object",
            "properties": {"bot_id": {"type": "integer", "description": "Bot's numeric Telegram ID"}},
            "required": ["bot_id"],
        },
    },
    {
        "name": "get_network_stats",
        "description": "Get aggregated statistics across all user's bots",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_audience_activity",
        "description": "Get hot/warm/cold/lost user segment counts for a specific bot",
        "input_schema": {
            "type": "object",
            "properties": {"bot_id": {"type": "integer"}},
            "required": ["bot_id"],
        },
    },
    {
        "name": "get_growth_trend",
        "description": "Get daily new users trend for a bot over N days",
        "input_schema": {
            "type": "object",
            "properties": {
                "bot_id": {"type": "integer"},
                "days": {"type": "integer", "description": "Number of days to look back (default 7)", "default": 7},
            },
            "required": ["bot_id"],
        },
    },
    {
        "name": "get_seo_recommendations",
        "description": "Get SEO score and optimization recommendations for a bot's profile",
        "input_schema": {
            "type": "object",
            "properties": {"bot_id": {"type": "integer"}},
            "required": ["bot_id"],
        },
    },
]


async def run_tool(name: str, inputs: dict, pool: asyncpg.Pool, user_id: int) -> str:
    import json
    try:
        if name == "get_my_bots":
            result = await get_my_bots(pool, user_id)
        elif name == "get_bot_details":
            result = await get_bot_details(pool, user_id, inputs["bot_id"])
        elif name == "get_network_stats":
            result = await get_network_stats(pool, user_id)
        elif name == "get_audience_activity":
            result = await get_audience_activity(pool, user_id, inputs["bot_id"])
        elif name == "get_growth_trend":
            result = await get_growth_trend(pool, user_id, inputs["bot_id"], inputs.get("days", 7))
        elif name == "get_seo_recommendations":
            result = await get_seo_recommendations(pool, user_id, inputs["bot_id"])
        else:
            result = {"error": f"Unknown tool: {name}"}
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})
