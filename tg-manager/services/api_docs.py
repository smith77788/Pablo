"""Документация публичного API и вебхуков: OpenAPI 3.1 + страница /api/docs.

Спецификация собирается из кода, а не лежит отдельным файлом: каталог событий
берётся у `services.webhooks.EVENTS`, области ключей — у `services.api_keys`.
Сверку «каждый маршрут /api/v1 описан, каждое событие имеет пример» держит
tests/test_api_docs.py — новая ручка без описания роняет CI, а не тихо
остаётся без документации.

Страница /api/docs — самодостаточный HTML без внешних скриптов и шрифтов:
её открывают из мини-аппа и у клиентов за прокси, где CDN не всегда доступен.
"""
from __future__ import annotations

import html
import json

API_VERSION = "1.1.0"

# ── Общие куски схемы ────────────────────────────────────────────────────────

_ERROR = {"type": "object", "properties": {"error": {"type": "string"}},
          "required": ["error"]}

_OPERATION = {
    "type": "object",
    "properties": {
        "id": {"type": "integer", "example": 1842},
        "type": {"type": "string", "example": "broadcast"},
        "type_label": {"type": "string", "example": "Рассылка"},
        "label": {"type": "string", "example": "Акция ноября"},
        "status": {"type": "string", "enum": ["pending", "running", "paused", "scheduled",
                                              "waiting_approval", "done", "partial",
                                              "failed", "cancelled"]},
        "total_items": {"type": "integer", "example": 500},
        "done_items": {"type": "integer", "example": 498},
        "error": {"type": ["string", "null"]},
        "created_at": {"type": ["string", "null"], "format": "date-time"},
        "started_at": {"type": ["string", "null"], "format": "date-time"},
        "finished_at": {"type": ["string", "null"], "format": "date-time"},
    },
}

_ACCOUNT = {
    "type": "object",
    "properties": {
        "id": {"type": "integer", "example": 17},
        "phone": {"type": "string", "example": "+79990001122"},
        "first_name": {"type": "string", "example": "Анна"},
        "username": {"type": "string", "example": "anna_sales"},
        "is_active": {"type": "boolean"},
    },
}

_MESSAGE = {
    "type": "object",
    "properties": {
        "id": {"type": "integer", "example": 5512},
        "date": {"type": ["string", "null"], "format": "date-time"},
        "text": {"type": "string", "example": "Здравствуйте! Чем помочь?"},
        "out": {"type": "boolean", "description": "true — сообщение отправил аккаунт"},
        "from_id": {"type": ["integer", "null"]},
        "buttons": {"type": "array", "items": {
            "type": "object", "properties": {"text": {"type": "string"},
                                             "data": {"type": "string"}}}},
    },
}


def _resp(desc: str, schema: dict | None = None, example=None) -> dict:
    out: dict = {"description": desc}
    if schema is not None:
        content: dict = {"schema": schema}
        if example is not None:
            content["example"] = example
        out["content"] = {"application/json": content}
    return out


_STD = {
    "401": {"$ref": "#/components/responses/Unauthorized"},
    "403": {"$ref": "#/components/responses/Forbidden"},
    "429": {"$ref": "#/components/responses/TooManyRequests"},
}

_OWNER_PARAM = {
    "name": "owner_id", "in": "query", "required": False,
    "schema": {"type": "integer"},
    "description": "Не нужен с личным ключом (владелец берётся из ключа); если передан — "
                   "обязан совпасть с владельцем ключа, иначе 403. Обязателен только со "
                   "служебным ключом платформы.",
}


def _op(summary: str, desc: str, *, scope: str | None, limit: int | None,
        params=None, body=None, responses=None, tag: str = "API") -> dict:
    d = desc
    if scope:
        d += f"\n\nОбласть ключа: `{scope}`. Лимит: {limit} запросов в минуту на владельца."
    op: dict = {"summary": summary, "description": d, "tags": [tag]}
    if params:
        op["parameters"] = params
    if body:
        op["requestBody"] = {"required": True, "content": {"application/json": body}}
    op["responses"] = {**(responses or {}), **(_STD if scope else {})}
    if scope is None:
        op["security"] = []
    else:
        op["x-scope"] = scope
        op["x-rate-limit-per-minute"] = limit
    return op


