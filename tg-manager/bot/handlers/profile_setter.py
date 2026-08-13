"""Сеттер профилей — массовое действие с аккаунтами (полное меню «Выберите действие»).

Один бэкенд: op_worker `bulk_set_profile` → profile_setter_engine.apply_op(op, params)
для каждого выбранного аккаунта. Здесь — UI, раскрывающий ВСЕ операции движка,
сгруппированные как в панели аккаунтов:
  • Проверка: проверить на ограничение
  • Настройка: фото (уст/удал), имя/bio, юзернейм (уст/удал), удалить bio
  • Безопасность: 2FA (уст/удал), код авторизации, закрыть сессии, держать онлайн
  • Приватность: открыть/закрыть инвайт, открыть/скрыть номер
Спинтакс {A|B} — рандомизация значения под каждый аккаунт.
"""
from __future__ import annotations

import html
import json
import logging

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ProfileSetterCb, BmCb
from bot.utils.op_helpers import terminal_kb

log = logging.getLogger(__name__)
router = Router()


class SetterFSM(StatesGroup):
    value = State()     # получаем значение от пользователя (для операций с вводом)
    acc_count = State() # кол-во аккаунтов


# Операции БЕЗ пользовательского ввода — сразу к выбору числа аккаунтов.
_NO_INPUT_OPS = {
    "remove_avatar", "remove_username", "clear_bio",
    "close_sessions", "set_online", "check_restriction", "login_code",
    # Локальные (DB-only, без Telegram) операции без ввода:
    "loc_versions", "loc_gender_m", "loc_gender_f",
}
# Локальные операции (не идут через bulk_set_profile/apply_op — синхронный DB-путь).
_LOCAL_OPS = {"loc_versions", "loc_gender_m", "loc_gender_f", "loc_role_add", "loc_role_del"}
# Человекочитаемые метки операций.
_OP_LABELS = {
    "name": "Имя/Фамилия/Bio", "avatar": "Фото", "username": "Юзернейм",
    "2fa": "2FA пароль", "reset_2fa": "Удалить 2FA", "remove_avatar": "Удалить фото",
    "remove_username": "Удалить юзернейм", "clear_bio": "Удалить bio",
    "close_sessions": "Закрыть сессии", "set_online": "Держать онлайн",
    "check_restriction": "Проверка ограничения", "login_code": "Код авторизации",
    "privacy": "Приватность",
    "loc_versions": "Исправить версии", "loc_gender_m": "Пол: 👨",
    "loc_gender_f": "Пол: 👩", "loc_role_add": "Добавить роль",
    "loc_role_del": "Удалить роль",
}


async def _edit(cb: CallbackQuery, text: str, markup=None):
    try:
        await cb.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=markup, parse_mode="HTML")
    await cb.answer()


def _cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ProfileSetterCb(action="menu"))
    return kb.as_markup()


async def _total_accs(pool: asyncpg.Pool, owner_id: int) -> int:
    row = await pool.fetchrow(
        "SELECT COUNT(*) AS cnt FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
        "AND (cooldown_until IS NULL OR cooldown_until < NOW())",
        owner_id,
    )
    return int(row["cnt"]) if row else 0


# ── Главное меню ─────────────────────────────────────────────────────────────

