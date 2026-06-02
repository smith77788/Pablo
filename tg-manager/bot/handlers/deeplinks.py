"""Deep links manager and referral tracking."""

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
import asyncpg
from bot.callbacks import DeepLinkCb, BmCb
from bot.keyboards import (
    deeplinks_menu,
    deeplink_view_menu,
    back_to_bot,
    subscription_locked_markup,
)
from bot.states import CreateDeepLink
from bot.utils.subscription import require_plan, locked_text
from database import db
from aiogram.utils.keyboard import InlineKeyboardBuilder

router = Router()


def _dl_cancel_kb(bot_id: int) -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=DeepLinkCb(action="menu", bot_id=bot_id))
    return kb.as_markup()


@router.callback_query(DeepLinkCb.filter(F.action == "menu"))
async def cb_dl_menu(
    callback: CallbackQuery, callback_data: DeepLinkCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.message.edit_text(
            locked_text("Диплинки и рефералы", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter", back_callback=BmCb(action="main")),
        )
        return
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.message.edit_text(
            "❌ Бот не найден. Возможно, он был удалён.",
            parse_mode="HTML",
        )
        return
    links = await db.get_deep_links(pool, callback_data.bot_id)
    total_refs = await db.get_referral_total(pool, callback_data.bot_id)
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    total_clicks = sum(lnk["click_count"] for lnk in links)
    if not links:
        empty_hint = (
            "\n\n💡 <b>Создайте первый диплинк!</b>\n"
            "Нажмите «➕ Создать диплинк» и придумайте метку источника — "
            "например <code>instagram</code> или <code>youtube</code>. "
            "Поделитесь ссылкой и смотрите, откуда приходят подписчики."
        )
    else:
        empty_hint = ""
    await callback.message.edit_text(
        f"🔗 <b>Диплинки — {label}</b>\n\n"
        "📌 <b>Что это?</b>\n"
        "Диплинк — это специальная ссылка на вашего бота с меткой. Когда человек переходит по ней и нажимает Start — система записывает, откуда он пришёл.\n\n"
        "💡 <b>Зачем нужно?</b>\n"
        "Создайте разные ссылки для Instagram, ВКонтакте, YouTube — и точно узнайте, какой канал приносит больше всего подписчиков.\n\n"
        f"Ссылок создано: <b>{len(links)}</b>\n"
        f"Кликов всего: <b>{total_clicks}</b>\n"
        f"Рефералов: <b>{total_refs}</b>\n\n"
        f"Формат ссылки: <code>t.me/username?start=ПАРАМЕТР</code>"
        f"{empty_hint}",
        parse_mode="HTML",
        reply_markup=deeplinks_menu(callback_data.bot_id, links),
    )


@router.callback_query(DeepLinkCb.filter(F.action == "view"))
async def cb_dl_view(
    callback: CallbackQuery, callback_data: DeepLinkCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.message.edit_text(
            "❌ Бот не найден. Возможно, он был удалён.",
            parse_mode="HTML",
        )
        return
    links = await db.get_deep_links(pool, callback_data.bot_id)
    link = next((l for l in links if l["id"] == callback_data.link_id), None)
    if not link:
        await callback.answer("Ссылка не найдена.", show_alert=True)
        return
    username = row.get("username") or ""
    url = (
        f"https://t.me/{username}?start={link['start_param']}"
        if username
        else f"start={link['start_param']}"
    )
    ctr = (
        round(link["unique_users"] / link["click_count"] * 100, 1)
        if link["click_count"]
        else 0
    )
    safe_name = (
        link["name"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    await callback.message.edit_text(
        f"🔗 <b>{safe_name}</b>\n\n"
        f"Параметр: <code>{link['start_param']}</code>\n"
        f"Ссылка: <code>{url}</code>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"  Кликов: <b>{link['click_count']}</b>\n"
        f"  Уникальных: <b>{link['unique_users']}</b>\n"
        f"  Повторные: {link['click_count'] - link['unique_users']}\n"
        f"  Уник. %: {ctr}%\n\n"
        f"Создана: {link['created_at'].strftime('%d.%m.%Y')}",
        parse_mode="HTML",
        reply_markup=deeplink_view_menu(callback_data.bot_id, callback_data.link_id),
    )


@router.callback_query(DeepLinkCb.filter(F.action == "create"))
async def cb_dl_create(
    callback: CallbackQuery, callback_data: DeepLinkCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(CreateDeepLink.waiting_name)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "🔗 <b>Новый диплинк</b>\n\n"
        "Введите название ссылки (для вашего удобства):\n"
        "Например: <code>Instagram bio</code>, <code>VK post</code>, <code>YouTube desc</code>",
        parse_mode="HTML",
        reply_markup=_dl_cancel_kb(callback_data.bot_id),
    )


@router.message(CreateDeepLink.waiting_name, F.text)
async def msg_dl_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not name:
        data = await state.get_data()
        await message.answer("⚠️ Название не может быть пустым. Введите снова:", reply_markup=_dl_cancel_kb(data.get("bot_id", 0)))
        return
    if len(name) > 200:
        await message.answer("⚠️ Слишком длинное название (макс. 200 символов). Введите снова:", reply_markup=_dl_cancel_kb((await state.get_data()).get("bot_id", 0)))
        return
    await state.update_data(link_name=name)
    await state.set_state(CreateDeepLink.waiting_param)
    data = await state.get_data()
    await message.answer(
        "🔗 Введите уникальный <b>start параметр</b> (латиница, цифры, _, -):\n\n"
        "Например: <code>insta</code>, <code>vk2024</code>, <code>yt_video1</code>\n\n"
        "<i>Будет использоваться в ссылке: t.me/bot?start=ВАШ_ПАРАМЕТР</i>",
        parse_mode="HTML",
        reply_markup=_dl_cancel_kb(data.get("bot_id", 0)),
    )


@router.message(CreateDeepLink.waiting_param, F.text)
async def msg_dl_param(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    import re

    data = await state.get_data()
    param = message.text.strip().replace(" ", "_")
    # Validate before clearing state so user can retry
    if not re.match(r"^[a-zA-Z0-9_-]{1,50}$", param):
        await message.answer(
            "❌ Параметр должен содержать только латиницу, цифры, _ или - (максимум 50 символов).\n\n"
            "Попробуйте снова:",
            reply_markup=_dl_cancel_kb(data.get("bot_id", 0)),
        )
        return  # keep state active so user can send another value
    try:
        await db.create_deep_link(pool, data["bot_id"], data["link_name"], param)
    except Exception:
        await message.answer(
            "❌ Параметр <b>{}</b> уже занят. Введите другой:".format(param),
            parse_mode="HTML",
        )
        return  # keep state active
    await state.clear()
    row = await db.get_bot(pool, data["bot_id"], message.from_user.id)
    username = row.get("username") or "" if row else ""
    url = f"https://t.me/{username}?start={param}" if username else f"?start={param}"
    safe_link_name = (
        data["link_name"]
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    await message.answer(
        f"✅ <b>Диплинк создан!</b>\n\n"
        f"Название: {safe_link_name}\n"
        f"Ссылка: <code>{url}</code>\n\n"
        "Поделитесь этой ссылкой в нужном месте и отслеживайте трафик.",
        parse_mode="HTML",
        reply_markup=back_to_bot(data["bot_id"]),
    )


@router.callback_query(DeepLinkCb.filter(F.action == "delete"))
async def cb_dl_delete(
    callback: CallbackQuery, callback_data: DeepLinkCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("🗑 Диплинк удалён.")
    await db.delete_deep_link(pool, callback_data.link_id, callback_data.bot_id)
    links = await db.get_deep_links(pool, callback_data.bot_id)
    total_refs = await db.get_referral_total(pool, callback_data.bot_id)
    if not links:
        empty_hint = (
            "\n\n💡 <b>Список пуст.</b> Нажмите «➕ Создать диплинк», "
            "чтобы добавить новую ссылку с меткой источника."
        )
    else:
        empty_hint = ""
    await callback.message.edit_text(
        f"🔗 <b>Диплинки</b>\n\nСсылок: {len(links)} | Рефералов: {total_refs}{empty_hint}",
        parse_mode="HTML",
        reply_markup=deeplinks_menu(callback_data.bot_id, links),
    )


@router.callback_query(DeepLinkCb.filter(F.action == "leaders"))
async def cb_dl_leaders(
    callback: CallbackQuery, callback_data: DeepLinkCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    leaders = await db.get_referral_leaderboard(pool, callback_data.bot_id, limit=10)
    total = await db.get_referral_total(pool, callback_data.bot_id)
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()
    kb.button(
        text="◀️ Назад",
        callback_data=DeepLinkCb(action="menu", bot_id=callback_data.bot_id),
    )
    if leaders:
        lines = []
        medals = ["🥇", "🥈", "🥉"] + ["🎖"] * 10
        for i, l in enumerate(leaders):
            lines.append(
                f"{medals[i]} ID <code>{l['referrer_user_id']}</code> — {l['referral_count']} чел."
            )
        body = "\n".join(lines)
    else:
        body = "Рефералов пока нет."
    await callback.message.edit_text(
        f"🏆 <b>Топ рефереров</b>\n\nВсего рефералов: <b>{total}</b>\n\n{body}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