def paths() -> dict:
    """Описания всех маршрутов /api/v1. Ключ — путь в нотации OpenAPI."""
    return {
        "/api/v1/health": {"get": _op(
            "Проверка доступности", "Без ключа. Отвечает, пока сервис жив.",
            scope=None, limit=None,
            responses={"200": _resp("Сервис работает", {"type": "object"},
                                    {"status": "ok", "service": "Infragram REST API v1"})})},
        "/api/v1/me": {"get": _op(
            "Кто я", "Чей это ключ и что ему разрешено. Удобно первым запросом "
            "проверить, что ключ вставлен правильно.",
            scope="read", limit=60,
            responses={"200": _resp("Владелец ключа", {"type": "object", "properties": {
                "owner_id": {"type": "integer"}, "admin": {"type": "boolean"},
                "scopes": {"type": "array", "items": {"type": "string"}},
                "key_id": {"type": ["integer", "null"]}}},
                {"owner_id": 123456789, "admin": False, "scopes": ["read", "write"],
                 "key_id": 7})})},
        "/api/v1/accounts": {"get": _op(
            "Аккаунты Telegram", "Все аккаунты владельца ключа.",
            scope="read", limit=30, params=[_OWNER_PARAM],
            responses={"200": _resp("Список аккаунтов", {"type": "array", "items": _ACCOUNT},
                                    [{"id": 17, "phone": "+79990001122", "first_name": "Анна",
                                      "username": "anna_sales", "is_active": True}]),
                       "400": {"$ref": "#/components/responses/BadRequest"}})},
        "/api/v1/operations": {"get": _op(
            "Операции", "Рассылки, инвайты, парсинг и другие фоновые операции — "
            "новые сверху. Листание: передайте `next_before_id` из ответа в `before_id`.",
            scope="read", limit=60, params=[
                _OWNER_PARAM,
                {"name": "status", "in": "query", "schema": {"type": "string",
                 "enum": _OPERATION["properties"]["status"]["enum"]}},
                {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1,
                                                            "maximum": 500, "default": 50}},
                {"name": "before_id", "in": "query", "schema": {"type": "integer"}}],
            responses={"200": _resp("Страница операций", {"type": "object", "properties": {
                "operations": {"type": "array", "items": _OPERATION},
                "next_before_id": {"type": ["integer", "null"]}}}),
                "400": {"$ref": "#/components/responses/BadRequest"}})},
        "/api/v1/operations/{op_id}": {"get": _op(
            "Одна операция", "Состояние операции: сколько сделано, чем закончилась.",
            scope="read", limit=120, params=[
                {"name": "op_id", "in": "path", "required": True, "schema": {"type": "integer"}},
                _OWNER_PARAM],
            responses={"200": _resp("Операция", _OPERATION),
                       "404": _resp("Операции нет или она чужая", _ERROR,
                                    {"error": "operation not found"})})},
        "/api/v1/send_message": {"post": _op(
            "Отправить сообщение", "Сообщение от имени аккаунта владельца.",
            scope="write", limit=10, body={"schema": {"type": "object", "required": [
                "account_id", "chat_id", "text"], "properties": {
                "account_id": {"type": "integer"}, "chat_id": {"type": "integer"},
                "text": {"type": "string", "maxLength": 4096},
                "owner_id": {"type": "integer"}}},
                "example": {"account_id": 17, "chat_id": 777000, "text": "Добрый день!"}},
            responses={"200": _resp("Отправлено", {"type": "object", "properties": {
                "ok": {"type": "boolean"}}}, {"ok": True}),
                "400": {"$ref": "#/components/responses/BadRequest"},
                "404": _resp("Аккаунта нет или он чужой", _ERROR)})},
        "/api/v1/click_button": {"post": _op(
            "Нажать инлайн-кнопку", "Нажатие кнопки под сообщением бота от имени аккаунта. "
            "`button_data` — поле `data` кнопки из `/get_messages`.",
            scope="write", limit=10, body={"schema": {"type": "object", "required": [
                "account_id", "chat_id", "message_id", "button_data"], "properties": {
                "account_id": {"type": "integer"}, "chat_id": {"type": "integer"},
                "message_id": {"type": "integer"},
                "button_data": {"type": "string", "maxLength": 512},
                "owner_id": {"type": "integer"}}}},
            responses={"200": _resp("Ответ бота на нажатие", {"type": "object", "properties": {
                "ok": {"type": "boolean"}, "alert": {"type": "boolean"},
                "message": {"type": "string"}}}),
                "400": {"$ref": "#/components/responses/BadRequest"},
                "404": _resp("Аккаунта нет или он чужой", _ERROR)})},
        "/api/v1/get_messages": {"get": _op(
            "Последние сообщения чата", "Сообщения чата глазами аккаунта, новые сверху.",
            scope="read", limit=15, params=[
                {"name": "account_id", "in": "query", "required": True, "schema": {"type": "integer"}},
                {"name": "chat_id", "in": "query", "required": True, "schema": {"type": "integer"}},
                {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1,
                                                            "maximum": 100, "default": 20}},
                _OWNER_PARAM],
            responses={"200": _resp("Сообщения", {"type": "object", "properties": {
                "messages": {"type": "array", "items": _MESSAGE},
                "count": {"type": "integer"}}}),
                "400": {"$ref": "#/components/responses/BadRequest"},
                "404": _resp("Аккаунта нет или он чужой", _ERROR)})},
        "/api/v1/openapi.json": {"get": _op(
            "Эта спецификация", "OpenAPI 3.1 — для Postman, Insomnia, генераторов клиентов.",
            scope=None, limit=None, tag="Документация",
            responses={"200": _resp("Спецификация", {"type": "object"})})},
    }


