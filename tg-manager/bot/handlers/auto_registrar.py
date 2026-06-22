"""Авторег — автоматическая регистрация аккаунтов Telegram через SMS-сервисы.

Поддерживает: 5sim.net, sms-activate.org
Режимы: одиночный (с поддержкой 2FA через FSM) и батч (N аккаунтов, 2FA пропускается).

Процесс одиночного:
  выбор страны → заказ номера → отправка кода → ожидание SMS (фон)
  → confirm_code → если 2FA: FSM запрашивает пароль → сохранение в tg_accounts

Процесс батч:
  N × (заказ номера → SMS → регистрация)  — 2FA-номера пропускаются автоматически
"""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

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

# ── Константы ─────────────────────────────────────────────────────────────────

_SERVICES = {
    "5sim": "5sim.net",
    "smsactivate": "sms-activate.org",
}
_SETTING_SERVICE  = "sms_api_service"
_SETTING_5SIM_KEY = "sms_api_5sim_key"
_SETTING_SMSA_KEY = "sms_api_smsa_key"

_SMS_WAIT_SEC = 120       # максимум ожидания SMS
_INTER_REG_DELAY = 3.0    # пауза между регистрациями в батч-режиме

# ── In-memory: ожидание 2FA  ──────────────────────────────────────────────────
# user_id → {phone, order_id, sms_client, batch_state}
_pending_2fa: dict[int, dict[str, Any]] = {}


# ── FSM ───────────────────────────────────────────────────────────────────────

class AutoRegFSM(StatesGroup):
    set_key   = State()   # ввод API-ключа
    enter_2fa = State()   # ввод пароля 2FA после авторег
    batch_cnt = State()   # ввод количества аккаунтов для батча


# ── Вспомогательные функции ───────────────────────────────────────────────────


async def _get_sms_client(pool: asyncpg.Pool):
    """Возвращает (client, service_key) или (None, service_key) если ключ не задан."""
    from services.sms_api_engine import get_sms_client
    service = await db.get_platform_setting(pool, _SETTING_SERVICE, "5sim")
    key_setting = _SETTING_5SIM_KEY if service == "5sim" else _SETTING_SMSA_KEY
    key = await db.get_platform_setting(pool, key_setting, "")
    if not key:
        return None, service
    return get_sms_client(service, key), service


async def _save_account(
    pool: asyncpg.Pool,
    owner_id: int,
    phone: str,
    session_str: str,
    info: dict,
) -> int:
    """Сохраняет/обновляет аккаунт в tg_accounts, возвращает id."""
    return await pool.fetchval(
        """INSERT INTO tg_accounts
           (owner_id, phone, session_str, tg_user_id, first_name, username,
            device_model, system_version, app_version, lang_code, system_lang_code,
            is_active, trust_score, acc_status, added_at)
           VALUES ($1,$2,$3,$4,$5,$6,'BotMother','9.0','9.0','en','en-US',TRUE,1.0,'active',NOW())
           ON CONFLICT (owner_id, phone) DO UPDATE
             SET session_str=$3, tg_user_id=$4, first_name=$5, username=$6,
                 is_active=TRUE, acc_status='active', status_reason=NULL,
                 status_checked_at=now(), last_used=now()
           RETURNING id""",
        owner_id,
        phone,
        session_str,
        info.get("tg_user_id"),
        info.get("first_name", ""),
        info.get("username", ""),
    )


def _menu_kb() -> object:
    return (
        InlineKeyboardBuilder()
        .button(text="◀️ Назад", callback_data=AutoRegCb(action="menu"))
        .as_markup()
    )


def _format_acc(info: dict, phone: str) -> str:
    name = html.escape(info.get("first_name") or "")
    uname = info.get("username", "")
    uname_str = f" (@{html.escape(uname)})" if uname else ""
    return f"📱 <code>{html.escape(phone)}</code> — {name}{uname_str}"


# ── Главное меню ──────────────────────────────────────────────────────────────


