"""«Модератор чатов» — Chatkeeper-подобный бот-модератор.

Оператор добавляет нашего бота администратором в свой чат, и тот сразу:
  • удаляет системные уведомления (вошёл/вышел/закрепил/сменил фото/название…);
  • приветствует новичков (приветствие само удаляется через N сек);
  • модерирует: /ban /unban /mute /unmute /kick /warn /unwarn /del;
  • антиспам: удаляет ссылки/пересылки от не-админов (опционально);
  • панель /guard — тумблеры настроек прямо в чате (для админов).

Приватная часть (в личке с ботом): список охраняемых чатов + кнопка «добавить
бота в чат» через deeplink ?startgroup c запрошенными правами админа.

Группо-скоупные хендлеры фильтруются по типу чата, поэтому не мешают приватному
UI других модулей; когда модерация не нужна — хендлер поднимает SkipHandler,
и апдейт идёт дальше по роутерам.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time

import asyncpg
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (CallbackQuery, ChatMemberUpdated, ChatPermissions,
                           Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import GuardCb, BmCb
from services import chat_guard as cg
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)
router = Router()

_GROUP = {"group", "supergroup"}
_ADMIN_STATUSES = {"creator", "administrator"}
# Права, которые просим при добавлении бота в чат (deeplink ?startgroup&admin=…).
_WANT_RIGHTS = "delete_messages+restrict_members+ban_users+pin_messages"
_LINK_RE = re.compile(r"(https?://|t\.me/|telegram\.me/|@[A-Za-z]\w{3,})", re.I)


# ── Вспомогательное ──────────────────────────────────────────────────────────

async def _is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Является ли пользователь админом/создателем чата (для команд модерации)."""
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return getattr(m, "status", "") in _ADMIN_STATUSES
    except Exception:
        return False


async def _safe_delete(msg: Message) -> bool:
    try:
        await msg.delete()
        return True
    except (TelegramBadRequest, Exception):
        return False


async def _delete_later(bot: Bot, chat_id: int, message_id: int, ttl: int) -> None:
    """Удалить сообщение через ttl секунд (для авто-исчезающего приветствия)."""
    try:
        await asyncio.sleep(max(1, int(ttl)))
        await bot.delete_message(chat_id, message_id)
    except Exception:
        log_exc_swallow(log, "guard: delete_later")


def _target_from_message(msg: Message, command: CommandObject | None):
    """Достать (user_id, mention) цели команды: из reply или из аргумента @user/id."""
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        name = html.escape(u.full_name or (f"@{u.username}" if u.username else str(u.id)))
        return u.id, name
    arg = (command.args or "").strip() if command else ""
    if arg:
        tok = arg.split()[0]
        if tok.lstrip("-").isdigit():
            return int(tok), html.escape(tok)
        # @username нельзя надёжно резолвить в id без запроса — вернём как есть,
        # Telegram-методы модерации принимают @username не всегда, поэтому просим reply.
    return None, ""


def _parse_minutes(command: CommandObject | None, default: int) -> int:
    """Минуты из аргумента /mute (30 / 30m / 2h / 1d). Пусто → default."""
    if not command or not command.args:
        return default
    tok = command.args.strip().split()[0].lower()
    m = re.match(r"^(\d+)\s*([mhd]?)$", tok)
    if not m:
        return default
    n = int(m.group(1))
    return {"": n, "m": n, "h": n * 60, "d": n * 1440}[m.group(2)]


# ══════════════════════════════════════════════════════════════════════════════
#  1. Авто-активация: бота сделали/сняли админом
# ══════════════════════════════════════════════════════════════════════════════

