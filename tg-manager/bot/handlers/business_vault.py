"""«Хранилище» (Echo Vault): приём Telegram Business-апдейтов и зеркалирование
переписки в архив.

Подключение бота в настройках бизнес-аккаунта присылает business_connection;
далее приходят business_message / edited_business_message /
deleted_business_messages. Мы сохраняем контент (шифрованный), фиксируем правки и
помечаем удаления — НЕ стирая сам контент (в этом весь смысл хранилища).

Внешний контент из Telegram — ДАННЫЕ, не инструкции: ничего из него не
исполняем, только сохраняем; сырой текст/секреты не логируем.
"""
from __future__ import annotations

import logging

import asyncpg
from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Message

from services import vault_service as vault

log = logging.getLogger(__name__)
router = Router(name="business_vault")


@router.message(Command("vault"))
async def cmd_vault(message: Message, pool: asyncpg.Pool) -> None:
    """Экран «Хранилище»: статус подключения, инструкция и кнопка открытия архива."""
    uid = message.from_user.id if message.from_user else None
    conn = await vault.active_connection_for_owner(pool, uid) if uid else None

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    _has_btn = False
    try:
        from config import MINI_APP_URL as _URL
        from bot.handlers.botmother_menu import _valid_mini_app_url
        from aiogram.types import WebAppInfo
        url = _valid_mini_app_url(_URL)
        if url:
            # web_app-кнопка не передаёт start_param → кладём #vault в URL, мини-апп
            # его читает при загрузке и открывает Хранилище сразу.
            deep = f"{url}#vault" if "?" not in url else f"{url}&screen=vault"
            kb.button(text="🗄 Открыть Хранилище", web_app=WebAppInfo(url=deep))
            _has_btn = True
    except Exception:
        log.debug("cmd_vault: mini-app URL недоступен")

    me = await message.bot.get_me()
    handle = f"@{me.username}" if me and me.username else "этого бота"
    if conn:
        head = ("✅ <b>Хранилище подключено.</b>\n\n"
                "Бот сохраняет все сообщения в ваших личных чатах — даже удалённые. "
                "Откройте архив кнопкой ниже.")
        if not conn.get("can_reply"):
            head += ("\n\nЧтобы отвечать собеседникам прямо из «Хранилища», включите боту "
                     "право отвечать в настройках бизнес-аккаунта.")
    else:
        head = (
            "🗄 <b>Хранилище переписки</b>\n\n"
            "Бот <b>автоматически сохраняет все сообщения</b>, которые вы отправляете и "
            "получаете в своих чатах — чтобы вы могли вернуться к ним в любой момент, "
            "<b>даже если диалог удалён</b>.\n\n"
            "Чтобы бот начал запись, подключите его в бизнес-аккаунте:\n"
            "1. Настройки Telegram → <b>Telegram для бизнеса</b>\n"
            "2. <b>Чат-боты</b>\n"
            f"3. Введите <b>{handle}</b> и разрешите доступ к чатам\n\n"
            "🔒 Все сообщения хранятся в зашифрованном виде.")
    await message.answer(head, parse_mode="HTML",
                         reply_markup=kb.as_markup() if _has_btn else None)
    # Сам список диалогов — прямо в боте, как список чатов Telegram. Раньше
    # /vault отвечал только кнопкой «Открыть», и без мини-аппа к переписке
    # было не подойти вовсе.
    if uid:
        try:
            text, markup = await render_dialog_list(pool, uid, "all", 0)
            if markup is not None:
                await message.answer(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            log.debug("cmd_vault: список диалогов не собран owner=%s", uid,
                      exc_info=True)


def _rights_dict(bc: BusinessConnection) -> dict:
    rights = getattr(bc, "rights", None)
    if rights is None:
        return {}
    try:
        return rights.model_dump(exclude_none=True)  # pydantic (aiogram 3.x)
    except Exception:
        return {}


def _can_reply(bc: BusinessConnection) -> bool:
    # Bot API 9.0 перенёс can_reply в rights.can_reply; старое поле can_reply
    # оставлено для совместимости. Читаем оба, приоритет — rights.
    rights = getattr(bc, "rights", None)
    if rights is not None and getattr(rights, "can_reply", None) is not None:
        return bool(rights.can_reply)
    return bool(getattr(bc, "can_reply", False))


@router.business_connection()
async def on_business_connection(event: BusinessConnection, bot: Bot,
                                 pool: asyncpg.Pool) -> None:
    """Подключение/отключение/изменение прав бизнес-бота."""
    owner = getattr(event, "user", None)
    owner_id = getattr(owner, "id", None)
    if owner_id is None:
        return
    is_enabled = bool(getattr(event, "is_enabled", True))
    can_reply = _can_reply(event)
    try:
        await vault.upsert_connection(
            pool, event.id, owner_id, getattr(event, "user_chat_id", None),
            can_reply, is_enabled, _rights_dict(event))
    except Exception:
        log.exception("vault: upsert_connection failed conn=%s", event.id)
        return
    log.info("vault: business connection %s owner=%s enabled=%s reply=%s",
             event.id, owner_id, is_enabled, can_reply)

    chat_id = getattr(event, "user_chat_id", None) or owner_id
    try:
        if is_enabled:
            await bot.send_message(
                chat_id,
                "✅ <b>Хранилище подключено!</b>\n\n"
                "Теперь бот сохраняет все сообщения в ваших личных чатах — вы сможете "
                "вернуться к ним в любой момент, <b>даже если диалог удалён</b>.\n\n"
                + ("Также вы можете <b>отвечать собеседникам прямо из «Хранилища»</b>.\n\n"
                   if can_reply else
                   "Чтобы отвечать собеседникам прямо из «Хранилища», включите боту "
                   "право отвечать в настройках бизнес-аккаунта.\n\n")
                + "Откройте <b>«Хранилище»</b>, чтобы просмотреть архив.",
                parse_mode="HTML")
        else:
            await bot.send_message(
                chat_id,
                "⏸ <b>Хранилище отключено.</b> Новые сообщения больше не сохраняются. "
                "Уже сохранённое остаётся в архиве.",
                parse_mode="HTML")
    except Exception:
        log.debug("vault: не удалось отправить уведомление о подключении owner=%s", owner_id)


async def _owner_for(pool: asyncpg.Pool, conn_id: str | None) -> int | None:
    if not conn_id:
        return None
    conn = await vault.get_connection(pool, conn_id)
    return conn["owner_id"] if conn else None


@router.business_message()
async def on_business_message(message: Message, bot: Bot, pool: asyncpg.Pool) -> None:
    """Новое сообщение в бизнес-чате пользователя — архивируем + сенсор намерений."""
    conn_id = getattr(message, "business_connection_id", None)
    owner_id = await _owner_for(pool, conn_id)
    if owner_id is None:
        log.debug("vault: business_message без известного подключения %s", conn_id)
        return
    try:
        await vault.archive_message(pool, message, owner_id, conn_id)
    except Exception:
        log.exception("vault: archive_message failed owner=%s", owner_id)
    # Сенсор намерений: только ВХОДЯЩИЕ (что написал собеседник). Fail-open —
    # сбой сенсора не должен ломать архивацию.
    try:
        if vault.direction_of(message, owner_id) == "in":
            from services import intent_sensor
            await intent_sensor.scan_incoming(
                pool, bot, owner_id, vault.peer_of(message), vault.text_of(message))
    except Exception:
        log.debug("vault: intent_sensor failed owner=%s", owner_id)


@router.edited_business_message()
async def on_edited_business_message(message: Message, bot: Bot,
                                     pool: asyncpg.Pool) -> None:
    """Правка сообщения — сохраняем и старую, и новую версию; «ловец» шлёт
    было→стало (только для входящих правок собеседника)."""
    conn_id = getattr(message, "business_connection_id", None)
    owner_id = await _owner_for(pool, conn_id)
    if owner_id is None:
        return
    try:
        info = await vault.record_edit(pool, message, owner_id)
    except Exception:
        log.exception("vault: record_edit failed owner=%s", owner_id)
        return
    if not info.get("changed") or info.get("direction") != "in":
        return
    try:
        prefs = await vault.get_notify_prefs(pool, owner_id)
        if not prefs["notify_edited"]:
            return
        who = _esc(info.get("peer_name") or "Собеседник")
        await bot.send_message(
            prefs["user_chat_id"] or owner_id,
            f"✏️ <b>{who}</b> изменил сообщение:\n\n"
            f"<b>Было:</b> {_esc(_short(info['old']))}\n"
            f"<b>Стало:</b> {_esc(_short(info['new']))}",
            parse_mode="HTML")
    except Exception:
        log.debug("vault: edit-уведомление не отправлено owner=%s", owner_id)


@router.deleted_business_messages()
async def on_deleted_business_messages(event: BusinessMessagesDeleted, bot: Bot,
                                       pool: asyncpg.Pool) -> None:
    """Удаление у пользователя — помечаем в архиве, контент СОХРАНЯЕМ; «ловец»
    шлёт, что именно удалил собеседник."""
    owner_id = await _owner_for(pool, getattr(event, "business_connection_id", None))
    if owner_id is None:
        return
    chat_id = getattr(getattr(event, "chat", None), "id", None)
    msg_ids = list(getattr(event, "message_ids", None) or [])
    if chat_id is None or not msg_ids:
        return
    try:
        # Сначала достаём содержимое ВХОДЯЩИХ (для уведомления), пока не потеряли контекст.
        incoming = await vault.deleted_incoming_rows(pool, owner_id, chat_id, msg_ids)
        n = await vault.mark_deleted(pool, owner_id, chat_id, msg_ids)
        log.info("vault: помечено удалёнными %d сообщ. owner=%s chat=%s", n, owner_id, chat_id)
    except Exception:
        log.exception("vault: mark_deleted failed owner=%s", owner_id)
        return
    if not incoming:
        return  # удалены только исходящие самого пользователя — не сигнал
    try:
        prefs = await vault.get_notify_prefs(pool, owner_id)
        if not prefs["notify_deleted"]:
            return
        who = _esc((incoming[0].get("peer_name")) or "Собеседник")
        lines = [f"🗑 <b>{who}</b> удалил сообщений: {len(incoming)}"]
        has_media = False
        for m in incoming[:5]:
            body = m["text"] or m["media_label"] or "—"
            if m.get("media_label"):
                has_media = True
            lines.append(f"• {_esc(_short(body))}")
        if has_media:
            # Подпись «📷 Фото» без самого файла бесполезна ровно в тот момент,
            # когда он нужнее всего — вложение сохранено, и его можно открыть.
            lines.append("\n📎 Вложение сохранено — откройте, чтобы посмотреть.")
        await bot.send_message(prefs["user_chat_id"] or owner_id,
                               "\n".join(lines), parse_mode="HTML",
                               reply_markup=_open_chat_kb(chat_id))
    except Exception:
        log.debug("vault: delete-уведомление не отправлено owner=%s", owner_id)


def _open_chat_kb(chat_id: int):
    """Кнопка «открыть этот чат в Хранилище».

    Без неё уведомление было тупиком: «собеседник удалил сообщение» — а чтобы
    увидеть ЧТО (особенно вложение, которое теперь можно открыть), надо было
    вручную зайти в приложение, найти раздел и нужный чат среди прочих.
    Мини-апп понимает «#vault:<chat_id>» и открывает сразу переписку.
    Возвращает None, если URL мини-аппа не настроен — тогда шлём как раньше.
    """
    try:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
        from bot.handlers.botmother_menu import _valid_mini_app_url
        from config import MINI_APP_URL

        url = _valid_mini_app_url(MINI_APP_URL)
        if not url:
            return None
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔎 Открыть переписку",
                                 web_app=WebAppInfo(url=f"{url}#vault:{int(chat_id)}"))
        ], [
            # Карточка диалога в самом боте: сколько сообщений, с какого дня
            # переписка, что удалено, как быстро вы отвечаете.
            InlineKeyboardButton(text="ℹ️ О диалоге",
                                 callback_data=VaultCb(a="open", c=int(chat_id)).pack())
        ]])
    except Exception:
        return None


