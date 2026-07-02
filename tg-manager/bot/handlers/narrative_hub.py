"""Narrative Hub — Cross-Network Narrative Engine UI.

Координированные кампании для создания органических трендов.
Несколько каналов подхватывают одну тему с разных углов в течение 2–6 часов.

Entry: NarrCb(action="menu")
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import NarrCb, BmCb
from bot.states import NarrativeFSM
from services import narrative_engine
from services.ai_providers import configured_providers
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)
router = Router(name="narrative_hub")

# ── Constants ─────────────────────────────────────────────────────────────────

_STATUS_ICONS = {
    "draft":     "📝",
    "active":    "🟢",
    "paused":    "⏸",
    "completed": "✅",
    "cancelled": "🚫",
}

_STATUS_LABELS = {
    "draft":     "Черновик",
    "active":    "Активна",
    "paused":    "Пауза",
    "completed": "Завершена",
    "cancelled": "Отменена",
}

_TYPE_LABELS = {
    "trend":     "🌊 Тренд",
    "launch":    "🚀 Запуск",
    "awareness": "📢 Осведомлённость",
    "counter":   "⚔️ Контр-нарратив",
}

_SPREAD_OPTIONS = [2, 3, 4, 6, 8, 12]

_ANGLE_ICONS = {
    "news":     "📰",
    "expert":   "🎓",
    "story":    "📖",
    "stats":    "📊",
    "opinion":  "💬",
    "question": "❓",
    "review":   "🔍",
    "trend":    "📈",
}

_POST_STATUS_ICONS = {
    "pending":   "⏳",
    "published": "✅",
    "failed":    "❌",
    "cancelled": "🚫",
}


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _edit(cb: CallbackQuery, text: str, markup=None) -> None:
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception as e:
        err = str(e).lower()
        if "message is not modified" in err:
            return
        if "there is no text in the message to edit" in err:
            try:
                await cb.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=markup)
                return
            except Exception:
                pass
        if "message to edit not found" in err or "message can't be edited" in err:
            await cb.bot.send_message(cb.from_user.id, text, parse_mode="HTML", reply_markup=markup)
        else:
            log.warning("narrative_hub _edit error: %s", e)


def _back_btn() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К кампаниям", callback_data=NarrCb(action="menu"))
    return kb


def _format_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%d.%m %H:%M")


async def _get_user_channels(pool: asyncpg.Pool, owner_id: int) -> list[dict]:
    """Возвращает каналы пользователя из managed_channels."""
    rows = await pool.fetch(
        "SELECT * FROM managed_channels WHERE owner_id=$1 ORDER BY title",
        owner_id,
    )
    return [dict(r) for r in rows]


# ── Menu ──────────────────────────────────────────────────────────────────────


@router.callback_query(NarrCb.filter(F.action == "menu"))
async def cb_narr_menu(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()

    try:
        campaigns = await narrative_engine.list_campaigns(pool, callback.from_user.id, limit=15)
    except Exception as e:
        log.error("narrative_hub cb_narr_menu: %s", e)
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=BmCb(action="growth"))
        await callback.message.edit_text(
            "📖 <b>Narrative Engine</b>\n\n"
            "⚠️ Модуль недоступен — таблицы не созданы в базе данных.\n\n"
            "Администратору необходимо применить миграцию <code>schema_v121.sql</code>.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    active = [c for c in campaigns if c["status"] == "active"]
    paused = [c for c in campaigns if c["status"] == "paused"]
    completed = [c for c in campaigns if c["status"] == "completed"]

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Новая кампания", callback_data=NarrCb(action="create"))

    # Активные кампании
    for c in active + paused:
        icon = _STATUS_ICONS.get(c["status"], "❓")
        pub = c.get("posts_published", 0)
        total = c.get("posts_total", 0)
        label = f"{icon} {html.escape(c['topic'][:30])} [{pub}/{total}]"
        kb.button(text=label, callback_data=NarrCb(action="detail", campaign_id=c["id"]))

    # Завершённые (последние 5)
    for c in completed[:5]:
        icon = _STATUS_ICONS.get(c["status"], "❓")
        label = f"{icon} {html.escape(c['topic'][:30])}"
        kb.button(text=label, callback_data=NarrCb(action="detail", campaign_id=c["id"]))

    kb.button(text="◀️ Назад", callback_data=BmCb(action="growth"))
    kb.adjust(1)

    active_cnt = len(active)
    total_cnt = len(campaigns)

    text = (
        "🌊 <b>Narrative Engine</b>\n\n"
        "Координированное создание трендов: несколько каналов подхватывают одну тему\n"
        "с разных углов в течение 2–6 часов, создавая эффект органического тренда.\n\n"
        f"Кампаний: <b>{total_cnt}</b>  |  Активных: <b>{active_cnt}</b>"
    )
    await _edit(callback, text, kb.as_markup())


# ── Create — Step 1: Topic ─────────────────────────────────────────────────────


@router.callback_query(NarrCb.filter(F.action == "create"))
async def cb_narr_create(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(NarrativeFSM.waiting_topic)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=NarrCb(action="menu"))

    await _edit(
        callback,
        "🌊 <b>Новая нарративная кампания</b>\n\n"
        "<b>Шаг 1/5 — Тема</b>\n\n"
        "Введите тему кампании. Это ключевая идея, которую все каналы будут продвигать.\n\n"
        "<i>Пример: «Рост цен на недвижимость в 2025» или «Запуск нового продукта X»</i>",
        kb.as_markup(),
    )


@router.message(NarrativeFSM.waiting_topic, F.text)
async def msg_narr_topic(message: Message, state: FSMContext) -> None:
    topic = (message.text or "").strip()
    if not topic or len(topic) > 256:
        await message.answer("⚠️ Тема должна быть от 1 до 256 символов.", parse_mode="HTML")
        return

    await state.update_data(topic=topic)
    await state.set_state(NarrativeFSM.waiting_core_message)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=NarrCb(action="menu"))

    await message.answer(
        f"✅ Тема: <b>{html.escape(topic)}</b>\n\n"
        "<b>Шаг 2/5 — Ключевое сообщение</b>\n\n"
        "Введите суть, которую должны донести все посты. Это будет основой для AI.\n\n"
        "<i>Пример: «Это уникальная возможность, которую нельзя упустить» или "
        "«Новый продукт решает проблему X лучше всех аналогов»</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Create — Step 2: Core message ─────────────────────────────────────────────


@router.message(NarrativeFSM.waiting_core_message, F.text)
async def msg_narr_core(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    core_message = (message.text or "").strip()
    if not core_message or len(core_message) > 1000:
        await message.answer("⚠️ Сообщение должно быть от 1 до 1000 символов.", parse_mode="HTML")
        return

    await state.update_data(core_message=core_message, selected_channels=[])
    await state.set_state(NarrativeFSM.choosing_channels)

    # Получаем каналы пользователя
    channels = await _get_user_channels(pool, message.from_user.id)

    kb = InlineKeyboardBuilder()
    if not channels:
        kb.button(text="❌ Нет каналов", callback_data=NarrCb(action="menu"))
        await message.answer(
            "⚠️ У вас нет управляемых каналов.\n"
            "Сначала добавьте каналы через раздел «Каналы».",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        await state.clear()
        return

    for ch in channels[:20]:
        title = ch.get("title") or ch.get("username") or f"id{ch.get('channel_id', '?')}"
        username = ch.get("username", "")
        kb.button(
            text=f"⬜ {html.escape(title[:30])}",
            callback_data=NarrCb(action="channel_toggle", campaign_id=ch.get("channel_id", 0)),
        )
    kb.button(text="✅ Выбраны (0) → Далее", callback_data=NarrCb(action="type_pick"))
    kb.button(text="❌ Отмена", callback_data=NarrCb(action="menu"))
    kb.adjust(1)

    await message.answer(
        f"✅ Сообщение сохранено.\n\n"
        "<b>Шаг 3/5 — Выбор каналов</b>\n\n"
        "Выберите каналы, которые будут участвовать в кампании.\n"
        "Каждый канал получит уникальный пост с разным углом подачи.\n\n"
        "<i>Рекомендуется: 3–6 каналов</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Create — Step 3: Channel toggle ───────────────────────────────────────────


@router.callback_query(NarrCb.filter(F.action == "channel_toggle"), NarrativeFSM.choosing_channels)
async def cb_narr_channel_toggle(
    callback: CallbackQuery, callback_data: NarrCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    data = await state.get_data()
    selected: list[int] = data.get("selected_channels", [])
    channel_id = callback_data.campaign_id  # repurposed field

    if channel_id in selected:
        selected.remove(channel_id)
    else:
        selected.append(channel_id)
    await state.update_data(selected_channels=selected)

    # Перестраиваем клавиатуру
    channels = await _get_user_channels(pool, callback.from_user.id)
    kb = InlineKeyboardBuilder()
    for ch in channels[:20]:
        ch_id = ch.get("channel_id", 0)
        title = ch.get("title") or ch.get("username") or f"id{ch_id}"
        mark = "✅" if ch_id in selected else "⬜"
        kb.button(
            text=f"{mark} {html.escape(title[:30])}",
            callback_data=NarrCb(action="channel_toggle", campaign_id=ch_id),
        )

    cnt = len(selected)
    label = f"✅ Выбрано ({cnt}) → Далее" if cnt > 0 else "⬜ Выберите каналы"
    kb.button(text=label, callback_data=NarrCb(action="type_pick"))
    kb.button(text="❌ Отмена", callback_data=NarrCb(action="menu"))
    kb.adjust(1)

    await _edit(
        callback,
        f"<b>Шаг 3/5 — Выбор каналов</b>\n\n"
        f"Выбрано: <b>{cnt}</b> канал(ов)\n\n"
        "Каждый канал получит уникальный пост с разным углом подачи.\n"
        "<i>Рекомендуется: 3–6 каналов</i>",
        kb.as_markup(),
    )


# ── Create — Step 4: Campaign type ────────────────────────────────────────────


@router.callback_query(NarrCb.filter(F.action == "channels_back"))
async def cb_narr_channels_back(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    """Back navigation to channel selection step, preserving FSM data."""
    await callback.answer()
    data = await state.get_data()
    selected: list[int] = data.get("selected_channels", [])

    await state.set_state(NarrativeFSM.choosing_channels)

    channels = await _get_user_channels(pool, callback.from_user.id)
    kb = InlineKeyboardBuilder()
    for ch in channels[:20]:
        ch_id = ch.get("channel_id", 0)
        title = ch.get("title") or ch.get("username") or f"id{ch_id}"
        mark = "✅" if ch_id in selected else "⬜"
        kb.button(
            text=f"{mark} {html.escape(title[:30])}",
            callback_data=NarrCb(action="channel_toggle", campaign_id=ch_id),
        )
    cnt = len(selected)
    label = f"✅ Выбрано ({cnt}) → Далее" if cnt > 0 else "⬜ Выберите каналы"
    kb.button(text=label, callback_data=NarrCb(action="type_pick"))
    kb.button(text="❌ Отмена", callback_data=NarrCb(action="menu"))
    kb.adjust(1)

    await _edit(
        callback,
        f"<b>Шаг 3/5 — Выбор каналов</b>\n\n"
        f"Выбрано: <b>{cnt}</b> канал(ов)\n\n"
        "Каждый канал получит уникальный пост с разным углом подачи.\n"
        "<i>Рекомендуется: 3–6 каналов</i>",
        kb.as_markup(),
    )


@router.callback_query(NarrCb.filter(F.action == "type_pick"))
async def cb_narr_type_pick(
    callback: CallbackQuery, state: FSMContext
) -> None:
    await callback.answer()
    data = await state.get_data()
    selected = data.get("selected_channels", [])

    if not selected:
        await callback.answer("⚠️ Выберите хотя бы один канал!", show_alert=True)
        return

    await state.set_state(NarrativeFSM.choosing_type)

    kb = InlineKeyboardBuilder()
    for ctype in ["trend", "launch", "awareness", "counter"]:
        label = _TYPE_LABELS[ctype]
        kb.button(
            text=label,
            callback_data=NarrCb(action=f"set_type_{ctype}"),
        )
    kb.button(text="◀️ Назад", callback_data=NarrCb(action="channels_back"))
    kb.adjust(1)

    await _edit(
        callback,
        f"<b>Шаг 4/5 — Тип кампании</b>\n\n"
        f"Каналов в кампании: <b>{len(selected)}</b>\n\n"
        "Выберите тип кампании:\n\n"
        "🌊 <b>Тренд</b> — создаём органический тренд вокруг темы\n"
        "🚀 <b>Запуск</b> — анонсируем новый продукт/событие\n"
        "📢 <b>Осведомлённость</b> — повышаем осведомлённость\n"
        "⚔️ <b>Контр-нарратив</b> — альтернативный взгляд",
        kb.as_markup(),
    )


@router.callback_query(NarrCb.filter(F.action.startswith("set_type_")))
async def cb_narr_set_type(
    callback: CallbackQuery, callback_data: NarrCb, state: FSMContext
) -> None:
    await callback.answer()
    ctype = callback_data.action.replace("set_type_", "")
    if ctype not in _TYPE_LABELS:
        await callback.answer("Неверный тип", show_alert=True)
        return

    await state.update_data(campaign_type=ctype)
    await state.set_state(NarrativeFSM.choosing_spread)

    kb = InlineKeyboardBuilder()
    for h in _SPREAD_OPTIONS:
        kb.button(
            text=f"⏱ {h} ч",
            callback_data=NarrCb(action=f"set_spread_{h}"),
        )
    kb.button(text="◀️ Назад", callback_data=NarrCb(action="type_pick"))
    kb.adjust(3)

    await _edit(
        callback,
        f"<b>Шаг 5/5 — Интервал распространения</b>\n\n"
        f"Тип: <b>{_TYPE_LABELS.get(ctype, ctype)}</b>\n\n"
        "За сколько часов публиковать все посты?\n"
        "Посты распределятся равномерно по выбранному времени.\n\n"
        "<i>Рекомендуется: 3–6 часов для органичного тренда</i>",
        kb.as_markup(),
    )


# ── Create — Step 5: Spread hours → Preview ───────────────────────────────────


@router.callback_query(NarrCb.filter(F.action.startswith("set_spread_")))
async def cb_narr_set_spread(
    callback: CallbackQuery, callback_data: NarrCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    try:
        spread_hours = int(callback_data.action.replace("set_spread_", ""))
    except ValueError:
        await callback.answer("Неверный интервал", show_alert=True)
        return

    await state.update_data(spread_hours=spread_hours)
    await state.set_state(NarrativeFSM.previewing)

    data = await state.get_data()
    topic = data.get("topic", "")
    core_message = data.get("core_message", "")
    selected_channel_ids: list[int] = data.get("selected_channels", [])
    ctype = data.get("campaign_type", "trend")

    # Получаем usernames каналов
    channels = await _get_user_channels(pool, callback.from_user.id)
    channels_map = {ch.get("channel_id", 0): ch for ch in channels}

    selected_channels = [
        channels_map[cid] for cid in selected_channel_ids if cid in channels_map
    ]
    channel_usernames = [
        ch.get("username") or str(ch.get("channel_id", "")) for ch in selected_channels
    ]
    channel_titles = [
        ch.get("title") or ch.get("username") or str(ch.get("channel_id", "?"))
        for ch in selected_channels
    ]
    # Публикация идёт через Telethon-сессию аккаунта, которым канал был добавлен
    # (как в self_promo.py) — боты тут ни при чём, у них нет доступа к каналу.
    channel_meta = [
        {"channel_id": ch.get("channel_id"), "acc_id": ch.get("acc_id"), "username": ch.get("username")}
        for ch in selected_channels
    ]

    await state.update_data(
        channel_usernames=channel_usernames,
        channel_titles=channel_titles,
        channel_meta=channel_meta,
    )

    # Генерируем посты
    providers = configured_providers()
    ai_provider = providers[0] if providers else None

    loading_kb = InlineKeyboardBuilder()
    loading_kb.button(text="⏳ Генерация...", callback_data=NarrCb(action="menu"))
    await _edit(
        callback,
        "🤖 <b>Генерация постов...</b>\n\n"
        "AI создаёт уникальные посты для каждого канала с разных углов.\n"
        "Это займёт несколько секунд.",
        loading_kb.as_markup(),
    )

    try:
        posts = await narrative_engine.generate_campaign_posts(
            topic=topic,
            core_message=core_message,
            channels_count=len(channel_usernames),
            spread_hours=spread_hours,
            ai_provider=ai_provider,
        )
    except Exception as e:
        log_exc_swallow(log, f"narrative_hub: AI generation error: {e}")
        posts = []

    # Детект заглушек: _call_ai возвращает текст в [...] когда AI недоступен.
    # Такие посты НЕЛЬЗЯ публиковать в реальные каналы — блокируем запуск.
    _ai_failed = (not posts) or any(
        (p.get("content", "") or "").lstrip().startswith("[") for p in posts
    )
    await state.update_data(generated_posts=posts, ai_failed=_ai_failed)

    if _ai_failed:
        kb = InlineKeyboardBuilder()
        kb.button(text="🔁 Попробовать снова", callback_data=NarrCb(action="create"))
        kb.button(text="◀️ В меню", callback_data=NarrCb(action="menu"))
        kb.adjust(1)
        await _edit(
            callback,
            "⚠️ <b>AI не настроен или недоступен</b>\n\n"
            "Не удалось сгенерировать реальные тексты постов — публиковать "
            "посты-заглушки в каналы нельзя.\n\n"
            "Добавьте API-ключ AI-провайдера (OpenRouter / Groq / Gemini) "
            "в настройках и попробуйте снова.",
            kb.as_markup(),
        )
        return

    # Показываем предпросмотр
    preview_lines = [
        f"🌊 <b>Предпросмотр кампании</b>\n\n"
        f"📌 <b>Тема:</b> {html.escape(topic)}\n"
        f"💬 <b>Сообщение:</b> {html.escape(core_message[:100])}{'...' if len(core_message) > 100 else ''}\n"
        f"🏷 <b>Тип:</b> {_TYPE_LABELS.get(ctype, ctype)}\n"
        f"⏱ <b>Интервал:</b> {spread_hours} ч\n"
        f"📡 <b>Каналов:</b> {len(channel_usernames)}\n\n"
        f"<b>Сгенерированные посты:</b>\n"
    ]

    for i, (post, title) in enumerate(zip(posts, channel_titles)):
        angle_icon = _ANGLE_ICONS.get(post.get("angle", ""), "📝")
        angle_name = narrative_engine._ANGLE_LABELS.get(post.get("angle", ""), post.get("angle", ""))
        offset_min = post.get("scheduled_offset_minutes", 0)
        offset_str = f"+{offset_min // 60}ч {offset_min % 60}мин" if offset_min else "сразу"
        content_preview = (post.get("content", "")[:150]).replace("<", "&lt;").replace(">", "&gt;")
        preview_lines.append(
            f"\n<b>{i+1}. {html.escape(title[:25])} — {angle_icon} {angle_name} [{offset_str}]</b>\n"
            f"<i>{content_preview}{'...' if len(post.get('content', '')) > 150 else ''}</i>"
        )

    preview_text = "\n".join(preview_lines)
    # Telegram limit 4096
    if len(preview_text) > 3800:
        preview_text = preview_text[:3800] + "\n\n<i>... (предпросмотр обрезан)</i>"

    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Запустить кампанию", callback_data=NarrCb(action="launch"))
    kb.button(text="❌ Отмена", callback_data=NarrCb(action="menu"))
    kb.adjust(1)

    await _edit(callback, preview_text, kb.as_markup())


# ── Create — Launch ───────────────────────────────────────────────────────────


@router.callback_query(NarrCb.filter(F.action == "launch"))
async def cb_narr_launch(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    data = await state.get_data()

    topic = data.get("topic", "")
    core_message = data.get("core_message", "")
    channel_usernames: list[str] = data.get("channel_usernames", [])
    channel_meta: list[dict] = data.get("channel_meta", [])
    ctype = data.get("campaign_type", "trend")
    spread_hours = data.get("spread_hours", 4)

    if not topic or not channel_usernames:
        await callback.answer("⚠️ Данные кампании потеряны. Начните заново.", show_alert=True)
        await state.clear()
        return

    # Получаем предсгенерированные посты из FSM
    generated_posts: list[dict] = data.get("generated_posts", [])

    providers = configured_providers()
    ai_provider = providers[0] if providers else None

    # Guard: не запускаем кампанию с постами-заглушками или без AI-провайдера.
    # Без ключей create_campaign сгенерирует те же [...] заглушки и зальёт их в каналы.
    _has_placeholder = any(
        (p.get("content", "") or "").lstrip().startswith("[") for p in generated_posts
    )
    if data.get("ai_failed") or _has_placeholder or (not generated_posts and ai_provider is None):
        kb = InlineKeyboardBuilder()
        kb.button(text="🔁 Попробовать снова", callback_data=NarrCb(action="create"))
        kb.button(text="◀️ В меню", callback_data=NarrCb(action="menu"))
        kb.adjust(1)
        await _edit(
            callback,
            "⚠️ <b>AI не настроен или недоступен</b>\n\n"
            "Запуск отменён: нельзя публиковать посты-заглушки. "
            "Добавьте API-ключ AI-провайдера и сгенерируйте кампанию заново.",
            kb.as_markup(),
        )
        return

    loading_kb = InlineKeyboardBuilder()
    loading_kb.button(text="⏳ Запуск...", callback_data=NarrCb(action="menu"))
    await _edit(callback, "⚙️ <b>Создаём кампанию...</b>", loading_kb.as_markup())

    try:
        if generated_posts:
            # Используем уже сгенерированные посты — создаём кампанию напрямую
            campaign_id = await _create_campaign_with_posts(
                pool=pool,
                owner_id=callback.from_user.id,
                topic=topic,
                core_message=core_message,
                channel_usernames=channel_usernames,
                channel_meta=channel_meta,
                spread_hours=spread_hours,
                campaign_type=ctype,
                posts=generated_posts,
            )
        else:
            campaign_id = await narrative_engine.create_campaign(
                pool=pool,
                owner_id=callback.from_user.id,
                topic=topic,
                core_message=core_message,
                channel_usernames=channel_usernames,
                channel_meta=channel_meta,
                spread_hours=spread_hours,
                campaign_type=ctype,
                ai_provider=ai_provider,
            )
    except Exception as e:
        log_exc_swallow(log, f"narrative_hub launch error: {e}")
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=NarrCb(action="menu"))
        await _edit(
            callback,
            f"❌ <b>Ошибка запуска кампании</b>\n\n<code>{html.escape(str(e)[:300])}</code>",
            kb.as_markup(),
        )
        return

    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Детали кампании", callback_data=NarrCb(action="detail", campaign_id=campaign_id))
    kb.button(text="◀️ К кампаниям", callback_data=NarrCb(action="menu"))
    kb.adjust(1)

    await _edit(
        callback,
        f"✅ <b>Кампания запущена!</b>\n\n"
        f"📌 <b>Тема:</b> {html.escape(topic)}\n"
        f"📡 <b>Каналов:</b> {len(channel_usernames)}\n"
        f"⏱ <b>Интервал:</b> {spread_hours} ч\n\n"
        f"Посты будут публиковаться автоматически согласно расписанию.",
        kb.as_markup(),
    )


async def _create_campaign_with_posts(
    pool: asyncpg.Pool,
    owner_id: int,
    topic: str,
    core_message: str,
    channel_usernames: list[str],
    spread_hours: int,
    campaign_type: str,
    posts: list[dict],
    channel_meta: list[dict] | None = None,
) -> int:
    """Создаёт кампанию с уже готовыми постами."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    channel_meta = channel_meta or []

    async with pool.acquire() as conn:
        async with conn.transaction():
            campaign_id = await conn.fetchval(
                """INSERT INTO narrative_campaigns
                   (owner_id, topic, core_message, campaign_type, spread_hours,
                    posts_total, status, started_at)
                   VALUES ($1, $2, $3, $4, $5, $6, 'active', NOW())
                   RETURNING id""",
                owner_id, topic, core_message, campaign_type,
                spread_hours, len(channel_usernames),
            )

            for i, (username, post) in enumerate(zip(channel_usernames, posts)):
                scheduled_at = now + timedelta(minutes=post.get("scheduled_offset_minutes", 0))
                meta = channel_meta[i] if i < len(channel_meta) else {}
                await conn.execute(
                    """INSERT INTO narrative_posts
                       (campaign_id, owner_id, channel_username, channel_id, acc_id, angle,
                        content, scheduled_at, status)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending')""",
                    campaign_id, owner_id, username,
                    meta.get("channel_id"), meta.get("acc_id"),
                    post.get("angle", "news"),
                    post.get("content", ""),
                    scheduled_at,
                )

    return campaign_id


