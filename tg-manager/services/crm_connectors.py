"""Коннекторы amoCRM и Bitrix24: воронка Infragram ↔ сделки в CRM владельца.

ЧТО ЕДЕТ В CRM. Подключение — получатель событий (`webhook_endpoints`, kind
'amocrm' | 'bitrix24'), поэтому очередь, повторы, журнал доставок и
выключение после серии сбоев у него те же, что у вебхука. Обработчик события:

  pipeline.updated   контакт воронки (экран контакта, канбан, сенсор, бот)
                     → контакт + сделка в CRM, этап и сумма по соответствию;
  deal.updated       простая сделка (crm_deals: сценарий, бот) → сделка;
  contact.started    человек открыл бота → контакт + сделка на первом этапе;
  contact.field_set  ответ на вопрос сценария → телефон/почта в контакт,
                     остальное — заметкой;
  order.created      заказ ИИ-продавца → сделка «Заказ №…» с суммой и составом;
  payment.received   оплата → сделка в «Успешно», заметка с суммой.

Какая запись у нас стала какой записью в CRM — `crm_links`. Без неё каждое
событие по человеку заводило бы в CRM нового.

ЧТО ЕДЕТ ОБРАТНО. Смена этапа сделки в CRM приходит входящим вебхуком на
`/webhook/crm/{inbound_token}` (`handle_inbound`). Телу запроса мы не верим:
по id сделки этап и сумма перечитываются из самой CRM, а Bitrix24 ещё и
сверяет application_token. Новый этап пишется той же дверью, что и экран
контакта (`crm_engine.upsert_crm`), — второй путь записи стадии означал бы
второй набор правил.

ЭХО. Смена этапа из CRM порождает у нас pipeline.updated, который поехал бы
обратно в CRM. Поэтому в `crm_links` хранится этап и сумма, о которых стороны
уже договорились: событие с тем же этапом и суммой в CRM не отправляется.

ДОСТУПЫ. amoCRM — долгосрочный токен (Настройки → Интеграции → «Создать
интеграцию» → «Долгосрочный токен»), адрес только *.amocrm.ru|com /
*.kommo.com. Bitrix24 — входящий вебхук (адрес с секретом внутри) с правом
crm. Оба секрета хранятся зашифрованно (token_vault) и наружу не отдаются:
на экране — только домен.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

log = logging.getLogger(__name__)

CRM_KINDS = ("amocrm", "bitrix24")
CRM_LABELS = {"amocrm": "amoCRM", "bitrix24": "Bitrix24"}

# События, на которые CRM подписывается при подключении. Остальное (операции,
# аккаунты) к продажам отношения не имеет и в CRM было бы шумом.
DEFAULT_EVENTS = ["pipeline.updated", "deal.updated", "contact.started",
                  "contact.field_set", "order.created", "payment.received"]

# Этапы, которые сопоставляются с этапами CRM, — этапы воронки контактов
# (crm_engine.STAGE_LABELS). У простых сделок (crm_deals) в базе живут ДВА
# словаря: старый (new/contacted/qualified — schema_v90, бот, сценарии) и
# новый, совпадающий с воронкой (CHECK из inline-миграции mini_app_api).
# Читаем оба, пишем тем, которым записана сама строка.
STAGES = ("lead", "contact", "proposal", "negotiation", "won", "lost")
DEAL_TO_STAGE = {"new": "lead", "contacted": "contact", "qualified": "proposal",
                 "won": "won", "lost": "lost",
                 **{st: st for st in ("lead", "contact", "proposal", "negotiation")}}
STAGE_TO_DEAL = {"lead": "new", "contact": "contacted", "proposal": "qualified",
                 "negotiation": "qualified", "won": "won", "lost": "lost"}

_TIMEOUT_S = 15
_TAG = "Infragram"
_MAX_INBOUND_IDS = 20
_AMO_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\.(amocrm\.ru|amocrm\.com|kommo\.com)$")
_BITRIX_PATH_RE = re.compile(r"^/rest/\d+/[A-Za-z0-9_]+/?$")


class CrmError(Exception):
    """Отказ CRM. `message` — по-русски, его видит владелец в журнале."""

    def __init__(self, message: str, code: int | None = None, *, not_found: bool = False):
        super().__init__(message)
        self.message = message
        self.code = code
        self.not_found = not_found


# ── Разбор ввода владельца ───────────────────────────────────────────────────

def parse_amo_domain(raw: str) -> str:
    """«mycompany», «mycompany.amocrm.ru», ссылка на аккаунт → домен.
    ValueError с текстом для экрана, если это не amoCRM/Kommo."""
    s = (raw or "").strip().lower()
    if not s:
        raise ValueError("Укажите адрес amoCRM, например mycompany.amocrm.ru")
    if "://" in s:
        s = urlparse(s).hostname or ""
    s = s.split("/")[0].strip(".")
    if "." not in s:
        s += ".amocrm.ru"
    if not _AMO_HOST_RE.match(s):
        raise ValueError("Это не адрес amoCRM: нужен вид mycompany.amocrm.ru (или .kommo.com)")
    return s


def parse_bitrix_webhook(raw: str) -> str:
    """Адрес входящего вебхука Bitrix24 → нормализованный (с «/» в конце).
    Метод в конце адреса (…/profile.json) отрезается — его часто копируют
    вместе с адресом из примера в самом Bitrix24."""
    s = (raw or "").strip()
    if not s:
        raise ValueError("Вставьте адрес входящего вебхука Bitrix24")
    p = urlparse(s)
    if p.scheme != "https" or not p.hostname:
        raise ValueError("Нужен адрес вида https://ваш-портал.bitrix24.ru/rest/1/код/")
    path = p.path or "/"
    m = re.match(r"^(/rest/\d+/[A-Za-z0-9_]+)(/.*)?$", path)
    if not m:
        raise ValueError("Это не входящий вебхук: в адресе должно быть /rest/<номер>/<код>/")
    norm = f"https://{p.hostname.lower()}{m.group(1)}/"
    if not _BITRIX_PATH_RE.match(urlparse(norm).path):
        raise ValueError("Это не входящий вебхук Bitrix24")
    return norm


def _price(v) -> Decimal:
    try:
        d = Decimal(str(v if v not in (None, "") else 0))
    except (InvalidOperation, ValueError):
        return Decimal(0)
    return d.quantize(Decimal("0.01")) if d.is_finite() else Decimal(0)


def money(amount, currency: str | None) -> str:
    """Сумма для заметки. Платёжные суммы Telegram — в минимальных единицах,
    кроме Звёзд (XTR), которые целые."""
    cur = (currency or "").upper()
    try:
        a = int(amount)
    except (TypeError, ValueError):
        return f"{amount} {cur}".strip()
    if cur == "XTR":
        return f"{a} ⭐"
    return f"{a / 100:.2f} {cur}".strip()


# ── Соответствие этапов ──────────────────────────────────────────────────────

def auto_stage_map(stages: list[dict]) -> dict[str, str]:
    """Этапы воронки CRM (по порядку) → соответствие нашим этапам.

    «Выиграно»/«Проиграно» — на закрывающие этапы CRM, четыре рабочих этапа —
    равномерно по открытым этапам CRM: в воронке из двух открытых этапов «Лид»
    и «Контакт» лягут на первый, «Предложение» и «Переговоры» — на второй.
    Владелец поправит на экране, если воронка устроена иначе. ЧИСТАЯ."""
    opens = [s for s in stages if s.get("kind") == "open"]
    won = next((s for s in stages if s.get("kind") == "won"), None)
    lost = next((s for s in stages if s.get("kind") == "lost"), None)
    out: dict[str, str] = {}
    if opens:
        n = len(opens)
        for i, st in enumerate(STAGES[:4]):
            out[st] = str(opens[round(i * (n - 1) / 3)]["id"])
    if won:
        out["won"] = str(won["id"])
    if lost:
        out["lost"] = str(lost["id"])
    return out


def stage_for_status(cfg: dict, status_id) -> str | None:
    """Этап CRM → наш этап. Этап, которого нет в соответствии (в CRM этапов
    больше, чем у нас), считается ближайшим нашим этапом слева по воронке:
    сделка, ушедшая в «Согласование договора», для нас всё ещё «Переговоры».
    None — этап из другой воронки или неизвестный. ЧИСТАЯ."""
    sid = str(status_id)
    smap = cfg.get("stage_map") or {}
    for st in STAGES:
        if str(smap.get(st, "")) == sid:
            return st
    order = [str(s["id"]) for s in (cfg.get("stages") or [])]
    if sid not in order:
        return None
    pos = order.index(sid)
    best, best_pos = None, -1
    for st in STAGES:
        mapped = str(smap.get(st, ""))
        if mapped in order:
            p = order.index(mapped)
            if p <= pos and p > best_pos:
                best, best_pos = st, p
    return best


# ── Клиенты CRM ──────────────────────────────────────────────────────────────

async def _request(http, method: str, url: str, *, headers=None, json_body=None):
    """Один запрос → (код, разобранный JSON или {}). Сеть — CrmError."""
    import aiohttp
    try:
        async with http.request(method, url, json=json_body, headers=headers or {},
                                allow_redirects=False,
                                timeout=aiohttp.ClientTimeout(total=_TIMEOUT_S)) as resp:
            code = resp.status
            raw = await resp.read()
    except asyncio.TimeoutError:
        raise CrmError(f"CRM не ответила за {_TIMEOUT_S} с")
    except aiohttp.ClientError as e:
        raise CrmError(f"сетевая ошибка: {type(e).__name__}")
    data = {}
    if raw:
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            data = {"_raw": raw[:300].decode("utf-8", "replace")}
    return code, data


class AmoClient:
    kind = "amocrm"

    def __init__(self, http, domain: str, token: str):
        self.http = http
        self.base = f"https://{domain}"
        self.domain = domain
        self.token = token

    async def _call(self, method: str, path: str, body=None) -> dict:
        code, data = await _request(
            self.http, method, self.base + path, json_body=body,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"})
        if code in (200, 201, 202, 204):
            return data if isinstance(data, dict) else {"_list": data}
        if code == 401:
            raise CrmError("amoCRM не принял токен — выпустите новый долгосрочный токен", code)
        if code == 402:
            raise CrmError("аккаунт amoCRM не оплачен", code)
        if code == 403:
            raise CrmError("у токена нет доступа к этому разделу amoCRM", code)
        if code == 404:
            raise CrmError("запись в amoCRM не найдена (удалена?)", code, not_found=True)
        if code == 429:
            raise CrmError("amoCRM просит реже (лимит запросов) — повторим", code)
        detail = ""
        if isinstance(data, dict):
            detail = data.get("detail") or data.get("title") or ""
            errs = (data.get("validation-errors") or [])
            if errs:
                detail = (detail + " " + json.dumps(errs, ensure_ascii=False))[:200]
        raise CrmError(f"amoCRM ответил {code}" + (f": {detail}" if detail else ""), code)

    async def pipelines(self) -> list[dict]:
        d = await self._call("GET", "/api/v4/leads/pipelines")
        out = []
        for p in (d.get("_embedded") or {}).get("pipelines") or []:
            stages = []
            for s in sorted((p.get("_embedded") or {}).get("statuses") or [],
                            key=lambda s: s.get("sort") or 0):
                sid = int(s.get("id") or 0)
                if sid == 142:
                    kind = "won"
                elif sid == 143:
                    kind = "lost"
                elif int(s.get("type") or 0) == 1:
                    continue  # «Неразобранное» — сюда сделку поставить нельзя
                else:
                    kind = "open"
                stages.append({"id": str(sid), "name": s.get("name") or str(sid), "kind": kind})
            out.append({"id": str(p.get("id")), "name": p.get("name") or "Воронка",
                        "is_main": bool(p.get("is_main")), "stages": stages})
        return out

    async def account_name(self) -> str:
        d = await self._call("GET", "/api/v4/account")
        return d.get("name") or self.domain

    @staticmethod
    def _contact_fields(person: dict) -> dict:
        c = {"name": (person.get("name") or "Без имени")[:250]}
        if person.get("first_name"):
            c["first_name"] = person["first_name"][:250]
        if person.get("last_name"):
            c["last_name"] = person["last_name"][:250]
        cfv = []
        if person.get("phone"):
            cfv.append({"field_code": "PHONE",
                        "values": [{"value": person["phone"], "enum_code": "WORK"}]})
        if person.get("email"):
            cfv.append({"field_code": "EMAIL",
                        "values": [{"value": person["email"], "enum_code": "WORK"}]})
        if cfv:
            c["custom_fields_values"] = cfv
        return c

    async def create_contact(self, person: dict) -> str:
        d = await self._call("POST", "/api/v4/contacts", [self._contact_fields(person)])
        return str(d["_embedded"]["contacts"][0]["id"])

    async def update_contact(self, ext_id: str, person: dict) -> None:
        body = self._contact_fields(person)
        if not person.get("name"):
            body.pop("name", None)
        await self._call("PATCH", f"/api/v4/contacts/{int(ext_id)}", body)

    async def create_deal(self, *, name: str, price, currency, status: str | None,
                          pipeline: str | None, contact_ext_id: str | None) -> str:
        lead = {"name": name[:250], "price": int(_price(price)),
                "_embedded": {"tags": [{"name": _TAG}]}}
        if pipeline:
            lead["pipeline_id"] = int(pipeline)
        if status:
            lead["status_id"] = int(status)
        if contact_ext_id:
            lead["_embedded"]["contacts"] = [{"id": int(contact_ext_id)}]
        d = await self._call("POST", "/api/v4/leads", [lead])
        return str(d["_embedded"]["leads"][0]["id"])

    async def update_deal(self, ext_id: str, *, status: str | None, pipeline: str | None,
                          price=None, name: str | None = None) -> None:
        body: dict = {}
        if status:
            body["status_id"] = int(status)
            if pipeline:
                body["pipeline_id"] = int(pipeline)
        if price is not None:
            body["price"] = int(_price(price))
        if name:
            body["name"] = name[:250]
        if body:
            await self._call("PATCH", f"/api/v4/leads/{int(ext_id)}", body)

    async def add_note(self, deal_ext_id: str, text: str) -> None:
        await self._call("POST", f"/api/v4/leads/{int(deal_ext_id)}/notes",
                         [{"note_type": "common", "params": {"text": text[:4000]}}])

    async def get_deal(self, ext_id: str) -> dict:
        d = await self._call("GET", f"/api/v4/leads/{int(ext_id)}")
        return {"status": str(d.get("status_id")), "pipeline": str(d.get("pipeline_id")),
                "price": _price(d.get("price"))}

    async def register_inbound(self, url: str) -> None:
        await self._call("POST", "/api/v4/webhooks",
                         {"destination": url, "settings": ["status_lead", "update_lead"]})

    async def unregister_inbound(self, url: str) -> None:
        await self._call("DELETE", "/api/v4/webhooks", {"destination": url})


class BitrixClient:
    kind = "bitrix24"

    def __init__(self, http, webhook_url: str):
        self.http = http
        self.base = webhook_url
        self.domain = urlparse(webhook_url).hostname or ""

    async def _call(self, method: str, params: dict | None = None):
        from services.security import resolve_url_is_public
        if not await resolve_url_is_public(self.base):
            raise CrmError("адрес Bitrix24 ведёт во внутреннюю сеть или не резолвится")
        code, data = await _request(self.http, "POST", f"{self.base}{method}.json",
                                    json_body=params or {})
        err = data.get("error") if isinstance(data, dict) else None
        if code == 200 and not err:
            return data.get("result")
        desc = (data.get("error_description") or err or "") if isinstance(data, dict) else ""
        if code == 401 or err in ("invalid_token", "INVALID_CREDENTIALS", "NO_AUTH_FOUND"):
            raise CrmError("Bitrix24 не принял вебхук — проверьте адрес и что вебхук не удалён", code)
        if err == "insufficient_scope":
            raise CrmError("у вебхука Bitrix24 нет права «CRM» — включите его в настройках вебхука", code)
        if code == 503 or err == "QUERY_LIMIT_EXCEEDED":
            raise CrmError("Bitrix24 просит реже (лимит запросов) — повторим", code)
        nf = "not found" in str(desc).lower() or "не найден" in str(desc).lower()
        raise CrmError(f"Bitrix24: {desc or ('ответ ' + str(code))}"[:300], code, not_found=nf)

    async def pipelines(self) -> list[dict]:
        cats = [{"id": "0", "name": "Общая", "is_main": True}]
        try:
            res = await self._call("crm.category.list", {"entityTypeId": 2})
            got = (res or {}).get("categories") if isinstance(res, dict) else None
            if got:
                cats = [{"id": str(c.get("id")), "name": c.get("name") or "Воронка",
                         "is_main": bool(c.get("isDefault")) or str(c.get("id")) == "0"}
                        for c in got]
        except CrmError as e:
            if e.code in (401,) or "вебхук" in e.message:
                raise
        out = []
        for c in cats:
            entity = "DEAL_STAGE" if c["id"] == "0" else f"DEAL_STAGE_{c['id']}"
            res = await self._call("crm.status.list", {"filter": {"ENTITY_ID": entity},
                                                       "order": {"SORT": "ASC"}}) or []
            stages = []
            for s in res:
                sid = str(s.get("STATUS_ID") or "")
                sem = ((s.get("EXTRA") or {}).get("SEMANTICS") or "").lower()
                tail = sid.rsplit(":", 1)[-1]
                if sem == "success" or (not sem and tail == "WON"):
                    kind = "won"
                elif sem == "failure" or (not sem and tail in ("LOSE", "APOLOGY")):
                    kind = "lost"
                else:
                    kind = "open"
                stages.append({"id": sid, "name": s.get("NAME") or sid, "kind": kind})
            out.append({**c, "stages": stages})
        return out

    async def account_name(self) -> str:
        return self.domain

    @staticmethod
    def _contact_fields(person: dict) -> dict:
        f: dict = {"SOURCE_ID": "OTHER", "SOURCE_DESCRIPTION": _TAG}
        if person.get("first_name") or person.get("last_name"):
            f["NAME"] = (person.get("first_name") or "")[:100]
            f["LAST_NAME"] = (person.get("last_name") or "")[:100]
        elif person.get("name"):
            f["NAME"] = person["name"][:100]
        if person.get("phone"):
            f["PHONE"] = [{"VALUE": person["phone"], "VALUE_TYPE": "WORK"}]
        if person.get("email"):
            f["EMAIL"] = [{"VALUE": person["email"], "VALUE_TYPE": "WORK"}]
        if person.get("username"):
            f["IM"] = [{"VALUE": "@" + person["username"].lstrip("@"), "VALUE_TYPE": "TELEGRAM"}]
        return f

    async def create_contact(self, person: dict) -> str:
        return str(await self._call("crm.contact.add", {"fields": self._contact_fields(person)}))

    async def update_contact(self, ext_id: str, person: dict) -> None:
        f = self._contact_fields(person)
        f.pop("SOURCE_ID", None)
        f.pop("SOURCE_DESCRIPTION", None)
        if not person.get("name") and not person.get("first_name"):
            f.pop("NAME", None)
            f.pop("LAST_NAME", None)
        f.pop("IM", None)
        if f:
            await self._call("crm.contact.update", {"id": ext_id, "fields": f})

    async def create_deal(self, *, name: str, price, currency, status: str | None,
                          pipeline: str | None, contact_ext_id: str | None) -> str:
        f: dict = {"TITLE": name[:250], "OPPORTUNITY": float(_price(price)),
                   "SOURCE_ID": "OTHER", "SOURCE_DESCRIPTION": _TAG}
        if currency:
            f["CURRENCY_ID"] = str(currency).upper()[:3]
        if pipeline is not None and str(pipeline) != "":
            f["CATEGORY_ID"] = int(pipeline)
        if status:
            f["STAGE_ID"] = status
        if contact_ext_id:
            f["CONTACT_ID"] = int(contact_ext_id)
        return str(await self._call("crm.deal.add", {"fields": f}))

    async def update_deal(self, ext_id: str, *, status: str | None, pipeline: str | None,
                          price=None, name: str | None = None) -> None:
        f: dict = {}
        if status:
            f["STAGE_ID"] = status
        if price is not None:
            f["OPPORTUNITY"] = float(_price(price))
        if name:
            f["TITLE"] = name[:250]
        if f:
            res = await self._call("crm.deal.update", {"id": ext_id, "fields": f})
            if res is False:
                raise CrmError("сделка в Bitrix24 не найдена (удалена?)", not_found=True)

    async def add_note(self, deal_ext_id: str, text: str) -> None:
        await self._call("crm.timeline.comment.add", {"fields": {
            "ENTITY_ID": int(deal_ext_id), "ENTITY_TYPE": "deal", "COMMENT": text[:4000]}})

    async def get_deal(self, ext_id: str) -> dict:
        d = await self._call("crm.deal.get", {"id": ext_id}) or {}
        return {"status": str(d.get("STAGE_ID") or ""), "pipeline": str(d.get("CATEGORY_ID") or "0"),
                "price": _price(d.get("OPPORTUNITY"))}


def make_client(http, kind: str, credential: str):
    if kind == "amocrm":
        domain, _, token = credential.partition("\n")
        return AmoClient(http, domain, token)
    if kind == "bitrix24":
        return BitrixClient(http, credential)
    raise ValueError("неизвестная CRM")


def _client_for(http, ep: dict):
    from services.token_vault import decrypt_token
    return make_client(http, ep["kind"], decrypt_token(ep.get("secret_enc") or ""))


def _cfg(ep: dict) -> dict:
    cfg = ep.get("crm_config") or {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except ValueError:
            cfg = {}
    return cfg if isinstance(cfg, dict) else {}


# ── Люди и записи у нас ──────────────────────────────────────────────────────

def _first(v) -> str:
    """Первый телефон/почта из JSONB-списка (строки или {value})."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return v.strip()
    if isinstance(v, list) and v:
        x = v[0]
        if isinstance(x, dict):
            x = x.get("value") or x.get("number") or x.get("phone") or x.get("email") or ""
        return str(x or "").strip()
    return ""


