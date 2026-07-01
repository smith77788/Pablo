"""Persona Hub — управление AI-персонами с персистентной памятью."""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import PersonaCb, BmCb
from bot.states import PersonaCreateFSM

log = logging.getLogger(__name__)
router = Router()

_SPEECH_STYLES = {
    "neutral":    ("💬", "Нейтральный"),
    "formal":     ("🎩", "Формальный"),
    "casual":     ("😊", "Разговорный"),
    "expert":     ("🔬", "Экспертный"),
    "friendly":   ("🤝", "Дружелюбный"),
    "sarcastic":  ("😏", "Саркастичный"),
}

_EVENT_LABELS = {
    "comment":  "💬 Комментарий",
    "reaction": "❤️  Реакция",
    "follow":   "➕ Подписка",
    "message":  "✉️  Сообщение",
    "post":     "📝 Публикация",
}

_SENTIMENT_ICONS = {
    "positive": "🟢",
    "neutral":  "⚪",
    "negative": "🔴",
}


# ── helpers ───────────────────────────────────────────────────────────────────


async def _edit(cb: CallbackQuery, text: str, markup=None) -> None:
    await cb.answer()
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception as e:
        err_str = str(e).lower()
        if "message is not modified" in err_str:
            return
        if "there is no text in the message to edit" in err_str:
            try:
                await cb.message.edit_caption(
                    caption=text, parse_mode="HTML", reply_markup=markup
                )
                return
            except Exception:
                pass
        if "message to edit not found" in err_str or "message can't be edited" in err_str:
            await cb.bot.send_message(
                cb.from_user.id, text, parse_mode="HTML", reply_markup=markup
            )
        else:
            log.warning("persona_hub _edit error: %s", e)


async def _list_personas(pool: asyncpg.Pool, owner_id: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        """
        SELECT pp.*, a.phone, a.username, a.first_name
        FROM persona_profiles pp
        LEFT JOIN tg_accounts a ON a.id = pp.account_id
        WHERE pp.owner_id = $1
        ORDER BY pp.id DESC
        """,
        owner_id,
    )


async def _get_persona_row(
    pool: asyncpg.Pool, persona_id: int, owner_id: int
) -> asyncpg.Record | None:
    return await pool.fetchrow(
        """
        SELECT pp.*, a.phone, a.username, a.first_name
        FROM persona_profiles pp
        LEFT JOIN tg_accounts a ON a.id = pp.account_id
        WHERE pp.id = $1 AND pp.owner_id = $2
        """,
        persona_id,
        owner_id,
    )


def _acc_label(row: asyncpg.Record) -> str:
    name = (
        row.get("username")
        or row.get("first_name")
        or row.get("phone")
        or f"id{row.get('account_id', '?')}"
    )
    return html.escape(str(name))


def _ts(dt: datetime | None) -> str:
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%d.%m.%Y %H:%M")


# ── Menu ──────────────────────────────────────────────────────────────────────


