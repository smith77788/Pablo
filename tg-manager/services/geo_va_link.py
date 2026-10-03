"""Attach a city-aware Virtual Administrator to Global Presence channels."""

import json
import logging


MAX_GEO_TARGETS = 500
_WARNING = "Администратор не подключён: "
log = logging.getLogger(__name__)


def config_from_plan(selection):
    if isinstance(selection, str):
        try:
            selection = json.loads(selection)
        except (TypeError, ValueError):
            return None
    if not isinstance(selection, dict):
        return None
    config = selection.get("virtual_admin")
    return config if isinstance(config, dict) and config.get("enabled") is True else None


def validate_config(value, asset_type):
    """Return a compact, safe plan configuration or a user-facing error."""
    if value is None or value is False:
        return None, None
    if not isinstance(value, dict):
        return None, "Настройки администратора должны быть объектом"
    if value.get("enabled") is not True:
        return None, None
    if asset_type not in ("channel", "package", "full_package"):
        return None, "Виртуального администратора можно подключить только к каналам"
    topic = value.get("topic")
    if not isinstance(topic, str) or not topic.strip() or len(topic.strip()) > 300:
        return None, "Укажите тематику администратора (до 300 символов)"
    mode = value.get("publish_mode", "review")
    if mode not in ("review", "auto"):
        return None, "Режим публикаций: проверка или авто"
    posts = value.get("posts_per_day", 2)
    if type(posts) is not int or not 1 <= posts <= 12:
        return None, "Количество публикаций в день: от 1 до 12"
    contact = value.get("lead_contact", "")
    if not isinstance(contact, str) or len(contact.strip()) > 200 or any(
        ord(char) < 32 for char in contact
    ):
        return None, "Целевой ресурс: одна строка, до 200 символов"
    return {"enabled": True, "topic": topic.strip(),
            "publish_mode": mode, "posts_per_day": posts,
            "lead_contact": contact.strip()}, None


def settings_for_target(config, target):
    city = str(target.get("city") or "").strip()
    region = str(target.get("region") or "").strip()
    country = str(target.get("country") or "").strip()
    place = ", ".join(x for x in (city, region, country) if x)
    topic = config["topic"]
    if place:
        topic = f"{topic} — {place}"
    notes = ("Пиши только о проверенных событиях и фактах, относящихся к "
             f"{place or 'тематике канала'}. Не выдумывай новости, адреса, даты "
             "и источники. Если достоверной информации нет, не публикуй пост.")
    return {"topic": topic[:500], "notes": notes[:1000],
            "publish_mode": config["publish_mode"],
            "posts_per_day": config["posts_per_day"],
            "lead_contact": config.get("lead_contact", "")}


async def install_for_target(pool, owner_id, channel_id, target, config):
    """The channel is already created; admin failures must never recreate it."""
    from services import channel_admin

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await channel_admin.install(
                    conn, owner_id, channel_id, settings_for_target(config, target))
    except Exception:
        # Keep target 'done': retrying Telegram channel creation would duplicate it.
        log.exception("Geo VA installation failed for owner=%s channel=%s", owner_id, channel_id)
        warning = _WARNING + "ошибка настройки; попробуйте повторить"
        await pool.execute(
            "UPDATE global_presence_targets SET error_message=$1 WHERE id=$2 AND plan_id=$3",
            warning, target["id"], target["plan_id"],
        )
        return False
    await pool.execute(
        "UPDATE global_presence_targets SET error_message=NULL "
        "WHERE id=$1 AND plan_id=$2 AND error_message LIKE $3",
        target["id"], target["plan_id"], _WARNING + "%",
    )
    return True


async def retry_missing(pool, owner_id, plan_id, config, limit=100):
    """Reconcile only channels with no VA row, never overwriting user settings."""
    rows = await pool.fetch(
        "SELECT t.id, t.plan_id, t.city, t.region, t.country, t.result_asset_id "
        "FROM global_presence_targets t "
        "JOIN managed_channels m ON m.owner_id=$1 AND m.channel_id=t.result_asset_id "
        "  AND m.type='channel' "
        "LEFT JOIN va_channel_admin va ON va.owner_id=$1 AND va.channel_id=t.result_asset_id "
        "WHERE t.plan_id=$2 AND t.status='done' AND t.result_asset_id IS NOT NULL "
        "  AND va.channel_id IS NULL ORDER BY t.id LIMIT $3",
        owner_id, plan_id, limit,
    )
    installed = 0
    for row in rows:
        if await install_for_target(pool, owner_id, row["result_asset_id"], row, config):
            installed += 1
    return {"checked": len(rows), "installed": installed, "failed": len(rows) - installed,
            "more": len(rows) == limit}