# ── Вебхуки ──────────────────────────────────────────────────────────────────

# Пример данных (`data`) каждого события. Ключи — ровно те, что кладут
# источники (триггеры schema_v262/v270/v272, шина сценариев).
EVENT_EXAMPLES: dict[str, dict] = {
    "operation.done": {"operation_id": 1842, "op_type": "broadcast", "status": "done",
                       "total_items": 500, "done_items": 500, "error": None,
                       "created_at": "2026-11-01T10:00:00+00:00",
                       "finished_at": "2026-11-01T10:42:00+00:00"},
    "operation.partial": {"operation_id": 1843, "op_type": "invite", "status": "partial",
                          "total_items": 200, "done_items": 141, "error": None},
    "operation.failed": {"operation_id": 1844, "op_type": "parse", "status": "failed",
                         "total_items": 0, "done_items": 0, "error": "FloodWait 3600"},
    "operation.cancelled": {"operation_id": 1845, "op_type": "broadcast",
                            "status": "cancelled", "total_items": 300, "done_items": 12},
    "account.added": {"account_id": 17, "phone": "+79990001122", "username": "anna_sales",
                      "status": "ok"},
    "account.lost": {"account_id": 17, "phone": "+79990001122", "username": "anna_sales",
                     "old_status": "ok", "new_status": "banned"},
    "account.status_changed": {"account_id": 17, "phone": "+79990001122",
                               "old_status": "ok", "new_status": "flood"},
    "account.restricted": {"account_id": 17, "phone": "+79990001122", "kind": "spamblock",
                           "severity": "high"},
    "pipeline.updated": {"contact_id": "5b0c6a8e-3c1f-4a59-9d1e-2f0b8f6d1c2a",
                         "stage": "negotiation", "old_stage": "proposal",
                         "deal_value": 45000, "currency": "RUB", "created": False},
    "deal.updated": {"deal_id": 311, "title": "Заявка из сценария — @ivan", "contact": "@ivan",
                     "stage": "contacted", "old_stage": "new", "value": 12000,
                     "created": False},
    "contact.started": {"param": "promo_nov", "new": True, "bot_id": 7012345678,
                        "chat_id": 555000111, "automation_event_id": 90211},
    "message.received": {"text": "Сколько стоит доставка?", "bot_id": 7012345678,
                         "chat_id": 555000111, "automation_event_id": 90212},
    "button.clicked": {"workflow_id": 12, "button": "Записаться", "bot_id": 7012345678,
                       "chat_id": 555000111, "automation_event_id": 90213},
    "contact.tag_added": {"tag": "vip", "bot_id": 7012345678, "chat_id": 555000111,
                          "automation_event_id": 90214},
    "contact.tag_removed": {"tag": "холодный", "bot_id": 7012345678, "chat_id": 555000111,
                            "automation_event_id": 90215},
    "contact.field_set": {"field": "phone", "value": "+79990002233", "bot_id": 7012345678,
                          "chat_id": 555000111, "automation_event_id": 90216},
    "contact.opted_out": {"bot_id": 7012345678, "chat_id": 555000111,
                          "automation_event_id": 90217},
    "order.created": {"order_id": 4410, "summary": "Пицца Маргарита × 2", "bot_id": 7012345678,
                      "chat_id": 555000111, "automation_event_id": 90218},
    "payment.received": {"source": "order", "order_id": 4410, "amount": 129000,
                         "currency": "RUB", "charge_id": "tg_ch_…", "bot_id": 7012345678,
                         "chat_id": 555000111, "automation_event_id": 90219},
    "deal.created": {"deal_id": 312, "title": "Заявка из сценария — @ivan",
                     "source": "workflow", "workflow_id": 12, "bot_id": 7012345678,
                     "chat_id": 555000111, "automation_event_id": 90220},
    "deal.stage_changed": {"deal_id": 312, "stage": "qualified", "old_stage": "contacted",
                           "automation_event_id": 90221},
    "keyword.hit": {"keyword": "доставка", "chat_id": -1001234567890,
                    "automation_event_id": 90222},
    "workflow.completed": {"workflow_id": 12, "run_id": 5501, "bot_id": 7012345678,
                           "chat_id": 555000111, "automation_event_id": 90223},
    "webhook.test": {"message": "Проверка связи с Infragram", "endpoint_id": 3},
}