# ── Detail ────────────────────────────────────────────────────────────────────


@router.callback_query(NarrCb.filter(F.action == "detail"))
async def cb_narr_detail(
    callback: CallbackQuery, callback_data: NarrCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    campaign_id = callback_data.campaign_id
    owner_id = callback.from_user.id

    campaign = await narrative_engine.get_campaign(pool, campaign_id, owner_id)
    if not campaign:
        await callback.answer("Кампания не найдена", show_alert=True)
        return

    status_data = await narrative_engine.get_campaign_status(pool, campaign_id)
    posts = status_data.get("posts", [])
    published = status_data.get("published", 0)
    total = status_data.get("total", 0)
    pct = status_data.get("progress_pct", 0)

    status = campaign["status"]
    status_icon = _STATUS_ICONS.get(status, "❓")
    status_label = _STATUS_LABELS.get(status, status)
    ctype_label = _TYPE_LABELS.get(campaign["campaign_type"], campaign["campaign_type"])

    # Прогресс-бар
    filled = int(pct / 100 * 10)
    bar = "█" * filled + "░" * (10 - filled)
    progress_str = f"[{bar}] {published}/{total} ({pct}%)"

    lines = [
        f"{status_icon} <b>Кампания #{campaign_id}</b>\n",
        f"📌 <b>Тема:</b> {html.escape(campaign['topic'])}",
        f"🏷 <b>Тип:</b> {ctype_label}",
        f"⏱ <b>Интервал:</b> {campaign['spread_hours']} ч",
        f"📊 <b>Статус:</b> {status_label}",
        f"📈 <b>Прогресс:</b> {progress_str}",
        f"🕐 <b>Создана:</b> {_format_dt(campaign.get('created_at'))}",
    ]

    if campaign.get("started_at"):
        lines.append(f"▶️ <b>Старт:</b> {_format_dt(campaign.get('started_at'))}")
    if campaign.get("completed_at"):
        lines.append(f"✅ <b>Завершена:</b> {_format_dt(campaign.get('completed_at'))}")

    lines.append("\n<b>Посты:</b>")
    for p in posts:
        post_status = _POST_STATUS_ICONS.get(p["status"], "❓")
        angle_icon = _ANGLE_ICONS.get(p.get("angle", ""), "📝")
        angle_name = narrative_engine._ANGLE_LABELS.get(p.get("angle", ""), p.get("angle", ""))
        channel = html.escape((p["channel_username"] or "").lstrip("@")[:20])
        sched = _format_dt(p.get("scheduled_at"))
        pub = _format_dt(p.get("published_at"))
        time_str = f"опубл. {pub}" if p["status"] == "published" else f"план. {sched}"
        lines.append(
            f"{post_status} {angle_icon} <b>{channel}</b> — {angle_name} [{time_str}]"
        )
        if p.get("error_text") and p["status"] == "failed":
            err = html.escape(p["error_text"][:80])
            lines.append(f"   ⚠️ <i>{err}</i>")

    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3800] + "\n<i>... (обрезано)</i>"

    kb = InlineKeyboardBuilder()

    if status == "active":
        kb.button(text="⏸ Пауза", callback_data=NarrCb(action="pause", campaign_id=campaign_id))
    elif status == "paused":
        kb.button(text="▶️ Возобновить", callback_data=NarrCb(action="resume", campaign_id=campaign_id))

    if status in ("active", "paused", "draft"):
        kb.button(text="🚫 Отменить", callback_data=NarrCb(action="confirm_cancel", campaign_id=campaign_id))

    kb.button(text="👁 Предпросмотр постов", callback_data=NarrCb(action="preview", campaign_id=campaign_id))
    kb.button(text="◀️ К кампаниям", callback_data=NarrCb(action="menu"))
    kb.adjust(1)

    await _edit(callback, text, kb.as_markup())