@router.callback_query(AutoRegCb.filter(F.action == "menu"))
async def cb_autoreg_menu(cb: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await state.clear()
    _pending_2fa.pop(cb.from_user.id, None)

    service = await db.get_platform_setting(pool, _SETTING_SERVICE, "5sim")
    service_label = _SERVICES.get(service, service)
    key_setting = _SETTING_5SIM_KEY if service == "5sim" else _SETTING_SMSA_KEY
    key = await db.get_platform_setting(pool, key_setting, "")

    balance_str = ""
    if key:
        try:
            from services.sms_api_engine import get_sms_client
            client = get_sms_client(service, key)
            bal = await asyncio.wait_for(client.get_balance(), timeout=8)
            balance_str = f" · баланс <b>${bal:.2f}</b>"
        except Exception:
            balance_str = " · <i>баланс недоступен</i>"

    key_icon = "✅" if key else "❌"
    text = (
        "<b>🤖 Авторег</b>\n\n"
        "Регистрация аккаунтов Telegram через виртуальные номера.\n\n"
        f"<b>Сервис:</b> {service_label}\n"
        f"<b>API-ключ:</b> {key_icon}{balance_str}\n\n"
        "Новые аккаунты сохраняются в «Мои аккаунты»."
    )
    kb = InlineKeyboardBuilder()
    if key:
        kb.button(text="➕ Зарегистрировать 1 аккаунт", callback_data=AutoRegCb(action="pick_country", sub="single"))
        kb.button(text="📦 Батч (несколько аккаунтов)", callback_data=AutoRegCb(action="batch_ask"))
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
    await cb.answer()
    service = await db.get_platform_setting(pool, _SETTING_SERVICE, "5sim")
    text = (
        "<b>⚙️ Настройки SMS API</b>\n\n"
        f"Сервис: <b>{_SERVICES.get(service, service)}</b>\n\n"
        "Выберите сервис или введите API-ключ:"
    )
    kb = InlineKeyboardBuilder()
    for svc_k, svc_n in _SERVICES.items():
        mark = "✅ " if service == svc_k else ""
        kb.button(text=f"{mark}{svc_n}", callback_data=AutoRegCb(action="set_service", sub=svc_k))
    kb.button(text="🔑 Ввести API-ключ", callback_data=AutoRegCb(action="set_key"))
    kb.button(text="◀️ Назад", callback_data=AutoRegCb(action="menu"))
    kb.adjust(1)
    try:
        await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(AutoRegCb.filter(F.action == "set_service"))
async def cb_autoreg_set_service(cb: CallbackQuery, callback_data: AutoRegCb, pool: asyncpg.Pool) -> None:
    svc = callback_data.sub
    if svc not in _SERVICES:
        await cb.answer("Неизвестный сервис", show_alert=True)
        return
    await db.set_platform_setting(pool, _SETTING_SERVICE, svc)
    await cb.answer(f"✅ {_SERVICES[svc]}")
    await cb_autoreg_settings(cb, pool)


@router.callback_query(AutoRegCb.filter(F.action == "set_key"))
async def cb_autoreg_set_key(cb: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    service = await db.get_platform_setting(pool, _SETTING_SERVICE, "5sim")
    await state.set_state(AutoRegFSM.set_key)
    await state.update_data(service=service)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=AutoRegCb(action="settings"))
    try:
        await cb.message.edit_text(
            f"🔑 Введите API-ключ для <b>{_SERVICES.get(service, service)}</b>:",
            reply_markup=kb.as_markup(), parse_mode="HTML",
        )
    except Exception:
        await cb.message.answer(
            f"🔑 Введите API-ключ для <b>{_SERVICES.get(service, service)}</b>:",
            reply_markup=kb.as_markup(), parse_mode="HTML",
        )
    await cb.answer()


@router.message(AutoRegFSM.set_key)
async def msg_autoreg_set_key(msg: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    key = (msg.text or "").strip()
    if len(key) < 8:
        await msg.answer("⚠️ Слишком короткий ключ. Введите полный API-ключ.")
        return
    data = await state.get_data()
    service = data.get("service", "5sim")
    setting_key = _SETTING_5SIM_KEY if service == "5sim" else _SETTING_SMSA_KEY
    await db.set_platform_setting(pool, setting_key, key)
    await state.clear()
    await msg.answer(
        f"✅ API-ключ <b>{_SERVICES.get(service, service)}</b> сохранён.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardBuilder()
        .button(text="◀️ К настройкам", callback_data=AutoRegCb(action="settings"))
        .as_markup(),
    )


# ── Выбор страны ──────────────────────────────────────────────────────────────


@router.callback_query(AutoRegCb.filter(F.action == "pick_country"))
async def cb_autoreg_pick_country(cb: CallbackQuery, callback_data: AutoRegCb, pool: asyncpg.Pool) -> None:
    mode = callback_data.sub or "single"  # "single" | "batch"
    await cb.answer("⏳ Загружаю список стран…")

    client, _ = await _get_sms_client(pool)
    if not client:
        await cb.message.edit_text("❌ API-ключ не настроен.", reply_markup=_menu_kb())
        return

    try:
        countries = await asyncio.wait_for(client.get_countries(), timeout=15)
    except Exception as exc:
        await cb.message.edit_text(
            f"❌ Ошибка загрузки стран: <code>{html.escape(str(exc)[:150])}</code>",
            parse_mode="HTML", reply_markup=_menu_kb(),
        )
        return

    # Известные коды популярных стран (5sim и sms-activate кодируют по-разному)
    _POPULAR = {"russia", "0", "ukraine", "4", "usa", "1", "india", "22",
                "indonesia", "6", "brazil", "73", "philippines", "55"}
    popular = [c for c in countries if c["code"].lower() in _POPULAR or
               c["name"].lower() in {"russia", "ukraine", "usa", "india"}][:8]
    # Дедупликация
    seen = {c["code"] for c in popular}
    rest = [c for c in countries if c["code"] not in seen][:24]

    start_action = "start"
    kb = InlineKeyboardBuilder()
    for c in popular:
        kb.button(text=c["name"], callback_data=AutoRegCb(action=start_action, sub=f"{mode}:{c['code']}"))
    for c in rest:
        kb.button(text=c["name"], callback_data=AutoRegCb(action=start_action, sub=f"{mode}:{c['code']}"))
    kb.button(text="◀️ Назад", callback_data=AutoRegCb(action="menu"))
    kb.adjust(2)

    try:
        await cb.message.edit_text(
            "<b>🌍 Выберите страну</b>",
            reply_markup=kb.as_markup(), parse_mode="HTML",
        )
    except Exception:
        await cb.message.answer(
            "<b>🌍 Выберите страну</b>",
            reply_markup=kb.as_markup(), parse_mode="HTML",
        )


# ── Батч-режим: ввод количества ──────────────────────────────────────────────


@router.callback_query(AutoRegCb.filter(F.action == "batch_ask"))
async def cb_autoreg_batch_ask(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AutoRegFSM.batch_cnt)
    kb = InlineKeyboardBuilder()
    for n in [3, 5, 10, 20]:
        kb.button(text=str(n), callback_data=AutoRegCb(action="batch_country", sub=str(n)))
    kb.button(text="❌ Отмена", callback_data=AutoRegCb(action="menu"))
    kb.adjust(4, 1)
    try:
        await cb.message.edit_text(
            "<b>📦 Батч-регистрация</b>\n\nСколько аккаунтов зарегистрировать?\n"
            "2FA-номера пропускаются автоматически.",
            reply_markup=kb.as_markup(), parse_mode="HTML",
        )
    except Exception:
        await cb.message.answer(
            "<b>📦 Батч-регистрация</b>\n\nСколько аккаунтов?",
            reply_markup=kb.as_markup(), parse_mode="HTML",
        )
    await cb.answer()


@router.message(AutoRegFSM.batch_cnt)
async def msg_autoreg_batch_cnt(msg: Message, state: FSMContext) -> None:
    raw = (msg.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 50):
        await msg.answer("⚠️ Введите число от 1 до 50.")
        return
    await state.clear()
    cnt = int(raw)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Выбрать страну", callback_data=AutoRegCb(action="batch_country", sub=str(cnt)))
    kb.button(text="❌ Отмена", callback_data=AutoRegCb(action="menu"))
    kb.adjust(1)
    await msg.answer(
        f"📦 Батч: <b>{cnt}</b> аккаунтов. Выберите страну:",
        reply_markup=kb.as_markup(), parse_mode="HTML",
    )


@router.callback_query(AutoRegCb.filter(F.action == "batch_country"))
async def cb_autoreg_batch_country(cb: CallbackQuery, callback_data: AutoRegCb, state: FSMContext, pool: asyncpg.Pool) -> None:
    sub = callback_data.sub or "5"
    cnt = int(sub) if sub.isdigit() else 5
    await state.update_data(batch_cnt=cnt)
    await cb.answer()
    client, _ = await _get_sms_client(pool)
    if not client:
        await cb.message.edit_text("❌ API-ключ не настроен.", reply_markup=_menu_kb())
        return
    try:
        countries = await asyncio.wait_for(client.get_countries(), timeout=15)
    except Exception as exc:
        await cb.message.edit_text(
            f"❌ Ошибка: <code>{html.escape(str(exc)[:150])}</code>",
            parse_mode="HTML", reply_markup=_menu_kb(),
        )
        return

    _POPULAR = {"russia", "0", "ukraine", "4", "usa", "1", "india", "22"}
    popular = [c for c in countries if c["code"].lower() in _POPULAR or
               c["name"].lower() in {"russia", "ukraine", "usa", "india"}][:8]
    seen = {c["code"] for c in popular}
    rest = [c for c in countries if c["code"] not in seen][:24]

    kb = InlineKeyboardBuilder()
    for c in popular + rest:
        kb.button(text=c["name"], callback_data=AutoRegCb(action="start", sub=f"batch:{c['code']}"))
    kb.button(text="◀️ Назад", callback_data=AutoRegCb(action="menu"))
    kb.adjust(2)
    try:
        await cb.message.edit_text(
            f"<b>🌍 Страна для батч-регистрации</b> ({cnt} аккаунтов):",
            reply_markup=kb.as_markup(), parse_mode="HTML",
        )
    except Exception:
        await cb.message.answer(
            f"<b>🌍 Страна</b> ({cnt} аккаунтов):",
            reply_markup=kb.as_markup(), parse_mode="HTML",
        )


# ── Точка входа: одиночный или батч ──────────────────────────────────────────


@router.callback_query(AutoRegCb.filter(F.action == "start"))
async def cb_autoreg_start(cb: CallbackQuery, callback_data: AutoRegCb, state: FSMContext, pool: asyncpg.Pool) -> None:
    sub = callback_data.sub or "single:russia"
    parts = sub.split(":", 1)
    mode = parts[0]           # "single" | "batch"
    country = parts[1] if len(parts) > 1 else "russia"

    client, _ = await _get_sms_client(pool)
    if not client:
        await cb.answer("❌ API-ключ не настроен", show_alert=True)
        return

    if mode == "batch":
        data = await state.get_data()
        cnt = int(data.get("batch_cnt", 5))
        await state.clear()
        await cb.answer(f"⏳ Запускаю батч {cnt} аккаунтов…")
        status_msg = await cb.message.edit_text(
            f"📦 <b>Батч-регистрация</b> · {cnt} аккаунтов · {country}\n\n⏳ Запускаю…",
            parse_mode="HTML",
        )
        asyncio.create_task(_do_batch_register(
            pool=pool,
            owner_id=cb.from_user.id,
            country=country,
            cnt=cnt,
            sms_client=client,
            status_msg=status_msg,
        ))
    else:
        await cb.answer("⏳ Заказываю номер…")
        await _start_single_register(cb, pool, country, client, state)


# ── Одиночная регистрация ─────────────────────────────────────────────────────


async def _start_single_register(
    cb: CallbackQuery,
    pool: asyncpg.Pool,
    country: str,
    sms_client,
    state: FSMContext,
) -> None:
    owner_id = cb.from_user.id

    # 1. Заказ номера
    try:
        order = await asyncio.wait_for(sms_client.buy_number(country), timeout=15)
    except Exception as exc:
        await cb.message.edit_text(
            f"❌ Ошибка заказа номера: <code>{html.escape(str(exc)[:200])}</code>",
            parse_mode="HTML", reply_markup=_menu_kb(),
        )
        return

    phone: str = order["phone"]
    order_id: str = order["id"]

    # 2. Отправка кода в Telegram
    try:
        from services.account_manager import start_login
        phone_code_hash, hint = await asyncio.wait_for(start_login(phone), timeout=20)
    except Exception as exc:
        await sms_client.cancel_order(order_id)
        await cb.message.edit_text(
            f"❌ Ошибка запроса кода: <code>{html.escape(str(exc)[:200])}</code>",
            parse_mode="HTML", reply_markup=_menu_kb(),
        )
        return

    status_msg = await cb.message.edit_text(
        f"📱 <b>Номер:</b> <code>{html.escape(phone)}</code>\n"
        f"⏳ Ожидаю SMS-код от Telegram…\n\n"
        f"<i>Это может занять до {_SMS_WAIT_SEC // 60} мин.</i>",
        parse_mode="HTML",
    )

    # 3. Фоновое ожидание SMS + подтверждение
    asyncio.create_task(_wait_and_confirm(
        pool=pool,
        owner_id=owner_id,
        phone=phone,
        order_id=order_id,
        phone_code_hash=phone_code_hash,
        sms_client=sms_client,
        status_msg=status_msg,
        state=state,
    ))


async def _wait_and_confirm(
    pool: asyncpg.Pool,
    owner_id: int,
    phone: str,
    order_id: str,
    phone_code_hash: str,
    sms_client,
    status_msg,
    state: FSMContext,
) -> None:
    """Ждёт SMS, подтверждает код. При 2FA — переводит FSM в enter_2fa."""
    from services.account_manager import confirm_code, get_client_info_and_session, cleanup_pending

    try:
        code = await sms_client.get_sms(order_id, timeout_sec=_SMS_WAIT_SEC)
    except Exception as exc:
        log.warning("autoreg: SMS polling error for %s: %s", phone, exc)
        code = None

    if not code:
        await sms_client.cancel_order(order_id)
        await cleanup_pending(phone)
        try:
            await status_msg.edit_text(
                f"❌ <b>SMS не получен</b>\n\n"
                f"Номер: <code>{html.escape(phone)}</code>\n"
                f"Код не пришёл в течение {_SMS_WAIT_SEC} сек.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardBuilder()
                .button(text="🔄 Попробовать снова", callback_data=AutoRegCb(action="pick_country", sub="single"))
                .button(text="◀️ Назад", callback_data=AutoRegCb(action="menu"))
                .adjust(1).as_markup(),
            )
        except Exception:
            pass
        return

    # Подтверждение кода
    try:
        result = await confirm_code(phone, code, phone_code_hash)
    except Exception as exc:
        await sms_client.cancel_order(order_id)
        await cleanup_pending(phone)
        try:
            await status_msg.edit_text(
                f"❌ Неверный код: <code>{html.escape(str(exc)[:200])}</code>",
                parse_mode="HTML", reply_markup=_menu_kb(),
            )
        except Exception:
            pass
        return

    if result == "need_2fa":
        # Сохраняем состояние для FSM-обработки 2FA
        _pending_2fa[owner_id] = {
            "phone": phone,
            "order_id": order_id,
            "sms_client": sms_client,
        }
        await state.set_state(AutoRegFSM.enter_2fa)
        await state.update_data(phone=phone)
        try:
            await status_msg.edit_text(
                f"🔐 <b>Требуется пароль 2FA</b>\n\n"
                f"Номер: <code>{html.escape(phone)}</code>\n\n"
                f"Этот аккаунт защищён двухэтапной верификацией.\n"
                f"Введите пароль 2FA:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardBuilder()
                .button(text="❌ Отмена", callback_data=AutoRegCb(action="cancel_2fa"))
                .as_markup(),
            )
        except Exception:
            pass
        return

    # Получаем и сохраняем сессию
    try:
        session_str, info = await get_client_info_and_session(phone)
    except Exception as exc:
        await cleanup_pending(phone)
        try:
            await status_msg.edit_text(
                f"❌ Ошибка сессии: <code>{html.escape(str(exc)[:200])}</code>",
                parse_mode="HTML", reply_markup=_menu_kb(),
            )
        except Exception:
            pass
        return
    finally:
        await cleanup_pending(phone)

    acc_id = await _save_account(pool, owner_id, phone, session_str, info)
    try:
        await status_msg.edit_text(
            f"✅ <b>Аккаунт зарегистрирован!</b>\n\n"
            f"{_format_acc(info, phone)}\n"
            f"🆔 DB id: {acc_id}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder()
            .button(text="➕ Ещё один", callback_data=AutoRegCb(action="pick_country", sub="single"))
            .button(text="◀️ Главное", callback_data=AutoRegCb(action="menu"))
            .adjust(1).as_markup(),
        )
    except Exception:
        pass


# ── FSM: ввод 2FA пароля ──────────────────────────────────────────────────────


@router.message(AutoRegFSM.enter_2fa)
async def msg_autoreg_enter_2fa(msg: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    from services.account_manager import confirm_2fa, get_client_info_and_session, cleanup_pending

    owner_id = msg.from_user.id
    pending = _pending_2fa.pop(owner_id, None)
    if not pending:
        await state.clear()
        await msg.answer("⚠️ Сессия 2FA истекла. Начните заново.", reply_markup=_menu_kb())
        return

    phone: str = pending["phone"]
    order_id: str = pending["order_id"]
    sms_client = pending["sms_client"]
    password = (msg.text or "").strip()

    if not password:
        # Возвращаем pending обратно — дадим ещё попытку
        _pending_2fa[owner_id] = pending
        await msg.answer("⚠️ Введите пароль 2FA (он не может быть пустым).")
        return

    await msg.answer("⏳ Проверяю пароль 2FA…")

    try:
        await confirm_2fa(phone, password)
    except ValueError as exc:
        _pending_2fa[owner_id] = pending   # вернуть для повторной попытки
        await msg.answer(
            f"❌ {html.escape(str(exc))}\n\nВведите пароль ещё раз:",
        )
        return
    except Exception as exc:
        await cleanup_pending(phone)
        await sms_client.cancel_order(order_id)
        await state.clear()
        await msg.answer(
            f"❌ Ошибка 2FA: <code>{html.escape(str(exc)[:200])}</code>",
            parse_mode="HTML", reply_markup=_menu_kb(),
        )
        return

    try:
        session_str, info = await get_client_info_and_session(phone)
    except Exception as exc:
        await cleanup_pending(phone)
        await state.clear()
        await msg.answer(
            f"❌ Ошибка сессии: <code>{html.escape(str(exc)[:200])}</code>",
            parse_mode="HTML", reply_markup=_menu_kb(),
        )
        return
    finally:
        await cleanup_pending(phone)

    await state.clear()
    acc_id = await _save_account(pool, owner_id, phone, session_str, info)
    await msg.answer(
        f"✅ <b>Аккаунт с 2FA зарегистрирован!</b>\n\n"
        f"{_format_acc(info, phone)}\n"
        f"🆔 DB id: {acc_id}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardBuilder()
        .button(text="➕ Ещё один", callback_data=AutoRegCb(action="pick_country", sub="single"))
        .button(text="◀️ Главное", callback_data=AutoRegCb(action="menu"))
        .adjust(1).as_markup(),
    )


@router.callback_query(AutoRegCb.filter(F.action == "cancel_2fa"))
async def cb_autoreg_cancel_2fa(cb: CallbackQuery, state: FSMContext) -> None:
    owner_id = cb.from_user.id
    pending = _pending_2fa.pop(owner_id, None)
    if pending:
        phone = pending.get("phone", "")
        order_id = pending.get("order_id", "")
        sms_client = pending.get("sms_client")
        if sms_client and order_id:
            try:
                await sms_client.cancel_order(order_id)
            except Exception:
                pass
        if phone:
            from services.account_manager import cleanup_pending
            try:
                await cleanup_pending(phone)
            except Exception:
                pass
    await state.clear()
    await cb.answer("Отменено")
    try:
        await cb.message.edit_text(
            "❌ Регистрация отменена.",
            reply_markup=_menu_kb(),
        )
    except Exception:
        pass


# ── Батч-регистрация ──────────────────────────────────────────────────────────


async def _do_batch_register(
    pool: asyncpg.Pool,
    owner_id: int,
    country: str,
    cnt: int,
    sms_client,
    status_msg,
) -> None:
    """Регистрирует cnt аккаунтов последовательно. 2FA-номера пропускаются."""
    from services.account_manager import start_login, confirm_code, get_client_info_and_session, cleanup_pending

    ok_accs: list[str] = []
    failed: list[str] = []

    for i in range(1, cnt + 1):
        progress = (
            f"📦 <b>Батч-регистрация</b> · {country}\n\n"
            f"⏳ Аккаунт <b>{i}/{cnt}</b>…\n"
            + (f"✅ Готово: {len(ok_accs)}\n" if ok_accs else "")
            + (f"⚠️ Пропущено: {len(failed)}\n" if failed else "")
        )
        try:
            await status_msg.edit_text(progress, parse_mode="HTML")
        except Exception:
            pass

        phone = ""
        order_id = ""
        try:
            # Заказываем номер
            order = await asyncio.wait_for(sms_client.buy_number(country), timeout=20)
            phone = order["phone"]
            order_id = order["id"]

            # Запрашиваем код в TG
            phone_code_hash, _ = await asyncio.wait_for(start_login(phone), timeout=20)

            # Ждём SMS
            code = await sms_client.get_sms(order_id, timeout_sec=_SMS_WAIT_SEC)
            if not code:
                await sms_client.cancel_order(order_id)
                await cleanup_pending(phone)
                failed.append(f"{phone} — SMS не получен")
                continue

            # Подтверждаем
            result = await confirm_code(phone, code, phone_code_hash)
            if result == "need_2fa":
                await cleanup_pending(phone)
                await sms_client.cancel_order(order_id)
                failed.append(f"{phone} — 2FA (пропущен)")
                continue

            # Сохраняем
            session_str, info = await get_client_info_and_session(phone)
            await cleanup_pending(phone)
            acc_id = await _save_account(pool, owner_id, phone, session_str, info)
            ok_accs.append(f"{phone} → id{acc_id}")

        except Exception as exc:
            log.warning("autoreg batch i=%d phone=%s: %s", i, phone, exc)
            if order_id:
                try:
                    await sms_client.cancel_order(order_id)
                except Exception:
                    pass
            if phone:
                try:
                    from services.account_manager import cleanup_pending as _cp
                    await _cp(phone)
                except Exception:
                    pass
            failed.append(f"{phone or '?'} — {str(exc)[:60]}")

        if i < cnt:
            await asyncio.sleep(_INTER_REG_DELAY)

    # Итоговый отчёт
    ok_lines = "\n".join(f"✅ {a}" for a in ok_accs) or "—"
    fail_lines = "\n".join(f"⚠️ {f}" for f in failed) or "—"
    report = (
        f"📦 <b>Батч-регистрация завершена</b>\n\n"
        f"<b>Зарегистрировано: {len(ok_accs)}/{cnt}</b>\n\n"
        f"{ok_lines}\n\n"
        + (f"<b>Пропущено:</b>\n{fail_lines}" if failed else "")
    )
    try:
        await status_msg.edit_text(
            report,
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder()
            .button(text="📦 Ещё батч", callback_data=AutoRegCb(action="batch_ask"))
            .button(text="◀️ Главное", callback_data=AutoRegCb(action="menu"))
            .adjust(1).as_markup(),
        )
    except Exception:
        pass
