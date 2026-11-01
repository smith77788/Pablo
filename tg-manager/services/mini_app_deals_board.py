"""Канбан сделок: воронка контактов по стадиям, колонка на стадию.

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

# Порядок карточек в колонке: сначала то, что требует действия (напоминание,
# у которого подошёл срок), потом свежие касания. Без добора по id карточки
# с одинаковым updated_at прыгали бы между страницами.
_CARD_ORDER = (
    "(cc.next_reminder_at IS NOT NULL AND cc.next_reminder_at <= NOW()) DESC, "
    "COALESCE(cc.last_interaction_at, cc.updated_at) DESC NULLS LAST, cc.id DESC"
)

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


async def load_board(pool, owner_id: int, per_stage: int) -> dict:
    """Доска целиком: все стадии и первая страница карточек в каждой."""
    from services.contacts_hub.crm_engine import pipeline

    summary = await pipeline(pool, owner_id)
    rows = await pool.fetch(
        f"""SELECT * FROM (
                SELECT {_CARD_COLUMNS},
                       ROW_NUMBER() OVER (PARTITION BY COALESCE(cc.stage, 'lead')
                                          ORDER BY {_CARD_ORDER}) AS rn
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
    columns = []
    for s in summary.get("stages", []):
        cards = by_stage.get(s["stage"], [])
        columns.append({
            "stage": s["stage"],
            "label": s["label"],
            "count": int(s["count"]),
            "by_currency": s.get("by_currency") or {},
            "cards": cards,
            "has_more": int(s["count"]) > len(cards),
        })
    return {
        "columns": columns,
        "total": int(summary.get("total") or 0),
        "reminders_7d": int(summary.get("reminders_7d") or 0),
        "per_stage": per_stage,
    }


async def load_column(pool, owner_id: int, stage: str, limit: int, offset: int) -> dict:
    """Следующая страница одной колонки."""
    rows = await pool.fetch(
        f"""SELECT {_CARD_COLUMNS}
            FROM contact_crm cc
            JOIN unified_contacts uc
              ON uc.id = cc.contact_id AND uc.owner_id = cc.owner_id
            WHERE cc.owner_id = $1 AND COALESCE(cc.stage, 'lead') = $2
            ORDER BY {_CARD_ORDER}
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
                return _json_resp(await load_column(
                    pool, uid, stage, limit, _list_offset(request)))
            return _json_resp(await load_board(pool, uid, limit))
        except Exception:
            log.exception("crm_board uid=%s stage=%s", uid, stage or "-")
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/crm/board", crm_board)
