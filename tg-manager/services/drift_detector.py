"""
Drift Detector — периодически проверяет managed_channels на изменения
title / username / about и записывает дрейф в restriction_events.

Также сравнивает обнаруженные изменения с asset templates владельца
для определения: ожидаемый дрейф (по шаблону) или неожиданный.
"""

import asyncio
import json
import logging
import re

import asyncpg

from database import db
from services import account_manager

log = logging.getLogger(__name__)

_INTERVAL = 4 * 3600  # 4 часа между полными сканами
_BATCH_SIZE = 10  # каналов за одну сессию аккаунта
_PAUSE_BETWEEN = 3.0  # секунд между запросами к одному аккаунту


async def run(pool: asyncpg.Pool, bot) -> None:
    while True:
        try:
            await _check_all(pool, bot)
        except Exception:
            log.exception("drift_detector cycle error")
        await asyncio.sleep(_INTERVAL)


async def _compare_with_templates(
    pool: asyncpg.Pool, owner_id: int, changes: dict
) -> dict:
    """Сравнивает обнаруженные изменения с шаблонами активов владельца.

    Returns:
        dict с ключами:
        - matched_templates: list[dict] — шаблоны, которые совпадают с изменениями
        - verdict: "template_match" | "partial_match" | "unexpected" | "no_templates"
    """
    result = {
        "matched_templates": [],
        "verdict": "no_templates",
    }

    # Fetch all channel and group templates for this owner
    templates = await pool.fetch(
        """SELECT id, name, template
           FROM asset_templates
           WHERE owner_id = $1
             AND asset_type IN ('channel', 'group')""",
        owner_id,
    )
    if not templates:
        return result

    result["verdict"] = "unexpected"
    matched = []

    for tpl in templates:
        params = tpl["template"] or {}
        if not isinstance(params, dict):
            try:
                params = json.loads(params)
            except (json.JSONDecodeError, TypeError):
                params = {}

        tpl_title = (params.get("title") or "").strip()
        tpl_username = (params.get("username") or "").strip()
        tpl_desc = (params.get("description") or "").strip()

        score = 0
        total = 0
        details = []

        # Check title match (supports placeholders like {{CITY}}, {{COUNTRY}})
        if "title" in changes and tpl_title:
            total += 1
            new_title = changes["title"]["new"]
            # Compare ignoring placeholders - check if pattern matches
            pattern = re.escape(tpl_title)
            pattern = pattern.replace(r"\{\{CITY\}\}", r".+")
            pattern = pattern.replace(r"\{\{COUNTRY\}\}", r".+")
            pattern = pattern.replace(r"\{\{CITY_SLUG\}\}", r"[a-z0-9_-]+")
            pattern = pattern.replace(r"\{\{INDEX\}\}", r"\d+")
            pattern = pattern.replace(r"\{\{\w+\}\}", r"\S+")
            if re.match(f"^{pattern}$", new_title):
                score += 1
                details.append(f"title ✅ (по шаблону «{tpl['name']}»)")

        if "username" in changes and tpl_username:
            total += 1
            new_username = changes["username"]["new"]
            pattern = re.escape(tpl_username)
            pattern = pattern.replace(r"\{\{CITY_SLUG\}\}", r"[a-z0-9_-]+")
            pattern = pattern.replace(r"\{\{CITY\}\}", r"\w+")
            pattern = pattern.replace(r"\{\{INDEX\}\}", r"\d+")
            pattern = pattern.replace(r"\{\{\w+\}\}", r"\S+")
            if re.match(f"^{pattern}$", new_username):
                score += 1
                details.append(f"username ✅ (по шаблону «{tpl['name']}»)")

        # Check about/description match
        if "about" in changes and tpl_desc:
            total += 1
            new_about = changes["about"]["new"]
            pattern = re.escape(tpl_desc)
            pattern = pattern.replace(r"\{\{CITY\}\}", r".+")
            pattern = pattern.replace(r"\{\{COUNTRY\}\}", r".+")
            pattern = pattern.replace(r"\{\{CITY_SLUG\}\}", r"[a-z0-9_-]+")
            pattern = pattern.replace(r"\{\{\w+\}\}", r"\S+")
            if re.match(f"^{pattern}$", new_about):
                score += 1
                details.append(f"about ✅ (по шаблону «{tpl['name']}»)")

        if total > 0 and score > 0:
            match_ratio = score / total
            matched.append(
                {
                    "template_id": tpl["id"],
                    "template_name": tpl["name"],
                    "score": score,
                    "total": total,
                    "ratio": match_ratio,
                    "details": details,
                }
            )

    if matched:
        best = max(matched, key=lambda m: m["ratio"])
        result["matched_templates"] = matched
        if len(matched) == 1 and best["ratio"] == 1.0:
            result["verdict"] = "template_match"
        elif best["ratio"] >= 0.5:
            result["verdict"] = "partial_match"

    return result