@router.my_chat_member()
async def on_my_status(event: ChatMemberUpdated, bot: Bot, pool: asyncpg.Pool) -> None:
    """Реакция на изменение статуса САМОГО бота в чате.

    Стал админом → ставим чат под охрану и сразу включаем чистку системных
    сообщений (как Chatkeeper). Разжаловали/удалили → снимаем с охраны.
    """
    chat = event.chat
    if chat.type not in _GROUP and chat.type != "channel":
        return
    new = event.new_chat_member
    status = getattr(new, "status", "")
    promoter = event.from_user

    if status == "administrator":
        rec = await cg.register_chat(
            pool, promoter.id if promoter else 0, chat.id,
            title=chat.title or "", username=chat.username or "")
        can_delete = bool(getattr(new, "can_delete_messages", False))
        can_restrict = bool(getattr(new, "can_restrict_members", False))
        lines = [
            "🛡 <b>Модератор активирован</b>",
            "",
            "Я слежу за порядком в этом чате. Уже работаю:",
            "• 🧹 удаляю системные уведомления (вход/выход/закреп/фото/название);",
            "• 👋 приветствую новичков (если задано приветствие);",
            "• 🔨 команды: /ban /mute /kick /warn /del (в ответ на сообщение).",
            "",
            "⚙️ Настройки: команда <code>/guard</code> (для админов чата).",
        ]
        if not can_delete:
            lines.append("\n⚠️ <b>Нет права «Удаление сообщений»</b> — я не смогу "
                         "чистить уведомления. Выдайте его в настройках админа.")
        if not can_restrict:
            lines.append("⚠️ <b>Нет права «Блокировка пользователей»</b> — бан/мьют "
                         "работать не будут. Выдайте его в настройках админа.")
        try:
            await bot.send_message(chat.id, "\n".join(lines), parse_mode="HTML")
        except Exception:
            log_exc_swallow(log, "guard: activation message")
        log.info("chat_guard: активирован chat=%s owner=%s del=%s restrict=%s",
                 chat.id, rec.get("owner_id"), can_delete, can_restrict)
    elif status in {"left", "kicked", "member", "restricted"}:
        # Убрали из чата или сняли админку → охрана невозможна.
        await cg.deactivate_chat(pool, chat.id)
        old_status = getattr(getattr(event, "old_chat_member", None), "status", "")
        # Только что добавили обычным участником (без админки) → подсказываем.
        if status == "member" and old_status in {"left", "kicked", ""}:
            try:
                await bot.send_message(
                    chat.id,
                    "🛡 Спасибо, что добавили меня! Чтобы я чистил системные "
                    "уведомления и модерировал чат, выдайте мне права "
                    "<b>администратора</b> (удаление сообщений + блокировка "
                    "пользователей). Как только выдадите — я активируюсь сам.",
                    parse_mode="HTML")
            except Exception:
                log_exc_swallow(log, "guard: need-admin hint")
        log.info("chat_guard: деактивирован chat=%s (статус %s)", chat.id, status)


# ══════════════════════════════════════════════════════════════════════════════
#  2. Мгновенная чистка системных сообщений + приветствие новичков
# ══════════════════════════════════════════════════════════════════════════════

@router.message(F.chat.type.in_(_GROUP), F.content_type.in_(cg.ALL_SERVICE_TYPES))
async def on_service_message(message: Message, bot: Bot, pool: asyncpg.Pool) -> None:
    """Системное сообщение в группе — удалить по настройкам + приветствовать."""
    guard = await cg.is_guarded(pool, message.chat.id)
    if not guard:
        return
    settings = guard["settings"]
    ct = message.content_type
    # Приветствие новичков — до удаления «X присоединился».
    if ct in cg.JOIN_TYPES and settings.get("welcome_text"):
        await _greet_newcomers(message, bot, settings)
    if cg.should_delete_service(ct, settings):
        await _safe_delete(message)


