"""Модуль Host-Server: покупаемый доступ + маркетплейс инфраструктуры.

Три сценария (по ТЗ):
  1. СДАТЬ в аренду свою инфраструктуру  → host_offerings (create_offering).
  2. ВЗЯТЬ в аренду чужую               → host_rentals (rent_offering).
  3. С СОГЛАСИЯ пользователя использовать его вычислительные мощности: на его
     устройстве ставится и работает инфраструктура арендаторов, а он получает
     арендную плату → offering kind='device_compute' + allow_device_compute=TRUE.

Модуль — покупаемая лицензия (как Strike): разовая оплата → host_server_access.
Цена РЕДАКТИРУЕМАЯ в рантайме через platform_settings (админ меняет без деплоя),
с фолбэком на env PRICE_HOST_SERVER и дефолт.

БЕЗОПАСНОСТЬ (важно): фактический запуск чужой инфраструктуры на устройстве
владельца (сценарий 3) — критичный по рискам слой (изоляция, доверие, RCE на
чужом железе). Здесь реализованы ТОЛЬКО модель данных, согласие, цена и учёт
аренды. Реальный исполнительный агент на устройстве вынесен за отдельный
security-гейт (``device_compute_execution_enabled`` → False), чтобы не выдавать
неготовое за готовое. Согласие (allow_device_compute) — необходимое, но не
достаточное условие: без включённого гейта ворклоады не запускаются.
"""

from __future__ import annotations

import logging
import os
import random
import string

import asyncpg

log = logging.getLogger(__name__)

# ── константы / словари ──────────────────────────────────────────────────────

PRICE_SETTING_KEY = "host_server_price_usd"
DEFAULT_PRICE_USD = 99

OFFERING_KINDS = ("proxy", "server", "device_compute")
PERIODS = ("hour", "day", "month")
_PERIOD_HOURS = {"hour": 1, "day": 24, "month": 24 * 30}

# статусы аренды и разрешённые переходы (кто их делает — проверяет вызывающий)
RENTAL_STATUSES = ("pending", "active", "ended", "cancelled", "rejected")
_PROVIDER_TRANSITIONS = {
    "pending": {"active", "rejected"},
    "active": {"ended"},
}
_TENANT_TRANSITIONS = {
    "pending": {"cancelled"},
    "active": {"cancelled"},
}

_CREATE_ACCESS_TABLE = """
CREATE TABLE IF NOT EXISTS host_server_access (
    user_id BIGINT PRIMARY KEY,
    purchased_at TIMESTAMPTZ DEFAULT now(),
    payment_ref TEXT,
    granted_by BIGINT
)
"""


# ── цена (редактируемая в рантайме) ──────────────────────────────────────────

def _env_price() -> int:
    raw = os.getenv("PRICE_HOST_SERVER")
    if raw is None or raw.strip() == "":
        return DEFAULT_PRICE_USD
    try:
        val = int(float(raw))
        return val if val > 0 else DEFAULT_PRICE_USD
    except (TypeError, ValueError):
        return DEFAULT_PRICE_USD


async def get_price(pool: asyncpg.Pool) -> int:
    """Текущая цена модуля в USD. Приоритет: platform_settings → env → дефолт."""
    try:
        from database import db
        raw = await db.get_platform_setting(pool, PRICE_SETTING_KEY, "")
    except Exception as e:
        log.debug("host_server.get_price setting read failed: %s", e)
        raw = ""
    if raw:
        try:
            val = int(float(raw))
            if val > 0:
                return val
        except (TypeError, ValueError):
            log.warning("host_server: bad stored price %r — fallback", raw)
    return _env_price()


async def set_price(pool: asyncpg.Pool, price_usd: int) -> int:
    """Задать цену модуля (админ). Возвращает применённое значение.

    Валидация: 1..100000 USD. Значение вне диапазона отклоняется (ValueError),
    чтобы опечатка не обнулила/не взорвала цену.
    """
    try:
        val = int(price_usd)
    except (TypeError, ValueError):
        raise ValueError("price must be an integer number of USD")
    if not (1 <= val <= 100_000):
        raise ValueError("price must be between 1 and 100000 USD")
    from database import db
    await db.set_platform_setting(pool, PRICE_SETTING_KEY, str(val))
    return val