@router.callback_query(ProfileSetterCb.filter(F.action == "menu"))
async def cb_setter_menu(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await state.clear()
    total = await _total_accs(pool, callback.from_user.id)
    kb = InlineKeyboardBuilder()
    # Настройка аккаунтов
    kb.button(text="📝 Имя/Bio", callback_data=ProfileSetterCb(action="op_name"))
    kb.button(text="🖼 Фото", callback_data=ProfileSetterCb(action="op_avatar"))
    kb.button(text="🆔 Юзернейм", callback_data=ProfileSetterCb(action="op_username"))
    kb.button(text="🗑 Удалить фото", callback_data=ProfileSetterCb(action="op_remove_avatar"))
    kb.button(text="🗑 Удалить юзернейм", callback_data=ProfileSetterCb(action="op_remove_username"))
    kb.button(text="🗑 Удалить bio", callback_data=ProfileSetterCb(action="op_clear_bio"))
    # Безопасность
    kb.button(text="🔑 Установить 2FA", callback_data=ProfileSetterCb(action="op_2fa"))
    kb.button(text="🔓 Удалить 2FA", callback_data=ProfileSetterCb(action="op_reset_2fa"))
    kb.button(text="🔢 Код авторизации", callback_data=ProfileSetterCb(action="op_login_code"))
    kb.button(text="🚪 Закрыть сессии", callback_data=ProfileSetterCb(action="op_close_sessions"))
    kb.button(text="🟢 Держать онлайн", callback_data=ProfileSetterCb(action="op_set_online"))
    # Проверка
    kb.button(text="🛑 Проверка ограничения", callback_data=ProfileSetterCb(action="op_check_restriction"))
    # Приватность
    kb.button(text="📨 Открыть инвайт", callback_data=ProfileSetterCb(action="pv_invite_on"))
    kb.button(text="🔒 Закрыть инвайт", callback_data=ProfileSetterCb(action="pv_invite_off"))
    kb.button(text="📱 Открыть номер", callback_data=ProfileSetterCb(action="pv_phone_on"))
    kb.button(text="🙈 Скрыть номер", callback_data=ProfileSetterCb(action="pv_phone_off"))
    # Локальные (без Telegram): версии / пол / роль
    kb.button(text="🔢 Исправить версии", callback_data=ProfileSetterCb(action="op_loc_versions"))
    kb.button(text="👨 Пол: муж", callback_data=ProfileSetterCb(action="op_loc_gender_m"))
    kb.button(text="👩 Пол: жен", callback_data=ProfileSetterCb(action="op_loc_gender_f"))
    kb.button(text="🏷 Добавить роль", callback_data=ProfileSetterCb(action="op_loc_role_add"))
    kb.button(text="🏷 Удалить роль", callback_data=ProfileSetterCb(action="op_loc_role_del"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="monitoring"))
    kb.adjust(3, 3, 3, 2, 1, 2, 2, 3, 2, 1)
    await _edit(
        callback,
        "🎨 <b>Действие с аккаунтами</b>\n\n"
        "Массовое применение к выбранным аккаунтам. Спинтакс "
        "<code>{A|B}</code> рандомизирует значение под каждый аккаунт.\n\n"
        f"🔑 Доступно аккаунтов: <b>{total}</b>",
        kb.as_markup(),
    )


# ── Операции С вводом значения ───────────────────────────────────────────────

_INPUT_PROMPTS = {
    "name": ("📝 <b>Имя / Фамилия / Bio</b>\n\nВведите (каждое поле с новой строки):\n\n"
             "<code>Имя: {Алекс|Макс|Игорь}\nФамилия: {Петров|Иванов}\n"
             "Bio: {Предприниматель|Бизнес}</code>\n\n"
             "Пустое поле = не менять. Спинтакс {A|B} — рандом на каждый акк."),
    "avatar": ("🖼 <b>Фото профиля</b>\n\nПрямая ссылка на изображение (JPG/PNG):\n\n"
               "<code>https://example.com/photo.jpg</code>"),
    "username": ("🆔 <b>Юзернейм</b>\n\nВведите юзернейм (без @). Спинтакс "
                 "<code>{shop|store}_{01|02}</code> даёт уникальный на каждый акк.\n"
                 "<i>Занятые/некорректные будут отмечены в отчёте.</i>"),
    "2fa": ("🔑 <b>Установить 2FA пароль</b>\n\nВведите (каждое с новой строки):\n\n"
            "<code>НовыйПароль\nТекущийПароль (если уже стоит)\nПодсказка (необяз.)</code>\n\n"
            "<i>Если 2FA ещё нет — строку текущего пароля оставьте пустой.</i>"),
    "reset_2fa": ("🔓 <b>Удалить 2FA пароль</b>\n\nВведите текущий пароль (для снятия).\n"
                  "<i>Если у аккаунтов разные пароли — операция снимет там, где подходит.</i>"),
    "loc_role_add": ("🏷 <b>Добавить роль</b>\n\nВведите название роли (тег) для аккаунтов, "
                     "например <code>прогрев</code> или <code>рабочий</code>."),
    "loc_role_del": ("🏷 <b>Удалить роль</b>\n\nВведите название роли (тег) для снятия."),
}


@router.callback_query(ProfileSetterCb.filter(F.action.startswith("op_")))
async def cb_setter_op(callback: CallbackQuery, callback_data: ProfileSetterCb, state: FSMContext,
                       pool: asyncpg.Pool) -> None:
    op = callback_data.action[3:]  # "op_name" → "name"
    await state.update_data(op=op)
    if op in _NO_INPUT_OPS:
        await _ask_acc_count_cb(callback, state, pool, op)
        return
    prompt = _INPUT_PROMPTS.get(op)
    if not prompt:
        await callback.answer("Неизвестная операция", show_alert=True)
        return
    await state.set_state(SetterFSM.value)
    await _edit(callback, prompt, _cancel_kb())


# ── Операции приватности (без ввода, параметр в действии) ────────────────────

@router.callback_query(ProfileSetterCb.filter(F.action.startswith("pv_")))
async def cb_setter_privacy(callback: CallbackQuery, callback_data: ProfileSetterCb,
                            state: FSMContext, pool: asyncpg.Pool) -> None:
    # pv_invite_on / pv_invite_off / pv_phone_on / pv_phone_off
    _, key, onoff = callback_data.action.split("_")
    allow = onoff == "on"
    await state.update_data(op="privacy", privacy_key=key, privacy_allow=allow)
    _lbl = {("invite", True): "Открыть инвайт", ("invite", False): "Закрыть инвайт",
            ("phone", True): "Открыть номер", ("phone", False): "Скрыть номер"}[(key, allow)]
    await _ask_acc_count_cb(callback, state, pool, "privacy", label=_lbl)


# ── Ввод значения → к числу аккаунтов ────────────────────────────────────────

@router.message(SetterFSM.value, F.text)
async def msg_setter_value(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    op = data.get("op", "")
    text = message.text or ""

    if op == "name":
        parsed = _parse_name_bio(text)
        if not any(parsed.values()):
            await message.answer("⚠️ Не распознано. Формат:\nИмя: Текст\nФамилия: Текст\nBio: Текст",
                                 reply_markup=terminal_kb())
            return
        await state.update_data(name_data=parsed)
    elif op == "avatar":
        url = text.strip()
        if not url.startswith("http"):
            await message.answer("⚠️ Введите прямую ссылку на изображение (https://...)")
            return
        await state.update_data(avatar_url=url)
    elif op == "username":
        uname = text.strip().lstrip("@")
        if not uname:
            await message.answer("⚠️ Введите юзернейм (без @)")
            return
        await state.update_data(username=uname)
    elif op == "2fa":
        parts = [p.strip() for p in text.split("\n") if p.strip()]
        new_pass = parts[0] if parts else ""
        if not new_pass or len(new_pass) < 4:
            await message.answer("⚠️ Пароль должен быть минимум 4 символа")
            return
        await state.update_data(new_password=new_pass,
                                current_password=parts[1] if len(parts) > 1 else "",
                                hint=parts[2] if len(parts) > 2 else "")
    elif op == "reset_2fa":
        await state.update_data(current_password=text.strip())
    elif op in ("loc_role_add", "loc_role_del"):
        role = text.strip()
        if not role or len(role) > 40:
            await message.answer("⚠️ Введите название роли (до 40 символов)")
            return
        await state.update_data(role=role)

    total = await _total_accs(pool, message.from_user.id)
    await state.set_state(SetterFSM.acc_count)
    await message.answer(
        f"✅ Данные приняты.\n\nДоступно аккаунтов: <b>{total}</b>\n"
        "Сколько аккаунтов оформить? (0 = все):",
        parse_mode="HTML", reply_markup=_cancel_kb())


async def _ask_acc_count_cb(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool,
                            op: str, label: str | None = None) -> None:
    """Для операций без ввода: сразу спросить число аккаунтов (из callback)."""
    total = await _total_accs(pool, callback.from_user.id)
    await state.set_state(SetterFSM.acc_count)
    await _edit(
        callback,
        f"🎯 <b>{label or _OP_LABELS.get(op, op)}</b>\n\n"
        f"Доступно аккаунтов: <b>{total}</b>\n"
        "Сколько аккаунтов обработать? (0 = все) — отправьте число:",
        _cancel_kb())


@router.message(SetterFSM.acc_count)
async def msg_setter_acc_count(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    try:
        n = int(message.text or "0")
    except ValueError:
        await message.answer("⚠️ Введите число")
        return
    total = await _total_accs(pool, message.from_user.id)
    use = min(n, total) if n > 0 else total
    if use == 0:
        await message.answer("⚠️ Нет доступных аккаунтов.")
        return
    await state.update_data(acc_count=use)
    data = await state.get_data()
    op = data.get("op", "")
    lbl = _OP_LABELS.get(op, op)
    if op == "privacy":
        lbl = {("invite", True): "Открыть инвайт", ("invite", False): "Закрыть инвайт",
               ("phone", True): "Открыть номер", ("phone", False): "Скрыть номер"}.get(
            (data.get("privacy_key"), data.get("privacy_allow")), "Приватность")
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Применить", callback_data=ProfileSetterCb(action="confirm"))
    kb.button(text="❌ Отмена", callback_data=ProfileSetterCb(action="menu"))
    kb.adjust(2)
    await message.answer(
        f"🎨 <b>Подтверждение</b>\n\nОперация: <b>{lbl}</b>\n🔑 Аккаунтов: <b>{use}</b>",
        parse_mode="HTML", reply_markup=kb.as_markup())


# ── Подтверждение → постановка операции ──────────────────────────────────────

@router.callback_query(ProfileSetterCb.filter(F.action == "confirm"))
async def cb_setter_confirm(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    await state.clear()
    owner_id = callback.from_user.id
    acc_count = data.get("acc_count", 1)
    op = data.get("op", "")

    rows = await pool.fetch(
        "SELECT id FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
        "AND (cooldown_until IS NULL OR cooldown_until < NOW()) "
        "ORDER BY trust_score DESC NULLS LAST LIMIT $2",
        owner_id, acc_count,
    )
    account_ids = [r["id"] for r in rows]
    if not account_ids:
        await callback.answer("⚠️ Нет доступных аккаунтов", show_alert=True)
        return

    # Локальные операции (версии/пол/роль) — синхронный DB-путь, без очереди/Telegram.
    if op in _LOCAL_OPS:
        await _run_local_op(callback, op, owner_id, account_ids, data, pool)
        return

    params: dict = {"op": op, "account_ids": account_ids}
    if op == "name":
        params["name_data"] = data.get("name_data", {})
    elif op == "avatar":
        params["avatar_url"] = data.get("avatar_url", "")
    elif op == "username":
        params["username"] = data.get("username", "")
    elif op == "2fa":
        params["new_password"] = data.get("new_password", "")
        params["current_password"] = data.get("current_password", "")
        params["hint"] = data.get("hint", "")
    elif op == "reset_2fa":
        params["current_password"] = data.get("current_password", "")
    elif op == "privacy":
        params["privacy_key"] = data.get("privacy_key", "phone")
        params["privacy_allow"] = bool(data.get("privacy_allow", False))

    lbl = _OP_LABELS.get(op, op)
    label = f"Действие: {lbl} × {len(account_ids)} акк."
    op_id = await pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
        "VALUES($1,'bulk_set_profile','pending',$2,$3,$4) RETURNING id",
        owner_id, json.dumps(params), len(account_ids), label,
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Детали операции", callback_data=BmCb(action="op_detail", op_id=op_id))
    kb.button(text="◀️ В меню", callback_data=ProfileSetterCb(action="menu"))
    kb.adjust(1)
    await _edit(
        callback,
        f"✅ <b>Операция поставлена в очередь</b>\n\n🆔 #{op_id}\n{html.escape(label)}",
        kb.as_markup())


# ── Локальные операции (версии/пол/роль) — синхронный DB-путь ────────────────

async def _run_local_op(callback: CallbackQuery, op: str, owner_id: int,
                        account_ids: list, data: dict, pool: asyncpg.Pool) -> None:
    from services import account_bulk_attrs as aba
    try:
        if op == "loc_versions":
            n = await aba.regenerate_versions(pool, owner_id, account_ids)
            what = "Перегенерированы версии/устройство"
        elif op in ("loc_gender_m", "loc_gender_f"):
            g = "m" if op.endswith("_m") else "f"
            n = await aba.set_gender(pool, owner_id, account_ids, g)
            what = f"Проставлен пол: {'👨 муж' if g == 'm' else '👩 жен'}"
        elif op == "loc_role_add":
            n = await aba.add_role(pool, owner_id, account_ids, data.get("role", ""))
            what = f"Добавлена роль «{html.escape(data.get('role', ''))}»"
        elif op == "loc_role_del":
            n = await aba.remove_role(pool, owner_id, account_ids, data.get("role", ""))
            what = f"Снята роль «{html.escape(data.get('role', ''))}»"
        else:
            await callback.answer("Неизвестная операция", show_alert=True)
            return
    except ValueError as e:
        await callback.answer(f"⚠️ {e}", show_alert=True)
        return
    except Exception:
        log.exception("local op %s failed", op)
        await callback.answer("Ошибка операции", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ В меню", callback_data=ProfileSetterCb(action="menu"))
    kb.adjust(1)
    await _edit(
        callback,
        f"✅ <b>Готово</b>\n\n{what}\nЗатронуто аккаунтов: <b>{n}</b>",
        kb.as_markup())


# ── Вспомогательные ──────────────────────────────────────────────────────────

def _parse_name_bio(text: str) -> dict:
    result = {"first_name": "", "last_name": "", "about": ""}
    for line in text.splitlines():
        line = line.strip()
        low = line.lower()
        if low.startswith("имя:"):
            result["first_name"] = line[4:].strip()
        elif low.startswith("фамилия:"):
            result["last_name"] = line[8:].strip()
        elif low.startswith("bio:") or low.startswith("о себе:") or low.startswith("about:"):
            result["about"] = line.split(":", 1)[1].strip()
        elif not any(result.values()):
            result["first_name"] = line  # первая строка = имя
    return result
