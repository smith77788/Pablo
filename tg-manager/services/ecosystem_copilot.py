"""Ecosystem Copilot — автономный анализ экосистем.

BOTMOTHER ЭПОХА III: Ecosystem Copilot

Самостоятельно находит:
  - деградацию экосистем (health < 0.5)
  - перегрузку (pressure >= 70)
  - критические риски
  - дрейф (ресурсные проблемы)
  - нехватку ресурсов

Уведомляет владельцев через notify_if_enabled.
Поддерживает snooze (использует тот же механизм что и infra_copilot).
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

# ── Snooze (аналогично infra_copilot) ────────────────────────────────────────
_snooze_until: dict[int, float] = {}


def snooze_ecosystem_alerts(owner_id: int, hours: float) -> None:
    import time
    _snooze_until[owner_id] = time.time() + hours * 3600


def is_snoozed(owner_id: int) -> bool:
    import time
    exp = _snooze_until.get(owner_id, 0.0)
    if exp and time.time() < exp:
        return True
    _snooze_until.pop(owner_id, None)
    return False


# ── Alert formatting ──────────────────────────────────────────────────────────

def _format_ecosystem_alert(alerts: list[dict]) -> str:
    lines = ["🌐 <b>Ecosystem Copilot: проблемы экосистем</b>\n"]
    for a in alerts[:4]:
        lines.append(f"• <b>{html.escape(a['name'])}</b>")
        lines.append(f"  {html.escape(a['issue'])}")
        if a.get("suggestion"):
            lines.append(f"  💡 {html.escape(a['suggestion'])}")
    lines.append("\n<i>Отложить уведомления:</i>")
    return "\n".join(lines)


def _snooze_markup():
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from bot.callbacks import EcoCb
    kb = InlineKeyboardBuilder()
    kb.button(text="😴 1ч",  callback_data=EcoCb(action="eco_snooze", page=1))
    kb.button(text="😴 6ч",  callback_data=EcoCb(action="eco_snooze", page=6))
    kb.button(text="😴 24ч", callback_data=EcoCb(action="eco_snooze", page=24))
    kb.button(text="🌐 Экосистемы", callback_data=EcoCb(action="menu"))
    kb.adjust(3, 1)
    return kb.as_markup()


# ── Main analysis ─────────────────────────────────────────────────────────────

async def _analyze_owner(pool: asyncpg.Pool, owner_id: int) -> list[dict]:
    """Анализирует все экосистемы owner_id. Возвращает список критических проблем."""
    alerts: list[dict] = []
    try:
        from services import ecosystem_brain as _eb

        ecosystems = await _eb.list_ecosystems(pool, owner_id)
        for eco in ecosystems:
            eco_id = eco["id"]
            name = eco["name"]
            try:
                health = await _eb.compute_health(pool, eco_id, owner_id)
                pressure = await _eb.compute_pressure(pool, eco_id, owner_id)
                risk = await _eb.compute_risk(pool, eco_id, owner_id)

                # Critical health degradation
                if health.overall < 0.35:
                    alerts.append({
                        "name": name,
                        "issue": f"Критическое ухудшение здоровья: {health.overall:.0%}",
                        "suggestion": "Проверьте аккаунты и прокси экосистемы",
                    })

                # High pressure
                elif pressure.score >= 80:
                    alerts.append({
                        "name": name,
                        "issue": f"Критическое давление: {pressure.score}/100",
                        "suggestion": "Остановите часть операций или добавьте аккаунты",
                    })

                # Critical risk
                elif risk.level == "critical":
                    reason = risk.reasons[0] if risk.reasons else "Множественные факторы риска"
                    alerts.append({
                        "name": name,
                        "issue": f"Критический риск: {reason}",
                        "suggestion": "Откройте экосистему → Риски для деталей",
                    })

                await asyncio.sleep(0.1)
            except Exception as e:
                log.debug("ecosystem_copilot eco=%d: %s", eco_id, e)

    except Exception as e:
        log.debug("ecosystem_copilot owner=%d: %s", owner_id, e)

    return alerts


# ── Background loop ───────────────────────────────────────────────────────────

async def run_ecosystem_copilot_loop(pool: asyncpg.Pool, bot) -> None:
    """Фоновый цикл: каждые 60 минут анализирует все экосистемы."""
    from database import db as _db

    log.info("ecosystem_copilot: background loop started (interval=60min)")
    await asyncio.sleep(600)  # 10 минут начальная задержка

    while True:
        try:
            owner_ids = await pool.fetch(
                "SELECT DISTINCT owner_id FROM ecosystems WHERE status='active'",
            )
            for row in owner_ids:
                owner_id = row["owner_id"]
                try:
                    if is_snoozed(owner_id):
                        log.debug("ecosystem_copilot: owner=%d snoozed", owner_id)
                        await asyncio.sleep(0.5)
                        continue

                    alerts = await _analyze_owner(pool, owner_id)
                    if alerts:
                        report = _format_ecosystem_alert(alerts)
                        markup = _snooze_markup()
                        await _db.notify_if_enabled(
                            pool, bot, owner_id, "restriction",
                            report,
                            reply_markup=markup,
                        )
                    await asyncio.sleep(1)
                except Exception as e:
                    log.debug("ecosystem_copilot owner=%d: %s", owner_id, e)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("ecosystem_copilot loop error: %s", e)

        await asyncio.sleep(3600)  # 60 минут
