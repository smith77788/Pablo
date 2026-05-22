from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.callbacks import BotCb, EditCb, AudCb, WebhookCb, BroadcastCb, BulkCb

PAGE_SIZE = 5


def main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 Мои боты",       callback_data=BotCb(action="list", page=0))
    kb.button(text="➕ Добавить бота",   callback_data=BotCb(action="add"))
    kb.button(text="📦 Массовые операции", callback_data=BulkCb(action="menu"))
    kb.adjust(2, 1)
    return kb.as_markup()


def bulk_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Имя всем",           callback_data=BulkCb(action="name"))
    kb.button(text="🌍 Имя по GEO всем",    callback_data=BulkCb(action="name_lang"))
    kb.button(text="📄 Описание всем",      callback_data=BulkCb(action="desc"))
    kb.button(text="🌍 Описание по GEO",    callback_data=BulkCb(action="desc_lang"))
    kb.button(text="📃 Краткое всем",       callback_data=BulkCb(action="short"))
    kb.button(text="🌍 Краткое по GEO",     callback_data=BulkCb(action="short_lang"))
    kb.button(text="🔍 Проверить токены",   callback_data=BulkCb(action="check"))
    kb.button(text="◀️ Главное меню",       callback_data=BotCb(action="list", page=0))
    kb.adjust(2, 2, 2, 1, 1)
    return kb.as_markup()


def bots_list(bots: list, page: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    start = page * PAGE_SIZE
    chunk = bots[start: start + PAGE_SIZE]
    for bot in chunk:
        label = f"@{bot['username']}" if bot["username"] else bot["first_name"]
        kb.button(text=f"🤖 {label}", callback_data=BotCb(action="select", bot_id=bot["bot_id"]))
    kb.adjust(1)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=BotCb(action="list", page=page - 1).pack()))
    if start + PAGE_SIZE < len(bots):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=BotCb(action="list", page=page + 1).pack()))
    if nav:
        kb.row(*nav)

    kb.row(InlineKeyboardButton(text="➕ Добавить", callback_data=BotCb(action="add").pack()))
    return kb.as_markup()


def bot_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Профиль",    callback_data=EditCb(action="menu", bot_id=bot_id))
    kb.button(text="👥 Аудитория",  callback_data=AudCb(action="menu", bot_id=bot_id))
    kb.button(text="📢 Рассылка",   callback_data=BroadcastCb(action="menu", bot_id=bot_id))
    kb.button(text="🔗 Вебхук",     callback_data=WebhookCb(action="menu", bot_id=bot_id))
    kb.button(text="⚖️ Сравнить",   callback_data=AudCb(action="compare", bot_id=bot_id))
    kb.button(text="🗑 Удалить",    callback_data=BotCb(action="delete", bot_id=bot_id))
    kb.button(text="◀️ К списку",   callback_data=BotCb(action="list", page=0))
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()


def edit_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Имя",              callback_data=EditCb(action="name", bot_id=bot_id))
    kb.button(text="🌍 Имя по GEO",       callback_data=EditCb(action="name_lang", bot_id=bot_id))
    kb.button(text="📄 Описание",         callback_data=EditCb(action="desc", bot_id=bot_id))
    kb.button(text="🌍 Описание по GEO",  callback_data=EditCb(action="desc_lang", bot_id=bot_id))
    kb.button(text="📃 Краткое описание", callback_data=EditCb(action="short", bot_id=bot_id))
    kb.button(text="🌍 Краткое по GEO",   callback_data=EditCb(action="short_lang", bot_id=bot_id))
    kb.button(text="🖼 Фото",             callback_data=EditCb(action="photo", bot_id=bot_id))
    kb.button(text="◀️ Назад",            callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(2, 2, 2, 1, 1)
    return kb.as_markup()


def audience_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить",    callback_data=AudCb(action="refresh", bot_id=bot_id))
    kb.button(text="⚖️ Сравнить",   callback_data=AudCb(action="compare", bot_id=bot_id))
    kb.button(text="◀️ Назад",      callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(2, 1)
    return kb.as_markup()


def webhook_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Установить URL", callback_data=WebhookCb(action="set", bot_id=bot_id))
    kb.button(text="❌ Удалить вебхук", callback_data=WebhookCb(action="delete", bot_id=bot_id))
    kb.button(text="◀️ Назад",          callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(2, 1)
    return kb.as_markup()


def broadcast_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Написать рассылку", callback_data=BroadcastCb(action="compose", bot_id=bot_id))
    kb.button(text="📋 История",           callback_data=BroadcastCb(action="status", bot_id=bot_id))
    kb.button(text="◀️ Назад",             callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def broadcast_confirm(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Запустить", callback_data=BroadcastCb(action="confirm", bot_id=bot_id))
    kb.button(text="❌ Отмена",    callback_data=BroadcastCb(action="cancel", bot_id=bot_id))
    kb.adjust(2)
    return kb.as_markup()


def bots_pick(bots: list, exclude_bot_id: int) -> InlineKeyboardMarkup:
    """Keyboard for picking a second bot to compare audiences."""
    kb = InlineKeyboardBuilder()
    for bot in bots:
        if bot["bot_id"] == exclude_bot_id:
            continue
        label = f"@{bot['username']}" if bot["username"] else bot["first_name"]
        kb.button(text=label, callback_data=AudCb(action="pick_b",
                                                   bot_id=exclude_bot_id,
                                                   target_id=bot["bot_id"]))
    kb.adjust(1)
    return kb.as_markup()


def confirm_delete(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=BotCb(action="confirm_delete", bot_id=bot_id))
    kb.button(text="❌ Отмена",      callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(2)
    return kb.as_markup()


def back_to_bot(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К боту", callback_data=BotCb(action="select", bot_id=bot_id))
    return kb.as_markup()
