"""Owner-scoped decisions connecting the administrator to existing product modules."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from services import channel_admin as ca
from services import (
    content_memory,
    infra_memory,
    op_status,
    va_learning,
    va_references,
    va_strategy,
)


def _date(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


async def eligible_accounts(pool, owner_id: int, channel_id: int) -> list[int]:
    rows = await pool.fetch(
        "SELECT DISTINCT a.id FROM managed_channels mc JOIN tg_accounts a "
        "ON a.id=mc.acc_id AND a.owner_id=mc.owner_id "
        "WHERE mc.owner_id=$1 AND mc.channel_id=$2 AND a.is_active=TRUE "
        "AND COALESCE(a.acc_status,'active') NOT IN ('banned','session_expired','deleted','restricted','flood','warming') "
        "AND (a.cooldown_until IS NULL OR a.cooldown_until <= now())", int(owner_id), int(channel_id),
    )
    safe = []
    for row in rows:
        if not await infra_memory.is_account_quarantined(pool, int(row["id"])):
            safe.append(int(row["id"]))
    return safe


def knowledge(profile: dict, strategy: dict) -> list[dict]:
    effective = va_strategy.apply_strategy(profile, strategy)
    own, shared = ca._business_obj(profile), strategy.get("business", {})
    uses_network = bool(effective.get("network_strategy"))
    result = []
    for key, label in (("facts", "Факты"), ("faq", "Ответы клиентам"),
                       ("products", "Предложение"), ("voice_examples", "Голос автора"),
                       ("banned_topics", "Запретные темы"), ("competitors", "Конкуренты"),
                       ("sales_share", "Лимит продаж")):
        local = own.get(key)
        has_local = local is not None and (not isinstance(local, str) or bool(local.strip()))
        value = ca._business_obj(effective).get(key)
        source = "канал" if has_local else "общая стратегия" if uses_network and key in shared else "не задано"
        if key in ("banned_topics", "competitors") and has_local and uses_network and shared.get(key):
            source = "общая стратегия + канал"
        result.append({"key": key, "label": label, "source": source, "value": value})
    return result


def audit_plan(profile: dict, plan: list[dict], recent: list[str], now: datetime) -> list[str]:
    issues, slots, topics = [], set(), set()
    history = list(recent)
    cap = ca.sales_cap(profile)
    for item in plan:
        at = _date(item.get("slot_at"))
        if at is None or at < now:
            issues.append("В плане есть просроченный слот или неизвестное время")
        if at and at in slots:
            issues.append("Несколько публикаций запланированы на одно время")
        slots.add(at)
        topic = " ".join(str(item.get("topic") or "").casefold().split())
        if topic and topic in topics:
            issues.append("В плане повторяются темы")
        if topic:
            topics.add(topic)
        pillar = item.get("pillar") or ""
        history.append(pillar)
        if (cap is not None and ca.is_selling(pillar)
                and sum(ca.is_selling(p) for p in history[-10:]) > cap * 10):
            issues.append("Готовый план превышает действующий лимит продающих рубрик")
    return list(dict.fromkeys(issues))


def build(profile, strategy, refs, plan, recent, lessons, safe_accounts, operation,
          decisions, sample_counts, *, now=None):
    now = now or datetime.now(UTC)
    effective = va_strategy.apply_strategy(profile, strategy)
    fields = knowledge(profile, strategy)
    cards = []

    def card(key, title, detail, action, level="info"):
        cards.append({"key": key, "title": title, "detail": detail, "action": action, "level": level})

    required = {"facts", "faq", "voice_examples"}
    if ca._business_obj(effective).get("goal") in ("sales", "leads"):
        required.add("products")
    missing = [f["label"] for f in fields if f["key"] in required and not f["value"]]
    card("knowledge", "Полнота знаний", "Рекомендуется заполнить: " + ", ".join(missing)
         if missing else "Основные факты, ответы и голос автора заданы", "knowledge",
         "warning" if missing else "ok")
    network = effective.get("network_strategy")
    share = ca._business_obj(effective).get("sales_share")
    card("strategy", "Действующая стратегия",
         (f"Роль: {va_strategy.ROLES.get(effective.get('network_role'), '')}. "
          f"Целевой ресурс: {network['destination']}. Переходы: до {network['cta_share']} %. "
          if network else "Канал работает без общей стратегии. ") +
         (f"Продающие рубрики: до {share} %." if share is not None else "Лимит продающих рубрик не задан."),
         "strategy")
    stale = [r for r in refs if r.get("status") != "ready" or not _date(r.get("analyzed_at"))
             or now - _date(r["analyzed_at"]) > timedelta(days=7)]
    news = ca.is_news_channel(profile)
    fresh_news = va_references.has_fresh_news_signals(refs) if news else True
    card("sources", "Актуальность источников",
         "Нет свежих новостных сигналов: новости нельзя придумывать" if not fresh_news else
         f"Источников: {len(refs)}. Требуют проверки или обновления: {len(stale)}.",
         "knowledge", "warning" if stale or not fresh_news else "ok")
    issues = audit_plan(effective, plan, recent, now)
    card("plan", "Проверка будущих публикаций", "; ".join(issues) if issues else
         f"Проверено слотов: {len(plan)}. " + ("Конфликтов времени, тем и лимита продаж не найдено."
                                               if plan else "План ещё не сформирован."),
         "plan", "warning" if issues or not plan else "ok")
    card("accounts", "Аккаунты для публикации", f"Доступно аккаунтов вне карантина: {len(safe_accounts)}. "
         "Окончательные права и лимиты проверит исполнитель операции.",
         "health", "ok" if safe_accounts else "warning")
    if operation:
        card("operation", "Последняя операция публикации",
             f"№{operation['id']}: {op_status.label(operation['status'])}. "
             "Пост считается доставленным только после подтверждения исполнителя.", "operations",
             "warning" if op_status.normalize(operation["status"]) in ("failed", "cancelled", "partial") else "info")
    else:
        card("operation", "Последняя операция публикации", "Ожидающей подтверждения публикации нет.", "operations")
    card("editor", "Обратная связь редактору", "; ".join(lessons) if lessons else
         "Причины отклонения черновиков будут учитываться при следующих текстах.",
         "editorial", "warning" if lessons else "info")
    ready = sum(r["posts"] >= va_learning.MIN_POSTS for r in sample_counts)
    card("learning", "Новые данные для обучения",
         f"Рубрик с минимум тремя новыми суточными замерами: {ready}. Для сравнения нужны минимум две. "
         "Старые накопленные просмотры не заменяют суточный замер.", "decisions",
         "ok" if ready >= 2 else "info")
    return {"cards": cards, "knowledge": fields, "decisions": decisions,
            "samples": sample_counts, "plan_issues": issues}


async def load(pool, owner_id: int, channel_id: int) -> dict:
    profile = await ca.get_admin(pool, owner_id, channel_id)
    channel = await ca.channel_row(pool, owner_id, channel_id)
    if not profile or not channel:
        return {}
    profile = {**profile, "title": channel.get("title") or ""}
    strategy, refs, plan, recent, lessons, safe, decisions, counts = await asyncio.gather(
        va_strategy.get_strategy(pool, owner_id), va_references.list_refs(pool, owner_id, channel_id),
        ca.get_plan(pool, owner_id, channel_id),
        content_memory.recent_pillars(pool, owner_id, str(channel_id)),
        ca.owner_lessons(pool, owner_id, channel_id), eligible_accounts(pool, owner_id, channel_id),
        va_learning.history(pool, owner_id, channel_id),
        pool.fetch("SELECT pillar,count(*) AS posts FROM va_channel_posts "
                   "WHERE owner_id=$1 AND channel_key=$2 AND learning_sampled_at IS NOT NULL "
                   "AND learned_at IS NULL AND published_at > now() - interval '30 days' "
                   "GROUP BY pillar", int(owner_id), str(channel_id)),
    )
    operation = None
    if profile.get("last_op_id"):
        operation = await pool.fetchrow(
            "SELECT id,status FROM operation_queue WHERE owner_id=$1 AND id=$2",
            int(owner_id), int(profile["last_op_id"]),
        )
    return build(profile, strategy["settings"], refs, plan, recent, lessons, safe, operation,
                 decisions, [{"pillar": r["pillar"], "posts": int(r["posts"])} for r in counts])


async def network_actions(pool, owner_id: int) -> list[dict]:
    rows = await pool.fetch(
        "SELECT a.channel_id,a.last_error,a.enabled,a.topic,a.project_info,a.brief,"
        "(SELECT MAX(title) FROM managed_channels mc WHERE mc.owner_id=a.owner_id "
        "AND mc.channel_id=a.channel_id) AS title,"
        "EXISTS(SELECT 1 FROM va_admin_drafts d WHERE d.owner_id=a.owner_id "
        "AND d.channel_id=a.channel_id AND d.status='pending') AS review,"
        "NOT EXISTS(SELECT 1 FROM va_admin_plan p WHERE p.owner_id=a.owner_id "
        "AND p.channel_id=a.channel_id AND p.status='planned' AND p.slot_at>now()) AS empty_plan "
        "FROM va_channel_admin a WHERE a.owner_id=$1 "
        "ORDER BY (a.last_error IS NOT NULL) DESC, review DESC, empty_plan DESC,a.channel_id LIMIT 20",
        int(owner_id),
    )
    actions = []
    for row in rows:
        profile = dict(row)
        profile["title"] = row["title"] or ""
        empty_plan = bool(row["empty_plan"]) and not ca.is_news_channel(profile)
        reason = ("Ошибка работы: " + str(row["last_error"])[:150] if row["last_error"] else
                  "Черновики ждут решения" if row["review"] else
                  "Нет будущего плана" if row["enabled"] and empty_plan else "")
        if reason:
            actions.append({"channel_id": str(row["channel_id"]),
                            "title": row["title"] or str(row["channel_id"]), "reason": reason})
    return actions
