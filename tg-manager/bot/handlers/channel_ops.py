"""Channel & Account Operations handler.

Provides full Telegram account management via connected Telethon sessions:
  - Create channels/groups (single + bulk across all accounts)
  - Join / leave channels
  - Post content, send reactions
  - Edit channel settings (title, about, username, invite link, delete)
  - Manage members (view, invite, kick)
  - Edit account profile (name, bio, username)
  - Create bots via @BotFather automated dialog
  - Report content

Subscription gates:
  STARTER: join/leave, post, reactions, profile, report
  PRO:     create channel, member management, bulk, BotFather
"""

from __future__ import annotations

import asyncio
import html
import logging
import random
import re
import time
import aiohttp
import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ChanCb, ContactInvCb, BmCb, SubCb, AccCb
from services import task_registry as _treg
from bot.states import (
    BulkChanFSM,
    BulkCreateFSM,
    BulkDmFSM,
    BulkPostChansFSM,
    BulkReportFSM,
    ContactInviteFSM,
    CreateBotFSM,
    CreateChannelFSM,
    EditChannelFSM,
    InviteUsersFSM,
    JoinChannelFSM,
    MyChannelsFSM,
    PostToChannelFSM,
    ReportFSM,
    SendReactionFSM,
    UpdateProfileFSM,
)
from bot.utils.subscription import require_plan
from bot.utils.op_helpers import _acc_label, _progress_text, backoff
from services import session_simulator
from services.logger import log_exc_swallow
from bot.utils.event_status import mark_handled_error

# ── Bulk Pacing Presets ──────────────────────────────────────────────────────

_BULK_PACING = {
    "safe": {
        "label": "🐢 Безопасный",
        "item_delay": (90, 150),  # seconds between items
        "cooldown_every": 5,  # long pause every N items
        "cooldown_delay": (300, 600),  # long pause range
        "desc": "~5-10 мин между группами по 5",
    },
    "medium": {
        "label": "🐇 Средний",
        "item_delay": (45, 90),
        "cooldown_every": 5,
        "cooldown_delay": (120, 300),
        "desc": "~2-5 мин между группами по 5",
    },
    "fast": {
        "label": "🚀 Быстрый",
        "item_delay": (30, 60),
        "cooldown_every": 5,
        "cooldown_delay": (120, 300),
        "desc": "~2-3 мин между группами по 5",
    },
    "turbo": {
        "label": "⚡ Турбо",
        "item_delay": (45, 90),
        "cooldown_every": 3,
        "cooldown_delay": (180, 360),
        "desc": "⚠️ Умеренный риск. Каждые 3 — большая пауза",
    },
}
_DEFAULT_PACING = "medium"


def _estimate_duration(total_items: int, pacing_key: str) -> str:
    """Estimate total duration for a bulk operation."""
    preset = _BULK_PACING.get(pacing_key, _BULK_PACING[_DEFAULT_PACING])
    avg_item = sum(preset["item_delay"]) / 2
    avg_cooldown = sum(preset["cooldown_delay"]) / 2
    cooldown_every = preset["cooldown_every"]
    num_cooldowns = max(0, (total_items - 1) // cooldown_every)
    total_secs = total_items * avg_item + num_cooldowns * avg_cooldown
    mins = total_secs / 60
    if mins < 1:
        return f"~{int(total_secs)} сек"
    if mins < 60:
        return f"~{int(mins)} мин"
    hours = mins / 60
    return f"~{hours:.1f} ч"


def _pacing_kb(current: str) -> InlineKeyboardBuilder:
    """Build pacing selection keyboard."""
    kb = InlineKeyboardBuilder()
    for key, preset in _BULK_PACING.items():
        prefix = "✅ " if key == current else ""
        kb.button(
            text=f"{prefix}{preset['label']}",
            callback_data=ChanCb(action=f"bulk_pacing_{key}"),
        )
    kb.adjust(2, 2)
    return kb


from database import db

log = logging.getLogger(__name__)
router = Router()


def _friendly_join_error(error_str: str) -> str:
    """Translate raw Telethon/Telegram join errors into user-friendly Russian messages."""
    e = error_str.lower()
    if "userbannedinchannels" in e or "you're banned" in e or "banned in channel" in e:
        return (
            "🚫 <b>Аккаунт заблокирован в этом канале/группе</b>\n\n"
            "Администраторы канала запретили вступление для этого аккаунта. "
            "Попробуйте другой аккаунт."
        )
    if (
        "channelprivate" in e
        or "channel_private" in e
        or "private" in e
        and "channel" in e
    ):
        return (
            "🔒 <b>Закрытый канал/группа</b>\n\n"
            "Этот канал или группа закрыты. Для вступления нужна пригласительная ссылка "
            "<code>https://t.me/+...</code>, а не просто @username."
        )
    if "floodwait" in e or "flood_wait" in e or "flood wait" in e or "a wait of" in e:
        import re as _re

        m = _re.search(r"(\d+)", error_str)
        seconds = int(m.group(1)) if m else 60
        minutes = seconds // 60
        time_str = f"{minutes} мин" if minutes > 0 else f"{seconds} сек"
        return (
            f"⏳ <b>Telegram требует паузу {time_str}</b>\n\n"
            "Слишком много запросов. Подождите немного перед следующей попыткой."
        )
    if "invitehash" in e or "invite_hash" in e or "invalid" in e and "hash" in e:
        return (
            "❌ <b>Недействительная ссылка-приглашение</b>\n\n"
            "Ссылка устарела или была отозвана. Запросите новую ссылку у администратора канала."
        )
    if "usernotmutualcontact" in e or "not mutual" in e:
        return (
            "❌ <b>Доступ ограничен</b>\n\n"
            "Для вступления в эту группу нужно быть в контактах её участника."
        )
    if "channelstoomuchchat" in e or "too much" in e:
        return (
            "❌ <b>Слишком много чатов</b>\n\n"
            "Аккаунт уже состоит в максимально допустимом количестве каналов/групп. "
            "Выйдите из нескольких каналов и попробуйте снова."
        )
    if "usernamenotoccupied" in e or "username not occupied" in e:
        return (
            "❌ <b>Канал не найден</b>\n\n"
            "Канал или группа с таким @username не существует. "
            "Проверьте правильность написания."
        )
    # Generic fallback — show technical error but clean
    return f"❌ <b>Ошибка вступления</b>\n\n<code>{html.escape(error_str[:200])}</code>"


_DISCLAIMER = (
    "\n\n<i>⚠️ <b>Важно:</b> Strike Module является инструментом для подачи "
    "законных жалоб через официальные механизмы Telegram Trust &amp; Safety. "
    "Результат зависит исключительно от решения модераторов Telegram. "
    "Использование модуля не гарантирует удаление или блокировку ресурса.</i>"
)

# Store active background tasks for cancellation: (user_id, task_type) → Task
_active_tasks: dict[tuple[int, str], asyncio.Task] = {}

_STARTER = "paid"
_PRO = "paid"

REPORT_REASONS = {
    "spam": "🚫 Спам",
    "violence": "⚠️ Насилие",
    "pornography": "🔞 Контент 18+",
    "childabuse": "🚨 Детский материал",
    "copyright": "©️ Нарушение авторских прав",
    "other": "📋 Другое",
}

# Быстрые пресеты для типовых незаконных ресурсов
_REPORT_PRESETS = {
    "drugs": ("other", "🟣 Наркотики"),
    "terrorism": ("violence", "💣 Терроризм"),
    "fraud": ("spam", "💸 Мошенничество"),
    "csam": ("childabuse", "🚨 CSAM"),
    "weapons": ("violence", "🔫 Оружие"),
    "darknet": ("other", "🕸 Даркнет-услуги"),
    "escort": ("pornography", "🟤 Эскорт/проституция"),
}


# Многоязычные тексты жалоб — каждая отправка использует следующий вариант по кругу
_REPORT_MESSAGES: dict[str, list[str]] = {
    "spam": [
        "This channel is distributing illegal drugs and controlled substances.",
        "Этот канал распространяет наркотики и запрещённые вещества.",
        "Dieser Kanal vertreibt illegale Drogen und Betäubungsmittel.",
        "Ce canal distribue des drogues illégales et des substances contrôlées.",
        "Este canal distribuye drogas ilegales y sustancias controladas.",
        "Цей канал поширює наркотики та заборонені речовини.",
        "Questo canale distribuisce droghe illegali e sostanze controllate.",
        "Este canal distribui drogas ilegais e substâncias controladas.",
        "Bu kanal yasadışı uyuşturucu ve kontrollü madde dağıtıyor.",
        "このチャンネルは違法薬物を販売しています。",
    ],
    "violence": [
        "This channel promotes terrorism and incites violence against civilians.",
        "Этот канал пропагандирует терроризм и призывает к насилию.",
        "Dieser Kanal fördert Terrorismus und Gewalt gegen Zivilisten.",
        "Cette chaîne promeut le terrorisme et incite à la violence.",
        "Este canal promueve el terrorismo y la violencia contra civiles.",
        "Цей канал пропагує тероризм та насильство проти мирних жителів.",
        "Questo canale promuove il terrorismo e la violenza.",
        "Este canal promove terrorismo e incita à violência.",
        "Bu kanal terörizmi ve şiddeti teşvik ediyor.",
        "このチャンネルはテロリズムと暴力を促進しています。",
    ],
    "pornography": [
        "This channel distributes illegal adult content without age verification.",
        "Этот канал распространяет незаконный контент для взрослых.",
        "Dieser Kanal verbreitet illegale pornografische Inhalte.",
        "Ce canal diffuse du contenu pornographique illégal.",
        "Este canal distribuye contenido pornográfico ilegal.",
        "Цей канал поширює незаконний контент для дорослих.",
        "Questo canale distribuisce contenuto pornografico illegale.",
        "Este canal distribui conteúdo pornográfico ilegal.",
        "Bu kanal yasadışı pornografik içerik yayıyor.",
        "このチャンネルは違法なアダルトコンテンツを配布しています。",
    ],
    "childabuse": [
        "Child sexual abuse material detected. Immediate action required.",
        "Обнаружены материалы сексуального насилия над детьми. Требуется немедленная реакция.",
        "Kindesmissbrauchsmaterial entdeckt. Sofortiges Handeln erforderlich.",
        "Matériel d'abus sexuel sur enfants. Action immédiate requise.",
        "Material de abuso sexual infantil. Se requiere acción inmediata.",
        "Виявлено матеріали сексуального насилля над дітьми. Потрібне негайне реагування.",
        "Rilevato materiale di abuso sessuale su minori. Azione immediata richiesta.",
        "Material de abuso sexual infantil detectado. Ação imediata necessária.",
        "Çocuk cinsel istismar materyali tespit edildi. Acil müdahale gerekli.",
        "児童性的虐待素材が検出されました。即座の対応が必要です。",
    ],
    "copyright": [
        "This channel systematically violates copyright and distributes pirated content.",
        "Этот канал систематически нарушает авторские права и распространяет пиратский контент.",
        "Dieser Kanal verletzt systematisch Urheberrechte.",
        "Ce canal viole systématiquement les droits d'auteur.",
        "Este canal viola sistemáticamente los derechos de autor.",
        "Цей канал систематично порушує авторські права.",
        "Questo canale viola sistematicamente i diritti d'autore.",
        "Este canal viola sistematicamente direitos autorais.",
        "Bu kanal sistematik olarak telif haklarını ihlal ediyor.",
        "このチャンネルは著作権を組織的に侵害しています。",
    ],
    "other": [
        "This channel offers illegal darknet services and prohibited goods.",
        "Этот канал предлагает незаконные даркнет-услуги и запрещённые товары.",
        "Dieser Kanal bietet illegale Darknet-Dienste und verbotene Waren an.",
        "Ce canal propose des services darknet illégaux et des produits interdits.",
        "Este canal ofrece servicios darknet ilegales y bienes prohibidos.",
        "Цей канал пропонує незаконні даркнет-послуги та заборонені товари.",
        "Questo canale offre servizi darknet illegali e merci vietate.",
        "Este canal oferece serviços darknet ilegais e bens proibidos.",
        "Bu kanal yasadışı darknet hizmetleri ve yasaklı ürünler sunuyor.",
        "このチャンネルは違法なダークネットサービスを提供しています。",
    ],
}

REACTION_EMOJIS = ["👍", "❤️", "🔥", "🎉", "😮", "😢", "👎", "💯", "🤔", "🤩"]


def _parse_tme_post_link(text: str) -> tuple[int | str | None, int | None]:
    """Parse a t.me post link → (channel_ref, msg_id).

    Supports:
      https://t.me/channelname/123      → ("channelname", 123)
      https://t.me/c/1234567890/123     → (-1001234567890, 123)  private channel
      t.me/channelname/123              → same without https
    Returns (None, None) if input is not a valid link.
    """
    t = text.strip()
    # Private channel: t.me/c/<channel_id>/<msg_id>
    m = re.match(r"(?:https?://)?t\.me/c/(\d+)/(\d+)", t)
    if m:
        return int(f"-100{m.group(1)}"), int(m.group(2))
    # Public channel: t.me/<username>/<msg_id>
    m = re.match(r"(?:https?://)?t\.me/([a-zA-Z0-9_]+)/(\d+)", t)
    if m:
        return m.group(1), int(m.group(2))
    return None, None


def _human_delay(min_s: float, max_s: float) -> float:
    """Return a random human-like delay between min_s and max_s seconds."""
    import random

    return random.uniform(min_s, max_s)


# ── Helpers ────────────────────────────────────────────────────────────────


async def _get_accounts(pool: asyncpg.Pool, owner_id: int) -> list[asyncpg.Record]:
    try:
        return await pool.fetch(
            "SELECT a.id, a.session_str, a.phone, a.first_name, a.username, a.is_active, "
            "a.trust_score, a.flood_count_7d, a.cooldown_until, a.tg_user_id, "
            "a.device_model, a.system_version, a.app_version, "
            "a.lang_code, a.system_lang_code, a.proxy_id, "
            "p.proxy_url, p.geo_country "
            "FROM tg_accounts a "
            "LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
            "WHERE a.owner_id=$1 ORDER BY a.added_at",
            owner_id,
        )
    except Exception as e:
        log.warning("_get_accounts error: %s", e)
        return []


def _back_kb(acc_id: int = 0) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu", acc_id=acc_id))
    return kb


def _main_menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    # ── Каналы и группы
    kb.button(text="📢 Создать канал", callback_data=ChanCb(action="create_channel"))
    kb.button(text="👥 Создать группу", callback_data=ChanCb(action="create_group"))
    kb.button(text="🔗 Вступить", callback_data=ChanCb(action="join"))
    kb.button(text="🚪 Выйти", callback_data=ChanCb(action="leave_pick"))
    # ── Управление
    kb.button(text="✏️ Управление каналом", callback_data=ChanCb(action="manage_pick"))
    kb.button(text="📋 Мои каналы/чаты", callback_data=ChanCb(action="my_chans"))
    # ── Публикация
    kb.button(text="📤 Опубликовать пост", callback_data=ChanCb(action="post_pick"))
    kb.button(text="👥 Участники", callback_data=ChanCb(action="members_pick"))
    # ── Аккаунт
    kb.button(text="🙋 Профиль аккаунта", callback_data=ChanCb(action="profile_pick"))
    kb.button(text="🤖 Создать бота", callback_data=ChanCb(action="botfather_pick"))
    # ── Прочее
    kb.button(
        text="👥 Инвайт из контактов", callback_data=ChanCb(action="contact_invite")
    )
    kb.button(text="👍 Реакция на пост", callback_data=ChanCb(action="react_pick"))
    kb.button(text="🚨 Жалоба (1 акк)", callback_data=ChanCb(action="report_pick"))
    # ── Нижний ряд
    kb.button(text="⚡ Массовые операции", callback_data=ChanCb(action="bulk_menu"))
    kb.adjust(2, 2, 2, 2, 2, 2, 1)
    return kb


def _bulk_menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    # ── Создание
    kb.button(
        text="📢 Создать канал/группу (bulk)",
        callback_data=ChanCb(action="bulk_create"),
    )
    # ── Вступление / выход
    kb.button(
        text="🔗 Вступить в каналы (список)", callback_data=ChanCb(action="bulk_join")
    )
    kb.button(
        text="🚪 Выйти из каналов (список)", callback_data=ChanCb(action="bulk_leave")
    )
    # ── Публикация
    kb.button(
        text="📢 Пост во все каналы аккаунта",
        callback_data=ChanCb(action="bulk_post_chans"),
    )
    kb.button(
        text="📤 Пост с нескольких аккаунтов", callback_data=ChanCb(action="bulk_post")
    )
    # ── Рассылка
    kb.button(text="✉️ DM по username-списку", callback_data=ChanCb(action="bulk_dm"))
    # ── Массовое управление каналами
    kb.button(
        text="🔤 Username каналам (bulk)",
        callback_data=ChanCb(action="bulk_chan_uname"),
    )
    kb.button(
        text="📄 Описание каналам (bulk)",
        callback_data=ChanCb(action="bulk_chan_about"),
    )
    # ── Профиль аккаунтов
    kb.button(
        text="✏️ Имя аккаунта (bulk)", callback_data=ChanCb(action="bulk_prof_name")
    )
    kb.button(
        text="📝 Bio аккаунта (bulk)", callback_data=ChanCb(action="bulk_prof_bio")
    )
    kb.button(
        text="🔤 Username аккаунта (bulk)",
        callback_data=ChanCb(action="bulk_prof_uname"),
    )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    return kb


def _make_title(base: str, mode: str, global_idx: int, acc_label: str) -> str:
    if mode == "num":
        return f"{base} {global_idx}"
    if mode == "acc":
        return f"{base} ({acc_label[:20]})"
    return base


# OP label map for display
_BULK_OP_LABELS = {
    "create": "📢 Создать канал/группу",
    "botfather": "🤖 Создать бота через @BotFather",
    "dm": "✉️ Рассылка по username-списку",
    "join": "🔗 Вступить в канал",
    "leave": "🚪 Выйти из канала",
    "post": "📤 Опубликовать пост",
    "prof_name": "✏️ Изменить имя",
    "prof_bio": "📝 Изменить bio",
    "prof_uname": "🔤 Изменить username",
    "chan_uname": "🔤 Username каналам (bulk)",
    "chan_about": "📄 Описание каналам (bulk)",
}


def _bulk_select_kb(
    accounts: list, selected: set[int], op: str
) -> InlineKeyboardBuilder:
    """Account selection keyboard with toggles."""
    kb = InlineKeyboardBuilder()
    for acc in accounts:
        icon = "✅" if acc["id"] in selected else "☐"
        label = f"{icon} {_acc_label(acc)}"
        kb.button(text=label, callback_data=f"chan:bsel:{op}:{acc['id']}")
    n = len(selected)
    kb.button(text="✅ Выбрать все", callback_data=f"chan:bsall:{op}")
    kb.button(text="☐ Снять все", callback_data=f"chan:bsnone:{op}")
    if n > 0:
        kb.button(
            text=f"▶️ Продолжить с {n} аккаунт{'ом' if n == 1 else 'ами'}",
            callback_data=f"chan:bsdone:{op}",
        )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="bulk_menu").pack())
    kb.adjust(1)
    return kb


def _account_picker_kb(accounts: list, action: str) -> InlineKeyboardBuilder:
    """Inline keyboard to pick one account for an action."""
    kb = InlineKeyboardBuilder()
    for acc in accounts:
        label = ("✅ " if acc["is_active"] else "❌ ") + _acc_label(acc)
        kb.button(text=label, callback_data=ChanCb(action=action, acc_id=acc["id"]))
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    return kb


async def _send_or_edit(msg_or_cb, text: str, kb, edit: bool = True) -> None:
    markup = kb.as_markup() if hasattr(kb, "as_markup") else kb
    if edit and hasattr(msg_or_cb, "message"):
        try:
            await msg_or_cb.message.edit_text(
                text, parse_mode="HTML", reply_markup=markup
            )
            return
        except Exception:
            log_exc_swallow(
                log, "Сбой edit_text в _send_or_edit — отправляю новое сообщение"
            )
        await msg_or_cb.message.answer(text, parse_mode="HTML", reply_markup=markup)
    else:
        target = msg_or_cb if hasattr(msg_or_cb, "answer") else msg_or_cb.message
        await target.answer(text, parse_mode="HTML", reply_markup=markup)


# ── /ops entry point (redirect to BotMother OS) ────────────────────────────


