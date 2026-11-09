"""Легитимная жалоба по почте — команда /report_abuse.

ОДНА достоверная жалоба оператора со своего подключённого почтового ящика в
официальный abuse-адрес Telegram, с доказательствами как ссылками/ID. Это НЕ
флот и не массовая рассылка — один оператор, одно письмо. Движок —
services/abuse_report. Сам запрещённый материал бот не принимает: только ссылки.
"""
from __future__ import annotations

import html
import logging

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from services import abuse_report, report_channels

log = logging.getLogger(__name__)
router = Router()


class AbuseFSM(StatesGroup):
    mailbox = State()
    category = State()
    recipient = State()
    target = State()
    evidence = State()
    context = State()
    confirm = State()


def _cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="ar:cancel")
    return kb.as_markup()


def _cat_kb():
    kb = InlineKeyboardBuilder()
    for c in abuse_report.categories_public():
        kb.button(text=c["label"], callback_data=f"ar:cat:{c['key']}")
    kb.button(text="❌ Отмена", callback_data="ar:cancel")
    kb.adjust(1)
    return kb.as_markup()


_INTRO = (
    "📧 <b>Легитимная жалоба по почте</b>\n\n"
    "Одно обоснованное обращение с <b>вашего</b> подключённого почтового ящика в "
    "официальный abuse-адрес Telegram. Решение о блокировке принимает модерация — "
    "это обращение с доказательствами, а не гарантия удаления.\n\n"
    "⚠️ Прикладывайте только <b>ссылки и ID</b> на нарушение (ссылка на сообщение, "
    "t.me/…). <b>Сам запрещённый материал пересылать нельзя</b> — это противозаконно.\n\n"
    "Выберите категорию нарушения:"
)


