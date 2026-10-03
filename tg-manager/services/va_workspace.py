"""Модель чтения экрана канала: строгая основа и независимые доп. разделы."""
from __future__ import annotations

import asyncio

from services import channel_admin as ca
from services import channel_brain_store as cbs
from services import va_references

SECTION_TIMEOUT_SECONDS = 3.0


async def load_workspace(pool, owner_id: int, channel_id: int) -> dict | None:
    """Вернуть прежний контракт экрана с предупреждениями или None для чужого канала."""
    channel = await ca.channel_row(pool, owner_id, channel_id)
    if not channel:
        return None

    admin = await ca.get_admin(pool, owner_id, channel_id)
    # get_profile скрывает ошибки БД; здесь отсутствие политики и сбой различаются.
    row = await pool.fetchrow(
        "SELECT owner_id, channel_key, brand_rules, pillars, mix_weights, "
        "autonomy_mode, max_streak, dup_threshold "
        "FROM va_channel_brain WHERE owner_id=$1 AND channel_key=$2",
        owner_id, str(channel_id),
    )
    brain = cbs._row_to_brain(row) if row else None
    warnings: list[str] = []
    payload = {
        "ok": True,
        "channel": {"id": str(channel_id), "title": channel.get("title") or "",
                    "username": channel.get("username") or ""},
        "settings": ca.settings_public(admin),
        "pillars": cbs.to_public(brain)["pillars"],
        "plan": [],
        "drafts": [],
        "report": None,
        "events": [],
        "references": [],
        "warnings": warnings,
    }
    if not admin:
        return payload

    async def load_section(key, label, reader):
        try:
            value = await asyncio.wait_for(
                reader(pool, owner_id, channel_id), timeout=SECTION_TIMEOUT_SECONDS,
            )
            return key, value, None
        except TimeoutError:
            return key, payload[key], f"{label}: превышено время ожидания загрузки."
        except Exception:  # noqa: BLE001 - отказ одного доп. раздела не роняет экран
            return key, payload[key], f"{label}: не удалось загрузить раздел."

    sections = (
        ("plan", "План публикаций", ca.get_plan),
        ("drafts", "Черновики", ca.list_drafts),
        ("report", "Отчёт", ca.channel_report),
        ("events", "Журнал", ca.events),
        ("references", "Каналы-образцы", va_references.list_refs),
    )
    tasks = [asyncio.create_task(load_section(*section)) for section in sections]
    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        # gather не отменяет соседей при отмене одного дочернего чтения.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    for key, value, warning in results:
        payload[key] = value
        if warning is not None:
            warnings.append(warning)
    return payload