@router.message(Command("ops"))
async def cmd_ops(message: Message) -> None:
    from bot.callbacks import BmCb

    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Открыть BotMother OS", callback_data=BmCb(action="main"))
    await message.answer(
        "⚡ <b>Операции с аккаунтами</b>\n\n"
        "Откройте BotMother OS и перейдите в:\n"
        "<code>/menu → 📱 Активы → 📡 Каналы</code>",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    return


# ── Main menu callback ─────────────────────────────────────────────────────


@router.callback_query(ChanCb.filter(F.action == "menu"))
async def cb_chan_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _STARTER):
        _lock_kb = InlineKeyboardBuilder()
        _lock_kb.button(text="💳 Оформить подписку", callback_data=SubCb(action="menu"))
        _lock_kb.button(text="◀️ Назад", callback_data=BmCb(action="assets"))
        _lock_kb.adjust(1)
        await callback.message.edit_text(
            "🔒 <b>Операции с аккаунтами — 💎 ПОДПИСКА</b>\n\n"
            "Для доступа нужна 💎 подписка.\n\n"
            "Оформить: /subscription",
            parse_mode="HTML",
            reply_markup=_lock_kb.as_markup(),
        )
        return
    accounts = await _get_accounts(pool, callback.from_user.id)
    count = len(accounts)
    active = sum(1 for a in accounts if a["is_active"])
    await callback.message.edit_text(
        f"📡 <b>Операции с аккаунтами</b>\n\n"
        f"Аккаунтов: <b>{count}</b> ({active} активных)\n\n"
        "• <b>Управление каналом</b> → название / описание / <b>username</b> / ссылка\n"
        "• <b>Создать</b> → новый канал или группу\n"
        "• <b>Вступить / Выйти</b> → управление подписками\n"
        "• <b>Опубликовать пост</b> → от имени аккаунта\n"
        "• <b>Профиль аккаунта</b> → имя, bio, username аккаунта\n"
        "• <b>⚡ Массовые операции</b> → одно действие на всех аккаунтах\n\n"
        "💡 Нет аккаунтов? 📱 /accounts",
        parse_mode="HTML",
        reply_markup=_main_menu_kb().as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# CREATE CHANNEL / GROUP (single account)
# ══════════════════════════════════════════════════════════════════════════


@router.callback_query(ChanCb.filter(F.action.in_({"create_channel", "create_group"})))
async def cb_create_pick_account(
    callback: CallbackQuery,
    callback_data: ChanCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _PRO):
        await callback.message.edit_text(
            "🔒 <b>Создание каналов/групп — 💎 ПОДПИСКА</b>\n\n"
            "Оформите подписку PRO: /subscription",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    is_group = callback_data.action == "create_group"
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    if not active:
        await callback.message.edit_text(
            "⚠️ Нет активных аккаунтов. Проверьте подключение: /accounts",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    entity_type = "группу" if is_group else "канал"
    await state.update_data(is_group=is_group)
    if len(active) == 1:
        await state.update_data(
            acc_id=active[0]["id"],
            session_str=active[0]["session_str"]
            if "session_str" in active[0]
            else None,
        )
        await _start_create_channel_fsm(callback.message, state, entity_type, edit=True)
        return
    kb = _account_picker_kb(
        active, "create_channel_acc" if not is_group else "create_group_acc"
    )
    await callback.message.edit_text(
        f"📢 <b>Выберите аккаунт</b>\n\nС какого аккаунта создать {entity_type}?",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(
    ChanCb.filter(F.action.in_({"create_channel_acc", "create_group_acc"}))
)
async def cb_create_account_chosen(
    callback: CallbackQuery,
    callback_data: ChanCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    acc = await db.get_account_for_telethon(
        pool, callback_data.acc_id, callback.from_user.id
    )
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()
    is_group = callback_data.action == "create_group_acc"
    entity_type = "группу" if is_group else "канал"
    await state.update_data(
        acc_id=acc["id"], session_str=acc["session_str"], is_group=is_group
    )
    await _start_create_channel_fsm(callback.message, state, entity_type, edit=True)


async def _start_create_channel_fsm(
    msg, state: FSMContext, entity_type: str, edit: bool = False
) -> None:
    await state.set_state(CreateChannelFSM.waiting_title)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    text = f"📝 <b>Название {entity_type}</b>\n\nВведите название (до 128 символов):"
    if edit:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            return
        except Exception:
            log_exc_swallow(
                log, "Сбой edit_text при установке названия — отправляю новое сообщение"
            )
    await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.message(CreateChannelFSM.waiting_title)
async def fsm_create_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title or len(title) > 128:
        await message.answer("⚠️ Название от 1 до 128 символов. Попробуйте ещё раз:")
        return
    await state.update_data(title=title)
    await state.set_state(CreateChannelFSM.waiting_about)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=ChanCb(action="skip_about"))
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        "📄 <b>Описание</b>\n\nВведите описание (до 255 символов) или пропустите:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "skip_about"))
async def cb_skip_about(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(about="")
    await _show_create_confirm(callback.message, state, edit=True)


@router.message(CreateChannelFSM.waiting_about)
async def fsm_create_about(message: Message, state: FSMContext) -> None:
    about = (message.text or "").strip()[:255]
    await state.update_data(about=about)
    await _show_create_confirm(message, state, edit=False)


async def _show_create_confirm(msg, state: FSMContext, edit: bool = False) -> None:
    data = await state.get_data()
    title = html.escape(data.get("title", ""))
    about = html.escape(data.get("about", ""))
    is_group = data.get("is_group", False)
    entity_type = "Группа" if is_group else "Канал"
    text = (
        f"✅ <b>Подтвердите создание</b>\n\n"
        f"Тип: <b>{entity_type}</b>\n"
        f"Название: <b>{title}</b>\n"
        f"Описание: <b>{about or '—'}</b>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Создать", callback_data=ChanCb(action="do_create"))
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    kb.adjust(2)
    await state.set_state(CreateChannelFSM.confirming)
    if edit:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            return
        except Exception:
            log_exc_swallow(
                log, "Сбой edit_text при подтверждении — отправляю новое сообщение"
            )
    await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(ChanCb.filter(F.action == "do_create"))
async def cb_do_create(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Создаю...")
    data = await state.get_data()
    await state.clear()
    acc_id = data.get("acc_id")
    if not acc_id:
        await callback.message.edit_text(
            "⚠️ Сессия истекла. Начните заново: /ops",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    acc = await db.get_account_for_telethon(pool, acc_id, callback.from_user.id)
    if not acc:
        await callback.message.edit_text(
            "⚠️ Аккаунт не найден.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    from services import account_manager

    result = await account_manager.create_channel(
        acc["session_str"],
        title=data["title"],
        about=data.get("about", ""),
        megagroup=data.get("is_group", False),
        _acc=acc,
    )
    if "error" in result:
        err = html.escape(result["error"])
        await callback.message.edit_text(
            f"❌ <b>Ошибка создания</b>\n\n<code>{err}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    title_s = html.escape(result.get("title") or "—")
    channel_id = result.get("channel_id") or 0
    invite = result.get("invite_link", "")
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✏️ Управлять",
        callback_data=ChanCb(
            action="manage_channel", acc_id=acc_id, channel_id=channel_id
        ),
    )
    kb.button(text="◀️ Меню", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    _type_label = (result.get("type") or "канал").capitalize()
    await callback.message.edit_text(
        f"✅ <b>{_type_label} создан!</b>\n\n"
        f"Название: <b>{title_s}</b>\n"
        f"ID: <code>{channel_id or '—'}</code>\n"
        + (f"Ссылка: {html.escape(invite)}" if invite else ""),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# BULK CREATE (all active accounts)
# ══════════════════════════════════════════════════════════════════════════


@router.callback_query(ChanCb.filter(F.action == "bulk_create"))
async def cb_bulk_create_start(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _PRO):
        await callback.message.edit_text(
            "🔒 <b>Массовое создание — 💎 ПОДПИСКА</b>\n\nОформите: /subscription",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    try:
        accounts = await pool.fetch(
            "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE",
            callback.from_user.id,
        )
    except Exception:
        accounts = []
    selected = {a["id"] for a in accounts}
    await state.update_data(bulk_op="create", bulk_selected=list(selected))
    await _show_bulk_select(callback, pool, "create", selected)


@router.message(BulkCreateFSM.waiting_title)
async def fsm_bulk_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title or len(title) > 128:
        await message.answer("⚠️ Название от 1 до 128 символов:")
        return
    await state.update_data(title=title)
    await state.set_state(BulkCreateFSM.waiting_about)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=ChanCb(action="bulk_skip_about"))
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
    kb.adjust(1)
    await message.answer(
        "📄 Описание (или пропустите):", parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(ChanCb.filter(F.action == "bulk_skip_about"))
async def cb_bulk_skip_about(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(about="")
    await _bulk_choose_type(callback.message, state, edit=True)


@router.message(BulkCreateFSM.waiting_about)
async def fsm_bulk_about(message: Message, state: FSMContext) -> None:
    await state.update_data(about=(message.text or "").strip()[:255])
    await _bulk_choose_type(message, state, edit=False)


async def _bulk_choose_type(msg, state: FSMContext, edit: bool) -> None:
    await state.set_state(BulkCreateFSM.choosing_type)
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Канал", callback_data=ChanCb(action="bulk_type_channel"))
    kb.button(text="👥 Группа", callback_data=ChanCb(action="bulk_type_group"))
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    kb.adjust(2, 1)
    text = "Тип создаваемого объекта:"
    if edit:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            return
        except Exception:
            log_exc_swallow(
                log, "Сбой edit_text при выборе типа bulk — отправляю новое сообщение"
            )
    await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(
    ChanCb.filter(F.action.in_({"bulk_type_channel", "bulk_type_group"}))
)
async def cb_bulk_type_chosen(
    callback: CallbackQuery,
    callback_data: ChanCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    is_group = callback_data.action == "bulk_type_group"
    await state.update_data(is_group=is_group)
    await state.set_state(BulkCreateFSM.waiting_count)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
    await callback.message.edit_text(
        "📢 <b>Сколько каналов создать на каждом аккаунте?</b>\n\n"
        "Введите число от 1 до 10:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BulkCreateFSM.waiting_count)
async def fsm_bulk_create_count(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 10):
        await message.answer("⚠️ Введите число от 1 до 10:")
        return
    count = int(raw)
    await state.update_data(channel_count=count)
    await state.set_state(BulkCreateFSM.choosing_name_mode)
    kb = InlineKeyboardBuilder()
    kb.button(
        text="☐ Без изменений", callback_data=ChanCb(action="bulk_name_mode_none")
    )
    kb.button(
        text="🔢 Порядковый номер (1,2,3…)",
        callback_data=ChanCb(action="bulk_name_mode_num"),
    )
    kb.button(text="👤 По аккаунту", callback_data=ChanCb(action="bulk_name_mode_acc"))
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
    kb.adjust(1)
    await message.answer(
        "📢 <b>Режим уникализации имён</b>\n\n"
        "Как назвать создаваемые объекты?\n\n"
        "• <b>Без изменений</b> — все получат одинаковое название\n"
        "• <b>Порядковый номер</b> — «Название 1», «Название 2»…\n"
        "• <b>По аккаунту</b> — «Название (Аккаунт1)», «Название (Аккаунт2)»",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _render_bulk_confirm(
    msg_or_cb, state: FSMContext, pacing: str | None = None
) -> None:
    """Render the bulk create confirmation screen with pacing selection."""
    data = await state.get_data()
    selected_ids = data.get("bulk_selected", [])
    n_acc = len(selected_ids)
    count = data.get("channel_count", 1)
    total = n_acc * count
    title_s = html.escape(data["title"])
    entity = "группа" if data.get("is_group") else "канал"
    mode = data.get("name_mode", "none")
    mode_labels = {
        "none": "Без изменений",
        "num": "Порядковый номер",
        "acc": "По аккаунту",
    }

    pacing = pacing or data.get("bulk_pacing", _DEFAULT_PACING)
    preset = _BULK_PACING.get(pacing, _BULK_PACING[_DEFAULT_PACING])
    eta = _estimate_duration(total, pacing)

    await state.update_data(bulk_pacing=pacing)
    await state.set_state(BulkCreateFSM.confirming)

    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"✅ Создать {total} объект(ов)",
        callback_data=ChanCb(action="do_bulk_create"),
    )
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
    kb.adjust(1)

    pacing_kb = _pacing_kb(pacing)
    # Place pacing buttons above execute/cancel
    final_kb = InlineKeyboardBuilder()
    for row in pacing_kb.as_markup().inline_keyboard:
        final_kb.row(*row)
    for row in kb.as_markup().inline_keyboard:
        final_kb.row(*row)

    text = (
        f"🔁 <b>Подтверждение массового создания</b>\n\n"
        f"📋 Тип: <b>{entity}</b>\n"
        f"✏️ Название: <b>{title_s}</b>\n"
        f"🔤 Режим имён: <b>{mode_labels[mode]}</b>\n"
        f"📱 Аккаунтов: <b>{n_acc}</b> × <b>{count}</b> = итого <b>{total}</b>\n\n"
        f"⏱ <b>Темп:</b> {preset['label']}\n"
        f"⏳ Расчётное время: <b>{eta}</b>\n"
        f"<i>{preset['desc']}</i>\n\n"
        "⚠️ Telegram может ограничить создание с одного IP."
    )

    if isinstance(msg_or_cb, CallbackQuery):
        try:
            await msg_or_cb.message.edit_text(
                text, parse_mode="HTML", reply_markup=final_kb.as_markup()
            )
        except Exception:
            await msg_or_cb.message.answer(
                text, parse_mode="HTML", reply_markup=final_kb.as_markup()
            )
    else:
        try:
            await msg_or_cb.edit_text(
                text, parse_mode="HTML", reply_markup=final_kb.as_markup()
            )
        except Exception:
            await msg_or_cb.answer(
                text, parse_mode="HTML", reply_markup=final_kb.as_markup()
            )


@router.callback_query(
    ChanCb.filter(
        F.action.in_(
            {"bulk_name_mode_none", "bulk_name_mode_num", "bulk_name_mode_acc"}
        )
    )
)
async def cb_bulk_name_mode(
    callback: CallbackQuery, callback_data: ChanCb, state: FSMContext
) -> None:
    await callback.answer()
    mode_map = {
        "bulk_name_mode_none": "none",
        "bulk_name_mode_num": "num",
        "bulk_name_mode_acc": "acc",
    }
    mode = mode_map[callback_data.action]
    await state.update_data(name_mode=mode)
    await _render_bulk_confirm(callback, state)


# ── Pacing selection handler ────────────────────────────────────────────────


@router.callback_query(ChanCb.filter(F.action.startswith("bulk_pacing_")))
async def cb_bulk_pacing(
    callback: CallbackQuery, callback_data: ChanCb, state: FSMContext
) -> None:
    pacing_key = callback_data.action.replace("bulk_pacing_", "")
    if pacing_key not in _BULK_PACING:
        pacing_key = _DEFAULT_PACING
    await callback.answer(_BULK_PACING[pacing_key]["label"])
    await state.update_data(bulk_pacing=pacing_key)
    await _render_bulk_confirm(callback, state, pacing=pacing_key)


async def _bulk_create_bg(
    pool: asyncpg.Pool,
    progress_msg,
    data: dict,
    accounts: list,
    user_id: int,
) -> None:
    """Фоновое выполнение массового создания каналов с round-robin по аккаунтам."""
    from services import account_manager, op_worker
    from database import db as _db

    channel_count = data.get("channel_count", 1)
    name_mode = data.get("name_mode", "none")
    results_ok: list[str] = []
    results_err: list[str] = []
    total_ops = len(accounts) * channel_count
    done_ops = 0
    global_idx = 1
    attempt = 0

    # Claim accounts so op_worker/warmup don't use the same sessions in parallel
    _claimed_ids = [int(a["id"]) for a in accounts]
    await op_worker.mark_accounts_in_use(_claimed_ids)

    active_accounts = list(accounts)
    task_list = [
        (i, active_accounts[i % len(active_accounts)] if active_accounts else None)
        for i in range(total_ops)
    ]

    try:
        for task_i, acc in task_list:
            if not acc:
                results_err.append("❌ Нет доступных аккаунтов")
                done_ops += 1
                global_idx += 1
                continue
            label = html.escape(acc["first_name"] or acc["phone"])
            title = _make_title(
                data["title"], name_mode, global_idx, acc["first_name"] or acc["phone"]
            )
            tried_accs: set[int] = set()
            result = None
            for candidate in active_accounts:
                if candidate["id"] in tried_accs:
                    continue
                tried_accs.add(candidate["id"])
                result = await account_manager.create_channel(
                    candidate["session_str"],
                    title=title,
                    about=data.get("about", ""),
                    megagroup=data.get("is_group", False),
                    _acc=dict(candidate),
                )
                if result.get("banned"):
                    await _db.deactivate_account(
                        pool, candidate["id"], "banned detected in bulk op"
                    )
                    active_accounts = [
                        a for a in active_accounts if a["id"] != candidate["id"]
                    ]
                    continue
                if account_manager.is_dead_session_error(result.get("error")):
                    await _db.deactivate_account(
                        pool, candidate["id"], "dead session detected in bulk op"
                    )
                    active_accounts = [
                        a for a in active_accounts if a["id"] != candidate["id"]
                    ]
                    continue
                if result.get("flood_wait"):
                    continue
                break
            if result is None:
                result = {"error": "нет доступных аккаунтов"}
            if "error" in result:
                results_err.append(
                    f"❌ {html.escape(label)}: {html.escape(result['error'][:60])}"
                )
            else:
                results_ok.append(
                    f"✅ {html.escape(title)}: id={result.get('channel_id', '?')}"
                )
                # Сохранить созданный канал в managed_channels
                try:
                    await pool.execute(
                        """INSERT INTO managed_channels
                               (owner_id, acc_id, channel_id, title, username, access_hash, type)
                           VALUES($1,$2,$3,$4,$5,$6,$7)
                           ON CONFLICT(owner_id, channel_id) DO UPDATE SET title=$4""",
                        user_id,
                        candidate["id"],
                        result["channel_id"],
                        title,
                        result.get("username") or None,
                        result.get("access_hash", 0) or 0,
                        result.get("type", "channel"),
                    )
                except Exception:
                    log_exc_swallow(log, "Сбой записи канала в managed_channels")
            done_ops += 1
            global_idx += 1
            try:
                await progress_msg.edit_text(
                    _progress_text(
                        "Создание каналов...",
                        done_ops,
                        total_ops,
                        len(results_ok),
                        len(results_err),
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(log, "Сбой обновления прогресса bulk-создания")
            if done_ops < total_ops:
                attempt = (attempt + 1) % 5
                pacing_key = data.get("bulk_pacing", _DEFAULT_PACING)
                preset = _BULK_PACING.get(pacing_key, _BULK_PACING[_DEFAULT_PACING])
                cooldown_every = preset["cooldown_every"]
                if (task_i + 1) % cooldown_every == 0:
                    base_delay = _human_delay(*preset["cooldown_delay"])
                else:
                    base_delay = _human_delay(*preset["item_delay"])
                chaos = session_simulator.chaos_factor()
                flood = result.get("flood_wait", 0)
                await asyncio.sleep(
                    max(backoff(attempt, base=2.0, cap=30.0), flood, base_delay * chaos)
                )
    except asyncio.CancelledError:
        ok = len(results_ok)
        err = len(results_err)
        try:
            await progress_msg.edit_text(
                f"❌ <b>Создание каналов отменено</b>\n\n"
                f"✅ Создано: <b>{ok}</b>  ❌ Ошибок: <b>{err}</b>",
                parse_mode="HTML",
                reply_markup=_back_kb().as_markup(),
            )
        except Exception:
            pass
        raise
    except Exception as exc:
        log.exception("bulk_create_bg FATAL user=%s: %s", user_id, exc)
        try:
            await progress_msg.edit_text(
                f"❌ <b>Ошибка при создании каналов</b>\n\n<code>{html.escape(str(exc)[:200])}</code>",
                parse_mode="HTML",
                reply_markup=_back_kb().as_markup(),
            )
        except Exception:
            pass
        return
    finally:
        await op_worker.release_accounts(_claimed_ids)

    lines = ["🔁 <b>Результаты массового создания</b>\n"]
    lines += results_ok + results_err
    try:
        await progress_msg.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
    except Exception:
        pass


@router.callback_query(ChanCb.filter(F.action == "do_bulk_create"))
async def cb_do_bulk_create(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Запускаю массовое создание...")
    data = await state.get_data()
    selected_ids = data.get("bulk_selected", [])
    await state.clear()
    try:
        if selected_ids:
            accounts = await pool.fetch(
                "SELECT a.id, a.session_str, a.first_name, a.phone, "
                "a.device_model, a.system_version, a.app_version, p.proxy_url "
                "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
                "WHERE a.owner_id=$1 AND a.id = ANY($2::bigint[]) AND a.session_str IS NOT NULL",
                callback.from_user.id,
                selected_ids,
            )
        else:
            accounts = await pool.fetch(
                "SELECT a.id, a.session_str, a.first_name, a.phone, "
                "a.device_model, a.system_version, a.app_version, p.proxy_url "
                "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
                "WHERE a.owner_id=$1 AND a.is_active=TRUE AND a.session_str IS NOT NULL",
                callback.from_user.id,
            )
    except Exception as exc:
        mark_handled_error(f"bulk_create_confirm accounts: {exc}")
        await callback.message.edit_text(
            f"❌ Ошибка загрузки аккаунтов: <code>{html.escape(str(exc)[:200])}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    channel_count = data.get("channel_count", 1)
    total_ops = len(accounts) * channel_count
    user_id = callback.from_user.id

    if not accounts:
        await callback.message.edit_text(
            "⚠️ Нет доступных аккаунтов для создания каналов.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    # ── Pre-flight: лимит каналов по тарифу ──────────────────────────────────
    from bot.utils.subscription import get_channel_limit

    try:
        chan_limit = await get_channel_limit(pool, user_id)
        current_chans = (
            await pool.fetchval(
                "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", user_id
            )
            or 0
        )
    except Exception:
        chan_limit, current_chans = 9999, 0
        log_exc_swallow(log, "bulk_create channel limit check failed")
    if current_chans + total_ops > chan_limit:
        from aiogram.utils.keyboard import InlineKeyboardBuilder as _IKB

        _kb = _IKB()
        _kb.button(
            text="💳 Оформить подписку",
            callback_data=SubCb(action="choose_plan", plan="paid"),
        )
        _kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
        _kb.adjust(1)
        await callback.message.edit_text(
            "⛔️ <b>Лимит каналов по тарифу</b>\n\n"
            f"Сейчас каналов: <b>{current_chans}</b> из <b>{chan_limit}</b>\n"
            f"Запрошено создать: <b>{total_ops}</b>\n\n"
            "💎 Оформите подписку — без ограничений на каналы и боты.",
            parse_mode="HTML",
            reply_markup=_kb.as_markup(),
        )
        return

    # ── Pre-flight: проверить возраст и trust_score аккаунтов ────────────────
    from datetime import datetime, timezone as _tz

    acc_ids = [a["id"] for a in accounts]
    try:
        health_rows = await pool.fetch(
            "SELECT id, added_at, trust_score, first_name, phone FROM tg_accounts WHERE id = ANY($1)",
            acc_ids,
        )
    except Exception:
        health_rows = []
    risk_warnings: list[str] = []
    blocked_accs: list[str] = []
    for row in health_rows:
        label = html.escape(row["first_name"] or row["phone"] or str(row["id"]))
        ts = float(row["trust_score"] or 0.5)
        added_at = row["added_at"]
        age_days = 0
        if added_at:
            age_days = (datetime.now(_tz.utc) - added_at.replace(tzinfo=_tz.utc)).days
        if age_days < 14:
            blocked_accs.append(f"• {label} — {age_days} дн. в системе (мин. 14)")
        elif ts < 0.35:
            blocked_accs.append(f"• {label} — trust_score {ts:.2f} (мин. 0.35)")
        elif ts < 0.5 or age_days < 30:
            risk_warnings.append(f"• {label} — возраст {age_days} дн., trust={ts:.2f}")

    if blocked_accs:
        await callback.message.edit_text(
            "🚫 <b>Операция заблокирована — аккаунты не готовы</b>\n\n"
            "Следующие аккаунты не прошли минимальные требования безопасности:\n"
            + "\n".join(blocked_accs)
            + "\n\n💡 <b>Что сделать:</b>\n"
            "1. Запустите <b>🌱 Прогрев аккаунтов</b> на 14+ дней\n"
            "2. Используйте аккаунты возрастом 30+ дней с trust_score ≥ 0.5\n"
            "3. Не создавайте каналы с только что купленных/импортированных аккаунтов",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    pacing_key = data.get("bulk_pacing", _DEFAULT_PACING)
    risk_note = ""
    if pacing_key in ("fast", "turbo"):
        risk_note = "\n\n⚠️ <b>Выбран быстрый темп</b> — повышен риск бана аккаунтов!"
    if risk_warnings:
        risk_note += "\n\n🟡 <b>Аккаунты с риском:</b>\n" + "\n".join(risk_warnings)

    progress_msg = await callback.message.edit_text(
        _progress_text("Создание каналов...", 0, total_ops, 0, 0) + risk_note,
        parse_mode="HTML",
    )
    task = asyncio.create_task(
        _bulk_create_bg(pool, progress_msg, dict(data), list(accounts), user_id)
    )
    _treg.register(user_id, "bulk_create", f"Создание каналов ({total_ops} шт.)", task)


# ══════════════════════════════════════════════════════════════════════════
# BULK POST TO MULTIPLE CHANNELS
# ══════════════════════════════════════════════════════════════════════════


@router.callback_query(ChanCb.filter(F.action == "bulk_post_chans"))
async def cb_bulk_post_chans_start(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _STARTER):
        await callback.message.edit_text(
            "🔒 <b>Пост в каналы — 💎 ПОДПИСКА</b>\n\nОформите: /subscription",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    if not active:
        empty_kb = InlineKeyboardBuilder()
        empty_kb.button(text="📱 Добавить аккаунт", callback_data=AccCb(action="menu"))
        empty_kb.button(text="◀️ Назад", callback_data=ChanCb(action="bulk_menu"))
        empty_kb.adjust(1)
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Для публикации постов в каналы нужен хотя бы один активный аккаунт.\n\n"
            "Добавьте аккаунт через раздел 📱 Аккаунты.",
            parse_mode="HTML",
            reply_markup=empty_kb.as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    for acc in active:
        label = acc["first_name"] or acc["phone"] or f"id={acc['id']}"
        kb.button(
            text=f"👤 {label}",
            callback_data=ChanCb(action="bulk_post_chans_acc", acc_id=acc["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="bulk_menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "📤 <b>Пост в несколько каналов</b>\n\nВыберите аккаунт — загружу его каналы:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "bulk_post_chans_acc"))
async def cb_bulk_post_chans_acc(
    callback: CallbackQuery,
    callback_data: ChanCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    acc = await db.get_account_for_telethon(
        pool, callback_data.acc_id, callback.from_user.id
    )
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer("⏳ Загружаю каналы...")
    from services import account_manager

    try:
        dialogs = await account_manager.get_dialogs(acc["session_str"], _acc=acc)
    except Exception as _e:
        log.warning("bpchans get_dialogs failed acc=%s: %s", acc.get("id"), _e)
        await callback.message.edit_text(
            f"❌ Не удалось получить каналы аккаунта: <code>{html.escape(str(_e)[:150])}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    channels = [
        d
        for d in (dialogs or [])
        if d.get("type") in ("channel", "megagroup", "supergroup")
    ]
    if not channels:
        await callback.message.edit_text(
            "ℹ️ <b>Нет каналов у этого аккаунта</b>\n\n"
            "Этот аккаунт не состоит ни в одном канале или группе.\n\n"
            "Вступите в канал через 🔗 <b>Вступить</b> или создайте новый через 📢 <b>Создать канал</b>.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    await state.update_data(
        bpchans_acc_id=acc["id"],
        bpchans_channels=channels,
        bpchans_selected=[],
        bpchans_page=0,
    )
    await state.set_state(BulkPostChansFSM.choosing_channels)
    await _show_bpchans_page(callback.message, state, edit=True)


async def _show_bpchans_page(msg, state: FSMContext, edit: bool = False) -> None:
    data = await state.get_data()
    channels = data.get("bpchans_channels", [])
    selected = set(data.get("bpchans_selected", []))
    page = data.get("bpchans_page", 0)
    per_page = 8
    total_pages = max(1, (len(channels) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    chunk = channels[start : start + per_page]

    kb = InlineKeyboardBuilder()
    for ch in chunk:
        cid = ch["id"]
        title = (ch.get("title") or f"id={cid}")[:30]
        mark = "✅ " if cid in selected else ""
        kb.button(
            text=f"{mark}{title}",
            callback_data=f"chan:cpsel:{data['bpchans_acc_id']}:{cid}",
        )
    nav_btns = []
    if page > 0:
        nav_btns.append(("◀️", f"chan:cppage:{page - 1}"))
    if page < total_pages - 1:
        nav_btns.append(("▶️", f"chan:cppage:{page + 1}"))
    for label, cbd in nav_btns:
        kb.button(text=label, callback_data=cbd)

    n_sel = len(selected)
    if n_sel:
        kb.button(
            text=f"▶️ Продолжить ({n_sel} канал(ов))",
            callback_data=f"chan:cpsdone:{data['bpchans_acc_id']}",
        )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="bulk_post_chans"))
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
    kb.adjust(1)

    text = (
        f"📤 <b>Выберите каналы для поста</b>\n"
        f"Стр. {page + 1}/{total_pages} · Выбрано: {n_sel}\n\n"
        "Нажмите на канал для выбора/снятия:"
    )
    if edit:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            return
        except Exception:
            log_exc_swallow(
                log, "Сбой edit_text при выборе каналов — отправляю новое сообщение"
            )
    await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("chan:cpsel:"))
async def cb_bpchans_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    parts = callback.data.split(":")
    ch_id = int(parts[3])
    data = await state.get_data()
    selected = list(data.get("bpchans_selected", []))
    if ch_id in selected:
        selected.remove(ch_id)
    else:
        selected.append(ch_id)
    await state.update_data(bpchans_selected=selected)
    await _show_bpchans_page(callback.message, state, edit=True)


@router.callback_query(F.data.startswith("chan:cppage:"))
async def cb_bpchans_page(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    page = int(callback.data.split(":")[2])
    await state.update_data(bpchans_page=page)
    await _show_bpchans_page(callback.message, state, edit=True)


@router.callback_query(F.data.startswith("chan:cpsdone:"))
async def cb_bpchans_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    selected = data.get("bpchans_selected", [])
    if not selected:
        await callback.answer("Выберите хотя бы один канал.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(BulkPostChansFSM.waiting_text)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
    await callback.message.edit_text(
        f"📝 <b>Введите текст поста</b>\n\nВыбрано каналов: {len(selected)}\n\nПоддерживается HTML-разметка:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _bulk_post_chans_bg(
    pool: asyncpg.Pool,
    acc_id: int,
    progress_msg,
    acc: dict,
    selected_ids: list,
    ch_map: dict,
    text: str,
    total: int,
) -> None:
    from services import account_manager
    from database import db as _db

    ok, err = 0, 0
    attempt = 0
    try:
        for idx, ch_id in enumerate(selected_ids, 1):
            access_hash = ch_map.get(ch_id, {}).get("access_hash", 0) or 0
            result = await account_manager.post_to_channel(
                acc["session_str"], ch_id, text, access_hash=access_hash, _acc=acc
            )
            if result.get("banned"):
                await _db.deactivate_account(pool, acc_id, "banned detected in bulk op")
                err += 1
            elif "error" in result:
                err += 1
            else:
                ok += 1
            try:
                await progress_msg.edit_text(
                    _progress_text("Публикация постов...", idx, total, ok, err),
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(log, "Сбой обновления прогресса массовой публикации")
            if attempt >= 4:
                attempt = 0
            else:
                attempt += 1
            flood = result.get("flood_wait", 0)
            await asyncio.sleep(max(backoff(attempt, base=2.0, cap=30.0), flood))
    except asyncio.CancelledError:
        log.info("_bulk_post_chans_bg: отменено")
        raise
    except Exception:
        log_exc_swallow(log, "_bulk_post_chans_bg: неожиданная ошибка")

    lines = [
        "📤 <b>Результаты публикации</b>\n",
        f"Каналов: {total} · ✅ {ok} · ❌ {err}\n",
    ]
    try:
        await progress_msg.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
    except Exception:
        log_exc_swallow(log, "_bulk_post_chans_bg: сбой финального отчёта")


@router.message(BulkPostChansFSM.waiting_text)
async def fsm_bpchans_text(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    text = message.text or message.caption or ""
    if not text.strip():
        await message.answer("⚠️ Введите текст поста:")
        return
    data = await state.get_data()
    selected_ids = data.get("bpchans_selected", [])
    acc_id = data.get("bpchans_acc_id")
    channels = data.get("bpchans_channels", [])
    await state.clear()

    acc = await db.get_account_for_telethon(pool, acc_id, message.from_user.id)
    if not acc:
        await message.answer("❌ Аккаунт не найден.")
        return

    ch_map = {ch["id"]: ch for ch in channels}
    total = len(selected_ids)
    progress_msg = await message.answer(
        _progress_text("Публикация постов...", 0, total, 0, 0),
        parse_mode="HTML",
    )
    task = asyncio.create_task(
        _bulk_post_chans_bg(
            pool, acc_id, progress_msg, acc, selected_ids, ch_map, text, total
        )
    )
    _treg.register(
        message.from_user.id,
        "bulk_post_chans",
        f"Публикация в {total} каналов",
        task,
    )


# ══════════════════════════════════════════════════════════════════════════
# JOIN CHANNEL
# ══════════════════════════════════════════════════════════════════════════


@router.callback_query(ChanCb.filter(F.action == "join"))
async def cb_join_pick_account(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _STARTER):
        await callback.message.edit_text(
            "🔒 <b>Вступление в каналы — 💎 ПОДПИСКА</b>\n\nОформить: /subscription",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    if not active:
        empty_kb = InlineKeyboardBuilder()
        empty_kb.button(text="📱 Добавить аккаунт", callback_data=AccCb(action="menu"))
        empty_kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
        empty_kb.adjust(1)
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Для вступления в каналы нужен хотя бы один активный аккаунт.\n\n"
            "Добавьте аккаунт через раздел 📱 Аккаунты.",
            parse_mode="HTML",
            reply_markup=empty_kb.as_markup(),
        )
        return
    if len(active) == 1:
        try:
            acc = await pool.fetchrow(
                "SELECT id, session_str FROM tg_accounts WHERE id=$1", active[0]["id"]
            )
        except Exception:
            acc = None
        if not acc:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        await state.update_data(acc_id=acc["id"], session_str=acc["session_str"])
        await _start_join_fsm(callback.message, state, edit=True)
        return
    kb = _account_picker_kb(active, "join_acc")
    await callback.message.edit_text(
        "🔗 <b>Вступить в канал</b>\n\nВыберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "join_acc"))
async def cb_join_account_chosen(
    callback: CallbackQuery,
    callback_data: ChanCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    acc = await db.get_account_for_telethon(
        pool, callback_data.acc_id, callback.from_user.id
    )
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(acc_id=acc["id"], session_str=acc["session_str"])
    await _start_join_fsm(callback.message, state, edit=True)


async def _start_join_fsm(msg, state: FSMContext, edit: bool = False) -> None:
    await state.set_state(JoinChannelFSM.waiting_invite)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    text = (
        "🔗 <b>Вступить в канал</b>\n\n"
        "Введите username канала или ссылку:\n"
        "• <code>@channelname</code>\n"
        "• <code>https://t.me/channelname</code>\n"
        "• <code>https://t.me/+AbcPrivateHash</code>"
    )
    if edit:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            return
        except Exception:
            log_exc_swallow(
                log, "Сбой edit_text при настройке join — отправляю новое сообщение"
            )
    await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


# fsm_join_invite is now handled by fsm_join_invite_combined below (supports bulk selection)


# ══════════════════════════════════════════════════════════════════════════
# LEAVE CHANNEL
# ══════════════════════════════════════════════════════════════════════════


@router.callback_query(ChanCb.filter(F.action == "leave_pick"))
async def cb_leave_pick_account(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _STARTER):
        await callback.message.edit_text(
            "🔒 /subscription", reply_markup=_back_kb().as_markup()
        )
        return
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    if not active:
        empty_kb = InlineKeyboardBuilder()
        empty_kb.button(text="📱 Добавить аккаунт", callback_data=AccCb(action="menu"))
        empty_kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
        empty_kb.adjust(1)
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Для выхода из каналов нужен хотя бы один активный аккаунт.\n\n"
            "Добавьте аккаунт через раздел 📱 Аккаунты.",
            parse_mode="HTML",
            reply_markup=empty_kb.as_markup(),
        )
        return
    kb = _account_picker_kb(active, "leave_dialogs")
    await callback.message.edit_text(
        "🚪 <b>Выйти из канала</b>\n\nВыберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "leave_dialogs"))
async def cb_leave_show_dialogs(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Загружаю список каналов...")
    acc = await db.get_account_for_telethon(
        pool, callback_data.acc_id, callback.from_user.id
    )
    if not acc:
        await callback.message.edit_text(
            "❌ Аккаунт не найден.", reply_markup=_back_kb().as_markup()
        )
        return
    from services import account_manager

    try:
        dialogs = await account_manager.get_dialogs(
            acc["session_str"], limit=30, _acc=acc
        )
    except Exception as _e:
        log.warning("leave get_dialogs failed acc=%s: %s", acc.get("id"), _e)
        await callback.message.edit_text(
            f"❌ Не удалось получить список каналов: <code>{html.escape(str(_e)[:150])}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    if not dialogs:
        await callback.message.edit_text(
            "ℹ️ <b>Каналов не найдено</b>\n\n"
            "Этот аккаунт не состоит ни в одном канале или группе.\n\n"
            "Для выхода из канала сначала вступите через 🔗 <b>Вступить</b>.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    for d in dialogs[:20]:
        label = f"{'📢' if d['type'] == 'channel' else '👥'} {d['title'][:30]}"
        kb.button(
            text=label,
            callback_data=ChanCb(
                action="do_leave", acc_id=callback_data.acc_id, channel_id=d["id"]
            ),
        )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "🚪 <b>Выберите канал для выхода:</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "do_leave"))
async def cb_do_leave(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool
) -> None:
    acc = await db.get_account_for_telethon(
        pool, callback_data.acc_id, callback.from_user.id
    )
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer("⏳ Выхожу...")
    from services import account_manager

    ok = await account_manager.leave_channel(
        acc["session_str"], callback_data.channel_id, _acc=acc
    )
    if ok:
        await callback.message.edit_text(
            "✅ <b>Вышел из канала</b>",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
    else:
        await callback.message.edit_text(
            "❌ <b>Не удалось выйти</b>\n\nВозможно, вы уже не являетесь участником.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )


# ══════════════════════════════════════════════════════════════════════════
# POST TO CHANNEL
# ══════════════════════════════════════════════════════════════════════════


@router.callback_query(ChanCb.filter(F.action == "post_pick"))
async def cb_post_pick_account(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _STARTER):
        await callback.message.edit_text(
            "🔒 /subscription", reply_markup=_back_kb().as_markup()
        )
        return
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    if not active:
        empty_kb = InlineKeyboardBuilder()
        empty_kb.button(text="📱 Добавить аккаунт", callback_data=AccCb(action="menu"))
        empty_kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
        empty_kb.adjust(1)
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Для публикации постов нужен хотя бы один активный аккаунт.\n\n"
            "Добавьте аккаунт через раздел 📱 Аккаунты.",
            parse_mode="HTML",
            reply_markup=empty_kb.as_markup(),
        )
        return
    kb = _account_picker_kb(active, "post_dialogs")
    await callback.message.edit_text(
        "📤 <b>Опубликовать пост</b>\n\nВыберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "post_dialogs"))
async def cb_post_show_dialogs(
    callback: CallbackQuery,
    callback_data: ChanCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer("⏳ Загружаю каналы...")
    acc = await db.get_account_for_telethon(
        pool, callback_data.acc_id, callback.from_user.id
    )
    if not acc:
        await callback.message.edit_text(
            "❌ Аккаунт не найден.", reply_markup=_back_kb().as_markup()
        )
        return
    from services import account_manager

    owned = await account_manager.scan_owned_assets(acc["session_str"], _acc=acc)
    dialogs = [{**ch, "type": "channel"} for ch in owned.get("channels", [])] + [
        {**gr, "type": "megagroup"} for gr in owned.get("groups", [])
    ]
    if not dialogs:
        await callback.message.edit_text(
            "ℹ️ <b>Нет каналов для публикации</b>\n\n"
            "Этот аккаунт не управляет ни одним каналом или группой.\n\n"
            "Создайте канал через 📢 <b>Создать канал</b> или вступите в существующий.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    await state.update_data(acc_id=callback_data.acc_id)
    kb = InlineKeyboardBuilder()
    for d in dialogs[:20]:
        label = f"{'📢' if d['type'] == 'channel' else '👥'} {d['title'][:30]}"
        kb.button(
            text=label,
            callback_data=ChanCb(
                action="post_channel", acc_id=callback_data.acc_id, channel_id=d["id"]
            ),
        )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "📤 <b>Выберите канал для публикации:</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "post_channel"))
async def cb_post_channel_chosen(
    callback: CallbackQuery, callback_data: ChanCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.update_data(
        acc_id=callback_data.acc_id, channel_id=callback_data.channel_id
    )
    await state.set_state(PostToChannelFSM.waiting_text)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    await callback.message.edit_text(
        "📝 <b>Текст публикации</b>\n\nВведите текст поста (поддерживается HTML):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )

    # Single-account post is now handled by fsm_bulk_post_text below (bulk=False path)


# ══════════════════════════════════════════════════════════════════════════
# MANAGE CHANNEL (title / about / username / invite link / delete)
# ══════════════════════════════════════════════════════════════════════════


@router.callback_query(ChanCb.filter(F.action == "manage_pick"))
async def cb_manage_pick_account(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    if not active:
        empty_kb = InlineKeyboardBuilder()
        empty_kb.button(text="📱 Добавить аккаунт", callback_data=AccCb(action="menu"))
        empty_kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
        empty_kb.adjust(1)
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Для управления каналами нужен хотя бы один активный аккаунт.\n\n"
            "Добавьте аккаунт через раздел 📱 Аккаунты.",
            parse_mode="HTML",
            reply_markup=empty_kb.as_markup(),
        )
        return
    kb = _account_picker_kb(active, "manage_dialogs")
    await callback.message.edit_text(
        "✏️ <b>Управление каналом</b>\n\nВыберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "manage_dialogs"))
async def cb_manage_show_dialogs(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    acc_id = callback_data.acc_id
    uid = callback.from_user.id

    # First try managed_channels from DB (instant)
    try:
        db_chans = await pool.fetch(
            "SELECT channel_id, title, username FROM managed_channels "
            "WHERE owner_id=$1 AND acc_id=$2 ORDER BY title",
            uid,
            acc_id,
        )
    except Exception:
        db_chans = []

    kb = InlineKeyboardBuilder()
    if db_chans:
        for ch in db_chans[:25]:
            uname_tag = (
                f" @{ch['username']}" if ch.get("username") else " (без username)"
            )
            title = (ch["title"] or f"ID {ch['channel_id']}")[:28]
            kb.button(
                text=f"✏️ {title}{uname_tag}",
                callback_data=ChanCb(
                    action="manage_channel", acc_id=acc_id, channel_id=ch["channel_id"]
                ),
            )
        kb.button(
            text="🔄 Загрузить из Telegram",
            callback_data=ChanCb(action="manage_dialogs_live", acc_id=acc_id),
        )
    else:
        kb.button(
            text="📥 Загрузить из Telegram",
            callback_data=ChanCb(action="manage_dialogs_live", acc_id=acc_id),
        )

    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    header = (
        f"✏️ <b>Каналы/группы аккаунта</b>\nНайдено в базе: {len(db_chans)}\n\n"
        if db_chans
        else "✏️ <b>Нет сохранённых каналов</b>\n\nЗагрузите из Telegram:\n"
    )
    await callback.message.edit_text(
        header, parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(ChanCb.filter(F.action == "manage_dialogs_live"))
async def cb_manage_show_dialogs_live(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Загружаю из Telegram...")
    acc = await db.get_account_for_telethon(
        pool, callback_data.acc_id, callback.from_user.id
    )
    if not acc:
        await callback.message.edit_text(
            "❌ Аккаунт не найден.", reply_markup=_back_kb().as_markup()
        )
        return
    from services import account_manager

    result = await account_manager.scan_owned_assets(acc["session_str"], _acc=acc)
    all_items = result.get("channels", []) + result.get("groups", [])
    # Save to DB for future use
    if all_items:
        await db.upsert_managed_channels(
            pool, callback.from_user.id, callback_data.acc_id, all_items
        )
    if not all_items:
        await callback.message.edit_text(
            "ℹ️ <b>Нет каналов с правами администратора</b>\n\n"
            "Этот аккаунт не является администратором ни одного канала или группы.\n\n"
            "Создайте канал через 📢 <b>Создать канал</b> или запросите права у владельца.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    for d in all_items[:25]:
        uname_tag = f" @{d['username']}" if d.get("username") else " (без username)"
        title = (d["title"] or f"ID {d['id']}")[:28]
        kb.button(
            text=f"✏️ {title}{uname_tag}",
            callback_data=ChanCb(
                action="manage_channel", acc_id=callback_data.acc_id, channel_id=d["id"]
            ),
        )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"✏️ <b>Ваши каналы/группы:</b> {len(all_items)}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "manage_channel"))
async def cb_manage_channel_menu(
    callback: CallbackQuery, callback_data: ChanCb
) -> None:
    await callback.answer()
    acc_id = callback_data.acc_id
    ch_id = callback_data.channel_id
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✏️ Изменить название",
        callback_data=ChanCb(action="edit_title", acc_id=acc_id, channel_id=ch_id),
    )
    kb.button(
        text="📄 Изменить описание",
        callback_data=ChanCb(action="edit_about", acc_id=acc_id, channel_id=ch_id),
    )
    kb.button(
        text="🔤 Установить username",
        callback_data=ChanCb(action="edit_uname", acc_id=acc_id, channel_id=ch_id),
    )
    kb.button(
        text="🔗 Ссылка-приглашение",
        callback_data=ChanCb(action="get_invite", acc_id=acc_id, channel_id=ch_id),
    )
    kb.button(
        text="👑 Со-Администраторы",
        callback_data=ChanCb(action="manage_admins", acc_id=acc_id, channel_id=ch_id),
    )
    kb.button(
        text="🗑 Удалить канал",
        callback_data=ChanCb(action="del_channel", acc_id=acc_id, channel_id=ch_id),
    )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="manage_pick"))
    kb.adjust(2, 2, 2, 1)
    await callback.message.edit_text(
        f"✏️ <b>Управление каналом</b>\n\nID: <code>{ch_id}</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "edit_title"))
async def cb_edit_title(
    callback: CallbackQuery, callback_data: ChanCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(EditChannelFSM.waiting_value)
    await state.update_data(
        field="title", acc_id=callback_data.acc_id, channel_id=callback_data.channel_id
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    await callback.message.edit_text(
        "✏️ Введите новое <b>название</b>:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "edit_about"))
async def cb_edit_about(
    callback: CallbackQuery, callback_data: ChanCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(EditChannelFSM.waiting_value)
    await state.update_data(
        field="about", acc_id=callback_data.acc_id, channel_id=callback_data.channel_id
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    await callback.message.edit_text(
        "📄 Введите новое <b>описание</b>:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "edit_uname"))
async def cb_edit_uname(
    callback: CallbackQuery, callback_data: ChanCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(EditChannelFSM.waiting_value)
    await state.update_data(
        field="username",
        acc_id=callback_data.acc_id,
        channel_id=callback_data.channel_id,
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    await callback.message.edit_text(
        "🔤 Введите новый <b>username</b> (без @, только a-z, 0-9, _):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(EditChannelFSM.waiting_value)
async def fsm_edit_value(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    value = (message.text or "").strip()
    data = await state.get_data()
    await state.clear()
    acc = await db.get_account_for_telethon(
        pool, data.get("acc_id"), message.from_user.id
    )
    if not acc:
        await message.answer(
            "⚠️ Аккаунт не найден. Начните заново: /ops",
            reply_markup=_back_kb().as_markup(),
        )
        return
    from services import account_manager

    field = data["field"]
    ch_id = data["channel_id"]
    kb = _back_kb()
    if field == "title":
        ok = await account_manager.edit_channel_title(
            acc["session_str"], ch_id, value, _acc=acc
        )
        await message.answer(
            "✅ Название изменено!" if ok else "❌ Ошибка изменения названия.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    elif field == "about":
        ok = await account_manager.edit_channel_about(
            acc["session_str"], ch_id, value, _acc=acc
        )
        await message.answer(
            "✅ Описание изменено!" if ok else "❌ Ошибка изменения описания.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    elif field == "username":
        err = await account_manager.set_channel_username(
            acc["session_str"], ch_id, value, _acc=acc
        )
        if err:
            await message.answer(
                f"❌ Ошибка: <code>{html.escape(err)}</code>",
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )
        else:
            await message.answer(
                f"✅ Username установлен: @{html.escape(value)}",
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )


@router.callback_query(ChanCb.filter(F.action == "get_invite"))
async def cb_get_invite(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool
) -> None:
    acc = await db.get_account_for_telethon(
        pool, callback_data.acc_id, callback.from_user.id
    )
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer("⏳ Получаю ссылку...")
    from services import account_manager

    link = await account_manager.get_channel_invite_link(
        acc["session_str"], callback_data.channel_id, _acc=acc
    )
    if link:
        await callback.message.edit_text(
            f"🔗 <b>Ссылка-приглашение</b>\n\n<code>{html.escape(link)}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb(callback_data.acc_id).as_markup(),
        )
    else:
        await callback.message.edit_text(
            "❌ Не удалось получить ссылку. Проверьте права аккаунта.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )


@router.callback_query(ChanCb.filter(F.action == "manage_admins"))
async def cb_manage_admins(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool
) -> None:
    """Show managed accounts and let owner promote them to admin in the channel."""
    await callback.answer()
    acc_id = callback_data.acc_id
    ch_id = callback_data.channel_id
    owner_id = callback.from_user.id

    try:
        accounts = await pool.fetch(
            "SELECT id, phone, first_name, username, tg_user_id FROM tg_accounts "
            "WHERE owner_id=$1 AND is_active=TRUE AND tg_user_id IS NOT NULL AND id != $2 "
            "ORDER BY trust_score DESC NULLS LAST",
            owner_id,
            acc_id,
        )
    except Exception:
        accounts = []

    kb = InlineKeyboardBuilder()
    lines = [
        "👑 <b>Со-Администраторы</b>\n",
        "Выберите аккаунты для промоции в администраторы канала.\n",
        f"Канал ID: <code>{ch_id}</code>\n",
    ]

    if not accounts:
        lines.append("<i>Нет других активных аккаунтов с известным Telegram ID.</i>")
    else:
        lines.append(f"Доступно {len(accounts)} аккаунтов:")
        for acc in accounts:
            name = (acc["first_name"] or "").strip()
            uname = (
                f"@{acc['username']}" if acc.get("username") else acc.get("phone", "")
            )
            label = f"{name} ({uname})" if name else uname
            kb.button(
                text=f"👑 Промовать: {label}",
                callback_data=ChanCb(
                    action="do_promote", acc_id=acc_id, channel_id=ch_id, page=acc["id"]
                ),
            )
        kb.button(
            text="👑 Промовать ВСЕХ",
            callback_data=ChanCb(action="promote_all", acc_id=acc_id, channel_id=ch_id),
        )

    kb.button(
        text="◀️ Назад",
        callback_data=ChanCb(action="manage_channel", acc_id=acc_id, channel_id=ch_id),
    )
    kb.adjust(1)
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(ChanCb.filter(F.action == "do_promote"))
async def cb_do_promote(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool
) -> None:
    """Promote a single account to admin using the owner's account."""
    acc_id = callback_data.acc_id  # owner account (has admin rights)
    target_db_id = callback_data.page  # target account db id
    ch_id = callback_data.channel_id
    owner_id = callback.from_user.id

    owner_acc = await db.get_account_for_telethon(pool, acc_id, owner_id)
    if not owner_acc:
        await callback.answer("Аккаунт-администратор не найден.", show_alert=True)
        return

    try:
        target = await pool.fetchrow(
            "SELECT id, phone, first_name, tg_user_id FROM tg_accounts WHERE id=$1 AND owner_id=$2",
            target_db_id,
            owner_id,
        )
    except Exception:
        target = None
    if not target or not target["tg_user_id"]:
        await callback.answer(
            "Целевой аккаунт не найден или нет Telegram ID.", show_alert=True
        )
        return

    await callback.answer("⏳ Промовую в администраторы...")
    from services import account_manager

    ok = await account_manager.promote_to_admin(
        owner_acc["session_str"], ch_id, target["tg_user_id"], _acc=owner_acc
    )
    name = target["first_name"] or target["phone"] or f"id{target['id']}"
    _promote_back_kb = InlineKeyboardBuilder()
    _promote_back_kb.button(
        text="◀️ К администраторам",
        callback_data=ChanCb(action="manage_admins", acc_id=acc_id, channel_id=ch_id),
    )
    if ok:
        await callback.message.edit_text(
            f"✅ <b>{html.escape(name)} теперь администратор!</b>",
            parse_mode="HTML",
            reply_markup=_promote_back_kb.as_markup(),
        )
    else:
        await callback.message.edit_text(
            f"❌ <b>Не удалось промовать {html.escape(name)}</b>\n\nПроверьте: аккаунт должен быть участником канала.",
            parse_mode="HTML",
            reply_markup=_promote_back_kb.as_markup(),
        )


@router.callback_query(ChanCb.filter(F.action == "promote_all"))
async def cb_promote_all(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool
) -> None:
    """Promote all managed accounts to admin using the owner's account."""
    acc_id = callback_data.acc_id
    ch_id = callback_data.channel_id
    owner_id = callback.from_user.id

    owner_acc = await db.get_account_for_telethon(pool, acc_id, owner_id)
    if not owner_acc:
        await callback.answer("Аккаунт-администратор не найден.", show_alert=True)
        return
    await callback.answer("⏳ Промовую всех аккаунтов...")

    try:
        accounts = await pool.fetch(
            "SELECT id, phone, first_name, tg_user_id FROM tg_accounts "
            "WHERE owner_id=$1 AND is_active=TRUE AND tg_user_id IS NOT NULL AND id != $2",
            owner_id,
            acc_id,
        )
    except Exception:
        accounts = []

    from services import account_manager

    ok_count = 0
    fail_count = 0
    for acc in accounts:
        try:
            ok = await account_manager.promote_to_admin(
                owner_acc["session_str"], ch_id, acc["tg_user_id"], _acc=owner_acc
            )
            if ok:
                ok_count += 1
            else:
                fail_count += 1
        except Exception:
            fail_count += 1
        await asyncio.sleep(2)

    kb = InlineKeyboardBuilder()
    kb.button(
        text="◀️ Назад",
        callback_data=ChanCb(action="manage_admins", acc_id=acc_id, channel_id=ch_id),
    )
    await callback.message.edit_text(
        f"👑 <b>Промоция завершена</b>\n\n"
        f"✅ Успешно: <b>{ok_count}</b>\n"
        f"❌ Ошибки: <b>{fail_count}</b>\n\n"
        f"<i>Аккаунты должны быть участниками канала перед промоцией.</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "del_channel"))
async def cb_del_channel_confirm(
    callback: CallbackQuery, callback_data: ChanCb
) -> None:
    await callback.answer()
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🗑 ДА, УДАЛИТЬ НАВСЕГДА",
        callback_data=ChanCb(
            action="do_delete",
            acc_id=callback_data.acc_id,
            channel_id=callback_data.channel_id,
        ),
    )
    kb.button(
        text="◀️ Отмена",
        callback_data=ChanCb(
            action="manage_channel",
            acc_id=callback_data.acc_id,
            channel_id=callback_data.channel_id,
        ),
    )
    kb.adjust(1)
    await callback.message.edit_text(
        f"⚠️ <b>Удалить канал?</b>\n\nID: <code>{callback_data.channel_id}</code>\n\n"
        "Это действие <b>необратимо</b>. Все сообщения будут удалены.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "do_delete"))
async def cb_do_delete(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool
) -> None:
    acc = await db.get_account_for_telethon(
        pool, callback_data.acc_id, callback.from_user.id
    )
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer("⏳ Удаляю...")
    from services import account_manager

    ok = await account_manager.delete_channel(
        acc["session_str"], callback_data.channel_id, _acc=acc
    )
    await callback.message.edit_text(
        "✅ <b>Канал удалён.</b>"
        if ok
        else "❌ <b>Ошибка удаления.</b> Проверьте права.",
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# MEMBERS
# ══════════════════════════════════════════════════════════════════════════


@router.callback_query(ChanCb.filter(F.action == "members_pick"))
async def cb_members_pick_account(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _STARTER):
        await callback.message.edit_text(
            "🔒 <b>Управление участниками — 💎 ПОДПИСКА</b>\n\nОформите: /subscription",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    if not active:
        empty_kb = InlineKeyboardBuilder()
        empty_kb.button(text="📱 Добавить аккаунт", callback_data=AccCb(action="menu"))
        empty_kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
        empty_kb.adjust(1)
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Для управления участниками нужен хотя бы один активный аккаунт.\n\n"
            "Добавьте аккаунт через раздел 📱 Аккаунты.",
            parse_mode="HTML",
            reply_markup=empty_kb.as_markup(),
        )
        return
    kb = _account_picker_kb(active, "members_dialogs")
    await callback.message.edit_text(
        "👥 <b>Участники</b>\n\nВыберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "members_dialogs"))
async def cb_members_dialogs(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool
) -> None:
    acc = await db.get_account_for_telethon(
        pool, callback_data.acc_id, callback.from_user.id
    )
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer("⏳ Загружаю каналы...")
    from services import account_manager

    try:
        dialogs = await account_manager.get_dialogs(
            acc["session_str"], limit=30, _acc=acc
        )
    except Exception as _e:
        log.warning("members get_dialogs failed acc=%s: %s", acc.get("id"), _e)
        await callback.message.edit_text(
            f"❌ Не удалось получить список каналов: <code>{html.escape(str(_e)[:150])}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    if not dialogs:
        await callback.message.edit_text(
            "ℹ️ <b>Нет каналов/групп</b>\n\n"
            "Этот аккаунт не состоит ни в одном канале или группе.\n\n"
            "Вступите в канал через 🔗 <b>Вступить</b> или создайте новый.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    for d in dialogs[:20]:
        label = f"{'📢' if d['type'] == 'channel' else '👥'} {d['title'][:30]}"
        kb.button(
            text=label,
            callback_data=ChanCb(
                action="members_menu", acc_id=callback_data.acc_id, channel_id=d["id"]
            ),
        )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "👥 <b>Выберите канал/группу:</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "members_menu"))
async def cb_members_menu(callback: CallbackQuery, callback_data: ChanCb) -> None:
    await callback.answer()
    acc_id, ch_id = callback_data.acc_id, callback_data.channel_id
    kb = InlineKeyboardBuilder()
    kb.button(
        text="👁 Просмотр участников",
        callback_data=ChanCb(action="members_view", acc_id=acc_id, channel_id=ch_id),
    )
    kb.button(
        text="➕ Пригласить",
        callback_data=ChanCb(action="members_invite", acc_id=acc_id, channel_id=ch_id),
    )
    kb.button(
        text="🚫 Кикнуть пользователя",
        callback_data=ChanCb(action="members_kick", acc_id=acc_id, channel_id=ch_id),
    )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="members_pick"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"👥 <b>Управление участниками</b>\n\nID канала: <code>{ch_id}</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "members_view"))
async def cb_members_view(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Загружаю участников...")
    acc = await db.get_account_for_telethon(
        pool, callback_data.acc_id, callback.from_user.id
    )
    if not acc:
        await callback.message.edit_text(
            "❌ Аккаунт не найден.", reply_markup=_back_kb().as_markup()
        )
        return
    from services import account_manager

    members = await account_manager.get_channel_members(
        acc["session_str"], callback_data.channel_id, limit=30, _acc=acc
    )
    if not members:
        kb_back = InlineKeyboardBuilder()
        kb_back.button(
            text="◀️ Назад",
            callback_data=ChanCb(
                action="members_menu",
                acc_id=callback_data.acc_id,
                channel_id=callback_data.channel_id,
            ),
        )
        await callback.message.edit_text(
            "ℹ️ <b>Участники недоступны</b>\n\n"
            "Список пуст или у аккаунта нет прав на просмотр участников.\n\n"
            "Убедитесь, что аккаунт является администратором канала/группы.",
            parse_mode="HTML",
            reply_markup=kb_back.as_markup(),
        )
        return
    lines = [f"👥 <b>Участники ({len(members)}):</b>\n"]
    for m in members:
        uname = f"@{html.escape(m['username'])}" if m["username"] else ""
        name = html.escape(m["first_name"])
        bot_tag = " 🤖" if m["is_bot"] else ""
        lines.append(f"• {name} {uname}{bot_tag} — <code>{m['user_id']}</code>")
    kb = InlineKeyboardBuilder()
    kb.button(
        text="◀️ Назад",
        callback_data=ChanCb(
            action="members_menu",
            acc_id=callback_data.acc_id,
            channel_id=callback_data.channel_id,
        ),
    )
    await callback.message.edit_text(
        "\n".join(lines[:35]),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# INVITE USERS — мульти-аккаунт, параллельный инвайт
# ══════════════════════════════════════════════════════════════════════════


@router.callback_query(ChanCb.filter(F.action == "members_invite"))
async def cb_members_invite(
    callback: CallbackQuery,
    callback_data: ChanCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    # Получаем данные канала из БД для отображения
    channel_row = None
    if callback_data.channel_id:
        try:
            channel_row = await pool.fetchrow(
                "SELECT title, access_hash FROM managed_channels WHERE channel_id=$1 AND owner_id=$2",
                callback_data.channel_id,
                callback.from_user.id,
            )
        except Exception:
            channel_row = None
    channel_display = (channel_row["title"] if channel_row else None) or str(
        callback_data.channel_id
    )
    access_hash = (channel_row["access_hash"] if channel_row else 0) or 0

    # Все данные сохраняем в FSM state — никаких in-memory dict
    # primary_acc_id = аккаунт-владелец канала, он будет повышать остальных до админа
    await state.update_data(
        channel_id=callback_data.channel_id,
        access_hash=access_hash,
        channel_display=channel_display,
        primary_acc_id=callback_data.acc_id,
        inv_selected_accounts=[],
    )
    await state.set_state(InviteUsersFSM.choosing_accounts)

    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    if not active:
        await state.clear()
        await callback.message.edit_text(
            "⚠️ Нет активных аккаунтов. Подключите аккаунты: /accounts",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    # Выбираем все активные аккаунты по умолчанию
    default_ids = [a["id"] for a in active]
    await state.update_data(inv_selected_accounts=default_ids)
    await _show_invite_acc_selector(
        callback.message, active, set(default_ids), channel_display, edit=True
    )


async def _show_invite_acc_selector(
    msg,
    accounts: list,
    selected: set[int],
    channel_display: str,
    edit: bool = True,
) -> None:
    kb = InlineKeyboardBuilder()
    for acc in accounts:
        icon = "✅" if acc["id"] in selected else "☐"
        kb.button(
            text=f"{icon} {_acc_label(acc)}", callback_data=f"invite:acc:{acc['id']}"
        )
    kb.button(text="✅ Выбрать все", callback_data="invite:acc:selall")
    kb.button(text="☐ Снять все", callback_data="invite:acc:selnone")
    n = len(selected)
    if n > 0:
        kb.button(text=f"▶️ Продолжить ({n} акк.)", callback_data="invite:acc:done")
    kb.button(text="❌ Отмена", callback_data="invite:acc:cancel")
    kb.adjust(1)
    text = (
        f"➕ <b>Инвайт пользователей</b>\n"
        f"Канал: <code>{html.escape(channel_display)}</code>\n\n"
        f"Выберите аккаунты для инвайта.\n"
        f"Список равномерно распределится между аккаунтами.\n\n"
        f"Выбрано: <b>{n}</b> аккаунт(ов)"
    )
    if edit:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            return
        except Exception:
            pass
    await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(
    InviteUsersFSM.choosing_accounts, F.data.startswith("invite:acc:")
)
async def cb_invite_acc_action(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    action = callback.data.split("invite:acc:")[1]
    data = await state.get_data()
    selected = set(data.get("inv_selected_accounts", []))
    channel_display = data.get("channel_display", "?")
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]

    if action == "cancel":
        await state.clear()
        await callback.answer()
        await callback.message.edit_text(
            "❌ Инвайт отменён.", reply_markup=_back_kb().as_markup()
        )
        return

    if action == "selall":
        selected = {a["id"] for a in active}
    elif action == "selnone":
        selected = set()
    elif action == "done":
        if not selected:
            await callback.answer("Выберите хотя бы один аккаунт.", show_alert=True)
            return
        await callback.answer()
        await state.update_data(inv_selected_accounts=list(selected))
        await state.set_state(InviteUsersFSM.waiting_usernames)
        kb = InlineKeyboardBuilder()
        kb.button(text="📎 Загрузить .txt файл", callback_data="invite:hint:file")
        kb.button(text="❌ Отмена", callback_data="invite:cancel_all")
        kb.adjust(1)
        await callback.message.edit_text(
            f"➕ <b>Пригласить пользователей</b>\n"
            f"Канал: <code>{html.escape(channel_display)}</code>\n"
            f"Аккаунтов: <b>{len(selected)}</b>\n\n"
            "Введите username'ы через запятую или по одному на строку:\n"
            "<code>@user1, @user2, @user3</code>\n\n"
            "Или отправьте <b>.txt файл</b> (до 1 МБ).\n\n"
            "⚠️ Лимит Telegram: <b>200 инвайтов/сутки</b> на аккаунт.\n"
            "Список автоматически распределится между аккаунтами.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return
    else:
        try:
            acc_id_int = int(action)
            if acc_id_int in selected:
                selected.discard(acc_id_int)
            else:
                selected.add(acc_id_int)
        except ValueError:
            pass

    await callback.answer()
    await state.update_data(inv_selected_accounts=list(selected))
    await _show_invite_acc_selector(
        callback.message, active, selected, channel_display, edit=True
    )


@router.callback_query(F.data == "invite:cancel_all")
async def cb_invite_cancel_all(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await callback.message.edit_text(
        "❌ Инвайт отменён.", reply_markup=_back_kb().as_markup()
    )


@router.callback_query(F.data == "invite:hint:file")
async def cb_invite_hint_file(callback: CallbackQuery) -> None:
    await callback.answer("Отправьте .txt файл со списком username'ов", show_alert=True)


@router.message(InviteUsersFSM.waiting_usernames, F.document)
async def fsm_invite_usernames_file(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    doc = message.document
    if not doc or (doc.file_size and doc.file_size > 1_000_000):
        await message.answer("⚠️ Файл слишком большой. Максимум 1 МБ.")
        return
    try:
        fi = await message.bot.get_file(doc.file_id)
        dl = await message.bot.download_file(fi.file_path)
        raw = (dl.read() if hasattr(dl, "read") else bytes(dl)).decode(
            "utf-8", errors="ignore"
        )
    except Exception as e:
        await state.clear()
        await message.answer(f"⚠️ Не удалось прочитать файл: {e}")
        return
    usernames = _parse_username_list(raw)
    if not usernames:
        await message.answer("⚠️ Файл не содержит распознанных usernames.")
        return
    await _show_invite_count_menu(usernames, message, state)


@router.message(InviteUsersFSM.waiting_usernames, F.text)
async def fsm_invite_usernames(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    raw = (message.text or "").replace(",", "\n")
    usernames = [u.strip() for u in raw.split("\n") if u.strip()]
    if not usernames:
        await message.answer("⚠️ Список пуст. Начните заново: /ops")
        return
    await _show_invite_count_menu(usernames, message, state)


async def _show_invite_count_menu(
    usernames: list[str], message, state: FSMContext
) -> None:
    total = len(usernames)
    await state.update_data(usernames=usernames)
    await state.set_state(InviteUsersFSM.choosing_count)

    data = await state.get_data()
    n_acc = max(1, len(data.get("inv_selected_accounts", [])))
    per_acc = max(1, (total + n_acc - 1) // n_acc)

    opts = []
    for n in [50, 100, 200]:
        if n <= total:
            opts.append(n)
    if total not in opts and total > 0:
        opts.append(total)

    kb = InlineKeyboardBuilder()
    for n in opts:
        kb.button(text=f"👥 {n} человек", callback_data=f"invite:count:{n}")
    if total > 200:
        kb.button(
            text=f"📋 Все {total} (батчами по 100)", callback_data="invite:count:all"
        )
    kb.button(text="✏️ Своё число", callback_data="invite:count:custom")
    kb.button(text="❌ Отмена", callback_data="invite:count:cancel")
    kb.adjust(2)

    await message.answer(
        f"➕ <b>Выбор количества для инвайта</b>\n\n"
        f"Загружено пользователей: <b>{total}</b>\n"
        f"Аккаунтов: <b>{n_acc}</b> · По <b>~{per_acc}</b> на аккаунт\n"
        f"⚠️ Лимит Telegram: <b>200/сутки</b> на аккаунт\n\n"
        f"Сколько человек пригласить (всего)?",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("invite:count:"))
async def cb_invite_count(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    action = callback.data.split("invite:count:")[1]

    if action == "cancel":
        await state.clear()
        await callback.message.edit_text(
            "❌ Инвайт отменён.", reply_markup=_back_kb().as_markup()
        )
        return

    if action == "custom":
        await state.set_state(InviteUsersFSM.waiting_custom_count)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data="invite:count:cancel")
        await callback.message.edit_text(
            "✏️ Введите число пользователей для инвайта (1–500):",
            reply_markup=kb.as_markup(),
        )
        return

    data = await state.get_data()
    usernames: list[str] = data.get("usernames", [])

    if action == "all":
        limit = len(usernames)
    else:
        try:
            limit = int(action)
        except ValueError:
            limit = min(100, len(usernames))

    limit = max(1, min(limit, len(usernames)))
    selected_usernames = usernames[:limit]
    fsm_data = dict(data)
    await state.clear()
    await _run_invite_bg(selected_usernames, callback, pool, fsm_data)


@router.message(InviteUsersFSM.waiting_custom_count, F.text)
async def fsm_invite_custom_count(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    try:
        limit = int((message.text or "").strip())
        if limit < 1:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Введите целое число от 1 до 500.")
        return

    data = await state.get_data()
    usernames: list[str] = data.get("usernames", [])
    limit = min(limit, len(usernames))
    selected_usernames = usernames[:limit]
    fsm_data = dict(data)
    await state.clear()
    await _run_invite_bg(selected_usernames, message, pool, fsm_data)


def _format_invite_progress(
    channel_display: str,
    acc_status: dict,
    total_usernames: int,
) -> str:
    lines = [
        "⏳ <b>Мульти-инвайт запущен</b>",
        f"Канал: <code>{html.escape(channel_display)}</code>",
        f"Пользователей: <b>{total_usernames}</b>",
        "",
    ]
    for _aid, st in acc_status.items():
        name = html.escape((st["name"] or "")[:22])
        inv = st["invited"]
        fail = st["failed"]
        tot = st["total"]
        phase = st.get("phase", "")
        icon = "✅" if st["done"] else "⏳"
        err_s = f" ⚠️ {html.escape(st.get('error', '')[:40])}" if st.get("error") else ""
        phase_s = f" [{phase}]" if phase and not st["done"] else ""
        lines.append(f"{icon} <b>{name}</b>: ✅{inv} ❌{fail}/{tot}{phase_s}{err_s}")
    lines += ["", "<i>Для отмены: /tasks</i>"]
    return "\n".join(lines)


async def _run_invite_bg(
    usernames: list[str],
    trigger,
    pool,
    fsm_data: dict,
) -> None:
    """Параллельный инвайт через N аккаунтов.

    Preflight для каждого не-основного аккаунта:
      1. Вступить в канал (join_channel_by_id)
      2. Основной аккаунт повышает его до admin (invite_users=True)
    Затем все аккаунты инвайтят свою часть списка параллельно.
    """
    from services import account_manager as _am

    is_cb = hasattr(trigger, "message")
    user_id = trigger.from_user.id
    msg_obj = trigger.message if is_cb else trigger

    channel_id = fsm_data.get("channel_id")
    access_hash = fsm_data.get("access_hash", 0)
    channel_display = fsm_data.get("channel_display") or str(channel_id)
    selected_acc_ids = fsm_data.get("inv_selected_accounts") or []
    primary_acc_id = fsm_data.get("primary_acc_id")

    if not channel_id:
        await msg_obj.answer(
            "⚠️ Данные сессии потеряны. Начните заново: /ops",
            reply_markup=_back_kb().as_markup(),
        )
        return

    # ── Загружаем аккаунты (включая tg_user_id для promote_to_admin) ──────
    _acc_q = (
        "SELECT a.id, a.tg_user_id, a.session_str, a.first_name, a.phone, "
        "a.device_model, a.system_version, a.app_version, p.proxy_url "
        "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
        "WHERE a.owner_id=$1 AND a.is_active=TRUE AND a.session_str IS NOT NULL"
    )
    try:
        if selected_acc_ids:
            accounts = await pool.fetch(
                _acc_q + " AND a.id = ANY($2::bigint[])",
                user_id,
                selected_acc_ids,
            )
        else:
            accounts = await pool.fetch(_acc_q + " LIMIT 1", user_id)
    except Exception as exc:
        mark_handled_error(f"invite_users accounts: {exc}")
        await msg_obj.answer(
            f"❌ Ошибка загрузки аккаунтов: <code>{html.escape(str(exc)[:200])}</code>",
            reply_markup=_back_kb().as_markup(),
        )
        return

    if not accounts:
        await msg_obj.answer(
            "⚠️ Нет активных аккаунтов. Подключите аккаунты: /accounts",
            reply_markup=_back_kb().as_markup(),
        )
        return

    n_acc = len(accounts)
    total = len(usernames)

    # Определяем основной аккаунт (владелец/admin канала)
    primary_acc = next((a for a in accounts if a["id"] == primary_acc_id), accounts[0])

    # Равномерно распределяем список
    chunk_size = max(1, (total + n_acc - 1) // n_acc)
    slices: list[list[str]] = [
        usernames[i * chunk_size : (i + 1) * chunk_size]
        for i in range(n_acc)
        if usernames[i * chunk_size : (i + 1) * chunk_size]
    ]

    acc_status: dict[int, dict] = {}
    for i, acc in enumerate(accounts):
        sl_len = len(slices[i]) if i < len(slices) else 0
        acc_status[acc["id"]] = {
            "name": acc["first_name"] or acc["phone"] or f"id={acc['id']}",
            "invited": 0,
            "failed": 0,
            "done": False,
            "total": sl_len,
            "phase": "ожидание",
        }

    status_msg = await msg_obj.answer(
        _format_invite_progress(channel_display, acc_status, total),
        parse_mode="HTML",
    )

    async def _upd(aid: int, phase: str) -> None:
        acc_status[aid]["phase"] = phase
        try:
            await status_msg.edit_text(
                _format_invite_progress(channel_display, acc_status, total),
                parse_mode="HTML",
            )
        except Exception:
            pass

    async def _run_one(acc: asyncpg.Record, unames: list[str]) -> None:
        acc_dict = dict(acc)
        aid = acc_dict["id"]
        is_primary = aid == primary_acc["id"]

        # ── Preflight: join + promote (только для не-основных аккаунтов) ──
        if not is_primary:
            await _upd(aid, "🔗 вступление...")
            join_res = await _am.join_channel_by_id(
                acc_dict["session_str"],
                channel_id,
                access_hash,
                _acc=acc_dict,
            )
            if not join_res.get("ok"):
                acc_status[aid]["error"] = join_res.get("error", "join failed")[:50]
                acc_status[aid]["done"] = True
                acc_status[aid]["phase"] = "❌ join"
                await _upd(aid, "❌ join")
                return

            # tg_user_id: из БД или из get_me(), который join_channel_by_id вызвал
            tg_uid = acc_dict.get("tg_user_id") or join_res.get("tg_user_id") or 0

            if tg_uid:
                await _upd(aid, "👑 права...")
                promoted = await _am.promote_to_admin(
                    primary_acc["session_str"],
                    channel_id,
                    tg_uid,
                    _acc=dict(primary_acc),
                    access_hash=access_hash,
                    post_messages=False,
                    invite_users=True,
                )
                if not promoted:
                    log.warning(
                        "invite preflight: promote failed acc=%s uid=%s", aid, tg_uid
                    )
                    # Не прерываем — для групп admin не обязателен
                await asyncio.sleep(random.uniform(1.5, 3.0))

        # ── Invite ────────────────────────────────────────────────────────
        await _upd(aid, "📨 инвайт...")

        async def _progress(_done: int, _total: int, inv: int, fail_cnt: int) -> None:
            acc_status[aid]["invited"] = inv
            acc_status[aid]["failed"] = fail_cnt
            try:
                await status_msg.edit_text(
                    _format_invite_progress(channel_display, acc_status, total),
                    parse_mode="HTML",
                )
            except Exception:
                pass

        try:
            result = await _am.invite_users_to_channel(
                acc_dict["session_str"],
                channel_id,
                unames,
                _acc=acc_dict,
                access_hash=access_hash,
                batch_size=min(100, len(unames)),
                batch_delay=65.0,
                progress_cb=_progress,
            )
            acc_status[aid]["invited"] = result.get("invited", 0)
            acc_status[aid]["failed"] = len(result.get("failed") or [])
            if result.get("error"):
                acc_status[aid]["error"] = str(result["error"])[:50]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            acc_status[aid]["error"] = str(exc)[:50]
            log.exception("invite_one acc=%s: %s", aid, exc)
        finally:
            acc_status[aid]["done"] = True
            acc_status[aid]["phase"] = (
                "✅" if not acc_status[aid].get("error") else "❌"
            )

    async def _bg() -> None:
        tasks: list[asyncio.Task] = []
        try:
            tasks = [
                asyncio.create_task(_run_one(accounts[i], slices[i]))
                for i in range(min(len(accounts), len(slices)))
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            total_inv = sum(v["invited"] for v in acc_status.values())
            total_fail = sum(v["failed"] for v in acc_status.values())
            bot_obj = getattr(trigger, "bot", None) or getattr(msg_obj, "bot", None)
            if bot_obj:
                try:
                    await bot_obj.send_message(
                        user_id,
                        f"❌ <b>Инвайт отменён</b>\n✅ Приглашено: {total_inv}  ❌ Ошибок: {total_fail}",
                        parse_mode="HTML",
                    )
                except Exception:
                    log_exc_swallow(
                        log,
                        f"channel_ops: invite cancel notification failed user_id={user_id}",
                    )
            return
        except Exception as exc:
            log.exception("invite bg FATAL: %s", exc)

        total_inv = sum(v["invited"] for v in acc_status.values())
        total_fail = sum(v["failed"] for v in acc_status.values())
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
        try:
            await status_msg.edit_text(
                f"✅ <b>Инвайт завершён</b>\n\n"
                f"Канал: <code>{html.escape(channel_display)}</code>\n"
                f"Аккаунтов: <b>{n_acc}</b>  Пользователей: <b>{total}</b>\n"
                f"✅ Приглашено: <b>{total_inv}</b>  ❌ Ошибок: <b>{total_fail}</b>",
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )
        except Exception:
            pass

    task = asyncio.create_task(_bg())
    _treg.register(user_id, "invite", f"Инвайт в {channel_display[:30]}", task)


@router.callback_query(ChanCb.filter(F.action == "members_kick"))
async def cb_members_kick(
    callback: CallbackQuery, callback_data: ChanCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(InviteUsersFSM.waiting_channel_id)  # reuse state for kick
    await state.update_data(
        acc_id=callback_data.acc_id,
        channel_id=callback_data.channel_id,
        action="kick",
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    await callback.message.edit_text(
        "🚫 <b>Кикнуть пользователя</b>\n\nВведите Telegram ID пользователя (число):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(InviteUsersFSM.waiting_channel_id)
async def fsm_kick_user_id(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("⚠️ Введите числовой Telegram ID.")
        return
    await state.clear()
    acc = await db.get_account_for_telethon(
        pool, data.get("acc_id"), message.from_user.id
    )
    if not acc:
        await message.answer("⚠️ Аккаунт не найден.")
        return
    from services import account_manager

    ok = await account_manager.kick_from_channel(
        acc["session_str"], data["channel_id"], user_id, _acc=acc
    )
    await message.answer(
        f"✅ Пользователь <code>{user_id}</code> удалён."
        if ok
        else f"❌ Не удалось удалить <code>{user_id}</code>. Проверьте права.",
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# ACCOUNT PROFILE
# ══════════════════════════════════════════════════════════════════════════


@router.callback_query(ChanCb.filter(F.action == "profile_pick"))
async def cb_profile_pick_account(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _STARTER):
        await callback.message.edit_text(
            "🔒 /subscription", reply_markup=_back_kb().as_markup()
        )
        return
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    if not active:
        empty_kb = InlineKeyboardBuilder()
        empty_kb.button(text="📱 Добавить аккаунт", callback_data=AccCb(action="menu"))
        empty_kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
        empty_kb.adjust(1)
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Для редактирования профиля нужен хотя бы один активный аккаунт.\n\n"
            "Добавьте аккаунт через раздел 📱 Аккаунты.",
            parse_mode="HTML",
            reply_markup=empty_kb.as_markup(),
        )
        return
    kb = _account_picker_kb(active, "profile_menu")
    await callback.message.edit_text(
        "🙋 <b>Профиль аккаунта</b>\n\nВыберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "profile_menu"))
async def cb_profile_menu(callback: CallbackQuery, callback_data: ChanCb) -> None:
    await callback.answer()
    acc_id = callback_data.acc_id
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✏️ Изменить имя", callback_data=ChanCb(action="prof_name", acc_id=acc_id)
    )
    kb.button(
        text="📝 Изменить bio", callback_data=ChanCb(action="prof_bio", acc_id=acc_id)
    )
    kb.button(
        text="🔤 Изменить username",
        callback_data=ChanCb(action="prof_uname", acc_id=acc_id),
    )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="profile_pick"))
    kb.adjust(2, 1, 1)
    await callback.message.edit_text(
        "🙋 <b>Профиль аккаунта</b>\n\nВыберите что изменить:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


for _prof_action, _prof_field, _prof_prompt in [
    ("prof_name", "first_name", "✏️ Введите новое <b>имя</b>:"),
    ("prof_bio", "about", "📝 Введите новое <b>bio</b> (до 70 символов):"),
    ("prof_uname", "username", "🔤 Введите новый <b>username</b> аккаунта (без @):"),
]:

    def _make_prof_handler(prof_field, prof_prompt):
        async def _prof_handler(
            callback: CallbackQuery, callback_data: ChanCb, state: FSMContext
        ):
            await callback.answer()
            await state.set_state(UpdateProfileFSM.waiting_value)
            await state.update_data(field=prof_field, acc_id=callback_data.acc_id)
            kb = InlineKeyboardBuilder()
            kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
            await callback.message.edit_text(
                prof_prompt, parse_mode="HTML", reply_markup=kb.as_markup()
            )

        return _prof_handler

    router.callback_query(ChanCb.filter(F.action == _prof_action))(
        _make_prof_handler(_prof_field, _prof_prompt)
    )

    # Single-account profile update is now handled by fsm_update_profile below (bulk=False path)


# ══════════════════════════════════════════════════════════════════════════
# CREATE BOT VIA BOTFATHER
# ══════════════════════════════════════════════════════════════════════════


@router.callback_query(ChanCb.filter(F.action == "botfather_pick"))
async def cb_botfather_pick_account(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _PRO):
        await callback.message.edit_text(
            "🔒 <b>Создание бота — 💎 ПОДПИСКА</b>\n\nОформите: /subscription",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    await state.update_data(bulk_op="botfather", bulk_selected=[])
    await _show_bulk_select(callback, pool, "botfather", set())


@router.message(CreateBotFSM.waiting_count)
async def fsm_botfather_count(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 5):
        await message.answer("⚠️ Введите число от 1 до 5:")
        return
    await state.update_data(bot_count=int(raw))
    await state.set_state(CreateBotFSM.waiting_name)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
    await message.answer(
        "🤖 <b>Создание ботов</b>\n\n"
        "Введите <b>отображаемое имя</b> бота (одинаковое для всех):\n\n"
        "Например: <i>My Sales Bot</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(CreateBotFSM.waiting_name)
async def fsm_botfather_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 64:
        await message.answer("⚠️ Имя от 1 до 64 символов:")
        return
    await state.update_data(bot_name=name)
    await state.set_state(CreateBotFSM.waiting_username)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
    await message.answer(
        f"🤖 Имя: <b>{html.escape(name)}</b>\n\n"
        "Введите <b>базовый username</b> бота.\n"
        "Для нескольких ботов будет добавляться порядковый номер (например: <i>mysalesbot</i>, <i>mysalesbot2</i>):\n\n"
        "Например: <i>mysalesbot</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _botfather_create_bg(
    pool: asyncpg.Pool,
    user_id: int,
    msg,
    accounts: list,
    base_username: str,
    bot_name: str,
    total: int,
) -> None:
    from services import account_manager, op_worker
    from database import db as _db

    results_ok, results_err = [], []
    done_ops = 0
    attempt = 0
    active_accounts = list(accounts)

    # Claim accounts so op_worker/warmup don't use the same sessions in parallel
    _claimed_ids = [int(a["id"]) for a in accounts]
    await op_worker.mark_accounts_in_use(_claimed_ids)

    try:
        for global_i in range(total):
            if not active_accounts:
                results_err.append("❌ Нет доступных аккаунтов")
                done_ops += 1
                continue
            acc_idx = global_i % len(active_accounts)
            acc = active_accounts[acc_idx]
            acc_label = html.escape(acc["first_name"] or acc["phone"])
            from services.username_engine import unique_bot_username
            username = unique_bot_username(base_username, global_i)

            tried_accs: set[int] = set()
            result = None
            for candidate in active_accounts:
                if candidate["id"] in tried_accs:
                    continue
                tried_accs.add(candidate["id"])
                result = await account_manager.create_bot_via_botfather(
                    candidate["session_str"],
                    bot_name,
                    username,
                    _acc=dict(candidate),
                )
                if result.get("banned"):
                    await _db.deactivate_account(
                        pool, candidate["id"], "banned detected in bulk op"
                    )
                    active_accounts = [
                        a for a in active_accounts if a["id"] != candidate["id"]
                    ]
                    continue
                if account_manager.is_dead_session_error(result.get("error")):
                    await _db.deactivate_account(
                        pool, candidate["id"], "dead session detected in bulk op"
                    )
                    active_accounts = [
                        a for a in active_accounts if a["id"] != candidate["id"]
                    ]
                    continue
                if result.get("flood_wait"):
                    continue
                break
            if result is None:
                result = {"error": "нет доступных аккаунтов"}

            if "error" in result:
                results_err.append(
                    f"❌ {acc_label} [{username}]: {html.escape(result['error'][:60])}"
                )
            else:
                token = result.get("token") or ""
                bot_username = result.get("username") or ""
                display_name = result.get("display_name") or bot_name
                # Auto-save to managed_bots so bot appears in "Мои боты" immediately
                saved = False
                if token:
                    try:
                        raw_id = int(token.split(":")[0])
                        saved = await _db.add_bot(
                            pool,
                            token=token,
                            bot_id=raw_id,
                            username=bot_username,
                            first_name=display_name,
                            added_by=user_id,
                        )
                    except Exception:
                        pass
                saved_icon = "💾" if saved else "⚠️"
                results_ok.append(
                    f"✅ {acc_label}: @{html.escape(bot_username)} {saved_icon}"
                )
            done_ops += 1
            try:
                await msg.edit_text(
                    _progress_text(
                        "Создание ботов...",
                        done_ops,
                        total,
                        len(results_ok),
                        len(results_err),
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(log, "Сбой обновления прогресса создания ботов")
            if attempt >= 4:
                attempt = 0
            else:
                attempt += 1
            flood = result.get("flood_wait", 0)
            await asyncio.sleep(max(backoff(attempt, base=2.0, cap=60.0), flood))
    except asyncio.CancelledError:
        log.info("_botfather_create_bg: отменено")
        raise
    except Exception:
        log_exc_swallow(log, "_botfather_create_bg: неожиданная ошибка")
    finally:
        await op_worker.release_accounts(_claimed_ids)

    lines = [f"🤖 <b>Результаты создания ботов</b> ({len(results_ok)}/{total})\n"]
    lines += results_ok + results_err
    try:
        await msg.edit_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=_back_kb().as_markup()
        )
    except Exception:
        log_exc_swallow(log, "_botfather_create_bg: сбой финального отчёта")


@router.message(CreateBotFSM.waiting_username)
async def fsm_botfather_username(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    base_username = (message.text or "").strip().lstrip("@")
    if not base_username or len(base_username) < 5:
        await message.answer("⚠️ Username минимум 5 символов.")
        return
    data = await state.get_data()
    await state.clear()

    selected_ids = data.get("bulk_selected", [])
    bot_count = data.get("bot_count", 1)

    if not selected_ids and data.get("acc_id"):
        selected_ids = [data["acc_id"]]

    try:
        accounts = await pool.fetch(
            "SELECT a.id, a.session_str, a.first_name, a.phone, "
            "a.device_model, a.system_version, a.app_version, p.proxy_url "
            "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
            "WHERE a.owner_id=$1 AND a.id = ANY($2::bigint[]) AND a.session_str IS NOT NULL",
            message.from_user.id,
            selected_ids,
        )
    except Exception as exc:
        mark_handled_error(f"botfather_create accounts: {exc}")
        await message.answer(
            f"❌ Ошибка загрузки аккаунтов: <code>{html.escape(str(exc)[:200])}</code>"
        )
        return
    if not accounts:
        await message.answer("⚠️ Аккаунты не найдены. Начните заново: /ops")
        return

    total = len(accounts) * bot_count
    msg = await message.answer(
        _progress_text("Создание ботов...", 0, total, 0, 0),
        parse_mode="HTML",
    )
    task = asyncio.create_task(
        _botfather_create_bg(
            pool,
            message.from_user.id,
            msg,
            list(accounts),
            base_username,
            data.get("bot_name", ""),
            total,
        )
    )
    _treg.register(
        message.from_user.id,
        "botfather_create",
        f"Создание {total} ботов через BotFather",
        task,
    )


@router.callback_query(F.data.startswith("add_bot_token:"))
async def cb_add_bot_token(
    callback: CallbackQuery, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    await callback.answer()
    token = callback.data.split(":", 1)[1]
    from database import db as _db
    from services import bot_api as _bot_api
    from bot.keyboards import bot_menu

    progress = await callback.message.answer("⏳ Добавляю бота...")
    bot_info = await _bot_api.get_me(http, token)
    if not bot_info:
        await progress.edit_text(
            "❌ Не удалось получить информацию о боте. Токен недействителен.",
            reply_markup=_back_kb().as_markup(),
        )
        return
    added = await _db.add_bot(
        pool,
        token=token,
        bot_id=bot_info["id"],
        username=bot_info.get("username", ""),
        first_name=bot_info.get("first_name", ""),
        added_by=callback.from_user.id,
    )
    safe = (bot_info.get("username") or bot_info.get("first_name", "")).replace(
        "&", "&amp;"
    )
    if added:
        await progress.edit_text(
            f"✅ Бот @{safe} добавлен в платформу!",
            parse_mode="HTML",
            reply_markup=bot_menu(bot_info["id"], username=bot_info.get("username")),
        )
    else:
        await progress.edit_text(
            f"⚠️ Бот @{safe} уже добавлен в вашу платформу.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )


# ══════════════════════════════════════════════════════════════════════════
# REACTIONS
# ══════════════════════════════════════════════════════════════════════════


@router.callback_query(ChanCb.filter(F.action == "react_pick"))
async def cb_react_pick_account(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _STARTER):
        await callback.message.edit_text(
            "🔒 /subscription", reply_markup=_back_kb().as_markup()
        )
        return
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    if not active:
        empty_kb = InlineKeyboardBuilder()
        empty_kb.button(text="📱 Добавить аккаунт", callback_data=AccCb(action="menu"))
        empty_kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
        empty_kb.adjust(1)
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Для отправки реакций нужен хотя бы один активный аккаунт.\n\n"
            "Добавьте аккаунт через раздел 📱 Аккаунты.",
            parse_mode="HTML",
            reply_markup=empty_kb.as_markup(),
        )
        return
    kb = _account_picker_kb(active, "react_dialogs")
    await callback.message.edit_text(
        "👍 <b>Реакция на пост</b>\n\nВыберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "react_dialogs"))
async def cb_react_dialogs(
    callback: CallbackQuery,
    callback_data: ChanCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    acc = await db.get_account_for_telethon(
        pool, callback_data.acc_id, callback.from_user.id
    )
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer("⏳ Загружаю каналы...")
    from services import account_manager

    try:
        dialogs = await account_manager.get_dialogs(
            acc["session_str"], limit=30, _acc=acc
        )
    except Exception as _e:
        log.warning("react_dialogs get_dialogs failed acc=%s: %s", acc.get("id"), _e)
        await callback.message.edit_text(
            f"❌ Не удалось получить список каналов: <code>{html.escape(str(_e)[:150])}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    await state.update_data(acc_id=callback_data.acc_id)
    if not dialogs:
        await callback.message.edit_text(
            "ℹ️ <b>Нет каналов для реакции</b>\n\n"
            "Этот аккаунт не состоит ни в одном канале или группе.\n\n"
            "Вступите в канал через 🔗 <b>Вступить</b> или вставьте ссылку на пост напрямую.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    for d in dialogs[:20]:
        label = f"{'📢' if d['type'] == 'channel' else '👥'} {d['title'][:30]}"
        kb.button(
            text=label,
            callback_data=ChanCb(
                action="react_channel", acc_id=callback_data.acc_id, channel_id=d["id"]
            ),
        )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "👍 <b>Выберите канал:</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "react_channel"))
async def cb_react_channel(
    callback: CallbackQuery, callback_data: ChanCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(SendReactionFSM.waiting_msg_id)
    await state.update_data(
        acc_id=callback_data.acc_id, channel_id=callback_data.channel_id
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    await callback.message.edit_text(
        "👍 <b>Сообщение для реакции</b>\n\n"
        "Введите <b>ссылку на пост</b> или <b>ID сообщения</b>:\n\n"
        "• <code>https://t.me/channelname/123</code>\n"
        "• <code>https://t.me/c/1234567890/123</code> (приватный канал)\n"
        "• <code>123</code> (ID сообщения в выбранном канале)",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(SendReactionFSM.waiting_msg_id)
async def fsm_react_msg_id(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    channel_ref, msg_id = _parse_tme_post_link(text)
    if msg_id is not None:
        # Link provided — override channel ref if parsed from link
        updates: dict = {"msg_id": msg_id}
        if channel_ref is not None:
            updates["channel_ref"] = channel_ref
        await state.update_data(**updates)
    else:
        try:
            msg_id = int(text)
        except ValueError:
            await message.answer(
                "⚠️ Укажите ссылку на пост или числовой ID.\n\n"
                "Примеры:\n"
                "• <code>https://t.me/channelname/123</code>\n"
                "• <code>123</code>",
                parse_mode="HTML",
            )
            return
        await state.update_data(msg_id=msg_id)
    await state.set_state(SendReactionFSM.choosing_emoji)
    kb = InlineKeyboardBuilder()
    for emoji in REACTION_EMOJIS:
        kb.button(text=emoji, callback_data=f"chan:do_react:{emoji}")
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    kb.adjust(5, 5, 1)
    await message.answer(
        "👍 <b>Выберите реакцию:</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("chan:do_react:"))
async def cb_do_react(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    parts = callback.data.split(":", 2)
    emoji = parts[2] if len(parts) >= 3 else "👍"
    data = await state.get_data()
    await state.clear()
    acc = await db.get_account_for_telethon(
        pool, data.get("acc_id"), callback.from_user.id
    )
    if not acc:
        await callback.message.edit_text(
            "⚠️ Аккаунт не найден.", reply_markup=_back_kb().as_markup()
        )
        return
    # channel_ref overrides channel_id when post link was pasted
    channel = data.get("channel_ref") or data.get("channel_id")
    from services import account_manager

    ok = await account_manager.send_reaction(
        acc["session_str"], channel, data["msg_id"], emoji, _acc=acc
    )
    await callback.message.edit_text(
        f"✅ Реакция {emoji} отправлена!" if ok else "❌ Ошибка отправки реакции.",
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# REPORT CONTENT
# ══════════════════════════════════════════════════════════════════════════


@router.callback_query(ChanCb.filter(F.action == "report_pick"))
async def cb_report_pick_account(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    if not active:
        empty_kb = InlineKeyboardBuilder()
        empty_kb.button(text="📱 Добавить аккаунт", callback_data=AccCb(action="menu"))
        empty_kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
        empty_kb.adjust(1)
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Для подачи жалобы нужен хотя бы один активный аккаунт.\n\n"
            "Добавьте аккаунт через раздел 📱 Аккаунты.",
            parse_mode="HTML",
            reply_markup=empty_kb.as_markup(),
        )
        return
    kb = _account_picker_kb(active, "report_start")
    await callback.message.edit_text(
        "🚨 <b>Пожаловаться на контент</b>\n\nВыберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "report_start"))
async def cb_report_account_chosen(
    callback: CallbackQuery, callback_data: ChanCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(ReportFSM.waiting_peer)
    await state.update_data(acc_id=callback_data.acc_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    await callback.message.edit_text(
        "🚨 <b>Жалоба</b>\n\nВведите username канала/пользователя:\n<code>@username</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(ReportFSM.waiting_peer)
async def fsm_report_peer(message: Message, state: FSMContext) -> None:
    peer = (message.text or "").strip()
    await state.update_data(peer=peer)
    await state.set_state(ReportFSM.choosing_reason)
    kb = InlineKeyboardBuilder()
    for key, label in REPORT_REASONS.items():
        kb.button(text=label, callback_data=f"chan:report_reason:{key}")
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    kb.adjust(2, 2, 2, 1)
    await message.answer(
        f"🚨 Жалоба на: <code>{html.escape(peer)}</code>\n\nВыберите причину:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("chan:report_reason:"))
async def cb_report_reason(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    reason = callback.data.split(":", 2)[2] if ":" in callback.data else "spam"
    data = await state.get_data()
    await state.clear()
    acc = await db.get_account_for_telethon(
        pool, data.get("acc_id"), callback.from_user.id
    )
    if not acc:
        await callback.message.edit_text(
            "⚠️ Аккаунт не найден.", reply_markup=_back_kb().as_markup()
        )
        return
    from services import account_manager

    msg_pool = _REPORT_MESSAGES.get(reason, _REPORT_MESSAGES["other"])
    report_msg = random.choice(msg_pool)
    ok = await account_manager.report_peer(
        acc["session_str"], data["peer"], reason, message=report_msg, _acc=acc
    )
    label = REPORT_REASONS.get(reason, reason)
    await callback.message.edit_text(
        f"✅ <b>Жалоба отправлена!</b>\n\nПричина: {label}\nОбъект: <code>{html.escape(data['peer'])}</code>"
        if ok
        else f"❌ <b>Ошибка отправки жалобы</b>\n\n"
        f"Объект: <code>{html.escape(data.get('peer', '?'))}</code>\n\n"
        "Возможные причины:\n"
        "• Username не существует или написан с ошибкой\n"
        "• Аккаунт не имеет доступа к этому контенту\n"
        "• Telegram временно ограничил жалобы — повторите позже",
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )


# ── Bulk Report — multi-account ───────────────────────────────────────────


@router.callback_query(ChanCb.filter(F.action == "bulk_report"))
async def cb_bulk_report_start(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    # Проверка доступа к Strike Module
    try:
        await pool.execute(
            "CREATE TABLE IF NOT EXISTS strike_access "
            "(user_id BIGINT PRIMARY KEY, purchased_at TIMESTAMPTZ DEFAULT now(), "
            "payment_ref TEXT, granted_by BIGINT)"
        )
        has_strike = await pool.fetchrow(
            "SELECT 1 FROM strike_access WHERE user_id=$1", callback.from_user.id
        )
    except Exception:
        has_strike = None
    if not has_strike:
        from bot.callbacks import StrikeCb

        kb = InlineKeyboardBuilder()
        kb.button(
            text="⚔️ Купить Strike Module — $250 USDT",
            callback_data=StrikeCb(action="buy"),
        )
        kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
        kb.adjust(1)
        await callback.message.edit_text(
            "⚔️ <b>Strike Module</b>\n\n"
            "Функция многоаккаунтной зачистки нелегального контента "
            "доступна по отдельной лицензии.\n\n"
            "💰 Стоимость: <b>$250 USDT</b> · Пожизненный доступ",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    if not active:
        empty_kb = InlineKeyboardBuilder()
        empty_kb.button(text="📱 Добавить аккаунт", callback_data=AccCb(action="menu"))
        empty_kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
        empty_kb.adjust(1)
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Для массовой подачи жалоб нужен хотя бы один активный аккаунт.\n\n"
            "Добавьте аккаунт через раздел 📱 Аккаунты.",
            parse_mode="HTML",
            reply_markup=empty_kb.as_markup(),
        )
        return
    await state.update_data(active_ids=[a["id"] for a in active])
    kb = InlineKeyboardBuilder()
    kb.button(text="👤 Один ресурс", callback_data=ChanCb(action="br_mode_single"))
    kb.button(text="📋 Список ресурсов", callback_data=ChanCb(action="br_mode_batch"))
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    kb.adjust(2, 1)
    await callback.message.edit_text(
        f"🚨 <b>Жалоба с нескольких аккаунтов</b>\n\n"
        f"Доступно аккаунтов: <b>{len(active)}</b>\n\n"
        "Выберите режим:\n"
        "• <b>Один ресурс</b> — жалоба на один канал/бот\n"
        "• <b>Список ресурсов</b> — вставить несколько username сразу",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "br_mode_single"))
async def cb_br_mode_single(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    from bot.handlers.strike import _has_access

    if not await _has_access(pool, callback.from_user.id):
        await callback.answer("Нет доступа к Strike Module.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(BulkReportFSM.waiting_peer)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    await callback.message.edit_text(
        "🚨 <b>Жалоба — один ресурс</b>\n\n"
        "Введите username или ссылку:\n"
        "<code>@username</code>\n"
        "<code>https://t.me/username</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "br_mode_batch"))
async def cb_br_mode_batch(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    from bot.handlers.strike import _has_access

    if not await _has_access(pool, callback.from_user.id):
        await callback.answer("Нет доступа к Strike Module.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(BulkReportFSM.waiting_peers_batch)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    await callback.message.edit_text(
        "🚨 <b>Жалоба — список ресурсов</b>\n\n"
        "Вставьте список через новую строку или запятую:\n\n"
        "<code>@drugs_channel\n"
        "@scam_bot\n"
        "https://t.me/illegal_shop</code>\n\n"
        "Каждый ресурс получит жалобы от всех выбранных аккаунтов.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


def _normalize_peer(p: str) -> str:
    p = p.strip().rstrip("/")
    if p.startswith("https://t.me/"):
        p = "@" + p.split("t.me/")[-1].split("?")[0].rstrip("/")
    elif not p.startswith("@"):
        p = "@" + p.lstrip("@")
    return p


def _reason_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    # Быстрые пресеты (7 штук: drugs/terrorism/fraud/csam/weapons/darknet/escort)
    for key, (reason, label) in _REPORT_PRESETS.items():
        kb.button(text=label, callback_data=f"chan:br_preset:{key}")
    # Стандартные причины TG (6 штук)
    for key, label in REPORT_REASONS.items():
        kb.button(text=label, callback_data=f"chan:br_reason:{key}")
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    # 7 пресетов + 6 причин + 1 отмена = 14 кнопок
    kb.adjust(4, 3, 3, 3, 1)
    return kb


@router.message(BulkReportFSM.waiting_peer)
async def fsm_bulk_report_peer(message: Message, state: FSMContext) -> None:
    peer = _normalize_peer(message.text or "")
    if not peer or peer == "@":
        await message.answer("⚠️ Введите username.")
        return
    await state.update_data(peer=peer, peers=[peer])
    await state.set_state(BulkReportFSM.choosing_reason)
    await message.answer(
        f"🚨 Жалоба на: <code>{html.escape(peer)}</code>\n\nВыберите тип нарушения:",
        parse_mode="HTML",
        reply_markup=_reason_kb().as_markup(),
    )


@router.message(BulkReportFSM.waiting_peers_batch)
async def fsm_bulk_report_peers_batch(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").replace(",", "\n")
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if not lines:
        await message.answer("⚠️ Введите хотя бы один username.")
        return
    peers = [_normalize_peer(l) for l in lines if l]
    peers = list(dict.fromkeys(p for p in peers if len(p) > 1))  # дедупликация
    await state.update_data(peer=peers[0], peers=peers)
    await state.set_state(BulkReportFSM.choosing_reason)
    preview = "\n".join(f"• <code>{html.escape(p)}</code>" for p in peers[:5])
    if len(peers) > 5:
        preview += f"\n<i>...и ещё {len(peers) - 5}</i>"
    await message.answer(
        f"🚨 Жалоба на <b>{len(peers)}</b> ресурс(ов):\n{preview}\n\n"
        "Выберите тип нарушения:",
        parse_mode="HTML",
        reply_markup=_reason_kb().as_markup(),
    )


@router.callback_query(F.data.startswith("chan:br_preset:"))
async def cb_br_preset(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    preset_key = callback.data.split(":", 2)[2]
    reason, _ = _REPORT_PRESETS.get(preset_key, ("other", ""))
    await state.update_data(reason=reason, preset=preset_key)
    await state.set_state(BulkReportFSM.selecting_accounts)
    data = await state.get_data()
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    selected = [a["id"] for a in active]
    await state.update_data(selected_ids=selected)
    peers = data.get("peers", [data.get("peer", "")])
    await _show_bulk_report_account_picker(
        callback.message,
        active,
        selected,
        peers[0],
        reason,
        edit=True,
        extra_info=f"Ресурсов: <b>{len(peers)}</b>" if len(peers) > 1 else None,
    )


@router.callback_query(F.data.startswith("chan:br_reason:"))
async def cb_bulk_report_reason(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    reason = callback.data.split(":", 2)[2]
    data = await state.get_data()
    await state.update_data(reason=reason)
    await state.set_state(BulkReportFSM.selecting_accounts)

    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    selected = [a["id"] for a in active]
    await state.update_data(selected_ids=selected)

    peers = data.get("peers", [data.get("peer", "")])
    await _show_bulk_report_account_picker(
        callback.message,
        active,
        selected,
        peers[0],
        reason,
        edit=True,
        extra_info=f"Ресурсов: <b>{len(peers)}</b>" if len(peers) > 1 else None,
    )


async def _show_bulk_report_account_picker(
    message,
    accounts: list,
    selected: list,
    peer: str,
    reason: str,
    edit: bool = False,
    extra_info: str | None = None,
) -> None:
    label = REPORT_REASONS.get(reason, reason)
    lines = [
        "🚨 <b>Выберите аккаунты для жалобы</b>",
        f"Объект: <code>{html.escape(peer)}</code>",
    ]
    if extra_info:
        lines.append(extra_info)
    lines += [
        f"Причина: {label}",
        f"Выбрано: <b>{len(selected)}/{len(accounts)}</b>\n",
    ]
    kb = InlineKeyboardBuilder()
    for acc in accounts:
        acc_id = acc["id"]
        is_sel = acc_id in selected
        phone = acc.get("phone", "")[-4:] if acc.get("phone") else "----"
        name = acc.get("first_name") or f"acc{acc_id}"
        mark = "✅" if is_sel else "☐"
        kb.button(
            text=f"{mark} {html.escape(name)} ···{phone}",
            callback_data=f"chan:br_toggle:{acc_id}",
        )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(text="✅ Выбрать все", callback_data="chan:br_selall"),
        InlineKeyboardButton(text="☐ Снять все", callback_data="chan:br_selno"),
    )
    if selected:
        kb.row(
            InlineKeyboardButton(
                text=f"🚀 Отправить жалобы ({len(selected)} акк)",
                callback_data="chan:br_confirm",
            )
        )
    kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data="chan:br_cancel"))

    text = "\n".join(lines)
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("chan:br_toggle:"))
async def cb_br_toggle(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    acc_id = int(callback.data.split(":")[-1])
    data = await state.get_data()
    selected = list(data.get("selected_ids", []))
    if acc_id in selected:
        selected.remove(acc_id)
    else:
        selected.append(acc_id)
    await state.update_data(selected_ids=selected)
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    peers = data.get("peers", [data.get("peer", "")])
    extra = f"Ресурсов: <b>{len(peers)}</b>" if len(peers) > 1 else None
    peer_display = data.get("peer") or (data.get("peers") or [""])[0]
    await _show_bulk_report_account_picker(
        callback.message,
        active,
        selected,
        peer_display,
        data.get("reason", "spam"),
        edit=True,
        extra_info=extra,
    )


@router.callback_query(F.data == "chan:br_selall")
async def cb_br_selall(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    selected = [a["id"] for a in active]
    await state.update_data(selected_ids=selected)
    data = await state.get_data()
    peers = data.get("peers", [data.get("peer", "")])
    extra = f"Ресурсов: <b>{len(peers)}</b>" if len(peers) > 1 else None
    peer_display = data.get("peer") or (data.get("peers") or [""])[0]
    await _show_bulk_report_account_picker(
        callback.message,
        active,
        selected,
        peer_display,
        data.get("reason", "spam"),
        edit=True,
        extra_info=extra,
    )


@router.callback_query(F.data == "chan:br_selno")
async def cb_br_selno(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    await state.update_data(selected_ids=[])
    data = await state.get_data()
    peers = data.get("peers", [data.get("peer", "")])
    extra = f"Ресурсов: <b>{len(peers)}</b>" if len(peers) > 1 else None
    peer_display = data.get("peer") or (data.get("peers") or [""])[0]
    await _show_bulk_report_account_picker(
        callback.message,
        active,
        [],
        peer_display,
        data.get("reason", "spam"),
        edit=True,
        extra_info=extra,
    )


@router.callback_query(F.data == "chan:br_cancel")
async def cb_br_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    await callback.message.edit_text(
        "❌ Жалоба отменена.",
        reply_markup=_back_kb().as_markup(),
    )


@router.callback_query(F.data == "chan:br_confirm")
async def cb_br_confirm(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    data = await state.get_data()
    await state.clear()

    peers = data.get("peers") or [data.get("peer", "")]
    peers = [p for p in peers if p]
    reason = data.get("reason", "spam")
    preset = data.get("preset") or None
    selected_ids = data.get("selected_ids", [])
    label = REPORT_REASONS.get(reason, reason)

    accounts = await _get_accounts(pool, callback.from_user.id)
    # Convert asyncpg.Record to plain dict to prevent KeyError in strike_engine
    chosen = [dict(a) for a in accounts if a["id"] in selected_ids and a["is_active"]]

    if not chosen:
        await callback.message.edit_text(
            "⚠️ Нет выбранных аккаунтов.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    from services import strike_engine

    # ═══════════════════════════════════════════════════════════════════════
    # ФАЗА 0: PRE-FLIGHT — проверка аккаунтов перед атакой
    # ═══════════════════════════════════════════════════════════════════════
    viable = strike_engine.preflight_accounts(chosen)
    if not viable:
        await callback.message.edit_text(
            "⚠️ <b>Нет доступных аккаунтов.</b>\n\n"
            "Все аккаунты либо в кулдауне, либо имеют слишком низкий trust_score.\n"
            "Подождите и попробуйте снова.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    # Warmup overlap guard: exclude accounts with active warmup plans
    try:
        _warmup_rows = await pool.fetch(
            "SELECT account_id FROM account_warmup_plans WHERE owner_id=$1 AND status='active'",
            callback.from_user.id,
        )
        _warming_ids = {r["account_id"] for r in _warmup_rows}
        if _warming_ids:
            _before = len(viable)
            viable = [a for a in viable if a.get("id") not in _warming_ids]
            if len(viable) < _before:
                log.warning(
                    "channel_ops strike: excluded %d warmup accounts",
                    _before - len(viable),
                )
    except Exception:
        pass

    if not viable:
        await callback.message.edit_text(
            "⚠️ <b>Нет доступных аккаунтов.</b>\n\n"
            "Все аккаунты на прогреве или в кулдауне.\n"
            "Завершите прогрев или добавьте новые аккаунты.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    skipped = len(chosen) - len(viable)
    preflight_note = ""
    if skipped > 0:
        preflight_note = (
            f"\n⚠️ Пропущено аккаунтов в кулдауне/прогреве: <b>{skipped}</b>"
        )

    # ═══════════════════════════════════════════════════════════════════════
    # ФАЗА 1: РАЗВЕДКА — лучшим аккаунтом карту цели
    # ═══════════════════════════════════════════════════════════════════════
    status_msg = await callback.message.edit_text(
        f"⚔️ <b>Strike v2 — Инициализация</b>\n\n"
        f"🛡 <b>Pre-flight:</b> {len(viable)}/{len(chosen)} аккаунтов готовы{preflight_note}\n"
        f"🔍 <b>Фаза 1: Разведка цели...</b>\n"
        f"Цели: <b>{len(peers)}</b> · Лучший разведчик: trust={viable[0].get('trust_score', 0):.1f}\n"
        f"Маппинг: администраторы, узлы сети, боты, сообщения...",
        parse_mode="HTML",
    )

    all_intel: dict[str, dict] = {}
    for peer in peers:
        try:
            from services import account_manager

            intel = await account_manager.strike_map_target(
                viable[0]["session_str"], peer, _acc=viable[0]
            )
        except Exception as e:
            intel = {
                "error": str(e)[:100],
                "admin_ids": [],
                "mentioned_usernames": [],
                "bot_usernames": [],
                "pinned_msg_ids": [],
                "latest_msg_ids": [],
                "title": peer,
                "members": 0,
                "linked_group_id": None,
            }
        all_intel[peer] = intel

    # План атаки: распределяем аккаунты по волнам
    waves = strike_engine.plan_waves(viable, num_waves=3)
    wave_sizes = [len(w) for w in waves]
    wave_desc = " → ".join(f"W{i + 1}:{s}" for i, s in enumerate(wave_sizes) if s > 0)

    intel_lines = ["⚔️ <b>Strike v2 — Разведка завершена</b>\n"]
    for peer, intel in all_intel.items():
        nodes = (
            len(intel.get("admin_ids", []))
            + len(intel.get("mentioned_usernames", []))
            + len(intel.get("bot_usernames", []))
            + (1 if intel.get("linked_group_id") else 0)
        )
        intel_lines.append(
            f"🎯 <code>{html.escape(peer)}</code>\n"
            f"   📋 {intel.get('title', '?')} · {intel.get('members', 0):,} подписчиков\n"
            f"   👤 Админов: <b>{len(intel.get('admin_ids', []))}</b> · "
            f"Узлов сети: <b>{nodes}</b>\n"
            f"   📌 Закреплённых: <b>{len(intel.get('pinned_msg_ids', []))}</b> · "
            f"Сообщений: <b>{len(intel.get('latest_msg_ids', []))}</b>"
        )
    intel_lines.append(f"\n⚡ <b>План атаки:</b> {wave_desc}")
    intel_lines.append(
        f"🔄 Волны с паузами {strike_engine._WAVE_COOLDOWN[0]}-{strike_engine._WAVE_COOLDOWN[1]}с"
    )
    intel_lines.append("\n<i>Фазы 2-6 запускаются в фоне. Для отмены: /tasks</i>")
    try:
        await status_msg.edit_text("\n".join(intel_lines), parse_mode="HTML")
    except Exception:
        log_exc_swallow(log, "Сбой отправки статуса разведки Strike")

    task = asyncio.create_task(
        _strike_bg_v2(
            pool=pool,
            status_msg=status_msg,
            bot=callback.bot,
            user_id=callback.from_user.id,
            peers=peers,
            viable=viable,
            waves=waves,
            all_intel=all_intel,
            reason=reason,
            preset=preset,
            label=label,
        )
    )
    _treg.register(
        callback.from_user.id,
        "strike",
        f"Strike v2 {', '.join(peers[:2])[:40]}",
        task,
    )


async def _strike_bg_v2(
    pool,
    status_msg,
    bot,
    user_id: int,
    peers: list,
    viable: list,
    waves: list,
    all_intel: dict,
    reason: str,
    preset: str | None,
    label: str,
) -> None:
    """Фоновое выполнение Strike v2 — эшелонированная атака с верификацией."""
    from services import strike_engine

    # Закреплённый заголовок — не зависит от status_msg.text (устаревает после edit_text)
    _header = (
        f"⚔️ <b>Strike v2</b> — {html.escape(label)}\n"
        f"🎯 Целей: <b>{len(peers)}</b> · Аккаунтов: <b>{len(viable)}</b>"
    )

    async def _progress(phase: str, detail: str) -> None:
        try:
            await status_msg.edit_text(
                f"{_header}\n\n"
                f"⚡ <b>{html.escape(detail)}</b>\n\n"
                f"<i>Для отмены: /tasks</i>",
                parse_mode="HTML",
            )
        except Exception:
            log_exc_swallow(log, "Сбой обновления статуса фазы Strike")

    try:
        await _progress(
            "strike",
            f"Фаза 2-4: Эшелонированная атака — {len(peers)} целей, {len(viable)} аккаунтов",
        )

        # Загрузить режим пользователя
        strike_mode_row = await pool.fetchrow(
            "SELECT mode FROM strike_access WHERE user_id=$1", user_id
        )
        strike_mode = (
            strike_mode_row.get("mode", "normal") if strike_mode_row else "normal"
        )

        plan = strike_engine.StrikePlan(
            targets=peers,
            accounts=viable,
            reason=reason,
            preset=preset,
            label=label,
            intel=all_intel,
            waves=waves,
            started_at=time.time(),
            phase="strike",
            mode=strike_mode,
            owner_id=user_id,
        )
        results = await strike_engine.staggered_strike(
            plan, progress_cb=_progress, pool=pool
        )

        # ── Сохранение в историю (до верификации — не теряем при рестарте) ──
        _history_ids: dict[str, int] = {}
        for r in results:
            try:
                _row_id = await pool.fetchval(
                    """INSERT INTO strike_history(owner_id, target, reason, preset,
                       accounts_used, peer_reported, msgs_reported, msgs_fetched,
                       pinned_reported, admins_reported, network_nodes, network_reports,
                       blocked, verified_down, duration_s, abuse_form_ok, spambot_escalation)
                       VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
                       RETURNING id""",
                    user_id,
                    r.target,
                    reason,
                    preset or None,
                    r.unique_accounts,
                    r.peer_reported,
                    r.msgs_reported,
                    getattr(r, "msgs_fetched", 0),
                    r.pinned_reported,
                    r.admins_reported,
                    r.network_nodes,
                    r.network_reports,
                    r.blocked,
                    r.verified_down,
                    r.duration_s,
                    r.abuse_form_ok,
                    r.spambot_escalation,
                )
                if _row_id:
                    _history_ids[r.target] = _row_id
            except Exception:
                log_exc_swallow(log, "Сбой сохранения истории Strike")

        # ── Фаза 5: Верификация ──
        await _progress("verify", "Фаза 5: Проверка результата...")
        _verify_acc = viable[0] if viable else None
        for r in results:
            try:
                if _verify_acc is None:
                    r.verified_down = None
                    continue
                is_down = await strike_engine.verify_target_takedown(
                    _verify_acc,
                    r.target,
                    max_attempts=1,
                    delay_range=(10, 20),
                )
                r.verified_down = is_down
                if r.target in _history_ids:
                    try:
                        await pool.execute(
                            "UPDATE strike_history SET verified_down=$1 WHERE id=$2",
                            r.verified_down,
                            _history_ids[r.target],
                        )
                    except Exception:
                        log_exc_swallow(log, "Сбой обновления verified_down в истории")
            except Exception:
                r.verified_down = None

        # ── Финальный отчёт ──
        summary_text = strike_engine.format_strike_summary(results)
        summary_text += "\n\n" + _DISCLAIMER
        kb = InlineKeyboardBuilder()
        kb.button(text="📋 История", callback_data="strike:history")
        kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
        kb.adjust(2)
        await status_msg.edit_text(
            summary_text, parse_mode="HTML", reply_markup=kb.as_markup()
        )

    except asyncio.CancelledError as _ce:
        _is_user = bool(_ce.args and _ce.args[0] == "user_requested")
        _cancel_msg = (
            "⚔️ <b>Strike отменён пользователем.</b>"
            if _is_user
            else "⚔️ <b>Strike прерван (перезапуск сервиса).</b>\n\n<i>Повторите операцию.</i>"
        )
        try:
            await bot.send_message(user_id, _cancel_msg, parse_mode="HTML")
        except Exception:
            log_exc_swallow(
                log, "Сбой отправки уведомления об отмене Strike", user_id=user_id
            )
    except Exception as exc:
        log.exception("_strike_bg_v2 error user=%s: %s", user_id, exc)
        try:
            await bot.send_message(
                user_id,
                f"⚠️ <b>Ошибка Strike</b>\n\n<code>{html.escape(str(exc)[:200])}</code>",
                parse_mode="HTML",
            )
        except Exception:
            log_exc_swallow(
                log, "Сбой отправки уведомления об ошибке Strike", user_id=user_id
            )


async def _strike_bg(
    pool,
    status_msg,
    bot,
    user_id: int,
    peers: list,
    chosen: list,
    all_intel: dict,
    reason: str,
    preset: str | None,
    label: str,
) -> None:
    """Обратная совместимость — делегирует в _strike_bg_v2."""
    from services import strike_engine

    viable = strike_engine.preflight_accounts(chosen)
    if not viable:
        viable = chosen
    # Warmup overlap guard (best-effort, no pool available in legacy path)
    waves = strike_engine.plan_waves(viable, num_waves=3)
    await _strike_bg_v2(
        pool=pool,
        status_msg=status_msg,
        bot=bot,
        user_id=user_id,
        peers=peers,
        viable=viable,
        waves=waves,
        all_intel=all_intel,
        reason=reason,
        preset=preset,
        label=label,
    )


# ══════════════════════════════════════════════════════════════════════════
# BULK MENU (mass operations across ALL active accounts)
# ══════════════════════════════════════════════════════════════════════════


@router.callback_query(ChanCb.filter(F.action == "bulk_menu"))
async def cb_bulk_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _PRO):
        await callback.message.edit_text(
            "🔒 <b>Массовые операции — 💎 ПОДПИСКА</b>\n\nОформите: /subscription",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    try:
        accounts = await pool.fetch(
            "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE",
            callback.from_user.id,
        )
    except Exception:
        accounts = []
    count = len(accounts)
    await callback.message.edit_text(
        f"⚡ <b>Массовые операции</b>\n\n"
        f"Активных аккаунтов: <b>{count}</b>\n\n"
        "Выберите операцию — затем выберете конкретные аккаунты (или все сразу):\n"
        "• 📢 Создать канал/группу — создаст на выбранных аккаунтах\n"
        "• 🔗 Вступить в канал — все выбранные вступят по ссылке\n"
        "• 🚪 Выйти из канала — все выбранные покинут канал\n"
        "• 📤 Опубликовать пост — опубликует от всех выбранных\n"
        "• ✏️ Имя / 📝 Bio / 🔤 Username — изменить профиль аккаунтов\n\n"
        "💡 После выбора операции появится список аккаунтов с чекбоксами",
        parse_mode="HTML",
        reply_markup=_bulk_menu_kb().as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# BULK ACCOUNT SELECTION (toggles → confirm → execute)
# ══════════════════════════════════════════════════════════════════════════


async def _show_bulk_select(
    msg_or_cb, pool: asyncpg.Pool, op: str, selected: set[int], edit: bool = True
) -> None:
    """Render account selection keyboard for a bulk operation."""
    from aiogram.types import CallbackQuery as _CQ

    is_cb = isinstance(msg_or_cb, _CQ)
    owner_id = msg_or_cb.from_user.id

    try:
        accounts = await pool.fetch(
            "SELECT id, first_name, username, phone, is_active FROM tg_accounts "
            "WHERE owner_id=$1 ORDER BY added_at",
            owner_id,
        )
    except Exception:
        accounts = []
    active = [a for a in accounts if a["is_active"]]

    if not active:
        text = (
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Добавьте аккаунт через 📱 <b>Мои аккаунты</b> в главном меню,\n"
            "или нажмите /accounts"
        )
        kb = _back_kb()
        if is_cb:
            try:
                await msg_or_cb.message.edit_text(
                    text, parse_mode="HTML", reply_markup=kb.as_markup()
                )
            except Exception:
                await msg_or_cb.message.answer(
                    text, parse_mode="HTML", reply_markup=kb.as_markup()
                )
        else:
            await msg_or_cb.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
        return

    # Filter selected to only existing active account IDs
    active_ids = {a["id"] for a in active}
    selected = selected & active_ids if selected else active_ids

    op_label = _BULK_OP_LABELS.get(op, op)
    n = len(selected)
    total = len(active)
    text = (
        f"⚡ <b>{op_label}</b>\n\n"
        f"Выбрано: <b>{n}</b> из {total} аккаунтов\n\n"
        "Нажмите на аккаунт чтобы включить/выключить.\n"
        "Когда готово — нажмите <b>▶️ Продолжить</b>."
    )
    kb = _bulk_select_kb(active, selected, op)
    if is_cb:
        try:
            await msg_or_cb.message.edit_text(
                text, parse_mode="HTML", reply_markup=kb.as_markup()
            )
        except Exception:
            await msg_or_cb.message.answer(
                text, parse_mode="HTML", reply_markup=kb.as_markup()
            )
    else:
        await msg_or_cb.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


# Entry point for each bulk operation — shows account picker with all accounts pre-selected
@router.callback_query(
    ChanCb.filter(
        F.action.in_(
            {
                "bulk_dm",
                "bulk_join",
                "bulk_leave",
                "bulk_post",
                "bulk_prof_name",
                "bulk_prof_bio",
                "bulk_prof_uname",
                "bulk_chan_uname",
                "bulk_chan_about",
            }
        )
    )
)
async def cb_bulk_start_op(
    callback: CallbackQuery,
    callback_data: ChanCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    op_map = {
        "bulk_dm": "dm",
        "bulk_join": "join",
        "bulk_leave": "leave",
        "bulk_post": "post",
        "bulk_prof_name": "prof_name",
        "bulk_prof_bio": "prof_bio",
        "bulk_prof_uname": "prof_uname",
        "bulk_chan_uname": "chan_uname",
        "bulk_chan_about": "chan_about",
    }
    op = op_map[callback_data.action]
    try:
        accounts = await pool.fetch(
            "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE",
            callback.from_user.id,
        )
    except Exception:
        accounts = []
    selected = {a["id"] for a in accounts}  # start with all selected
    await state.update_data(bulk_op=op, bulk_selected=list(selected))
    await _show_bulk_select(callback, pool, op, selected)


# Toggle a single account
@router.callback_query(F.data.startswith("chan:bsel:"))
async def cb_bulk_toggle_acc(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    parts = callback.data.split(":")  # chan, bsel, op, acc_id
    if len(parts) < 4:
        return
    op = parts[2]
    try:
        acc_id = int(parts[3])
    except ValueError:
        return
    data = await state.get_data()
    selected = set(data.get("bulk_selected", []))
    if acc_id in selected:
        selected.discard(acc_id)
    else:
        selected.add(acc_id)
    await state.update_data(bulk_selected=list(selected), bulk_op=op)
    await _show_bulk_select(callback, pool, op, selected)


# Select all accounts
@router.callback_query(F.data.startswith("chan:bsall:"))
async def cb_bulk_select_all(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    op = callback.data.split(":", 2)[2] if callback.data.count(":") >= 2 else ""
    try:
        accounts = await pool.fetch(
            "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE",
            callback.from_user.id,
        )
    except Exception:
        accounts = []
    selected = {a["id"] for a in accounts}
    await state.update_data(bulk_selected=list(selected), bulk_op=op)
    await _show_bulk_select(callback, pool, op, selected)


# Deselect all accounts
@router.callback_query(F.data.startswith("chan:bsnone:"))
async def cb_bulk_select_none(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    op = callback.data.split(":", 2)[2] if callback.data.count(":") >= 2 else ""
    await state.update_data(bulk_selected=[], bulk_op=op)
    await _show_bulk_select(callback, pool, op, set())


# Confirm selection — route to operation-specific input
@router.callback_query(F.data.startswith("chan:bsdone:"))
async def cb_bulk_confirm_selection(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    op = callback.data.split(":", 2)[2] if callback.data.count(":") >= 2 else ""
    data = await state.get_data()
    selected_ids = data.get("bulk_selected", [])
    if not selected_ids:
        await callback.answer("⚠️ Не выбрано ни одного аккаунта.", show_alert=True)
        return
    await callback.answer()

    # Route to the appropriate input step
    if op == "create":
        await state.update_data(bulk_op=op)
        await state.set_state(BulkCreateFSM.waiting_title)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
        await callback.message.edit_text(
            f"🔁 <b>Массовое создание</b>\n\n"
            f"Выбрано аккаунтов: <b>{len(selected_ids)}</b>\n\n"
            "Введите <b>название</b> канала/группы:",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )

    elif op == "dm":
        await state.update_data(bulk_op=op)
        await state.set_state(BulkDmFSM.waiting_usernames)
        n_acc = len(selected_ids)
        # delay per account: 5s single, 3s two, 2.5s three+
        delay_s = 5.0 if n_acc == 1 else (3.0 if n_acc == 2 else 2.5)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
        await callback.message.edit_text(
            f"✉️ <b>Рассылка личных сообщений</b>\n\n"
            f"Аккаунтов для отправки: <b>{n_acc}</b>\n"
            f"Задержка между сообщениями: ~<b>{delay_s:.0f}с</b>\n\n"
            "📋 <b>Шаг 1/2 — Список получателей</b>\n\n"
            "Отправьте список username (по одному на строку):\n\n"
            "<code>@username1\n@username2\n@username3</code>\n\n"
            "💡 Символ @ необязателен. Принимаются также числовые ID.\n"
            "⚠️ Рекомендуется не более 200 получателей за сеанс.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )

    elif op == "join":
        await state.update_data(bulk_op=op)
        await state.set_state(JoinChannelFSM.waiting_invite)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
        await callback.message.edit_text(
            f"🔗 <b>Вступить в канал</b>\n\n"
            f"Выбрано аккаунтов: <b>{len(selected_ids)}</b>\n\n"
            "Введите username или ссылку-приглашение:\n"
            "• <code>@channelname</code>\n"
            "• <code>https://t.me/+AbcHash</code>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )

    elif op == "leave":
        await state.update_data(bulk_op=op)
        await state.set_state(PostToChannelFSM.waiting_channel_id)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
        await callback.message.edit_text(
            f"🚪 <b>Выйти из канала</b>\n\n"
            f"Выбрано аккаунтов: <b>{len(selected_ids)}</b>\n\n"
            "Введите username или числовой ID канала:",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )

    elif op == "post":
        await state.update_data(bulk_op=op)
        await state.set_state(PostToChannelFSM.waiting_channel_id)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
        await callback.message.edit_text(
            f"📤 <b>Опубликовать пост</b>\n\n"
            f"Выбрано аккаунтов: <b>{len(selected_ids)}</b>\n\n"
            "Введите username или числовой ID канала для публикации:",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )

    elif op == "botfather":
        await state.update_data(bulk_op=op)
        await state.set_state(CreateBotFSM.waiting_count)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
        await callback.message.edit_text(
            f"🤖 <b>Создать боты через @BotFather</b>\n\n"
            f"Выбрано аккаунтов: <b>{len(selected_ids)}</b>\n\n"
            "Сколько ботов создать на каждом аккаунте? (1–5):",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )

    elif op in ("prof_name", "prof_bio", "prof_uname"):
        field_map = {
            "prof_name": ("first_name", "✏️ Введите новое <b>имя</b>:"),
            "prof_bio": ("about", "📝 Введите новое <b>bio</b> (до 70 символов):"),
            "prof_uname": (
                "username",
                "🔤 Введите <b>username</b> (для 2-го+ аккаунтов добавится цифра):",
            ),
        }
        field, prompt = field_map[op]
        await state.update_data(bulk_op=op, bulk_field=field)
        await state.set_state(UpdateProfileFSM.waiting_value)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
        await callback.message.edit_text(
            f"{prompt}\n\n<i>Выбрано аккаунтов: {len(selected_ids)}</i>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )

    elif op == "chan_uname":
        await state.update_data(bulk_op=op)
        await state.set_state(BulkChanFSM.waiting_value)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
        await callback.message.edit_text(
            f"🔤 <b>Массовая установка username каналов</b>\n\n"
            f"Выбрано аккаунтов: <b>{len(selected_ids)}</b>\n\n"
            "Система выберет все каналы из выбранных аккаунтов и установит им username по шаблону.\n\n"
            "Введите <b>базовый username</b> (цифра будет добавлена автоматически):\n\n"
            "<code>mychannel</code> → <code>mychannel1</code>, <code>mychannel2</code>…\n\n"
            "⚠️ Username должен содержать только буквы, цифры и _\n"
            "⚠️ Канал должен быть публичным для установки username",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )

    elif op == "chan_about":
        await state.update_data(bulk_op=op)
        await state.set_state(BulkChanFSM.waiting_value)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
        await callback.message.edit_text(
            f"📄 <b>Массовое обновление описания каналов</b>\n\n"
            f"Выбрано аккаунтов: <b>{len(selected_ids)}</b>\n\n"
            "Система выберет все каналы из выбранных аккаунтов и установит им одинаковое описание.\n\n"
            "Введите <b>текст описания</b> (до 255 символов):",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )


async def _bulk_leave_channel_bg(
    pool: asyncpg.Pool,
    user_id: int,
    msg,
    accounts: list,
    channel_ref: str,
    total: int,
) -> None:
    from services import account_manager
    from database import db as _db
    from services.op_worker import write_op_audit as _op_audit

    ok_list, err_list = [], []
    attempt = 0
    try:
        for idx, acc in enumerate(accounts):
            label = html.escape(acc["first_name"] or acc["phone"])
            result = None
            try:
                result = await account_manager.leave_channel(
                    acc["session_str"], channel_ref, _acc=dict(acc)
                )
            except Exception as e:
                err_list.append(f"❌ {label}: {str(e)[:50]}")
                await _op_audit(
                    pool,
                    user_id,
                    "leave",
                    "error",
                    target=str(channel_ref),
                    account_id=acc["id"],
                    error_msg=str(e)[:200],
                )
            if result is not None:
                if isinstance(result, dict) and result.get("banned"):
                    await _db.deactivate_account(
                        pool, acc["id"], "banned detected in bulk op"
                    )
                    err_list.append(f"❌ {label}: забанен")
                    await _op_audit(
                        pool,
                        user_id,
                        "leave",
                        "error",
                        target=str(channel_ref),
                        account_id=acc["id"],
                        error_msg="banned",
                    )
                elif isinstance(result, dict) and result.get("flood_wait"):
                    err_list.append(f"⏳ {label}: flood_wait, пропущен")
                    await _op_audit(
                        pool,
                        user_id,
                        "leave",
                        "flood_wait",
                        target=str(channel_ref),
                        account_id=acc["id"],
                        flood_wait_s=result.get("flood_wait"),
                    )
                elif result:
                    ok_list.append(f"✅ {label}")
                    await _op_audit(
                        pool,
                        user_id,
                        "leave",
                        "success",
                        target=str(channel_ref),
                        account_id=acc["id"],
                    )
                else:
                    err_list.append(f"❌ {label}: не удалось")
                    await _op_audit(
                        pool,
                        user_id,
                        "leave",
                        "error",
                        target=str(channel_ref),
                        account_id=acc["id"],
                        error_msg="leave returned False",
                    )
            try:
                await msg.edit_text(
                    _progress_text(
                        "Покидаю каналы...", idx + 1, total, len(ok_list), len(err_list)
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(log, "Сбой обновления прогресса покидания каналов")
            if attempt >= 4:
                attempt = 0
            else:
                attempt += 1
            flood = result.get("flood_wait", 0) if isinstance(result, dict) else 0
            await asyncio.sleep(max(backoff(attempt, base=2.0, cap=30.0), flood))
    except asyncio.CancelledError:
        log.info("_bulk_leave_channel_bg: отменено")
        raise
    except Exception:
        log_exc_swallow(log, "_bulk_leave_channel_bg: неожиданная ошибка")

    lines = [f"🚪 <b>Выход из {html.escape(channel_ref)}</b>\n"] + ok_list + err_list
    try:
        await msg.edit_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=_back_kb().as_markup()
        )
    except Exception:
        log_exc_swallow(log, "_bulk_leave_channel_bg: сбой финального отчёта")


# ── FSM: channel reference input (leave or post) ──────────────────────────


@router.message(PostToChannelFSM.waiting_channel_id)
async def fsm_bulk_channel_id(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    channel_ref = (message.text or "").strip()
    data = await state.get_data()
    op = data.get("bulk_op", "")
    selected_ids = data.get("bulk_selected", [])
    await state.clear()

    try:
        accounts = (
            await pool.fetch(
                "SELECT a.id, a.session_str, a.first_name, a.phone, "
                "a.device_model, a.system_version, a.app_version, p.proxy_url "
                "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
                "WHERE a.owner_id=$1 AND a.id = ANY($2::bigint[]) AND a.session_str IS NOT NULL",
                message.from_user.id,
                selected_ids,
            )
            if selected_ids
            else []
        )
    except Exception:
        accounts = []

    if not accounts:
        await message.answer("⚠️ Нет выбранных аккаунтов. Начните заново: /ops")
        return

    if op == "leave":
        total = len(accounts)
        msg = await message.answer(
            _progress_text("Покидаю каналы...", 0, total, 0, 0), parse_mode="HTML"
        )
        task = asyncio.create_task(
            _bulk_leave_channel_bg(
                pool, message.from_user.id, msg, list(accounts), channel_ref, total
            )
        )
        _treg.register(
            message.from_user.id,
            "bulk_leave",
            f"Выход из {html.escape(channel_ref)} ({total} аккаунтов)",
            task,
        )

    elif op == "post":
        await state.update_data(
            bulk_op=op, bulk_selected=selected_ids, channel_id_ref=channel_ref
        )
        await state.set_state(PostToChannelFSM.waiting_text)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
        await message.answer(
            f"📝 Введите <b>текст поста</b> для <code>{html.escape(channel_ref)}</code>:\n\n"
            "<i>Поддерживается HTML-форматирование</i>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )


async def _bulk_post_to_channel_bg(
    pool: asyncpg.Pool,
    user_id: int,
    msg,
    accounts: list,
    channel_ref: str,
    text_to_post: str,
    bulk_access_hash: int,
    total: int,
) -> None:
    from services import account_manager
    from database import db as _db

    ok_list, err_list = [], []
    attempt = 0
    try:
        for idx, acc in enumerate(accounts):
            label = html.escape(acc["first_name"] or acc["phone"])
            acc_id_cur = acc.get("id")
            result = await account_manager.post_to_channel(
                acc["session_str"],
                channel_ref,
                text_to_post,
                access_hash=bulk_access_hash,
                _acc=dict(acc),
            )
            if result.get("banned"):
                if acc_id_cur:
                    await _db.deactivate_account(
                        pool, acc_id_cur, "banned detected in bulk op"
                    )
                err_list.append(f"❌ {label}: забанен")
            elif result.get("flood_wait"):
                err_list.append(f"⏳ {label}: flood_wait, пропущен")
            elif "msg_id" in result:
                ok_list.append(f"✅ {label}: msg_id={result['msg_id']}")
            else:
                err_list.append(
                    f"❌ {label}: {html.escape(result.get('error', 'ошибка')[:60])}"
                )
            try:
                await msg.edit_text(
                    _progress_text(
                        "Публикую посты...", idx + 1, total, len(ok_list), len(err_list)
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(log, "Сбой обновления прогресса публикации в канал")
            if attempt >= 4:
                attempt = 0
            else:
                attempt += 1
            flood = result.get("flood_wait", 0)
            await asyncio.sleep(max(backoff(attempt), flood))
    except asyncio.CancelledError:
        log.info("_bulk_post_to_channel_bg: отменено")
        raise
    except Exception:
        log_exc_swallow(log, "_bulk_post_to_channel_bg: неожиданная ошибка")

    lines = (
        [f"📤 <b>Публикация в {html.escape(channel_ref)}</b>\n"] + ok_list + err_list
    )
    try:
        await msg.edit_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=_back_kb().as_markup()
        )
    except Exception:
        log_exc_swallow(log, "_bulk_post_to_channel_bg: сбой финального отчёта")


# ── FSM: post text input ──────────────────────────────────────────────────


@router.message(PostToChannelFSM.waiting_text)
async def fsm_bulk_post_text(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    text_to_post = (message.text or "").strip()
    if not text_to_post:
        await message.answer("⚠️ Введите текст поста:")
        return
    data = await state.get_data()
    op = data.get("bulk_op", "")
    selected_ids = data.get("bulk_selected", [])

    if op == "post" and selected_ids:
        channel_ref = data.get("channel_id_ref", "")
        await state.clear()
        try:
            accounts = await pool.fetch(
                "SELECT a.id, a.session_str, a.first_name, a.phone, "
                "a.device_model, a.system_version, a.app_version, p.proxy_url "
                "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
                "WHERE a.owner_id=$1 AND a.id = ANY($2::bigint[]) AND a.session_str IS NOT NULL",
                message.from_user.id,
                selected_ids,
            )
        except Exception:
            accounts = []
        if not accounts:
            await message.answer("⚠️ Аккаунты не найдены. Начните заново: /ops")
            return
        total = len(accounts)
        bulk_access_hash = 0
        if channel_ref.lstrip("-").isdigit():
            cid = abs(int(channel_ref))
            try:
                ah_row = await pool.fetchrow(
                    "SELECT access_hash FROM managed_channels WHERE owner_id=$1 AND channel_id=$2",
                    message.from_user.id,
                    cid,
                )
            except Exception:
                ah_row = None
            bulk_access_hash = (ah_row["access_hash"] if ah_row else 0) or 0

        msg = await message.answer(
            _progress_text("Публикую посты...", 0, total, 0, 0), parse_mode="HTML"
        )
        task = asyncio.create_task(
            _bulk_post_to_channel_bg(
                pool,
                message.from_user.id,
                msg,
                list(accounts),
                channel_ref,
                text_to_post,
                bulk_access_hash,
                total,
            )
        )
        _treg.register(
            message.from_user.id,
            "bulk_post_to_channel",
            f"Публикация в {html.escape(channel_ref)} ({total} аккаунтов)",
            task,
        )
    else:
        # Single-account post (from cb_post_channel_chosen)
        acc_id = data.get("acc_id")
        ch_id = data.get("channel_id")
        await state.clear()
        acc = await db.get_account_for_telethon(pool, acc_id, message.from_user.id)
        if not acc:
            await message.answer("⚠️ Аккаунт не найден. Начните заново: /ops")
            return
        msg = await message.answer("⏳ Публикую...")
        from services import account_manager

        try:
            single_ah_row = await pool.fetchrow(
                "SELECT access_hash FROM managed_channels WHERE owner_id=$1 AND channel_id=$2",
                message.from_user.id,
                ch_id,
            )
        except Exception:
            single_ah_row = None
        single_access_hash = (single_ah_row["access_hash"] if single_ah_row else 0) or 0
        result = await account_manager.post_to_channel(
            acc["session_str"],
            ch_id,
            text_to_post,
            access_hash=single_access_hash,
            _acc=acc,
        )
        kb = _back_kb()
        if "msg_id" in result:
            await msg.edit_text(
                f"✅ <b>Пост опубликован!</b>\n\nID сообщения: <code>{result['msg_id']}</code>",
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )
        else:
            err_detail = html.escape(result.get("error", "неизвестная ошибка")[:120])
            await msg.edit_text(
                f"❌ <b>Ошибка публикации</b>\n\n<code>{err_detail}</code>",
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )


# ── FSM: bulk join (uses selected accounts from state) ────────────────────


@router.message(JoinChannelFSM.waiting_invite)
async def fsm_join_invite_combined(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    invite = (message.text or "").strip()
    data = await state.get_data()
    op = data.get("bulk_op", "")
    selected_ids = data.get("bulk_selected", [])
    is_bulk = op == "join" and bool(selected_ids)
    await state.clear()

    from services import account_manager

    if is_bulk:
        try:
            accounts = await pool.fetch(
                "SELECT a.id, a.session_str, a.first_name, a.phone, "
                "a.device_model, a.system_version, a.app_version, p.proxy_url "
                "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
                "WHERE a.owner_id=$1 AND a.id = ANY($2::bigint[]) AND a.session_str IS NOT NULL",
                message.from_user.id,
                selected_ids,
            )
        except Exception:
            accounts = []
        if not accounts:
            await message.answer("⚠️ Аккаунты не найдены. Начните заново: /ops")
            return
        from database import db as _db

        total = len(accounts)
        msg = await message.answer(
            _progress_text("Вступаю в канал...", 0, total, 0, 0), parse_mode="HTML"
        )
        ok_list, err_list = [], []
        active_accounts = list(accounts)
        attempt = 0
        from services.op_worker import write_op_audit as _op_audit

        # Round-robin: distribute join attempts across accounts
        for idx, acc in enumerate(active_accounts):
            label = html.escape(acc["first_name"] or acc["phone"])
            result = await account_manager.join_channel(
                acc["session_str"], invite, _acc=dict(acc)
            )
            if result.get("banned"):
                await _db.deactivate_account(
                    pool, acc["id"], "banned detected in bulk op"
                )
                err_list.append(f"❌ {label}: забанен")
                await _op_audit(
                    pool,
                    message.from_user.id,
                    "join",
                    "error",
                    target=invite,
                    account_id=acc["id"],
                    error_msg="banned_in_channel",
                )
            elif result.get("flood_wait"):
                err_list.append(f"⏳ {label}: flood_wait, пропущен")
                await _op_audit(
                    pool,
                    message.from_user.id,
                    "join",
                    "flood_wait",
                    target=invite,
                    account_id=acc["id"],
                    flood_wait_s=result.get("flood_wait"),
                )
            elif "error" in result:
                err_raw = result["error"]
                err_e = err_raw.lower()
                if "userbannedinchannels" in err_e or "banned in channel" in err_e:
                    err_list.append(f"🚫 {label}: заблокирован в канале")
                elif "channelprivate" in err_e or "channel_private" in err_e:
                    err_list.append(
                        f"🔒 {label}: закрытый канал (нужна ссылка-приглашение)"
                    )
                elif "floodwait" in err_e or "flood_wait" in err_e:
                    err_list.append(f"⏳ {label}: FloodWait — пауза Telegram")
                elif "usernotmutualcontact" in err_e:
                    err_list.append(f"❌ {label}: доступ только для контактов")
                elif "channelstoomuchchat" in err_e or "too much" in err_e:
                    err_list.append(f"❌ {label}: превышен лимит каналов")
                elif "invitehash" in err_e or "invalid" in err_e and "hash" in err_e:
                    err_list.append(f"❌ {label}: недействительная ссылка-приглашение")
                else:
                    err_list.append(f"❌ {label}: {html.escape(err_raw[:60])}")
                await _op_audit(
                    pool,
                    message.from_user.id,
                    "join",
                    "error",
                    target=invite,
                    account_id=acc["id"],
                    error_msg=err_raw[:200],
                )
            else:
                ok_list.append(f"✅ {label}: вступил")
                await _op_audit(
                    pool,
                    message.from_user.id,
                    "join",
                    "success",
                    target=invite,
                    account_id=acc["id"],
                )
            try:
                await msg.edit_text(
                    _progress_text(
                        "Вступаю в канал...",
                        idx + 1,
                        total,
                        len(ok_list),
                        len(err_list),
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(log, "Сбой обновления прогресса вступления в канал")
            # Exponential backoff; reset every 5 iterations
            if attempt >= 4:
                attempt = 0
            else:
                attempt += 1
            flood = result.get("flood_wait", 0)
            await asyncio.sleep(max(backoff(attempt), flood))
        lines = [f"🔗 <b>Вступление в {html.escape(invite)}</b>\n"] + ok_list + err_list
        await msg.edit_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=_back_kb().as_markup()
        )
        return

    # Single-account join
    acc = await db.get_account_for_telethon(
        pool, data.get("acc_id"), message.from_user.id
    )
    if not acc:
        await message.answer("⚠️ Аккаунт не найден. Начните заново: /ops")
        return
    msg = await message.answer("⏳ Вступаю...")
    from services.op_worker import write_op_audit as _op_audit

    result = await account_manager.join_channel(acc["session_str"], invite, _acc=acc)
    kb = _back_kb()
    if "error" in result:
        await _op_audit(
            pool,
            message.from_user.id,
            "join",
            "error",
            target=invite,
            account_id=acc.get("id"),
            error_msg=result["error"][:200],
        )
        friendly_msg = _friendly_join_error(result["error"])
        await msg.edit_text(
            friendly_msg,
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    else:
        await _op_audit(
            pool,
            message.from_user.id,
            "join",
            "success",
            target=invite,
            account_id=acc.get("id"),
        )
        title = html.escape(result.get("title", ""))
        members = result.get("members", 0)
        await msg.edit_text(
            f"✅ <b>Вступил в канал!</b>\n\n"
            f"Название: <b>{title}</b>\n"
            f"Участников: <b>{members:,}</b>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )


async def _bulk_update_profile_bg(
    pool: asyncpg.Pool,
    user_id: int,
    msg,
    accounts: list,
    field: str,
    value: str,
    total: int,
) -> None:
    from services import account_manager
    from database import db as _db

    ok_list, err_list = [], []
    attempt = 0
    try:
        for i, acc in enumerate(accounts):
            label = html.escape(acc["first_name"] or acc["phone"])
            actual_value = f"{value}{i + 1}" if field == "username" else value
            try:
                if field == "username":
                    result = await account_manager.update_account_username(
                        acc["session_str"], actual_value, _acc=dict(acc)
                    )
                    if isinstance(result, dict) and result.get("banned"):
                        await _db.deactivate_account(
                            pool, acc["id"], "banned detected in bulk op"
                        )
                        err_list.append(f"❌ {label}: забанен")
                    elif isinstance(result, dict) and result.get("flood_wait"):
                        err_list.append(f"⏳ {label}: flood_wait, пропущен")
                    elif result and not isinstance(result, dict):
                        err_list.append(f"❌ {label}: {html.escape(str(result)[:50])}")
                    else:
                        ok_list.append(f"✅ {label}: @{html.escape(actual_value)}")
                else:
                    result = await account_manager.update_profile(
                        acc["session_str"], **{field: value}, _acc=dict(acc)
                    )
                    if isinstance(result, dict) and result.get("banned"):
                        await _db.deactivate_account(
                            pool, acc["id"], "banned detected in bulk op"
                        )
                        err_list.append(f"❌ {label}: забанен")
                    elif isinstance(result, dict) and result.get("flood_wait"):
                        err_list.append(f"⏳ {label}: flood_wait, пропущен")
                    elif result:
                        ok_list.append(f"✅ {label}")
                    else:
                        err_list.append(f"❌ {label}: ошибка")
            except Exception as e:
                err_list.append(f"❌ {label}: {str(e)[:50]}")
            try:
                await msg.edit_text(
                    _progress_text(
                        "Обновляю профили...", i + 1, total, len(ok_list), len(err_list)
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(log, "Сбой обновления прогресса обновления профилей")
            if attempt >= 4:
                attempt = 0
            else:
                attempt += 1
            await asyncio.sleep(backoff(attempt, base=2.0, cap=30.0))
    except asyncio.CancelledError:
        log.info("_bulk_update_profile_bg: отменено")
        raise
    except Exception:
        log_exc_swallow(log, "_bulk_update_profile_bg: неожиданная ошибка")

    lines = [f"✏️ <b>Обновление {field}</b>\n"] + ok_list + err_list
    try:
        await msg.edit_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=_back_kb().as_markup()
        )
    except Exception:
        log_exc_swallow(log, "_bulk_update_profile_bg: сбой финального отчёта")


# ── FSM: profile update (single or bulk with selected accounts) ───────────


@router.message(UpdateProfileFSM.waiting_value)
async def fsm_update_profile(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    value = (message.text or "").strip()
    data = await state.get_data()
    await state.clear()

    op = data.get("bulk_op", "")
    selected_ids = data.get("bulk_selected", [])
    is_bulk = op.startswith("prof_") and bool(selected_ids)
    field = data.get("bulk_field") or data.get("field", "")

    from services import account_manager

    if is_bulk:
        try:
            accounts = await pool.fetch(
                "SELECT a.id, a.session_str, a.first_name, a.phone, "
                "a.device_model, a.system_version, a.app_version, p.proxy_url "
                "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
                "WHERE a.owner_id=$1 AND a.id = ANY($2::bigint[]) AND a.session_str IS NOT NULL",
                message.from_user.id,
                selected_ids,
            )
        except Exception:
            accounts = []
        if not accounts:
            await message.answer("⚠️ Аккаунты не найдены.")
            return
        total = len(accounts)
        msg = await message.answer(
            _progress_text("Обновляю профили...", 0, total, 0, 0), parse_mode="HTML"
        )
        task = asyncio.create_task(
            _bulk_update_profile_bg(
                pool, message.from_user.id, msg, list(accounts), field, value, total
            )
        )
        _treg.register(
            message.from_user.id,
            "bulk_update_profile",
            f"Обновление {field} у {total} аккаунтов",
            task,
        )
    else:
        acc = await db.get_account_for_telethon(
            pool, data.get("acc_id"), message.from_user.id
        )
        if not acc:
            await message.answer("⚠️ Аккаунт не найден.")
            return
        kb = _back_kb()
        if field == "username":
            err = await account_manager.update_account_username(
                acc["session_str"], value, _acc=acc
            )
            if err:
                await message.answer(
                    f"❌ Ошибка: <code>{html.escape(err)}</code>",
                    parse_mode="HTML",
                    reply_markup=kb.as_markup(),
                )
            else:
                await message.answer(
                    f"✅ Username обновлён: @{html.escape(value)}",
                    parse_mode="HTML",
                    reply_markup=kb.as_markup(),
                )
        else:
            ok = await account_manager.update_profile(
                acc["session_str"], **{field: value}, _acc=acc
            )
            await message.answer(
                "✅ Профиль обновлён!" if ok else "❌ Ошибка обновления профиля.",
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )


# ══════════════════════════════════════════════════════════════════════════
# BULK DM — mass direct messages to a username list
# ══════════════════════════════════════════════════════════════════════════


def _parse_username_list(raw: str) -> list[str]:
    """Parse a multiline/comma-separated username list into clean targets."""
    import re

    # split on newlines, commas, semicolons, spaces
    parts = re.split(r"[\n,;]+", raw)
    result = []
    seen: set[str] = set()
    for p in parts:
        p = p.strip().lstrip("@").strip()
        if not p:
            continue
        key = p.lower()
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result


@router.message(BulkDmFSM.waiting_usernames, F.document)
async def fsm_bulk_dm_usernames_file(message: Message, state: FSMContext) -> None:
    doc = message.document
    if not doc:
        await message.answer("⚠️ Документ не получен.")
        return
    if doc.file_size and doc.file_size > 200_000:
        await message.answer("⚠️ Файл слишком большой. Максимум 200 КБ.")
        return
    try:
        file_info = await message.bot.get_file(doc.file_id)
        dl = await message.bot.download_file(file_info.file_path)
        raw = (dl.read() if hasattr(dl, "read") else bytes(dl)).decode(
            "utf-8", errors="ignore"
        )
    except Exception as e:
        await state.clear()
        await message.answer(f"⚠️ Не удалось прочитать файл: {e}")
        return
    usernames = _parse_username_list(raw)
    if not usernames:
        await message.answer("⚠️ Файл не содержит распознанных usernames.")
        return
    if len(usernames) > 500:
        usernames = usernames[:500]
        await message.answer("⚠️ Взяты первые 500 получателей из файла.")
    await _proceed_bulk_dm_usernames(usernames, message, state)


@router.message(BulkDmFSM.waiting_usernames, F.text)
async def fsm_bulk_dm_usernames(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    usernames = _parse_username_list(raw)
    if not usernames:
        await message.answer(
            "⚠️ Список пустой. Отправьте usernames — по одному на строке:\n"
            "<code>@username1\n@username2</code>",
            parse_mode="HTML",
        )
        return

    await _proceed_bulk_dm_usernames(usernames, message, state)


async def _proceed_bulk_dm_usernames(
    usernames: list[str], message, state: FSMContext
) -> None:
    await state.update_data(bulk_dm_usernames=usernames)
    await state.set_state(BulkDmFSM.waiting_text)

    data = await state.get_data()
    selected_ids = data.get("bulk_selected", [])
    n_acc = max(len(selected_ids), 1)
    delay_s = 5.0 if n_acc == 1 else (3.0 if n_acc == 2 else 2.5)
    est_min = round(len(usernames) * delay_s / 60, 1)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))

    await message.answer(
        f"✅ Получателей: <b>{len(usernames)}</b>\n"
        f"Аккаунтов: <b>{n_acc}</b> | задержка ~{delay_s:.0f}с\n"
        f"Ориентировочное время: ~<b>{est_min}</b> мин\n\n"
        "📝 <b>Шаг 2/2 — Текст сообщения</b>\n\n"
        "Отправьте текст, который будет разослан всем получателям.\n"
        "Поддерживается HTML-форматирование: <b>жирный</b>, <i>курсив</i>, <code>код</code>.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _bulk_dm_bg(
    pool: asyncpg.Pool,
    user_id: int,
    progress_msg,
    accounts: list,
    usernames: list[str],
    text_to_send: str,
    base_delay: float,
    total: int,
) -> None:
    from services import account_manager
    from database import db as _db

    ok_list: list[str] = []
    err_list: list[str] = []
    flood_wait_total = 0
    active_accounts = list(accounts)

    try:
        for i, username in enumerate(usernames):
            if not active_accounts:
                err_list.append(f"❌ @{html.escape(username)}: нет активных аккаунтов")
                continue
            n_active = len(active_accounts)
            acc = active_accounts[i % n_active]
            result = await account_manager.send_dm(
                acc["session_str"], username, text_to_send, _acc=dict(acc)
            )

            u_escaped = html.escape(username)
            if result.get("banned"):
                await _db.deactivate_account(
                    pool, acc["id"], "banned detected in bulk op"
                )
                active_accounts = [a for a in active_accounts if a["id"] != acc["id"]]
                err_list.append(f"❌ @{u_escaped}: аккаунт забанен")
            elif result.get("flood_wait"):
                flood_wait_total += result.get("flood_wait", 0)
                err_list.append(f"⏳ @{u_escaped}: flood_wait")
            elif result.get("ok"):
                ok_list.append(f"✅ @{u_escaped}")
            else:
                err = html.escape(result.get("error", "неизвестная ошибка")[:60])
                err_list.append(f"❌ @{u_escaped}: {err}")
                flood_wait_total += result.get("flood_wait", 0)

            if (i + 1) % 5 == 0 or i + 1 == total:
                try:
                    await progress_msg.edit_text(
                        _progress_text(
                            "Рассылка ЛС...", i + 1, total, len(ok_list), len(err_list)
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    log_exc_swallow(log, "Сбой обновления прогресса рассылки ЛС")

            wait = base_delay + min(flood_wait_total, 30)
            flood_wait_total = max(0, flood_wait_total - base_delay)
            await asyncio.sleep(wait)
    except asyncio.CancelledError:
        log.info("_bulk_dm_bg: отменено")
        raise
    except Exception:
        log_exc_swallow(log, "_bulk_dm_bg: неожиданная ошибка")

    sent = len(ok_list)
    failed = len(err_list)
    header = (
        f"📊 <b>Рассылка завершена</b>\n\n"
        f"Всего: <b>{total}</b> | ✅ Успешно: <b>{sent}</b> | ❌ Ошибок: <b>{failed}</b>\n\n"
    )
    error_section = ""
    if err_list:
        shown_errors = err_list[:30]
        error_section = "<b>Ошибки:</b>\n" + "\n".join(shown_errors)
        if len(err_list) > 30:
            error_section += f"\n<i>...и ещё {len(err_list) - 30} ошибок</i>"
    try:
        await progress_msg.edit_text(
            header + error_section,
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
    except Exception:
        log_exc_swallow(log, "_bulk_dm_bg: сбой финального отчёта")


@router.message(BulkDmFSM.waiting_text)
async def fsm_bulk_dm_text(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    text_to_send = message.text or message.caption or ""
    if not text_to_send.strip():
        await message.answer("⚠️ Текст не может быть пустым. Отправьте текст сообщения:")
        return

    data = await state.get_data()
    usernames = data.get("bulk_dm_usernames", [])
    selected_ids = data.get("bulk_selected", [])
    await state.clear()

    if not usernames or not selected_ids:
        await message.answer("⚠️ Данные рассылки устарели. Начните заново: /ops")
        return

    try:
        accounts = await pool.fetch(
            "SELECT a.id, a.session_str, a.first_name, a.phone, "
            "a.device_model, a.system_version, a.app_version, p.proxy_url "
            "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
            "WHERE a.owner_id=$1 AND a.id = ANY($2::bigint[]) AND a.is_active=TRUE AND a.session_str IS NOT NULL",
            message.from_user.id,
            selected_ids,
        )
    except Exception:
        accounts = []
    if not accounts:
        await message.answer("⚠️ Аккаунты не найдены. Начните заново: /ops")
        return

    n_acc = len(accounts)
    base_delay = 5.0 if n_acc == 1 else (3.0 if n_acc == 2 else 2.5)
    total = len(usernames)

    progress_msg = await message.answer(
        f"⏳ <b>Рассылка запущена</b>\n\n"
        f"Получателей: <b>{total}</b> | Аккаунтов: <b>{n_acc}</b>\n"
        f"Задержка: ~{base_delay:.0f}с | Ожидаемое время: ~{round(total * base_delay / 60, 1)} мин\n\n"
        "⏳ 0 / " + str(total),
        parse_mode="HTML",
    )
    task = asyncio.create_task(
        _bulk_dm_bg(
            pool,
            message.from_user.id,
            progress_msg,
            list(accounts),
            usernames,
            text_to_send,
            base_delay,
            total,
        )
    )
    _treg.register(
        message.from_user.id,
        "bulk_dm",
        f"Рассылка ЛС в {total} получателей",
        task,
    )


# ── FSM: bulk channel username / about input ──────────────────────────────


@router.message(BulkChanFSM.waiting_value)
async def fsm_bulk_chan_value(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:

    value = (message.text or "").strip()
    if not value:
        await message.answer("⚠️ Значение не может быть пустым. Введите текст:")
        return

    data = await state.get_data()
    op = data.get("bulk_op", "")
    selected_ids = data.get("bulk_selected", [])
    await state.clear()

    if not selected_ids:
        await message.answer("⚠️ Данные операции устарели. Начните заново: /ops")
        return

    # Validate username pattern
    if op == "chan_uname":
        import re

        base_uname = value.lstrip("@")
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]{3,}$", base_uname):
            kb_cancel = InlineKeyboardBuilder()
            kb_cancel.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
            await message.answer(
                "⚠️ Username должен начинаться с буквы, содержать только a–z, 0–9, _ и быть длиной 5+ символов.\n"
                "Введите базовый username заново:",
                reply_markup=kb_cancel.as_markup(),
            )
            await state.set_state(BulkChanFSM.waiting_value)
            await state.update_data(bulk_op=op, bulk_selected=selected_ids)
            return
    elif op == "chan_about" and len(value) > 255:
        value = value[:255]

    # Count channels from DB cache for preview
    try:
        chan_count = (
            await pool.fetchval(
                "SELECT COUNT(*) FROM managed_channels mc "
                "WHERE mc.owner_id=$1 AND mc.acc_id = ANY($2::bigint[])",
                message.from_user.id,
                selected_ids,
            )
            or 0
        )
    except Exception:
        chan_count = 0

    if chan_count == 0:
        await message.answer(
            "⚠️ Нет каналов в базе для выбранных аккаунтов.\n\n"
            "Сначала загрузите каналы через <b>🔎 Мои каналы → Загрузить из Telegram</b>.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    # Save value and op in state, show preview before executing
    await state.update_data(bulk_op=op, bulk_selected=selected_ids, bulk_value=value)
    await state.set_state(BulkChanFSM.waiting_confirm)

    op_label = "🔤 Username" if op == "chan_uname" else "📄 Описание"
    value_preview = html.escape(value[:80])
    try:
        acc_count = (
            await pool.fetchval(
                "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND id=ANY($2::bigint[]) AND is_active=TRUE",
                message.from_user.id,
                selected_ids,
            )
            or 0
        )
    except Exception:
        acc_count = len(selected_ids)
    eta_s = chan_count * 6  # ~6s per channel average
    eta_str = f"{eta_s // 60} мин" if eta_s >= 60 else f"{eta_s}с"

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Запустить", callback_data=ChanCb(action="bulk_chan_exec"))
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
    kb.adjust(2)
    await message.answer(
        f"<b>{op_label} каналам (bulk) — Предпросмотр</b>\n\n"
        f"Значение: <code>{value_preview}</code>\n"
        f"Каналов: <b>{chan_count}</b>\n"
        f"Аккаунтов: <b>{acc_count}</b>\n"
        f"Оценочное время: ~<b>{eta_str}</b>\n\n"
        "Подтвердите запуск операции:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _bulk_chan_exec_bg(
    pool: asyncpg.Pool,
    owner_id: int,
    progress_msg,
    channels: list,
    acc_by_id: dict,
    op: str,
    base_uname: str,
    value: str,
    op_label: str,
    total: int,
) -> None:
    from services import account_manager, op_worker

    ok_list: list[str] = []
    err_list: list[str] = []
    uname_counter = 1

    # Claim accounts so op_worker/warmup don't use the same sessions in parallel
    _claimed_ids = [int(aid) for aid in acc_by_id]
    await op_worker.mark_accounts_in_use(_claimed_ids)

    try:
        for idx, chan in enumerate(channels):
            chan_title = html.escape(chan["title"] or str(chan["channel_id"]))
            acc = acc_by_id.get(chan["acc_id"])
            if not acc:
                err_list.append(f"❌ {chan_title}: аккаунт не найден")
                continue

            if op == "chan_uname":
                from services.username_engine import unique_channel_username, generate_username_variants, _short_suffix

                # Generate unique starting candidate for this slot, then fall back to variants
                initial = unique_channel_username(base_uname, idx)
                variants_to_try = [initial]
                # Add more fallbacks from generator (starting from slot-specific base)
                for v in generate_username_variants(f"{base_uname}{_short_suffix(idx, 2)}"):
                    if v not in variants_to_try:
                        variants_to_try.append(v)

                assigned = None
                last_err = ""
                for variant in variants_to_try[:12]:
                    err = await account_manager.set_channel_username(
                        acc["session_str"], chan["channel_id"], variant, _acc=acc
                    )
                    if not err:
                        assigned = variant
                        break
                    last_err = err
                    # Only retry on "taken" errors, not on other failures
                    if not any(k in err.lower() for k in ("taken", "occupied", "username_occupied", "занят", "already")):
                        break
                    await asyncio.sleep(2.0)

                if assigned:
                    ok_list.append(f"✅ {chan_title}: @{assigned}")
                    try:
                        await pool.execute(
                            "UPDATE managed_channels SET username=$1 WHERE owner_id=$2 AND channel_id=$3",
                            assigned,
                            owner_id,
                            chan["channel_id"],
                        )
                    except Exception:
                        pass
                else:
                    err_list.append(f"❌ {chan_title}: {html.escape(last_err[:60])}")

            elif op == "chan_about":
                ok = await account_manager.edit_channel_about(
                    acc["session_str"], chan["channel_id"], value, _acc=acc
                )
                if ok:
                    ok_list.append(f"✅ {chan_title}")
                else:
                    err_list.append(f"❌ {chan_title}: ошибка обновления")

            if (idx + 1) % 3 == 0 or idx + 1 == total:
                try:
                    await progress_msg.edit_text(
                        _progress_text(
                            f"{op_label} каналам...",
                            idx + 1,
                            total,
                            len(ok_list),
                            len(err_list),
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    log_exc_swallow(log, "Сбой обновления прогресса bulk-операции")

            await asyncio.sleep(backoff(idx % 5, base=2.0, cap=20.0))
    except asyncio.CancelledError:
        log.info("_bulk_chan_exec_bg: отменено")
        raise
    except Exception:
        log_exc_swallow(log, "_bulk_chan_exec_bg: неожиданная ошибка")
    finally:
        await op_worker.release_accounts(_claimed_ids)

    header = (
        f"📊 <b>{op_label} каналам — завершено</b>\n\n"
        f"Всего: <b>{total}</b> | ✅ Успешно: <b>{len(ok_list)}</b> | ❌ Ошибок: <b>{len(err_list)}</b>\n\n"
    )
    detail = "\n".join((ok_list + err_list)[:40])
    if len(ok_list) + len(err_list) > 40:
        detail += f"\n<i>...и ещё {len(ok_list) + len(err_list) - 40} строк</i>"
    try:
        await progress_msg.edit_text(
            header + detail,
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
    except Exception:
        log_exc_swallow(log, "_bulk_chan_exec_bg: сбой финального отчёта")


@router.callback_query(ChanCb.filter(F.action == "bulk_chan_exec"))
async def cb_bulk_chan_exec(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    data = await state.get_data()
    op = data.get("bulk_op", "")
    selected_ids = data.get("bulk_selected", [])
    value = data.get("bulk_value", "")
    await state.clear()

    if not selected_ids or not value or op not in ("chan_uname", "chan_about"):
        await callback.message.edit_text(
            "⚠️ Данные операции устарели. Начните заново: /ops",
            reply_markup=_back_kb().as_markup(),
        )
        return

    base_uname = value.lstrip("@") if op == "chan_uname" else ""

    try:
        accounts = await pool.fetch(
            "SELECT a.id, a.session_str, a.first_name, a.phone, "
            "a.device_model, a.system_version, a.app_version, p.proxy_url "
            "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
            "WHERE a.owner_id=$1 AND a.id = ANY($2::bigint[]) AND a.is_active=TRUE AND a.session_str IS NOT NULL",
            callback.from_user.id,
            selected_ids,
        )
    except Exception as exc:
        mark_handled_error(f"bulk_chan_exec accounts: {exc}")
        await callback.message.edit_text(
            f"❌ Ошибка загрузки аккаунтов: <code>{html.escape(str(exc)[:200])}</code>",
            reply_markup=_back_kb().as_markup(),
        )
        return
    if not accounts:
        await callback.message.edit_text(
            "⚠️ Аккаунты не найдены. Начните заново: /ops",
            reply_markup=_back_kb().as_markup(),
        )
        return

    try:
        channels = await pool.fetch(
            "SELECT mc.channel_id, mc.title, mc.username, mc.acc_id, mc.access_hash "
            "FROM managed_channels mc "
            "WHERE mc.owner_id=$1 AND mc.acc_id = ANY($2::bigint[]) "
            "ORDER BY mc.acc_id, mc.title",
            callback.from_user.id,
            selected_ids,
        )
    except Exception:
        channels = []

    if not channels:
        await callback.message.edit_text(
            "⚠️ Нет каналов в базе для выбранных аккаунтов.\n\n"
            "Сначала загрузите каналы через <b>🔎 Мои каналы → Загрузить из Telegram</b>.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    total = len(channels)
    op_label = "🔤 Username" if op == "chan_uname" else "📄 Описание"
    progress_msg = await callback.message.edit_text(
        f"⏳ <b>{op_label} каналам (bulk)</b>\n\n"
        f"Каналов: <b>{total}</b> | Аккаунтов: <b>{len(accounts)}</b>\n\n"
        f"⏳ 0 / {total}",
        parse_mode="HTML",
    )

    acc_by_id = {acc["id"]: dict(acc) for acc in accounts}
    task = asyncio.create_task(
        _bulk_chan_exec_bg(
            pool,
            callback.from_user.id,
            progress_msg,
            list(channels),
            acc_by_id,
            op,
            base_uname,
            value,
            op_label,
            total,
        )
    )
    _treg.register(
        callback.from_user.id,
        "bulk_chan_exec",
        f"{op_label} {total} каналов",
        task,
    )


# ══════════════════════════════════════════════════════════════════════════
# MY CHANNELS — browse channels from connected accounts
# ══════════════════════════════════════════════════════════════════════════

_CHANS_PAGE_SIZE = 8


@router.callback_query(ChanCb.filter(F.action == "my_chans"))
async def cb_my_chans(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _STARTER):
        await callback.message.edit_text(
            "🔒 <b>Мои каналы — 💎 ПОДПИСКА</b>\n\nОформить: /subscription",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    if not active:
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\nДобавьте через /accounts",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    if len(active) == 1:
        try:
            acc = await pool.fetchrow(
                "SELECT * FROM tg_accounts WHERE id=$1", active[0]["id"]
            )
        except Exception:
            acc = None
        if not acc:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        await state.update_data(
            my_chans_acc_id=acc["id"], my_chans_session=acc["session_str"]
        )
        await state.set_state(MyChannelsFSM.browsing)
        await _show_my_chans_page(
            callback.message,
            pool,
            acc["session_str"],
            acc["id"],
            page=0,
            edit=True,
            owner_id=callback.from_user.id,
            acc_row=dict(acc),
        )
        return
    kb = InlineKeyboardBuilder()
    for a in active:
        kb.button(
            text=_acc_label(a),
            callback_data=ChanCb(action="my_chans_acc", acc_id=a["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    await state.set_state(MyChannelsFSM.choosing_account)
    await callback.message.edit_text(
        "📋 <b>Мои каналы/чаты</b>\n\nВыберите аккаунт для просмотра:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "my_chans_acc"))
async def cb_my_chans_acc(
    callback: CallbackQuery,
    callback_data: ChanCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    try:
        acc = await pool.fetchrow(
            "SELECT * FROM tg_accounts WHERE id=$1 AND owner_id=$2",
            callback_data.acc_id,
            callback.from_user.id,
        )
    except Exception:
        acc = None
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(
        my_chans_acc_id=acc["id"], my_chans_session=acc["session_str"]
    )
    await state.set_state(MyChannelsFSM.browsing)
    await _show_my_chans_page(
        callback.message,
        pool,
        acc["session_str"],
        acc["id"],
        page=0,
        edit=True,
        owner_id=callback.from_user.id,
        acc_row=dict(acc),
    )


@router.callback_query(ChanCb.filter(F.action == "my_chans_page"))
async def cb_my_chans_page(
    callback: CallbackQuery,
    callback_data: ChanCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    data = await state.get_data()
    session = data.get("my_chans_session")
    acc_id = data.get("my_chans_acc_id")
    if not session:
        await callback.message.edit_text(
            "⚠️ Сессия устарела. Начните заново: /ops",
            reply_markup=_back_kb().as_markup(),
        )
        return
    await _show_my_chans_page(
        callback.message,
        pool,
        session,
        acc_id,
        page=callback_data.page,
        edit=True,
        owner_id=callback.from_user.id,
    )


@router.callback_query(ChanCb.filter(F.action == "my_chans_refresh"))
async def cb_my_chans_refresh(
    callback: CallbackQuery,
    callback_data: ChanCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer("🔄 Обновляю из Telegram...")
    data = await state.get_data()
    session = data.get("my_chans_session")
    acc_id = callback_data.acc_id or data.get("my_chans_acc_id")
    if not session:
        await callback.message.edit_text(
            "⚠️ Сессия устарела. Начните заново: /ops",
            reply_markup=_back_kb().as_markup(),
        )
        return
    await _show_my_chans_page(
        callback.message,
        pool,
        session,
        acc_id,
        page=0,
        edit=True,
        force_refresh=True,
        owner_id=callback.from_user.id,
    )


async def _show_my_chans_page(
    msg,
    pool: asyncpg.Pool,
    session_str: str,
    acc_id: int,
    page: int,
    edit: bool = True,
    force_refresh: bool = False,
    owner_id: int = 0,
    acc_row: dict | None = None,
) -> None:
    from services import account_manager
    from database.db import get_managed_channels, upsert_managed_channels

    cached = await get_managed_channels(pool, owner_id, acc_id) if owner_id else []
    need_fetch = force_refresh or not cached

    if need_fetch:
        try:
            if edit:
                await msg.edit_text(
                    "⏳ Загружаю список каналов из Telegram...", parse_mode="HTML"
                )
        except Exception:
            log_exc_swallow(log, "Сбой отображения статуса загрузки каналов")
        try:
            owned = await account_manager.scan_owned_assets(session_str, _acc=acc_row)
            raw_channels = owned.get("channels", [])
            raw_groups = owned.get("groups", [])
            # Merge into uniform format with type info
            raw = []
            for ch in raw_channels:
                raw.append({**ch, "type": "channel"})
            for gr in raw_groups:
                raw.append({**gr, "type": "megagroup"})
        except Exception as e:
            kb = _back_kb()
            try:
                await msg.edit_text(
                    f"❌ Не удалось загрузить каналы: {html.escape(str(e)[:80])}",
                    parse_mode="HTML",
                    reply_markup=kb.as_markup(),
                )
            except Exception:
                log_exc_swallow(log, "Сбой отображения ошибки загрузки каналов")
            return
        if owner_id:
            await upsert_managed_channels(pool, owner_id, acc_id, raw)
        dialogs = raw
    else:
        dialogs = [
            {
                "id": r["channel_id"],
                "title": r["title"] or "",
                "username": r["username"] or "",
                "type": "channel",
                "members": 0,
            }
            for r in cached
        ]

    total = len(dialogs)
    total_pages = max(1, (total + _CHANS_PAGE_SIZE - 1) // _CHANS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = dialogs[page * _CHANS_PAGE_SIZE : (page + 1) * _CHANS_PAGE_SIZE]

    kb = InlineKeyboardBuilder()
    for ch in chunk:
        ch_type = "📢" if ch.get("type") == "channel" else "👥"
        uname = f" @{ch['username']}" if ch.get("username") else ""
        members = f" · {ch['members']:,}" if ch.get("members") else ""
        label = f"{ch_type} {(ch.get('title') or '')[:28]}{uname}{members}"
        kb.button(
            text=label,
            callback_data=ChanCb(
                action="my_chans_item", channel_id=ch["id"], acc_id=acc_id
            ),
        )
    kb.adjust(1)

    nav_row = []
    if page > 0:
        nav_row.append(
            ("◀ Пред.", ChanCb(action="my_chans_page", page=page - 1, acc_id=acc_id))
        )
    if page < total_pages - 1:
        nav_row.append(
            ("След. ▶", ChanCb(action="my_chans_page", page=page + 1, acc_id=acc_id))
        )
    for btn_label, cd in nav_row:
        kb.button(text=btn_label, callback_data=cd)
    if nav_row:
        kb.adjust(*([1] * len(chunk)), len(nav_row))

    kb.button(
        text="🔄 Обновить из Telegram",
        callback_data=ChanCb(action="my_chans_refresh", acc_id=acc_id),
    )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))

    src = "Telegram" if need_fetch else "кэш"
    text = (
        f"📋 <b>Мои каналы/чаты</b>\n\n"
        f"Всего: <b>{total}</b> · Страница <b>{page + 1}/{total_pages}</b> · <i>{src}</i>\n\n"
        "Нажмите на канал для управления:"
    )
    try:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception:
        log_exc_swallow(log, "Сбой отображения списка каналов")


@router.callback_query(ChanCb.filter(F.action == "my_chans_item"))
async def cb_my_chans_item(
    callback: CallbackQuery,
    callback_data: ChanCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    ch_id = callback_data.channel_id
    acc_id = callback_data.acc_id

    kb = InlineKeyboardBuilder()
    kb.button(
        text="📤 Опубликовать пост",
        callback_data=ChanCb(action="my_chans_post", channel_id=ch_id, acc_id=acc_id),
    )
    kb.button(
        text="🚪 Покинуть",
        callback_data=ChanCb(action="my_chans_leave", channel_id=ch_id, acc_id=acc_id),
    )
    kb.button(
        text="◀️ К списку",
        callback_data=ChanCb(action="my_chans_page", page=0, acc_id=acc_id),
    )
    kb.adjust(1)

    await callback.message.edit_text(
        f"📋 <b>Действия с каналом</b>\n\n"
        f"ID: <code>{ch_id}</code>\n\n"
        "Выберите действие:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "my_chans_leave"))
async def cb_my_chans_leave(
    callback: CallbackQuery,
    callback_data: ChanCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    data = await state.get_data()
    acc = None
    session = data.get("my_chans_session")
    if not session:
        try:
            acc = await db.get_account_for_telethon(
                pool, callback_data.acc_id, callback.from_user.id
            )
        except Exception:
            acc = None
        session = acc["session_str"] if acc else None
    if not session:
        await callback.message.edit_text(
            "⚠️ Сессия устарела. Начните заново: /ops",
            reply_markup=_back_kb().as_markup(),
        )
        return
    from services import account_manager

    progress = await callback.message.edit_text(
        "⏳ Покидаю канал...", parse_mode="HTML"
    )
    ok = await account_manager.leave_channel(
        session, str(callback_data.channel_id), _acc=dict(acc) if acc else None
    )
    kb = InlineKeyboardBuilder()
    kb.button(
        text="◀️ К списку",
        callback_data=ChanCb(
            action="my_chans_page", page=0, acc_id=callback_data.acc_id
        ),
    )
    await progress.edit_text(
        "✅ Вы покинули канал!"
        if ok
        else "❌ Не удалось покинуть канал. Проверьте права.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "my_chans_post"))
async def cb_my_chans_post(
    callback: CallbackQuery, callback_data: ChanCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.update_data(
        my_chans_post_ch_id=callback_data.channel_id,
        my_chans_post_acc_id=callback_data.acc_id,
    )
    await state.set_state(MyChannelsFSM.posting)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="my_chans"))
    await callback.message.edit_text(
        f"📤 <b>Публикация поста</b>\n\n"
        f"Канал ID: <code>{callback_data.channel_id}</code>\n\n"
        "Введите текст поста (поддерживается HTML):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(MyChannelsFSM.posting, F.text)
async def fsm_my_chans_post_text(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    text_to_post = (message.text or "").strip()
    if not text_to_post:
        await message.answer("⚠️ Текст не может быть пустым.")
        return
    data = await state.get_data()
    ch_id = data.get("my_chans_post_ch_id")
    acc_id = data.get("my_chans_post_acc_id")
    await state.clear()

    try:
        session_row = await pool.fetchrow(
            "SELECT * FROM tg_accounts WHERE id=$1 AND owner_id=$2",
            acc_id,
            message.from_user.id,
        )
    except Exception:
        session_row = None
    if not session_row:
        await message.answer("⚠️ Аккаунт не найден. Начните заново: /ops")
        return
    from services import account_manager

    try:
        access_hash_row = await pool.fetchrow(
            "SELECT access_hash FROM managed_channels WHERE owner_id=$1 AND channel_id=$2",
            message.from_user.id,
            ch_id,
        )
    except Exception:
        access_hash_row = None
    access_hash = (access_hash_row["access_hash"] if access_hash_row else 0) or 0
    msg = await message.answer("⏳ Публикую...")
    result = await account_manager.post_to_channel(
        session_row["session_str"],
        ch_id,
        text_to_post,
        access_hash=access_hash,
        _acc=dict(session_row),
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К каналам", callback_data=ChanCb(action="my_chans"))
    if "msg_id" in result:
        await msg.edit_text(
            f"✅ <b>Пост опубликован!</b>\n\nID сообщения: <code>{result['msg_id']}</code>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    else:
        err_detail = html.escape(result.get("error", "неизвестная ошибка")[:120])
        await msg.edit_text(
            f"❌ <b>Ошибка публикации</b>\n\n<code>{err_detail}</code>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )


# ── Contact Invite Flow ────────────────────────────────────────────────────────


def _cinv_channel_picker_kb(channels: list, page: int = 0) -> InlineKeyboardBuilder:
    PAGE = 10
    start = page * PAGE
    chunk = channels[start : start + PAGE]
    kb = InlineKeyboardBuilder()
    for ch in chunk:
        label = (
            f"@{ch['username']}"
            if ch.get("username")
            else (ch.get("title") or str(ch["channel_id"]))[:32]
        )
        kb.button(
            text=label,
            callback_data=ContactInvCb(
                action="pick_channel", channel_id=ch["channel_id"]
            ),
        )
    nav = []
    if page > 0:
        nav.append(InlineKeyboardBuilder())
        kb.button(
            text="◀️", callback_data=ContactInvCb(action="chans_page", page=page - 1)
        )
    if start + PAGE < len(channels):
        kb.button(
            text="▶️", callback_data=ContactInvCb(action="chans_page", page=page + 1)
        )
    kb.button(
        text="✏️ Ввести вручную", callback_data=ContactInvCb(action="enter_channel")
    )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    return kb


def _cinv_acc_picker_kb(accounts: list, selected: set) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for acc in accounts:
        mark = "✅" if acc["id"] in selected else "⬜"
        label = _acc_label(acc)
        kb.button(
            text=f"{mark} {label}",
            callback_data=ContactInvCb(action="toggle_acc", acc_id=acc["id"]),
        )
    if selected:
        kb.button(
            text=f"🚀 Продолжить ({len(selected)} акк.)",
            callback_data=ContactInvCb(action="proceed"),
        )
    if len(selected) < len(accounts):
        kb.button(text="✅ Выбрать все", callback_data=ContactInvCb(action="all_accs"))
    else:
        kb.button(
            text="⬜ Снять выбор", callback_data=ContactInvCb(action="deselect_all")
        )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="contact_invite"))
    kb.adjust(1)
    return kb


@router.callback_query(ChanCb.filter(F.action == "contact_invite"))
async def cb_contact_invite_start(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    await state.clear()
    if not await require_plan(pool, callback.from_user.id, _PRO):
        from bot.utils.subscription import locked_text

        await callback.message.edit_text(
            locked_text("Инвайт из контактов", "pro"),
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    channels = await _get_managed_channels_cached(pool, callback.from_user.id)
    kb = _cinv_channel_picker_kb(channels, 0)
    total = len(channels)
    text = (
        f"👥 <b>Инвайт из контактов</b>\n\n"
        f"Выберите канал/чат куда пригласить контакты со всех аккаунтов:\n\n"
        f"<i>Каналов в кэше: {total}. Нет нужного — введите вручную или обновите список в «Мои каналы/чаты».</i>"
        if channels
        else "👥 <b>Инвайт из контактов</b>\n\n"
        "Кэш каналов пуст. Введите @username или числовой ID канала/группы вручную.\n\n"
        "<i>Чтобы заполнить кэш: /ops → 📋 Мои каналы/чаты</i>"
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(ContactInvCb.filter(F.action == "chans_page"))
async def cb_cinv_chans_page(
    callback: CallbackQuery,
    callback_data: ContactInvCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    channels = await _get_managed_channels_cached(pool, callback.from_user.id)
    kb = _cinv_channel_picker_kb(channels, callback_data.page)
    await callback.message.edit_reply_markup(reply_markup=kb.as_markup())


@router.callback_query(ContactInvCb.filter(F.action == "enter_channel"))
async def cb_cinv_enter_channel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ContactInviteFSM.entering_channel)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="contact_invite"))
    await callback.message.edit_text(
        "✏️ <b>Введите @username или ID канала/группы</b>\n\n"
        "Примеры:\n"
        "• <code>@mychannel</code>\n"
        "• <code>-1001234567890</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(ContactInviteFSM.entering_channel, F.text)
async def fsm_cinv_channel_input(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    raw = message.text.strip()
    try:
        ch_id = int(raw)
        identifier = str(ch_id)
        display = str(ch_id)
        access_hash = 0
        _channel_id = ch_id
    except ValueError:
        identifier = raw if raw.startswith("@") else f"@{raw}"
        display = identifier
        access_hash = 0
        _channel_id = 0

    await state.update_data(
        channel_identifier=identifier,
        channel_display=display,
        channel_id=_channel_id,
        access_hash=access_hash,
    )
    accounts = await _get_accounts(pool, message.from_user.id)
    if not accounts:
        await message.answer("⚠️ Нет подключённых аккаунтов. Добавьте через /accounts")
        await state.clear()
        return
    await state.set_state(ContactInviteFSM.choosing_accounts)
    kb = _cinv_acc_picker_kb(accounts, set())
    await message.answer(
        f"📱 <b>Выберите аккаунты</b>\n\nКанал: <b>{display}</b>\n\n"
        "Контакты будут собраны со всех выбранных аккаунтов и объединены по уникальным пользователям:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ContactInvCb.filter(F.action == "pick_channel"))
async def cb_cinv_pick_channel(
    callback: CallbackQuery,
    callback_data: ContactInvCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    ch_id = callback_data.channel_id
    try:
        row = await pool.fetchrow(
            "SELECT title, username, access_hash, acc_id FROM managed_channels WHERE owner_id=$1 AND channel_id=$2",
            callback.from_user.id,
            ch_id,
        )
    except Exception:
        row = None
    display = (
        f"@{row['username']}"
        if row and row["username"]
        else (row["title"] if row else str(ch_id))
    )
    access_hash = (row["access_hash"] if row else 0) or 0
    # Используем @username как join_identifier если есть — co-accounts могут вступить без invite link
    _username = row["username"] if row else None
    channel_identifier = f"@{_username}" if _username else str(ch_id)

    await state.update_data(
        channel_id=ch_id,
        channel_identifier=channel_identifier,
        channel_display=display,
        access_hash=access_hash,
        primary_acc_id=(row["acc_id"] if row else 0) or 0,
    )
    accounts = await _get_accounts(pool, callback.from_user.id)
    if not accounts:
        await callback.message.edit_text(
            "⚠️ Нет подключённых аккаунтов. Добавьте через /accounts",
            reply_markup=_back_kb().as_markup(),
        )
        await state.clear()
        return
    await state.set_state(ContactInviteFSM.choosing_accounts)
    kb = _cinv_acc_picker_kb(accounts, set())
    await callback.message.edit_text(
        f"📱 <b>Выберите аккаунты</b>\n\nКанал: <b>{display}</b>\n\n"
        "Контакты будут собраны со всех выбранных аккаунтов и объединены по уникальным пользователям:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _cinv_refresh_acc_picker(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    data = await state.get_data()
    selected = set(data.get("selected_accs", []))
    display = data.get("channel_display", "?")
    accounts = await _get_accounts(pool, callback.from_user.id)
    kb = _cinv_acc_picker_kb(accounts, selected)
    await callback.message.edit_text(
        f"📱 <b>Выберите аккаунты</b>\n\nКанал: <b>{display}</b>\n\n"
        f"Выбрано: <b>{len(selected)}</b> из {len(accounts)}:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ContactInvCb.filter(F.action == "toggle_acc"))
async def cb_cinv_toggle_acc(
    callback: CallbackQuery,
    callback_data: ContactInvCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    data = await state.get_data()
    selected = set(data.get("selected_accs", []))
    if callback_data.acc_id in selected:
        selected.discard(callback_data.acc_id)
    else:
        selected.add(callback_data.acc_id)
    await state.update_data(selected_accs=list(selected))
    await _cinv_refresh_acc_picker(callback, pool, state)


@router.callback_query(ContactInvCb.filter(F.action == "all_accs"))
async def cb_cinv_all_accs(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    accounts = await _get_accounts(pool, callback.from_user.id)
    selected = {a["id"] for a in accounts}
    await state.update_data(selected_accs=list(selected))
    await _cinv_refresh_acc_picker(callback, pool, state)


@router.callback_query(ContactInvCb.filter(F.action == "deselect_all"))
async def cb_cinv_deselect_all(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    await state.update_data(selected_accs=[])
    await _cinv_refresh_acc_picker(callback, pool, state)


@router.callback_query(ContactInvCb.filter(F.action == "proceed"))
async def cb_cinv_proceed(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    data = await state.get_data()
    selected_accs = data.get("selected_accs", [])
    primary_acc_id = int(data.get("primary_acc_id") or 0)
    channel_display = data.get("channel_display", "?")
    if not selected_accs:
        await callback.answer("Выберите хотя бы один аккаунт.", show_alert=True)
        return
    if primary_acc_id and primary_acc_id not in selected_accs:
        selected_accs = [primary_acc_id, *selected_accs]
        await state.update_data(selected_accs=selected_accs)
    await callback.answer()
    msg = await callback.message.edit_text(
        f"⏳ Подсчёт контактов с {len(selected_accs)} аккаунт(ов)...",
        parse_mode="HTML",
    )
    from services import account_manager as _am

    try:
        acc_rows = await pool.fetch(
            "SELECT a.id, a.session_str, a.first_name, a.username, "
            "a.device_model, a.system_version, a.app_version, p.proxy_url "
            "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
            "WHERE a.id = ANY($1::int[]) AND a.owner_id=$2 AND a.is_active=true",
            selected_accs,
            callback.from_user.id,
        )
    except Exception:
        acc_rows = []

    async def _fetch_one(acc) -> list:
        try:
            return await _am.get_contacts(acc["session_str"], _acc=dict(acc))
        except Exception:
            return []

    all_contact_lists = await asyncio.gather(*[_fetch_one(a) for a in acc_rows])
    unique_ids: set[int] = set()
    for contacts in all_contact_lists:
        for c in contacts:
            unique_ids.add(c["user_id"])

    await state.update_data(contact_count=len(unique_ids))
    await state.set_state(ContactInviteFSM.confirming)
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Запустить инвайт", callback_data=ContactInvCb(action="run"))
    kb.button(text="❌ Отмена", callback_data=ContactInvCb(action="cancel"))
    kb.adjust(1)
    est_min = max(1, len(unique_ids) // 60)
    await msg.edit_text(
        f"👥 <b>Подтверждение инвайта</b>\n\n"
        f"Канал: <b>{channel_display}</b>\n"
        f"Аккаунтов: <b>{len(acc_rows)}</b>\n"
        f"Уникальных контактов: <b>{len(unique_ids):,}</b>\n\n"
        f"⏱ Примерное время: ~{est_min} мин.\n"
        f"Процесс запустится в фоне, уведомление придёт по завершении.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ContactInvCb.filter(F.action == "run"))
async def cb_cinv_run(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    data = await state.get_data()
    selected_accs = data.get("selected_accs", [])
    primary_acc_id = int(data.get("primary_acc_id") or 0)
    channel_id = data.get("channel_id", 0)
    channel_identifier = data.get("channel_identifier", "")
    channel_display = data.get("channel_display", "?")
    access_hash = data.get("access_hash", 0)
    if primary_acc_id and primary_acc_id not in selected_accs:
        selected_accs = [primary_acc_id, *selected_accs]
    if not selected_accs or not channel_identifier:
        await callback.answer("Недостаточно данных. Начните заново.", show_alert=True)
        return
    await callback.answer()
    await state.clear()
    try:
        acc_rows = await pool.fetch(
            "SELECT a.id, a.session_str, a.tg_user_id, a.first_name, a.username, a.phone, "
            "a.device_model, a.system_version, a.app_version, p.proxy_url "
            "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
            "WHERE a.id = ANY($1::int[]) AND a.owner_id=$2 AND a.is_active=true",
            selected_accs,
            callback.from_user.id,
        )
    except Exception:
        acc_rows = []
    selected_order = {acc_id: idx for idx, acc_id in enumerate(selected_accs)}
    acc_rows = sorted(
        acc_rows,
        key=lambda acc: (
            0 if primary_acc_id and acc["id"] == primary_acc_id else 1,
            selected_order.get(acc["id"], 9999),
        ),
    )
    if not acc_rows:
        await callback.message.edit_text(
            "⚠️ Аккаунты не найдены или деактивированы.",
            reply_markup=_back_kb().as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отменить", callback_data=ContactInvCb(action="cancel_invite"))
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="contact_invite"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"🚀 <b>Инвайт запущен в фоне</b>\n\n"
        f"Канал: <b>{channel_display}</b>\n"
        f"Аккаунтов: <b>{len(acc_rows)}</b>\n\n"
        "<i>Система автоматически подготовит аккаунты к инвайту:\n"
        "1️⃣ Добавит их в канал\n"
        "2️⃣ Сделает администраторами\n"
        "3️⃣ Распределит контакты для инвайта</i>\n\n"
        "Уведомление придёт когда всё завершится.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
    # Create and store task for cancellation
    task = asyncio.create_task(
        _cinv_bg(
            bot=callback.bot,
            user_id=callback.from_user.id,
            acc_rows=list(acc_rows),
            channel_id=channel_id,
            channel_identifier=channel_identifier,
            access_hash=access_hash,
            channel_display=channel_display,
            pool=pool,
        )
    )
    _active_tasks[(callback.from_user.id, "cinv")] = task
    _treg.register(
        callback.from_user.id, "mass_join", f"Инвайт в {channel_display[:30]}", task
    )


@router.callback_query(ContactInvCb.filter(F.action == "cancel"))
async def cb_cinv_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено.")
    await callback.message.edit_text(
        "❌ Инвайт отменён.", reply_markup=_back_kb().as_markup()
    )


@router.callback_query(ContactInvCb.filter(F.action == "cancel_invite"))
async def cb_cinv_cancel_running(callback: CallbackQuery) -> None:
    """Отменить уже запущенный фоновый инвайт."""
    key = (callback.from_user.id, "cinv")
    task = _active_tasks.get(key)
    if task and not task.done():
        task.cancel()
        _active_tasks.pop(key, None)
        await callback.answer("✅ Инвайт отменяется...")
        await callback.message.edit_text(
            "❌ <b>Инвайт отменён</b>\n\nОперация прервана. Уже приглашённые контакты останутся в канале.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
    else:
        await callback.answer("Нет активного инвайта для отмены.", show_alert=True)


async def _get_managed_channels_cached(pool: asyncpg.Pool, owner_id: int) -> list:
    from database import db as _db

    return await _db.get_managed_channels(pool, owner_id)


async def _cinv_bg(
    bot,
    user_id: int,
    acc_rows: list,
    channel_id: int,
    channel_identifier: str,
    access_hash: int,
    channel_display: str,
    pool=None,
) -> None:
    """Background task: collect contacts from accounts and invite to channel.

    The first account in acc_rows is treated as the primary (channel admin).
    It auto-promotes all other accounts to admin before distributing invite work.
    Supports cancellation via asyncio.Task.cancel().
    """
    from services import account_manager as _am

    try:
        await _cinv_bg_inner(
            bot,
            user_id,
            acc_rows,
            channel_id,
            channel_identifier,
            access_hash,
            channel_display,
            pool,
            _am,
        )
    except asyncio.CancelledError as _ce:
        _is_user = bool(_ce.args and _ce.args[0] == "user_requested")
        _cinv_msg = (
            "❌ <b>Инвайт отменён</b>\n\nОперация прервана пользователем."
            if _is_user
            else "❌ <b>Инвайт прерван (перезапуск сервиса).</b>\n\n<i>Повторите операцию.</i>"
        )
        try:
            await bot.send_message(user_id, _cinv_msg, parse_mode="HTML")
        except Exception:
            log_exc_swallow(log, "Сбой уведомления об отмене инвайта")
        log.info(
            "_cinv_bg: cancelled (%s) user=%s",
            "user" if _is_user else "system",
            user_id,
        )
    except Exception as exc:
        try:
            await bot.send_message(
                user_id,
                f"⚠️ <b>Ошибка инвайта</b>\n\n<code>{html.escape(str(exc)[:200])}</code>",
                parse_mode="HTML",
            )
        except Exception:
            log_exc_swallow(log, "Сбой уведомления об ошибке инвайта")
        log.exception("_cinv_bg error user=%s: %s", user_id, exc)
    finally:
        _active_tasks.pop((user_id, "cinv"), None)


async def _cinv_bg_inner(
    bot,
    user_id: int,
    acc_rows: list,
    channel_id: int,
    channel_identifier: str,
    access_hash: int,
    channel_display: str,
    pool,
    _am,
) -> None:
    chan_target = channel_id if channel_id else channel_identifier
    _ch_disp = html.escape(channel_display)
    _n_accs = len(acc_rows)
    _has_join_step = _n_accs > 1 and pool is not None
    _total_steps = 3 if _has_join_step else 2
    invite_accounts = list(acc_rows)

    # Progress message — edited throughout instead of spamming new messages
    _pm = None
    _join_done = "✅ Аккаунты подключены\n" if _has_join_step else ""

    async def _upd(text: str) -> None:
        nonlocal _pm
        try:
            if _pm is None:
                _pm = await bot.send_message(user_id, text, parse_mode="HTML")
            else:
                await _pm.edit_text(text, parse_mode="HTML")
        except Exception:
            pass

    # 0. Auto-add co-accounts to channel, then promote to admin
    if _has_join_step:
        await _upd(
            f"⚙️ <b>Инвайт · {_ch_disp}</b>\n\n"
            f"📶 <b>Шаг 1/{_total_steps}:</b> Подключаю {_n_accs - 1} доп. аккаунта к каналу..."
        )
    if len(acc_rows) > 1 and pool is not None:
        primary = acc_rows[0]
        primary_dict = dict(primary)
        ready_accounts = [primary]

        # Resolve join identifier. Prefer invite link so private channels work.
        join_identifier: str | None = None
        try:
            invite_link = await _am.get_channel_invite_link(
                primary["session_str"],
                chan_target,
                _acc=primary_dict,
                access_hash=access_hash,
            )
            if invite_link:
                join_identifier = invite_link
        except Exception as e:
            log.warning("cinv get_invite_link: %s", e)
        if (
            not join_identifier
            and channel_identifier
            and channel_identifier.startswith("@")
        ):
            join_identifier = channel_identifier

        # Join ALL co-accounts in PARALLEL — no sequential waits between them
        if join_identifier or (channel_id and access_hash):

            async def _join_one(other: dict) -> bool:
                try:
                    if join_identifier:
                        result = await _am.join_channel(
                            other["session_str"], join_identifier, _acc=dict(other)
                        )
                        ok = not result.get("error")
                    else:
                        result = await _am.join_channel_by_id(
                            other["session_str"],
                            channel_id,
                            access_hash,
                            _acc=dict(other),
                        )
                        ok = result.get("ok", False)
                    if ok:
                        log.info(
                            "cinv: co-account %s joined %s", other["id"], chan_target
                        )
                    else:
                        log.warning(
                            "cinv join error acc=%s: %s",
                            other["id"],
                            result.get("error") or result.get("error_msg", "unknown"),
                        )
                    return ok
                except Exception as e:
                    log.warning("cinv co-account join acc=%s: %s", other["id"], e)
                    return False

            join_candidates = list(acc_rows[1:])
            join_results = await asyncio.gather(
                *[_join_one(other) for other in join_candidates],
                return_exceptions=False,
            )
            joined_acc_ids = {
                acc["id"] for acc, ok in zip(join_candidates, join_results) if ok
            }
            added_ok = sum(1 for r in join_results if r)
            log.info(
                "cinv: joined %d/%d co-accounts to %s",
                added_ok,
                len(acc_rows) - 1,
                chan_target,
            )
        else:
            added_ok = 0
            joined_acc_ids = set()
            log.warning(
                "cinv: no join_identifier and no channel_id+access_hash — skipping co-account joins"
            )

        # Brief wait before promoting (let server register membership)
        await asyncio.sleep(random.uniform(3, 8))

        # Promote co-accounts to admin SEQUENTIALLY (primary makes the API calls)
        # Short delays only — we already waited enough in the join phase
        promo_ok = 0
        for idx, other in enumerate(acc_rows[1:]):
            if other["id"] not in joined_acc_ids:
                continue
            tg_uid = other.get("tg_user_id")
            if not tg_uid:
                try:
                    tg_uid = await _am.get_own_user_id(
                        other["session_str"], _acc=dict(other)
                    )
                    if tg_uid:
                        await pool.execute(
                            "UPDATE tg_accounts SET tg_user_id=$1 WHERE id=$2",
                            tg_uid,
                            other["id"],
                        )
                except Exception:
                    log_exc_swallow(
                        log, "Сбой получения tg_user_id для promote_to_admin"
                    )
            if tg_uid:
                try:
                    ok = await _am.promote_to_admin(
                        primary["session_str"],
                        channel_id if channel_id else channel_identifier,
                        tg_uid,
                        _acc=primary_dict,
                        access_hash=access_hash,
                    )
                    if ok:
                        promo_ok += 1
                        ready_accounts.append(other)
                        log.info("cinv: promoted co-account %s to admin", tg_uid)
                    else:
                        log.warning("cinv promote failed for user %s", tg_uid)
                except Exception as e:
                    log.warning("cinv auto-promote acc=%s: %s", other["id"], e)
            # Short pause between promotes — primary account, one request at a time
            if idx < len(acc_rows) - 2:
                await asyncio.sleep(random.uniform(5, 12))

        if promo_ok > 0:
            log.info(
                "cinv: promoted %d co-accounts to admin in %s", promo_ok, chan_target
            )
        invite_accounts = ready_accounts

    # 1. Collect contacts from ALL accounts in PARALLEL
    _contacts_step = 2 if _has_join_step else 1
    await _upd(
        f"⚙️ <b>Инвайт · {_ch_disp}</b>\n\n"
        f"{_join_done}"
        f"📇 <b>Шаг {_contacts_step}/{_total_steps}:</b> Собираю контакты с {_n_accs} аккаунтов..."
    )

    async def _get_contacts_one(acc: dict) -> list:
        try:
            return await _am.get_contacts(acc["session_str"], _acc=dict(acc))
        except Exception as e:
            log.warning("cinv get_contacts acc=%s: %s", acc["id"], e)
            return []

    all_contact_lists = await asyncio.gather(
        *[_get_contacts_one(acc) for acc in acc_rows],
        return_exceptions=False,
    )
    contacts_map: dict[int, dict] = {}
    for contacts in all_contact_lists:
        for c in contacts:
            contacts_map[c["user_id"]] = c

    if not contacts_map:
        await _upd(
            "⚠️ <b>Инвайт: нет контактов</b>\n\nНи у одного аккаунта не найдено контактов."
        )
        return

    # 2. Build identifier list: @username preferred, phone as fallback
    identifiers: list[str] = []
    for c in contacts_map.values():
        if c["username"]:
            identifiers.append(f"@{c['username']}")
        elif c["phone"]:
            ph = c["phone"]
            identifiers.append(ph if ph.startswith("+") else f"+{ph}")

    if not identifiers:
        await _upd(
            "⚠️ <b>Инвайт: нет идентификаторов</b>\n\n"
            "У контактов нет username и телефонов — невозможно пригласить."
        )
        return

    # 3. Split contacts round-robin among prepared admin accounts.
    per_account_cap = 25 if len(invite_accounts) == 1 else 35
    total_cap = max(1, len(invite_accounts)) * per_account_cap
    invite_identifiers = identifiers[:total_cap]
    skipped_by_cap = max(0, len(identifiers) - len(invite_identifiers))
    n = len(invite_accounts)
    chunks = [invite_identifiers[i::n][:per_account_cap] for i in range(n)]
    if skipped_by_cap:
        log.info(
            "cinv: skipped %d contacts by conservative per-run caps", skipped_by_cap
        )

    # 4. Invite in PARALLEL — each account works on its own chunk simultaneously
    _invite_step = _total_steps
    await _upd(
        f"⚙️ <b>Инвайт · {_ch_disp}</b>\n\n"
        f"{_join_done}"
        f"✅ Контактов: <b>{len(identifiers):,}</b> ({len(contacts_map)} уник.)\n"
        f"📨 <b>Шаг {_invite_step}/{_total_steps}:</b> Рассылаю инвайты ({_n_accs} аккаунтов)..."
    )

    async def _invite_one(acc: dict, chunk: list) -> tuple[int, int]:
        if not chunk:
            return 0, 0
        try:
            log.info(
                "cinv: account %s inviting %d users to %s",
                acc["id"],
                len(chunk),
                chan_target,
            )
            res = await _am.invite_users_to_channel(
                acc["session_str"],
                channel_id if channel_id else channel_identifier,
                chunk,
                _acc=dict(acc),
                access_hash=access_hash,
                batch_size=8,
                batch_delay=random.uniform(240, 480),
            )
            invited = res.get("invited", 0)
            failed = len(res.get("failed", []))
            if res.get("error"):
                log.warning("cinv hard error acc=%s: %s", acc["id"], res["error"])
                failed += max(0, len(chunk) - invited)
            log.info(
                "cinv: account %s invited=%d failed=%d", acc["id"], invited, failed
            )
            return invited, failed
        except Exception as e:
            log.warning("cinv invite acc=%s: %s", acc["id"], e)
            return 0, len(chunk)

    invite_results = await asyncio.gather(
        *[_invite_one(acc, chunk) for acc, chunk in zip(invite_accounts, chunks)],
        return_exceptions=False,
    )
    total_invited = sum(r[0] for r in invite_results)
    total_failed = sum(r[1] for r in invite_results)

    # 5. Notify user — edit the progress message with final summary + per-account breakdown
    _breakdown_lines = []
    for acc, (ok, fail), chunk in zip(invite_accounts, invite_results, chunks):
        name = (acc.get("first_name") or f"acc{acc['id']}")[:16]
        _breakdown_lines.append(f"  {html.escape(name)}: {ok}/{len(chunk)}")
    _breakdown = "\n".join(_breakdown_lines) if _breakdown_lines else ""

    await _upd(
        f"✅ <b>Инвайт завершён!</b>\n\n"
        f"Канал: <b>{_ch_disp}</b>\n"
        f"Контактов: <b>{len(identifiers):,}</b> · приглашено: <b>{total_invited}</b> · ошибок: <b>{total_failed}</b>"
        + (f"\n\n📊 <b>Разбивка:</b>\n{_breakdown}" if _breakdown else "")
    )
