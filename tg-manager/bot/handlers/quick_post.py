"""Quick Post Wizard — публикация в каналы за 5 шагов.

Шаг 1: написать текст
Шаг 2: выбрать каналы
Шаг 3: прикрепить медиа (фото/видео/документ) или пропустить
Шаг 4: задержка между постами
Шаг 5: предпросмотр + публикация

Точки входа:
  /post — команда бота
  QuickPostCb(action="start") — кнопка «✍️ Создать пост» в главном меню
"""

from __future__ import annotations

import html
import json
import logging

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import AssetTplCb, QuickPostCb, BmCb
from bot.keyboards import subscription_locked_markup
from bot.states import QuickPostFSM
from bot.utils.subscription import require_plan, locked_text
from bot.utils.event_status import mark_handled_error

log = logging.getLogger(__name__)
router = Router()

_PAGE_SIZE = 8

# Telegram limits
_TG_CAPTION_LIMIT = 4096
_TG_BUTTON_LIMIT = 200

_TIMING_OPTIONS: dict[int, str] = {
    5: "⚡ 5 сек (быстро)",
    30: "🛡️ 30 сек (безопасно)",
    60: "🐌 60 сек (осторожно)",
    -1: "🧠 Умный (30–90 сек)",
}


# ── DB helpers ─────────────────────────────────────────────────────────────


async def _load_channels(pool: asyncpg.Pool, user_id: int) -> list[asyncpg.Record]:
    """Возвращает уникальные каналы пользователя из managed_channels."""
    try:
        return await pool.fetch(
            "SELECT channel_id AS id, MAX(title) AS title, MAX(access_hash) AS access_hash, "
            "MIN(acc_id) AS acc_id "
            "FROM managed_channels WHERE owner_id=$1 "
            "GROUP BY channel_id ORDER BY MAX(title)",
            user_id,
        )
    except Exception:
        return []


async def _load_post_templates(
    pool: asyncpg.Pool, user_id: int
) -> list[asyncpg.Record]:
    """Возвращает шаблоны постов пользователя."""
    try:
        return await pool.fetch(
            "SELECT id, name, template FROM asset_templates "
            "WHERE owner_id=$1 AND asset_type='post' ORDER BY created_at DESC LIMIT 10",
            user_id,
        )
    except Exception:
        return []


async def _save_post_template(
    pool: asyncpg.Pool, user_id: int, name: str, text: str
) -> int:
    """Сохраняет текст поста как шаблон. Возвращает id нового шаблона."""
    try:
        row = await pool.fetchrow(
            "INSERT INTO asset_templates (owner_id, asset_type, name, template) "
            "VALUES ($1, 'post', $2, $3) RETURNING id",
            user_id,
            name,
            json.dumps({"text": text}),
        )
    except Exception as e:
        raise RuntimeError(f"Failed to create post template: {e}") from e
    if not row:
        raise RuntimeError("Failed to create post template")
    return row["id"]


# ── Step indicator ─────────────────────────────────────────────────────────


def _step(n: int, title: str) -> str:
    dots = "●" * n + "○" * (5 - n)
    return f"{dots}  Шаг {n} из 5 — {title}"


def _fmt_dur(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} сек"
    m, s = divmod(seconds, 60)
    return f"{m} мин {s} сек" if s else f"{m} мин"


def _plural_channels(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} канал"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return f"{n} канала"
    return f"{n} каналов"


# ── Step 1: write text ─────────────────────────────────────────────────────


_STEP1_TEXT = (
    "✍️ <b>{step}</b>\n\n"
    "Напишите текст публикации и отправьте его сообщением.\n\n"
    "<i>Поддерживается форматирование Telegram: <b>жирный</b>, <i>курсив</i>, "
    "<code>моноширинный</code>, ссылки.</i>\n\n"
    "📏 <i>Лимит Telegram: {limit} символов для текста поста.</i>"
)

_STEP1_WITH_COUNT = (
    "✍️ <b>{step}</b>\n\n"
    "Напишите текст публикации и отправьте его сообщением.\n\n"
    "<i>Поддерживается форматирование Telegram: <b>жирный</b>, <i>курсив</i>, "
    "<code>моноширинный</code>, ссылки.</i>\n\n"
    "📏 <i>Символов: <b>{count}/{limit}</b> {warn}</i>"
)