async def _greet_newcomers(message: Message, bot: Bot, settings: dict) -> None:
    """Отправить приветствие новичкам с плейсхолдерами {name}/{chat}."""
    tpl = str(settings.get("welcome_text") or "")
    if not tpl:
        return
    for u in (message.new_chat_members or []):
        if getattr(u, "is_bot", False):
            continue
        name = html.escape(u.full_name or (f"@{u.username}" if u.username else "друг"))
        text = tpl.replace("{name}", name).replace(
            "{chat}", html.escape(message.chat.title or "чат"))
        try:
            sent = await bot.send_message(message.chat.id, text, parse_mode="HTML")
        except Exception:
            log_exc_swallow(log, "guard: welcome send")
            continue
        ttl = int(settings.get("welcome_ttl") or 0)
        if ttl > 0:
            asyncio.create_task(_delete_later(bot, message.chat.id, sent.message_id, ttl))


# ══════════════════════════════════════════════════════════════════════════════
#  3. Антиспам обычных сообщений (ссылки/пересылки от не-админов)
# ══════════════════════════════════════════════════════════════════════════════

@router.message(F.chat.type.in_(_GROUP), F.from_user, ~F.from_user.is_bot)
async def on_group_message(message: Message, bot: Bot, pool: asyncpg.Pool) -> None:
    """Антиспам живых сообщений. Если модерации нет/не сработала — SkipHandler,
    чтобы апдейт продолжил идти по другим роутерам."""
    from aiogram.dispatcher.event.bases import SkipHandler
    guard = await cg.is_guarded(pool, message.chat.id)
    if not guard:
        raise SkipHandler
    settings = guard["settings"]
    if not (settings.get("antispam_links") or settings.get("antispam_forward")):
        raise SkipHandler
    uid = message.from_user.id
    # Админов чата не трогаем.
    if await _is_chat_admin(bot, message.chat.id, uid):
        raise SkipHandler

    is_fwd = bool(message.forward_origin or message.forward_from
                  or message.forward_from_chat)
    text = message.text or message.caption or ""
    has_link = bool(_LINK_RE.search(text)) or bool(message.entities and any(
        e.type in ("url", "text_link", "mention") for e in message.entities))

    hit = ((settings.get("antispam_forward") and is_fwd)
           or (settings.get("antispam_links") and has_link))
    if not hit:
        raise SkipHandler
    if await _safe_delete(message):
        log.info("chat_guard: антиспам удалил сообщение chat=%s user=%s (fwd=%s link=%s)",
                 message.chat.id, uid, is_fwd, has_link)
    # сообщение обработано (удалено или попытались) — не пропускаем дальше


# ══════════════════════════════════════════════════════════════════════════════
#  4. Команды модерации (в группе, только для админов чата)
# ══════════════════════════════════════════════════════════════════════════════

async def _guard_precheck(message: Message, bot: Bot, pool: asyncpg.Pool) -> dict | None:
    """Общие проверки команд модерации: чат под охраной + автор — админ."""
    guard = await cg.is_guarded(pool, message.chat.id)
    if not guard:
        return None
    if not await _is_chat_admin(bot, message.chat.id, message.from_user.id):
        try:
            await message.reply("⛔️ Команда только для админов чата.")
        except Exception:
            log_exc_swallow(log, "guard: precheck reply")
        return None
    return guard


@router.message(Command("ban"), F.chat.type.in_(_GROUP))
async def cmd_ban(message: Message, command: CommandObject, bot: Bot,
                  pool: asyncpg.Pool) -> None:
    if not await _guard_precheck(message, bot, pool):
        return
    uid, name = _target_from_message(message, command)
    if not uid:
        await message.reply("⚠️ Ответьте этой командой на сообщение нарушителя "
                            "(или укажите числовой ID).")
        return
    try:
        await bot.ban_chat_member(message.chat.id, uid)
        await cg.reset_warnings(pool, message.chat.id, uid)
        await message.reply(f"🔨 Забанен: {name}")
    except Exception as e:
        await message.reply(f"⚠️ Не удалось забанить: {html.escape(str(e)[:120])}")


