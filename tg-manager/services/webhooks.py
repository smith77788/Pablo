"""Исходящие вебхуки: события Infragram → система владельца (n8n, Zapier, Make,
свой сервер) или строка в Google Sheets.

КАК УСТРОЕНО.
  1. Событие пишется в `integration_events` ТРИГГЕРОМ базы (schema_v262):
     конец операции, новый аккаунт, потеря аккаунта, ограничение от Telegram.
     Триггер, а не вызов из кода, потому что операция закрывается в десятке
     мест op_worker'а, и хук в одном месте молча терял бы остальные. Код
     может добавить своё событие через `emit` (например, тестовое).
  2. Цикл `run` раскладывает каждое событие по подписанным получателям
     (`webhook_deliveries`, ровно одна строка на пару событие × получатель —
     уникальный ключ) и доставляет то, чему пришёл срок.
  3. Доставка — «хотя бы один раз»: строка занимается арендой
     (next_attempt_at сдвигается вперёд ДО отправки), процесс, умерший
     посреди отправки, повторит её после аренды. Получатель отличает повтор
     по заголовку X-Infragram-Event-Id — он одинаков у всех попыток.
  4. Неудача — повтор с растущей паузой (1 мин … 12 ч), восемь попыток. Подряд
     50 неудач или ответ 410 Gone — получатель выключается с причиной, которую
     владелец видит на экране «Интеграции».

ПОДПИСЬ. `X-Infragram-Signature: t=<unix>,v1=<hex>` где
v1 = HMAC-SHA256(secret, f"{t}.{тело}"). Метка времени внутри подписи не даёт
переиграть старый запрос. Секрет показывается владельцу один раз (при создании
и при перевыпуске) и хранится зашифрованным (token_vault).

SSRF. Адрес получателя проверяется при сохранении И перед каждой отправкой
(`security.resolve_url_is_public`: только https, ни одного внутреннего адреса
в DNS), редиректы не выполняются — иначе вебхук стал бы способом дотянуться
до внутренней сети платформы.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import hmac
import json
import logging
import secrets
import time

log = logging.getLogger(__name__)

# ── Каталог событий ──────────────────────────────────────────────────────────
# Подпись — то, что видит владелец на экране выбора событий. Источник каждого
# события (кроме webhook.test) — триггер в schema_v262_integrations.sql;
# сверку держит tests/test_integrations_webhooks.py.
EVENTS: dict[str, str] = {
    "operation.done": "Операция выполнена",
    "operation.partial": "Операция выполнена частично",
    "operation.failed": "Операция провалилась",
    "operation.cancelled": "Операция отменена",
    "account.added": "Добавлен аккаунт",
    "account.lost": "Аккаунт потерян (бан, спамблок, заморозка, сессия)",
    "account.status_changed": "Статус аккаунта изменился",
    "account.restricted": "Ограничение от Telegram",
    "webhook.test": "Тестовое событие",
}

KINDS = ("http", "sheets")
MAX_ENDPOINTS = 20
MAX_ATTEMPTS = 8
# Пауза перед попыткой N+1 после неудачной попытки N (секунды).
_BACKOFF_S = (60, 300, 900, 3600, 3 * 3600, 6 * 3600, 12 * 3600)
DISABLE_AFTER_FAILURES = 50
# Аренда строки на время отправки: дольше таймаута запроса с запасом.
_LEASE_S = 120
_HTTP_TIMEOUT_S = 10
_CLAIM_BATCH = 50
_FANOUT_BATCH = 500
_CONCURRENCY = 10
RETENTION_DAYS = 14
_USER_AGENT = "Infragram-Webhooks/1.0"


def backoff_seconds(attempts: int) -> int:
    """Пауза после `attempts` неудачных попыток. Чистая функция."""
    if attempts < 1:
        return _BACKOFF_S[0]
    return _BACKOFF_S[min(attempts - 1, len(_BACKOFF_S) - 1)]


def normalize_events(events) -> list[str]:
    """Ввод владельца → список известных событий. Пусто = подписка на всё."""
    if isinstance(events, str):
        events = [e.strip() for e in events.split(",")]
    got = []
    for e in events or []:
        e = str(e or "").strip().lower()
        if e in EVENTS and e != "webhook.test" and e not in got:
            got.append(e)
    return got


def generate_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(24)


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """Значение заголовка X-Infragram-Signature. Чистая функция."""
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode() + body,
                   hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def verify(secret: str, header: str, body: bytes, *, tolerance_s: int = 300,
           now: float | None = None) -> bool:
    """Проверка подписи — образец для получателя (и для тестов)."""
    try:
        parts = dict(p.split("=", 1) for p in (header or "").split(","))
        ts = int(parts["t"])
        got = parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs((now or time.time()) - ts) > tolerance_s:
        return False
    want = sign(secret, ts, body).split("v1=", 1)[1]
    return hmac.compare_digest(got.encode(), want.encode())


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def build_body(event: str, event_id: int, created_at, data: dict) -> bytes:
    """Тело запроса. Ключи стабильные — на них строят сценарии в n8n/Zapier."""
    return json.dumps({
        "id": event_id,
        "event": event,
        "created_at": _iso(created_at),
        "data": data or {},
    }, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")


def summarize(event: str, data: dict) -> str:
    """Человеческая строка о событии — для таблицы и для экрана журнала."""
    d = data or {}
    label = EVENTS.get(event, event)
    if event.startswith("operation."):
        try:
            from services import operation_bus as _bus
            kind = _bus.describe(d.get("op_type") or "")
        except Exception:
            kind = d.get("op_type") or ""
        done, total = d.get("done_items") or 0, d.get("total_items") or 0
        tail = f": {done}/{total}" if total else ""
        err = f" — {d['error']}" if d.get("error") else ""
        return f"{label} #{d.get('operation_id')} ({kind}){tail}{err}"[:500]
    if event.startswith("account."):
        who = d.get("phone") or (("@" + d["username"]) if d.get("username") else "")
        who = who or f"#{d.get('account_id')}"
        if event in ("account.lost", "account.status_changed"):
            return f"{label}: {who} {d.get('old_status') or '—'} → {d.get('new_status')}"
        if event == "account.restricted":
            return f"{label}: {who} ({d.get('kind')}, {d.get('severity')})"
        return f"{label}: {who}"
    return label


# ── Хранилище получателей ────────────────────────────────────────────────────

def _endpoint_public(r) -> dict:
    return {
        "id": r["id"],
        "kind": r["kind"],
        "name": r["name"] or "",
        "url": r["url"] or "",
        "spreadsheet_id": r["spreadsheet_id"] or "",
        "sheet_name": r["sheet_name"] or "",
        "events": list(r["events"] or []),
        "is_active": bool(r["is_active"]),
        "disabled_reason": r["disabled_reason"] or "",
        "consecutive_failures": int(r["consecutive_failures"] or 0),
        "last_success_at": _iso(r["last_success_at"]),
        "last_failure_at": _iso(r["last_failure_at"]),
        "last_error": r["last_error"] or "",
        "created_at": _iso(r["created_at"]),
    }


_ENDPOINT_COLS = """id, kind, name, url, spreadsheet_id, sheet_name, events,
    is_active, disabled_reason, consecutive_failures, last_success_at,
    last_failure_at, last_error, created_at"""


async def list_endpoints(pool, owner_id: int) -> list[dict]:
    rows = await pool.fetch(
        f"SELECT {_ENDPOINT_COLS} FROM webhook_endpoints WHERE owner_id=$1 "
        "ORDER BY id DESC LIMIT 100", owner_id)
    return [_endpoint_public(r) for r in rows]


async def get_endpoint(pool, owner_id: int, endpoint_id: int) -> dict | None:
    r = await pool.fetchrow(
        f"SELECT {_ENDPOINT_COLS} FROM webhook_endpoints WHERE id=$1 AND owner_id=$2",
        endpoint_id, owner_id)
    return _endpoint_public(r) if r else None


async def validate_http_url(url: str) -> str:
    """Адрес получателя → нормализованный, либо ValueError с русским текстом."""
    from services.security import is_safe_public_url, resolve_url_is_public
    url = (url or "").strip()
    if not url:
        raise ValueError("Укажите адрес, на который слать события")
    if len(url) > 2000:
        raise ValueError("Адрес слишком длинный")
    if not is_safe_public_url(url):
        raise ValueError("Нужен публичный адрес https:// — внутренние адреса и http запрещены")
    if not await resolve_url_is_public(url):
        raise ValueError("Адрес не открывается из интернета или ведёт во внутреннюю сеть")
    return url


def validate_sheet_target(spreadsheet: str, sheet_name: str) -> tuple[str, str]:
    from services import google_sheets as _gs
    sid = _gs.parse_spreadsheet_id(spreadsheet)
    if not sid:
        raise ValueError("Вставьте ссылку на таблицу Google или её ID")
    name = (sheet_name or "").strip()[:100] or "События Infragram"
    return sid, name


async def create_endpoint(pool, owner_id: int, *, kind: str = "http", name: str = "",
                          url: str = "", spreadsheet: str = "", sheet_name: str = "",
                          events=None) -> dict:
    """Новый получатель. Для http возвращает и секрет — показать ОДИН раз."""
    kind = (kind or "http").strip().lower()
    if kind not in KINDS:
        raise ValueError("Неизвестный тип получателя")
    n = int(await pool.fetchval(
        "SELECT COUNT(*) FROM webhook_endpoints WHERE owner_id=$1", owner_id) or 0)
    if n >= MAX_ENDPOINTS:
        raise ValueError(f"Получателей уже {MAX_ENDPOINTS} — удалите ненужный")
    evs = normalize_events(events)
    name = (name or "").strip()[:64]
    secret = None
    if kind == "http":
        url = await validate_http_url(url)
        secret = generate_secret()
        from services.token_vault import encrypt_token
        row = await pool.fetchrow(
            """INSERT INTO webhook_endpoints (owner_id, kind, name, url, secret_enc, events)
               VALUES ($1, 'http', $2, $3, $4, $5::text[]) RETURNING id""",
            owner_id, name, url, encrypt_token(secret), evs)
    else:
        from services import google_sheets as _gs
        if not await _gs.get_connection(pool, owner_id):
            raise ValueError("Сначала подключите Google Sheets (сервисный аккаунт)")
        sid, sname = validate_sheet_target(spreadsheet, sheet_name)
        row = await pool.fetchrow(
            """INSERT INTO webhook_endpoints
                   (owner_id, kind, name, spreadsheet_id, sheet_name, events)
               VALUES ($1, 'sheets', $2, $3, $4, $5::text[]) RETURNING id""",
            owner_id, name, sid, sname, evs)
    out = await get_endpoint(pool, owner_id, row["id"])
    if secret:
        out["secret"] = secret
    return out


async def update_endpoint(pool, owner_id: int, endpoint_id: int, *,
                          is_active=None, events=None, name=None) -> dict | None:
    cur = await get_endpoint(pool, owner_id, endpoint_id)
    if not cur:
        return None
    new_events = normalize_events(events) if events is not None else cur["events"]
    new_name = (str(name).strip()[:64] if name is not None else cur["name"])
    new_active = bool(is_active) if is_active is not None else cur["is_active"]
    # Включение заново обнуляет серию неудач и причину выключения: иначе
    # первый же сбой после включения снова выключил бы получателя.
    await pool.execute(
        """UPDATE webhook_endpoints
           SET events=$3::text[], name=$4, is_active=$5,
               consecutive_failures = CASE WHEN $5 AND NOT is_active THEN 0
                                           ELSE consecutive_failures END,
               disabled_reason = CASE WHEN $5 THEN NULL ELSE disabled_reason END
           WHERE id=$1 AND owner_id=$2""",
        endpoint_id, owner_id, new_events, new_name, new_active)
    return await get_endpoint(pool, owner_id, endpoint_id)


async def rotate_secret(pool, owner_id: int, endpoint_id: int) -> str | None:
    from services.token_vault import encrypt_token
    secret = generate_secret()
    res = await pool.execute(
        "UPDATE webhook_endpoints SET secret_enc=$3 WHERE id=$1 AND owner_id=$2 AND kind='http'",
        endpoint_id, owner_id, encrypt_token(secret))
    return secret if res != "UPDATE 0" else None


async def delete_endpoint(pool, owner_id: int, endpoint_id: int) -> bool:
    res = await pool.execute(
        "DELETE FROM webhook_endpoints WHERE id=$1 AND owner_id=$2",
        endpoint_id, owner_id)
    return res != "DELETE 0"


async def list_deliveries(pool, owner_id: int, endpoint_id: int, limit: int = 50) -> list[dict]:
    rows = await pool.fetch(
        """SELECT d.id, d.event, d.status, d.attempts, d.last_status_code, d.last_error,
                  d.created_at, d.delivered_at, d.next_attempt_at, ev.payload
           FROM webhook_deliveries d
           JOIN integration_events ev ON ev.id = d.event_id
           WHERE d.endpoint_id=$1 AND d.owner_id=$2
           ORDER BY d.id DESC LIMIT $3""",
        endpoint_id, owner_id, limit)
    out = []
    for r in rows:
        payload = r["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        out.append({
            "id": r["id"], "event": r["event"],
            "event_label": EVENTS.get(r["event"], r["event"]),
            "summary": summarize(r["event"], payload or {}),
            "status": r["status"], "attempts": r["attempts"],
            "status_code": r["last_status_code"], "error": r["last_error"] or "",
            "created_at": _iso(r["created_at"]), "delivered_at": _iso(r["delivered_at"]),
            "next_attempt_at": _iso(r["next_attempt_at"]) if r["status"] == "pending" else None,
        })
    return out


async def redeliver(pool, owner_id: int, delivery_id: int) -> bool:
    """Повторить доставку (из журнала) — сразу, с новым счётом попыток."""
    res = await pool.execute(
        """UPDATE webhook_deliveries
           SET status='pending', attempts=0, next_attempt_at=now(), last_error=NULL
           WHERE id=$1 AND owner_id=$2 AND status <> 'pending'""",
        delivery_id, owner_id)
    return res != "UPDATE 0"


async def emit(pool, owner_id: int, event: str, data: dict) -> int | None:
    """Записать событие из кода. Никогда не бросает; None — не записано.

    Как и у триггеров, строка появляется только при активном получателе.
    """
    try:
        return await pool.fetchval(
            """INSERT INTO integration_events (owner_id, event, payload)
               SELECT $1, $2, $3::jsonb
               WHERE EXISTS (SELECT 1 FROM webhook_endpoints
                             WHERE owner_id=$1 AND is_active)
               RETURNING id""",
            owner_id, event, json.dumps(data or {}, ensure_ascii=False, default=str))
    except Exception as e:
        log.warning("webhooks.emit %s owner=%s не записано: %s", event, owner_id,
                    type(e).__name__)
        return None


# ── Доставка ─────────────────────────────────────────────────────────────────

async def fan_out(pool) -> int:
    """События → доставки подписанным получателям. Одним запросом: занятые
    строки событий (SKIP LOCKED) раскладываются и отмечаются атомарно."""
    return int(await pool.fetchval(
        f"""WITH ev AS (
                SELECT id, owner_id, event FROM integration_events
                WHERE fanned_out_at IS NULL
                ORDER BY id LIMIT {_FANOUT_BATCH}
                FOR UPDATE SKIP LOCKED
            ),
            ins AS (
                INSERT INTO webhook_deliveries (endpoint_id, event_id, owner_id, event)
                SELECT e.id, ev.id, ev.owner_id, ev.event
                FROM ev JOIN webhook_endpoints e
                  ON e.owner_id = ev.owner_id AND e.is_active
                 AND (cardinality(e.events) = 0 OR ev.event = ANY(e.events))
                ON CONFLICT (endpoint_id, event_id) DO NOTHING
                RETURNING 1
            ),
            upd AS (
                UPDATE integration_events SET fanned_out_at = now()
                WHERE id IN (SELECT id FROM ev)
                RETURNING 1
            )
            SELECT (SELECT COUNT(*) FROM ins)""") or 0)


async def _claim_due(pool) -> list[dict]:
    rows = await pool.fetch(
        f"""WITH due AS (
                SELECT id FROM webhook_deliveries
                WHERE status='pending' AND next_attempt_at <= now()
                ORDER BY next_attempt_at
                LIMIT {_CLAIM_BATCH}
                FOR UPDATE SKIP LOCKED
            )
            UPDATE webhook_deliveries d
            SET attempts = d.attempts + 1,
                next_attempt_at = now() + make_interval(secs => {_LEASE_S})
            FROM due WHERE d.id = due.id
            RETURNING d.id, d.endpoint_id, d.event_id, d.owner_id, d.event, d.attempts""")
    if not rows:
        return []
    ids = [r["event_id"] for r in rows]
    eps = [r["endpoint_id"] for r in rows]
    evs = {r["id"]: r for r in await pool.fetch(
        "SELECT id, payload, created_at FROM integration_events WHERE id = ANY($1::bigint[])",
        ids)}
    ends = {r["id"]: r for r in await pool.fetch(
        """SELECT id, owner_id, kind, url, secret_enc, spreadsheet_id, sheet_name, is_active
           FROM webhook_endpoints WHERE id = ANY($1::bigint[])""", eps)}
    out = []
    for r in rows:
        ev, ep = evs.get(r["event_id"]), ends.get(r["endpoint_id"])
        payload = ev["payload"] if ev else {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        out.append({**dict(r), "payload": payload or {},
                    "created_at": ev["created_at"] if ev else None,
                    "endpoint": dict(ep) if ep else None})
    return out


async def send_http(http, endpoint: dict, event: str, event_id: int, created_at,
                    data: dict) -> tuple[bool, int | None, str]:
    """Одна попытка POST. (успех, код ответа, текст ошибки по-русски)."""
    from services.security import resolve_url_is_public
    from services.token_vault import decrypt_token
    import aiohttp
    url = endpoint.get("url") or ""
    if not await resolve_url_is_public(url):
        return False, None, "адрес ведёт во внутреннюю сеть или не резолвится"
    secret = decrypt_token(endpoint.get("secret_enc") or "")
    body = build_body(event, event_id, created_at, data)
    ts = int(time.time())
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": _USER_AGENT,
        "X-Infragram-Event": event,
        "X-Infragram-Event-Id": str(event_id),
        "X-Infragram-Timestamp": str(ts),
        "X-Infragram-Signature": sign(secret, ts, body),
    }
    try:
        async with http.post(url, data=body, headers=headers, allow_redirects=False,
                             timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)) as resp:
            code = resp.status
            if 200 <= code < 300:
                return True, code, ""
            snippet = ""
            try:
                snippet = (await resp.content.read(300)).decode("utf-8", "replace").strip()
            except Exception:
                snippet = ""
            if 300 <= code < 400:
                return False, code, "получатель ответил переадресацией — укажите конечный адрес"
            return False, code, f"ответ {code}" + (f": {snippet[:200]}" if snippet else "")
    except asyncio.TimeoutError:
        return False, None, f"нет ответа за {_HTTP_TIMEOUT_S} с"
    except aiohttp.ClientError as e:
        return False, None, f"сетевая ошибка: {type(e).__name__}"


async def _record(pool, item: dict, ok: bool, code: int | None, err: str) -> None:
    did, ep_id = item["id"], item["endpoint_id"]
    if ok:
        await pool.execute(
            """UPDATE webhook_deliveries SET status='delivered', delivered_at=now(),
                   last_status_code=$2, last_error=NULL WHERE id=$1""", did, code)
        await pool.execute(
            """UPDATE webhook_endpoints SET consecutive_failures=0, last_success_at=now()
               WHERE id=$1""", ep_id)
        return
    attempts = int(item["attempts"])
    final = attempts >= MAX_ATTEMPTS
    await pool.execute(
        """UPDATE webhook_deliveries
           SET status=$2, last_status_code=$3, last_error=$4,
               next_attempt_at = now() + make_interval(secs => $5)
           WHERE id=$1""",
        did, "failed" if final else "pending", code, (err or "")[:500],
        float(backoff_seconds(attempts)))
    gone = code == 410
    await pool.execute(
        """UPDATE webhook_endpoints
           SET consecutive_failures = consecutive_failures + 1,
               last_failure_at = now(), last_error = $2,
               is_active = CASE WHEN $3 OR consecutive_failures + 1 >= $4
                                THEN FALSE ELSE is_active END,
               disabled_reason = CASE
                   WHEN $3 THEN 'Получатель ответил 410 Gone — адрес больше не принимает события'
                   WHEN consecutive_failures + 1 >= $4
                       THEN 'Выключен после ' || $4::text || ' неудачных доставок подряд'
                   ELSE disabled_reason END
           WHERE id=$1""",
        ep_id, (err or "")[:500], gone, DISABLE_AFTER_FAILURES)


async def _deliver_sheets(pool, items: list[dict]) -> None:
    """Строки одного листа — одним запросом: у Sheets лимит на число запросов."""
    from services import google_sheets as _gs
    groups: dict[int, list[dict]] = {}
    for it in items:
        groups.setdefault(it["endpoint_id"], []).append(it)
    for _ep_id, group in groups.items():
        ep = group[0]["endpoint"]
        rows = [_gs.event_row(it["event"], it["event_id"], it["created_at"],
                              summarize(it["event"], it["payload"]), it["payload"])
                for it in group]
        ok, code, err = await _gs.append_event_rows(
            pool, int(ep["owner_id"]), ep["spreadsheet_id"], ep["sheet_name"], rows)
        for it in group:
            await _record(pool, it, ok, code, err)


async def deliver_due(pool, http) -> int:
    items = await _claim_due(pool)
    if not items:
        return 0
    live, sheets = [], []
    for it in items:
        ep = it["endpoint"]
        if not ep or not ep["is_active"]:
            await pool.execute(
                "UPDATE webhook_deliveries SET status='skipped', last_error=$2 WHERE id=$1",
                it["id"], "получатель выключен")
            continue
        (sheets if ep["kind"] == "sheets" else live).append(it)
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _one(it):
        async with sem:
            try:
                ok, code, err = await send_http(http, it["endpoint"], it["event"],
                                                it["event_id"], it["created_at"],
                                                it["payload"])
            except Exception as e:  # доставка одной строки не валит пачку
                ok, code, err = False, None, f"внутренняя ошибка: {type(e).__name__}"
            await _record(pool, it, ok, code, err)

    await asyncio.gather(*(_one(it) for it in live))
    if sheets:
        await _deliver_sheets(pool, sheets)
    return len(items)


async def send_test(pool, http, owner_id: int, endpoint_id: int) -> dict:
    """Тестовое событие одному получателю — сразу, с ответом на экран."""
    r = await pool.fetchrow(
        """SELECT id, owner_id, kind, url, secret_enc, spreadsheet_id, sheet_name
           FROM webhook_endpoints WHERE id=$1 AND owner_id=$2""", endpoint_id, owner_id)
    if not r:
        return {"ok": False, "error": "Получатель не найден"}
    now = _dt.datetime.now(_dt.timezone.utc)
    data = {"message": "Проверка связи с Infragram", "endpoint_id": endpoint_id}
    if r["kind"] == "sheets":
        from services import google_sheets as _gs
        ok, code, err = await _gs.append_event_rows(
            pool, owner_id, r["spreadsheet_id"], r["sheet_name"],
            [_gs.event_row("webhook.test", 0, now, EVENTS["webhook.test"], data)])
    else:
        ok, code, err = await send_http(http, dict(r), "webhook.test", 0, now, data)
    return {"ok": ok, "status_code": code, "error": err}


async def prune(pool) -> None:
    """Старые события (вместе с их доставками — каскадом) — через RETENTION_DAYS."""
    await pool.execute(
        """DELETE FROM integration_events
           WHERE id IN (SELECT id FROM integration_events
                        WHERE created_at < now() - make_interval(days => $1)
                          AND fanned_out_at IS NOT NULL
                          AND NOT EXISTS (SELECT 1 FROM webhook_deliveries d
                                          WHERE d.event_id = integration_events.id
                                            AND d.status = 'pending')
                        LIMIT 5000)""",
        RETENTION_DAYS)


async def run(pool) -> None:
    """Фоновый цикл доставки. Сбой круга — лог и следующий круг."""
    import aiohttp
    last_prune = 0.0
    async with aiohttp.ClientSession(trust_env=True) as http:
        while True:
            busy = False
            try:
                await fan_out(pool)
                busy = (await deliver_due(pool, http)) > 0
                if time.monotonic() - last_prune > 3600:
                    last_prune = time.monotonic()
                    await prune(pool)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("webhooks: круг доставки упал")
            await asyncio.sleep(1 if busy else 5)