# ── Preview all posts ─────────────────────────────────────────────────────────


@router.callback_query(NarrCb.filter(F.action == "preview"))
async def cb_narr_preview(
    callback: CallbackQuery, callback_data: NarrCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    campaign_id = callback_data.campaign_id
    owner_id = callback.from_user.id

    campaign = await narrative_engine.get_campaign(pool, campaign_id, owner_id)
    if not campaign:
        await callback.answer("Кампания не найдена", show_alert=True)
        return

    posts = await narrative_engine.get_campaign_posts(pool, campaign_id)

    lines = [f"👁 <b>Посты кампании #{campaign_id}</b>\n<b>{html.escape(campaign['topic'])}</b>\n"]
    for i, p in enumerate(posts):
        angle_icon = _ANGLE_ICONS.get(p.get("angle", ""), "📝")
        angle_name = narrative_engine._ANGLE_LABELS.get(p.get("angle", ""), "")
        post_status = _POST_STATUS_ICONS.get(p["status"], "❓")
        channel = html.escape((p["channel_username"] or "").lstrip("@")[:25])
        content = (p.get("content") or "").replace("<", "&lt;").replace(">", "&gt;")
        lines.append(
            f"\n{post_status} <b>{i+1}. {channel}</b> — {angle_icon} {angle_name}\n"
            f"<i>{content[:300]}{'...' if len(content) > 300 else ''}</i>"
        )

    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3800] + "\n\n<i>... (предпросмотр обрезан)</i>"

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=NarrCb(action="detail", campaign_id=campaign_id))

    await _edit(callback, text, kb.as_markup())