def _esc(s) -> str:
    import html
    return html.escape(str(s or ""))


def _short(s, n: int = 200) -> str:
    s = " ".join(str(s or "").split())
    return (s[:n] + "…") if len(s) > n else (s or "—")



# ── Список диалогов в боте ───────────────────────────────────────────────────
# То же, что в мини-аппе, но кнопками: папки, непрочитанные, ждущие ответа,
# карточка диалога с аналитикой и действиями. Решение «ждёт ли ответа» — одно,
# в services.vault_dialogs; его же читает напоминание организма.

from aiogram import F  # noqa: E402
from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.state import State, StatesGroup  # noqa: E402
from aiogram.types import CallbackQuery  # noqa: E402

from bot.callbacks import VaultCb  # noqa: E402

_PAGE = 8
_FOLDER_LABEL = {"all": "Все", "unread": "Непрочит.", "waiting": "Ждут",
                 "archived": "Архив", "deleted": "Удалённые"}


class VaultReplyFSM(StatesGroup):
    text = State()


def _ago(iso: str | None) -> str:
    """«5 мин», «3 ч», «2 дн» — без часового пояса, который бот не знает."""
    if not iso:
        return ""
    import datetime as _dt
    try:
        ts = _dt.datetime.fromisoformat(iso)
    except ValueError:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    secs = max(0, (_dt.datetime.now(_dt.timezone.utc) - ts).total_seconds())
    if secs < 60:
        return "сейчас"
    if secs < 3600:
        return f"{int(secs // 60)} мин"
    if secs < 86400:
        return f"{int(secs // 3600)} ч"
    if secs < 86400 * 30:
        return f"{int(secs // 86400)} дн"
    return ts.strftime("%d.%m.%y")