def person_name(p: dict) -> str:
    disp = (p.get("display_name") or "").strip()
    if disp:
        return disp
    full = " ".join(x for x in ((p.get("first_name") or "").strip(),
                                (p.get("last_name") or "").strip()) if x)
    if full:
        return full
    u = (p.get("username") or "").strip()
    return "@" + u if u else "Без имени"


async def _person_from_contact(pool, owner_id: int, contact_id: str) -> dict | None:
    r = await pool.fetchrow(
        """SELECT uc.display_name, uc.first_name, uc.last_name, uc.username, uc.company,
                  uc.phones, uc.emails, cc.stage, cc.deal_value, cc.currency
           FROM unified_contacts uc
           LEFT JOIN contact_crm cc ON cc.contact_id = uc.id AND cc.owner_id = uc.owner_id
           WHERE uc.id = $1::uuid AND uc.owner_id = $2""", str(contact_id), owner_id)
    if not r:
        return None
    d = dict(r)
    return {"name": person_name(d), "first_name": d.get("first_name") or "",
            "last_name": d.get("last_name") or "", "username": d.get("username") or "",
            "company": d.get("company") or "", "phone": _first(d.get("phones")),
            "email": _first(d.get("emails")), "stage": d.get("stage") or "lead",
            "price": _price(d.get("deal_value")), "currency": (d.get("currency") or "USD").upper()}


