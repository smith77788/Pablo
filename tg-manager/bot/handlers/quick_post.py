"""Quick Post Wizard — публикация в каналы за 4 шага.

Шаг 1: написать текст
Шаг 2: выбрать каналы
Шаг 3: задержка между постами
Шаг 4: предпросмотр + публикация

Точки входа:
  /post — команда бота
  QuickPostCb(action="start") — кнопка «✍️ Создать пост» в главном меню
"""

from __future__ import annotations

import asyncio
import html
import json
import logging

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import AssetTplCb, QuickPostCb
from bot.states import QuickPostFSM
from bot.handlers.mass_publish import _mpub_bg
from services import task_registry as _treg

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
    return await pool.fetch(
        "SELECT channel_id AS id, MAX(title) AS title, MAX(access_hash) AS access_hash, "
        "MIN(acc_id) AS acc_id "
        "FROM managed_channels WHERE owner_id=$1 "
        "GROUP BY channel_id ORDER BY MAX(title)",
        user_id,
    )


async def _load_post_templates(
    pool: asyncpg.Pool, user_id: int
) -> list[asyncpg.Record]:
    """Возвращает шаблоны постов пользователя."""
    return await pool.fetch(
        "SELECT id, name, template FROM asset_templates "
        "WHERE owner_id=$1 AND asset_type='post' ORDER BY created_at DESC LIMIT 10",
        user_id,
    )


async def _save_post_template(
    pool: asyncpg.Pool, user_id: int, name: str, text: str
) -> int:
    """Сохраняет текст поста как шаблон. Возвращает id нового шаблона."""
    row = await pool.fetchrow(
        "INSERT INTO asset_templates (owner_id, asset_type, name, template) "
        "VALUES ($1, 'post', $2, $3) RETURNING id",
        user_id,
        name,
        json.dumps({"text": text}),
    )
    return row["id"]


# ── Step indicator ─────────────────────────────────────────────────────────


def _step(n: int, title: str) -> str:
    dots = "●" * n + "○" * (4 - n)
    return f"{dots}  Шаг {n} из 4 — {title}"


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
        except Exception:
            await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
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
        except Exception:
            pass
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
    row = await pool.fetchrow(
        "SELECT template FROM asset_templates WHERE id=$1 AND owner_id=$2 AND asset_type='post'",
        tpl_id,
        callback.from_user.id,
    )
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


# ── Step 3: timing ─────────────────────────────────────────────────────────


def _timing_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for delay_s, label in _TIMING_OPTIONS.items():
        kb.button(text=label, callback_data=QuickPostCb(action="timing", val=delay_s))
    kb.button(text="◀️ К каналам", callback_data=QuickPostCb(action="back_to_chans"))
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
    await state.set_state(QuickPostFSM.picking_timing)
    await callback.message.edit_text(
        f"⏱️ <b>{_step(3, 'Задержка между постами')}</b>\n\n"
        f"Выбрано: <b>{_plural_channels(len(sel))}</b>\n\n"
        "Задержка защищает аккаунты от временных ограничений.\n"
        "Для небольшого числа каналов подойдёт «Быстро».\n\n"
        "Выберите режим публикации:",
        parse_mode="HTML",
        reply_markup=_timing_kb().as_markup(),
    )


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


# ── Step 4: preview + confirm ──────────────────────────────────────────────


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
    timing_label = _TIMING_OPTIONS.get(delay_s, f"{delay_s} сек")

    effective_delay = 60 if delay_s < 0 else delay_s
    est_seconds = len(sel) * effective_delay
    preview = post_text[:300] + ("…" if len(post_text) > 300 else "")

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
        f"👀 <b>{_step(4, 'Подтверждение')}</b>\n\n"
        f"Каналов: <b>{_plural_channels(len(sel))}</b>\n"
        f"Задержка: <b>{timing_label}</b>\n"
        f"Расчётное время: ~<b>{_fmt_dur(est_seconds)}</b>\n\n"
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
        f"⏱️ <b>{_step(3, 'Задержка между постами')}</b>\n\n"
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
    await callback.answer("⏳ Запускаю публикацию…")
    sd = await state.get_data()
    await state.clear()

    post_text: str = sd.get("post_text", "")
    selected_chan_ids: list[int] = sd.get("selected_chan_ids", [])
    delay_s: int = sd.get("delay_s", 30)

    if not post_text or not selected_chan_ids:
        await callback.message.edit_text(
            "⚠️ Данные сессии не найдены. Начните заново: /post",
            parse_mode="HTML",
        )
        return

    rows = await pool.fetch(
        "SELECT DISTINCT ON (mc.channel_id) "
        "mc.channel_id AS id, mc.title, mc.access_hash, "
        "a.id AS acc_id, a.session_str, a.first_name, a.phone, "
        "a.device_model, a.system_version, a.app_version, p.proxy_url "
        "FROM managed_channels mc "
        "JOIN tg_accounts a ON a.id = mc.acc_id "
        "LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE "
        "WHERE mc.owner_id=$1 AND mc.channel_id = ANY($2::bigint[]) "
        "AND a.is_active = TRUE "
        "ORDER BY mc.channel_id, a.id",
        callback.from_user.id,
        selected_chan_ids,
    )

    if not rows:
        await callback.message.edit_text(
            "⚠️ Не найдены активные аккаунты для выбранных каналов.\n"
            "Убедитесь, что аккаунты активны (/accounts).",
            parse_mode="HTML",
        )
        return

    pairs = []
    for row in rows:
        acc_dict = {
            "id": row["acc_id"],
            "session_str": row["session_str"],
            "first_name": row["first_name"],
            "phone": row["phone"],
            "device_model": row["device_model"],
            "system_version": row["system_version"],
            "app_version": row["app_version"],
            "proxy_url": row["proxy_url"],
        }
        chan_dict = {
            "id": row["id"],
            "title": row["title"],
            "access_hash": row["access_hash"],
        }
        pairs.append((acc_dict, chan_dict))

    total = len(pairs)
    progress_msg = await callback.message.edit_text(
        f"📤 <b>Публикация запущена!</b>\n\n"
        f"Каналов: <b>{total}</b>\n"
        f"<i>Прогресс обновляется автоматически.\nДля отмены используйте /tasks</i>",
        parse_mode="HTML",
    )

    task = asyncio.create_task(
        _mpub_bg(
            bot=callback.bot,
            user_id=callback.from_user.id,
            progress_msg=progress_msg,
            pairs=pairs,
            post_text=post_text,
            delay_s=delay_s,
        )
    )
    _treg.register(
        callback.from_user.id,
        "publish",
        f"Быстрый пост → {_plural_channels(total)}",
        task,
    )
