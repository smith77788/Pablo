from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.callbacks import (
    BotCb,
    EditCb,
    AudCb,
    WebhookCb,
    BroadcastCb,
    BulkCb,
    CommandsCb,
    TemplateCb,
    ScheduleCb,
    MultigeoCb,
    AutoReplyCb,
    RelayCb,
    FunnelCb,
    StatsCb,
    NoteCb,
    SwarmCb,
    CrmCb,
    AutoCb,
    ExperimentCb,
    DeepLinkCb,
    EngageCb,
    SeoCb,
    NetworkCb,
    ClusterCb,
    SubCb,
    AiCb,
    NetBcCb,
    AccCb,
    RankCb,
    ChanCb,
    RefCb,
    BmCb,
    TaskCb,
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


def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 BotMother OS",    callback_data=BmCb(action="main"))
    kb.button(text="➕ Добавить бота",   callback_data=BotCb(action="add"))
    kb.button(text="⚡ Активные задачи", callback_data=TaskCb(action="list"))
    kb.button(text="❓ Справка",         callback_data=BotCb(action="help"))
    if is_admin:
        kb.button(text="⚙️ Админка", callback_data="adm:main")
    kb.adjust(1, 2, 2, 1 if not is_admin else 2)
    return kb.as_markup()


def network_ops_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    # ── Управление сетью ──
    kb.button(text="📊 Аналитика сети", callback_data=NetworkCb(action="analytics"))
    kb.button(text="🌐 Кластеры", callback_data=NetworkCb(action="clusters"))
    kb.button(text="🏆 Рейтинг ботов", callback_data=NetworkCb(action="ranking"))
    kb.button(text="⚖️ Веса роутинга", callback_data=NetworkCb(action="routing"))
    kb.button(text="❤️ Здоровье сети", callback_data=NetworkCb(action="health"))
    kb.button(
        text="👥 Пересечение аудиторий", callback_data=NetworkCb(action="overlap")
    )
    kb.button(
        text="📢 Сетевая рассылка v2", callback_data=NetBcCb(action="choose_target")
    )
    kb.button(text="🔄 Клонировать настройки", callback_data=NetworkCb(action="clone"))
    # ── Массовые операции ──
    kb.button(text="✏️ Имя всем", callback_data=NetworkCb(action="bulk_name"))
    kb.button(text="🌍 Имя по GEO", callback_data=NetworkCb(action="bulk_name_lang"))
    kb.button(text="📄 Описание всем", callback_data=NetworkCb(action="bulk_desc"))
    kb.button(text="🌍 Описание GEO", callback_data=NetworkCb(action="bulk_desc_lang"))
    kb.button(text="📃 Краткое всем", callback_data=NetworkCb(action="bulk_short"))
    kb.button(text="🌍 Краткое GEO", callback_data=NetworkCb(action="bulk_short_lang"))
    kb.button(text="🤖 Команды всем", callback_data=NetworkCb(action="bulk_commands"))
    kb.button(
        text="🌍 Команды GEO", callback_data=NetworkCb(action="bulk_commands_lang")
    )
    kb.button(text="🔍 Проверить токены", callback_data=NetworkCb(action="bulk_check"))
    kb.button(text="📥 Импорт ботов", callback_data=NetworkCb(action="bulk_import"))
    kb.button(text="◀️ Главное меню", callback_data=BotCb(action="main"))
    kb.adjust(2, 2, 2, 2, 2, 2, 2, 2, 1, 1)
    return kb.as_markup()


def subscription_locked_markup(
    required_plan: str,
    back_callback=None,
) -> InlineKeyboardMarkup:
    """Lock screen markup. Pass back_callback to add a working Back button."""
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Оформить подписку", callback_data=SubCb(action="menu"))
    if back_callback is not None:
        kb.button(text="◀️ Назад", callback_data=back_callback)
    kb.adjust(1)
    return kb.as_markup()


def net_broadcast_target_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="📢 Всем ботам → их аудитория",
        callback_data=NetBcCb(action="choose_segment", segment="all_each"),
    )
    kb.button(
        text="🎯 Уникальным юзерам (дедупликация)",
        callback_data=NetBcCb(action="choose_segment", segment="unique"),
    )
    kb.button(
        text="❄️ Холодные по всей сети",
        callback_data=NetBcCb(action="type_message", segment="cold_all"),
    )
    kb.button(
        text="💀 Потерянные по всей сети",
        callback_data=NetBcCb(action="type_message", segment="lost_all"),
    )
    kb.button(
        text="🌍 По языку (вся сеть)", callback_data=NetBcCb(action="choose_lang")
    )
    kb.button(text="◀️ Сеть & операции", callback_data=NetworkCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()


def net_broadcast_lang_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code, flag, name in LANGUAGES:
        kb.button(
            text=f"{flag} {name}",
            callback_data=NetBcCb(action="type_message", segment="lang", lang=code),
        )
    kb.button(text="◀️ Назад", callback_data=NetBcCb(action="choose_target"))
    kb.adjust(2)
    return kb.as_markup()


def bulk_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Имя всем", callback_data=BulkCb(action="name"))
    kb.button(text="🌍 Имя по GEO", callback_data=BulkCb(action="name_lang"))
    kb.button(text="📄 Описание всем", callback_data=BulkCb(action="desc"))
    kb.button(text="🌍 Описание по GEO", callback_data=BulkCb(action="desc_lang"))
    kb.button(text="📃 Краткое всем", callback_data=BulkCb(action="short"))
    kb.button(text="🌍 Краткое по GEO", callback_data=BulkCb(action="short_lang"))
    kb.button(text="🤖 Команды всем", callback_data=BulkCb(action="commands"))
    kb.button(text="🌍 Команды по GEO", callback_data=BulkCb(action="commands_lang"))
    kb.button(text="🔍 Проверить токены", callback_data=BulkCb(action="check"))
    kb.button(text="◀️ Главное меню", callback_data=BotCb(action="main"))
    kb.adjust(2, 2, 2, 2, 1, 1)
    return kb.as_markup()


def bots_list(bots: list, page: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    start = page * PAGE_SIZE
    chunk = bots[start : start + PAGE_SIZE]
    for bot in chunk:
        label = f"@{bot['username']}" if bot["username"] else bot["first_name"]
        aud = bot["audience_count"] if "audience_count" in bot.keys() else ""
        suffix = f" · {aud} чел." if aud else ""
        kb.button(
            text=f"🤖 {label}{suffix}",
            callback_data=BotCb(action="select", bot_id=bot["bot_id"]),
        )
    kb.adjust(1)

    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀️", callback_data=BotCb(action="list", page=page - 1).pack()
            )
        )
    if start + PAGE_SIZE < len(bots):
        nav.append(
            InlineKeyboardButton(
                text="▶️", callback_data=BotCb(action="list", page=page + 1).pack()
            )
        )
    if nav:
        kb.row(*nav)

    kb.row(
        InlineKeyboardButton(
            text="➕ Добавить", callback_data=BotCb(action="add").pack()
        )
    )
    return kb.as_markup()