@router.message(Command("unban"), F.chat.type.in_(_GROUP))
async def cmd_unban(message: Message, command: CommandObject, bot: Bot,
                    pool: asyncpg.Pool) -> None:
    if not await _guard_precheck(message, bot, pool):
        return
    uid, name = _target_from_message(message, command)
    if not uid:
        await message.reply("⚠️ Ответьте на сообщение или укажите ID.")
        return
    try:
        await bot.unban_chat_member(message.chat.id, uid, only_if_banned=True)
        await message.reply(f"✅ Разбанен: {name}")
    except Exception as e:
        await message.reply(f"⚠️ Не удалось: {html.escape(str(e)[:120])}")


@router.message(Command("kick"), F.chat.type.in_(_GROUP))
async def cmd_kick(message: Message, command: CommandObject, bot: Bot,
                   pool: asyncpg.Pool) -> None:
    if not await _guard_precheck(message, bot, pool):
        return
    uid, name = _target_from_message(message, command)
    if not uid:
        await message.reply("⚠️ Ответьте на сообщение нарушителя.")
        return
    try:
        # Кик = бан + мгновенный разбан (пользователь удалён, но может вернуться).
        await bot.ban_chat_member(message.chat.id, uid)
        await asyncio.sleep(0.5)
        await bot.unban_chat_member(message.chat.id, uid, only_if_banned=True)
        await message.reply(f"👋 Удалён из чата: {name}")
    except Exception as e:
        await message.reply(f"⚠️ Не удалось: {html.escape(str(e)[:120])}")


@router.message(Command("mute"), F.chat.type.in_(_GROUP))
async def cmd_mute(message: Message, command: CommandObject, bot: Bot,
                   pool: asyncpg.Pool) -> None:
    guard = await _guard_precheck(message, bot, pool)
    if not guard:
        return
    uid, name = _target_from_message(message, command)
    if not uid:
        await message.reply("⚠️ Ответьте на сообщение нарушителя. Пример: "
                            "<code>/mute 30</code> (минут) или <code>/mute 2h</code>.")
        return
    minutes = _parse_minutes(command, int(guard["settings"].get("mute_minutes") or 60))
    until = int(time.time()) + minutes * 60
    try:
        await bot.restrict_chat_member(
            message.chat.id, uid,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until)
        await message.reply(f"🔇 {name} в мьюте на {minutes} мин.")
    except Exception as e:
        await message.reply(f"⚠️ Не удалось замьютить: {html.escape(str(e)[:120])}")


@router.message(Command("unmute"), F.chat.type.in_(_GROUP))
async def cmd_unmute(message: Message, command: CommandObject, bot: Bot,
                     pool: asyncpg.Pool) -> None:
    if not await _guard_precheck(message, bot, pool):
        return
    uid, name = _target_from_message(message, command)
    if not uid:
        await message.reply("⚠️ Ответьте на сообщение пользователя.")
        return
    try:
        await bot.restrict_chat_member(
            message.chat.id, uid,
            permissions=ChatPermissions(
                can_send_messages=True, can_send_audios=True, can_send_documents=True,
                can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                can_send_voice_notes=True, can_send_polls=True,
                can_send_other_messages=True, can_add_web_page_previews=True))
        await message.reply(f"🔊 Мьют снят: {name}")
    except Exception as e:
        await message.reply(f"⚠️ Не удалось: {html.escape(str(e)[:120])}")


