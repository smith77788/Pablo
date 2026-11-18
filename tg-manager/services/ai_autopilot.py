"""Автопилот ИИ-ассистента: исполнять действия сразу, а не только черновиком.

Владелец просил, чтобы наш ИИ реально ЗАПУСКАЛ действия, а не только давал
рекомендации и черновики операций с ручным подтверждением. Этот флаг — на
владельца, и по умолчанию ВЫКЛЮЧЕН: действия ассистента ставят массовые
операции по живым аккаунтам, поэтому право на авто-пуск даёт только сам
владелец (включает тумблером в чате ассистента).

Важно: автопилот снимает РОВНО шаг ручного подтверждения в чате. Сами действия
идут через те же пути (bot/utils/ai_tools.execute_action → operation_bus), то
есть защитные гейты исполнителей (канарейка, идемпотентность, карантин,
FloodWait, честный лог) сохраняются полностью. Это не «ИИ делает что угодно без
границ», а «владелец разрешил не переспрашивать на каждое действие».
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def is_enabled(pool, owner_id: int) -> bool:
    """Включён ли автопилот ИИ у этого владельца. Фейлится закрыто (False):
    при любом сбое чтения действуем как раньше — спрашиваем подтверждение."""
    try:
        val = await pool.fetchval(
            "SELECT ai_autopilot FROM platform_users WHERE user_id=$1", int(owner_id))
        return bool(val)
    except Exception as e:
        log.debug("ai_autopilot.is_enabled owner=%s: %s", owner_id, e)
        return False


async def set_enabled(pool, owner_id: int, on: bool) -> bool:
    """Включить/выключить автопилот. Возвращает фактическое состояние.

    platform_users заводится при первом входе владельца; на всякий случай
    делаем UPSERT, чтобы тумблер работал и до того, как строка появилась."""
    try:
        await pool.execute(
            "INSERT INTO platform_users(user_id, ai_autopilot) VALUES($1,$2) "
            "ON CONFLICT(user_id) DO UPDATE SET ai_autopilot=$2",
            int(owner_id), bool(on))
        return bool(on)
    except Exception as e:
        log.warning("ai_autopilot.set_enabled owner=%s on=%s: %s", owner_id, on, e)
        # Не удалось записать — сообщаем честное текущее состояние из базы.
        return await is_enabled(pool, owner_id)