# ── доступ к модулю (лицензия) ───────────────────────────────────────────────

async def _ensure_access_table(pool: asyncpg.Pool) -> None:
    try:
        await pool.execute(_CREATE_ACCESS_TABLE)
    except Exception as e:
        log.debug("host_server ensure access table: %s", e)


async def has_access(pool: asyncpg.Pool, user_id: int) -> bool:
    """Есть ли доступ к модулю: админ/enterprise — да; иначе запись о покупке."""
    try:
        from bot.utils.subscription import is_platform_admin, get_plan
        if is_platform_admin(user_id):
            return True
        try:
            if await get_plan(pool, user_id) == "enterprise":
                return True
        except Exception:
            pass
    except Exception:
        pass
    await _ensure_access_table(pool)
    try:
        row = await pool.fetchrow(
            "SELECT 1 FROM host_server_access WHERE user_id=$1", user_id
        )
    except Exception:
        row = None
    return row is not None


async def grant_access(
    pool: asyncpg.Pool, user_id: int, payment_ref: str | None = None,
    granted_by: int | None = None,
) -> None:
    """Выдать доступ (из payment_checker при подтверждении оплаты, или админом).

    Идемпотентно (ON CONFLICT DO NOTHING) — повторное подтверждение не создаёт дубль.
    """
    await _ensure_access_table(pool)
    await pool.execute(
        """INSERT INTO host_server_access (user_id, payment_ref, granted_by)
           VALUES ($1, $2, $3) ON CONFLICT (user_id) DO NOTHING""",
        user_id, payment_ref, granted_by,
    )


def gen_reference() -> str:
    return "HST-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))


async def create_purchase(pool: asyncpg.Pool, user_id: int) -> dict:
    """Создать запись оплаты за модуль (плата разовая, plan='host_server').

    Возвращает {reference, wallet, price_usd}. Активация — в payment_checker при
    подтверждении транзакции (как у strike). period_months=0 → это не подписка.
    """
    if await has_access(pool, user_id):
        return {"already": True}
    price = await get_price(pool)
    wallet = os.getenv("TRON_WALLET", "") or "NOT_CONFIGURED"
    ref = gen_reference()
    for _ in range(5):
        exists = await pool.fetchrow("SELECT id FROM payments WHERE reference=$1", ref)
        if not exists:
            break
        ref = gen_reference()
    await pool.execute(
        """INSERT INTO payments
               (user_id, plan, period_months, currency, amount_crypto, amount_usd,
                wallet_address, reference)
           VALUES ($1, 'host_server', 0, 'USDT_TRC20', $2, $3, $4, $5)
           ON CONFLICT (reference) DO NOTHING""",
        user_id, float(price), float(price), wallet, ref,
    )
    return {"already": False, "reference": ref, "wallet": wallet, "price_usd": price}


# ── маркетплейс: предложения (сдать) ─────────────────────────────────────────

def _norm_kind(kind: str) -> str:
    k = (kind or "").strip().lower()
    return k if k in OFFERING_KINDS else "server"


def _norm_period(period: str) -> str:
    p = (period or "").strip().lower()
    return p if p in PERIODS else "month"


async def create_offering(
    pool: asyncpg.Pool,
    owner_id: int,
    *,
    kind: str,
    title: str,
    price_usd: float,
    period: str = "month",
    description: str | None = None,
    specs: dict | None = None,
    region: str | None = None,
    allow_device_compute: bool = False,
) -> dict:
    """Опубликовать предложение аренды. Требует доступ к модулю.

    Для kind='device_compute' обязательно явное согласие allow_device_compute —
    иначе ValueError (нельзя молча выставить чужое устройство под нагрузку).
    """
    if not await has_access(pool, owner_id):
        raise PermissionError("нет доступа к модулю Host-Server")
    kind = _norm_kind(kind)
    period = _norm_period(period)
    title = (title or "").strip()
    if not title:
        raise ValueError("title required")
    try:
        price = round(float(price_usd), 2)
    except (TypeError, ValueError):
        raise ValueError("price_usd must be a number")
    if price < 0:
        raise ValueError("price_usd must be >= 0")
    if kind == "device_compute" and not allow_device_compute:
        raise ValueError(
            "device_compute требует явного согласия (allow_device_compute=True)"
        )
    import json as _json
    row = await pool.fetchrow(
        """INSERT INTO host_offerings
               (owner_id, kind, title, description, specs, region,
                price_usd, period, allow_device_compute)
           VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9)
           RETURNING id""",
        owner_id, kind, title[:200], (description or "")[:2000],
        _json.dumps(specs or {}), (region or None),
        price, period, bool(allow_device_compute),
    )
    return {"id": row["id"], "kind": kind, "period": period, "price_usd": price}