async def _show_step1(target, edit: bool = True, char_count: int = 0) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="📄 Из шаблона", callback_data=QuickPostCb(action="from_template"))
    kb.button(text="❌ Отмена", callback_data=QuickPostCb(action="cancel"))
    kb.adjust(1)

    if char_count > 0:
        warn = "⚠️ Превышен лимит!" if char_count > _TG_CAPTION_LIMIT else ""
        text = _STEP1_WITH_COUNT.format(
            step=_step(1, "Текст поста"),
            count=char_count,
            limit=_TG_CAPTION_LIMIT,
            warn=warn,
        )
    else:
        text = _STEP1_TEXT.format(step=_step(1, "Текст поста"), limit=_TG_CAPTION_LIMIT)

    if edit:
        try:
            await target.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
        except Exception as _e:
            _es = str(_e).lower()
            if "message is not modified" in _es:
                pass
            elif "message to edit not found" in _es or "message can't be edited" in _es:
                await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
            elif "there is no text in the message to edit" in _es:
                try:
                    await target.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb.as_markup())
                except Exception:
                    await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
            else:
                log.warning("quick_post _show_step1 edit error: %s", _e)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.message(Command("post"))
async def cmd_post(message: Message, state: FSMContext) -> None:
    await state.set_state(QuickPostFSM.writing_text)
    await state.update_data(selected_chan_ids=[])
    await _show_step1(message, edit=False)


@router.callback_query(QuickPostCb.filter(F.action == "start"))
async def cb_qp_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(QuickPostFSM.writing_text)
    await state.update_data(selected_chan_ids=[])
    await _show_step1(callback.message)