async def _person_from_chat(pool, owner_id: int, bot_id, chat_id) -> dict | None:
    if not bot_id or not chat_id:
        return None
    r = await pool.fetchrow(
        """SELECT bu.username, bu.first_name, bu.last_name, bu.phone, bu.fields,
                  mb.username AS bot_username
           FROM bot_users bu JOIN managed_bots mb ON mb.bot_id = bu.bot_id
           WHERE bu.bot_id = $1 AND bu.user_id = $2 AND mb.added_by = $3""",
        int(bot_id), int(chat_id), owner_id)
    if not r:
        return None
    d = dict(r)
    fields = d.get("fields") or {}
    if isinstance(fields, str):
        try:
            fields = json.loads(fields)
        except ValueError:
            fields = {}
    return {"name": person_name(d), "first_name": d.get("first_name") or "",
            "last_name": d.get("last_name") or "", "username": d.get("username") or "",
            "phone": (d.get("phone") or fields.get("phone") or "").strip(),
            "email": str(fields.get("email") or "").strip(),
            "bot_username": d.get("bot_username") or ""}


async def _order(pool, owner_id: int, order_id) -> dict | None:
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        return None
    r = await pool.fetchrow(
        """SELECT id, bot_id, customer_chat_id, customer_username, customer_name, items,
                  total_cents, currency, status, contact, note
           FROM bot_sales_orders WHERE id = $1 AND owner_id = $2""", oid, owner_id)
    return dict(r) if r else None