async def list_market(
    pool: asyncpg.Pool,
    *,
    viewer_id: int | None = None,
    kind: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Витрина ЧУЖИХ активных предложений (viewer видит не свои)."""
    limit = max(1, min(200, int(limit or 50)))
    conds = ["o.is_active=TRUE"]
    args: list = []
    if viewer_id is not None:
        args.append(viewer_id)
        conds.append(f"o.owner_id <> ${len(args)}")
    if kind:
        args.append(_norm_kind(kind))
        conds.append(f"o.kind = ${len(args)}")
    args.append(limit)
    rows = await pool.fetch(
        f"""SELECT o.id, o.owner_id, o.kind, o.title, o.description, o.specs,
                   o.region, o.price_usd, o.period, o.allow_device_compute, o.created_at
            FROM host_offerings o
            WHERE {' AND '.join(conds)}
            ORDER BY o.created_at DESC
            LIMIT ${len(args)}""",
        *args,
    )
    return [dict(r) for r in rows]


async def my_offerings(pool: asyncpg.Pool, owner_id: int) -> list[dict]:
    rows = await pool.fetch(
        """SELECT id, kind, title, description, specs, region, price_usd, period,
                  is_active, allow_device_compute, created_at
           FROM host_offerings WHERE owner_id=$1 ORDER BY created_at DESC""",
        owner_id,
    )
    return [dict(r) for r in rows]


async def set_offering_active(
    pool: asyncpg.Pool, owner_id: int, offering_id: int, active: bool
) -> bool:
    """Включить/снять предложение (только владелец). True если строка затронута."""
    res = await pool.execute(
        """UPDATE host_offerings SET is_active=$3, updated_at=now()
           WHERE id=$1 AND owner_id=$2""",
        offering_id, owner_id, bool(active),
    )
    # asyncpg возвращает 'UPDATE N'; затронута ≥1 строка → успех
    try:
        return isinstance(res, str) and int(res.rsplit(" ", 1)[-1]) > 0
    except (ValueError, IndexError):
        return False


# ── маркетплейс: аренда (взять) ──────────────────────────────────────────────

async def rent_offering(
    pool: asyncpg.Pool, tenant_id: int, offering_id: int, *, units: int = 1
) -> dict:
    """Арендовать чужое предложение. Создаёт заявку аренды в статусе pending.

    Правила: предложение существует и активно; нельзя арендовать своё;
    цена фиксируется снапшотом; ends_at рассчитывается от периода×units.
    Провайдер потом подтверждает (set_rental_status → active).
    """
    try:
        units = max(1, min(1000, int(units)))
    except (TypeError, ValueError):
        units = 1
    off = await pool.fetchrow(
        """SELECT id, owner_id, price_usd, period, is_active
           FROM host_offerings WHERE id=$1""",
        offering_id,
    )
    if not off:
        raise ValueError("предложение не найдено")
    if not off["is_active"]:
        raise ValueError("предложение неактивно")
    if off["owner_id"] == tenant_id:
        raise ValueError("нельзя арендовать собственное предложение")
    period = off["period"]
    total_price = round(float(off["price_usd"]) * units, 2)
    hours = _PERIOD_HOURS.get(period, _PERIOD_HOURS["month"]) * units
    row = await pool.fetchrow(
        """INSERT INTO host_rentals
               (offering_id, tenant_id, provider_id, status, period, units,
                price_usd, ends_at)
           VALUES ($1,$2,$3,'pending',$4,$5,$6, now() + ($7 || ' hours')::interval)
           RETURNING id, ends_at""",
        offering_id, tenant_id, off["owner_id"], period, units,
        total_price, str(hours),
    )
    return {
        "id": row["id"], "status": "pending", "price_usd": total_price,
        "period": period, "units": units, "provider_id": off["owner_id"],
    }


async def my_rentals(pool: asyncpg.Pool, tenant_id: int) -> list[dict]:
    """Аренды, которые Я взял."""
    rows = await pool.fetch(
        """SELECT r.id, r.offering_id, r.provider_id, r.status, r.period, r.units,
                  r.price_usd, r.started_at, r.ends_at, r.created_at,
                  o.title, o.kind
           FROM host_rentals r JOIN host_offerings o ON o.id=r.offering_id
           WHERE r.tenant_id=$1 ORDER BY r.created_at DESC""",
        tenant_id,
    )
    return [dict(r) for r in rows]


async def incoming_rentals(pool: asyncpg.Pool, provider_id: int) -> list[dict]:
    """Заявки на МОИ предложения (я — провайдер)."""
    rows = await pool.fetch(
        """SELECT r.id, r.offering_id, r.tenant_id, r.status, r.period, r.units,
                  r.price_usd, r.started_at, r.ends_at, r.created_at,
                  o.title, o.kind
           FROM host_rentals r JOIN host_offerings o ON o.id=r.offering_id
           WHERE r.provider_id=$1 ORDER BY r.created_at DESC""",
        provider_id,
    )
    return [dict(r) for r in rows]


async def set_rental_status(
    pool: asyncpg.Pool, user_id: int, rental_id: int, new_status: str, *, as_provider: bool
) -> dict:
    """Сменить статус аренды с проверкой прав и допустимости перехода.

    as_provider=True — действует владелец предложения (подтвердить/отклонить/завершить);
    as_provider=False — арендатор (отменить). Возвращает {ok, status} или бросает.
    """
    new_status = (new_status or "").strip().lower()
    if new_status not in RENTAL_STATUSES:
        raise ValueError("неизвестный статус")
    scope_col = "provider_id" if as_provider else "tenant_id"
    row = await pool.fetchrow(
        f"SELECT status FROM host_rentals WHERE id=$1 AND {scope_col}=$2",
        rental_id, user_id,
    )
    if not row:
        raise PermissionError("аренда не найдена или нет прав")
    cur = row["status"]
    allowed = (_PROVIDER_TRANSITIONS if as_provider else _TENANT_TRANSITIONS).get(cur, set())
    if new_status not in allowed:
        raise ValueError(f"переход {cur}→{new_status} недопустим")
    # started_at выставляем при активации
    if new_status == "active":
        await pool.execute(
            """UPDATE host_rentals SET status=$2, started_at=COALESCE(started_at, now()),
                   updated_at=now() WHERE id=$1""",
            rental_id, new_status,
        )
    else:
        await pool.execute(
            "UPDATE host_rentals SET status=$2, updated_at=now() WHERE id=$1",
            rental_id, new_status,
        )
    return {"ok": True, "status": new_status}


# ── сценарий 3: исполнение на устройстве владельца (SECURITY-ГЕЙТ) ────────────

def device_compute_execution_enabled() -> bool:
    """Включён ли РЕАЛЬНЫЙ запуск чужой инфры на устройстве владельца.

    Дизайн исполнительного слоя (для ревью, не для прода) —
    docs/HOST_SERVER_DEVICE_AGENT_DESIGN.md.

    Отдельный security-гейт (env HOST_SERVER_DEVICE_EXEC=1). По умолчанию False:
    модель данных, согласие и оплата аренды работают, но фактический
    исполнительный агент на чужом железе не активируется, пока слой изоляции
    не пройдёт ревью. has_access/consent — необходимы, но не достаточны.
    """
    return os.getenv("HOST_SERVER_DEVICE_EXEC", "").strip().lower() in ("1", "true", "yes")