@router.message(Command("report_abuse"))
async def cmd_report_abuse(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    await state.clear()
    rows = await pool.fetch(
        'SELECT id, email FROM strike_email_accounts '
        'WHERE owner_id=$1 AND is_active=TRUE ORDER BY id', message.from_user.id)
    if not rows:
        from bot.callbacks import StrikeCb
        kb = InlineKeyboardBuilder()
        kb.button(text='Подключить почту', callback_data=StrikeCb(action='emails').pack())
        await message.answer(
            '📧 Жалоба уходит письмом в официальный abuse-адрес Telegram с вашего '
            'почтового ящика — так ответ Telegram придёт вам. Подключите ящик '
            '(Gmail, Яндекс, Mail.ru и др.: адрес и пароль приложения) и вернитесь '
            'к /report_abuse.',
            reply_markup=kb.as_markup())
        return
    await state.set_state(AbuseFSM.mailbox)
    kb = InlineKeyboardBuilder()
    for row in rows:
        kb.button(text=row['email'], callback_data=f"ar:mail:{row['id']}")
    kb.button(text='❌ Отмена', callback_data='ar:cancel')
    kb.adjust(1)
    await message.answer('Выберите свой ящик для одного обращения:', reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith('ar:mail:'), AbuseFSM.mailbox)
async def cb_mailbox(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    raw = callback.data.split(':', 2)[2]
    if not raw.isdigit() or len(raw) > 18:
        await callback.answer('Неверный ящик', show_alert=True)
        return
    row = await pool.fetchrow(
        'SELECT id, email FROM strike_email_accounts '
        'WHERE owner_id=$1 AND id=$2 AND is_active=TRUE', callback.from_user.id, int(raw))
    if not row:
        await callback.answer('Ящик отключён или недоступен', show_alert=True)
        return
    await state.update_data(mailbox_id=row['id'], mailbox_email=row['email'])
    await state.set_state(AbuseFSM.category)
    await callback.message.edit_text(_INTRO, parse_mode='HTML', reply_markup=_cat_kb())
    await callback.answer()


@router.callback_query(F.data == "ar:cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await callback.message.edit_text("Отменено. Наберите /report_abuse, чтобы начать заново.")
    except Exception:
        await callback.message.answer("Отменено. Наберите /report_abuse, чтобы начать заново.")
    await callback.answer()


@router.callback_query(F.data.startswith("ar:cat:"))
async def cb_category(callback: CallbackQuery, state: FSMContext) -> None:
    cat = callback.data.split(":", 2)[2]
    if cat not in abuse_report.CATEGORIES:
        await callback.answer("Неизвестная категория", show_alert=True)
        return
    await state.update_data(category=cat)
    await state.set_state(AbuseFSM.recipient)
    kb = InlineKeyboardBuilder()
    for channel in report_channels.for_category(cat):
        if channel['kind'] == 'email':
            kb.button(text=channel['label'], callback_data='ar:to:' + channel['key'])
        else:
            kb.button(text=channel['label'] + ' ↗', url=channel['address'])
    kb.button(text='❌ Отмена', callback_data='ar:cancel')
    kb.adjust(1)
    recommended = report_channels.email_recipient(cat)
    await callback.message.edit_text(
        f"Рекомендуемый получатель: <code>{html.escape(recommended)}</code>\n\n"
        "Выберите почтового получателя ниже. Ссылки ↗ открывают официальную "
        "форму для самостоятельной подачи и ничего не отправляют.\n\n"
        "App Store и Google Play: только жалоба на само приложение с описанием "
        "нарушения правил магазина, не на отдельный канал Telegram. "
        "Вход в аккаунт магазина может потребоваться.\n\n"
        f"Адреса сверены: {report_channels.CHECKED_ON}.",
        parse_mode='HTML', reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith('ar:to:'), AbuseFSM.recipient)
async def cb_recipient(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    key = callback.data.split(':', 2)[2]
    try:
        recipient = report_channels.email_recipient(data.get('category', ''), key)
    except ValueError:
        await callback.answer('Получатель недоступен для этой категории', show_alert=True)
        return
    await state.update_data(recipient_key=key)
    await state.set_state(AbuseFSM.target)
    await callback.message.edit_text(
        f"Получатель: <code>{html.escape(recipient)}</code>\n\n"
        "Пришлите <b>цель</b>: @username, ссылку t.me/… или ID ресурса.",
        parse_mode="HTML", reply_markup=_cancel_kb())
    await callback.answer()


@router.message(AbuseFSM.target)
async def st_target(message: Message, state: FSMContext) -> None:
    await state.update_data(target=(message.text or "").strip())
    await state.set_state(AbuseFSM.evidence)
    await message.answer(
        "Пришлите <b>доказательства — ссылки/ID</b>, по одной на строку "
        "(ссылки на конкретные сообщения t.me/…). <b>Только ссылки, не сам материал.</b>",
        parse_mode="HTML", reply_markup=_cancel_kb())


@router.message(AbuseFSM.evidence)
async def st_evidence(message: Message, state: FSMContext) -> None:
    # Только текст: медиа не принимаем (сам материал пересылать нельзя).
    if not (message.text or "").strip():
        await message.answer(
            "Нужны <b>ссылки/ID текстом</b>, по одной на строку. Медиа и файлы "
            "присылать нельзя — только ссылки на нарушение.",
            parse_mode="HTML", reply_markup=_cancel_kb())
        return
    await state.update_data(evidence=message.text)
    await state.set_state(AbuseFSM.context)
    await message.answer(
        "Опишите нарушение словами: что именно нарушает правила и почему (это пойдёт "
        "в текст обращения).", reply_markup=_cancel_kb())


@router.message(AbuseFSM.context)
async def st_context(message: Message, state: FSMContext) -> None:
    await state.update_data(context=(message.text or "").strip())
    data = await state.get_data()
    clean, errors = abuse_report.validate_report(data)
    if errors:
        await message.answer(
            "⚠️ Обращение не готово:\n• " + "\n• ".join(html.escape(e) for e in errors)
            + "\n\nИсправьте и пришлите описание ещё раз, либо отмените.",
            reply_markup=_cancel_kb())
        return
    rcpts = report_channels.email_recipient(clean['category'], clean.get('recipient_key'))
    await state.set_state(AbuseFSM.confirm)
    kb = InlineKeyboardBuilder()
    kb.button(text="📧 Отправить со своей почты", callback_data="ar:send")
    kb.button(text="❌ Отмена", callback_data="ar:cancel")
    kb.adjust(1)
    await message.answer(
        f"Проверьте обращение:\n\n"
        f"<b>Цель:</b> {html.escape(clean['target'])}\n"
        f"<b>Категория:</b> {html.escape(abuse_report.CATEGORIES[clean['category']]['label'])}\n"
        f"<b>Доказательств (ссылок):</b> {len(clean['evidence'])}\n"
        f"<b>Куда:</b> {html.escape(rcpts)}\n\n"
        f"<b>С ящика:</b> {html.escape(str(data.get('mailbox_email') or ''))}\n\n"
        "Письмо уйдёт с вашего подключённого ящика. «Отправлено» ≠ «заблокировано».",
        parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(F.data == "ar:send", AbuseFSM.confirm)
async def cb_send(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    await state.clear()
    await callback.answer("Отправляю…")
    res = await abuse_report.send_report(pool, callback.from_user.id, data)
    status = res.get("status")
    if status == "sent":
        txt = (f"✅ Обращение отправлено (№ <code>{res['ref_id']}</code>).\n\n"
               "Решение о блокировке принимает модерация платформы — это <b>не</b> "
               "подтверждение удаления ресурса.")
        esc = res.get("escalation") or []
        if esc:
            txt += ("\n\n<b>Для такого нарушения подайте отдельно в органы</b> "
                    "(они реально запускают удаление и расследование):\n• "
                    + "\n• ".join(html.escape(e) for e in esc))
    elif status == "no_mailbox":
        txt = ("📭 " + html.escape(res.get("message") or "Почта не подключена.")
               + "\n\nПодключите свою почту и повторите /report_abuse.")
    elif status == "invalid":
        txt = "⚠️ Обращение не прошло проверку:\n• " + "\n• ".join(
            html.escape(e) for e in res.get("errors", []))
    else:
        txt = ("⚠️ Не удалось отправить письмо с вашего ящика"
               + (f": {html.escape(res.get('error') or '')}" if res.get("error") else "")
               + ".\nПроверьте подключение почты и повторите /report_abuse.")
    try:
        await callback.message.edit_text(txt, parse_mode="HTML")
    except Exception:
        await callback.message.answer(txt, parse_mode="HTML")
