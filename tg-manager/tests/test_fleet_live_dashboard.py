"""«Флот сейчас» — живая вкладка единого дашборда.

Владелец открывал «📊 Дашборд» и видел список проблем или кольцо статусов, но
не видел главного: сколько аккаунтов сейчас в строю, что идёт прямо сейчас и
что случилось за сутки — и всё это без нажатия ⟳. Вкладка собирает это одним
запросом (`/api/miniapp/dashboard/live`) и обновляется сама.

Стережём:
  * снимок отдаёт состав флота, пульс, операции и ленту одним ответом;
  * события ограничений подписаны по-русски, неизвестный код — по важности;
  * упавшая выборка гасит свой блок, а не весь ответ;
  * счётчик «за сутки» считается своим запросом, а не по десяти строкам ленты;
  * экран рисует цифры, ведёт плитки в срезы, подсвечивает изменения и
    экранирует имена из Telegram;
  * дашборд открывается на этой вкладке, а скрипт грузится после dashboard.js.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import shutil
import subprocess
from pathlib import Path

from services import mini_app_dashboard as md

ROOT = Path(__file__).resolve().parents[1]
DASH = (ROOT / "mini_app" / "screens" / "dashboard.js").read_text(encoding="utf-8")
LIVE = ROOT / "mini_app" / "screens" / "fleet_live.js"
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")

NOW = dt.datetime(2026, 11, 23, 12, 0, tzinfo=dt.timezone.utc)


class _Pool:
    """Отвечает по ключевым словам запроса; `broken` — какие выборки падают."""

    def __init__(self, broken=()):
        self.broken = set(broken)
        self.sql: list[str] = []

    def _pick(self, q):
        if "account_warmup_plans" in q:
            return "warming"
        if "COUNT(DISTINCT account_id)" in q:
            return "crit"
        if "FROM restriction_events re" in q:
            return "restrictions"
        if "FROM tg_accounts" in q and "online" in q:
            return "fleet"
        if "queued" in q:
            return "ops_now"
        if "finished_at DESC" in q:
            return "finished"
        if "INTERVAL '24 hours'" in q and "operation_queue" in q:
            return "ops_24h"
        if "LIMIT 6" in q:
            return "active"
        return "other"

    async def fetchrow(self, q, *a):
        self.sql.append(q)
        k = self._pick(q)
        if k in self.broken:
            raise RuntimeError("блип базы")
        return {
            "fleet": {"total": 40, "online": 30, "cooldown": 3, "restricted": 2, "lost": 4, "session": 1},
            "warming": {"n": 7},
            "ops_now": {"running": 2, "queued": 5, "paused": 1},
            "ops_24h": {"done": 11, "failed": 2},
            "crit": {"n": 14},
        }.get(k)

    async def fetch(self, q, *a):
        self.sql.append(q)
        k = self._pick(q)
        if k in self.broken:
            raise RuntimeError("блип базы")
        if k == "active":
            return [{"id": 5, "op_type": "invite", "label": "", "status": "running",
                     "done": 40, "total": 100, "started_at": NOW, "created_at": NOW}]
        if k == "finished":
            return [{"id": 4, "op_type": "broadcast", "label": "Рассылка", "status": "partial",
                     "done": 8, "total": 10, "finished_at": NOW - dt.timedelta(hours=1)}]
        if k == "restrictions":
            return [
                {"id": 1, "event_type": "ban_detected", "severity": "critical",
                 "created_at": NOW - dt.timedelta(minutes=5), "account_id": 9,
                 "first_name": "", "username": "acc9", "phone": "+7999"},
                {"id": 2, "event_type": "something_new", "severity": "warning",
                 "created_at": NOW - dt.timedelta(hours=3), "account_id": None,
                 "first_name": None, "username": None, "phone": None},
            ]
        return []

    async def fetchval(self, q, *a):
        self.sql.append(q)
        return 0


def _patch(monkeypatch, pulse=None, attention=None):
    from services import infra_memory

    async def _p(pool, uid):
        if isinstance(pulse, Exception):
            raise pulse
        return pulse if pulse is not None else {
            "healthy": 30, "at_risk": 5, "quarantine": 4, "total": 39, "avg_score": 0.734}

    async def _a(pool, uid):
        return attention if attention is not None else {
            "items": [{"kind": "x", "severity": "high", "title": "t", "detail": "d", "count": 1}] * 5,
            "high": 2, "total": 5}

    monkeypatch.setattr(infra_memory, "get_account_health_summary", _p)
    monkeypatch.setattr(md, "attention_items", _a)


def test_snapshot_has_everything_in_one_answer(monkeypatch):
    _patch(monkeypatch)
    out = asyncio.run(md.live_snapshot(_Pool(), 1))
    assert out["fleet"] == {"total": 40, "online": 30, "cooldown": 3, "restricted": 2,
                            "lost": 4, "session": 1, "warming": 7}
    assert out["pulse"]["score"] == 73 and out["pulse"]["quarantine"] == 4
    assert out["ops"] == {"running": 2, "queued": 5, "paused": 1, "done_24h": 11, "failed_24h": 2}
    assert out["active"][0]["id"] == 5 and out["active"][0]["done"] == 40
    # Лента — по времени, свежее сверху: бан пять минут назад, затем операция.
    kinds = [e["kind"] for e in out["events"]]
    assert kinds == ["restriction", "op", "restriction"]
    assert out["events"][1]["severity"] == "warn"       # partial
    # «Требует внимания» — первые три карточки и честное общее число.
    assert out["attention"]["total"] == 5 and len(out["attention"]["items"]) == 3


def test_restrictions_speak_russian(monkeypatch):
    _patch(monkeypatch)
    ev = [e for e in asyncio.run(md.live_snapshot(_Pool(), 1))["events"] if e["kind"] == "restriction"]
    assert ev[0]["title"] == "Аккаунт забанен" and ev[0]["account"] == "@acc9"
    assert ev[0]["severity"] == "bad"
    # Незнакомый код детектора не печатается как есть.
    assert ev[1]["title"] == "Предупреждение по аккаунту"
    assert "something_new" not in json.dumps(ev, ensure_ascii=False)
    assert md.restriction_label("whatever", "critical") == "Серьёзное ограничение"


def test_critical_count_is_its_own_query_not_the_feed(monkeypatch):
    """Лента обрезана десятью строками; число за сутки обязано быть полным."""
    _patch(monkeypatch)
    out = asyncio.run(md.live_snapshot(_Pool(), 1))
    assert out["critical_24h"] == 14


def test_a_broken_block_does_not_blank_the_screen(monkeypatch):
    _patch(monkeypatch, pulse=RuntimeError("пульс упал"))
    out = asyncio.run(md.live_snapshot(_Pool(broken={"fleet", "restrictions"}), 1))
    assert out["fleet"] is None, "непрочитанный флот — «нет данных», а не нули"
    assert out["pulse"] is None
    assert out["ops"]["running"] == 2 and out["active"], "операции обязаны остаться"
    assert all(e["kind"] == "op" for e in out["events"])


def test_queries_are_scoped_and_carry_no_session(monkeypatch):
    _patch(monkeypatch)
    pool = _Pool()
    asyncio.run(md.live_snapshot(pool, 1))
    for q in pool.sql:
        assert "owner_id" in q, q
        assert "session_str" not in q, "живой сводке сессия аккаунта не нужна"


def test_route_is_registered():
    src = (ROOT / "services" / "mini_app_dashboard.py").read_text(encoding="utf-8")
    assert '"/api/miniapp/dashboard/live", dashboard_live' in src
    assert "/api/miniapp/dashboard/live" in LIVE.read_text(encoding="utf-8")


def test_dashboard_opens_live_and_script_is_loaded_after_hub():
    m = __import__("re").search(r"async function openUnifiedDashboard\(\)\s*\{(.*?)\n\}", DASH, __import__("re").S)
    assert m and "_udTabCur = 'live'" in m.group(1)
    assert "{ key: 'live'" in DASH and "_flLoad" in DASH
    i_db = HTML.index('<script src="screens/dashboard.js">')
    i_fl = HTML.index('<script src="screens/fleet_live.js">')
    assert i_db < i_fl


_HARNESS = r'''
const fs=require('fs');
const rd=p=>fs.readFileSync(p,'utf8').replace(/^.use strict.;?$/m,'');
global.window=global; global.STACK=['s-uni-dash']; global.document={visibilityState:'visible',
  getElementById:()=>null, addEventListener:()=>{}};
global.opRu=t=>({invite:'Инвайт'}[t]||t); global.ago=()=>'5 мин назад';
global.healthGoAccounts=()=>{}; global.openHealth=()=>{}; global.openWarmup=()=>{};
global.openProxies=()=>{}; global.openAccImportModal=()=>{};
eval(rd(process.argv[1]).replace('const UI =', 'var UI ='));
eval(rd(process.argv[2]).replace('let _udTabCur', 'var _udTabCur').replace('let _UD_ITEMS', 'var _UD_ITEMS'));
eval(rd(process.argv[3]));
const d=JSON.parse(process.argv[4]);
const prev=JSON.parse(JSON.stringify(d)); prev.fleet.online=33; prev.fleet.restricted=1;
const html=_flRender(d, prev);
const first=_flRender(d, null);
process.stdout.write(JSON.stringify({html, first, items:_UD_ITEMS.length}));
'''


def test_screen_renders_live_numbers_and_links():
    node = shutil.which("node")
    if not node:
        return
    snap = {
        "fleet": {"total": 40, "online": 30, "cooldown": 3, "restricted": 2, "lost": 4,
                  "session": 1, "warming": 7},
        "pulse": {"healthy": 30, "at_risk": 5, "quarantine": 4, "total": 39, "score": 73},
        "ops": {"running": 1, "queued": 2, "paused": 0, "done_24h": 9, "failed_24h": 1},
        "active": [{"id": 5, "op_type": "invite", "label": "", "status": "running",
                    "done": 40, "total": 100, "since": "2026-11-23T11:00:00+00:00"}],
        "events": [{"kind": "restriction", "at": "2026-11-23T11:55:00+00:00", "account_id": 9,
                    "title": "Аккаунт забанен", "account": "<b>evil</b>", "severity": "bad"}],
        "critical_24h": 1,
        "attention": {"total": 5, "high": 1, "items": [
            {"severity": "high", "title": "Упало", "detail": "", "count": 1,
             "action": {"label": "Повторить", "endpoint": "/x"}}]},
    }
    out = subprocess.run(
        [node, "-e", _HARNESS, str(ROOT / "mini_app/screens/ui_kit.js"),
         str(ROOT / "mini_app/screens/dashboard.js"), str(LIVE), json.dumps(snap)],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)
    html, first = r["html"], r["first"]
    # Цифры флота и пульса на месте, плитки ведут в свои срезы.
    assert ">73<" in html and "В строю" in html and "Прогрев" in html
    assert "healthGoAccounts(&#39;cooldown&#39;)" in html or "healthGoAccounts('cooldown')" in html
    assert "openOpDetail(5)" in html and "Инвайт" in html
    # Изменение с прошлого снимка подсвечено: в строю стало меньше — хуже,
    # спамблоков больше — тоже хуже («в строю» стоит и в герое, и в плитке).
    # Без прошлого снимка подсветки нет.
    assert html.count("fl-bump is-worse") == 3
    assert "fl-bump" not in first
    # Имя из Telegram экранировано.
    assert "<b>evil</b>" not in html and "&lt;b&gt;evil" in html
    # Карточки внимания исполняются через _udAct и знают свою карточку.
    assert "_udAct(0)" in html and r["items"] == 1
    assert "Все 5" in html
    # На экране нет английских статусов.
    for word in ("running", "pending", "partial"):
        assert f">{word}<" not in html