@router.message(Command("warn"), F.chat.type.in_(_GROUP))
async def cmd_warn(message: Message, command: CommandObject, bot: Bot,
                   pool: asyncpg.Pool) -> None:
    guard = await _guard_precheck(message, bot, pool)
    if not guard:
        return
    uid, name = _target_from_message(message, command)
    if not uid:
        await message.reply("⚠️ Ответьте на сообщение нарушителя.")
        return
    settings = guard["settings"]
    max_warns = int(settings.get("max_warns") or 3)
    warns = await cg.add_warning(pool, message.chat.id, uid)
    if warns < max_warns:
        await message.reply(f"⚠️ Предупреждение {warns}/{max_warns} — {name}")
        return
    # Лимит достигнут → наказание по настройке warn_action.
    action = settings.get("warn_action") or "mute"
    await cg.reset_warnings(pool, message.chat.id, uid)
    try:
        if action == "ban":
            await bot.ban_chat_member(message.chat.id, uid)
            await message.reply(f"🔨 {name}: {max_warns}/{max_warns} предупреждений → бан.")
        elif action == "kick":
            await bot.ban_chat_member(message.chat.id, uid)
            await asyncio.sleep(0.5)
            await bot.unban_chat_member(message.chat.id, uid, only_if_banned=True)
            await message.reply(f"👋 {name}: {max_warns}/{max_warns} → удалён из чата.")
        else:  # mute
            minutes = int(settings.get("mute_minutes") or 60)
            await bot.restrict_chat_member(
                message.chat.id, uid,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=int(time.time()) + minutes * 60)
            await message.reply(f"🔇 {name}: {max_warns}/{max_warns} → мьют {minutes} мин.")
    except Exception as e:
        await message.reply(f"⚠️ Лимит предупреждений достигнут, но наказание не "
                            f"применилось: {html.escape(str(e)[:100])}")


@router.message(Command("unwarn"), F.chat.type.in_(_GROUP))
async def cmd_unwarn(message: Message, command: CommandObject, bot: Bot,
                     pool: asyncpg.Pool) -> None:
    if not await _guard_precheck(message, bot, pool):
        return
    uid, name = _target_from_message(message, command)
    if not uid:
        await message.reply("⚠️ Ответьте на сообщение пользователя.")
        return
    await cg.reset_warnings(pool, message.chat.id, uid)
    await message.reply(f"✅ Предупреждения сняты: {name}")


@router.message(Command("del"), F.chat.type.in_(_GROUP))
async def cmd_del(message: Message, bot: Bot, pool: asyncpg.Pool) -> None:
    if not await _guard_precheck(message, bot, pool):
        return
    if not message.reply_to_message:
        await message.reply("⚠️ Ответьте командой <code>/del</code> на сообщение "
                            "для удаления.", parse_mode="HTML")
        return
    await _safe_delete(message.reply_to_message)
    await _safe_delete(message)


# ══════════════════════════════════════════════════════════════════════════════
#  5. Панель настроек /guard (в группе, для админов)
# ══════════════════════════════════════════════════════════════════════════════

def _panel_kb(chat_id: int, s: dict):
    def _t(on: bool) -> str:
        return "✅" if on else "☑️"
    kb = InlineKeyboardBuilder()
    kb.button(text=f"{_t(s['clean_service'])} Чистка системных",
              callback_data=GuardCb(action="toggle", chat_id=chat_id, key="clean_service"))
    kb.button(text=f"{_t(s['clean_join'])} Скрывать «вошёл»",
              callback_data=GuardCb(action="toggle", chat_id=chat_id, key="clean_join"))
    kb.button(text=f"{_t(s['clean_leave'])} Скрывать «вышел»",
              callback_data=GuardCb(action="toggle", chat_id=chat_id, key="clean_leave"))
    kb.button(text=f"{_t(s['clean_other'])} Скрывать закреп/фото/тему",
              callback_data=GuardCb(action="toggle", chat_id=chat_id, key="clean_other"))
    kb.button(text=f"{_t(s['antispam_links'])} Антиспам: ссылки",
              callback_data=GuardCb(action="toggle", chat_id=chat_id, key="antispam_links"))
    kb.button(text=f"{_t(s['antispam_forward'])} Антиспам: пересылки",
              callback_data=GuardCb(action="toggle", chat_id=chat_id, key="antispam_forward"))
    kb.adjust(1)
    return kb.as_markup()