def _event_example(event: str) -> dict:
    return {"id": 98765, "event": event, "created_at": "2026-11-01T10:42:00+00:00",
            "data": EVENT_EXAMPLES.get(event, {})}


def webhooks_spec() -> dict:
    from services import webhooks as _wh
    out = {}
    for ev, label in _wh.EVENTS.items():
        out[ev] = {"post": {
            "summary": label,
            "description": f"Событие `{ev}`. Подпись — заголовок `X-Infragram-Signature`.",
            "tags": ["Вебхуки"],
            "parameters": [
                {"name": "X-Infragram-Event", "in": "header", "schema": {"type": "string"},
                 "example": ev},
                {"name": "X-Infragram-Event-Id", "in": "header",
                 "schema": {"type": "string"}, "description": "Одинаков у всех повторов"},
                {"name": "X-Infragram-Timestamp", "in": "header", "schema": {"type": "string"}},
                {"name": "X-Infragram-Signature", "in": "header", "schema": {"type": "string"},
                 "example": "t=1793500000,v1=5f2b…"},
            ],
            "requestBody": {"content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/WebhookEnvelope"},
                "example": _event_example(ev)}}},
            "responses": {"200": {"description": "Любой ответ 2xx — доставлено"}},
        }}
    return out


def build_spec(server_url: str = "") -> dict:
    from services import api_keys as _keys
    scopes = "; ".join(f"`{s}` — {_keys.SCOPE_LABELS[s]}" for s in _keys.SCOPES)
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Infragram API",
            "version": API_VERSION,
            "description": (
                "Публичный API Infragram: аккаунты, операции, сообщения — и исходящие "
                "вебхуки о событиях.\n\nКлюч выпускается в мини-аппе: «Настройки» → "
                "«Интеграции» → «+ Ключ». Передаётся заголовком `X-Api-Key` (или "
                f"`Authorization: Bearer …`). Области ключа: {scopes}."),
        },
        "servers": [{"url": server_url.rstrip("/")}] if server_url else [],
        "security": [{"ApiKey": []}],
        "tags": [{"name": "API"}, {"name": "Вебхуки"}, {"name": "Документация"}],
        "paths": paths(),
        "webhooks": webhooks_spec(),
        "components": {
            "securitySchemes": {
                "ApiKey": {"type": "apiKey", "in": "header", "name": "X-Api-Key"},
            },
            "schemas": {
                "Error": _ERROR,
                "Operation": _OPERATION,
                "Account": _ACCOUNT,
                "Message": _MESSAGE,
                "WebhookEnvelope": {"type": "object", "required": ["id", "event", "created_at", "data"],
                                    "properties": {
                                        "id": {"type": "integer", "description": "id события; повтор несёт тот же"},
                                        "event": {"type": "string"},
                                        "created_at": {"type": "string", "format": "date-time"},
                                        "data": {"type": "object"}}},
            },
            "responses": {
                "BadRequest": _resp("Неверные параметры", _ERROR,
                                    {"error": "account_id, chat_id, text are required"}),
                "Unauthorized": _resp("Ключа нет, он неверный или отозван", _ERROR,
                                      {"error": "Unauthorized"}),
                "Forbidden": _resp("Ключу не хватает области или owner_id чужой", _ERROR,
                                   {"error": "У ключа нет области «write» — выпустите ключ с нужным доступом"}),
                "TooManyRequests": _resp("Превышен лимит запросов в минуту", _ERROR),
            },
        },
    }


