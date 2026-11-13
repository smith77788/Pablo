"""Карточка операции говорит, ЧТО и как запускали, и отдаёт журнал файлом.

Найдено живым обходом окна операций (03.11.2026):

* карточка не показывала параметры запуска — текст, цели, аккаунты, паузу:
  они лежали в params и до экрана не доходили;
* не было ни времени старта, ни длительности, ни скорости;
* журнал жил в окошке высотой 300 px внутри экрана, и выгрузить его — ни
  целиком, ни только неудачные цели, ради которых на него и смотрят, — было
  нечем;
* заголовок «Операция #3» и строка «Тип» вместо названия;
* у проваленной операции «Повторить» стоял дважды.

Плюс выгрузка истории операций писала в CSV внутренние имена типа и статуса.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest
from datetime import datetime, timedelta, timezone

from services import op_params_view as pv
from tests.miniapp_source import api_function, js_function

# ── описание параметров (чистое) ──────────────────────────────────────────────


def test_publish_params_read_like_a_human_wrote_them():
    rows = pv.describe("mass_publish", {
        "text": "Скидка 20% до пятницы", "channel_ids": [11, 22, 33, 44, 55],
        "delay": 30, "repeat_interval_min": 180, "target": "all", "owner_id": 7,
    })
    got = {r["label"]: r["value"] for r in rows}
    assert got["Текст"] == "Скидка 20% до пятницы"
    assert got["Каналы"].startswith("5: 11, 22, 33") and "и ещё 2" in got["Каналы"]
    assert got["Пауза между действиями"] == "30 секунд"
    assert got["Повтор"] == "каждые 3 часа"
    assert got["Куда"] == "каналы и группы"
    assert "owner_id" not in str(rows), "служебный ключ на экране"


def test_secrets_never_reach_the_screen():
    rows = pv.describe("x", {"text": "ok", "bot_token": "123:ABC", "session_str": "1Aa",
                             "proxy_url": "socks5://u:p@h", "password": "p"})
    blob = str(rows)
    for s in ("123:ABC", "1Aa", "socks5", "'p'"):
        assert s not in blob, f"секрет на экране: {s}"


def test_unknown_or_empty_params_show_nothing():
    assert pv.describe("x", {"weird_key": 1, "text": ""}) == []
    assert pv.describe("x", None) == []


# ── ручки ─────────────────────────────────────────────────────────────────────

_NOW = datetime.now(timezone.utc)


class _Pool:
    async def fetchrow(self, sql, *a):
        if "FROM operation_queue WHERE id=$1 AND owner_id=$2" in sql and "params AS _params" in sql:
            return {"id": 5, "op_type": "mass_publish", "status": "done", "label": None,
                    "total_items": 10, "done_items": 10, "error_msg": None,
                    "created_at": _NOW - timedelta(minutes=30),
                    "started_at": _NOW - timedelta(minutes=20),
                    "finished_at": _NOW - timedelta(minutes=10),
                    "scheduled_for": None, "summary": "Готово", "err_cnt": 2,
                    "_result": {}, "_params": {"text": "Привет", "channel_ids": [1, 2]}}
        if "SELECT op_type, label FROM operation_queue" in sql:
            return {"op_type": "mass_publish", "label": None}
        return None

    async def fetch(self, sql, *a):
        if "FROM operation_log" in sql:
            rows = [{"step_num": 1, "target": "@a", "status": "ok", "message": None, "created_at": _NOW},
                    {"step_num": 2, "target": "@b", "status": "error",
                     "message": "нет прав на публикацию", "created_at": _NOW}]
            if "lower(status) IN" in sql:
                rows = [r for r in rows if r["status"] == "error"]
            return rows
        return []

    async def fetchval(self, *a, **k):
        return 0

    async def execute(self, *a, **k):
        return "OK"


def _get(monkeypatch, path, pool=None):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from services import mini_app_api

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: 777)

    async def _go():
        app = web.Application()
        mini_app_api.setup_routes(app, pool or _Pool())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.get(path)
            ctype = r.headers.get("Content-Type", "")
            return r.status, (await r.json() if "json" in ctype else await r.text())
        finally:
            await client.close()

    return asyncio.run(_go())


def test_card_gets_params_and_duration(monkeypatch):
    status, d = _get(monkeypatch, "/api/miniapp/operation/5")
    assert status == 200, d
    labels = [p["label"] for p in d["params_view"]]
    assert "Текст" in labels and "Каналы" in labels, d["params_view"]
    assert d["duration_s"] == 600 and d["started_at"], d


def test_log_exports_as_csv_and_errors_only(monkeypatch):
    status, text = _get(monkeypatch, "/api/miniapp/operation/5/log.csv")
    assert status == 200 and "@a" in text and "@b" in text and "Успешно" in text
    status, text = _get(monkeypatch, "/api/miniapp/operation/5/log.csv?status=error")
    assert status == 200 and "@b" in text and "@a" not in text
    assert "нет прав на публикацию" in text


def test_history_export_speaks_russian():
    body = api_function("operation_export")
    assert "op_status.label(" in body and "OP_REGISTRY" in body


# ── экран ─────────────────────────────────────────────────────────────────────


def test_card_screen_shows_what_was_launched():
    body = js_function("openOpDetail")
    assert "params_view" in body and "Параметры запуска" in body
    assert "duration_s" in body and "started_at" in body
    assert "log.csv" in body and "status=error" in body
    assert "max-height:300px" not in body, "журнал снова в окошке с прокруткой"
    assert "a.fn === 'retryOp'" in body, "«Повторить» снова дублируется"


def test_strike_card_distinguishes_selected_and_passed_to_executor(monkeypatch):
    class StrikePool(_Pool):
        async def fetchrow(self, sql, *args):
            row = await super().fetchrow(sql, *args)
            if row and "_params" in row:
                row.update(op_type="strike", _params={"account_ids": [1, 2, 3]},
                           _result={"accounts_used": 1})
            return row

    status, body = _get(monkeypatch, "/api/miniapp/operation/5", StrikePool())
    assert status == 200
    params = {row["label"]: row["value"] for row in body["params_view"]}
    assert params["Выбрано при запуске"] == "3: 1, 2, 3"
    assert params["Передано исполнителю"] == "1"


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_strike_report_formats_only_allowlisted_tags():
    assert "opSummaryHtml(o.summary,o.op_type)" in js_function("openOpDetail")
    js = js_function("esc") + "\n" + js_function("opSummaryHtml") + r"""
const assert = require('node:assert/strict');
assert.equal(opSummaryHtml('<b>Итог</b>\n<code>@name</code> <i>Текст</i>', 'strike'),
             '<b>Итог</b>\n<code>@name</code> <i>Текст</i>');
assert.equal(opSummaryHtml('<img src=x onerror=alert(1)>', 'strike'),
             '&lt;img src=x onerror=alert(1)&gt;');
assert.equal(opSummaryHtml('<b onclick=alert(1)>', 'strike'),
             '&lt;b onclick=alert(1)&gt;');
assert.equal(opSummaryHtml('<b>Данные</b>', 'mass_publish'), '&lt;b&gt;Данные&lt;/b&gt;');
"""
    subprocess.run(["node", "-"], input=js, text=True, check=True)
