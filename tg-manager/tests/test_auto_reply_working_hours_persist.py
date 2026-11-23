"""Рабочие часы автоответа принимались формой и ТЕРЯЛИСЬ в INSERT
(create_auto_reply их не читал): правило отвечало круглосуточно, а владелец
считал, что ночью бот молчит. Тот же класс, что match_mode и кнопки
(test_auto_reply_match_mode)."""
import asyncio
import json
from pathlib import Path

from aiohttp import web
from services import mini_app_api as M
from services.mini_app_bots import auto_reply_hours

from tests.miniapp_source import api_function

HTML = (Path(__file__).resolve().parents[1] / "mini_app/index.html").read_text(encoding="utf-8")


def test_hours_parse_whole_window_only():
    assert auto_reply_hours({"active_from_hour": 9, "active_to_hour": 21}) == (9, 21)
    assert auto_reply_hours({"active_from_hour": "22", "active_to_hour": "6"}) == (22, 6)
    assert auto_reply_hours({"active_from_hour": 0, "active_to_hour": 0}) == (0, 0)
    assert auto_reply_hours({"active_from_hour": 9}) == (None, None)
    assert auto_reply_hours({"active_from_hour": 9, "active_to_hour": 24}) == (None, None)
    assert auto_reply_hours({"active_from_hour": "x", "active_to_hour": 5}) == (None, None)
    assert auto_reply_hours({"active_from_hour": True, "active_to_hour": 5}) == (None, None)
    assert auto_reply_hours({}) == (None, None)


def test_create_and_update_write_hours():
    assert "auto_reply_hours(body)" in api_function("_ar_parse")
    body = api_function("create_auto_reply")
    assert "f = _ar_parse(body)" in body
    insert = body[body.index("INSERT INTO auto_replies"):]
    assert "active_from_hour, active_to_hour" in insert[:400]
    assert 'f["active_from_hour"], f["active_to_hour"])' in insert[:900]
    upd = api_function("update_auto_reply")
    assert "f = _ar_parse(body)" in upd
    assert "active_from_hour=$10, active_to_hour=$11" in upd
    assert "WHERE added_by=$2 AND deleted_at IS NULL" in upd


def test_priority_field_matches_server_clamp():
    # Сервер режет приоритет в 0..1000; поле разрешало -100..100, и
    # отрицательный приоритет молча становился нулём.
    assert 'id="arPriority" min="0" max="1000"' in HTML


# ── Поведение ручек на заглушке пула ───────────────────────────────────────
UID = 770011


class _Pool:
    def __init__(self, owns=1, row=True):
        self.calls = []
        self._owns, self._row = owns, row

    async def fetchval(self, q, *a):
        self.calls.append((q, a))
        return self._owns

    async def fetchrow(self, q, *a):
        self.calls.append((q, a))
        return {"id": 5} if self._row else None

    async def fetch(self, q, *a):
        self.calls.append((q, a))
        return []

    async def execute(self, q, *a):
        self.calls.append((q, a))
        return "OK"


class _Req:
    def __init__(self, match, body):
        self.match_info = match
        self._body = body
        self.headers = {}
        self.rel_url = type("U", (), {"query": {}})()
        self.query = {}
        self.query_string = ""

    async def json(self):
        return self._body


def _route(pool, method, path):
    app = web.Application()
    M.setup_routes(app, pool)
    for r in app.router.routes():
        info = r.resource.get_info() if r.resource else {}
        if r.method == method and (info.get("path") or info.get("formatter")) == path:
            return r.handler
    raise AssertionError(path)


def _call(monkeypatch, pool, method, path, match, body):
    monkeypatch.setattr(M, "_get_uid", lambda r: UID)
    resp = asyncio.run(_route(pool, method, path)(_Req(match, body)))
    return resp.status, json.loads(resp.body.decode("utf-8"))


def test_start_rule_with_null_keyword_is_created(monkeypatch):
    # Форма шлёт keyword: null у правил «/start» и «любое сообщение», а
    # `body.get("keyword", "").strip()` падал на None — такие правила из
    # мини-аппа не создавались вовсе.
    pool = _Pool()
    st, d = _call(monkeypatch, pool, "POST", "/api/miniapp/bot/{bot_id}/auto_reply", {"bot_id": "42"},
                  {"trigger_type": "start", "keyword": None, "response_text": "Привет",
                   "active_from_hour": 9, "active_to_hour": 21})
    assert st == 200 and d["ok"] is True
    ins = [a for q, a in pool.calls if "INSERT INTO auto_replies" in q][0]
    assert ins[1] == "start" and ins[2] is None and ins[-2:] == (9, 21)


def test_update_rule_scoped_to_owner(monkeypatch):
    pool = _Pool()
    st, d = _call(monkeypatch, pool, "PUT", "/api/miniapp/auto_reply/{reply_id}", {"reply_id": "5"},
                  {"trigger_type": "keyword", "keyword": " цена ", "response_text": "100 ₽",
                   "match_mode": "exact", "priority": 7})
    assert st == 200 and d["id"] == 5
    q, a = [c for c in pool.calls if "UPDATE auto_replies" in c[0]][0]
    assert a[:7] == (5, UID, "keyword", "цена", "100 ₽", "exact", None) and a[7] == 7
    assert a[-2:] == (None, None)


def test_update_rejects_foreign_and_empty(monkeypatch):
    st, _ = _call(monkeypatch, _Pool(row=False), "PUT", "/api/miniapp/auto_reply/{reply_id}", {"reply_id": "5"},
                  {"trigger_type": "any", "response_text": "x"})
    assert st == 404
    st, _ = _call(monkeypatch, _Pool(), "PUT", "/api/miniapp/auto_reply/{reply_id}", {"reply_id": "5"},
                  {"trigger_type": "keyword", "keyword": "", "response_text": "x"})
    assert st == 400


def _js_fn(name):
    import re
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", HTML)
    assert m, name
    return m.group(0)


def test_edit_form_prefills_and_puts():
    import subprocess
    js = _js_fn("arButtonsText") + (
        "console.log(JSON.stringify([arButtonsText('[{\"text\":\"Цены &amp; сроки\",\"url\":\"https://a.b\"}]'),"
        "arButtonsText(null),arButtonsText([{text:'x'}])]))")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["Цены & сроки | https://a.b", "", ""]
    save = _js_fn("saveAutoReply")
    assert "'/api/miniapp/auto_reply/'+Number(_editId), {method:'PUT'" in save
    assert "editAr(${r.id})" in _js_fn("renderArList")
    assert "ar.buttons," in api_function("bot_auto_replies")