# ── Страница ─────────────────────────────────────────────────────────────────

_VERIFY_PY = '''import hmac, hashlib, time

def verify(secret: str, header: str, body: bytes, tolerance: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(","))
    ts, got = int(parts["t"]), parts["v1"]
    if abs(time.time() - ts) > tolerance:
        return False  # старый запрос — переигрывание
    want = hmac.new(secret.encode(), f"{ts}.".encode() + body,
                    hashlib.sha256).hexdigest()
    return hmac.compare_digest(got, want)

# Flask: verify(SECRET, request.headers["X-Infragram-Signature"], request.get_data())'''

_VERIFY_JS = '''const crypto = require("crypto");

function verify(secret, header, rawBody, tolerance = 300) {
  const parts = Object.fromEntries(header.split(",").map(p => p.split("=", 2)));
  const ts = Number(parts.t);
  if (Math.abs(Date.now() / 1000 - ts) > tolerance) return false;
  const want = crypto.createHmac("sha256", secret)
    .update(ts + "." + rawBody).digest("hex");
  return crypto.timingSafeEqual(Buffer.from(parts.v1), Buffer.from(want));
}
// Express: app.post("/hook", express.raw({type: "*/*"}), (req, res) => …req.body…)'''

_VERIFY_PHP = '''<?php
function verify(string $secret, string $header, string $body, int $tolerance = 300): bool {
    parse_str(str_replace(",", "&", $header), $p);
    if (abs(time() - (int)$p["t"]) > $tolerance) return false;
    $want = hash_hmac("sha256", $p["t"] . "." . $body, $secret);
    return hash_equals($want, $p["v1"]);
}
// verify($secret, $_SERVER["HTTP_X_INFRAGRAM_SIGNATURE"], file_get_contents("php://input"));'''

_CSS = """
:root{--bg:#fff;--fg:#1d2433;--muted:#5b6475;--line:#e3e7ee;--code:#f4f6fa;--acc:#2f6fed;
--get:#0b8a5b;--post:#2f6fed;--del:#c0392b}
@media (prefers-color-scheme:dark){:root{--bg:#12151c;--fg:#e6e9ef;--muted:#9aa3b2;
--line:#262c38;--code:#1b2029;--acc:#6b9bff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:920px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:20px;margin:36px 0 8px;padding-top:8px;
border-top:1px solid var(--line)}h3{font-size:16px;margin:22px 0 6px}
p,li{color:var(--fg)}.muted{color:var(--muted)}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}
code{background:var(--code);padding:1px 5px;border-radius:4px}
pre{background:var(--code);padding:12px;border-radius:8px;overflow-x:auto;margin:8px 0}
pre code{background:none;padding:0}
.ep{border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:12px 0}
.m{display:inline-block;font-weight:700;font-size:12px;color:#fff;border-radius:4px;
padding:2px 7px;margin-right:8px}.m.get{background:var(--get)}.m.post{background:var(--post)}
table{border-collapse:collapse;width:100%;margin:6px 0}td,th{border-bottom:1px solid var(--line);
padding:6px 8px;text-align:left;vertical-align:top;font-size:14px}th{color:var(--muted);font-weight:600}
nav a{color:var(--acc);text-decoration:none;margin-right:14px}a{color:var(--acc)}
details{margin:6px 0}summary{cursor:pointer;color:var(--acc)}
"""


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def _pre(obj) -> str:
    txt = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=2)
    return f"<pre><code>{_e(txt)}</code></pre>"


def _schema_rows(schema: dict) -> str:
    props = (schema or {}).get("properties") or {}
    req = set((schema or {}).get("required") or [])
    rows = []
    for name, p in props.items():
        t = p.get("type")
        t = " | ".join(t) if isinstance(t, list) else (t or "")
        extra = []
        if p.get("enum"):
            extra.append("одно из: " + ", ".join(map(str, p["enum"])))
        if p.get("maxLength"):
            extra.append(f"до {p['maxLength']} символов")
        if p.get("description"):
            extra.append(p["description"])
        rows.append(f"<tr><td><code>{_e(name)}</code>{' *' if name in req else ''}</td>"
                    f"<td>{_e(t)}</td><td>{_e('; '.join(extra))}</td></tr>")
    if not rows:
        return ""
    return "<table><tr><th>Поле</th><th>Тип</th><th>Описание</th></tr>" + "".join(rows) + "</table>"


