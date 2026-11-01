"""Канбан сделок: доска по стадиям contact_crm и её экран.

Проверяется то, на чём канбан мог бы соврать:
* колонки идут во ВСЕХ стадиях воронки и в её порядке, включая пустые;
* счёт колонки — серверный (тот же pipeline, что у чипов хаба), а не число
  показанных карточек, и «Показать ещё» есть ровно тогда, когда карточек больше;
* чужая стадия в запросе колонки — 400, а не пустая колонка;
* экран подключён, у него есть дверь, а стадию он меняет той же ручкой, что
  карточка клиента.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
import uuid

from services import mini_app_deals_board as board
from services.contacts_hub.crm_engine import STAGE_LABELS

from tests.miniapp_source import api_routes, api_source, miniapp_html, miniapp_source

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _row(stage, name="", username="", value=0.0, currency="USD", **kw):
    now = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
    r = {
        "crm_id": 1, "contact_id": uuid.uuid4(), "stage": stage,
        "deal_value": value, "currency": currency,
        "next_reminder_at": None, "next_reminder_text": None,
        "last_interaction_at": None, "last_message_preview": None,
        "updated_at": now, "display_name": name, "first_name": None,
        "last_name": None, "username": username, "company": None,
    }
    r.update(kw)
    return r


class _Pool:
    """Пул, отвечающий на запросы pipeline() и доски."""

    def __init__(self, crm_rows, card_rows):
        self.crm_rows = crm_rows      # для pipeline: stage, currency, cnt, total_value
        self.card_rows = card_rows

    async def fetch(self, sql, *args):
        if "GROUP BY stage, currency" in sql:
            return self.crm_rows
        if "ROW_NUMBER()" in sql:
            per = args[1]
            seen: dict = {}
            out = []
            for r in self.card_rows:
                seen[r["stage"]] = seen.get(r["stage"], 0) + 1
                if seen[r["stage"]] <= per:
                    out.append(r)
            return out
        stage, limit, offset = args[1], args[2], args[3]
        rows = [r for r in self.card_rows if r["stage"] == stage]
        return rows[offset:offset + limit]

    async def fetchval(self, sql, *args):
        if "next_reminder_at" in sql:
            return 2
        return len([r for r in self.card_rows if r["stage"] == args[1]])


def test_board_has_every_stage_in_funnel_order_with_server_counts():
    cards = [_row("lead", name=f"L{i}") for i in range(3)] + [_row("won", name="W", value=500)]
    pool = _Pool(
        crm_rows=[
            {"stage": "lead", "currency": "USD", "cnt": 3, "total_value": 0},
            {"stage": "won", "currency": "USD", "cnt": 1, "total_value": 500},
        ],
        card_rows=cards,
    )
    d = asyncio.run(board.load_board(pool, 7, per_stage=2))
    assert [c["stage"] for c in d["columns"]][:6] == list(STAGE_LABELS)
    by = {c["stage"]: c for c in d["columns"]}
    assert by["lead"]["count"] == 3 and len(by["lead"]["cards"]) == 2
    assert by["lead"]["has_more"] is True
    assert by["won"]["has_more"] is False
    assert by["won"]["by_currency"] == {"USD": 500.0}
    assert by["proposal"]["count"] == 0 and by["proposal"]["cards"] == []
    assert d["total"] == 4 and d["reminders_7d"] == 2


def test_column_page_reports_has_more_by_total():
    cards = [_row("contact", name=f"C{i}") for i in range(5)]
    pool = _Pool(crm_rows=[], card_rows=cards)
    d = asyncio.run(board.load_column(pool, 7, "contact", limit=2, offset=2))
    assert len(d["cards"]) == 2 and d["total"] == 5 and d["has_more"] is True
    d = asyncio.run(board.load_column(pool, 7, "contact", limit=2, offset=4))
    assert len(d["cards"]) == 1 and d["has_more"] is False


def test_card_name_falls_back_to_full_name_then_username():
    assert board.card_name({"display_name": " Аня "}) == "Аня"
    assert board.card_name({"first_name": "Иван", "last_name": "Петров"}) == "Иван Петров"
    assert board.card_name({"username": "ivan"}) == "@ivan"
    assert board.card_name({}) == "Без имени"


def test_card_payload_is_json_ready():
    p = board.card_payload(_row("lead", name="A", value=12.5, currency="rub"))
    assert isinstance(p["contact_id"], str)
    assert p["currency"] == "RUB" and p["deal_value"] == 12.5
    assert p["updated_at"].startswith("2026-11-01")


def test_route_registered_and_rejects_unknown_stage():
    import services.mini_app_api as api
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    orig = api._get_uid
    api._get_uid = lambda request: 7   # до setup_routes: ручка берёт его при регистрации
    try:
        app = web.Application()
        board.setup_routes(app, pool=None)
        handler = None
        for r in app.router.routes():
            if r.resource.canonical == "/api/miniapp/crm/board" and r.method == "GET":
                handler = r.handler
        assert handler is not None
        req = make_mocked_request("GET", "/api/miniapp/crm/board?stage=closed_won")
        resp = asyncio.run(handler(req))
    finally:
        api._get_uid = orig
    assert resp.status == 400


def test_api_module_wires_the_board():
    # Не через путь к монолиту (см. test_tests_do_not_pin_the_monolith):
    # регистрацию модуля в слое видно по api_source, сам путь — по api_routes.
    assert "/api/miniapp/crm/board" in api_routes()
    assert "mini_app_deals_board.setup_routes(app, pool)" in api_source()


def test_screen_is_loaded_has_a_door_and_moves_via_contact_crm():
    html = miniapp_html()
    assert '<script src="screens/deals_board.js"></script>' in html
    assert 'onclick="openDeals()"' in html
    js = (ROOT / "mini_app" / "screens" / "deals_board.js").read_text(encoding="utf-8")
    assert "async function openDeals()" in js
    assert "/api/miniapp/crm/board" in js
    # Стадию двигает та же ручка, что у карточки клиента, — второй путь записи
    # стадии означал бы второй набор правил.
    assert "/api/miniapp/uch/contacts/" in js and "/crm'" in js
    assert "openContactDetail(" in js
    assert "openDeals" in miniapp_source()
