"""Доска сделок: воронка контактов и простые сделки по стадиям, колонка на стадию.

Стадии сделок живут в ``contact_crm.stage`` (список один на проект —
``crm_engine.STAGE_LABELS``). До канбана воронку можно было только
отфильтровать чипом в хабе контактов: шесть чипов, каждый открывает свой
срез, и увидеть разом «где сейчас мои сделки и на какую сумму» было нельзя.
У лидеров (amoCRM, Bitrix24, Pipedrive) это первый экран продавца.

Ручка одна и отдаёт доску целиком: колонки во ВСЕХ стадиях, включая пустые
(пустая середина воронки читается как «там никого и не было»), по странице
карточек в каждой, плюс честный счёт и суммы по валютам — тем же
``crm_engine.pipeline``, что считает чипы хаба, чтобы числа на доске и в хабе
не расходились. «Показать ещё» в колонке — та же ручка с ``stage`` и
``offset``: тогда в ответе одна колонка.

Смену стадии канбан делает существующей ручкой
``POST /api/miniapp/uch/contacts/{id}/crm``: там проверка стадии, сигнал
виртуальному слою и журнал. Второй путь записи стадии означал бы второй набор
правил.
"""
from __future__ import annotations

import logging

from aiohttp import web

log = logging.getLogger(__name__)

# Карточек на колонку за раз. Доска — обзор, а не выгрузка: тридцать карточек
# на стадию закрывают экран телефона с запасом, дальше — «Показать ещё».
BOARD_PAGE_DEFAULT = 30
BOARD_PAGE_MAX = 100

# Порядок карточек в колонке один на доску и на «Показать ещё»: сначала то,
# что требует действия (напоминание, у которого подошёл срок), потом свежие
# касания. Добор по cc.id обязателен: без него карточки с одинаковым временем
# прыгали бы между страницами. Порядок написан в самих запросах, а не
# подставлен строкой: так его видит проверка листания
# (tests/test_paging_does_not_skip_or_repeat_people.py).

_CARD_COLUMNS = (
    "cc.id AS crm_id, cc.contact_id, COALESCE(cc.stage, 'lead') AS stage, "
    "cc.deal_value, cc.currency, cc.next_reminder_at, cc.next_reminder_text, "
    "cc.last_interaction_at, cc.last_message_preview, cc.updated_at, "
    "uc.display_name, uc.first_name, uc.last_name, uc.username, uc.company"
)


def card_name(row) -> str:
    """Имя на карточке: как человек записан, иначе имя и фамилия, иначе ник."""
    disp = (row.get("display_name") or "").strip()
    if disp:
        return disp
    full = " ".join(p for p in ((row.get("first_name") or "").strip(),
                                (row.get("last_name") or "").strip()) if p)
    if full:
        return full
    uname = (row.get("username") or "").strip()
    return "@" + uname if uname else "Без имени"


def _iso(v):
    return v.isoformat() if v is not None else None


def card_payload(row) -> dict:
    r = dict(row)
    return {
        "contact_id": str(r["contact_id"]),
        "stage": r["stage"],
        "name": card_name(r),
        "username": r.get("username") or "",
        "company": r.get("company") or "",
        "deal_value": float(r["deal_value"] or 0),
        "currency": (r.get("currency") or "USD").upper(),
        "next_reminder_at": _iso(r.get("next_reminder_at")),
        "next_reminder_text": (r.get("next_reminder_text") or "")[:120],
        "last_interaction_at": _iso(r.get("last_interaction_at")),
        "last_message_preview": (r.get("last_message_preview") or "")[:120],
        "updated_at": _iso(r.get("updated_at")),
    }


# ── Простые сделки (crm_deals) на той же доске ─────────────────────────────
#
# Сделок было две системы, и обе были видны владельцу: «CRM — Сделки» (список
# crm_deals, сумма с «$», нарисованным вручную) и «Канбан сделок» (воронка
# контактов, contact_crm). Числа на этапах расходились, валюта тоже, и куда
# смотреть, было непонятно. Простые сделки создают не только экран — их пишут
# сценарии, автоответчик и коннекторы amoCRM/Bitrix24, — поэтому таблица
# остаётся, а показывается ОДНОЙ доской: в той же колонке этапа, с той же
# суммой по валютам. Стадии у обеих таблиц одни (schema_v272).

_DEAL_COLUMNS = "d.id, d.title, d.contact, d.stage, d.value, d.currency, d.notes, d.updated_at"


