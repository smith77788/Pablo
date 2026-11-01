"""Google Sheets: выгрузки и лента событий в таблицу владельца.

КАК ПОДКЛЮЧАЕТСЯ. Сервисный аккаунт Google ВЛАДЕЛЬЦА: он создаёт его в Google
Cloud, скачивает JSON-ключ, вставляет в мини-апп («Интеграции» → Google Sheets)
и открывает свою таблицу на почту сервисного аккаунта с правом редактора.
Общий сервисный аккаунт платформы сознательно НЕ используется: таблица, открытая
ему, была бы доступна на запись любому владельцу, знающему её ID.

Ключ хранится зашифрованным (token_vault), наружу не отдаётся никогда — экран
видит только почту сервисного аккаунта.

ЗАВИСИМОСТЕЙ НЕ ДОБАВЛЯЕТ. JWT для OAuth подписывается RS256 через
pycryptodome (он уже стоит ради token_vault), запросы — aiohttp. Библиотеки
Google (google-auth, gspread) тянут десятки пакетов ради трёх вызовов REST.
"""
from __future__ import annotations

import base64
import csv
import datetime as _dt
import io
import json
import logging
import re
import time

log = logging.getLogger(__name__)

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
_API = "https://sheets.googleapis.com/v4/spreadsheets"
_TIMEOUT_S = 30
_CHUNK_ROWS = 2000
EXPORT_MAX_ROWS = 50_000

EVENT_HEADER = ["Время (UTC)", "Событие", "Описание", "Подробности", "ID события", "Код события"]

# Токен доступа живёт час; держим его в памяти процесса, чтобы не подписывать
# JWT на каждую строку ленты. Ключ — (владелец, почта сервисного аккаунта):
# смена ключа владельцем даёт новую почту и, значит, новый токен.
_token_cache: dict[tuple[int, str], tuple[str, float]] = {}

_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]{20,})")
_RAW_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{20,100}$")


