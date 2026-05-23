from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.callbacks import (
    BotCb, EditCb, AudCb, WebhookCb, BroadcastCb, BulkCb,
    CommandsCb, TemplateCb, ScheduleCb, MultigeoCb, AutoReplyCb, RelayCb, FunnelCb, StatsCb,
)

PAGE_SIZE = 5

LANGUAGES = [
    ("ru", "🇷🇺", "Русский"),
    ("en", "🇬🇧", "English"),
    ("uk", "🇺🇦", "Українська"),
    ("de", "🇩🇪", "Deutsch"),
    ("fr", "🇫🇷", "Français"),
    ("es", "🇪🇸", "Español"),
    ("it", "🇮🇹", "Italiano"),
    ("pt", "🇵🇹", "Português"),
    ("pl", "🇵🇱", "Polski"),
    ("tr", "🇹🇷", "Türkçe"),
]


def main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 Мои боты",           callback_data=BotCb(action="list", page=0))
    kb.button(text="➕ Добавить бота",       callback_data=BotCb(action="add"))
    kb.button(text="📥 Импорт ботов",        callback_data=BulkCb(action="import"))
    kb.button(text="📦 Массовые операции",   callback_data=BulkCb(action="menu"))
    kb.adjust(2, 2)
    return kb.as_markup()


def bulk_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Имя всем",            callback_data=BulkCb(action="name"))
    kb.button(text="🌍 Имя по GEO",          callback_data=BulkCb(action="name_lang"))
    kb.button(text="📄 Описание всем",       callback_data=BulkCb(action="desc"))
    kb.button(text="🌍 Описание по GEO",     callback_data=BulkCb(action="desc_lang"))
    kb.button(text="📃 Краткое всем",        callback_data=BulkCb(action="short"))
    kb.button(text="🌍 Краткое по GEO",      callback_data=BulkCb(action="short_lang"))
    kb.button(text="🤖 Команды всем",        callback_data=BulkCb(action="commands"))
    kb.button(text="🌍 Команды по GEO",      callback_data=BulkCb(action="commands_lang"))
    kb.button(text="🔍 Проверить токены",    callback_data=BulkCb(action="check"))
    kb.button(text="◀️ Главное меню",        callback_data=BotCb(action="list", page=0))
    kb.adjust(2, 2, 2, 2, 1, 1)
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
    kb.button(text="✏️ Профиль",      callback_data=EditCb(action="menu", bot_id=bot_id))
    kb.button(text="👥 Аудитория",    callback_data=AudCb(action="menu", bot_id=bot_id))
    kb.button(text="📢 Рассылка",     callback_data=BroadcastCb(action="menu", bot_id=bot_id))
    kb.button(text="⏰ Расписание",   callback_data=ScheduleCb(action="menu", bot_id=bot_id))
    kb.button(text="🤖 Команды",      callback_data=CommandsCb(action="menu", bot_id=bot_id))
    kb.button(text="📝 Шаблоны",      callback_data=TemplateCb(action="list", bot_id=bot_id))
    kb.button(text="💬 Авто-ответы",  callback_data=AutoReplyCb(action="menu", bot_id=bot_id))
    kb.button(text="📨 Inbox",         callback_data=RelayCb(action="menu", bot_id=bot_id))
    kb.button(text="🔗 Цепочки",      callback_data=FunnelCb(action="list", bot_id=bot_id))
    kb.button(text="🌐 Вебхук",       callback_data=WebhookCb(action="menu", bot_id=bot_id))
    kb.button(text="⚖️ Сравнить",    callback_data=AudCb(action="compare", bot_id=bot_id))
    kb.button(text="📊 Статистика",   callback_data=StatsCb(action="menu", bot_id=bot_id))
    kb.button(text="🗑 Удалить",      callback_data=BotCb(action="delete", bot_id=bot_id))
    kb.button(text="◀️ К списку",    callback_data=BotCb(action="list", page=0))
    kb.adjust(2, 2, 2, 2, 2, 2, 2, 1)
    return kb.as_markup()