# ── Связи ────────────────────────────────────────────────────────────────────

async def _link(pool, endpoint_id: int, key: str) -> dict | None:
    r = await pool.fetchrow(
        "SELECT * FROM crm_links WHERE endpoint_id=$1 AND local_key=$2", endpoint_id, key)
    return dict(r) if r else None


async def _save_link(pool, endpoint_id: int, owner_id: int, key: str, **fields) -> None:
    cols = ["contact_ext_id", "deal_ext_id", "last_stage", "last_price"]
    vals = [fields.get(c) for c in cols]
    present = [c in fields for c in cols]
    await pool.execute(
        """INSERT INTO crm_links (endpoint_id, local_key, owner_id,
                                  contact_ext_id, deal_ext_id, last_stage, last_price)
           VALUES ($1, $2, $3, $4, $5, $6, $7)
           ON CONFLICT (endpoint_id, local_key) DO UPDATE SET
               contact_ext_id = CASE WHEN $8 THEN EXCLUDED.contact_ext_id ELSE crm_links.contact_ext_id END,
               deal_ext_id    = CASE WHEN $9 THEN EXCLUDED.deal_ext_id    ELSE crm_links.deal_ext_id END,
               last_stage     = CASE WHEN $10 THEN EXCLUDED.last_stage    ELSE crm_links.last_stage END,
               last_price     = CASE WHEN $11 THEN EXCLUDED.last_price    ELSE crm_links.last_price END,
               updated_at = now()""",
        endpoint_id, key, owner_id, *vals, *present)