@router.callback_query(PersonaCb.filter(F.action == "menu"))
async def cb_persona_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    try:
        personas = await _list_personas(pool, callback.from_user.id)
    except Exception as e:
        log.error("persona_hub cb_persona_menu: %s", e)
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=BmCb(action="settings"))
        await callback.message.edit_text(
            "🎭 <b>Persona Ecosystem</b>\n\n"
            "⚠️ Модуль недоступен — таблицы не созданы в базе данных.\n\n"
            "Администратору необходимо применить миграцию <code>schema_v117.sql</code>.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()

    if not personas:
        text = (
            "🎭 <b>Persona Ecosystem</b>\n\n"
            "AI-персоны — это виртуальные личности с уникальным характером, "
            "стилем речи и персистентной памятью действий.\n\n"
            "Каждая персона привязана к аккаунту Telegram и действует "
            "согласно заданному образу.\n\n"
            "У вас ещё нет ни одной персоны. Создайте первую!"
        )
    else:
        active = sum(1 for p in personas if p["is_active"])
        text = (
            f"🎭 <b>Persona Ecosystem</b>\n\n"
            f"Персон: <b>{len(personas)}</b>  |  Активных: <b>{active}</b>\n\n"
        )
        for p in personas[:10]:
            status = "🟢" if p["is_active"] else "🔴"
            acc = _acc_label(p)
            niche = html.escape(p["niche"] or "—")
            text += f"{status} <b>{html.escape(p['persona_name'])}</b> — {acc} · {niche}\n"
            kb.button(
                text=f"{status} {p['persona_name'][:30]} ({acc[:20]})",
                callback_data=PersonaCb(action="view", persona_id=p["id"]),
            )

    kb.button(text="➕ Создать персону", callback_data=PersonaCb(action="create"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="settings"))
    kb.adjust(1)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Create wizard ─────────────────────────────────────────────────────────────


@router.callback_query(PersonaCb.filter(F.action == "create"))
async def cb_persona_create(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    accounts = await pool.fetch(
        "SELECT id, phone, username, first_name FROM tg_accounts "
        "WHERE owner_id = $1 AND COALESCE(acc_status,'active') NOT IN ('banned','deactivated','session_expired') ORDER BY id",
        callback.from_user.id,
    )
    if not accounts:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=PersonaCb(action="menu"))
        await callback.message.edit_text(
            "🎭 <b>Создание персоны</b>\n\n"
            "❌ У вас нет активных аккаунтов Telegram.\n"
            "Добавьте хотя бы один аккаунт, чтобы создать персону.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    # pick account step before FSM — store accounts list and show picker
    await state.update_data(
        _accounts=[
            {
                "id": a["id"],
                "label": html.escape(
                    a["username"] or a["first_name"] or a["phone"] or f"id{a['id']}"
                ),
            }
            for a in accounts
        ]
    )
    await state.set_state(PersonaCreateFSM.entering_name)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=PersonaCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "🎭 <b>Новая персона</b> — Шаг 1/6\n\n"
        "Введите <b>имя персоны</b> (например: «Анна», «Макс Иванов»):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(PersonaCreateFSM.entering_name)
async def fsm_persona_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()[:64]
    if not name:
        await message.answer("❌ Имя не может быть пустым. Введите имя персоны:")
        return
    await state.update_data(persona_name=name)
    await state.set_state(PersonaCreateFSM.entering_bio)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=PersonaCb(action="skip_bio"))
    kb.button(text="❌ Отмена", callback_data=PersonaCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"🎭 <b>{html.escape(name)}</b> — Шаг 2/6\n\n"
        "Введите <b>биографию</b> персоны (чем занимается, кто она):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(PersonaCb.filter(F.action == "skip_bio"))
async def cb_persona_skip_bio(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(bio="")
    await _ask_interests(callback, state)


@router.message(PersonaCreateFSM.entering_bio)
async def fsm_persona_bio(message: Message, state: FSMContext) -> None:
    bio = (message.text or "").strip()[:500]
    await state.update_data(bio=bio)
    await _ask_interests_msg(message, state)


async def _ask_interests(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(PersonaCreateFSM.entering_interests)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=PersonaCb(action="skip_interests"))
    kb.button(text="❌ Отмена", callback_data=PersonaCb(action="menu"))
    kb.adjust(1)
    sd = await state.get_data()
    name = html.escape(sd.get("persona_name", ""))
    await callback.message.edit_text(
        f"🎭 <b>{name}</b> — Шаг 3/6\n\n"
        "Введите <b>интересы</b> персоны через запятую\n"
        "(например: криптовалюты, технологии, путешествия):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _ask_interests_msg(message: Message, state: FSMContext) -> None:
    await state.set_state(PersonaCreateFSM.entering_interests)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=PersonaCb(action="skip_interests"))
    kb.button(text="❌ Отмена", callback_data=PersonaCb(action="menu"))
    kb.adjust(1)
    sd = await state.get_data()
    name = html.escape(sd.get("persona_name", ""))
    await message.answer(
        f"🎭 <b>{name}</b> — Шаг 3/6\n\n"
        "Введите <b>интересы</b> персоны через запятую:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(PersonaCb.filter(F.action == "skip_interests"))
async def cb_persona_skip_interests(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(interests=[])
    await _ask_niche(callback, state)


@router.message(PersonaCreateFSM.entering_interests)
async def fsm_persona_interests(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    interests = [i.strip() for i in raw.split(",") if i.strip()][:20]
    await state.update_data(interests=interests)
    await _ask_niche_msg(message, state)


async def _ask_niche(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(PersonaCreateFSM.entering_niche)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=PersonaCb(action="skip_niche"))
    kb.button(text="❌ Отмена", callback_data=PersonaCb(action="menu"))
    kb.adjust(1)
    sd = await state.get_data()
    name = html.escape(sd.get("persona_name", ""))
    await callback.message.edit_text(
        f"🎭 <b>{name}</b> — Шаг 4/6\n\n"
        "Введите <b>нишу</b> персоны (тематика канала / сфера деятельности):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _ask_niche_msg(message: Message, state: FSMContext) -> None:
    await state.set_state(PersonaCreateFSM.entering_niche)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=PersonaCb(action="skip_niche"))
    kb.button(text="❌ Отмена", callback_data=PersonaCb(action="menu"))
    kb.adjust(1)
    sd = await state.get_data()
    name = html.escape(sd.get("persona_name", ""))
    await message.answer(
        f"🎭 <b>{name}</b> — Шаг 4/6\n\n"
        "Введите <b>нишу</b> персоны:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(PersonaCb.filter(F.action == "skip_niche"))
async def cb_persona_skip_niche(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(niche="")
    await _ask_speech_style(callback, state)


@router.message(PersonaCreateFSM.entering_niche)
async def fsm_persona_niche(message: Message, state: FSMContext) -> None:
    niche = (message.text or "").strip()[:64]
    await state.update_data(niche=niche)
    await _ask_speech_style_msg(message, state)


async def _ask_speech_style(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(PersonaCreateFSM.entering_speech_style)
    kb = InlineKeyboardBuilder()
    for key, (icon, label) in _SPEECH_STYLES.items():
        kb.button(
            text=f"{icon} {label}",
            callback_data=PersonaCb(action=f"set_style_{key}"),
        )
    kb.button(text="❌ Отмена", callback_data=PersonaCb(action="menu"))
    kb.adjust(2)
    sd = await state.get_data()
    name = html.escape(sd.get("persona_name", ""))
    await callback.message.edit_text(
        f"🎭 <b>{name}</b> — Шаг 5/6\n\n"
        "Выберите <b>стиль речи</b> персоны:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _ask_speech_style_msg(message: Message, state: FSMContext) -> None:
    await state.set_state(PersonaCreateFSM.entering_speech_style)
    kb = InlineKeyboardBuilder()
    for key, (icon, label) in _SPEECH_STYLES.items():
        kb.button(
            text=f"{icon} {label}",
            callback_data=PersonaCb(action=f"set_style_{key}"),
        )
    kb.button(text="❌ Отмена", callback_data=PersonaCb(action="menu"))
    kb.adjust(2)
    sd = await state.get_data()
    name = html.escape(sd.get("persona_name", ""))
    await message.answer(
        f"🎭 <b>{name}</b> — Шаг 5/6\n\n"
        "Выберите <b>стиль речи</b> персоны:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(PersonaCb.filter(F.action.startswith("set_style_")))
async def cb_persona_set_style(
    callback: CallbackQuery, callback_data: PersonaCb, state: FSMContext
) -> None:
    style_key = callback_data.action.replace("set_style_", "")
    if style_key not in _SPEECH_STYLES:
        await callback.answer("Неверный стиль", show_alert=True)
        return
    await state.update_data(speech_style=style_key)
    await _ask_backstory(callback, state)


async def _ask_backstory(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(PersonaCreateFSM.entering_backstory)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=PersonaCb(action="skip_backstory"))
    kb.button(text="❌ Отмена", callback_data=PersonaCb(action="menu"))
    kb.adjust(1)
    sd = await state.get_data()
    name = html.escape(sd.get("persona_name", ""))
    await callback.message.edit_text(
        f"🎭 <b>{name}</b> — Шаг 6/6\n\n"
        "Введите <b>предысторию</b> персоны (опционально).\n"
        "Это даст AI дополнительный контекст при генерации ответов:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(PersonaCb.filter(F.action == "skip_backstory"))
async def cb_persona_skip_backstory(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(backstory="")
    await _show_confirm(callback, state)


@router.message(PersonaCreateFSM.entering_backstory)
async def fsm_persona_backstory(message: Message, state: FSMContext) -> None:
    backstory = (message.text or "").strip()[:1000]
    await state.update_data(backstory=backstory)
    await _show_confirm_msg(message, state)


async def _show_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(PersonaCreateFSM.confirming)
    sd = await state.get_data()
    text = _build_preview(sd)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Создать", callback_data=PersonaCb(action="do_create"))
    kb.button(text="❌ Отмена", callback_data=PersonaCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


async def _show_confirm_msg(message: Message, state: FSMContext) -> None:
    await state.set_state(PersonaCreateFSM.confirming)
    sd = await state.get_data()
    text = _build_preview(sd)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Создать", callback_data=PersonaCb(action="do_create"))
    kb.button(text="❌ Отмена", callback_data=PersonaCb(action="menu"))
    kb.adjust(1)
    await message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


def _build_preview(sd: dict) -> str:
    name = html.escape(sd.get("persona_name", ""))
    bio = html.escape(sd.get("bio", "") or "—")
    interests = sd.get("interests", [])
    interests_str = html.escape(", ".join(interests)) if interests else "—"
    niche = html.escape(sd.get("niche", "") or "—")
    style_key = sd.get("speech_style", "neutral")
    style_icon, style_label = _SPEECH_STYLES.get(style_key, ("💬", style_key))
    backstory = html.escape(sd.get("backstory", "") or "—")

    return (
        "🎭 <b>Персона — Предпросмотр</b>\n\n"
        f"<b>Имя:</b> {name}\n"
        f"<b>Биография:</b> {bio}\n"
        f"<b>Интересы:</b> {interests_str}\n"
        f"<b>Ниша:</b> {niche}\n"
        f"<b>Стиль речи:</b> {style_icon} {style_label}\n"
        f"<b>Предыстория:</b> {backstory}\n\n"
        "⚠️ Персона будет привязана к первому свободному аккаунту.\n"
        "Подтвердите создание:"
    )


@router.callback_query(PersonaCb.filter(F.action == "do_create"))
async def cb_persona_do_create(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    """Create or update a persona.

    If FSM data contains ``_edit_persona_id`` (set by the edit flow), the
    existing record is updated instead of inserting a new one.
    """
    await callback.answer()
    sd = await state.get_data()
    await state.clear()

    owner_id = callback.from_user.id

    # ── Edit path ─────────────────────────────────────────────────────────────
    edit_persona_id: int | None = sd.get("_edit_persona_id")
    if edit_persona_id:
        try:
            await pool.execute(
                """
                UPDATE persona_profiles
                SET persona_name = $1, bio = $2, interests = $3::TEXT[],
                    niche = $4, speech_style = $5, backstory = $6, updated_at = NOW()
                WHERE id = $7 AND owner_id = $8
                """,
                sd.get("persona_name", ""),
                sd.get("bio", ""),
                sd.get("interests", []),
                sd.get("niche", ""),
                sd.get("speech_style", "neutral"),
                sd.get("backstory", ""),
                edit_persona_id,
                owner_id,
            )
        except Exception as e:
            log.warning("persona_hub do_update error: %s", e)
            kb = InlineKeyboardBuilder()
            kb.button(
                text="◀️ Назад",
                callback_data=PersonaCb(action="view", persona_id=edit_persona_id),
            )
            await callback.message.edit_text(
                f"❌ Ошибка обновления персоны: {html.escape(str(e)[:200])}",
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )
            return

        kb = InlineKeyboardBuilder()
        kb.button(
            text="👁 Посмотреть",
            callback_data=PersonaCb(action="view", persona_id=edit_persona_id),
        )
        kb.button(text="◀️ К списку", callback_data=PersonaCb(action="menu"))
        kb.adjust(1)
        name = html.escape(sd.get("persona_name", ""))
        await callback.message.edit_text(
            f"✅ <b>Персона «{name}» обновлена!</b>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    # ── Create path ───────────────────────────────────────────────────────────
    # Pick first available account (not already used by another persona)
    used_accounts = {
        r["account_id"]
        for r in await pool.fetch(
            "SELECT account_id FROM persona_profiles WHERE owner_id = $1", owner_id
        )
    }
    accounts = await pool.fetch(
        "SELECT id FROM tg_accounts WHERE owner_id = $1 AND COALESCE(acc_status,'active') NOT IN ('banned','deactivated','session_expired') ORDER BY id",
        owner_id,
    )
    available = [a for a in accounts if a["id"] not in used_accounts]

    if not available:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=PersonaCb(action="menu"))
        await callback.message.edit_text(
            "❌ <b>Нет свободных аккаунтов</b>\n\n"
            "Все ваши аккаунты уже привязаны к персонам. "
            "Удалите существующую персону, чтобы освободить аккаунт.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    account_id = available[0]["id"]

    from services.persona_engine import create_persona as _create_persona

    try:
        persona_id = await _create_persona(
            pool=pool,
            account_id=account_id,
            owner_id=owner_id,
            name=sd.get("persona_name", "Персона"),
            bio=sd.get("bio", ""),
            interests=sd.get("interests", []),
            niche=sd.get("niche", ""),
            speech_style=sd.get("speech_style", "neutral"),
            backstory=sd.get("backstory", ""),
        )
    except Exception as e:
        log.warning("persona_hub do_create error: %s", e)
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=PersonaCb(action="menu"))
        await callback.message.edit_text(
            f"❌ Ошибка создания персоны: {html.escape(str(e)[:200])}",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    kb = InlineKeyboardBuilder()
    kb.button(
        text="👁 Посмотреть персону",
        callback_data=PersonaCb(action="view", persona_id=persona_id),
    )
    kb.button(text="◀️ К списку", callback_data=PersonaCb(action="menu"))
    kb.adjust(1)
    name = html.escape(sd.get("persona_name", ""))
    await callback.message.edit_text(
        f"✅ <b>Персона «{name}» создана!</b>\n\n"
        f"Persona ID: <code>{persona_id}</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── View persona ──────────────────────────────────────────────────────────────


@router.callback_query(PersonaCb.filter(F.action == "view"))
async def cb_persona_view(
    callback: CallbackQuery, callback_data: PersonaCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    persona_id = callback_data.persona_id
    p = await _get_persona_row(pool, persona_id, callback.from_user.id)
    if not p:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=PersonaCb(action="menu"))
        await callback.message.edit_text(
            "❌ Персона не найдена.", parse_mode="HTML", reply_markup=kb.as_markup()
        )
        return

    from services.persona_engine import get_persona_memory as _get_memory

    memory = await _get_memory(pool, persona_id, limit=20)
    status = "🟢 Активна" if p["is_active"] else "🔴 Выключена"
    acc = _acc_label(p)
    style_key = p["speech_style"] or "neutral"
    style_icon, style_label = _SPEECH_STYLES.get(style_key, ("💬", style_key))
    interests = list(p["interests"] or [])
    interests_str = html.escape(", ".join(interests)) if interests else "—"

    text = (
        f"🎭 <b>{html.escape(p['persona_name'])}</b>  {status}\n\n"
        f"<b>Аккаунт:</b> {acc}\n"
        f"<b>Возраст:</b> {p['age']} лет\n"
        f"<b>Ниша:</b> {html.escape(p['niche'] or '—')}\n"
        f"<b>Биография:</b> {html.escape(p['bio'] or '—')}\n"
        f"<b>Интересы:</b> {interests_str}\n"
        f"<b>Стиль речи:</b> {style_icon} {style_label}\n"
        f"<b>Тон:</b> {html.escape(p['tone'] or 'positive')}\n"
        f"<b>Создана:</b> {_ts(p['created_at'])}\n"
    )

    if p["backstory"]:
        text += f"\n<b>Предыстория:</b>\n{html.escape(p['backstory'][:300])}\n"

    if memory:
        text += f"\n<b>Последние {len(memory)} действий:</b>\n"
        for m in memory:
            evt_icon = _EVENT_LABELS.get(m["event_type"], f"• {m['event_type']}")
            sent_icon = _SENTIMENT_ICONS.get(m["sentiment"], "⚪")
            entity = html.escape(m.get("entity", "") or "")
            content_short = html.escape((m.get("content", "") or "")[:80])
            when = _ts(m.get("created_at"))
            text += f"\n{sent_icon} {evt_icon}"
            if entity:
                text += f" → {entity}"
            text += f"\n  <i>{content_short}</i>  <code>{when}</code>\n"
    else:
        text += "\n<i>Действий пока нет.</i>"

    kb = InlineKeyboardBuilder()
    toggle_action = "deactivate" if p["is_active"] else "activate"
    toggle_label = "🔴 Деактивировать" if p["is_active"] else "🟢 Активировать"
    kb.button(text=toggle_label, callback_data=PersonaCb(action=toggle_action, persona_id=persona_id))
    kb.button(text="✏️ Редактировать", callback_data=PersonaCb(action="edit", persona_id=persona_id))
    kb.button(text="🗑 Удалить", callback_data=PersonaCb(action="delete", persona_id=persona_id))
    kb.button(text="◀️ К списку", callback_data=PersonaCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Toggle active ─────────────────────────────────────────────────────────────


@router.callback_query(PersonaCb.filter(F.action.in_({"activate", "deactivate"})))
async def cb_persona_toggle(
    callback: CallbackQuery, callback_data: PersonaCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    persona_id = callback_data.persona_id
    new_state = callback_data.action == "activate"
    await pool.execute(
        "UPDATE persona_profiles SET is_active = $1, updated_at = NOW() WHERE id = $2 AND owner_id = $3",
        new_state,
        persona_id,
        callback.from_user.id,
    )
    # Redirect to view
    from bot.handlers.persona_hub import cb_persona_view
    await cb_persona_view(callback, callback_data.__class__(action="view", persona_id=persona_id), pool)


# ── Edit persona ──────────────────────────────────────────────────────────────


@router.callback_query(PersonaCb.filter(F.action == "edit"))
async def cb_persona_edit(
    callback: CallbackQuery, callback_data: PersonaCb, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    persona_id = callback_data.persona_id
    p = await _get_persona_row(pool, persona_id, callback.from_user.id)
    if not p:
        await _edit(callback, "❌ Персона не найдена.")
        return

    # Pre-fill FSM with existing data
    await state.update_data(
        persona_name=p["persona_name"],
        bio=p["bio"] or "",
        interests=list(p["interests"] or []),
        niche=p["niche"] or "",
        speech_style=p["speech_style"] or "neutral",
        backstory=p["backstory"] or "",
        _edit_persona_id=persona_id,
    )
    await state.set_state(PersonaCreateFSM.entering_name)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=PersonaCb(action="view", persona_id=persona_id))
    kb.adjust(1)
    await callback.message.edit_text(
        f"✏️ <b>Редактирование: {html.escape(p['persona_name'])}</b>\n\n"
        "Шаг 1/6 — Введите новое <b>имя</b> персоны\n"
        f"(текущее: <code>{html.escape(p['persona_name'])}</code>):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Delete persona ────────────────────────────────────────────────────────────


@router.callback_query(PersonaCb.filter(F.action == "delete"))
async def cb_persona_delete(
    callback: CallbackQuery, callback_data: PersonaCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    persona_id = callback_data.persona_id
    p = await _get_persona_row(pool, persona_id, callback.from_user.id)
    if not p:
        await _edit(callback, "❌ Персона не найдена.")
        return

    kb = InlineKeyboardBuilder()
    kb.button(
        text="⚠️ Да, удалить",
        callback_data=PersonaCb(action="delete_confirm", persona_id=persona_id),
    )
    kb.button(text="◀️ Отмена", callback_data=PersonaCb(action="view", persona_id=persona_id))
    kb.adjust(1)
    await callback.message.edit_text(
        f"🗑 <b>Удалить персону «{html.escape(p['persona_name'])}»?</b>\n\n"
        "⚠️ Будут удалены все данные персоны, включая память о действиях.\n"
        "Это действие необратимо.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(PersonaCb.filter(F.action == "delete_confirm"))
async def cb_persona_delete_confirm(
    callback: CallbackQuery, callback_data: PersonaCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    persona_id = callback_data.persona_id
    p = await _get_persona_row(pool, persona_id, callback.from_user.id)
    if not p:
        await _edit(callback, "❌ Персона не найдена.")
        return

    name = p["persona_name"]
    try:
        await pool.execute(
            "DELETE FROM persona_profiles WHERE id = $1 AND owner_id = $2",
            persona_id,
            callback.from_user.id,
        )
    except Exception as e:
        log.warning("persona_hub delete_confirm error: %s", e)
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=PersonaCb(action="menu"))
        await callback.message.edit_text(
            f"❌ Ошибка удаления: {html.escape(str(e)[:200])}",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К списку персон", callback_data=PersonaCb(action="menu"))
    await callback.message.edit_text(
        f"✅ Персона «{html.escape(name)}» удалена.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
