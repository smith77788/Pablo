"""Аккаунты WB Chat: хранилище, шифрование сессий, ротация, бюджеты.

Зеркалит аккаунтный слой Telegram (account_manager/resource_selector/account_budget),
но поверх абстрактного транспорта. Сессии и прокси шифруются в покое через тот же
token_vault, что и Telegram-сессии. Ротация выбирает «здоровый» аккаунт вне
кулдауна и в рамках дневного бюджета — чтобы не ловить баны за перебор.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from services.token_vault import decrypt_token, encrypt_token

# Дневной лимит действий на аккаунт по умолчанию (консервативно, как у Telegram).
DEFAULT_DAILY_BUDGET = 50


# ── Преобразование строки БД → аккаунт (с расшифровкой секретов) ──────────────
def _row_to_account(row: asyncpg.Record | None) -> dict | None:
    if row is None:
        return None
    acc = dict(row)
    # Расшифровываем сессию/прокси при чтении; наружу — открытые значения.
    acc["session"] = decrypt_token(acc.pop("session_enc", "") or "") or ""
    acc["proxy"] = decrypt_token(acc.pop("proxy_enc", "") or "") or ""
    dev = acc.get("device")
    if isinstance(dev, str):
        try:
            acc["device"] = json.loads(dev)
        except Exception:  # noqa: BLE001
            acc["device"] = {}
    return acc


# ── CRUD ─────────────────────────────────────────────────────────────────────
async def upsert_account(
    pool: asyncpg.Pool,
    *,
    owner_id: int,
    phone: str,
    session: str,
    user_id: str = "",
    wb_id: str = "",
    name: str = "",
    proxy: str | None = None,
    device: dict | None = None,
    status: str = "active",
) -> dict:
    """Создать/обновить аккаунт после успешного входа. Секреты шифруются.

    Апсерт по (owner_id, phone): повторный вход тем же номером обновляет сессию,
    а не плодит дубликат."""
    row = await pool.fetchrow(
        """INSERT INTO wb_accounts
               (owner_id, phone, wb_id, user_id, name, session_enc, proxy_enc, device, status,
                last_used_at, updated_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9, NULL, NOW())
           ON CONFLICT (owner_id, phone) DO UPDATE
               SET wb_id       = COALESCE(NULLIF(EXCLUDED.wb_id, ''), wb_accounts.wb_id),
                   user_id     = COALESCE(NULLIF(EXCLUDED.user_id, ''), wb_accounts.user_id),
                   name        = COALESCE(NULLIF(EXCLUDED.name, ''), wb_accounts.name),
                   session_enc = EXCLUDED.session_enc,
                   proxy_enc   = COALESCE(EXCLUDED.proxy_enc, wb_accounts.proxy_enc),
                   device      = EXCLUDED.device,
                   status      = EXCLUDED.status,
                   updated_at  = NOW()
           RETURNING *""",
        owner_id, phone.strip(), wb_id, user_id, name,
        encrypt_token(session), encrypt_token(proxy) if proxy else None,
        json.dumps(device or {}), status,
    )
    return _row_to_account(row)


async def get_account(pool: asyncpg.Pool, account_id: int) -> dict | None:
    row = await pool.fetchrow("SELECT * FROM wb_accounts WHERE id = $1", account_id)
    return _row_to_account(row)


async def list_accounts(pool: asyncpg.Pool, owner_id: int) -> list[dict]:
    rows = await pool.fetch(
        "SELECT * FROM wb_accounts WHERE owner_id = $1 ORDER BY id", owner_id
    )
    return [_row_to_account(r) for r in rows]


async def set_status(
    pool: asyncpg.Pool,
    account_id: int,
    status: str,
    *,
    cooldown_seconds: int = 0,
    health_delta: int = 0,
) -> None:
    """Обновить статус/здоровье/кулдаун аккаунта (health зажат в 0..100)."""
    await pool.execute(
        """UPDATE wb_accounts
               SET status = $2,
                   health_score = GREATEST(0, LEAST(100, health_score + $3)),
                   cooldown_until = CASE WHEN $4 > 0 THEN NOW() + ($4 || ' seconds')::interval
                                         ELSE cooldown_until END,
                   updated_at = NOW()
               WHERE id = $1""",
        account_id, status, health_delta, cooldown_seconds,
    )


async def mark_used(pool: asyncpg.Pool, account_id: int) -> None:
    await pool.execute(
        "UPDATE wb_accounts SET last_used_at = NOW(), updated_at = NOW() WHERE id = $1",
        account_id,
    )


async def penalize(pool: asyncpg.Pool, account_id: int, *, seconds: int, banned: bool = False) -> None:
    """Наказать аккаунт после flood/ошибки: снизить здоровье и увести в кулдаун."""
    await set_status(
        pool,
        account_id,
        "banned" if banned else "flood",
        cooldown_seconds=seconds,
        health_delta=-25 if banned else -10,
    )


# ── Ротация ──────────────────────────────────────────────────────────────────
async def pick_account(
    pool: asyncpg.Pool,
    owner_id: int,
    *,
    action_type: str = "dm",
    daily_budget: int = DEFAULT_DAILY_BUDGET,
    exclude_ids: list[int] | None = None,
) -> dict | None:
    """Выбрать аккаунт для действия: активный, вне кулдауна, в рамках бюджета.

    Приоритет — «здоровье» ↓, затем давность использования ↑ (реже используемый
    берётся раньше). Возвращает аккаунт с расшифрованными секретами или None,
    если свободных нет (все в кулдауне/исчерпали дневной лимит)."""
    exclude = exclude_ids or [0]
    rows = await pool.fetch(
        """SELECT a.*,
                  COALESCE(b.used, 0) AS budget_used
               FROM wb_accounts a
               LEFT JOIN wb_account_budget b
                 ON b.account_id = a.id
                AND b.action_date = CURRENT_DATE
                AND b.action_type = $3
               WHERE a.owner_id = $1
                 AND a.status = 'active'
                 AND (a.cooldown_until IS NULL OR a.cooldown_until <= NOW())
                 AND a.id <> ALL($4::bigint[])
                 AND COALESCE(b.used, 0) < $2
               ORDER BY a.health_score DESC, a.last_used_at ASC NULLS FIRST
               LIMIT 1""",
        owner_id, daily_budget, action_type, exclude,
    )
    return _row_to_account(rows[0]) if rows else None


# ── Дневные бюджеты ──────────────────────────────────────────────────────────
async def incr_budget(pool: asyncpg.Pool, account_id: int, action_type: str, n: int = 1) -> int:
    """Увеличить счётчик действий за сегодня. Вернуть новое значение."""
    row = await pool.fetchrow(
        """INSERT INTO wb_account_budget (account_id, action_date, action_type, used)
               VALUES ($1, CURRENT_DATE, $2, $3)
           ON CONFLICT (account_id, action_date, action_type) DO UPDATE
               SET used = wb_account_budget.used + EXCLUDED.used
           RETURNING used""",
        account_id, action_type, n,
    )
    return int(row["used"]) if row else 0


async def budget_used(pool: asyncpg.Pool, account_id: int, action_type: str) -> int:
    val = await pool.fetchval(
        """SELECT used FROM wb_account_budget
               WHERE account_id = $1 AND action_date = CURRENT_DATE AND action_type = $2""",
        account_id, action_type,
    )
    return int(val or 0)