class SheetsError(Exception):
    """Ошибка Google Sheets с текстом для владельца (по-русски) и кодом ответа."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def parse_spreadsheet_id(value: str) -> str | None:
    """Ссылка на таблицу или её ID → ID. Чистая функция."""
    v = (value or "").strip()
    m = _ID_RE.search(v)
    if m:
        return m.group(1)
    return v if _RAW_ID_RE.match(v) else None


def spreadsheet_url(spreadsheet_id: str, sheet_gid: int | None = None) -> str:
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
    return url + (f"#gid={sheet_gid}" if sheet_gid is not None else "")


def parse_service_account(raw) -> dict:
    """JSON-ключ сервисного аккаунта → проверенный словарь, иначе ValueError."""
    if isinstance(raw, dict):
        info = raw
    else:
        try:
            info = json.loads((raw or "").strip())
        except ValueError:
            raise ValueError("Это не JSON — вставьте содержимое файла ключа целиком")
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise ValueError("Нужен ключ СЕРВИСНОГО аккаунта (в файле \"type\": \"service_account\")")
    email = str(info.get("client_email") or "").strip()
    key = str(info.get("private_key") or "")
    if not email or "@" not in email:
        raise ValueError("В ключе нет client_email")
    if "PRIVATE KEY" not in key:
        raise ValueError("В ключе нет private_key")
    try:
        from Crypto.PublicKey import RSA
        RSA.import_key(key)
    except Exception:
        raise ValueError("private_key не читается — скачайте ключ заново")
    return {"type": "service_account", "client_email": email, "private_key": key,
            "private_key_id": str(info.get("private_key_id") or ""),
            "project_id": str(info.get("project_id") or "")}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def build_jwt(info: dict, now: int | None = None) -> str:
    """Подписанный RS256 JWT для обмена на токен доступа. Чистая функция."""
    from Crypto.Hash import SHA256
    from Crypto.PublicKey import RSA
    from Crypto.Signature import pkcs1_15
    now = int(now if now is not None else time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    if info.get("private_key_id"):
        header["kid"] = info["private_key_id"]
    claims = {"iss": info["client_email"], "scope": _SCOPE, "aud": _TOKEN_URL,
              "iat": now, "exp": now + 3600}
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode())
                     + "." + _b64url(json.dumps(claims, separators=(",", ":")).encode()))
    key = RSA.import_key(info["private_key"])
    sig = pkcs1_15.new(key).sign(SHA256.new(signing_input.encode()))
    return signing_input + "." + _b64url(sig)


# ── Подключение ──────────────────────────────────────────────────────────────

async def save_connection(pool, owner_id: int, raw) -> dict:
    from services.token_vault import encrypt_token
    info = parse_service_account(raw)
    await pool.execute(
        """INSERT INTO sheets_connections (owner_id, client_email, credentials_enc)
           VALUES ($1, $2, $3)
           ON CONFLICT (owner_id) DO UPDATE
           SET client_email=EXCLUDED.client_email,
               credentials_enc=EXCLUDED.credentials_enc, updated_at=now()""",
        owner_id, info["client_email"], encrypt_token(json.dumps(info)))
    _forget_tokens(owner_id)
    return {"client_email": info["client_email"]}


async def get_connection(pool, owner_id: int) -> dict | None:
    r = await pool.fetchrow(
        "SELECT client_email, created_at, updated_at FROM sheets_connections WHERE owner_id=$1",
        owner_id)
    if not r:
        return None
    return {"client_email": r["client_email"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None}


async def delete_connection(pool, owner_id: int) -> bool:
    _forget_tokens(owner_id)
    res = await pool.execute("DELETE FROM sheets_connections WHERE owner_id=$1", owner_id)
    return res != "DELETE 0"


def _forget_tokens(owner_id: int) -> None:
    for k in [k for k in _token_cache if k[0] == owner_id]:
        _token_cache.pop(k, None)


async def _load_info(pool, owner_id: int) -> dict:
    from services.token_vault import decrypt_token
    enc = await pool.fetchval(
        "SELECT credentials_enc FROM sheets_connections WHERE owner_id=$1", owner_id)
    if not enc:
        raise SheetsError("Google Sheets не подключён")
    try:
        return json.loads(decrypt_token(enc))
    except ValueError:
        raise SheetsError("Ключ сервисного аккаунта не читается — подключите заново")


async def _access_token(http, pool, owner_id: int) -> str:
    info = await _load_info(pool, owner_id)
    ck = (owner_id, info["client_email"])
    hit = _token_cache.get(ck)
    if hit and hit[1] > time.time() + 60:
        return hit[0]
    import aiohttp
    form = {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": build_jwt(info)}
    try:
        async with http.post(_TOKEN_URL, data=form,
                             timeout=aiohttp.ClientTimeout(total=_TIMEOUT_S)) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200 or "access_token" not in (body or {}):
                desc = (body or {}).get("error_description") or (body or {}).get("error") or ""
                raise SheetsError(
                    "Google отклонил ключ сервисного аккаунта"
                    + (f" ({str(desc)[:120]})" if desc else "")
                    + " — проверьте, что ключ не отозван", resp.status)
    except (aiohttp.ClientError, TimeoutError) as e:
        raise SheetsError(f"Google недоступен: {type(e).__name__}")
    token = body["access_token"]
    _token_cache[ck] = (token, time.time() + int(body.get("expires_in") or 3600))
    return token


def _explain(status: int, body: dict, client_email: str = "") -> str:
    msg = ((body or {}).get("error") or {}).get("message") if isinstance(body, dict) else ""
    if status == 403:
        return ("Нет доступа к таблице — откройте её на "
                + (client_email or "почту сервисного аккаунта") + " с правом редактора")
    if status == 404:
        return "Таблица не найдена — проверьте ссылку"
    if status == 429:
        return "Google ограничил частоту запросов — повторим позже"
    return f"Google Sheets ответил {status}" + (f": {str(msg)[:160]}" if msg else "")


async def _call(http, pool, owner_id: int, method: str, url: str, payload=None,
                params=None) -> dict:
    import aiohttp
    token = await _access_token(http, pool, owner_id)
    try:
        async with http.request(method, url, json=payload, params=params,
                                headers={"Authorization": f"Bearer {token}"},
                                timeout=aiohttp.ClientTimeout(total=_TIMEOUT_S)) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 300:
                if resp.status == 401:
                    _forget_tokens(owner_id)
                email = ""
                try:
                    email = (await get_connection(pool, owner_id) or {}).get("client_email", "")
                except Exception:
                    email = ""
                raise SheetsError(_explain(resp.status, body, email), resp.status)
            return body or {}
    except (aiohttp.ClientError, TimeoutError) as e:
        raise SheetsError(f"Google недоступен: {type(e).__name__}")


def _q(sheet: str) -> str:
    """Имя листа для A1-нотации: в кавычках, кавычка удваивается."""
    return "'" + sheet.replace("'", "''") + "'"


async def _sheet_ids(http, pool, owner_id: int, sid: str) -> dict[str, int]:
    meta = await _call(http, pool, owner_id, "GET", f"{_API}/{sid}",
                       params={"fields": "sheets.properties(sheetId,title)"})
    return {s["properties"]["title"]: s["properties"]["sheetId"]
            for s in meta.get("sheets", [])}


async def _add_sheet(http, pool, owner_id: int, sid: str, title: str) -> int:
    res = await _call(http, pool, owner_id, "POST", f"{_API}/{sid}:batchUpdate",
                      {"requests": [{"addSheet": {"properties": {"title": title}}}]})
    return res["replies"][0]["addSheet"]["properties"]["sheetId"]


async def _append(http, pool, owner_id: int, sid: str, sheet: str, rows: list[list]) -> None:
    for i in range(0, len(rows), _CHUNK_ROWS):
        await _call(http, pool, owner_id, "POST",
                    f"{_API}/{sid}/values/{_q(sheet)}!A1:append",
                    {"values": rows[i:i + _CHUNK_ROWS]},
                    params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"})


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    if isinstance(v, (bool, int, float, str)):
        return v
    return str(v)


# ── Лента событий ────────────────────────────────────────────────────────────

def event_row(event: str, event_id: int, created_at, summary: str, data: dict) -> list:
    when = created_at
    if isinstance(when, _dt.datetime):
        when = when.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    from services.webhooks import EVENTS
    return [_cell(when), EVENTS.get(event, event), summary or "",
            json.dumps(data or {}, ensure_ascii=False, default=str)[:45000],
            event_id, event]


async def append_event_rows(pool, owner_id: int, spreadsheet_id: str, sheet_name: str,
                            rows: list[list]) -> tuple[bool, int | None, str]:
    """Дописать строки событий; лист создаётся с заголовком при первой записи.
    (успех, код ответа Google, текст ошибки)."""
    import aiohttp
    try:
        async with aiohttp.ClientSession(trust_env=True) as http:
            sheets = await _sheet_ids(http, pool, owner_id, spreadsheet_id)
            if sheet_name not in sheets:
                await _add_sheet(http, pool, owner_id, spreadsheet_id, sheet_name)
                rows = [EVENT_HEADER] + rows
            await _append(http, pool, owner_id, spreadsheet_id, sheet_name, rows)
        return True, 200, ""
    except SheetsError as e:
        return False, e.status, str(e)


# ── Выгрузки ─────────────────────────────────────────────────────────────────

DATASETS = {
    "accounts": "Аккаунты",
    "operations": "Операции",
    "audience": "Собранная аудитория",
    "contacts": "Контакты",
}


async def collect_dataset(pool, owner_id: int, name: str) -> tuple[list, list[list]]:
    """(заголовок, строки) выгрузки — то же, что отдают CSV-выгрузки экранов."""
    if name == "accounts":
        rows = await pool.fetch(
            """SELECT id, phone, first_name, username, tg_user_id, is_active,
                      ROUND(COALESCE(trust_score, 1.0) * 100) AS trust_score,
                      COALESCE(acc_status, 'ok') AS acc_status, cooldown_until,
                      last_used, added_at
               FROM tg_accounts WHERE owner_id=$1
               ORDER BY is_active DESC, last_used DESC NULLS LAST LIMIT $2""",
            owner_id, EXPORT_MAX_ROWS)
        header = ["ID", "Телефон", "Имя", "Username", "Telegram ID", "Активен",
                  "Доверие, %", "Статус", "Остывает до", "Последнее использование",
                  "Добавлен"]
        return header, [[_cell(v) for v in r.values()] for r in rows]
    if name == "operations":
        rows = await pool.fetch(
            """SELECT id, op_type, label, status, total_items, done_items,
                      COALESCE(retry_count, 0) AS retries, error_msg, created_at, finished_at
               FROM operation_queue WHERE owner_id=$1
               ORDER BY id DESC LIMIT $2""", owner_id, EXPORT_MAX_ROWS)
        from services import operation_bus as _bus
        header = ["ID", "Тип", "Метка", "Статус", "Всего", "Сделано", "Повторов",
                  "Ошибка", "Создана", "Завершена"]
        out = []
        for r in rows:
            d = dict(r)
            d["op_type"] = _bus.describe(d["op_type"])
            out.append([_cell(v) for v in d.values()])
        return header, out
    if name == "audience":
        rows = await pool.fetch(
            """SELECT tg_user_id, username, first_name, last_name, phone, is_premium,
                      is_bot, is_active, source_title, parsed_at
               FROM parsed_audiences WHERE owner_id=$1
               ORDER BY parsed_at DESC LIMIT $2""", owner_id, EXPORT_MAX_ROWS)
        header = ["Telegram ID", "Username", "Имя", "Фамилия", "Телефон", "Premium",
                  "Бот", "Активен", "Источник", "Собран"]
        return header, [[_cell(v) for v in r.values()] for r in rows]
    if name == "contacts":
        from services.contacts_hub import export_engine as _ex
        text = await _ex.export_csv(pool, owner_id, None)
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        table = list(csv.reader(io.StringIO(text)))
        if not table:
            return [], []
        return table[0], table[1:EXPORT_MAX_ROWS + 1]
    raise ValueError("Неизвестная выгрузка")


async def export_dataset(pool, owner_id: int, spreadsheet: str, name: str) -> dict:
    """Выгрузить набор на НОВЫЙ лист таблицы. Возвращает ссылку на лист.

    Новый лист, а не перезапись: таблица владельца — его рабочий документ, и
    молча затереть лист, на который ссылаются его формулы, хуже лишней вкладки.
    """
    sid = parse_spreadsheet_id(spreadsheet)
    if not sid:
        raise ValueError("Вставьте ссылку на таблицу Google или её ID")
    if name not in DATASETS:
        raise ValueError("Неизвестная выгрузка")
    header, rows = await collect_dataset(pool, owner_id, name)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    title = f"{DATASETS[name]} {stamp}"
    import aiohttp
    async with aiohttp.ClientSession(trust_env=True) as http:
        existing = await _sheet_ids(http, pool, owner_id, sid)
        n = 2
        base = title
        while title in existing:
            title = f"{base} ({n})"
            n += 1
        gid = await _add_sheet(http, pool, owner_id, sid, title)
        await _append(http, pool, owner_id, sid, title, [header] + rows)
    return {"rows": len(rows), "sheet": title, "url": spreadsheet_url(sid, gid),
            "truncated": len(rows) >= EXPORT_MAX_ROWS}


async def check_access(pool, owner_id: int, spreadsheet: str) -> dict:
    """Проверить, что сервисный аккаунт видит таблицу. Для кнопки «Проверить»."""
    sid = parse_spreadsheet_id(spreadsheet)
    if not sid:
        raise ValueError("Вставьте ссылку на таблицу Google или её ID")
    import aiohttp
    async with aiohttp.ClientSession(trust_env=True) as http:
        sheets = await _sheet_ids(http, pool, owner_id, sid)
    return {"ok": True, "sheets": sorted(sheets), "url": spreadsheet_url(sid)}