def edit_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Имя",               callback_data=EditCb(action="name", bot_id=bot_id))
    kb.button(text="📄 Описание",          callback_data=EditCb(action="desc", bot_id=bot_id))
    kb.button(text="📃 Краткое описание",  callback_data=EditCb(action="short", bot_id=bot_id))
    kb.button(text="🌍 Мультигео",         callback_data=MultigeoCb(action="menu", bot_id=bot_id))
    kb.button(text="🖼 Фото",              callback_data=EditCb(action="photo", bot_id=bot_id))
    kb.button(text="🗑 Удалить фото",      callback_data=EditCb(action="del_photo", bot_id=bot_id))
    kb.button(text="🔑 Обновить токен",    callback_data=EditCb(action="update_token", bot_id=bot_id))
    kb.button(text="◀️ Назад",             callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(2, 2, 2, 1, 1)
    return kb.as_markup()


def audience_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить",      callback_data=AudCb(action="refresh", bot_id=bot_id))
    kb.button(text="📊 Статистика",    callback_data=AudCb(action="stats", bot_id=bot_id))
    kb.button(text="📤 Экспорт CSV",   callback_data=AudCb(action="export", bot_id=bot_id))
    kb.button(text="⚖️ Сравнить",     callback_data=AudCb(action="compare", bot_id=bot_id))
    kb.button(text="◀️ Назад",        callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def webhook_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Получить информацию",   callback_data=WebhookCb(action="info", bot_id=bot_id))
    kb.button(text="🔌 Отключить другие боты", callback_data=WebhookCb(action="disable", bot_id=bot_id))
    kb.button(text="◀️ Назад",                 callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def broadcast_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Написать рассылку",   callback_data=BroadcastCb(action="compose", bot_id=bot_id))
    kb.button(text="📋 Из шаблона",          callback_data=BroadcastCb(action="from_template", bot_id=bot_id))
    kb.button(text="🎯 По сегменту",         callback_data=BroadcastCb(action="segment", bot_id=bot_id))
    kb.button(text="📋 История рассылок",    callback_data=BroadcastCb(action="status", bot_id=bot_id))
    kb.button(text="◀️ Назад",               callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def broadcast_history(bot_id: int, broadcasts: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    status_emoji = {"pending": "⏳", "running": "🔄", "done": "✅", "cancelled": "❌"}
    for bc in broadcasts[:10]:
        emoji = status_emoji.get(bc["status"], "❓")
        date_str = bc["created_at"].strftime("%d.%m %H:%M")
        kb.button(
            text=f"{emoji} #{bc['id']} {date_str} ({bc['sent_count']}/{bc['total_users']})",
            callback_data=BroadcastCb(action="detail", bot_id=bot_id, broadcast_id=bc["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=BroadcastCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def broadcast_detail(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К истории", callback_data=BroadcastCb(action="status", bot_id=bot_id))
    return kb.as_markup()


def broadcast_from_template(bot_id: int, templates: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for t in templates[:8]:
        kb.button(
            text=f"📋 {t['name']}",
            callback_data=BroadcastCb(action="use_template", bot_id=bot_id, broadcast_id=t["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=BroadcastCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def broadcast_confirm(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Запустить",  callback_data=BroadcastCb(action="confirm", bot_id=bot_id))
    kb.button(text="❌ Отмена",     callback_data=BroadcastCb(action="cancel", bot_id=bot_id))
    kb.adjust(2)
    return kb.as_markup()


def broadcast_segment_menu(bot_id: int, languages: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    FLAG_MAP = {"ru": "🇷🇺", "en": "🇬🇧", "uk": "🇺🇦", "de": "🇩🇪",
                "fr": "🇫🇷", "es": "🇪🇸", "it": "🇮🇹", "pt": "🇵🇹",
                "pl": "🇵🇱", "tr": "🇹🇷", "unknown": "🌐"}
    for lang_info in languages[:8]:
        lang = lang_info["lang"]
        flag = FLAG_MAP.get(lang, "🌐")
        kb.button(
            text=f"{flag} {lang.upper()} ({lang_info['count']})",
            callback_data=BroadcastCb(action="segment_select", bot_id=bot_id, lang=lang),
        )
    kb.button(text="◀️ Назад", callback_data=BroadcastCb(action="menu", bot_id=bot_id))
    kb.adjust(2, 1)
    return kb.as_markup()


def commands_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить команду",    callback_data=CommandsCb(action="add", bot_id=bot_id))
    kb.button(text="📋 Задать весь список",  callback_data=CommandsCb(action="set_all", bot_id=bot_id))
    kb.button(text="🗑 Удалить все команды", callback_data=CommandsCb(action="delete", bot_id=bot_id))
    kb.button(text="◀️ Назад",              callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def templates_list(templates: list, bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for tpl in templates[:10]:
        kb.button(
            text=f"📝 {tpl['name'][:28]}",
            callback_data=TemplateCb(action="view", template_id=tpl["id"], bot_id=bot_id),
        )
    kb.adjust(1)
    kb.row(InlineKeyboardButton(
        text="➕ Новый шаблон",
        callback_data=TemplateCb(action="add", bot_id=bot_id).pack(),
    ))
    if bot_id:
        kb.row(InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=BotCb(action="select", bot_id=bot_id).pack(),
        ))
    else:
        kb.row(InlineKeyboardButton(
            text="◀️ Главное меню",
            callback_data=BotCb(action="list", page=0).pack(),
        ))
    return kb.as_markup()


def template_actions(template_id: int, bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if bot_id:
        kb.button(
            text="📢 Использовать для рассылки",
            callback_data=TemplateCb(action="use", template_id=template_id, bot_id=bot_id),
        )
    kb.button(
        text="🗑 Удалить шаблон",
        callback_data=TemplateCb(action="delete", template_id=template_id, bot_id=bot_id),
    )
    kb.button(
        text="◀️ К шаблонам",
        callback_data=TemplateCb(action="list", bot_id=bot_id),
    )
    kb.adjust(1)
    return kb.as_markup()


def schedule_menu(bot_id: int, schedules: list | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="➕ Запланировать рассылку",
        callback_data=ScheduleCb(action="create", bot_id=bot_id),
    )
    kb.button(text="📋 Из шаблона", callback_data=ScheduleCb(action="from_template", bot_id=bot_id))
    if schedules:
        for s in schedules:
            if s["status"] == "pending":
                dt = s["execute_at"].strftime("%d.%m %H:%M")
                preview = s["message_text"][:20].replace("\n", " ")
                kb.button(
                    text=f"❌ Отменить {dt} — {preview}…",
                    callback_data=ScheduleCb(action="cancel", bot_id=bot_id, schedule_id=s["id"]),
                )
    kb.button(text="◀️ Назад", callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def schedule_template_list(bot_id: int, templates: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for t in templates[:8]:
        kb.button(
            text=f"📋 {t['name']}",
            callback_data=ScheduleCb(action="use_template", bot_id=bot_id, schedule_id=t["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=ScheduleCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def bots_pick(bots: list, exclude_bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for bot in bots:
        if bot["bot_id"] == exclude_bot_id:
            continue
        label = f"@{bot['username']}" if bot["username"] else bot["first_name"]
        kb.button(
            text=label,
            callback_data=AudCb(action="pick_b", bot_id=exclude_bot_id, target_id=bot["bot_id"]),
        )
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


def multigeo_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Мультигео имена",       callback_data=MultigeoCb(action="names", bot_id=bot_id))
    kb.button(text="📋 Мультигео about",       callback_data=MultigeoCb(action="short", bot_id=bot_id))
    kb.button(text="📄 Мультигео description", callback_data=MultigeoCb(action="desc", bot_id=bot_id))
    kb.button(text="◀️ Назад",                 callback_data=EditCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def multigeo_field(bot_id: int, field: str, lang_vals: dict) -> InlineKeyboardMarkup:
    # field: "name", "short", "desc"
    # lang_vals: {"ru": "current value or ''", ...}
    kb = InlineKeyboardBuilder()
    action = f"lang_{field}"
    for code, flag, name in LANGUAGES:
        val = lang_vals.get(code, "")
        display = (val[:15] + "…") if len(val) > 15 else (val or "—")
        kb.button(
            text=f"{flag} {name}: {display}",
            callback_data=MultigeoCb(action=action, bot_id=bot_id, lang=code),
        )
    kb.button(text="◀️ Назад", callback_data=MultigeoCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def auto_reply_menu(bot_id: int, replies: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for r in replies[:10]:
        icon = "✅" if r["is_active"] else "❌"
        trigger = {"start": "/start", "keyword": f"🔑{r['keyword']}", "any": "любое"}.get(r["trigger_type"], "?")
        preview = r["response_text"][:20].replace("\n", " ")
        kb.button(
            text=f"{icon} {trigger} → {preview}…",
            callback_data=AutoReplyCb(action="view", bot_id=bot_id, reply_id=r["id"]),
        )
    kb.adjust(1)
    kb.row(InlineKeyboardButton(
        text="➕ Добавить правило",
        callback_data=AutoReplyCb(action="add", bot_id=bot_id).pack(),
    ))
    kb.row(InlineKeyboardButton(
        text="◀️ Назад",
        callback_data=BotCb(action="select", bot_id=bot_id).pack(),
    ))
    return kb.as_markup()


def auto_reply_trigger_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="▶️ /start",         callback_data=AutoReplyCb(action="trig_start", bot_id=bot_id))
    kb.button(text="🔑 Ключевое слово", callback_data=AutoReplyCb(action="trig_keyword", bot_id=bot_id))
    kb.button(text="💬 Любое сообщение", callback_data=AutoReplyCb(action="trig_any", bot_id=bot_id))
    kb.button(text="◀️ Назад",           callback_data=AutoReplyCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def relay_menu(bot_id: int, relay_enabled: bool, sessions: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    toggle = "🔴 Отключить inbox" if relay_enabled else "🟢 Включить inbox"
    kb.button(text=toggle, callback_data=RelayCb(action="toggle", bot_id=bot_id))
    for s in sessions:
        name = f"@{s['username']}" if s["username"] else (s["first_name"] or str(s["user_id"]))
        preview = ((s["last_text"] or "нет сообщений")[:22]).replace("\n", " ")
        kb.button(
            text=f"💬 {name}: {preview}",
            callback_data=RelayCb(action="session", bot_id=bot_id, session_id=s["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def relay_session_view(bot_id: int, session_id: int, templates: list = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    # Quick reply template buttons
    if templates:
        for t in templates[:5]:  # max 5 шаблонов
            kb.button(
                text=f"💬 {t['name']}",
                callback_data=RelayCb(action="quick_reply", bot_id=bot_id,
                                      session_id=session_id, template_id=t["id"]),
            )
    kb.button(text="🗑 Закрыть диалог", callback_data=RelayCb(action="close_session", bot_id=bot_id, session_id=session_id))
    kb.button(text="◀️ К Inbox", callback_data=RelayCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def auto_reply_view(bot_id: int, reply_id: int, is_active: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    toggle_text = "❌ Отключить" if is_active else "✅ Включить"
    kb.button(text=toggle_text, callback_data=AutoReplyCb(action="toggle", bot_id=bot_id, reply_id=reply_id))
    kb.button(text="🗑 Удалить",  callback_data=AutoReplyCb(action="delete", bot_id=bot_id, reply_id=reply_id))
    kb.button(text="◀️ Назад",   callback_data=AutoReplyCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def funnels_list(bot_id: int, funnels: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for f in funnels[:8]:
        icon = "✅" if f["is_active"] else "❌"
        trigger = "/start" if f["trigger_type"] == "start" else f"🔑{f['keyword']}"
        kb.button(
            text=f"{icon} {f['name']} [{trigger}]",
            callback_data=FunnelCb(action="view", bot_id=bot_id, funnel_id=f["id"]),
        )
    kb.adjust(1)
    kb.row(InlineKeyboardButton(
        text="➕ Создать цепочку",
        callback_data=FunnelCb(action="create", bot_id=bot_id).pack(),
    ))
    kb.row(InlineKeyboardButton(
        text="◀️ Назад",
        callback_data=BotCb(action="select", bot_id=bot_id).pack(),
    ))
    return kb.as_markup()


def funnel_view(bot_id: int, funnel_id: int, is_active: bool, step_count: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    toggle = "❌ Отключить" if is_active else "✅ Включить"
    kb.button(text=toggle, callback_data=FunnelCb(action="toggle", bot_id=bot_id, funnel_id=funnel_id))
    kb.button(text="➕ Добавить шаг", callback_data=FunnelCb(action="add_step", bot_id=bot_id, funnel_id=funnel_id, step=step_count))
    kb.button(text="🗑 Удалить", callback_data=FunnelCb(action="delete", bot_id=bot_id, funnel_id=funnel_id))
    kb.button(text="◀️ К цепочкам", callback_data=FunnelCb(action="list", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def funnel_trigger_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="▶️ /start", callback_data=FunnelCb(action="trig_start", bot_id=bot_id))
    kb.button(text="🔑 Ключевое слово", callback_data=FunnelCb(action="trig_keyword", bot_id=bot_id))
    kb.button(text="◀️ Отмена", callback_data=FunnelCb(action="list", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()
