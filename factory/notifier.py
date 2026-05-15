"""Simple Telegram notifier for Factory cycle results.

Provides:
  - send_telegram(text)          — low-level send to all admins via Bot API
  - notify_cycle_complete(results) — rich post-cycle summary
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or ""
_ADMIN_IDS_RAW = os.getenv("ADMIN_TELEGRAM_IDS", "")


def _get_admin_ids() -> list[str]:
    return [x.strip() for x in _ADMIN_IDS_RAW.split(",") if x.strip()]


def send_telegram(text: str, parse_mode: str = "HTML") -> bool:
    """Send *text* to all configured admin chat IDs via Telegram Bot API.

    Returns True if at least one message was delivered successfully.
    """
    token = TOKEN or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or ""
    if not token:
        return False

    admin_ids = _get_admin_ids()
    if not admin_ids:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    sent = 0
    for chat_id in admin_ids:
        try:
            payload = json.dumps(
                {
                    "chat_id": chat_id,
                    "text": text[:4096],
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                }
            ).encode()
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=10)
            sent += 1
        except Exception as e:
            print(f"[notifier] Failed to send to {chat_id}: {e}")
    return sent > 0


def notify_cycle_complete(results: dict) -> None:
    """Send a rich cycle-completion summary to all Telegram admins.

    *results* is the dict returned by factory.cycle.run_cycle().
    """
    health = results.get("health_score", "?")
    elapsed = results.get("elapsed_s") or results.get("duration_seconds", 0)
    focus = results.get("ceo_department_focus", "")
    phases = results.get("phases", {})
    decisions = results.get("decisions", [])

    icon = (
        "💚"
        if isinstance(health, (int, float)) and health >= 70
        else "🟡"
        if isinstance(health, (int, float)) and health >= 50
        else "🔴"
    )

    active_actions = len(
        [d for d in decisions if d.get("type") in ("grow", "create_mvp", "scale")]
    )

    # ── Business metrics ──────────────────────────────────────────────────────
    nm = results.get("nevesty_models", {})
    metric_parts: list[str] = []
    if nm.get("orders_7d") is not None:
        growth = nm.get("orders_growth_pct", 0)
        arrow = "↑" if growth >= 0 else "↓"
        metric_parts.append(f"📋 Заявок 7д: {nm['orders_7d']} ({arrow}{abs(growth):.0f}%)")
    if nm.get("revenue_30d"):
        metric_parts.append(f"💰 Выручка 30д: {nm['revenue_30d']:,} ₽")
    if nm.get("repeat_client_rate_pct") is not None:
        metric_parts.append(f"🔁 Повторные: {nm['repeat_client_rate_pct']}%")
    if nm.get("reviews_avg_rating"):
        metric_parts.append(f"⭐ Рейтинг: {nm['reviews_avg_rating']}/5")

    # ── Phase highlights ──────────────────────────────────────────────────────
    highlights: list[str] = []

    # Channel content
    cc = phases.get("channel_content", {})
    posts_n = cc.get("posts_generated") or len(cc.get("posts", []))
    if posts_n:
        highlights.append(f"📱 Постов для канала: {posts_n}")

    # Growth actions from CEO synthesis
    ceo = phases.get("ceo_synthesis", {})
    growth_actions = ceo.get("growth_actions", [])
    if growth_actions:
        top = (
            growth_actions[0].get("action", "")
            if isinstance(growth_actions[0], dict)
            else str(growth_actions[0])
        )
        highlights.append(f"💡 {len(growth_actions)} growth actions | топ: {top[:80]}")

    # Experiments
    exp = phases.get("experiment_tracking", {})
    if exp.get("active_checked"):
        highlights.append(f"🧪 Экспериментов: {exp['active_checked']}")

    # Content
    content = phases.get("content_dept", {})
    if content.get("models_updated"):
        highlights.append(f"✍️ Описаний моделей: {content['models_updated']}")

    # Ideas
    ideas = phases.get("ideas", {})
    if ideas.get("new"):
        highlights.append(f"🔬 Новых идей: {ideas['new']}")

    # AB
    ab = phases.get("ab_experiments", {})
    if ab.get("generated"):
        highlights.append(f"🔀 A/B гипотез: {ab['generated']}")

    # Social media
    sm = phases.get("social_media", {})
    if isinstance(sm, dict):
        sm_target = sm.get("analytics", {}).get("weekly_post_target")
        if sm_target:
            highlights.append(f"📸 Соц.сети: цель {sm_target} постов/нед")

    # CEO weekly
    if results.get("weekly_ceo_summary"):
        highlights.append(f"📰 CEO отчёт: {results['weekly_ceo_summary'][:100]}…")

    # ── Build message ─────────────────────────────────────────────────────────
    lines = [f"{icon} <b>AI Factory — цикл завершён</b>"]
    lines.append(f"📊 Health: {health}/100 | ⏱ {elapsed}с")
    if focus:
        lines.append(f"🎯 Фокус: {focus}")
    lines.append(f"📋 Решений: {len(decisions)} | Активных: {active_actions}")

    if metric_parts:
        lines.append("")
        lines.append("<b>Бизнес-метрики:</b>")
        lines.extend(metric_parts)

    if highlights:
        lines.append("")
        lines.append("<b>Результаты фаз:</b>")
        lines.extend([f"• {h}" for h in highlights[:8]])

    send_telegram("\n".join(lines))
