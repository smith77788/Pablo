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

import asyncpg

log = logging.getLogger(__name__)

# ── Snooze ────────────────────────────────────────────────────────────────────
# ЗАЧЕМ ЧЕРЕЗ БД. Заглушка хранилась ТОЛЬКО в этом словаре, а фоновый цикл
# читает её здесь же. Кнопку «😴 24ч» нажимают в боте — то есть в процессе,
# который обрабатывает апдейты; цикл же живёт в процессе-воркере (роль worker) и
# в его памяти словарь всегда пуст. Итог: бот отвечал «Уведомления отложены на
# 24ч», а уведомления продолжали приходить как ни в чём не бывало. Перезапуск
# сбрасывал заглушку даже в одном процессе.
#
# Теперь срок лежит в platform_settings (как у infra_copilot), а цикл
# перечитывает его каждый круг — заглушка работает и переживает рестарт.
_snooze_until: dict[int, float] = {}   # кэш процесса; истина — platform_settings
_SNOOZE_PREFIX = "eco_snooze_"


def snooze_ecosystem_alerts(owner_id: int, hours: float) -> float:
    """Заглушить в ПАМЯТИ процесса. Срок (unix) — для записи в БД."""
    import time

    exp = time.time() + hours * 3600
    _snooze_until[owner_id] = exp
    return exp


async def snooze_ecosystem_alerts_db(pool: asyncpg.Pool, owner_id: int,
                                     hours: float) -> float:
    """Заглушить НАДЁЖНО: и в памяти, и в БД (переживает рестарт и роли)."""
    from database import db as _db

    exp = snooze_ecosystem_alerts(owner_id, hours)
    await _db.set_platform_setting(pool, f"{_SNOOZE_PREFIX}{owner_id}", str(exp))
    return exp


async def clear_snooze_db(pool: asyncpg.Pool, owner_id: int) -> None:
    """Снять заглушку везде: иначе «Возобновить» сработало бы только локально."""
    from database import db as _db

    _snooze_until.pop(owner_id, None)
    await _db.set_platform_setting(pool, f"{_SNOOZE_PREFIX}{owner_id}", "0")


async def reload_snoozes_from_db(pool: asyncpg.Pool) -> None:
    """Подтянуть заглушки из БД (вызывается циклом на каждом круге)."""
    import time

    try:
        rows = await pool.fetch(
            "SELECT key, value FROM platform_settings WHERE key LIKE $1",
            f"{_SNOOZE_PREFIX}%")
    except Exception as e:
        # Молчание БД не должно превращаться в шквал уведомлений: оставляем то,
        # что уже знаем, и просто не обновляемся в этот круг.
        log.debug("ecosystem_copilot.reload_snoozes_from_db: %s", e)
        return
    now = time.time()
    for row in rows:
        try:
            owner_id = int(str(row["key"])[len(_SNOOZE_PREFIX):])
            exp = float(row["value"])
        except (ValueError, KeyError, TypeError):
            continue
        if exp > now:
            _snooze_until[owner_id] = exp
        else:
            _snooze_until.pop(owner_id, None)


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
    kb.button(text="😴 1ч", callback_data=EcoCb(action="eco_snooze", page=1))
    kb.button(text="😴 6ч", callback_data=EcoCb(action="eco_snooze", page=6))
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
                    alerts.append(
                        {
                            "name": name,
                            "issue": f"Критическое ухудшение здоровья: {health.overall:.0%}",
                            "suggestion": "Проверьте аккаунты и прокси экосистемы",
                        }
                    )

                # High pressure
                elif pressure.score >= 80:
                    alerts.append(
                        {
                            "name": name,
                            "issue": f"Критическое давление: {pressure.score}/100",
                            "suggestion": "Остановите часть операций или добавьте аккаунты",
                        }
                    )

                # Critical risk
                elif risk.level == "critical":
                    reason = (
                        risk.reasons[0]
                        if risk.reasons
                        else "Множественные факторы риска"
                    )
                    alerts.append(
                        {
                            "name": name,
                            "issue": f"Критический риск: {reason}",
                            "suggestion": "Откройте экосистему → Риски для деталей",
                        }
                    )

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
            # Заглушку ставят в ДРУГОМ процессе (бот), поэтому её надо перечитать,
            # иначе «отложено на 24ч» ничего не откладывает.
            await reload_snoozes_from_db(pool)
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
                            pool,
                            bot,
                            owner_id,
                            "restriction",
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
