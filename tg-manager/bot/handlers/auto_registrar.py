"""Авторег — автоматическая регистрация аккаунтов Telegram через SMS-сервисы.

Поддерживает: 5sim.net, sms-activate.org
Процесс: выбрать страну → заказать номер → получить OTP через SMS API → авторизоваться.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re

import asyncpg
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import AutoRegCb, BmCb
from database import db

log = logging.getLogger(__name__)
router = Router()

_SERVICES = {
    "5sim": "5sim.net",
    "smsactivate": "sms-activate.org",
}

_SETTING_SERVICE = "sms_api_service"
_SETTING_5SIM_KEY = "sms_api_5sim_key"
_SETTING_SMSA_KEY = "sms_api_smsa_key"


class AutoRegFSM(StatesGroup):
    set_key = State()     # ввод API-ключа для выбранного сервиса
    set_2fa = State()     # ввод 2FA-пароля (если потребуется)


# ── helpers ───────────────────────────────────────────────────────────────────


async def _get_sms_client(pool: asyncpg.Pool):
    """Создаёт SMS API клиент из настроек. Возвращает (client, service_name) или (None, '')."""
    from services.sms_api_engine import get_sms_client
    service = await db.get_platform_setting(pool, _SETTING_SERVICE, "5sim")
    if service == "5sim":
        key = await db.get_platform_setting(pool, _SETTING_5SIM_KEY, "")
    else:
        key = await db.get_platform_setting(pool, _SETTING_SMSA_KEY, "")
    if not key:
        return None, service
    return get_sms_client(service, key), service


def _back_kb() -> object:
    return InlineKeyboardBuilder().button(
        text="◀️ Назад", callback_data=AutoRegCb(action="menu")
    ).as_markup()


# ── Главное меню ──────────────────────────────────────────────────────────────


@router.callback_query(AutoRegCb.filter(F.action == "menu"))
async def cb_autoreg_menu(cb: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await state.clear()
    service = await db.get_platform_setting(pool, _SETTING_SERVICE, "5sim")
    service_label = _SERVICES.get(service, service)

    if service == "5sim":
        key = await db.get_platform_setting(pool, _SETTING_5SIM_KEY, "")
    else:
        key = await db.get_platform_setting(pool, _SETTING_SMSA_KEY, "")

    balance_str = ""
    if key:
        try:
            from services.sms_api_engine import get_sms_client
            client = get_sms_client(service, key)
            balance = await asyncio.wait_for(client.get_balance(), timeout=8)
            balance_str = f" — баланс: <b>${balance:.2f}</b>"
        except Exception:
            balance_str = " — <i>не удалось получить баланс</i>"

    key_status = f"✅ Ключ задан{balance_str}" if key else "❌ Ключ не настроен"

    text = (
        "<b>🤖 Авторег</b>\n\n"
        "Автоматическая регистрация Telegram-аккаунтов через виртуальные номера.\n\n"
        f"<b>Сервис:</b> {service_label}\n"
        f"<b>API-ключ:</b> {key_status}\n\n"
        "Зарегистрированные аккаунты автоматически сохраняются в «Мои аккаунты»."
    )
    kb = InlineKeyboardBuilder()
    if key:
        kb.button(text="🌍 Выбрать страну и зарегистрировать", callback_data=AutoRegCb(action="pick_country"))
    kb.button(text="⚙️ Настройки SMS API", callback_data=AutoRegCb(action="settings"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
    kb.adjust(1)
    try:
        await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await cb.answer()


# ── Настройки ─────────────────────────────────────────────────────────────────


@router.callback_query(AutoRegCb.filter(F.action == "settings"))
async def cb_autoreg_settings(cb: CallbackQuery, pool: asyncpg.Pool) -> None:
    service = await db.get_platform_setting(pool, _SETTING_SERVICE, "5sim")
    text = (
        "<b>⚙️ Настройки SMS API</b>\n\n"
        f"Текущий сервис: <b>{_SERVICES.get(service, service)}</b>\n\n"
        "Выберите сервис и введите API-ключ:"
    )
    kb = InlineKeyboardBuilder()
    for svc_key, svc_name in _SERVICES.items():
        mark = "✅ " if service == svc_key else ""
        kb.button(text=f"{mark}{svc_name}", callback_data=AutoRegCb(action="set_service", sub=svc_key))
    kb.button(text="🔑 Ввести API-ключ", callback_data=AutoRegCb(action="set_key"))
    kb.button(text="◀️ Назад", callback_data=AutoRegCb(action="menu"))
    kb.adjust(1)
    try:
        await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await cb.answer()


@router.callback_query(AutoRegCb.filter(F.action == "set_service"))
async def cb_autoreg_set_service(cb: CallbackQuery, callback_data: AutoRegCb, pool: asyncpg.Pool) -> None:
    service = callback_data.sub
    if service not in _SERVICES:
        await cb.answer("Неизвестный сервис", show_alert=True)
        return
    await db.set_platform_setting(pool, _SETTING_SERVICE, service)
    await cb.answer(f"✅ Сервис: {_SERVICES[service]}")
    await cb_autoreg_settings(cb, pool)


@router.callback_query(AutoRegCb.filter(F.action == "set_key"))
async def cb_autoreg_set_key(cb: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    service = await db.get_platform_setting(pool, _SETTING_SERVICE, "5sim")
    service_label = _SERVICES.get(service, service)
    await state.set_state(AutoRegFSM.set_key)
    await state.update_data(service=service)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=AutoRegCb(action="settings"))
    try:
        await cb.message.edit_text(
            f"🔑 Введите API-ключ для <b>{service_label}</b>:",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    except Exception:
        await cb.message.answer(
            f"🔑 Введите API-ключ для <b>{service_label}</b>:",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    await cb.answer()


@router.message(AutoRegFSM.set_key)
async def msg_autoreg_set_key(msg: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    key = (msg.text or "").strip()
    if len(key) < 10:
        await msg.answer("⚠️ Слишком короткий ключ. Введите полный API-ключ.")
        return
    data = await state.get_data()
    service = data.get("service", "5sim")
    setting_key = _SETTING_5SIM_KEY if service == "5sim" else _SETTING_SMSA_KEY
    await db.set_platform_setting(pool, setting_key, key)
    await state.clear()
    await msg.answer(
        f"✅ API-ключ для <b>{_SERVICES.get(service, service)}</b> сохранён.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardBuilder()
        .button(text="◀️ Назад к настройкам", callback_data=AutoRegCb(action="settings"))
        .as_markup(),
    )


# ── Выбор страны ──────────────────────────────────────────────────────────────


@router.callback_query(AutoRegCb.filter(F.action == "pick_country"))
async def cb_autoreg_pick_country(cb: CallbackQuery, pool: asyncpg.Pool) -> None:
    await cb.answer("⏳ Загружаю список стран…")
    client, service = await _get_sms_client(pool)
    if not client:
        await cb.message.edit_text(
            "❌ API-ключ не настроен. Перейдите в ⚙️ Настройки.",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        return

    try:
        countries = await asyncio.wait_for(client.get_countries(), timeout=15)
    except Exception as exc:
        await cb.message.edit_text(
            f"❌ Ошибка загрузки стран: <code>{html.escape(str(exc)[:150])}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        return

    # Показываем популярные страны + остальные
    POPULAR = ["russia", "0", "7", "ukraine", "usa", "1", "india", "91"]
    popular = [c for c in countries if c["code"].lower() in POPULAR][:6]
    rest = [c for c in countries if c not in popular][:30]

    kb = InlineKeyboardBuilder()
    text_lines = ["<b>🌍 Выберите страну</b>\n\nПопулярные:"]
    for c in popular:
        kb.button(
            text=c["name"],
            callback_data=AutoRegCb(action="start", sub=c["code"]),
        )
    if rest:
        text_lines.append("\nДругие страны:")
        for c in rest[:20]:
            kb.button(
                text=c["name"],
                callback_data=AutoRegCb(action="start", sub=c["code"]),
            )
    kb.button(text="◀️ Назад", callback_data=AutoRegCb(action="menu"))
    kb.adjust(2)

    try:
        await cb.message.edit_text(
            "\n".join(text_lines),
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    except Exception:
        await cb.message.answer(
            "\n".join(text_lines),
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )


# ── Регистрация: заказ номера + ожидание OTP ──────────────────────────────────


@router.callback_query(AutoRegCb.filter(F.action == "start"))
async def cb_autoreg_start(cb: CallbackQuery, callback_data: AutoRegCb, state: FSMContext, pool: asyncpg.Pool) -> None:
    country = callback_data.sub
    if not country:
        await cb.answer("⚠️ Не указана страна", show_alert=True)
        return

    await cb.answer("⏳ Заказываю номер…")
    client, service = await _get_sms_client(pool)
    if not client:
        await cb.message.edit_text(
            "❌ API-ключ не настроен.",
            reply_markup=_back_kb(),
        )
        return

    try:
        order = await asyncio.wait_for(client.buy_number(country), timeout=15)
    except Exception as exc:
        await cb.message.edit_text(
            f"❌ Ошибка заказа номера: <code>{html.escape(str(exc)[:200])}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        return

    phone: str = order["phone"]
    order_id: str = order["id"]
    owner_id = cb.from_user.id

    status_msg = await cb.message.edit_text(
        f"📱 <b>Номер получен:</b> <code>{html.escape(phone)}</code>\n\n"
        f"⏳ Ожидаю SMS-код от Telegram (до 2 минут)…\n\n"
        f"<i>Параллельно отправляю запрос авторизации…</i>",
        parse_mode="HTML",
    )

    # Запускаем авторизацию в фоне
    asyncio.create_task(
        _do_register(
            cb=cb,
            pool=pool,
            phone=phone,
            order_id=order_id,
            country=country,
            sms_client=client,
            owner_id=owner_id,
            status_msg=status_msg,
        )
    )


async def _do_register(
    cb: CallbackQuery,
    pool: asyncpg.Pool,
    phone: str,
    order_id: str,
    country: str,
    sms_client,
    owner_id: int,
    status_msg,
) -> None:
    """Фоновая задача: запрашивает код, ждёт SMS, авторизуется, сохраняет."""
    from services.account_manager import start_login, confirm_code, get_client_info_and_session, cleanup_pending

    kb_cancel = InlineKeyboardBuilder()
    kb_cancel.button(text="❌ Отменить", callback_data=AutoRegCb(action="cancel_order", sub=order_id))

    try:
        # Отправляем запрос кода
        try:
            phone_code_hash, hint = await start_login(phone)
        except Exception as exc:
            await sms_client.cancel_order(order_id)
            await status_msg.edit_text(
                f"❌ Ошибка запроса кода: <code>{html.escape(str(exc)[:200])}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardBuilder()
                .button(text="◀️ Назад", callback_data=AutoRegCb(action="menu"))
                .as_markup(),
            )
            return

        # Ждём SMS
        code = await sms_client.get_sms(order_id, timeout_sec=120)
        if not code:
            await sms_client.cancel_order(order_id)
            await status_msg.edit_text(
                f"❌ SMS-код не получен в течение 2 минут.\n"
                f"Номер: <code>{html.escape(phone)}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardBuilder()
                .button(text="🔄 Попробовать снова", callback_data=AutoRegCb(action="pick_country"))
                .button(text="◀️ Назад", callback_data=AutoRegCb(action="menu"))
                .adjust(1)
                .as_markup(),
            )
            return

        await status_msg.edit_text(
            f"📱 Номер: <code>{html.escape(phone)}</code>\n"
            f"🔑 Код получен: <code>{code}</code>\n\n"
            f"⏳ Авторизуюсь в Telegram…",
            parse_mode="HTML",
        )

        # Авторизуемся
        try:
            result = await confirm_code(phone, code, phone_code_hash)
            if result == "need_2fa":
                await sms_client.cancel_order(order_id)
                await cleanup_pending(phone)
                await status_msg.edit_text(
                    f"🔐 <b>Требуется пароль 2FA</b>\n\n"
                    f"Номер: <code>{html.escape(phone)}</code>\n\n"
                    f"Этот виртуальный номер уже привязан к аккаунту с 2FA.\n"
                    f"Попробуйте другой номер.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardBuilder()
                    .button(text="🔄 Другой номер", callback_data=AutoRegCb(action="pick_country"))
                    .button(text="◀️ Назад", callback_data=AutoRegCb(action="menu"))
                    .adjust(1)
                    .as_markup(),
                )
                return
        except Exception as exc:
            await sms_client.cancel_order(order_id)
            try:
                await cleanup_pending(phone)
            except Exception:
                pass
            await status_msg.edit_text(
                f"❌ Ошибка авторизации: <code>{html.escape(str(exc)[:200])}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardBuilder()
                .button(text="◀️ Назад", callback_data=AutoRegCb(action="menu"))
                .as_markup(),
            )
            return

        # Получаем сессию и информацию
        try:
            session_str, info = await get_client_info_and_session(phone)
        except Exception as exc:
            await sms_client.cancel_order(order_id)
            await status_msg.edit_text(
                f"❌ Ошибка получения сессии: <code>{html.escape(str(exc)[:200])}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardBuilder()
                .button(text="◀️ Назад", callback_data=AutoRegCb(action="menu"))
                .as_markup(),
            )
            return
        finally:
            await cleanup_pending(phone)

        # Сохраняем аккаунт в БД
        acc_id = await pool.fetchval(
            """INSERT INTO tg_accounts
               (owner_id, phone, session_str, tg_user_id, first_name, username,
                device_model, system_version, app_version, lang_code, system_lang_code,
                is_active, trust_score, added_at)
               VALUES ($1,$2,$3,$4,$5,$6,'BotMother','9.0','9.0','en','en-US',TRUE,50,NOW())
               ON CONFLICT (owner_id, phone) DO UPDATE
                 SET session_str=$3, tg_user_id=$4, first_name=$5, username=$6,
                     is_active=TRUE, acc_status=NULL, status_reason=NULL
               RETURNING id""",
            owner_id,
            phone,
            session_str,
            info.get("tg_user_id"),
            info.get("first_name", ""),
            info.get("username", ""),
        )

        first_name = html.escape(info.get("first_name", "") or "")
        username = info.get("username", "")
        username_str = f" (@{html.escape(username)})" if username else ""

        await status_msg.edit_text(
            f"✅ <b>Аккаунт зарегистрирован!</b>\n\n"
            f"📱 Номер: <code>{html.escape(phone)}</code>\n"
            f"👤 Имя: {first_name}{username_str}\n"
            f"🆔 ID: {acc_id}\n\n"
            f"Аккаунт добавлен в «Мои аккаунты».",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder()
            .button(text="🔄 Зарегистрировать ещё", callback_data=AutoRegCb(action="pick_country"))
            .button(text="◀️ Назад", callback_data=AutoRegCb(action="menu"))
            .adjust(1)
            .as_markup(),
        )

    except Exception as exc:
        log.exception("autoreg _do_register fatal: phone=%s", phone)
        try:
            await sms_client.cancel_order(order_id)
        except Exception:
            pass
        try:
            await status_msg.edit_text(
                f"❌ Критическая ошибка: <code>{html.escape(str(exc)[:200])}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardBuilder()
                .button(text="◀️ Назад", callback_data=AutoRegCb(action="menu"))
                .as_markup(),
            )
        except Exception:
            pass


@router.callback_query(AutoRegCb.filter(F.action == "cancel_order"))
async def cb_autoreg_cancel_order(cb: CallbackQuery, callback_data: AutoRegCb, pool: asyncpg.Pool) -> None:
    order_id = callback_data.sub
    if order_id:
        client, _ = await _get_sms_client(pool)
        if client:
            try:
                await client.cancel_order(order_id)
            except Exception:
                pass
    await cb.answer("✅ Заказ отменён")
    try:
        await cb.message.edit_text(
            "❌ Регистрация отменена.",
            reply_markup=InlineKeyboardBuilder()
            .button(text="◀️ Назад", callback_data=AutoRegCb(action="menu"))
            .as_markup(),
        )
    except Exception:
        pass