async def _check_all(pool: asyncpg.Pool, bot) -> None:
    # Channels not checked in last 3 hours (give margin before 4h interval)
    rows = await pool.fetch(
        """SELECT mc.id, mc.owner_id, mc.acc_id, mc.channel_id,
                  mc.title, mc.username, mc.about, mc.access_hash
           FROM managed_channels mc
           WHERE mc.last_drift_check IS NULL
              OR mc.last_drift_check < now() - INTERVAL '3 hours'
           ORDER BY mc.owner_id, mc.acc_id
           LIMIT 200"""
    )
    if not rows:
        log.debug("drift_detector: nothing to check")
        return

    log.info("drift_detector: checking %d channels", len(rows))

    # Group by (owner_id, acc_id) to minimise sessions opened
    groups: dict[tuple, list] = {}
    for r in rows:
        key = (r["owner_id"], r["acc_id"])
        groups.setdefault(key, []).append(r)

    for (owner_id, acc_id), channels in groups.items():
        acc = await pool.fetchrow(
            "SELECT * FROM tg_accounts WHERE id=$1 AND is_active=true", acc_id
        )
        if not acc:
            continue
        acc_dict = dict(acc)

        for ch in channels[:_BATCH_SIZE]:
            try:
                info = await account_manager.get_full_channel_info(
                    acc_dict["session_str"], ch["channel_id"], _acc=acc_dict
                )
            except Exception as e:
                log.warning(
                    "drift_detector get_full_channel_info error for channel %s: %s",
                    ch["channel_id"],
                    e,
                )
                info = None

            # Always update last_drift_check
            await pool.execute(
                "UPDATE managed_channels SET last_drift_check=now() WHERE id=$1",
                ch["id"],
            )

            if not info:
                await asyncio.sleep(1.0)
                continue

            new_title = (info.get("title") or "").strip()
            new_username = (info.get("username") or "").strip()
            new_about = (info.get("about") or "").strip()

            old_title = (ch["title"] or "").strip()
            old_username = (ch["username"] or "").strip()
            old_about = (ch["about"] or "").strip()

            changes: dict = {}
            if old_title and new_title and new_title != old_title:
                changes["title"] = {"old": old_title, "new": new_title}
            if old_username and new_username and new_username != old_username:
                changes["username"] = {"old": old_username, "new": new_username}
            if old_about and new_about and new_about != old_about:
                changes["about"] = {
                    "old": old_about[:200],
                    "new": new_about[:200],
                }

            if changes:
                log.info(
                    "drift_detector: channel %d changed — %s",
                    ch["channel_id"],
                    list(changes),
                )

                # Compare with templates
                tpl_result = await _compare_with_templates(pool, owner_id, changes)
                verdict = tpl_result["verdict"]
                if verdict == "template_match":
                    severity = "info"
                elif verdict == "partial_match":
                    severity = "warning"
                else:
                    severity = "warning" if len(changes) > 1 else "info"

                await pool.execute(
                    "INSERT INTO restriction_events"
                    "(owner_id, event_type, severity, details) "
                    "VALUES ($1, 'drift_detected', $2, $3)",
                    owner_id,
                    severity,
                    json.dumps(
                        {
                            "channel_id": ch["channel_id"],
                            "channel_title": old_title or new_title,
                            "changes": changes,
                            "template_verdict": verdict,
                            "matched_templates": [
                                {"name": m["template_name"], "ratio": m["ratio"]}
                                for m in tpl_result["matched_templates"]
                            ],
                        }
                    ),
                )
                # Update stored values
                await pool.execute(
                    "UPDATE managed_channels "
                    "SET title=$2, username=$3, about=$4 WHERE id=$1",
                    ch["id"],
                    new_title or old_title,
                    new_username or old_username,
                    new_about,
                )
                # Notify owner
                ch_name = old_title or new_title or f"#{ch['channel_id']}"
                change_lines = []
                for field, diff in changes.items():
                    change_lines.append(
                        f"  <b>{field}</b>: «{diff['old']}» → «{diff['new']}»"
                    )

                # Template verdict header
                if verdict == "template_match":
                    header = "✅ <b>Дрейф канала (по шаблону)</b>"
                    tpl_note = (
                        "\n\n🟢 Изменения соответствуют шаблону «"
                        + tpl_result["matched_templates"][0]["name"]
                        + "» — <i>ожидаемо</i>"
                    )
                elif verdict == "partial_match":
                    header = "⚠️ <b>Дрейф канала (частичное совпадение)</b>"
                    tpl_names = ", ".join(
                        f"«{m['template_name']}»"
                        for m in tpl_result["matched_templates"]
                    )
                    tpl_note = f"\n\n🟡 Частично совпадает с шаблонами: {tpl_names}"
                elif verdict == "unexpected":
                    header = "🔴 <b>Неожиданный дрейф канала</b>"
                    tpl_note = (
                        "\n\n🔴 Не соответствует ни одному шаблону — <i>проверьте!</i>"
                    )
                else:
                    header = "⚠️ <b>Дрейф канала</b>"
                    tpl_note = ""

                msg = (
                    f"{header}\n\n"
                    f"<b>{ch_name}</b> изменился:\n"
                    + "\n".join(change_lines)
                    + tpl_note
                )
                await db.notify_if_enabled(pool, bot, owner_id, "restriction", msg)
            elif not old_about and new_about:
                # First-time about capture — just store it silently
                await pool.execute(
                    "UPDATE managed_channels SET about=$2 WHERE id=$1",
                    ch["id"],
                    new_about,
                )

            await asyncio.sleep(_PAUSE_BETWEEN)