def bot_menu(bot_id: int, username: str | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Профиль", callback_data=EditCb(action="menu", bot_id=bot_id))
    kb.button(text="👥 Аудитория", callback_data=AudCb(action="menu", bot_id=bot_id))
    kb.button(
        text="📢 Рассылка", callback_data=BroadcastCb(action="menu", bot_id=bot_id)
    )
    kb.button(
        text="⏰ Расписание", callback_data=ScheduleCb(action="menu", bot_id=bot_id)
    )
    kb.button(text="🤖 Команды", callback_data=CommandsCb(action="menu", bot_id=bot_id))
    kb.button(text="📝 Шаблоны", callback_data=TemplateCb(action="list", bot_id=bot_id))
    kb.button(
        text="💬 Авто-ответы", callback_data=AutoReplyCb(action="menu", bot_id=bot_id)
    )
    kb.button(text="💬 Диалоги", callback_data=RelayCb(action="menu", bot_id=bot_id))
    kb.button(text="🔗 Цепочки", callback_data=FunnelCb(action="list", bot_id=bot_id))
    kb.button(text="🌐 Вебхук", callback_data=WebhookCb(action="menu", bot_id=bot_id))
    kb.button(text="⚖️ Сравнить", callback_data=AudCb(action="compare", bot_id=bot_id))
    kb.button(text="📊 Статистика", callback_data=StatsCb(action="menu", bot_id=bot_id))
    kb.button(text="🏷 CRM", callback_data=CrmCb(action="menu", bot_id=bot_id))
    kb.button(text="📝 Заметка", callback_data=NoteCb(action="edit", bot_id=bot_id))
    kb.button(text="🧬 Swarm", callback_data=SwarmCb(action="menu", bot_id=bot_id))
    kb.button(
        text="🧪 A/B Тесты", callback_data=ExperimentCb(action="list", bot_id=bot_id)
    )
    kb.button(
        text="🔗 Диплинки", callback_data=DeepLinkCb(action="menu", bot_id=bot_id)
    )
    kb.button(
        text="🎯 Активность", callback_data=EngageCb(action="menu", bot_id=bot_id)
    )
    kb.button(text="📈 SEO", callback_data=SeoCb(action="menu", bot_id=bot_id))
    kb.button(text="📊 Позиции", callback_data=RankCb(action="menu", bot_id=bot_id))
    kb.button(
        text="📤 Экспорт аудитории",
        callback_data=BotCb(action="export_audience", bot_id=bot_id),
    )
    kb.button(text="🗑 Удалить", callback_data=BotCb(action="delete", bot_id=bot_id))
    kb.button(text="◀️ К списку", callback_data=BotCb(action="list", page=0))
    if username:
        kb.row(
            InlineKeyboardButton(text="🔗 Открыть бота", url=f"https://t.me/{username}")
        )
    kb.adjust(2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 2, 1)
    return kb.as_markup()


def edit_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Имя", callback_data=EditCb(action="name", bot_id=bot_id))
    kb.button(text="📄 Описание", callback_data=EditCb(action="desc", bot_id=bot_id))
    kb.button(
        text="📃 Краткое описание", callback_data=EditCb(action="short", bot_id=bot_id)
    )
    kb.button(
        text="🌍 Мультигео", callback_data=MultigeoCb(action="menu", bot_id=bot_id)
    )
    kb.button(text="🖼 Фото", callback_data=EditCb(action="photo", bot_id=bot_id))
    kb.button(
        text="🗑 Удалить фото", callback_data=EditCb(action="del_photo", bot_id=bot_id)
    )
    kb.button(
        text="🔑 Обновить токен",
        callback_data=EditCb(action="update_token", bot_id=bot_id),
    )
    kb.button(
        text="💡 Проверить бота", callback_data=EditCb(action="health", bot_id=bot_id)
    )
    kb.button(text="◀️ Назад", callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(2, 2, 2, 2, 1)
    return kb.as_markup()


def user_profile_menu(
    bot_id: int, user_id: int, is_blocked: bool
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    block_text = "✅ Разблокировать" if is_blocked else "🚫 Заблокировать"
    block_action = "unblock_user" if is_blocked else "block_user"
    kb.button(
        text=block_text,
        callback_data=AudCb(action=block_action, bot_id=bot_id, target_id=user_id),
    )
    kb.button(text="◀️ Назад", callback_data=AudCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def audience_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=AudCb(action="refresh", bot_id=bot_id))
    kb.button(text="⚡ Собрать всех", callback_data=AudCb(action="scan", bot_id=bot_id))
    kb.button(text="📊 Статистика", callback_data=AudCb(action="stats", bot_id=bot_id))
    kb.button(
        text="📤 Написать юзеру", callback_data=AudCb(action="send_user", bot_id=bot_id)
    )
    kb.button(
        text="📤 Экспорт CSV", callback_data=AudCb(action="export", bot_id=bot_id)
    )
    kb.button(
        text="📊 Экспорт Excel", callback_data=AudCb(action="export_xlsx", bot_id=bot_id)
    )
    kb.button(text="⚖️ Сравнить", callback_data=AudCb(action="compare", bot_id=bot_id))
    kb.button(text="◀️ Назад", callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(2, 2, 2, 2, 1, 1)
    return kb.as_markup()


def webhook_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="📋 Получить информацию",
        callback_data=WebhookCb(action="info", bot_id=bot_id),
    )
    kb.button(
        text="🔌 Отключить другие боты",
        callback_data=WebhookCb(action="disable", bot_id=bot_id),
    )
    kb.button(text="◀️ Назад", callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def broadcast_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="📝 Написать рассылку",
        callback_data=BroadcastCb(action="compose", bot_id=bot_id),
    )
    kb.button(
        text="📋 Из шаблона",
        callback_data=BroadcastCb(action="from_template", bot_id=bot_id),
    )
    kb.button(
        text="🎯 По сегменту",
        callback_data=BroadcastCb(action="segment", bot_id=bot_id),
    )
    kb.button(
        text="📋 История рассылок",
        callback_data=BroadcastCb(action="status", bot_id=bot_id),
    )
    kb.button(
        text="📈 Сводка рассылок",
        callback_data=BroadcastCb(action="bc_summary", bot_id=bot_id),
    )
    kb.button(text="◀️ Назад", callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def broadcast_history(bot_id: int, broadcasts: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    status_emoji = {"pending": "⏳", "running": "🔄", "done": "✅", "cancelled": "❌"}
    for bc in broadcasts[:10]:
        emoji = status_emoji.get(bc["status"], "❓")
        date_str = bc["created_at"].strftime("%d.%m %H:%M")
        kb.button(
            text=f"{emoji} #{bc['id']} {date_str} ({bc['sent_count']}/{bc['total_users']})",
            callback_data=BroadcastCb(
                action="detail", bot_id=bot_id, broadcast_id=bc["id"]
            ),
        )
    kb.button(text="◀️ Назад", callback_data=BroadcastCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def broadcast_detail(
    bot_id: int, running_bc_id: int | None = None
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if running_bc_id:
        kb.button(
            text="🔄 Обновить статус",
            callback_data=BroadcastCb(
                action="detail", bot_id=bot_id, broadcast_id=running_bc_id
            ),
        )
    kb.button(
        text="◀️ К истории", callback_data=BroadcastCb(action="status", bot_id=bot_id)
    )
    kb.adjust(1)
    return kb.as_markup()


def broadcast_from_template(bot_id: int, templates: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for t in templates[:8]:
        kb.button(
            text=f"📋 {t['name']}",
            callback_data=BroadcastCb(
                action="use_template", bot_id=bot_id, broadcast_id=t["id"]
            ),
        )
    kb.button(text="◀️ Назад", callback_data=BroadcastCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def broadcast_confirm(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🚀 Запустить", callback_data=BroadcastCb(action="confirm", bot_id=bot_id)
    )
    kb.button(
        text="📩 Тест себе", callback_data=BroadcastCb(action="test", bot_id=bot_id)
    )
    kb.button(
        text="🔗 Добавить кнопку",
        callback_data=BroadcastCb(action="add_button", bot_id=bot_id),
    )
    kb.button(
        text="❌ Отмена", callback_data=BroadcastCb(action="cancel", bot_id=bot_id)
    )
    kb.adjust(2, 1, 1)
    return kb.as_markup()


def broadcast_segment_menu(bot_id: int, languages: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🆕 Новые за 7 дней",
        callback_data=BroadcastCb(
            action="segment_select", bot_id=bot_id, lang="__new7__"
        ),
    )
    kb.button(
        text="🆕 Новые за 30 дней",
        callback_data=BroadcastCb(
            action="segment_select", bot_id=bot_id, lang="__new30__"
        ),
    )
    FLAG_MAP = {
        "ru": "🇷🇺",
        "en": "🇬🇧",
        "uk": "🇺🇦",
        "de": "🇩🇪",
        "fr": "🇫🇷",
        "es": "🇪🇸",
        "it": "🇮🇹",
        "pt": "🇵🇹",
        "pl": "🇵🇱",
        "tr": "🇹🇷",
        "unknown": "🌐",
    }
    for lang_info in languages[:6]:
        lang = lang_info["lang"]
        flag = FLAG_MAP.get(lang, "🌐")
        kb.button(
            text=f"{flag} {lang.upper()} ({lang_info['count']})",
            callback_data=BroadcastCb(
                action="segment_select", bot_id=bot_id, lang=lang
            ),
        )
    kb.button(text="◀️ Назад", callback_data=BroadcastCb(action="menu", bot_id=bot_id))
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()


def commands_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="➕ Добавить команду",
        callback_data=CommandsCb(action="add", bot_id=bot_id),
    )
    kb.button(
        text="📋 Задать весь список",
        callback_data=CommandsCb(action="set_all", bot_id=bot_id),
    )
    kb.button(
        text="🗑 Удалить все команды",
        callback_data=CommandsCb(action="delete", bot_id=bot_id),
    )
    kb.button(text="◀️ Назад", callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def templates_list(templates: list, bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for tpl in templates[:10]:
        kb.button(
            text=f"📝 {tpl['name'][:28]}",
            callback_data=TemplateCb(
                action="view", template_id=tpl["id"], bot_id=bot_id
            ),
        )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(
            text="➕ Новый шаблон",
            callback_data=TemplateCb(action="add", bot_id=bot_id).pack(),
        ),
        InlineKeyboardButton(
            text="✨ AI-текст",
            callback_data=TemplateCb(action="ai_gen", bot_id=bot_id).pack(),
        ),
    )
    if bot_id:
        kb.row(
            InlineKeyboardButton(
                text="◀️ Назад",
                callback_data=BotCb(action="select", bot_id=bot_id).pack(),
            )
        )
    else:
        kb.row(
            InlineKeyboardButton(
                text="◀️ Главное меню",
                callback_data=BotCb(action="main").pack(),
            )
        )
    return kb.as_markup()


def template_actions(template_id: int, bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if bot_id:
        kb.button(
            text="📢 Использовать для рассылки",
            callback_data=TemplateCb(
                action="use", template_id=template_id, bot_id=bot_id
            ),
        )
    kb.button(
        text="🗑 Удалить шаблон",
        callback_data=TemplateCb(
            action="delete", template_id=template_id, bot_id=bot_id
        ),
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
    kb.button(
        text="📋 Из шаблона",
        callback_data=ScheduleCb(action="from_template", bot_id=bot_id),
    )
    if schedules:
        for s in schedules:
            if s["status"] == "pending":
                dt = s["execute_at"].strftime("%d.%m %H:%M")
                preview = s["message_text"][:20].replace("\n", " ")
                kb.button(
                    text=f"❌ Отменить {dt} — {preview}…",
                    callback_data=ScheduleCb(
                        action="cancel", bot_id=bot_id, schedule_id=s["id"]
                    ),
                )
    kb.button(text="◀️ Назад", callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def schedule_template_list(bot_id: int, templates: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for t in templates[:8]:
        kb.button(
            text=f"📋 {t['name']}",
            callback_data=ScheduleCb(
                action="use_template", bot_id=bot_id, schedule_id=t["id"]
            ),
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
            callback_data=AudCb(
                action="pick_b", bot_id=exclude_bot_id, target_id=bot["bot_id"]
            ),
        )
    kb.adjust(1)
    return kb.as_markup()


def confirm_delete(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Да, удалить",
        callback_data=BotCb(action="confirm_delete", bot_id=bot_id),
    )
    kb.button(text="❌ Отмена", callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(2)
    return kb.as_markup()


def back_to_bot(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К боту", callback_data=BotCb(action="select", bot_id=bot_id))
    return kb.as_markup()


def multigeo_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="📝 Мультигео имена",
        callback_data=MultigeoCb(action="names", bot_id=bot_id),
    )
    kb.button(
        text="📋 Мультигео about",
        callback_data=MultigeoCb(action="short", bot_id=bot_id),
    )
    kb.button(
        text="📄 Мультигео description",
        callback_data=MultigeoCb(action="desc", bot_id=bot_id),
    )
    kb.button(text="◀️ Назад", callback_data=EditCb(action="menu", bot_id=bot_id))
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
        trigger = {
            "start": "/start",
            "keyword": f"🔑{r['keyword']}",
            "any": "любое",
        }.get(r["trigger_type"], "?")
        preview = r["response_text"][:20].replace("\n", " ")
        kb.button(
            text=f"{icon} {trigger} → {preview}…",
            callback_data=AutoReplyCb(action="view", bot_id=bot_id, reply_id=r["id"]),
        )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(
            text="➕ Добавить правило",
            callback_data=AutoReplyCb(action="add", bot_id=bot_id).pack(),
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="📋 Копировать в бот",
            callback_data=AutoReplyCb(action="copy_to", bot_id=bot_id).pack(),
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=BotCb(action="select", bot_id=bot_id).pack(),
        )
    )
    return kb.as_markup()


def auto_reply_copy_target(from_bot_id: int, bots: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for b in bots:
        if b["bot_id"] == from_bot_id:
            continue
        label = f"@{b['username']}" if b["username"] else b["first_name"]
        kb.button(
            text=f"🤖 {label}",
            callback_data=AutoReplyCb(
                action="copy_confirm", bot_id=from_bot_id, target_bot_id=b["bot_id"]
            ),
        )
    kb.button(
        text="◀️ Назад", callback_data=AutoReplyCb(action="menu", bot_id=from_bot_id)
    )
    kb.adjust(1)
    return kb.as_markup()


def auto_reply_trigger_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="▶️ /start", callback_data=AutoReplyCb(action="trig_start", bot_id=bot_id)
    )
    kb.button(
        text="🔑 Ключевое слово",
        callback_data=AutoReplyCb(action="trig_keyword", bot_id=bot_id),
    )
    kb.button(
        text="💬 Любое сообщение",
        callback_data=AutoReplyCb(action="trig_any", bot_id=bot_id),
    )
    kb.button(text="◀️ Назад", callback_data=AutoReplyCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def relay_menu(
    bot_id: int, relay_enabled: bool, sessions: list
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    toggle = "🔴 Отключить диалоги" if relay_enabled else "🟢 Включить диалоги"
    kb.button(text=toggle, callback_data=RelayCb(action="toggle", bot_id=bot_id))
    for s in sessions:
        name = (
            f"@{s['username']}"
            if s["username"]
            else (s["first_name"] or str(s["user_id"]))
        )
        preview = ((s["last_text"] or "нет сообщений")[:22]).replace("\n", " ")
        kb.button(
            text=f"💬 {name}: {preview}",
            callback_data=RelayCb(action="session", bot_id=bot_id, session_id=s["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def relay_session_view(
    bot_id: int, session_id: int, templates: list = None
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    # Quick reply template buttons
    if templates:
        for t in templates[:5]:  # max 5 шаблонов
            kb.button(
                text=f"💬 {t['name']}",
                callback_data=RelayCb(
                    action="quick_reply",
                    bot_id=bot_id,
                    session_id=session_id,
                    template_id=t["id"],
                ),
            )
    kb.button(
        text="🗑 Закрыть диалог",
        callback_data=RelayCb(
            action="close_session", bot_id=bot_id, session_id=session_id
        ),
    )
    kb.button(text="◀️ К Inbox", callback_data=RelayCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def auto_reply_view(
    bot_id: int, reply_id: int, is_active: bool
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    toggle_text = "❌ Отключить" if is_active else "✅ Включить"
    kb.button(
        text=toggle_text,
        callback_data=AutoReplyCb(action="toggle", bot_id=bot_id, reply_id=reply_id),
    )
    kb.button(
        text="🗑 Удалить",
        callback_data=AutoReplyCb(action="delete", bot_id=bot_id, reply_id=reply_id),
    )
    kb.button(text="◀️ Назад", callback_data=AutoReplyCb(action="menu", bot_id=bot_id))
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
    kb.row(
        InlineKeyboardButton(
            text="➕ Создать",
            callback_data=FunnelCb(action="create", bot_id=bot_id).pack(),
        ),
        InlineKeyboardButton(
            text="📋 Скопировать из другого",
            callback_data=FunnelCb(action="copy_from", bot_id=bot_id).pack(),
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=BotCb(action="select", bot_id=bot_id).pack(),
        )
    )
    return kb.as_markup()


def funnel_copy_target(bot_id: int, bots: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for b in bots[:8]:
        label = f"@{b['username']}" if b["username"] else b["first_name"]
        kb.button(
            text=f"🤖 {label}",
            callback_data=FunnelCb(
                action="copy_confirm", bot_id=bot_id, target_bot_id=b["bot_id"]
            ),
        )
    kb.button(text="◀️ Назад", callback_data=FunnelCb(action="list", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def funnel_view(
    bot_id: int, funnel_id: int, is_active: bool, step_count: int
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    toggle = "❌ Отключить" if is_active else "✅ Включить"
    kb.button(
        text=toggle,
        callback_data=FunnelCb(action="toggle", bot_id=bot_id, funnel_id=funnel_id),
    )
    kb.button(
        text="➕ Добавить шаг",
        callback_data=FunnelCb(
            action="add_step", bot_id=bot_id, funnel_id=funnel_id, step=step_count
        ),
    )
    kb.button(
        text="📢 Написать подписчикам",
        callback_data=FunnelCb(action="broadcast", bot_id=bot_id, funnel_id=funnel_id),
    )
    kb.button(
        text="🗑 Удалить",
        callback_data=FunnelCb(action="delete", bot_id=bot_id, funnel_id=funnel_id),
    )
    kb.button(text="◀️ К цепочкам", callback_data=FunnelCb(action="list", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def funnel_trigger_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="▶️ /start", callback_data=FunnelCb(action="trig_start", bot_id=bot_id)
    )
    kb.button(
        text="🔑 Ключевое слово",
        callback_data=FunnelCb(action="trig_keyword", bot_id=bot_id),
    )
    kb.button(text="◀️ Отмена", callback_data=FunnelCb(action="list", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def swarm_menu(bot_id: int, row) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    swarm_on = row.get("swarm_enabled", False)
    toggle_text = "🟢 Отключить Swarm" if swarm_on else "⚫ Включить Swarm"
    kb.button(text=toggle_text, callback_data=SwarmCb(action="toggle", bot_id=bot_id))
    role = row.get("bot_role", "general")
    for r, label in [
        ("entry", "🚪 Entry"),
        ("conversion", "💰 Conversion"),
        ("retention", "🔄 Retention"),
        ("general", "⚙️ General"),
    ]:
        prefix = "✅ " if role == r else ""
        kb.button(
            text=f"{prefix}{label}",
            callback_data=SwarmCb(action=f"role_{r}", bot_id=bot_id),
        )
    kb.button(
        text="📊 Routing Stats", callback_data=SwarmCb(action="stats", bot_id=bot_id)
    )
    kb.button(
        text="🌐 Системный режим",
        callback_data=SwarmCb(action="set_mode", bot_id=bot_id),
    )
    kb.button(text="◀️ Назад", callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(1, 2, 2, 2, 1)
    return kb.as_markup()


def crm_menu(bot_id: int, tags: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for t in tags[:8]:
        kb.button(
            text=f"🏷 {t['tag']} ({t['count']})",
            callback_data=CrmCb(action="tag_detail", bot_id=bot_id, tag=t["tag"]),
        )
    kb.button(
        text="➕ Новый тег", callback_data=CrmCb(action="add_tag_global", bot_id=bot_id)
    )
    kb.button(
        text="🤖 Автоматизация", callback_data=AutoCb(action="menu", bot_id=bot_id)
    )
    kb.button(text="◀️ Назад", callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def tag_detail_menu(bot_id: int, tag: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="📢 Рассылка этому сегменту",
        callback_data=BroadcastCb(
            action="segment_select", bot_id=bot_id, lang=f"__tag__{tag}"
        ),
    )
    kb.button(
        text="🗑 Удалить тег у всех",
        callback_data=CrmCb(action="delete_tag_all", bot_id=bot_id, tag=tag),
    )
    kb.button(text="◀️ Назад", callback_data=CrmCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def automation_menu(bot_id: int, rules: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for r in rules[:8]:
        icon = "✅" if r["is_active"] else "❌"
        kb.button(
            text=f"{icon} {r['name']} [{r['trigger_type']}→{r['action_type']}]",
            callback_data=AutoCb(action="view", bot_id=bot_id, rule_id=r["id"]),
        )
    kb.button(
        text="➕ Новое правило", callback_data=AutoCb(action="add", bot_id=bot_id)
    )
    kb.button(text="◀️ Назад", callback_data=CrmCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def automation_trigger_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="📩 Сообщение получено",
        callback_data=AutoCb(action="trig_message", bot_id=bot_id),
    )
    kb.button(
        text="👤 Новый пользователь",
        callback_data=AutoCb(action="trig_joined", bot_id=bot_id),
    )
    kb.button(
        text="🔑 Ключевое слово",
        callback_data=AutoCb(action="trig_keyword", bot_id=bot_id),
    )
    kb.button(
        text="🏷 Тег добавлен", callback_data=AutoCb(action="trig_tag", bot_id=bot_id)
    )
    kb.button(text="◀️ Отмена", callback_data=AutoCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def automation_action_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="💬 Отправить сообщение",
        callback_data=AutoCb(action="act_send", bot_id=bot_id),
    )
    kb.button(
        text="🏷 Добавить тег", callback_data=AutoCb(action="act_add_tag", bot_id=bot_id)
    )
    kb.button(
        text="🗑 Удалить тег",
        callback_data=AutoCb(action="act_remove_tag", bot_id=bot_id),
    )
    kb.button(
        text="🔗 Подписать на цепочку",
        callback_data=AutoCb(action="act_funnel", bot_id=bot_id),
    )
    kb.button(text="◀️ Отмена", callback_data=AutoCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def automation_funnel_select(bot_id: int, funnels: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for f in funnels[:8]:
        kb.button(
            text=f["name"],
            callback_data=AutoCb(action="sel_funnel", bot_id=bot_id, rule_id=f["id"]),
        )
    kb.button(text="◀️ Отмена", callback_data=AutoCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def experiments_menu(bot_id: int, experiments: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    status_emoji = {"draft": "📝", "active": "🟢", "paused": "⏸", "completed": "✅"}
    for e in experiments[:8]:
        emoji = status_emoji.get(e["status"], "❓")
        kb.button(
            text=f"{emoji} {e['name']} [{e['experiment_type']}]",
            callback_data=ExperimentCb(action="view", bot_id=bot_id, exp_id=e["id"]),
        )
    kb.button(
        text="➕ Новый эксперимент",
        callback_data=ExperimentCb(action="create", bot_id=bot_id),
    )
    kb.button(text="◀️ Назад", callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def experiment_view_menu(bot_id: int, exp_id: int, status: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if status == "draft":
        kb.button(
            text="➕ Добавить вариант",
            callback_data=ExperimentCb(
                action="add_variant", bot_id=bot_id, exp_id=exp_id
            ),
        )
        kb.button(
            text="▶️ Запустить",
            callback_data=ExperimentCb(action="start", bot_id=bot_id, exp_id=exp_id),
        )
    elif status == "active":
        kb.button(
            text="⏸ Пауза",
            callback_data=ExperimentCb(action="pause", bot_id=bot_id, exp_id=exp_id),
        )
        kb.button(
            text="🏆 Выбрать победителя вручную",
            callback_data=ExperimentCb(
                action="pick_winner", bot_id=bot_id, exp_id=exp_id
            ),
        )
    elif status == "paused":
        kb.button(
            text="▶️ Возобновить",
            callback_data=ExperimentCb(action="resume", bot_id=bot_id, exp_id=exp_id),
        )
    kb.button(
        text="🗑 Удалить",
        callback_data=ExperimentCb(action="delete", bot_id=bot_id, exp_id=exp_id),
    )
    kb.button(
        text="◀️ К списку", callback_data=ExperimentCb(action="list", bot_id=bot_id)
    )
    kb.adjust(1)
    return kb.as_markup()


def experiment_type_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="▶️ /start сообщение",
        callback_data=ExperimentCb(action="type_start", bot_id=bot_id),
    )
    kb.button(
        text="💬 Авто-ответ",
        callback_data=ExperimentCb(action="type_reply", bot_id=bot_id),
    )
    kb.button(text="◀️ Отмена", callback_data=ExperimentCb(action="list", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def variant_pick_menu(bot_id: int, exp_id: int, variants: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for v in variants:
        ctr = (
            round(v["conversions"] / v["impressions"] * 100, 1)
            if v["impressions"]
            else 0
        )
        kb.button(
            text=f"🏆 {v['name']} (CTR: {ctr}%)",
            callback_data=ExperimentCb(
                action="set_winner", bot_id=bot_id, exp_id=exp_id, variant_id=v["id"]
            ),
        )
    kb.button(
        text="◀️ Назад",
        callback_data=ExperimentCb(action="view", bot_id=bot_id, exp_id=exp_id),
    )
    kb.adjust(1)
    return kb.as_markup()


def deeplinks_menu(bot_id: int, links: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for lnk in links[:10]:
        kb.button(
            text=f"🔗 {lnk['name']} — {lnk['click_count']} кликов ({lnk['unique_users']} уник.)",
            callback_data=DeepLinkCb(action="view", bot_id=bot_id, link_id=lnk["id"]),
        )
    kb.button(
        text="➕ Создать диплинк",
        callback_data=DeepLinkCb(action="create", bot_id=bot_id),
    )
    kb.button(
        text="🏆 Рефeral лидерборд",
        callback_data=DeepLinkCb(action="leaders", bot_id=bot_id),
    )
    kb.button(text="◀️ Назад", callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def deeplink_view_menu(bot_id: int, link_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🗑 Удалить",
        callback_data=DeepLinkCb(action="delete", bot_id=bot_id, link_id=link_id),
    )
    kb.button(text="◀️ Назад", callback_data=DeepLinkCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def engagement_menu(bot_id: int, segs: dict) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    hot = segs.get("hot", 0)
    warm = segs.get("warm", 0)
    cold = segs.get("cold", 0)
    lost = segs.get("lost", 0)
    kb.button(
        text=f"🔥 Горячие ({hot}) — < 24ч",
        callback_data=EngageCb(action="segment_hot", bot_id=bot_id),
    )
    kb.button(
        text=f"🌡 Тёплые ({warm}) — 1–7 дн",
        callback_data=EngageCb(action="segment_warm", bot_id=bot_id),
    )
    kb.button(
        text=f"❄️ Холодные ({cold}) — 7–30 дн → реактивировать",
        callback_data=EngageCb(action="reactivate_cold", bot_id=bot_id),
    )
    kb.button(
        text=f"💀 Потерянные ({lost}) — 30+ дн → реактивировать",
        callback_data=EngageCb(action="reactivate_lost", bot_id=bot_id),
    )
    kb.button(
        text="📊 Тепловая карта по часам",
        callback_data=EngageCb(action="heatmap", bot_id=bot_id),
    )
    kb.button(
        text="🏆 Топ-10 активных юзеров",
        callback_data=EngageCb(action="top_users", bot_id=bot_id),
    )
    kb.button(
        text="🏷 Авто-теги (hot/warm/cold/lost)",
        callback_data=EngageCb(action="autotag", bot_id=bot_id),
    )
    kb.button(text="◀️ Назад", callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def seo_menu(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="📊 SEO-скор профиля (0–100)",
        callback_data=SeoCb(action="analyze", bot_id=bot_id),
    )
    kb.button(
        text="🔑 Ключевые слова юзеров",
        callback_data=SeoCb(action="keywords", bot_id=bot_id),
    )
    kb.button(
        text="💡 SEO-советы для Telegram",
        callback_data=SeoCb(action="tips", bot_id=bot_id),
    )
    kb.button(text="◀️ Назад", callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


# ── Network Management Keyboards ─────────────────────────────────────────────


def network_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Аналитика сети", callback_data=NetworkCb(action="analytics"))
    kb.button(text="🌐 Кластеры", callback_data=NetworkCb(action="clusters"))
    kb.button(text="🏆 Рейтинг ботов", callback_data=NetworkCb(action="ranking"))
    kb.button(text="⚖️ Веса роутинга", callback_data=NetworkCb(action="routing"))
    kb.button(text="❤️ Здоровье сети", callback_data=NetworkCb(action="health"))
    kb.button(text="📢 Сетевая рассылка", callback_data=NetworkCb(action="broadcast"))
    kb.button(text="🔄 Клонировать настройки", callback_data=NetworkCb(action="clone"))
    kb.button(
        text="👥 Пересечение аудиторий", callback_data=NetworkCb(action="overlap")
    )
    kb.button(text="◀️ Главное меню", callback_data=BotCb(action="main"))
    kb.adjust(2, 2, 2, 2, 1)
    return kb.as_markup()


def network_clusters_menu(clusters: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c in clusters:
        kb.button(
            text=f"🌐 {c['cluster']} — {c['bot_count']} бот. · {c['total_audience']:,} юз.",
            callback_data=ClusterCb(action="view", cluster=c["cluster"]),
        )
    kb.button(text="◀️ Назад", callback_data=NetworkCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()


def network_cluster_view(cluster: str, bots: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🟢 Swarm ON для всех",
        callback_data=ClusterCb(action="bulk_swarm_on", cluster=cluster),
    )
    kb.button(
        text="⚫ Swarm OFF для всех",
        callback_data=ClusterCb(action="bulk_swarm_off", cluster=cluster),
    )
    kb.button(
        text="🚪 Роль: Entry всем",
        callback_data=ClusterCb(action="bulk_role_entry", cluster=cluster),
    )
    kb.button(
        text="💰 Роль: Conversion всем",
        callback_data=ClusterCb(action="bulk_role_conversion", cluster=cluster),
    )
    kb.button(
        text="🔄 Роль: Retention всем",
        callback_data=ClusterCb(action="bulk_role_retention", cluster=cluster),
    )
    kb.button(
        text="➕ Назначить бота в кластер",
        callback_data=ClusterCb(action="assign_start", cluster=cluster),
    )
    kb.button(text="◀️ К кластерам", callback_data=NetworkCb(action="clusters"))
    kb.adjust(2, 3, 1, 1)
    return kb.as_markup()


def network_assign_bot_pick(cluster: str, bots: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for b in bots[:12]:
        label = f"@{b['username']}" if b["username"] else b["first_name"]
        current = f" [{b.get('cluster') or 'default'}]"
        kb.button(
            text=f"🤖 {label}{current}",
            callback_data=ClusterCb(
                action="assign_confirm", cluster=cluster, bot_id=b["bot_id"]
            ),
        )
    kb.button(text="◀️ Назад", callback_data=ClusterCb(action="view", cluster=cluster))
    kb.adjust(1)
    return kb.as_markup()


def network_routing_menu(weights: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for w in weights[:10]:
        label = f"@{w['username']}" if w["username"] else w["first_name"]
        kb.button(
            text=f"⚖️ {label} — вес: {w['weight']:.1f}",
            callback_data=NetworkCb(action="set_weight_pick", bot_id=w["bot_id"]),
        )
    kb.button(
        text="🔄 Сбросить все веса (равные)",
        callback_data=NetworkCb(action="reset_weights"),
    )
    kb.button(text="◀️ Назад", callback_data=NetworkCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()


def network_clone_pick_source(bots: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for b in bots[:12]:
        label = f"@{b['username']}" if b["username"] else b["first_name"]
        kb.button(
            text=f"📤 {label} (источник)",
            callback_data=NetworkCb(action="clone_pick_dest", bot_id=b["bot_id"]),
        )
    kb.button(text="◀️ Назад", callback_data=NetworkCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()


def network_clone_pick_dest(src_id: int, bots: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for b in bots[:12]:
        if b["bot_id"] == src_id:
            continue
        label = f"@{b['username']}" if b["username"] else b["first_name"]
        kb.button(
            text=f"📥 {label} (цель)",
            callback_data=NetworkCb(action="clone_confirm", bot_id=b["bot_id"]),
        )
    kb.button(text="◀️ Назад", callback_data=NetworkCb(action="clone"))
    kb.adjust(1)
    return kb.as_markup()


def network_broadcast_confirm() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🚀 Запустить рассылку",
        callback_data=NetworkCb(action="broadcast_confirm"),
    )
    kb.button(text="❌ Отмена", callback_data=NetworkCb(action="broadcast_cancel"))
    kb.adjust(2)
    return kb.as_markup()