def _row_label(c: dict) -> str:
    marks = ""
    if c.get("pinned"):
        marks += "📌"
    if c.get("unread"):
        marks += f"🔵{c['unread']}"
    if c.get("awaiting_reply"):
        marks += "⏳"
    if c.get("purchased"):
        marks += "💰"
    prev = c.get("last_preview") or ""
    if c.get("last_direction") == "out" and prev:
        prev = "Вы: " + prev
    if c.get("last_deleted"):
        prev = "🗑 " + prev
    when = _ago(c.get("last_date"))
    label = f"{marks} {c['peer_name']}".strip()
    if prev:
        label += f": {prev}"
    if len(label) > 52:
        label = label[:51] + "…"
    return f"{label} · {when}" if when else label


async def render_dialog_list(pool, owner_id: int, folder: str, page: int):
    """(text, markup) списка диалогов. markup=None — архив пуст, показывать нечего."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from services import vault_dialogs as vd
    res = await vd.dialogs(pool, owner_id, folder=folder, limit=500)
    counts = res["counts"]
    if not counts.get("all") and not counts.get("archived"):
        return ("", None)
    chats = res["chats"]
    pages = max(1, (len(chats) + _PAGE - 1) // _PAGE)
    page = max(0, min(int(page), pages - 1))
    chunk = chats[page * _PAGE:(page + 1) * _PAGE]

    head = [f"💬 <b>Диалоги</b> · {_FOLDER_LABEL.get(folder, 'Все')}"]
    bits = [f"всего {counts['all']}"]
    if counts.get("unread"):
        bits.append(f"🔵 непрочитанных {counts['unread']} "
                    f"({counts.get('unread_messages', 0)} сообщ.)")
    if counts.get("waiting"):
        bits.append(f"⏳ ждут ответа {counts['waiting']}")
    head.append(" · ".join(bits))
    if not chunk:
        head.append("\nВ этой папке пусто.")
    else:
        head.append("\nНажмите на диалог — откроется карточка с перепиской, "
                    "аналитикой и действиями.")

    kb = InlineKeyboardBuilder()
    tabs = []
    for f in ("all", "unread", "waiting", "archived"):
        n = counts.get(f, 0)
        title = _FOLDER_LABEL[f] + (f" {n}" if n and f != "all" else "")
        if f == folder:
            title = "• " + title
        tabs.append(kb.button(text=title, callback_data=VaultCb(a="list", f=f, p=0)))
    for c in chunk:
        kb.button(text=_row_label(c),
                  callback_data=VaultCb(a="open", c=int(c["chat_id"]), f=folder, p=page))
    nav = 0
    if page > 0:
        kb.button(text="◀", callback_data=VaultCb(a="list", f=folder, p=page - 1)); nav += 1
    if pages > 1:
        kb.button(text=f"{page + 1}/{pages}",
                  callback_data=VaultCb(a="list", f=folder, p=page)); nav += 1
    if page < pages - 1:
        kb.button(text="▶", callback_data=VaultCb(a="list", f=folder, p=page + 1)); nav += 1
    kb.adjust(4, *([1] * len(chunk)), *([nav] if nav else []))
    return "\n".join(head), kb.as_markup()


async def render_dialog_card(pool, owner_id: int, chat_id: int, folder: str,
                             page: int, *, with_messages: int = 5):
    """(text, markup) карточки диалога: сводка + последние сообщения + действия."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from services import vault_dialogs as vd
    info = await vd.dialog_info(pool, owner_id, chat_id)
    if not info:
        return ("Диалог не найден в Хранилище.", None)
    text = vd.info_text(info)
    if with_messages:
        res = await vault.list_messages(pool, owner_id, chat_id, limit=with_messages,
                                        newest_first=True)
        lines = []
        for m in res["messages"]:
            who = "Вы" if m["direction"] == "out" else _esc(info["peer_name"])
            body = m["text"] or m["media_label"] or "—"
            mark = " 🗑" if m["deleted"] else (" ✏️" if m["edited"] else "")
            lines.append(f"<b>{who}</b> · {_ago(m['date'])}{mark}\n{_esc(_short(body, 160))}")
        if lines:
            text += "\n\n— последние сообщения —\n" + "\n\n".join(lines)
    kb = InlineKeyboardBuilder()
    c = int(chat_id)
    kb.button(text="✍️ Ответить", callback_data=VaultCb(a="reply", c=c, f=folder, p=page))
    kb.button(text="📜 Ещё 15", callback_data=VaultCb(a="msgs", c=c, f=folder, p=page))
    kb.button(text=("↩️ Снять «отвечено»" if info.get("handled") else "✅ Отвечено"),
              callback_data=VaultCb(a="done", c=c, f=folder, p=page))
    kb.button(text=("💰 Не купил" if info.get("purchased") else "💰 Купил"),
              callback_data=VaultCb(a="buy", c=c, f=folder, p=page))
    kb.button(text=("📌 Открепить" if info.get("pinned") else "📌 Закрепить"),
              callback_data=VaultCb(a="pin", c=c, f=folder, p=page))
    kb.button(text=("🗂 Из архива" if info.get("archived") else "🗄 В архив"),
              callback_data=VaultCb(a="arch", c=c, f=folder, p=page))
    kb.button(text=("🔔 Напоминать" if info.get("muted") else "🔕 Не напоминать"),
              callback_data=VaultCb(a="mute", c=c, f=folder, p=page))
    rows = [2, 2, 2, 1]
    web = _open_chat_kb(c)
    if web is not None:
        kb.button(text="📱 Переписка в приложении",
                  web_app=web.inline_keyboard[0][0].web_app)
        rows.append(1)
    kb.button(text="← К списку", callback_data=VaultCb(a="list", f=folder, p=page))
    rows.append(1)
    kb.adjust(*rows)
    if len(text) > 4000:
        text = text[:3990] + "…"
    return text, kb.as_markup()