def _panel_text(s: dict) -> str:
    wa = {"mute": "мьют", "ban": "бан", "kick": "кик"}.get(s.get("warn_action"), "мьют")
    return (
        "🛡 <b>Модератор чатов — настройки</b>\n\n"
        "Тумблеры чистки и антиспама ниже. Порог предупреждений: "
        f"<b>{s.get('max_warns', 3)}</b> → <b>{wa}</b>"
        + (f" {s.get('mute_minutes', 60)} мин" if s.get("warn_action") != "ban" else "")
        + ".\n"
        + ("👋 Приветствие: <b>задано</b>" if s.get("welcome_text") else
           "👋 Приветствие: <i>не задано</i> — команда <code>/welcome текст</code> "
           "(плейсхолдеры {name}, {chat})")
    )


@router.message(Command("guard"), F.chat.type.in_(_GROUP))
async def cmd_guard_panel(message: Message, bot: Bot, pool: asyncpg.Pool) -> None:
    guard = await _guard_precheck(message, bot, pool)
    if not guard:
        return
    await message.reply(_panel_text(guard["settings"]),
                        parse_mode="HTML",
                        reply_markup=_panel_kb(message.chat.id, guard["settings"]))


@router.message(Command("welcome"), F.chat.type.in_(_GROUP))
async def cmd_welcome(message: Message, command: CommandObject, bot: Bot,
                      pool: asyncpg.Pool) -> None:
    guard = await _guard_precheck(message, bot, pool)
    if not guard:
        return
    text = (command.args or "").strip()
    if not text:
        await cg.set_setting(pool, message.chat.id, "welcome_text", None)
        await message.reply("👋 Приветствие отключено.")
        return
    await cg.set_setting(pool, message.chat.id, "welcome_text", text)
    await message.reply(
        "✅ Приветствие сохранено. Плейсхолдеры: <code>{name}</code>, "
        "<code>{chat}</code>. Оно само удалится через "
        f"{guard['settings'].get('welcome_ttl', 60)} сек.", parse_mode="HTML")


@router.callback_query(GuardCb.filter(F.action == "toggle"))
async def cb_toggle(callback: CallbackQuery, callback_data: GuardCb, bot: Bot,
                    pool: asyncpg.Pool) -> None:
    chat_id = callback_data.chat_id
    # Тумблеры может крутить только админ чата.
    if not await _is_chat_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("⛔️ Только для админов чата.", show_alert=True)
        return
    try:
        s = await cg.toggle_setting(pool, chat_id, callback_data.key)
    except ValueError:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    await callback.answer("Готово")
    try:
        await callback.message.edit_text(
            _panel_text(s), parse_mode="HTML", reply_markup=_panel_kb(chat_id, s))
    except Exception:
        log_exc_swallow(log, "guard: panel edit")


# ══════════════════════════════════════════════════════════════════════════════
#  6. Приватное меню: список чатов + кнопка «добавить бота в чат»
# ══════════════════════════════════════════════════════════════════════════════

async def _render_private_menu(callback: CallbackQuery, pool: asyncpg.Pool,
                               bot: Bot) -> None:
    me = await bot.get_me()
    username = me.username or "bot"
    add_url = f"https://t.me/{username}?startgroup=guard&admin={_WANT_RIGHTS}"
    chats = await cg.list_chats(pool, callback.from_user.id)

    lines = [
        "🛡 <b>Модератор чатов</b>\n",
        "Добавьте бота администратором в свой чат — он мгновенно начнёт удалять "
        "системные уведомления (вошёл/вышел/закреп/фото) и работать модератором "
        "(бан/мьют/варны, антиспам, приветствия).\n",
        "Как у Chatkeeper/JoinHide, только внутри вашей инфраструктуры.\n",
    ]
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить бота в чат (админом)", url=add_url)
    if chats:
        lines.append(f"\n<b>Под охраной ({len(chats)}):</b>")
        for c in chats[:20]:
            icon = "🟢" if c["is_active"] else "⚪️"
            title = html.escape(c["title"] or (f"@{c['username']}" if c["username"]
                                               else str(c["chat_id"])))
            lines.append(f"{icon} {title}")
            kb.button(text=f"{icon} {title[:28]}",
                      callback_data=GuardCb(action="view", chat_id=c["chat_id"]))
    else:
        lines.append("\n<i>Пока нет ни одного чата под охраной.</i>")
    kb.button(text="ℹ️ Как это работает", callback_data=GuardCb(action="how"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="monitoring"))
    kb.adjust(1)
    try:
        await callback.message.edit_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=kb.as_markup(), disable_web_page_preview=True)
    except Exception:
        log_exc_swallow(log, "guard: private menu")


