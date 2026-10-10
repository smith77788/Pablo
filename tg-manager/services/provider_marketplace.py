"""Provider Marketplace — блок 1: провайдеры и каталог услуг.

Единый kernel маркетплейса поверх существующих паттернов Infragram (asyncpg-пул,
token_vault для секретов, schema_vN + defensive DDL). Внешние компании
регистрируются провайдерами, проходят верификацию и публикуют услуги; заказы,
биллинг, резеллеры и рекомендации — следующие блоки, ссылаются на эти сущности.

Деньги — в МИНОРНЫХ единицах (центах), int; никаких float. Секрет вебхука —
через token_vault (AES-256-GCM). API-ключи Provider API храним ТОЛЬКО хэшем.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from decimal import Decimal, InvalidOperation
from typing import Any

import logging

log = logging.getLogger(__name__)

# ── Справочники/константы ────────────────────────────────────────────────────────
PROVIDER_STATUSES = ("pending", "verified", "rejected", "suspended")
PROVIDER_CATEGORIES = (
    "proxy", "accounts", "smm", "hosting", "content", "design",
    "verification", "sms", "payments", "analytics", "other",
)
# Какие потребности продукта услуга может закрывать (для контекстных рекомендаций).
RESOURCE_KINDS = (
    "proxy", "accounts", "channel", "bot", "content", "sms_number",
    "design", "subscribers", "views", "reactions", "compute", "other", "",
)
_API_KEY_PREFIX = "mpk_"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


# ── Чистые помощники (тестируются без БД) ────────────────────────────────────────
def slugify(name: str) -> str:
    """Стабильный slug из имени. ЧИСТАЯ."""
    s = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    return s or "provider"


def to_cents(amount: Any) -> int:
    """Строку/число денег → минорные единицы (центы), округление банковское до цента.
    Отрицательное и мусор → ValueError. ЧИСТАЯ."""
    try:
        d = Decimal(str(amount).strip().replace(",", "."))
    except (InvalidOperation, AttributeError, ValueError):
        raise ValueError(f"некорректная сумма: {amount!r}")
    if d.is_nan() or d.is_infinite() or d < 0:
        raise ValueError(f"сумма должна быть неотрицательным числом: {amount!r}")
    return int((d * 100).quantize(Decimal("1")))


def format_cents(cents: int, currency: str = "USD") -> str:
    """Центы → человекочитаемая цена. ЧИСТАЯ."""
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d} {currency}"


def commission_split(price_cents: int, commission_bps: int,
                     reseller_margin_cents: int = 0) -> dict:
    """Разложение цены: provider price → +комиссия площадки → +маржа резеллера.

    commission_bps — комиссия Infragram в базисных пунктах (100 bps = 1%).
    Всё в центах, целочисленно (комиссия округляется вниз — площадка не завышает).
    ЧИСТАЯ функция — основа биллинга.
    """
    price_cents = int(price_cents)
    commission_bps = max(0, int(commission_bps))
    reseller_margin_cents = max(0, int(reseller_margin_cents))
    if price_cents < 0:
        raise ValueError("price_cents must be >= 0")
    platform_fee = (price_cents * commission_bps) // 10000
    final = price_cents + platform_fee + reseller_margin_cents
    return {
        "provider_cents": price_cents,        # получает провайдер
        "platform_cents": platform_fee,       # комиссия Infragram
        "reseller_cents": reseller_margin_cents,
        "final_cents": final,                 # платит покупатель
    }


def gen_api_key() -> tuple[str, str, str]:
    """(full_key, prefix, sha256_hash). full показывается провайдеру ОДИН раз.
    ЧИСТАЯ (кроме источника энтропии)."""
    raw = _API_KEY_PREFIX + secrets.token_urlsafe(32)
    return raw, raw[:12], hash_api_key(raw)


def hash_api_key(full_key: str) -> str:
    """sha256 полного ключа — то, что храним и по чему ищем. ЧИСТАЯ."""
    return hashlib.sha256((full_key or "").encode("utf-8")).hexdigest()


def validate_service_fields(price_cents: int, min_qty: int, max_qty: int) -> None:
    """Инварианты услуги; бросает ValueError. ЧИСТАЯ."""
    if int(price_cents) < 0:
        raise ValueError("цена не может быть отрицательной")
    if int(min_qty) < 1:
        raise ValueError("min_qty должен быть ≥ 1")
    if int(max_qty) < int(min_qty):
        raise ValueError("max_qty не может быть меньше min_qty")


def _norm_category(cat: str) -> str:
    c = (cat or "").strip().lower()
    return c if c in PROVIDER_CATEGORIES else "other"


def _norm_kind(kind: str) -> str:
    k = (kind or "").strip().lower()
    return k if k in RESOURCE_KINDS else "other"


# ── Аудит ────────────────────────────────────────────────────────────────────────
async def audit(pool, entity_type: str, entity_id: int | None, action: str,
                actor_id: int | None = None, meta: dict | None = None) -> None:
    import json
    try:
        await pool.execute(
            "INSERT INTO mp_audit_log(entity_type, entity_id, action, actor_id, meta) "
            "VALUES($1,$2,$3,$4,$5::jsonb)",
            entity_type, entity_id, action, actor_id, json.dumps(meta or {}),
        )
    except Exception as e:
        log.warning("mp audit failed %s/%s %s: %s", entity_type, entity_id, action, e)


# ── Провайдеры ───────────────────────────────────────────────────────────────────
async def _unique_slug(pool, base: str) -> str:
    slug = base
    for i in range(0, 50):
        cand = slug if i == 0 else f"{slug}-{i}"
        exists = await pool.fetchval("SELECT 1 FROM mp_providers WHERE slug=$1", cand)
        if not exists:
            return cand
    return f"{slug}-{secrets.token_hex(3)}"


async def register_provider(pool, owner_id: int, name: str, *, description: str = "",
                            category: str = "other", website: str = "",
                            contact: str = "") -> dict:
    """Регистрирует провайдера в статусе pending (ждёт верификации админом)."""
    name = (name or "").strip()
    if not name:
        raise ValueError("имя провайдера обязательно")
    if not owner_id:
        raise ValueError("owner_id обязателен")
    slug = await _unique_slug(pool, slugify(name))
    row = await pool.fetchrow(
        """INSERT INTO mp_providers(owner_id, slug, name, description, category,
                                    website, contact, status)
           VALUES($1,$2,$3,$4,$5,$6,$7,'pending') RETURNING *""",
        owner_id, slug, name, description.strip(), _norm_category(category),
        website.strip(), contact.strip(),
    )
    d = dict(row)
    await audit(pool, "provider", d["id"], "register", owner_id,
                {"name": name, "slug": slug})
    return d


async def get_provider(pool, provider_id: int) -> dict | None:
    r = await pool.fetchrow("SELECT * FROM mp_providers WHERE id=$1", provider_id)
    return dict(r) if r else None


async def get_provider_by_slug(pool, slug: str) -> dict | None:
    r = await pool.fetchrow("SELECT * FROM mp_providers WHERE slug=$1", slug)
    return dict(r) if r else None


async def list_providers(pool, *, owner_id: int | None = None,
                         status: str | None = None, category: str | None = None,
                         limit: int = 100, offset: int = 0) -> list[dict]:
    conds, args = [], []
    if owner_id is not None:
        args.append(owner_id); conds.append(f"owner_id=${len(args)}")
    if status:
        args.append(status); conds.append(f"status=${len(args)}")
    if category:
        args.append(category); conds.append(f"category=${len(args)}")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    args.extend([max(1, min(500, limit)), max(0, offset)])
    rows = await pool.fetch(
        f"SELECT * FROM mp_providers {where} ORDER BY created_at DESC "
        f"LIMIT ${len(args)-1} OFFSET ${len(args)}", *args)
    return [dict(r) for r in rows]


async def update_provider_profile(pool, provider_id: int, owner_id: int,
                                  **fields) -> dict | None:
    """Обновляет профиль (только владельцем). Меняемые поля — из белого списка.
    webhook_secret шифруется token_vault перед записью (webhook_secret → *_enc)."""
    allowed = {"name", "description", "category", "website", "contact",
               "api_base_url", "webhook_url"}
    sets, args = [], []
    for k, v in fields.items():
        if k == "webhook_secret":
            from services.token_vault import encrypt_token
            args.append(encrypt_token(str(v or "")))
            sets.append(f"webhook_secret_enc=${len(args)}")
            continue
        if k not in allowed or v is None:
            continue
        val = _norm_category(v) if k == "category" else str(v).strip()
        args.append(val); sets.append(f"{k}=${len(args)}")
    if not sets:
        return await get_provider(pool, provider_id)
    args.extend([provider_id, owner_id])
    row = await pool.fetchrow(
        f"UPDATE mp_providers SET {', '.join(sets)}, updated_at=now() "
        f"WHERE id=${len(args)-1} AND owner_id=${len(args)} RETURNING *", *args)
    if not row:
        return None
    await audit(pool, "provider", provider_id, "update_profile", owner_id,
                {"fields": [k for k in fields if k != "webhook_secret"]})
    return dict(row)


async def set_provider_status(pool, provider_id: int, status: str, *,
                              by: int | None = None, reason: str = "") -> dict | None:
    """Админское действие: verify/reject/suspend/pending. С аудитом."""
    if status not in PROVIDER_STATUSES:
        raise ValueError(f"недопустимый статус: {status}")
    verified_at = "now()" if status == "verified" else "NULL"
    row = await pool.fetchrow(
        f"""UPDATE mp_providers
            SET status=$2, reject_reason=$3, verified_by=$4,
                verified_at={verified_at}, updated_at=now()
            WHERE id=$1 RETURNING *""",
        provider_id, status, (reason or "").strip(), by,
    )
    if not row:
        return None
    await audit(pool, "provider", provider_id, status, by, {"reason": reason})
    return dict(row)


# ── API-ключи Provider API ───────────────────────────────────────────────────────
async def issue_api_key(pool, provider_id: int, *, scopes: str = "catalog,orders",
                        actor_id: int | None = None) -> dict:
    """Выдаёт новый ключ. ВОЗВРАЩАЕТ полный ключ ОДИН раз (в БД — только хэш)."""
    full, prefix, key_hash = gen_api_key()
    row = await pool.fetchrow(
        """INSERT INTO mp_provider_api_keys(provider_id, key_prefix, key_hash, scopes)
           VALUES($1,$2,$3,$4) RETURNING id, key_prefix, scopes, created_at""",
        provider_id, prefix, key_hash, scopes,
    )
    await audit(pool, "api_key", row["id"], "issue", actor_id,
                {"provider_id": provider_id, "prefix": prefix})
    return {"id": row["id"], "api_key": full, "key_prefix": prefix,
            "scopes": row["scopes"], "created_at": row["created_at"]}


async def list_api_keys(pool, provider_id: int) -> list[dict]:
    """Только префиксы/метаданные — сырые ключи не хранятся и не отдаются."""
    rows = await pool.fetch(
        """SELECT id, key_prefix, scopes, is_active, last_used_at, created_at, revoked_at
           FROM mp_provider_api_keys WHERE provider_id=$1 ORDER BY created_at DESC""",
        provider_id)
    return [dict(r) for r in rows]


async def revoke_api_key(pool, provider_id: int, key_id: int,
                         actor_id: int | None = None) -> bool:
    res = await pool.execute(
        "UPDATE mp_provider_api_keys SET is_active=FALSE, revoked_at=now() "
        "WHERE id=$1 AND provider_id=$2 AND is_active=TRUE", key_id, provider_id)
    ok = res.endswith("1")
    if ok:
        await audit(pool, "api_key", key_id, "revoke", actor_id,
                    {"provider_id": provider_id})
    return ok


async def authenticate_api_key(pool, full_key: str) -> dict | None:
    """Provider API auth: сырой ключ → провайдер (или None). Обновляет last_used_at.
    Провайдер должен быть не suspended/rejected. Ключ — активный."""
    if not full_key or not full_key.startswith(_API_KEY_PREFIX):
        return None
    key_hash = hash_api_key(full_key)
    row = await pool.fetchrow(
        """SELECT k.id AS key_id, k.scopes, p.*
           FROM mp_provider_api_keys k JOIN mp_providers p ON p.id=k.provider_id
           WHERE k.key_hash=$1 AND k.is_active=TRUE""", key_hash)
    if not row:
        return None
    d = dict(row)
    if d.get("status") in ("suspended", "rejected"):
        return None
    try:
        await pool.execute(
            "UPDATE mp_provider_api_keys SET last_used_at=now() WHERE id=$1",
            d["key_id"])
    except Exception:
        pass
    return d


# ── Каталог услуг ────────────────────────────────────────────────────────────────
async def create_service(pool, provider_id: int, *, title: str, category: str = "other",
                         resource_kind: str = "", description: str = "",
                         price_cents: int = 0, currency: str = "USD",
                         unit: str = "unit", min_qty: int = 1, max_qty: int = 1000000,
                         stock: int = -1, sla_hours: int = 24,
                         params_schema: list | None = None, external_id: str = "",
                         actor_id: int | None = None) -> dict:
    import json
    title = (title or "").strip()
    if not title:
        raise ValueError("название услуги обязательно")
    validate_service_fields(price_cents, min_qty, max_qty)
    row = await pool.fetchrow(
        """INSERT INTO mp_services(provider_id, external_id, category, resource_kind,
               title, description, price_cents, currency, unit, min_qty, max_qty,
               stock, sla_hours, params_schema)
           VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb)
           RETURNING *""",
        provider_id, (external_id or "").strip(), _norm_category(category),
        _norm_kind(resource_kind), title, (description or "").strip(),
        int(price_cents), (currency or "USD").upper()[:8], (unit or "unit").strip(),
        int(min_qty), int(max_qty), int(stock), int(sla_hours),
        json.dumps(params_schema or []),
    )
    d = dict(row)
    await audit(pool, "service", d["id"], "create", actor_id,
                {"provider_id": provider_id, "title": title})
    return d


async def update_service(pool, service_id: int, provider_id: int,
                         actor_id: int | None = None, **fields) -> dict | None:
    import json
    allowed_text = {"title", "description", "category", "resource_kind", "unit",
                    "currency", "external_id"}
    allowed_int = {"price_cents", "min_qty", "max_qty", "stock", "sla_hours"}
    sets, args = [], []
    for k, v in fields.items():
        if v is None:
            continue
        if k in allowed_text:
            val = (_norm_category(v) if k == "category"
                   else _norm_kind(v) if k == "resource_kind"
                   else str(v).strip())
            args.append(val); sets.append(f"{k}=${len(args)}")
        elif k in allowed_int:
            args.append(int(v)); sets.append(f"{k}=${len(args)}")
        elif k == "is_active":
            args.append(bool(v)); sets.append(f"is_active=${len(args)}")
        elif k == "params_schema":
            args.append(json.dumps(v or [])); sets.append(f"params_schema=${len(args)}::jsonb")
    if not sets:
        return await get_service(pool, service_id)
    # проверим инварианты на итоговых значениях
    cur = await get_service(pool, service_id)
    if not cur:
        return None
    merged = {**cur, **{k: v for k, v in fields.items() if v is not None}}
    validate_service_fields(merged.get("price_cents", 0),
                            merged.get("min_qty", 1), merged.get("max_qty", 1))
    args.extend([service_id, provider_id])
    row = await pool.fetchrow(
        f"UPDATE mp_services SET {', '.join(sets)}, updated_at=now() "
        f"WHERE id=${len(args)-1} AND provider_id=${len(args)} RETURNING *", *args)
    if not row:
        return None
    await audit(pool, "service", service_id, "update", actor_id,
                {"fields": list(fields)})
    return dict(row)


async def set_service_active(pool, service_id: int, provider_id: int, active: bool,
                             actor_id: int | None = None) -> bool:
    res = await pool.execute(
        "UPDATE mp_services SET is_active=$3, updated_at=now() "
        "WHERE id=$1 AND provider_id=$2", service_id, provider_id, bool(active))
    ok = res.endswith(" 1") or res.endswith("UPDATE 1")
    if ok:
        await audit(pool, "service", service_id,
                    "activate" if active else "deactivate", actor_id, {})
    return ok


async def get_service(pool, service_id: int) -> dict | None:
    r = await pool.fetchrow("SELECT * FROM mp_services WHERE id=$1", service_id)
    return dict(r) if r else None


async def list_services(pool, provider_id: int, *, include_inactive: bool = True,
                        limit: int = 200, offset: int = 0) -> list[dict]:
    cond = "" if include_inactive else "AND is_active=TRUE"
    rows = await pool.fetch(
        f"SELECT * FROM mp_services WHERE provider_id=$1 {cond} "
        f"ORDER BY created_at DESC LIMIT $2 OFFSET $3",
        provider_id, max(1, min(500, limit)), max(0, offset))
    return [dict(r) for r in rows]


async def browse_catalog(pool, *, category: str | None = None,
                         resource_kind: str | None = None, q: str | None = None,
                         limit: int = 50, offset: int = 0) -> list[dict]:
    """Публичный каталог: только VERIFIED-провайдеры, активные услуги в наличии.
    Возвращает услугу + имя/рейтинг провайдера (один запрос, без N+1)."""
    conds = ["p.status='verified'", "s.is_active=TRUE", "(s.stock < 0 OR s.stock > 0)"]
    args: list = []
    if category:
        args.append(category); conds.append(f"s.category=${len(args)}")
    if resource_kind:
        args.append(resource_kind); conds.append(f"s.resource_kind=${len(args)}")
    if q:
        args.append(f"%{q.strip().lower()}%")
        conds.append(f"(lower(s.title) LIKE ${len(args)} OR lower(s.description) LIKE ${len(args)})")
    args.extend([max(1, min(100, limit)), max(0, offset)])
    rows = await pool.fetch(
        f"""SELECT s.*, p.name AS provider_name, p.slug AS provider_slug,
                   p.rating AS provider_rating, p.success_rate AS provider_success_rate
            FROM mp_services s JOIN mp_providers p ON p.id=s.provider_id
            WHERE {' AND '.join(conds)}
            ORDER BY p.rating DESC, s.price_cents ASC
            LIMIT ${len(args)-1} OFFSET ${len(args)}""", *args)
    return [dict(r) for r in rows]


async def sync_services(pool, provider_id: int, items: list[dict],
                        actor_id: int | None = None) -> dict:
    """Идемпотентная массовая синхронизация каталога по external_id (Provider API).
    Существующие (по external_id) — обновляются, новые — создаются. Возвращает
    {created, updated}."""
    created = updated = 0
    for it in items or []:
        ext = str(it.get("external_id") or "").strip()
        if not ext:
            continue
        existing = await pool.fetchrow(
            "SELECT id FROM mp_services WHERE provider_id=$1 AND external_id=$2",
            provider_id, ext)
        fields = {k: it[k] for k in (
            "title", "description", "category", "resource_kind", "price_cents",
            "currency", "unit", "min_qty", "max_qty", "stock", "sla_hours",
            "params_schema") if k in it}
        if existing:
            await update_service(pool, existing["id"], provider_id,
                                 actor_id=actor_id, **fields)
            updated += 1
        else:
            await create_service(pool, provider_id, external_id=ext,
                                 actor_id=actor_id,
                                 title=fields.pop("title", ext), **fields)
            created += 1
    await audit(pool, "provider", provider_id, "catalog_sync", actor_id,
                {"created": created, "updated": updated})
    return {"created": created, "updated": updated}
