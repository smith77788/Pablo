"""Мозг организма: из живого мира — цепочки следующих действий.

Не «ещё один островной советчик»: читает ЕДИНЫЙ мир-снимок (world) и события
(spine) и предлагает связки МЕЖДУ модулями — то, чего система не делала. Пример:
входящее «цена» (Vault→CRM пометил горячим) → мозг видит горячих в графе →
предлагает «написать оффер сегментом» (Сегмент→действие). Каждая подсказка несёт
исполняемое действие (kind + payload), которое поверхность разворачивает в один тап.

build_suggestions — чистая функция (тестируется без БД). pulse — сборка живьём.
"""
from __future__ import annotations

_SEV = {"urgent": 0, "warn": 1, "opportunity": 2, "info": 3}


def build_suggestions(snap: dict, dismissed=()) -> list[dict]:
    fleet = snap.get("fleet") or {}
    ops = snap.get("ops") or {}
    graph = snap.get("graph") or {}
    vault = snap.get("vault") or {}
    goal = snap.get("goal")
    out: list[dict] = []
    dismissed = set(dismissed or ())

    def add(sid, sev, title, why, action):
        if sid in dismissed:
            return
        out.append({"id": sid, "severity": sev, "title": title,
                    "why": why, "action": action})

    vh = vault.get("health")
    if vh in ("disabled", "never"):
        add("vault_off", "urgent", "Хранилище не пишет",
            "Входящие ЛС не сохраняются — сенсор намерений и welcome-аналитика "
            "не работают. Переподключите бизнес-бота.", {"kind": "vault"})
    elif vh == "stale":
        d = vault.get("stale_days")
        add("vault_stale", "warn", "Хранилище молчит",
            f"{d} дн. без новых сообщений — вероятно бизнес-бот отвалился.",
            {"kind": "vault"})

    if fleet.get("governor_level") == "red":
        add("gov_red", "warn", f"Флот под давлением ×{fleet.get('governor_mult')}",
            f"Давление {fleet.get('pressure')}/100 — рисковые операции лучше "
            "отложить, флот прогреть.", {"kind": "governor"})

    lf = ops.get("last_failed")
    if lf and ops.get("failed_24h", 0) > 0:
        add("op_fail", "warn", f"Операция #{lf['op_id']} не выполнена",
            (lf.get("reason") or lf.get("op_type") or "").strip() or "без причины",
            {"kind": "operation", "op_id": lf["op_id"]})

    # Ключевая межмодульная цепочка: горячие из графа → оффер сегментом.
    hot = graph.get("hot_leads", 0)
    if hot > 0:
        add("hot_offer", "opportunity", f"{hot} горячих ждут оффера",
            "Контакты в переговорах/с тегом интереса. Напишите им оффер сегментом "
            "— одной рассылкой с защитой темпа.",
            {"kind": "segment_hot"})

    it = graph.get("intents_24h", 0)
    if it > 0:
        add("intents", "info", f"{it} сигналов намерения за сутки",
            "Люди писали ключевые фразы («цена», «купить»). Посмотрите и дожмите.",
            {"kind": "intents"})

    if fleet.get("dead", 0) > 0:
        add("dead", "opportunity", f"{fleet['dead']} мёртвых аккаунтов",
            "Невоскрешаемые (бан/деактивация/сессия) — удалите, чтобы не искажали "
            "планирование ёмкости.", {"kind": "purge_dead"})

    if not goal:
        add("set_goal", "info", "Задайте цель кампании",
            "Планировщик разложит «+N участников к сроку» по реальной ёмкости флота.",
            {"kind": "campaign"})
    else:
        lbl = goal.get("label") or f"+{goal.get('goal', '?')} участников"
        add("goal", "info", f"Активная цель: {lbl}",
            "Продолжайте по плану — планировщик пересчитает достижимость.",
            {"kind": "campaign"})

    if (ops.get("running", 0) == 0 and ops.get("pending", 0) == 0
            and fleet.get("active", 0) > 0 and graph.get("contacts", 0) > 0
            and fleet.get("governor_level") != "red" and hot == 0):
        add("idle", "info", "Флот свободен",
            "Нет активных операций, давление в норме. Запустите инвайт или "
            "напишите сегменту.", {"kind": "invite"})

    out.sort(key=lambda x: _SEV.get(x["severity"], 9))
    return out[:6]


def narrative(snap: dict) -> str:
    f = snap.get("fleet") or {}
    g = snap.get("graph") or {}
    o = snap.get("ops") or {}
    parts = [f"Флот: {f.get('accounts', 0)} акк. ({f.get('active', 0)} активны"
             + (f", {f['dead']} мёртвых" if f.get("dead") else "") + ")",
             f"давление {f.get('pressure', 0)}/100"]
    day = []
    if o.get("running"):
        day.append(f"{o['running']} операц. в работе")
    if g.get("intents_24h"):
        day.append(f"{g['intents_24h']} намерений за сутки")
    if g.get("hot_leads"):
        day.append(f"{g['hot_leads']} горячих")
    tail = (" · " + ", ".join(day)) if day else ""
    return " · ".join(parts) + tail + "."


async def pulse(pool, owner_id: int) -> dict:
    """Живой пульс: мир + повествование + цепочки действий (без отклонённых)."""
    from services.organism import world, spine
    snap = await world.snapshot(pool, owner_id)
    try:
        dismissed = await spine.state_get(pool, owner_id, "dismissed", []) or []
    except Exception:
        dismissed = []
    return {
        "narrative": narrative(snap),
        "snapshot": snap,
        "suggestions": build_suggestions(snap, dismissed),
    }