def deal_payload(row, main_currency: str) -> dict:
    """Карточка простой сделки. Валюта NULL — основная валюта владельца."""
    r = dict(row)
    return {
        "deal_id": int(r["id"]),
        "stage": r["stage"] or "lead",
        "title": (r.get("title") or "").strip() or "Сделка без названия",
        "contact": (r.get("contact") or "")[:120],
        "value": float(r.get("value") or 0),
        "currency": (r.get("currency") or main_currency or "RUB").upper(),
        "notes": (r.get("notes") or "")[:200],
        "updated_at": _iso(r.get("updated_at")),
    }


async def owner_main_currency(pool, owner_id: int) -> str:
    """Основная валюта владельца; сбой чтения — умолчание, а не отказ доски."""
    from services import owner_currency
    try:
        st = await owner_currency.get_settings(pool, owner_id)
        return (st.get("currency") or owner_currency.DEFAULT_MAIN).upper()
    except Exception:
        log.warning("доска сделок: основная валюта не прочитана uid=%s", owner_id, exc_info=True)
        return owner_currency.DEFAULT_MAIN


async def load_deals(pool, owner_id: int, per_stage: int, main_currency: str) -> dict:
    """Простые сделки по этапам: счёт, суммы по валютам и первая страница."""
    sums = await pool.fetch(
        """SELECT d.stage, UPPER(COALESCE(d.currency, $2)) AS cur, COUNT(*) AS cnt,
                  COALESCE(SUM(d.value), 0) AS total
           FROM crm_deals d WHERE d.owner_id = $1
           GROUP BY d.stage, UPPER(COALESCE(d.currency, $2))""",
        owner_id, main_currency,
    )
    rows = await pool.fetch(
        f"""SELECT * FROM (
                SELECT {_DEAL_COLUMNS},
                       ROW_NUMBER() OVER (PARTITION BY d.stage
                                          ORDER BY d.updated_at DESC, d.id DESC) AS rn
                FROM crm_deals d WHERE d.owner_id = $1
            ) t WHERE rn <= $2""",
        owner_id, per_stage,
    )
    out: dict[str, dict] = {}
    for r in sums:
        item = out.setdefault(r["stage"] or "lead", {"count": 0, "by_currency": {}, "cards": []})
        item["count"] += int(r["cnt"] or 0)
        val = float(r["total"] or 0)
        if val:
            item["by_currency"][r["cur"]] = item["by_currency"].get(r["cur"], 0.0) + val
    for r in sorted(rows, key=lambda x: int(x["rn"])):
        item = out.setdefault(r["stage"] or "lead", {"count": 0, "by_currency": {}, "cards": []})
        item["cards"].append(deal_payload(r, main_currency))
    return out


async def load_board(pool, owner_id: int, per_stage: int, main_currency: str = "RUB") -> dict:
    """Доска целиком: все стадии и первая страница карточек в каждой.

    В колонке два вида карточек: контакт в воронке и простая сделка. Счёт и
    листание у них свои (``count``/``has_more`` и ``deal_count``/
    ``deals_has_more``), а сумма колонки — общая, по валютам.
    """
    from services.contacts_hub.crm_engine import pipeline

    summary = await pipeline(pool, owner_id)
    deals = await load_deals(pool, owner_id, per_stage, main_currency)
    rows = await pool.fetch(
        f"""SELECT * FROM (
                SELECT {_CARD_COLUMNS},
                       ROW_NUMBER() OVER (PARTITION BY COALESCE(cc.stage, 'lead')
                                          ORDER BY (cc.next_reminder_at IS NOT NULL AND cc.next_reminder_at <= NOW()) DESC,
                     COALESCE(cc.last_interaction_at, cc.updated_at) DESC NULLS LAST, cc.id DESC) AS rn
                FROM contact_crm cc
                JOIN unified_contacts uc
                  ON uc.id = cc.contact_id AND uc.owner_id = cc.owner_id
                WHERE cc.owner_id = $1
            ) t WHERE rn <= $2""",
        owner_id, per_stage,
    )
    by_stage: dict[str, list] = {}
    for r in rows:
        by_stage.setdefault(r["stage"], []).append(card_payload(r))
    stages = list(summary.get("stages", []))
    known = {s["stage"] for s in stages}
    # Простая сделка на этапе, которого нет в списке (старая строка), не
    # прячется: иначе в итоге она есть, а ни в одной колонке её нет.
    for st in deals:
        if st not in known:
            stages.append({"stage": st, "label": st, "count": 0, "by_currency": {}})
    columns = []
    for s in stages:
        cards = by_stage.get(s["stage"], [])
        d = deals.get(s["stage"]) or {"count": 0, "by_currency": {}, "cards": []}
        money = dict(s.get("by_currency") or {})
        for cur, val in d["by_currency"].items():
            money[cur] = money.get(cur, 0.0) + val
        columns.append({
            "stage": s["stage"],
            "label": s["label"],
            "count": int(s["count"]),
            "by_currency": money,
            "cards": cards,
            "has_more": int(s["count"]) > len(cards),
            "deals": d["cards"],
            "deal_count": int(d["count"]),
            "deals_has_more": int(d["count"]) > len(d["cards"]),
        })
    return {
        "columns": columns,
        "total": int(summary.get("total") or 0),
        "deals_total": sum(int(d["count"]) for d in deals.values()),
        "reminders_7d": int(summary.get("reminders_7d") or 0),
        "per_stage": per_stage,
        "currency": main_currency,
    }