def _curl(base: str, method: str, path: str, op: dict) -> str:
    url = base.rstrip("/") + path.replace("{op_id}", "1842")
    q = [p for p in op.get("parameters") or [] if p.get("in") == "query" and p.get("required")]
    if q:
        url += "?" + "&".join(f"{p['name']}=…" for p in q)
    lines = [f"curl -X {method.upper()} '{url}'"]
    if op.get("security") != []:
        lines.append("  -H 'X-Api-Key: bm_ваш_ключ'")
    body = ((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}
    if body:
        ex = body.get("example") or {k: "…" for k in (body.get("schema") or {}).get("required") or []}
        lines.append("  -H 'Content-Type: application/json'")
        lines.append(f"  -d '{json.dumps(ex, ensure_ascii=False)}'")
    return " \\\n".join(lines)


def render_html(spec: dict) -> str:
    from services import webhooks as _wh
    base = (spec.get("servers") or [{"url": ""}])[0]["url"] if spec.get("servers") else ""
    out = [f"<h1>{_e(spec['info']['title'])}</h1>",
           f"<p class='muted'>Версия {_e(spec['info']['version'])} · "
           f"<a href='{_e(base)}/api/v1/openapi.json'>openapi.json</a> — импорт в Postman, "
           "Insomnia или генератор клиента</p>",
           "<nav><a href='#auth'>Доступ</a><a href='#endpoints'>Методы</a>"
           "<a href='#webhooks'>Вебхуки</a><a href='#signature'>Подпись</a>"
           "<a href='#events'>События</a><a href='#crm'>CRM</a></nav>"]
    out.append("<h2 id='auth'>Доступ</h2>")
    out.append("<p>Ключ выпускается в мини-аппе: <b>Настройки → Интеграции → «+ Ключ»</b> "
               "(или «+ Только чтение»). Ключ показывается один раз; храните его как пароль. "
               "Передавайте заголовком <code>X-Api-Key: bm_…</code> или "
               "<code>Authorization: Bearer bm_…</code>.</p>")
    out.append("<p>Владелец берётся из ключа — <code>owner_id</code> передавать не нужно. "
               "Чужой <code>owner_id</code> даёт 403.</p>")
    out.append("<table><tr><th>Код</th><th>Когда</th></tr>"
               "<tr><td>401</td><td>Ключа нет, он неверный или отозван</td></tr>"
               "<tr><td>403</td><td>Ключу не хватает области (<code>write</code> у ключа «только "
               "чтение») или <code>owner_id</code> чужой</td></tr>"
               "<tr><td>429</td><td>Превышен лимит запросов в минуту (у каждого метода свой)</td></tr>"
               "<tr><td>400</td><td>Неверные параметры — текст в поле <code>error</code></td></tr>"
               "</table>")
    out.append(f"<p>Базовый адрес: <code>{_e(base or 'https://ваш-сервер')}/api/v1</code></p>")

    out.append("<h2 id='endpoints'>Методы</h2>")
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            out.append("<div class='ep'>")
            out.append(f"<div><span class='m {method}'>{method.upper()}</span>"
                       f"<code>{_e(path)}</code> — {_e(op['summary'])}</div>")
            for para in op["description"].split("\n\n"):
                out.append(f"<p>{_e(para).replace('`', '')}</p>")
            params = op.get("parameters") or []
            if params:
                out.append("<table><tr><th>Параметр</th><th>Где</th><th>Описание</th></tr>")
                for p in params:
                    t = (p.get("schema") or {}).get("type", "")
                    req = " *" if p.get("required") else ""
                    out.append(f"<tr><td><code>{_e(p['name'])}</code>{req}</td>"
                               f"<td>{_e(p['in'])}, {_e(t)}</td>"
                               f"<td>{_e(p.get('description', ''))}</td></tr>")
                out.append("</table>")
            body = ((op.get("requestBody") or {}).get("content") or {}).get("application/json")
            if body:
                out.append("<h3>Тело запроса</h3>" + _schema_rows(body.get("schema") or {}))
            out.append("<details><summary>Пример запроса</summary>"
                       + _pre(_curl(base or "https://ваш-сервер", method, path, op)) + "</details>")
            ok = (op.get("responses") or {}).get("200") or {}
            ex = ((ok.get("content") or {}).get("application/json") or {}).get("example")
            if ex is not None:
                out.append("<details><summary>Пример ответа</summary>" + _pre(ex) + "</details>")
            out.append("</div>")

    out.append("<h2 id='webhooks'>Вебхуки</h2>")
    out.append("<p>Infragram сам сообщает вашей системе о событиях. Получатель добавляется в "
               "<b>Настройки → Интеграции → «+ Вебхук (URL)»</b>: адрес <code>https://</code> и "
               "список событий (ничего не отмечено — все). Каждое событие — "
               "<code>POST</code> с JSON:</p>")
    out.append(_pre(_event_example("operation.done")))
    out.append("<table><tr><th>Заголовок</th><th>Значение</th></tr>"
               "<tr><td><code>X-Infragram-Event</code></td><td>тип события</td></tr>"
               "<tr><td><code>X-Infragram-Event-Id</code></td><td>id события; одинаков у всех "
               "повторов — по нему отбрасывайте дубли</td></tr>"
               "<tr><td><code>X-Infragram-Timestamp</code></td><td>время отправки, unix</td></tr>"
               "<tr><td><code>X-Infragram-Signature</code></td><td><code>t=&lt;unix&gt;,v1=&lt;hex&gt;</code></td></tr>"
               "</table>")
    out.append(f"<p><b>Доставка.</b> Ответ 2xx — доставлено. Иначе повтор через 1, 5, 15 минут, "
               f"1, 3, 6 и 12 часов — всего до {_wh.MAX_ATTEMPTS} попыток. Ответ 410 Gone или "
               f"{_wh.DISABLE_AFTER_FAILURES} неудач подряд выключают получателя (причина видна в "
               f"«Интеграциях»). Таймаут ответа — {_wh._HTTP_TIMEOUT_S} с, переадресации не "
               "выполняются. Доставка «хотя бы один раз»: при сбое сети событие может прийти "
               "дважды с тем же <code>X-Infragram-Event-Id</code>. Журнал доставок и кнопка "
               "«Повторить» — на экране получателя.</p>")
    out.append("<h3 id='signature'>Проверка подписи</h3>")
    out.append("<p><code>v1 = HMAC-SHA256(секрет, t + \".\" + сырое_тело)</code>. Секрет "
               "<code>whsec_…</code> показывается один раз при создании получателя (и при "
               "«Новый секрет»). Проверяйте подпись по <b>сырому</b> телу, до разбора JSON, "
               "и отклоняйте запросы старше 5 минут.</p>")
    out.append("<h3>Python</h3>" + _pre(_VERIFY_PY))
    out.append("<h3>Node.js</h3>" + _pre(_VERIFY_JS))
    out.append("<h3>PHP</h3>" + _pre(_VERIFY_PHP))

    out.append("<h2 id='events'>События</h2>")
    out.append("<table><tr><th>Событие</th><th>Когда</th></tr>")
    for ev, label in _wh.EVENTS.items():
        out.append(f"<tr><td><code><a href='#ev-{_e(ev)}'>{_e(ev)}</a></code></td>"
                   f"<td>{_e(label)}</td></tr>")
    out.append("</table>")
    for ev, label in _wh.EVENTS.items():
        out.append(f"<h3 id='ev-{_e(ev)}'><code>{_e(ev)}</code> — {_e(label)}</h3>")
        out.append(_pre(_event_example(ev)))

    out.append("<h2 id='crm'>CRM: amoCRM и Bitrix24</h2>")
    out.append("<p>Готовые коннекторы подключаются без кода: <b>Интеграции → «+ amoCRM» / "
               "«+ Bitrix24»</b>. Человек из воронки, бота и заказов становится контактом и "
               "сделкой в CRM, этап и сумма приходят сами (соответствие этапов настраивается), "
               "ответы из бота — заметками, оплата переводит сделку в «Успешно». Смена этапа в "
               "CRM возвращается в Infragram: в amoCRM подписка оформляется автоматически, в "
               "Bitrix24 — исходящим вебхуком на событие ONCRMDEALUPDATE с адресом из экрана "
               "подключения. Журнал и повторы — те же, что у вебхуков.</p>")
    title = _e(spec["info"]["title"])
    return ("<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title} — документация</title><style>{_CSS}</style></head>"
            f"<body><main>{''.join(out)}</main></body></html>")