class _Sync:
    """Одна доставка: клиент CRM + настройки подключения."""

    def __init__(self, pool, ep: dict, crm_api):
        self.pool = pool
        self.ep = ep
        self.api = crm_api
        self.cfg = _cfg(ep)
        self.owner_id = int(ep["owner_id"])
        self.endpoint_id = int(ep["id"])

    def status_for(self, stage: str | None) -> str | None:
        if not stage:
            return None
        return (self.cfg.get("stage_map") or {}).get(stage) or None

    @property
    def pipeline(self) -> str | None:
        p = self.cfg.get("pipeline_id")
        return str(p) if p not in (None, "") else None

    async def ensure(self, key: str, person: dict, *, deal_name: str, stage: str | None,
                     price=None, currency=None, intro: str = "",
                     reuse_contact_key: str | None = None) -> tuple[dict, bool]:
        """Запись есть в CRM (контакт + сделка) — создать недостающее.
        Возвращает (связь, создана_ли_сделка_сейчас)."""
        link = await _link(self.pool, self.endpoint_id, key) or {}
        contact_ext = link.get("contact_ext_id")
        if not contact_ext and reuse_contact_key:
            other = await _link(self.pool, self.endpoint_id, reuse_contact_key)
            contact_ext = (other or {}).get("contact_ext_id")
        if not contact_ext:
            contact_ext = await self.api.create_contact(person)
            # Связь — сразу после создания контакта: если сделка не создастся,
            # повтор не заведёт второго такого же человека.
            await _save_link(self.pool, self.endpoint_id, self.owner_id, key,
                             contact_ext_id=contact_ext)
        elif not link.get("contact_ext_id"):
            await _save_link(self.pool, self.endpoint_id, self.owner_id, key,
                             contact_ext_id=contact_ext)
        deal_ext = link.get("deal_ext_id")
        created = False
        if not deal_ext:
            deal_ext = await self.api.create_deal(
                name=deal_name, price=price, currency=currency,
                status=self.status_for(stage), pipeline=self.pipeline,
                contact_ext_id=contact_ext)
            await _save_link(self.pool, self.endpoint_id, self.owner_id, key,
                             deal_ext_id=deal_ext, last_stage=stage,
                             last_price=_price(price) if price is not None else None)
            created = True
            note = intro
            if person.get("username"):
                note = (note + "\n" if note else "") + f"Telegram: @{person['username'].lstrip('@')}"
            if note:
                await self.api.add_note(deal_ext, note)
        link = {**link, "contact_ext_id": contact_ext, "deal_ext_id": deal_ext}
        if created:
            link["last_stage"] = stage
            link["last_price"] = _price(price) if price is not None else None
        return link, created

    async def push(self, key: str, link: dict, *, stage: str | None, price=None,
                   name: str | None = None) -> bool:
        """Этап/сумма у нас изменились — в CRM. False — нечего отправлять
        (CRM уже знает это состояние: эхо её же изменения)."""
        same_stage = stage is None or stage == link.get("last_stage")
        same_price = price is None or (link.get("last_price") is not None
                                       and _price(price) == _price(link.get("last_price")))
        if same_stage and same_price and not name:
            return False
        try:
            await self.api.update_deal(
                link["deal_ext_id"], status=None if same_stage else self.status_for(stage),
                pipeline=self.pipeline, price=None if same_price else price, name=name)
        except CrmError as e:
            if not e.not_found:
                raise
            # Сделку удалили в CRM — забыть связь; следующий повтор создаст новую.
            await _save_link(self.pool, self.endpoint_id, self.owner_id, key, deal_ext_id=None)
            raise CrmError("сделку удалили в CRM — заведём заново при повторе")
        upd = {}
        if stage is not None:
            upd["last_stage"] = stage
        if price is not None:
            upd["last_price"] = _price(price)
        if upd:
            await _save_link(self.pool, self.endpoint_id, self.owner_id, key, **upd)
        return True

    async def note(self, link: dict, text: str) -> None:
        if text and link.get("deal_ext_id"):
            await self.api.add_note(link["deal_ext_id"], text)


# ── Обработчики событий ──────────────────────────────────────────────────────

async def _on_pipeline(s: _Sync, data: dict) -> str:
    cid = data.get("contact_id")
    if not cid:
        return "пропущено: нет контакта"
    p = await _person_from_contact(s.pool, s.owner_id, cid)
    if not p:
        return "пропущено: контакт удалён"
    # Состояние — из базы, а не из события: события доставляются с повторами,
    # и старое событие после нового откатило бы этап в CRM назад.
    key = f"contact:{cid}"
    link, created = await s.ensure(key, p, deal_name=f"{p['name']} — Infragram",
                                   stage=p["stage"], price=p["price"], currency=p["currency"],
                                   intro="Контакт из воронки Infragram")
    if not created:
        await s.push(key, link, stage=p["stage"], price=p["price"])
    return ""


async def _on_deal(s: _Sync, data: dict) -> str:
    try:
        did = int(data.get("deal_id"))
    except (TypeError, ValueError):
        return "пропущено: нет сделки"
    r = await s.pool.fetchrow(
        "SELECT id, title, contact, stage, value, notes FROM crm_deals WHERE id=$1 AND owner_id=$2",
        did, s.owner_id)
    if not r:
        return "пропущено: сделка удалена"
    stage = DEAL_TO_STAGE.get(r["stage"], "lead")
    who = (r["contact"] or "").strip()
    person = {"name": who or r["title"]}
    if who.startswith("@"):
        person["username"] = who[1:]
    key = f"deal:{did}"
    link, created = await s.ensure(key, person, deal_name=r["title"], stage=stage,
                                   price=r["value"], intro=(r["notes"] or "")[:2000])
    if not created:
        await s.push(key, link, stage=stage, price=r["value"])
    return ""


async def _on_started(s: _Sync, data: dict) -> str:
    bot_id, chat_id = data.get("bot_id"), data.get("chat_id")
    p = await _person_from_chat(s.pool, s.owner_id, bot_id, chat_id)
    if not p:
        return "пропущено: человек бота не найден"
    key = f"chat:{bot_id}:{chat_id}"
    param = (data.get("param") or "").strip()
    bot = f"@{p['bot_username']}" if p.get("bot_username") else "бота"
    intro = f"Открыл {bot}" + (f", метка ссылки: {param}" if param else "")
    link, created = await s.ensure(key, p, deal_name=f"{p['name']} — Telegram", stage="lead",
                                   intro=intro)
    if not created and param:
        await s.note(link, f"Снова открыл {bot}, метка ссылки: {param}")
    return ""


async def _on_field(s: _Sync, data: dict) -> str:
    bot_id, chat_id = data.get("bot_id"), data.get("chat_id")
    p = await _person_from_chat(s.pool, s.owner_id, bot_id, chat_id)
    if not p:
        return "пропущено: человек бота не найден"
    key = f"chat:{bot_id}:{chat_id}"
    field = str(data.get("field") or "").strip()
    value = str(data.get("value") or "").strip()
    link, _created = await s.ensure(key, p, deal_name=f"{p['name']} — Telegram", stage="lead")
    if field in ("phone", "email") and value and link.get("contact_ext_id"):
        await s.api.update_contact(link["contact_ext_id"], {field: value})
    if field and value:
        await s.note(link, f"Ответ в боте — {field}: {value}"[:1000])
    return ""


async def _on_order(s: _Sync, data: dict) -> str:
    o = await _order(s.pool, s.owner_id, data.get("order_id"))
    if not o:
        return "пропущено: заказ не найден"
    contact = o.get("contact") or {}
    if isinstance(contact, str):
        try:
            contact = json.loads(contact)
        except ValueError:
            contact = {}
    person = {"name": (o.get("customer_name") or "").strip()
              or ("@" + o["customer_username"] if o.get("customer_username") else "Покупатель"),
              "username": o.get("customer_username") or "",
              "phone": str(contact.get("phone") or "").strip(),
              "email": str(contact.get("email") or "").strip()}
    items = o.get("items") or []
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError:
            items = []
    cur = (o.get("currency") or "USD").upper()
    lines = [f"Заказ №{o['id']} из бота"]
    for it in items[:30]:
        if isinstance(it, dict):
            lines.append(f"• {it.get('name', '')} × {it.get('qty', 1)} — "
                         f"{money(it.get('price_cents', 0), cur)}")
    if data.get("summary"):
        lines.append(str(data["summary"])[:1500])
    for k, v in list(contact.items())[:10]:
        lines.append(f"{k}: {v}")
    price = Decimal(int(o.get("total_cents") or 0)) / 100
    await s.ensure(f"order:{o['id']}", person, deal_name=f"Заказ №{o['id']}",
                   stage="proposal", price=price, currency=cur, intro="\n".join(lines),
                   reuse_contact_key=f"chat:{o['bot_id']}:{o['customer_chat_id']}")
    return ""