# ── Pause / Resume / Cancel ───────────────────────────────────────────────────


@router.callback_query(NarrCb.filter(F.action == "pause"))
async def cb_narr_pause(
    callback: CallbackQuery, callback_data: NarrCb, pool: asyncpg.Pool
) -> None:
    campaign_id = callback_data.campaign_id
    ok = await narrative_engine.pause_campaign(pool, campaign_id, callback.from_user.id)
    if ok:
        await callback.answer("⏸ Кампания поставлена на паузу")
    else:
        await callback.answer("⚠️ Не удалось поставить на паузу", show_alert=True)
    # Обновляем детали
    from aiogram.filters.callback_data import CallbackData as _CD
    fake = NarrCb(action="detail", campaign_id=campaign_id)
    await cb_narr_detail(callback, fake, pool)


@router.callback_query(NarrCb.filter(F.action == "resume"))
async def cb_narr_resume(
    callback: CallbackQuery, callback_data: NarrCb, pool: asyncpg.Pool
) -> None:
    campaign_id = callback_data.campaign_id
    ok = await narrative_engine.resume_campaign(pool, campaign_id, callback.from_user.id)
    if ok:
        await callback.answer("▶️ Кампания возобновлена")
    else:
        await callback.answer("⚠️ Не удалось возобновить", show_alert=True)
    fake = NarrCb(action="detail", campaign_id=campaign_id)
    await cb_narr_detail(callback, fake, pool)