@router.callback_query(QuickPostCb.filter(F.action == "cancel"))
async def cb_qp_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Создание поста отменено")
    try:
        await callback.message.edit_text(
            "❌ <b>Создание поста отменено.</b>\n\n"
            "Используйте /post или кнопку «✍️ Создать пост» в меню операций.",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ── Step 2: channel picker ─────────────────────────────────────────────────


async def _show_step2(
    target,
    channels: list,
    selected_ids: list[int],
    page: int,
    edit: bool = True,
) -> None:
    total = len(channels)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    slice_start = page * _PAGE_SIZE
    page_channels = channels[slice_start : slice_start + _PAGE_SIZE]
    sel_count = len(selected_ids)

    kb = InlineKeyboardBuilder()
    adjust_sizes: list[int] = []

    # Channel toggle buttons (1 per row)
    for ch in page_channels:
        ch_id = ch["id"]
        checked = "✅" if ch_id in selected_ids else "☐"
        title = (ch["title"] or f"id:{ch_id}")[:30]
        kb.button(
            text=f"{checked} {title}",
            callback_data=QuickPostCb(action="toggle", val=ch_id, page=page),
        )
    adjust_sizes.extend([1] * len(page_channels))

    # Navigation (◀️ ▶️) if multiple pages
    nav_count = 0
    if total_pages > 1:
        if page > 0:
            kb.button(text="◀️", callback_data=QuickPostCb(action="page", page=page - 1))
            nav_count += 1
        if page < total_pages - 1:
            kb.button(text="▶️", callback_data=QuickPostCb(action="page", page=page + 1))
            nav_count += 1
    if nav_count:
        adjust_sizes.append(nav_count)

    # Select all / deselect all
    kb.button(
        text="☑️ Выбрать все", callback_data=QuickPostCb(action="sel_all", page=page)
    )
    kb.button(
        text="☐ Снять все", callback_data=QuickPostCb(action="desel_all", page=page)
    )
    adjust_sizes.append(2)

    # Proceed button (only when channels are selected)
    if sel_count > 0:
        kb.button(
            text=f"Далее → ({_plural_channels(sel_count)})",
            callback_data=QuickPostCb(action="chans_done"),
        )
        adjust_sizes.append(1)

    # Back and cancel
    kb.button(text="◀️ К тексту", callback_data=QuickPostCb(action="back_to_text"))
    kb.button(text="❌ Отмена", callback_data=QuickPostCb(action="cancel"))
    adjust_sizes.extend([1, 1])

    kb.adjust(*adjust_sizes)

    page_label = f"  (стр. {page + 1}/{total_pages})" if total_pages > 1 else ""
    text = (
        f"📡 <b>{_step(2, 'Выберите каналы')}</b>\n\n"
        f"Выбрано: <b>{sel_count}</b> из {total}{page_label}\n\n"
        "<i>Нажмите на канал, чтобы выбрать/снять выбор.</i>"
    )
    if edit:
        try:
            await target.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            return
        except Exception as _e:
            _es = str(_e).lower()
            if "message is not modified" in _es:
                return
            if "message to edit not found" in _es or "message can't be edited" in _es:
                pass  # fall through to answer below
            else:
                log.warning("quick_post _show_step2 edit error: %s", _e)
                return
    await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.message(QuickPostFSM.writing_text)
async def msg_qp_text(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    text = (message.text or message.caption or "").strip()
    if not text:
        await message.answer(
            "⚠️ Текст не может быть пустым. Отправьте текст публикации:",
            parse_mode="HTML",
        )
        return

    char_count = len(text)
    if char_count > _TG_CAPTION_LIMIT:
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=QuickPostCb(action="cancel"))
        kb.adjust(1)
        await message.answer(
            f"⚠️ <b>Текст слишком длинный.</b>\n\n"
            f"Длина: <b>{char_count}</b> символов. Лимит Telegram: <b>{_TG_CAPTION_LIMIT}</b>.\n\n"
            "Сократите текст и отправьте снова:",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    channels = await _load_channels(pool, message.from_user.id)
    if not channels:
        await message.answer(
            "⚠️ <b>Каналов не найдено.</b>\n\n"
            "Сначала подключите аккаунты и импортируйте каналы:\n"
            "Меню → 📡 Каналы → 📥 Импорт из Telegram",
            parse_mode="HTML",
        )
        return

    sd = await state.get_data()
    selected_ids: list[int] = sd.get("selected_chan_ids", [])
    await state.update_data(post_text=text)
    await state.set_state(QuickPostFSM.picking_channels)

    sent = await message.answer(
        f"📡 Загружаю каналы… (текст: <b>{char_count}</b> символов)",
        parse_mode="HTML",
    )
    await _show_step2(sent, channels, selected_ids, page=0)


@router.callback_query(QuickPostCb.filter(F.action == "back_to_text"))
async def cb_qp_back_text(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(QuickPostFSM.writing_text)
    await _show_step1(callback.message)


# ── Template picker for Step 1 ─────────────────────────────────────────────


@router.callback_query(QuickPostCb.filter(F.action == "from_template"))
async def cb_qp_from_template(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    templates = await _load_post_templates(pool, callback.from_user.id)
    if not templates:
        kb = InlineKeyboardBuilder()
        kb.button(
            text="➕ Создать шаблон",
            callback_data=AssetTplCb(action="choose_type", asset_type="post"),
        )
        kb.button(
            text="◀️ Назад", callback_data=QuickPostCb(action="back_to_step1_prompt")
        )
        kb.adjust(1)
        await callback.message.edit_text(
            "📄 <b>Шаблоны постов</b>\n\n"
            "Шаблонов нет. Создайте первый шаблон, чтобы быстро переиспользовать тексты.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    kb = InlineKeyboardBuilder()
    for tpl in templates:
        try:
            data = (
                json.loads(tpl["template"])
                if isinstance(tpl["template"], str)
                else tpl["template"]
            )
            preview = (data.get("text") or "")[:40].replace("\n", " ")
        except Exception:
            preview = ""
        label = tpl["name"] or f"Шаблон #{tpl['id']}"
        btn_text = f"📝 {label}" + (f" — {preview}…" if preview else "")
        kb.button(
            text=btn_text[:64],
            callback_data=QuickPostCb(action="use_template", val=tpl["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=QuickPostCb(action="back_to_step1_prompt"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"📄 <b>Выберите шаблон поста</b>\n\nНайдено: {len(templates)} шт.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(QuickPostCb.filter(F.action == "back_to_step1_prompt"))
async def cb_qp_back_step1_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(QuickPostFSM.writing_text)
    await _show_step1(callback.message)


@router.callback_query(QuickPostCb.filter(F.action == "use_template"))
async def cb_qp_use_template(
    callback: CallbackQuery,
    callback_data: QuickPostCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    tpl_id = callback_data.val
    try:
        row = await pool.fetchrow(
            "SELECT template FROM asset_templates WHERE id=$1 AND owner_id=$2 AND asset_type='post'",
            tpl_id,
            callback.from_user.id,
        )
    except Exception as exc:
        mark_handled_error(f"qp_use_template: {exc}")
        await callback.answer("Ошибка загрузки шаблона.", show_alert=True)
        return
    if not row:
        await callback.answer("Шаблон не найден.", show_alert=True)
        return

    try:
        data = (
            json.loads(row["template"])
            if isinstance(row["template"], str)
            else row["template"]
        )
        text = data.get("text", "")
    except Exception:
        text = ""

    if not text:
        await callback.answer("Шаблон пустой.", show_alert=True)
        return

    await callback.answer("✅ Шаблон применён")
    channels = await _load_channels(pool, callback.from_user.id)
    if not channels:
        await callback.message.edit_text(
            "⚠️ <b>Каналов не найдено.</b>\n\n"
            "Сначала подключите аккаунты и импортируйте каналы:\n"
            "Меню → 📡 Каналы → 📥 Импорт из Telegram",
            parse_mode="HTML",
        )
        return

    sd = await state.get_data()
    selected_ids: list[int] = sd.get("selected_chan_ids", [])
    await state.update_data(post_text=text)
    await state.set_state(QuickPostFSM.picking_channels)
    await _show_step2(callback.message, channels, selected_ids, page=0)


@router.callback_query(
    QuickPostCb.filter(F.action == "toggle"), QuickPostFSM.picking_channels
)
async def cb_qp_toggle(
    callback: CallbackQuery,
    callback_data: QuickPostCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    sd = await state.get_data()
    selected_ids: list[int] = list(sd.get("selected_chan_ids", []))
    chan_id = callback_data.val
    if chan_id in selected_ids:
        selected_ids.remove(chan_id)
    else:
        selected_ids.append(chan_id)
    await state.update_data(selected_chan_ids=selected_ids)
    channels = await _load_channels(pool, callback.from_user.id)
    await _show_step2(callback.message, channels, selected_ids, page=callback_data.page)


@router.callback_query(
    QuickPostCb.filter(F.action == "page"), QuickPostFSM.picking_channels
)
async def cb_qp_page(
    callback: CallbackQuery,
    callback_data: QuickPostCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    sd = await state.get_data()
    selected_ids = sd.get("selected_chan_ids", [])
    channels = await _load_channels(pool, callback.from_user.id)
    await _show_step2(callback.message, channels, selected_ids, page=callback_data.page)


@router.callback_query(
    QuickPostCb.filter(F.action == "sel_all"), QuickPostFSM.picking_channels
)
async def cb_qp_sel_all(
    callback: CallbackQuery,
    callback_data: QuickPostCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    channels = await _load_channels(pool, callback.from_user.id)
    all_ids = [ch["id"] for ch in channels]
    await state.update_data(selected_chan_ids=all_ids)
    await callback.answer(f"✅ Выбрано {_plural_channels(len(all_ids))}")
    await _show_step2(callback.message, channels, all_ids, page=callback_data.page)


@router.callback_query(
    QuickPostCb.filter(F.action == "desel_all"), QuickPostFSM.picking_channels
)
async def cb_qp_desel_all(
    callback: CallbackQuery,
    callback_data: QuickPostCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    channels = await _load_channels(pool, callback.from_user.id)
    await state.update_data(selected_chan_ids=[])
    await callback.answer("☐ Выбор снят")
    await _show_step2(callback.message, channels, [], page=callback_data.page)


# ── Step 4: timing ─────────────────────────────────────────────────────────


def _timing_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for delay_s, label in _TIMING_OPTIONS.items():
        kb.button(text=label, callback_data=QuickPostCb(action="timing", val=delay_s))
    kb.button(text="◀️ К медиа", callback_data=QuickPostCb(action="back_to_media"))
    kb.button(text="❌ Отмена", callback_data=QuickPostCb(action="cancel"))
    kb.adjust(1)
    return kb


@router.callback_query(
    QuickPostCb.filter(F.action == "chans_done"), QuickPostFSM.picking_channels
)
async def cb_qp_chans_done(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    sd = await state.get_data()
    sel = sd.get("selected_chan_ids", [])
    if not sel:
        await callback.answer("⚠️ Выберите хотя бы один канал!", show_alert=True)
        return
    await callback.answer()
    await state.set_state(QuickPostFSM.uploading_media)
    await _show_step3_media(callback.message)


@router.callback_query(QuickPostCb.filter(F.action == "back_to_chans"))
async def cb_qp_back_chans(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    await state.set_state(QuickPostFSM.picking_channels)
    sd = await state.get_data()
    selected_ids = sd.get("selected_chan_ids", [])
    channels = await _load_channels(pool, callback.from_user.id)
    await _show_step2(callback.message, channels, selected_ids, page=0)


# ── Step 3: media upload ───────────────────────────────────────────────────


async def _show_step3_media(target, has_media: bool = False, edit: bool = True) -> None:
    """Показать шаг 3: прикрепление медиа."""
    kb = InlineKeyboardBuilder()
    if has_media:
        kb.button(text="🗑 Удалить медиа", callback_data=QuickPostCb(action="media_remove"))
    kb.button(text="⏭ Пропустить (без медиа)", callback_data=QuickPostCb(action="media_skip"))
    kb.button(text="◀️ К каналам", callback_data=QuickPostCb(action="back_to_chans"))
    kb.button(text="❌ Отмена", callback_data=QuickPostCb(action="cancel"))
    kb.adjust(1)

    media_hint = "✅ Медиа прикреплено." if has_media else "Медиа не выбрано."
    text = (
        f"🖼 <b>{_step(3, 'Медиа (необязательно)')}</b>\n\n"
        f"{media_hint}\n\n"
        "Отправьте <b>фото</b>, <b>видео</b> или <b>документ</b> — оно будет прикреплено к посту.\n"
        "Или нажмите <b>«Пропустить»</b>, чтобы публиковать только текст.\n\n"
        "<i>Поддерживаются: JPG, PNG, GIF, MP4, PDF и другие форматы Telegram.</i>"
    )
    if edit:
        try:
            await target.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            return
        except Exception as _e:
            _es = str(_e).lower()
            if "message is not modified" in _es:
                return
            if "there is no text in the message to edit" in _es:
                try:
                    await target.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb.as_markup())
                    return
                except Exception:
                    pass
            if "message to edit not found" in _es or "message can't be edited" in _es:
                pass  # fall through
            else:
                log.warning("quick_post _show_step3_media edit error: %s", _e)
    await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.message(QuickPostFSM.uploading_media, F.photo | F.video | F.document | F.animation)
async def msg_qp_media(message: Message, state: FSMContext) -> None:
    """Пользователь отправил медиа-файл на шаге 3."""
    if message.photo:
        file_id = message.photo[-1].file_id
        media_type = "photo"
    elif message.video:
        file_id = message.video.file_id
        media_type = "video"
    elif message.animation:
        file_id = message.animation.file_id
        media_type = "animation"
    elif message.document:
        file_id = message.document.file_id
        media_type = "document"
    else:
        await message.answer("⚠️ Неподдерживаемый тип медиа. Отправьте фото, видео или документ.")
        return

    await state.update_data(media_file_id=file_id, media_type=media_type)
    sent = await message.answer(
        f"✅ <b>Медиа прикреплено</b> ({media_type})\n\nНажмите «Далее» чтобы продолжить.",
        parse_mode="HTML",
    )
    await _show_step3_media(sent, has_media=True, edit=True)


@router.callback_query(QuickPostCb.filter(F.action == "media_remove"), QuickPostFSM.uploading_media)
async def cb_qp_media_remove(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("🗑 Медиа удалено")
    await state.update_data(media_file_id=None, media_type=None)
    await _show_step3_media(callback.message, has_media=False)


@router.callback_query(QuickPostCb.filter(F.action == "media_skip"), QuickPostFSM.uploading_media)
async def cb_qp_media_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(QuickPostFSM.picking_timing)
    sd = await state.get_data()
    sel = sd.get("selected_chan_ids", [])
    await callback.message.edit_text(
        f"⏱️ <b>{_step(4, 'Задержка между постами')}</b>\n\n"
        f"Выбрано: <b>{_plural_channels(len(sel))}</b>\n\n"
        "Задержка защищает аккаунты от временных ограничений.\n"
        "Для небольшого числа каналов подойдёт «Быстро».\n\n"
        "Выберите режим публикации:",
        parse_mode="HTML",
        reply_markup=_timing_kb().as_markup(),
    )


# ── Step 4: preview + confirm ──────────────────────────────────────────────


@router.callback_query(QuickPostCb.filter(F.action == "back_to_media"))
async def cb_qp_back_to_media(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(QuickPostFSM.uploading_media)
    sd = await state.get_data()
    has_media = bool(sd.get("media_file_id"))
    await _show_step3_media(callback.message, has_media=has_media)


@router.callback_query(
    QuickPostCb.filter(F.action == "timing"), QuickPostFSM.picking_timing
)
async def cb_qp_timing(
    callback: CallbackQuery,
    callback_data: QuickPostCb,
    state: FSMContext,
) -> None:
    await callback.answer()
    delay_s = callback_data.val
    await state.update_data(delay_s=delay_s)
    await state.set_state(QuickPostFSM.confirming)

    sd = await state.get_data()
    post_text: str = sd.get("post_text", "")
    sel: list[int] = sd.get("selected_chan_ids", [])
    media_file_id: str | None = sd.get("media_file_id")
    media_type: str | None = sd.get("media_type")
    timing_label = _TIMING_OPTIONS.get(delay_s, f"{delay_s} сек")

    effective_delay = 60 if delay_s < 0 else delay_s
    est_seconds = len(sel) * effective_delay
    preview = post_text[:300] + ("…" if len(post_text) > 300 else "")

    media_line = ""
    if media_file_id and media_type:
        _type_labels = {"photo": "📷 Фото", "video": "🎬 Видео", "animation": "🎞 GIF", "document": "📎 Документ"}
        media_line = f"Медиа: <b>{_type_labels.get(media_type, media_type)}</b> ✅\n"

    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"✅ Опубликовать! ({_plural_channels(len(sel))})",
        callback_data=QuickPostCb(action="publish"),
    )
    kb.button(
        text="💾 Сохранить как шаблон",
        callback_data=QuickPostCb(action="save_template"),
    )
    kb.button(text="◀️ К задержке", callback_data=QuickPostCb(action="back_to_timing"))
    kb.button(text="❌ Отмена", callback_data=QuickPostCb(action="cancel"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"👀 <b>{_step(5, 'Подтверждение')}</b>\n\n"
        f"Каналов: <b>{_plural_channels(len(sel))}</b>\n"
        f"Задержка: <b>{timing_label}</b>\n"
        f"Расчётное время: ~<b>{_fmt_dur(est_seconds)}</b>\n"
        f"{media_line}\n"
        f"Текст поста:\n———\n{html.escape(preview)}\n———",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(QuickPostCb.filter(F.action == "back_to_timing"))
async def cb_qp_back_timing(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(QuickPostFSM.picking_timing)
    sd = await state.get_data()
    sel = sd.get("selected_chan_ids", [])
    await callback.message.edit_text(
        f"⏱️ <b>{_step(4, 'Задержка между постами')}</b>\n\n"
        f"Выбрано: <b>{_plural_channels(len(sel))}</b>\n\n"
        "Выберите режим публикации:",
        parse_mode="HTML",
        reply_markup=_timing_kb().as_markup(),
    )


@router.callback_query(
    QuickPostCb.filter(F.action == "save_template"), QuickPostFSM.confirming
)
async def cb_qp_save_template(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    sd = await state.get_data()
    text = sd.get("post_text", "")
    if not text:
        await callback.answer("Нет текста для сохранения.", show_alert=True)
        return
    name_base = text[:40].strip().replace("\n", " ")
    name = name_base if name_base else "Быстрый пост"
    try:
        tpl_id = await _save_post_template(pool, callback.from_user.id, name, text)
        await callback.answer(f"✅ Шаблон сохранён (#{tpl_id})", show_alert=True)
    except Exception:
        log.exception("Ошибка сохранения шаблона быстрого поста")
        await callback.answer("❌ Не удалось сохранить шаблон.", show_alert=True)


# ── Publish ────────────────────────────────────────────────────────────────


@router.callback_query(
    QuickPostCb.filter(F.action == "publish"), QuickPostFSM.confirming
)
async def cb_qp_publish(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await state.clear()
        await callback.message.edit_text(
            locked_text("Публикация в каналы", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter", back_callback=BmCb(action="broadcasts")),
        )
        return
    await callback.answer("⏳ Запускаю публикацию…")
    sd = await state.get_data()
    await state.clear()

    post_text: str = sd.get("post_text", "")
    selected_chan_ids: list[int] = sd.get("selected_chan_ids", [])
    delay_s: int = sd.get("delay_s", 30)
    media_file_id: str | None = sd.get("media_file_id")
    media_type: str | None = sd.get("media_type")

    if not post_text or not selected_chan_ids:
        kb = InlineKeyboardBuilder()
        kb.button(text="✍️ Создать пост", callback_data=QuickPostCb(action="start"))
        kb.button(text="◀️ Назад", callback_data=BmCb(action="broadcasts"))
        kb.adjust(1)
        await callback.message.edit_text(
            "⚠️ <b>Данные сессии не найдены.</b>\n\nНачните заново или создайте новый пост:",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    # Content safety: запрещённый контент (CSAM / терроризм) не публикуется в каналы.
    from services import content_safety

    _v = await content_safety.enforce(pool, callback.from_user.id, post_text, surface="channel_post")
    if _v.blocked:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=BmCb(action="broadcasts"))
        kb.adjust(1)
        await callback.message.edit_text(
            content_safety.REFUSAL_TEXT,
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    try:
        rows = await pool.fetch(
            "SELECT DISTINCT ON (mc.channel_id) "
            "mc.channel_id AS id, mc.title, mc.access_hash, "
            "a.id AS acc_id, a.session_str, a.first_name, a.phone, "
            "a.device_model, a.system_version, a.app_version, p.proxy_url "
            "FROM managed_channels mc "
            "JOIN tg_accounts a ON a.id = mc.acc_id "
            "LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE "
            "WHERE mc.owner_id=$1 AND mc.channel_id = ANY($2::bigint[]) "
            "AND a.is_active = TRUE AND a.session_str IS NOT NULL "
            "ORDER BY mc.channel_id, a.id",
            callback.from_user.id,
            selected_chan_ids,
        )
    except Exception as exc:
        mark_handled_error(f"qp_post_confirm channels: {exc}")
        await callback.message.edit_text(
            "⚠️ Ошибка загрузки данных. Попробуйте ещё раз.",
            parse_mode="HTML",
        )
        return

    if not rows:
        await callback.message.edit_text(
            "⚠️ Не найдены активные аккаунты для выбранных каналов.\n"
            "Убедитесь, что аккаунты активны (/accounts).",
            parse_mode="HTML",
        )
        return

    total = len(rows)

    from services import operation_bus

    op_params: dict = {
        "text": post_text,
        "delay_seconds": delay_s,
        "channel_ids": selected_chan_ids,
    }
    if media_file_id and media_type:
        op_params["media_file_id"] = media_file_id
        op_params["media_type"] = media_type

    try:
        op_id = await operation_bus.submit(
            pool,
            callback.from_user.id,
            "mass_publish",
            op_params,
            total_items=total,
        )
    except Exception as _e:
        log.error("quick_post publish submit error: %s", _e)
        await callback.message.edit_text(
            "❌ <b>Ошибка постановки в очередь</b>\n\nПопробуйте ещё раз или обратитесь в поддержку.",
            parse_mode="HTML",
        )
        return

    if not op_id:
        await callback.message.edit_text(
            "⚠️ <b>Не удалось создать операцию</b>\n\nПовторите попытку через /post",
            parse_mode="HTML",
        )
        return

    await callback.message.edit_text(
        f"📤 <b>Публикация поставлена в очередь</b>\n\n"
        f"Каналов: <b>{total}</b>\n"
        f"ID операции: <code>#{op_id}</code>\n\n"
        f"Вы получите уведомление по завершении.\n"
        f"<i>Управление очередью: /ops → 📋 Очередь</i>",
        parse_mode="HTML",
    )