_TOGGLES = {"done": ("handled", "handled"), "buy": ("purchased", "purchased"),
            "pin": ("pinned", "pinned"), "arch": ("archived", "archived"),
            "mute": ("muted", "muted")}


@router.callback_query(VaultCb.filter())
async def on_vault_cb(cq: CallbackQuery, callback_data: VaultCb, pool: asyncpg.Pool,
                      state: FSMContext) -> None:
    uid = cq.from_user.id if cq.from_user else None
    if not uid:
        await cq.answer()
        return
    from services import vault_dialogs as vd
    a, c, f, p = callback_data.a, callback_data.c, callback_data.f, callback_data.p
    try:
        if a == "list":
            await state.clear()
            text, markup = await render_dialog_list(pool, uid, f, p)
            if markup is None:
                await cq.answer("Хранилище пока пусто", show_alert=True)
                return
            await _edit_or_send(cq, text, markup)
        elif a == "open":
            await vd.set_state(pool, uid, c, read=True)
            text, markup = await render_dialog_card(pool, uid, c, f, p)
            await _edit_or_send(cq, text, markup)
        elif a == "msgs":
            text, markup = await render_dialog_card(pool, uid, c, f, p, with_messages=15)
            await _edit_or_send(cq, text, markup)
        elif a in _TOGGLES:
            key, info_key = _TOGGLES[a]
            info = await vd.dialog_info(pool, uid, c) or {}
            await vd.set_state(pool, uid, c, **{key: not bool(info.get(info_key))})
            text, markup = await render_dialog_card(pool, uid, c, f, p)
            await _edit_or_send(cq, text, markup)
        elif a == "reply":
            conn = await vault.active_connection_for_owner(pool, uid)
            if not conn or not conn.get("can_reply"):
                await cq.answer("Боту не дано право отвечать: включите его в "
                                "Telegram для бизнеса → Чат-боты.", show_alert=True)
                return
            await state.set_state(VaultReplyFSM.text)
            await state.update_data(vault_chat=c, vault_folder=f, vault_page=p)
            from aiogram.utils.keyboard import InlineKeyboardBuilder
            kb = InlineKeyboardBuilder()
            kb.button(text="✖️ Отмена", callback_data=VaultCb(a="cancel", c=c, f=f, p=p))
            await cq.message.answer(
                "✍️ Напишите ответ одним сообщением — бот отправит его собеседнику "
                "от вашего имени.", reply_markup=kb.as_markup())
        elif a == "cancel":
            await state.clear()
            text, markup = await render_dialog_card(pool, uid, c, f, p)
            await _edit_or_send(cq, text, markup)
    except Exception:
        log.exception("vault cb failed owner=%s action=%s", uid, a)
        await cq.answer("Не получилось, попробуйте ещё раз", show_alert=True)
        return
    await cq.answer()


@router.message(VaultReplyFSM.text, F.text)
async def on_vault_reply_text(message: Message, bot: Bot, pool: asyncpg.Pool,
                              state: FSMContext) -> None:
    uid = message.from_user.id if message.from_user else None
    data = await state.get_data()
    chat_id = data.get("vault_chat")
    await state.clear()
    if not uid or not chat_id:
        return
    res = await vault.send_reply(pool, bot, uid, int(chat_id), message.text or "")
    if not res.get("ok"):
        await message.answer(f"⚠️ {_esc(res.get('error') or 'Не отправлено')}",
                             parse_mode="HTML")
        return
    text, markup = await render_dialog_card(
        pool, uid, int(chat_id), data.get("vault_folder") or "all",
        int(data.get("vault_page") or 0))
    await message.answer("✅ Отправлено.\n\n" + text, parse_mode="HTML",
                         reply_markup=markup)


async def _edit_or_send(cq: CallbackQuery, text: str, markup) -> None:
    try:
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception as exc:
        if "not modified" in str(exc):
            return
        await cq.message.answer(text, parse_mode="HTML", reply_markup=markup)