@router.callback_query(NarrCb.filter(F.action == "confirm_cancel"))
async def cb_narr_confirm_cancel(
    callback: CallbackQuery, callback_data: NarrCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    campaign_id = callback_data.campaign_id

    kb = InlineKeyboardBuilder()
    kb.button(text="🚫 Да, отменить", callback_data=NarrCb(action="cancel", campaign_id=campaign_id))
    kb.button(text="◀️ Нет, назад", callback_data=NarrCb(action="detail", campaign_id=campaign_id))
    kb.adjust(1)

    await _edit(
        callback,
        f"⚠️ <b>Отменить кампанию #{campaign_id}?</b>\n\n"
        "Все неопубликованные посты будут отменены.\n"
        "Это действие нельзя отменить.",
        kb.as_markup(),
    )


@router.callback_query(NarrCb.filter(F.action == "cancel"))
async def cb_narr_cancel(
    callback: CallbackQuery, callback_data: NarrCb, pool: asyncpg.Pool
) -> None:
    campaign_id = callback_data.campaign_id
    ok = await narrative_engine.cancel_campaign(pool, campaign_id, callback.from_user.id)
    if ok:
        await callback.answer("🚫 Кампания отменена")
    else:
        await callback.answer("⚠️ Не удалось отменить", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К кампаниям", callback_data=NarrCb(action="menu"))

    await _edit(
        callback,
        f"🚫 <b>Кампания #{campaign_id} отменена.</b>\n\n"
        "Все запланированные посты были отменены.",
        kb.as_markup(),
    )