async def _on_payment(s: _Sync, data: dict) -> str:
    text = "Оплата получена: " + money(data.get("amount"), data.get("currency"))
    if data.get("order_id"):
        o = await _order(s.pool, s.owner_id, data.get("order_id"))
        if not o:
            return "пропущено: заказ не найден"
        await _on_order(s, {"order_id": o["id"]})
        key = f"order:{o['id']}"
    else:
        bot_id, chat_id = data.get("bot_id"), data.get("chat_id")
        p = await _person_from_chat(s.pool, s.owner_id, bot_id, chat_id)
        if not p:
            return "пропущено: плательщик не найден"
        key = f"chat:{bot_id}:{chat_id}"
        await s.ensure(key, p, deal_name=f"{p['name']} — Telegram", stage="lead")
    link = await _link(s.pool, s.endpoint_id, key)
    if not link or not link.get("deal_ext_id"):
        return "пропущено: сделка не создана"
    await s.push(key, link, stage="won")
    await s.note(link, text)
    return ""


HANDLERS = {
    "pipeline.updated": _on_pipeline,
    "deal.updated": _on_deal,
    "contact.started": _on_started,
    "contact.field_set": _on_field,
    "order.created": _on_order,
    "payment.received": _on_payment,
}


async def deliver(pool, http, ep: dict, event: str, data: dict) -> tuple[bool, int | None, str]:
    """Доставка одного события в CRM → (успех, код ответа CRM, текст для журнала).
    Событие, которое CRM не нужно, — успех с пометкой «пропущено»."""
    handler = HANDLERS.get(event)
    if handler is None:
        return True, None, "пропущено: событие не для CRM"
    try:
        crm_api = _client_for(http, ep)
        note = await handler(_Sync(pool, ep, crm_api), data or {})
        return True, 200, note
    except CrmError as e:
        return False, e.code, e.message
    except (KeyError, IndexError, TypeError, ValueError) as e:
        return False, None, f"CRM ответила неожиданно: {type(e).__name__}"


# Бюджет времени на пачку одного подключения: строки заняты арендой на
# webhooks._LEASE_S (120 с), а запросов к CRM на событие бывает до четырёх.
# Что не успели — возвращается в очередь без траты попытки.
_GROUP_BUDGET_S = 80


async def deliver_group(pool, http, items: list[dict]) -> tuple[list, list[dict]]:
    """Доставки одного подключения — строго по порядку событий: «создан» и
    «сменил этап» об одном человеке, отправленные параллельно, завели бы в
    CRM две сделки. → ([(строка, результат)], [не начатые строки])."""
    import time as _time
    started = _time.monotonic()
    done, left = [], []
    for it in sorted(items, key=lambda x: x["event_id"]):
        if _time.monotonic() - started > _GROUP_BUDGET_S:
            left.append(it)
            continue
        try:
            res = await deliver(pool, http, it["endpoint"], it["event"], it["payload"])
        except Exception as e:
            # Сбой одной доставки (база, неожиданный ответ) не роняет пачку:
            # иначе уже доставленные в этой пачке строки остаются без итога,
            # после аренды уходят в CRM повторно — вторые заметки и сделки.
            log.warning("crm deliver: событие %s подключения %s упало", it.get("event_id"),
                        it.get("endpoint_id"), exc_info=True)
            res = (False, None, f"внутренняя ошибка: {type(e).__name__}")
        done.append((it, res))
    return done, left


# ── Подключение и настройка ──────────────────────────────────────────────────

def inbound_url(public_base: str, token: str) -> str:
    return f"{public_base.rstrip('/')}/webhook/crm/{token}"


async def _endpoint_row(pool, owner_id: int, endpoint_id: int) -> dict | None:
    r = await pool.fetchrow(
        """SELECT id, owner_id, kind, name, url, secret_enc, crm_config, inbound_token,
                  inbound_secret_enc, is_active, last_inbound_at
           FROM webhook_endpoints WHERE id=$1 AND owner_id=$2 AND kind = ANY($3::text[])""",
        endpoint_id, owner_id, list(CRM_KINDS))
    return dict(r) if r else None


async def connect(pool, http, owner_id: int, kind: str, *, domain: str = "",
                  token: str = "", webhook_url: str = "", public_base: str = "") -> dict:
    """Проверить доступ, прочитать воронки, сохранить подключение.
    ValueError — текст для экрана (неверный адрес, CRM не пустила)."""
    from services.token_vault import encrypt_token
    from services import webhooks as _wh
    kind = (kind or "").strip().lower()
    if kind == "amocrm":
        domain = parse_amo_domain(domain)
        token = (token or "").strip()
        if len(token) < 20:
            raise ValueError("Вставьте долгосрочный токен из настроек интеграции amoCRM")
        credential = f"{domain}\n{token}"
        shown = f"https://{domain}"
    elif kind == "bitrix24":
        credential = parse_bitrix_webhook(webhook_url)
        from services.security import resolve_url_is_public
        if not await resolve_url_is_public(credential):
            raise ValueError("Адрес портала не открывается из интернета или ведёт во внутреннюю сеть")
        shown = f"https://{urlparse(credential).hostname}"
    else:
        raise ValueError("Неизвестная CRM")
    if await pool.fetchval("SELECT 1 FROM webhook_endpoints WHERE owner_id=$1 AND kind=$2",
                           owner_id, kind):
        raise ValueError(f"{CRM_LABELS[kind]} уже подключена — сначала отключите прежнее подключение")
    n = int(await pool.fetchval(
        "SELECT COUNT(*) FROM webhook_endpoints WHERE owner_id=$1", owner_id) or 0)
    if n >= _wh.MAX_ENDPOINTS:
        raise ValueError(f"Получателей уже {_wh.MAX_ENDPOINTS} — удалите ненужный")
    crm_api = make_client(http, kind, credential)
    try:
        pipelines = await crm_api.pipelines()
        account = await crm_api.account_name()
    except CrmError as e:
        raise ValueError(f"{CRM_LABELS[kind]} не пустила: {e.message}")
    if not pipelines:
        raise ValueError(f"В {CRM_LABELS[kind]} нет ни одной воронки сделок")
    main = next((p for p in pipelines if p.get("is_main")), pipelines[0])
    cfg = {"account": account, "pipeline_id": main["id"], "pipeline_name": main["name"],
           "stages": main["stages"], "stage_map": auto_stage_map(main["stages"]),
           "inbound_auto": False}
    tok = secrets.token_urlsafe(24)
    row = await pool.fetchrow(
        """INSERT INTO webhook_endpoints (owner_id, kind, name, url, secret_enc, events,
                                          crm_config, inbound_token)
           VALUES ($1, $2, $3, $4, $5, $6::text[], $7::jsonb, $8) RETURNING id""",
        owner_id, kind, CRM_LABELS[kind], shown, encrypt_token(credential),
        DEFAULT_EVENTS, json.dumps(cfg, ensure_ascii=False), tok)
    if kind == "amocrm" and public_base:
        # amoCRM даёт подписаться на смену этапа по API — владельцу не нужно
        # идти в настройки. Не вышло (нет прав у токена) — экран покажет
        # адрес для ручной настройки.
        try:
            await crm_api.register_inbound(inbound_url(public_base, tok))
            cfg["inbound_auto"] = True
            await pool.execute(
                "UPDATE webhook_endpoints SET crm_config = crm_config || '{\"inbound_auto\": true}'::jsonb "
                "WHERE id=$1", row["id"])
        except CrmError as e:
            log.info("crm: amo webhook не зарегистрирован owner=%s: %s", owner_id, e.message)
    return await describe(pool, owner_id, row["id"], public_base=public_base)