async def load_column(pool, owner_id: int, stage: str, limit: int, offset: int) -> dict:
    """Следующая страница одной колонки."""
    rows = await pool.fetch(
        f"""SELECT {_CARD_COLUMNS}
            FROM contact_crm cc
            JOIN unified_contacts uc
              ON uc.id = cc.contact_id AND uc.owner_id = cc.owner_id
            WHERE cc.owner_id = $1 AND COALESCE(cc.stage, 'lead') = $2
            ORDER BY (cc.next_reminder_at IS NOT NULL AND cc.next_reminder_at <= NOW()) DESC,
                     COALESCE(cc.last_interaction_at, cc.updated_at) DESC NULLS LAST, cc.id DESC
            LIMIT $3 OFFSET $4""",
        owner_id, stage, limit, offset,
    )
    total = await pool.fetchval(
        "SELECT COUNT(*) FROM contact_crm cc "
        "JOIN unified_contacts uc ON uc.id = cc.contact_id AND uc.owner_id = cc.owner_id "
        "WHERE cc.owner_id = $1 AND COALESCE(cc.stage, 'lead') = $2",
        owner_id, stage,
    )
    cards = [card_payload(r) for r in rows]
    return {
        "stage": stage,
        "cards": cards,
        "total": int(total or 0),
        "has_more": offset + len(cards) < int(total or 0),
    }


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать ручку канбана сделок на приложении мини-аппа."""
    from services.contacts_hub.crm_engine import STAGE_LABELS
    from services.mini_app_api import (
        _INTERNAL_ERROR,
        _err,
        _get_uid,
        _json_resp,
        _list_limit,
        _list_offset,
    )

    async def crm_board(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        limit = _list_limit(request, BOARD_PAGE_DEFAULT, BOARD_PAGE_MAX)
        stage = (request.query.get("stage") or "").strip()
        try:
            if stage:
                # Чужая стадия — ошибка запроса, а не пустая колонка: пустая
                # выглядела бы правдой («там никого»).
                if stage not in STAGE_LABELS:
                    return _err("Неизвестная стадия сделки", 400)
                col = await load_column(pool, uid, stage, limit, _list_offset(request))
                # Итог и признак продолжения называются здесь, в ответе ручки:
                # колонка с потолком, которая молчит о нём, выглядела бы всей.
                return _json_resp({"stage": col["stage"], "cards": col["cards"],
                                   "total": col["total"], "has_more": col["has_more"]})
            b = await load_board(pool, uid, limit, await owner_main_currency(pool, uid))
            # У каждой колонки свой count и has_more (load_board), а у доски —
            # общий итог: контакты в воронке и простые сделки отдельно.
            return _json_resp({"columns": b["columns"], "total": b["total"],
                               "deals_total": b["deals_total"],
                               "reminders_7d": b["reminders_7d"],
                               "per_stage": b["per_stage"],
                               "currency": b["currency"]})
        except Exception:
            log.exception("crm_board uid=%s stage=%s", uid, stage or "-")
            return _err(_INTERNAL_ERROR, 500)

    async def crm_funnel_analytics(request: web.Request) -> web.Response:
        """Аналитика воронки: конверсия, время в стадии, застрявшие, прогноз
        (services/crm_funnel_analytics). Окно — 30/90/180/365 дней."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        raw = (request.query.get("days") or "").strip()
        try:
            from services import crm_funnel_analytics as fa
            days = int(raw) if raw.isdigit() else fa.DEFAULT_WINDOW
            if days not in fa.WINDOWS:
                return _err("Окно отчёта — 30, 90, 180 или 365 дней", 400)
            return _json_resp(await fa.report(pool, uid, days))
        except Exception:
            log.exception("crm_funnel_analytics uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/crm/board", crm_board)
    app.router.add_get("/api/miniapp/crm/funnel_analytics", crm_funnel_analytics)
