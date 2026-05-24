"""Deep links manager and referral tracking."""
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
import asyncpg
from bot.callbacks import DeepLinkCb, BotCb
from bot.keyboards import deeplinks_menu, deeplink_view_menu, back_to_bot
from bot.states import CreateDeepLink
from database import db

router = Router()


@router.callback_query(DeepLinkCb.filter(F.action == "menu"))
async def cb_dl_menu(callback: CallbackQuery, callback_data: DeepLinkCb,
                      pool: asyncpg.Pool) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    links = await db.get_deep_links(pool, callback_data.bot_id)
    total_refs = await db.get_referral_total(pool, callback_data.bot_id)
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    total_clicks = sum(lnk["click_count"] for lnk in links)
    await callback.message.edit_text(
        f"🔗 <b>Диплинки — {label}</b>\n\n"
        f"Ссылок: <b>{len(links)}</b>\n"
        f"Кликов всего: <b>{total_clicks}</b>\n"
        f"Рефералов: <b>{total_refs}</b>\n\n"
        "Каждый диплинк — это уникальный /start параметр для отслеживания источников трафика.\n"
        "Формат ссылки: <code>t.me/username?start=PARAM</code>",
        parse_mode="HTML",
        reply_markup=deeplinks_menu(callback_data.bot_id, links),
    )
    await callback.answer()


@router.callback_query(DeepLinkCb.filter(F.action == "view"))
async def cb_dl_view(callback: CallbackQuery, callback_data: DeepLinkCb,
                      pool: asyncpg.Pool) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    links = await db.get_deep_links(pool, callback_data.bot_id)
    link = next((l for l in links if l["id"] == callback_data.link_id), None)
    if not link:
        await callback.answer("Ссылка не найдена.", show_alert=True)
        return
    username = row.get("username") or ""
    url = f"https://t.me/{username}?start={link['start_param']}" if username else f"start={link['start_param']}"
    ctr = round(link['unique_users'] / link['click_count'] * 100, 1) if link['click_count'] else 0
    await callback.message.edit_text(
        f"🔗 <b>{link['name']}</b>\n\n"
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
    await callback.answer()


@router.callback_query(DeepLinkCb.filter(F.action == "create"))
async def cb_dl_create(callback: CallbackQuery, callback_data: DeepLinkCb,
                        state: FSMContext) -> None:
    await state.set_state(CreateDeepLink.waiting_name)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "🔗 <b>Новый диплинк</b>\n\n"
        "Введите название ссылки (для вашего удобства):\n"
        "Например: <code>Instagram bio</code>, <code>VK post</code>, <code>YouTube desc</code>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(CreateDeepLink.waiting_name)
async def msg_dl_name(message: Message, state: FSMContext) -> None:
    await state.update_data(link_name=message.text.strip())
    await state.set_state(CreateDeepLink.waiting_param)
    await message.answer(
        "🔗 Введите уникальный <b>start параметр</b> (латиница, цифры, _, -):\n\n"
        "Например: <code>insta</code>, <code>vk2024</code>, <code>yt_video1</code>\n\n"
        "<i>Будет использоваться в ссылке: t.me/bot?start=ВАШ_ПАРАМЕТР</i>",
        parse_mode="HTML",
    )


@router.message(CreateDeepLink.waiting_param)
async def msg_dl_param(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    await state.clear()
    param = message.text.strip().replace(" ", "_")
    # Validate param
    import re
    if not re.match(r'^[a-zA-Z0-9_-]{1,50}$', param):
        await message.answer(
            "❌ Параметр должен содержать только латиницу, цифры, _ или - (максимум 50 символов).\n"
            "Попробуйте ещё раз: /start или нажмите кнопку снова."
        )
        return
    try:
        link_id = await db.create_deep_link(pool, data["bot_id"], data["link_name"], param)
    except Exception:
        await message.answer("❌ Такой параметр уже существует. Используйте другой.")
        return
    row = await db.get_bot(pool, data["bot_id"], message.from_user.id)
    username = row.get("username") or "" if row else ""
    url = f"https://t.me/{username}?start={param}" if username else f"?start={param}"
    await message.answer(
        f"✅ <b>Диплинк создан!</b>\n\n"
        f"Название: {data['link_name']}\n"
        f"Ссылка: <code>{url}</code>\n\n"
        "Поделитесь этой ссылкой в нужном месте и отслеживайте трафик.",
        parse_mode="HTML",
        reply_markup=back_to_bot(data["bot_id"]),
    )


@router.callback_query(DeepLinkCb.filter(F.action == "delete"))
async def cb_dl_delete(callback: CallbackQuery, callback_data: DeepLinkCb,
                        pool: asyncpg.Pool) -> None:
    await db.delete_deep_link(pool, callback_data.link_id, callback_data.bot_id)
    links = await db.get_deep_links(pool, callback_data.bot_id)
    total_refs = await db.get_referral_total(pool, callback_data.bot_id)
    await callback.message.edit_text(
        f"🔗 <b>Диплинки</b>\n\nСсылок: {len(links)} | Рефералов: {total_refs}",
        parse_mode="HTML",
        reply_markup=deeplinks_menu(callback_data.bot_id, links),
    )
    await callback.answer("🗑 Диплинк удалён.")


@router.callback_query(DeepLinkCb.filter(F.action == "leaders"))
async def cb_dl_leaders(callback: CallbackQuery, callback_data: DeepLinkCb,
                         pool: asyncpg.Pool) -> None:
    leaders = await db.get_referral_leaderboard(pool, callback_data.bot_id, limit=10)
    total = await db.get_referral_total(pool, callback_data.bot_id)
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=DeepLinkCb(action="menu", bot_id=callback_data.bot_id))
    if leaders:
        lines = []
        medals = ["🥇", "🥈", "🥉"] + ["🎖"] * 10
        for i, l in enumerate(leaders):
            lines.append(f"{medals[i]} ID <code>{l['referrer_user_id']}</code> — {l['referral_count']} чел.")
        body = "\n".join(lines)
    else:
        body = "Рефералов пока нет."
    await callback.message.edit_text(
        f"🏆 <b>Топ рефереров</b>\n\nВсего рефералов: <b>{total}</b>\n\n{body}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()