async def describe(pool, owner_id: int, endpoint_id: int, *, public_base: str = "") -> dict | None:
    ep = await _endpoint_row(pool, owner_id, endpoint_id)
    if not ep:
        return None
    cfg = _cfg(ep)
    links = int(await pool.fetchval(
        "SELECT COUNT(*) FROM crm_links WHERE endpoint_id=$1 AND deal_ext_id IS NOT NULL",
        endpoint_id) or 0)
    return {
        "id": ep["id"], "kind": ep["kind"], "label": CRM_LABELS[ep["kind"]],
        "url": ep["url"] or "", "account": cfg.get("account") or "",
        "is_active": bool(ep["is_active"]),
        "pipeline_id": str(cfg.get("pipeline_id") or ""),
        "pipeline_name": cfg.get("pipeline_name") or "",
        "stages": cfg.get("stages") or [], "stage_map": cfg.get("stage_map") or {},
        "inbound_url": inbound_url(public_base, ep["inbound_token"])
        if public_base and ep.get("inbound_token") else "",
        "inbound_auto": bool(cfg.get("inbound_auto")),
        "inbound_secret_set": bool(ep.get("inbound_secret_enc")),
        "last_inbound_at": ep["last_inbound_at"].isoformat() if ep.get("last_inbound_at") else None,
        "synced_records": links,
    }


async def list_pipelines(pool, http, owner_id: int, endpoint_id: int) -> list[dict]:
    ep = await _endpoint_row(pool, owner_id, endpoint_id)
    if not ep:
        raise LookupError("Подключение не найдено")
    try:
        return await _client_for(http, ep).pipelines()
    except CrmError as e:
        raise ValueError(e.message)


async def configure(pool, http, owner_id: int, endpoint_id: int, *, pipeline_id=None,
                    stage_map: dict | None = None, inbound_secret: str | None = None) -> dict:
    """Воронка, соответствие этапов, токен исходящего вебхука Bitrix24."""
    from services.token_vault import encrypt_token
    ep = await _endpoint_row(pool, owner_id, endpoint_id)
    if not ep:
        raise LookupError("Подключение не найдено")
    cfg = _cfg(ep)
    if pipeline_id is not None or stage_map is not None:
        pipelines = await list_pipelines(pool, http, owner_id, endpoint_id)
        pid = str(pipeline_id if pipeline_id is not None else cfg.get("pipeline_id") or "")
        pipe = next((p for p in pipelines if str(p["id"]) == pid), None)
        if not pipe:
            raise ValueError("Такой воронки в CRM нет — обновите список")
        ids = {str(s["id"]) for s in pipe["stages"]}
        if stage_map is None or pid != str(cfg.get("pipeline_id")):
            new_map = auto_stage_map(pipe["stages"])
        else:
            new_map = {}
        for st, sid in (stage_map or {}).items():
            if st not in STAGES:
                raise ValueError("Неизвестный этап Infragram")
            if sid in (None, ""):
                new_map.pop(st, None)
                continue
            if str(sid) not in ids:
                raise ValueError("Этапа нет в выбранной воронке — обновите список")
            new_map[st] = str(sid)
        cfg.update({"pipeline_id": pid, "pipeline_name": pipe["name"],
                    "stages": pipe["stages"], "stage_map": new_map})
        await pool.execute("UPDATE webhook_endpoints SET crm_config=$2::jsonb WHERE id=$1",
                           endpoint_id, json.dumps(cfg, ensure_ascii=False))
    if inbound_secret is not None:
        sec = inbound_secret.strip()
        await pool.execute("UPDATE webhook_endpoints SET inbound_secret_enc=$2 WHERE id=$1",
                           endpoint_id, encrypt_token(sec) if sec else None)
    return await describe(pool, owner_id, endpoint_id)


async def disconnect(pool, http, owner_id: int, endpoint_id: int, *, public_base: str = "") -> bool:
    ep = await _endpoint_row(pool, owner_id, endpoint_id)
    if not ep:
        return False
    if ep["kind"] == "amocrm" and _cfg(ep).get("inbound_auto") and public_base:
        try:
            await _client_for(http, ep).unregister_inbound(
                inbound_url(public_base, ep["inbound_token"]))
        except CrmError:
            log.info("crm: amo webhook не снят endpoint=%s", endpoint_id)
    res = await pool.execute("DELETE FROM webhook_endpoints WHERE id=$1 AND owner_id=$2",
                             endpoint_id, owner_id)
    return res != "DELETE 0"


BACKFILL_LIMIT = 2000


async def backfill(pool, owner_id: int, endpoint_id: int) -> int:
    """Выгрузить в CRM всю текущую воронку (и простые сделки) — разово, при
    подключении. Доставки создаются ТОЛЬКО этому подключению: событие в общей
    ленте ушло бы и всем вебхукам владельца, которые этой выгрузки не ждут."""
    ep = await _endpoint_row(pool, owner_id, endpoint_id)
    if not ep:
        raise LookupError("Подключение не найдено")
    return int(await pool.fetchval(
        f"""WITH src AS (
                (SELECT 'pipeline.updated' AS event,
                        jsonb_build_object('contact_id', contact_id, 'stage', stage,
                                           'backfill', true) AS payload,
                        updated_at AS at
                 FROM contact_crm WHERE owner_id=$1
                 ORDER BY updated_at DESC NULLS LAST LIMIT {BACKFILL_LIMIT})
                UNION ALL
                (SELECT 'deal.updated', jsonb_build_object('deal_id', id, 'stage', stage,
                                                           'backfill', true), updated_at
                 FROM crm_deals WHERE owner_id=$1
                 ORDER BY updated_at DESC LIMIT {BACKFILL_LIMIT})
            ),
            ev AS (
                INSERT INTO integration_events (owner_id, event, payload, fanned_out_at)
                SELECT $1, event, payload, now() FROM src ORDER BY at ASC NULLS FIRST
                RETURNING id, event
            ),
            ins AS (
                INSERT INTO webhook_deliveries (endpoint_id, event_id, owner_id, event)
                SELECT $2, id, $1, event FROM ev
                RETURNING 1
            )
            SELECT COUNT(*) FROM ins""", owner_id, endpoint_id) or 0)