@router.callback_query(GuardCb.filter(F.action == "menu"))
async def cb_menu(callback: CallbackQuery, bot: Bot, pool: asyncpg.Pool,
                  state: FSMContext) -> None:
    try:
        await callback.answer()
    except Exception:
        log_exc_swallow(log, "guard: answer")
    await state.clear()
    await _render_private_menu(callback, pool, bot)


@router.callback_query(GuardCb.filter(F.action == "how"))
async def cb_how(callback: CallbackQuery, bot: Bot, pool: asyncpg.Pool) -> None:
    try:
        await callback.answer()
    except Exception:
        log_exc_swallow(log, "guard: answer")
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=GuardCb(action="menu"))
    await callback.message.edit_text(
        "ℹ️ <b>Как работает Модератор чатов</b>\n\n"
        "1️⃣ Нажмите «Добавить бота в чат» и выберите свой чат — Telegram сразу "
        "предложит выдать боту права админа (удаление сообщений, блокировка).\n"
        "2️⃣ Как только бот получит админку — он пришлёт подтверждение и начнёт "
        "чистить системные уведомления автоматически.\n"
        "3️⃣ Настройки — команда <code>/guard</code> прямо в чате (для админов).\n\n"
        "<b>Команды модерации</b> (в ответ на сообщение):\n"
        "• <code>/ban</code> · <code>/unban</code> — бан/разбан\n"
        "• <code>/mute 30</code> · <code>/unmute</code> — мьют на N мин / снять\n"
        "• <code>/kick</code> — удалить из чата\n"
        "• <code>/warn</code> · <code>/unwarn</code> — предупреждения\n"
        "• <code>/del</code> — удалить сообщение\n"
        "• <code>/welcome текст</code> — приветствие ({name}, {chat})",
        parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(GuardCb.filter(F.action == "view"))
async def cb_view(callback: CallbackQuery, callback_data: GuardCb, bot: Bot,
                  pool: asyncpg.Pool) -> None:
    try:
        await callback.answer()
    except Exception:
        log_exc_swallow(log, "guard: answer")
    c = await cg.get_chat(pool, callback_data.chat_id)
    if not c or c["owner_id"] != callback.from_user.id:
        await callback.answer("Чат не найден", show_alert=True)
        return
    s = c["settings"]
    title = html.escape(c["title"] or str(c["chat_id"]))
    status = "🟢 активна" if c["is_active"] else "⚪️ бот не в чате / без прав"
    on = lambda k: "вкл" if s.get(k) else "выкл"  # noqa: E731
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=GuardCb(action="menu"))
    await callback.message.edit_text(
        f"🛡 <b>{title}</b>\n\n"
        f"Охрана: {status}\n"
        f"🧹 Чистка системных: {on('clean_service')}\n"
        f"   • вошёл: {on('clean_join')} · вышел: {on('clean_leave')} · "
        f"прочее: {on('clean_other')}\n"
        f"🔗 Антиспам ссылок: {on('antispam_links')} · пересылок: {on('antispam_forward')}\n"
        f"👋 Приветствие: {'задано' if s.get('welcome_text') else 'нет'}\n\n"
        "Менять настройки удобнее командой <code>/guard</code> внутри самого чата.",
        parse_mode="HTML", reply_markup=kb.as_markup())
