"""Account Shield — proactive ban prediction and auto-cooling.

Background worker evaluates every active account every 30 minutes using
Physics Engine risk scores.  Depending on thresholds it:
  ok    — no action needed
  warn  — just logs a shield_action (no change to account)
  cool  — lowers account priority (sets a soft cooldown via tg_accounts)
  pause — sets is_active=FALSE for cool_duration_hours, notifies admin
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import asyncpg

from services import physics_engine

log = logging.getLogger(__name__)

_LOOP_INTERVAL = 1800  # 30 minutes


# ─── Config dataclass ─────────────────────────────────────────────────────────


@dataclass
class ShieldConfig:
    risk_threshold: float = 0.7
    ban_prob_threshold: float = 0.5
    auto_pause: bool = True
    notify_admin: bool = True
    cool_duration_hours: int = 24


# ─── Decision dataclass ───────────────────────────────────────────────────────


@dataclass
class ShieldDecision:
    account_id: int
    risk_score: float
    ban_probability: float
    action: str  # "ok" | "warn" | "cool" | "pause"
    note: str = ""


# ─── DB helpers ───────────────────────────────────────────────────────────────


async def get_shield_config(pool: asyncpg.Pool, owner_id: int) -> ShieldConfig:
    """Read per-owner shield config; return defaults if not yet configured."""
    try:
        row = await pool.fetchrow(
            """SELECT risk_threshold, ban_prob_threshold, auto_pause,
                      notify_admin, cool_duration_hours
               FROM shield_configs WHERE owner_id = $1""",
            owner_id,
        )
        if row:
            return ShieldConfig(
                risk_threshold=float(row["risk_threshold"]),
                ban_prob_threshold=float(row["ban_prob_threshold"]),
                auto_pause=bool(row["auto_pause"]),
                notify_admin=bool(row["notify_admin"]),
                cool_duration_hours=int(row["cool_duration_hours"]),
            )
    except Exception as exc:
        log.debug("account_shield.get_shield_config owner=%d: %s", owner_id, exc)
    return ShieldConfig()


async def _save_action(
    pool: asyncpg.Pool,
    owner_id: int,
    decision: ShieldDecision,
) -> None:
    try:
        await pool.execute(
            """INSERT INTO shield_actions
               (owner_id, account_id, action, risk_score, ban_probability, note)
               VALUES ($1,$2,$3,$4,$5,$6)""",
            owner_id,
            decision.account_id,
            decision.action,
            round(decision.risk_score, 4),
            round(decision.ban_probability, 4),
            decision.note or None,
        )
    except Exception as exc:
        log.debug(
            "account_shield._save_action acc=%d: %s", decision.account_id, exc
        )


# ─── Core evaluation ──────────────────────────────────────────────────────────


async def evaluate_account(
    pool: asyncpg.Pool,
    account_id: int,
    owner_id: int,
) -> ShieldDecision:
    """Evaluate one account and return the Shield decision.

    Uses physics_engine.get_account_risk() as the data source.
    Never raises.
    """
    risk = await physics_engine.get_account_risk(pool, account_id)
    risk_score = float(risk.get("risk_score") or 0.0)
    ban_prob = float(risk.get("ban_probability") or 0.0)
    flood_rate = float(risk.get("flood_rate_1h") or 0.0)

    cfg = await get_shield_config(pool, owner_id)

    if risk_score >= cfg.risk_threshold or ban_prob >= cfg.ban_prob_threshold:
        if ban_prob >= 0.8 or risk_score >= 0.9:
            action = "pause"
            note = (
                f"risk={risk_score:.2f} ban_prob={ban_prob:.2f} "
                f"flood_rate_1h={flood_rate:.2f} — автопауза"
            )
        else:
            action = "cool"
            note = (
                f"risk={risk_score:.2f} ban_prob={ban_prob:.2f} "
                f"flood_rate_1h={flood_rate:.2f} — охлаждение"
            )
    elif risk_score >= cfg.risk_threshold * 0.7 or ban_prob >= cfg.ban_prob_threshold * 0.7:
        action = "warn"
        note = (
            f"risk={risk_score:.2f} ban_prob={ban_prob:.2f} "
            f"flood_rate_1h={flood_rate:.2f} — предупреждение"
        )
    else:
        action = "ok"
        note = ""

    return ShieldDecision(
        account_id=account_id,
        risk_score=risk_score,
        ban_probability=ban_prob,
        action=action,
        note=note,
    )


# ─── Decision application ─────────────────────────────────────────────────────


async def apply_shield_decision(
    pool: asyncpg.Pool,
    decision: ShieldDecision,
    owner_id: int,
) -> None:
    """Apply the shield decision to tg_accounts and record the action.

    cool  — extend cooldown_until by 2h to lower effective priority
    pause — set is_active=FALSE for cool_duration_hours
    ok/warn — no structural change; just record the action
    """
    cfg = await get_shield_config(pool, owner_id)

    if decision.action == "cool":
        try:
            await pool.execute(
                """UPDATE tg_accounts
                   SET cooldown_until = GREATEST(
                           COALESCE(cooldown_until, NOW()),
                           NOW() + INTERVAL '2 hours'
                       ),
                       status_reason = $2
                   WHERE id = $1""",
                decision.account_id,
                decision.note or "Account Shield: охлаждение",
            )
        except Exception as exc:
            log.debug(
                "account_shield.apply cool acc=%d: %s", decision.account_id, exc
            )

    elif decision.action == "pause" and cfg.auto_pause:
        resume_at = datetime.now(timezone.utc) + timedelta(
            hours=cfg.cool_duration_hours
        )
        try:
            await pool.execute(
                """UPDATE tg_accounts
                   SET is_active = FALSE,
                       cooldown_until = $2,
                       status_reason = $3
                   WHERE id = $1""",
                decision.account_id,
                resume_at,
                decision.note or f"Account Shield: пауза на {cfg.cool_duration_hours}ч",
            )
        except Exception as exc:
            log.debug(
                "account_shield.apply pause acc=%d: %s", decision.account_id, exc
            )

    await _save_action(pool, owner_id, decision)


# ─── Background worker ────────────────────────────────────────────────────────


async def run(pool: asyncpg.Pool, bot) -> None:
    """Main loop: evaluate all active accounts every 30 minutes.

    For each owner:
      1. Fetch active accounts
      2. Evaluate each with physics_engine risk
      3. Apply decisions
      4. Notify admin (via bot.send_message) for "pause" actions if notify_admin=True
    """
    log.info("Account Shield started")
    while True:
        try:
            await _run_cycle(pool, bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Account Shield loop error: %s", exc)
        await asyncio.sleep(_LOOP_INTERVAL)


async def _run_cycle(pool: asyncpg.Pool, bot) -> None:
    # Gather all distinct owner_ids with active accounts
    try:
        owners = await pool.fetch(
            "SELECT DISTINCT owner_id FROM tg_accounts WHERE is_active=TRUE"
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.debug("account_shield._run_cycle owners: %s", exc)
        return

    paused_total = 0
    cooled_total = 0

    for owner_row in owners:
        owner_id: int = owner_row["owner_id"]
        try:
            accounts = await pool.fetch(
                """SELECT id FROM tg_accounts
                   WHERE owner_id=$1 AND is_active=TRUE""",
                owner_id,
            )
        except Exception as exc:
            log.debug(
                "account_shield._run_cycle accounts owner=%d: %s", owner_id, exc
            )
            continue

        cfg = await get_shield_config(pool, owner_id)

        for acc_row in accounts:
            account_id: int = acc_row["id"]
            try:
                decision = await evaluate_account(pool, account_id, owner_id)
                if decision.action in ("cool", "pause", "warn"):
                    await apply_shield_decision(pool, decision, owner_id)
                    if decision.action == "pause":
                        paused_total += 1
                        if cfg.notify_admin and bot is not None:
                            await _notify_pause(
                                bot, pool, owner_id, decision, cfg
                            )
                    elif decision.action == "cool":
                        cooled_total += 1
                # don't record "ok" to avoid bloating shield_actions table
            except Exception as exc:
                log.debug(
                    "account_shield: eval error acc=%d: %s", account_id, exc
                )
            await asyncio.sleep(0.05)

    if paused_total or cooled_total:
        log.info(
            "Account Shield cycle: paused=%d cooled=%d", paused_total, cooled_total
        )


async def _notify_pause(
    bot,
    pool: asyncpg.Pool,
    owner_id: int,
    decision: ShieldDecision,
    cfg: ShieldConfig,
) -> None:
    """Send a Telegram notification to the owner about a paused account."""
    try:
        acc = await pool.fetchrow(
            "SELECT phone, first_name, username FROM tg_accounts WHERE id=$1",
            decision.account_id,
        )
        if acc:
            name = (
                f"@{acc['username']}"
                if acc.get("username")
                else (acc.get("first_name") or acc.get("phone") or f"id{decision.account_id}")
            )
        else:
            name = f"id{decision.account_id}"

        text = (
            "🛡 <b>Account Shield — автопауза</b>\n\n"
            f"Аккаунт <b>{name}</b> поставлен на паузу.\n\n"
            f"📊 Risk score: <b>{decision.risk_score:.0%}</b>\n"
            f"⚠️ Вероятность бана: <b>{decision.ban_probability:.0%}</b>\n"
            f"⏸ Пауза на: <b>{cfg.cool_duration_hours}ч</b>\n\n"
            f"<i>{decision.note}</i>"
        )
        await bot.send_message(owner_id, text, parse_mode="HTML")
    except Exception as exc:
        log.debug("account_shield._notify_pause owner=%d: %s", owner_id, exc)