async def check(pool, http, owner_id: int, endpoint_id: int) -> dict:
    ep = await _endpoint_row(pool, owner_id, endpoint_id)
    if not ep:
        return {"ok": False, "error": "Подключение не найдено"}
    try:
        crm_api = _client_for(http, ep)
        pipes = await crm_api.pipelines()
        account = await crm_api.account_name()
    except CrmError as e:
        return {"ok": False, "status_code": e.code, "error": e.message}
    cfg = _cfg(ep)
    pipe = next((p for p in pipes if str(p["id"]) == str(cfg.get("pipeline_id"))), None)
    warn = "" if pipe else "Выбранной воронки в CRM больше нет — выберите другую"
    if pipe:
        ids = {str(s["id"]) for s in pipe["stages"]}
        lost = [st for st, sid in (cfg.get("stage_map") or {}).items() if str(sid) not in ids]
        if lost:
            warn = "Часть этапов удалили в CRM — проверьте соответствие этапов"
    return {"ok": True, "status_code": 200, "account": account, "warning": warn, "error": ""}


# ── Входящие: смена этапа в CRM → этап у нас ─────────────────────────────────

def _form_ids(form: dict, kind: str) -> list[str]:
    """id сделок из тела входящего вебхука (form-urlencoded, плоские ключи
    вида leads[status][0][id]). ЧИСТАЯ."""
    ids: list[str] = []
    if kind == "amocrm":
        pat = re.compile(r"^leads\[(status|update|add)\]\[\d+\]\[id\]$")
        for k, v in form.items():
            if pat.match(k):
                ids.append(str(v).strip())
    else:
        v = form.get("data[FIELDS][ID]")
        if v:
            ids.append(str(v).strip())
    seen, out = set(), []
    for i in ids:
        if i.isdigit() and i not in seen:
            seen.add(i)
            out.append(i)
    return out[:_MAX_INBOUND_IDS]


async def handle_inbound(pool, http, token: str, form: dict) -> tuple[int, str]:
    """Входящий вебхук CRM. → (HTTP-код, текст). CRM повторяет запрос при
    ответе не 2xx, поэтому «не наше» — тоже 200: повторять его бессмысленно."""
    import hmac
    from services.token_vault import decrypt_token
    if not token or len(token) > 64:
        return 404, "not found"
    r = await pool.fetchrow(
        """SELECT id, owner_id, kind, name, url, secret_enc, crm_config, inbound_token,
                  inbound_secret_enc, is_active
           FROM webhook_endpoints WHERE inbound_token=$1""", token)
    if not r:
        return 404, "not found"
    ep = dict(r)
    if not ep["is_active"]:
        return 200, "inactive"
    cfg = _cfg(ep)
    if ep["kind"] == "bitrix24":
        want = decrypt_token(ep.get("inbound_secret_enc") or "") if ep.get("inbound_secret_enc") else ""
        got = str(form.get("auth[application_token]") or "")
        if not want or not hmac.compare_digest(want.encode(), got.encode()):
            return 403, "bad application_token"
        if not str(form.get("event") or "").upper().startswith("ONCRMDEAL"):
            return 200, "ignored"
    else:
        sub = str(form.get("account[subdomain]") or "").strip().lower()
        host = urlparse(ep["url"] or "").hostname or ""
        if sub and host and host.split(".", 1)[0] != sub:
            return 403, "foreign account"
    await pool.execute("UPDATE webhook_endpoints SET last_inbound_at=now() WHERE id=$1", ep["id"])
    ids = _form_ids(form, ep["kind"])
    if not ids:
        return 200, "ok"
    crm_api = _client_for(http, ep)
    applied = 0
    for ext in ids:
        link = await pool.fetchrow(
            "SELECT * FROM crm_links WHERE endpoint_id=$1 AND deal_ext_id=$2", ep["id"], ext)
        if not link:
            continue
        try:
            deal = await crm_api.get_deal(ext)
        except CrmError as e:
            log.info("crm inbound: сделка %s не прочитана: %s", ext, e.message)
            continue
        if cfg.get("pipeline_id") and deal["pipeline"] and str(deal["pipeline"]) != str(cfg["pipeline_id"]):
            continue  # сделку увели в другую воронку — этапы не сопоставимы
        stage = stage_for_status(cfg, deal["status"])
        if stage is None:
            continue
        if await apply_inbound(pool, int(ep["owner_id"]), int(ep["id"]), dict(link),
                               stage, deal["price"], CRM_LABELS[ep["kind"]]):
            applied += 1
    return 200, f"ok {applied}"


async def apply_inbound(pool, owner_id: int, endpoint_id: int, link: dict, stage: str,
                        price, label: str) -> bool:
    """Записать этап/сумму из CRM у нас. False — менять нечего."""
    price = _price(price)
    if stage == link.get("last_stage") and (link.get("last_price") is not None
                                            and price == _price(link["last_price"])):
        return False
    key = link["local_key"]
    # Сначала связь: тогда pipeline.updated от нашей же записи увидит, что
    # CRM это состояние уже знает, и не поедет обратно.
    await _save_link(pool, endpoint_id, owner_id, key, last_stage=stage, last_price=price)
    if key.startswith("contact:"):
        from services.contacts_hub import crm_engine
        cid = key.split(":", 1)[1]
        cur = await pool.fetchrow(
            "SELECT stage, deal_value FROM contact_crm WHERE owner_id=$1 AND contact_id=$2::uuid",
            owner_id, cid)
        data = {"stage": stage}
        if price or (cur and cur["deal_value"]):
            data["deal_value"] = float(price)
        try:
            await crm_engine.upsert_crm(pool, owner_id, cid, data)
            await crm_engine.log_crm_activity(
                pool, owner_id, cid, "deal",
                f"Этап изменён в {label}: {crm_engine.STAGE_LABELS.get(stage, stage)}",
                {"source": "crm_inbound", "endpoint_id": endpoint_id})
        except crm_engine.ContactNotFound:
            return False
        return True
    if key.startswith("deal:"):
        did = int(key.split(":", 1)[1])
        cur = await pool.fetchval(
            "SELECT stage FROM crm_deals WHERE id=$1 AND owner_id=$2", did, owner_id)
        if cur is None:
            return False
        old_vocab = cur in ("new", "contacted", "qualified")
        target = STAGE_TO_DEAL[stage] if old_vocab else stage
        try:
            res = await pool.execute(
                "UPDATE crm_deals SET stage=$3, value=$4, updated_at=now() "
                "WHERE id=$1 AND owner_id=$2", did, owner_id, target, price)
        except Exception as e:
            # won/lost есть в обоих словарях — по ним не угадать, каким
            # записана база; промах ловит CHECK, тогда — другим словарём.
            if "check" not in str(e).lower():
                raise
            other = stage if target != stage else STAGE_TO_DEAL[stage]
            res = await pool.execute(
                "UPDATE crm_deals SET stage=$3, value=$4, updated_at=now() "
                "WHERE id=$1 AND owner_id=$2", did, owner_id, other, price)
        return res != "UPDATE 0"
    # Человек из бота и заказ живут у нас без этапа: CRM здесь главная,
    # согласованное состояние уже записано в связь.
    return True
