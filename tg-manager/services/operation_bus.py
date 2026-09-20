"""
Operation Bus — универсальный механизм постановки операций в очередь.

Заменяет прямые INSERT INTO operation_queue в 20+ handler-файлах.
Предоставляет единый API: submit / cancel / get_status / list_active.

Контракт:
  - Все op_type из OP_REGISTRY проходят через этот модуль
  - Прямые INSERT INTO operation_queue в новых handler'ах — запрещены
  - Существующие прямые INSERT — оставить как есть (инкрементальная миграция)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
import logging
from typing import Any, Optional

import asyncpg

log = logging.getLogger(__name__)

# Окно идемпотентности постановки операции (сек). См. submit(dedup_window_sec).
# 20с покрывает и двойной тап (<1с), и retry клиента после таймаута, но слишком
# коротко, чтобы подавить намеренный повторный запуск позже.
DEFAULT_DEDUP_WINDOW_SEC = 20


class PlanRequiredError(PermissionError):
    """Операция требует платной подписки, а у владельца её нет.

    Централизует enforcement OP_REGISTRY[op]['min_plan'] в submit(): раньше
    min_plan только объявлялся, но проверялся исключительно в хендлерах — если
    хендлер забывал гейт, free-юзер мог поставить платную операцию.
    """

    def __init__(self, op_type: str, required_plan: str) -> None:
        self.op_type = op_type
        self.required_plan = required_plan
        super().__init__(f"operation {op_type!r} requires plan {required_plan!r}")


class ImmunityBlockedError(PermissionError):
    """Тип операции временно приостановлен предохранителем Ban Weather.

    Взводится автоматически, когда паттерн прямо сейчас массово убивает
    аккаунты владельца (immunity_engine, шторм). Наследник PermissionError —
    middleware мини-аппа уже отдаёт такие ошибки чистым 403 с текстом причины,
    а не «внутренней ошибкой». Снимается автоматически по истечении cooldown.
    """

    def __init__(self, op_type: str, reason: str, until=None) -> None:
        self.op_type = op_type
        self.reason = reason
        self.until = until
        super().__init__(reason)


async def _enforce_immunity(pool, owner_id: int, op_type: str) -> None:
    """Спросить предохранитель Ban Weather перед постановкой операции.

    Fail-open: любая ошибка проверки НЕ должна мешать работе — иммунитет это
    страховка, а не критический путь (тот же принцип, что у проверки плана).
    """
    try:
        from services import immunity_engine

        policy = await immunity_engine.check_policy(pool, owner_id, op_type)
    except Exception:
        log.warning(
            "operation_bus: immunity check errored for op=%s owner=%s — allowing (fail-open)",
            op_type, owner_id, exc_info=True,
        )
        return
    if policy and policy.get("action") == "block":
        raise ImmunityBlockedError(op_type, policy.get("reason") or
                                   "Тип операции временно приостановлен", policy.get("until"))


async def _enforce_min_plan(pool, owner_id: int, op_type: str, meta: dict) -> None:
    """Проверить подписку владельца против min_plan операции.

    Fail-open: если сам механизм проверки недоступен/упал — НЕ блокируем (лучше
    пропустить, чем сломать масс-операцию из-за бага в проверке). Блокируем
    только при однозначно недостаточном тарифе. Админы и free-mode пропускаются
    внутри require_plan.
    """
    min_plan = meta.get("min_plan")
    if not min_plan:
        return
    try:
        from bot.utils.subscription import require_plan, coerce_plan
    except Exception:
        log.warning("operation_bus: subscription module unavailable — skip plan gate", exc_info=True)
        return
    if coerce_plan(min_plan) == "free":
        return
    try:
        allowed = await require_plan(pool, owner_id, min_plan)
    except Exception:
        log.warning(
            "operation_bus: plan check errored for op=%s owner=%s — allowing (fail-open)",
            op_type, owner_id, exc_info=True,
        )
        return
    if not allowed:
        raise PlanRequiredError(op_type, coerce_plan(min_plan))


# ── Registry всех типов операций ─────────────────────────────────────────────
# Ключ: op_type (совпадает с op_worker dispatch)
# description: отображается пользователю в очереди
# min_plan: минимальная подписка для выполнения (или None)
# max_retries: количество автоматических повторов при временной ошибке
# timeout_sec: потолок ОДНОГО прогона операции (необязательный). Объявляется
#     только там, где тип заведомо выбивается из общего потолка: либо работает
#     дольше (массовый инвайт с пейсингом на часы), либо обязан падать быстро
#     (обслуживающая операция, которой нечего ждать). Остальные берут
#     op_worker._OP_TIMEOUT_DEFAULT_S.
# retry_targets: описание точечного повтора упавших ЦЕЛЕЙ (необязательное):
#     {"param": "<поле params со списком целей>",
#      "prefix": "<префикс в operation_log.target, если есть>",
#      "kind": "int" | "str"}
#     Объявляется ТОЛЬКО когда operation_log.target однозначно обратим в
#     элемент этого списка. У большинства операций в лог пишется исполнитель
#     («acc#123») или человекочитаемая метка (заголовок канала) — повторять по
#     ним нельзя: это либо не цель, либо необратимо в идентификатор. Молчаливо
#     угадывать здесь опаснее, чем не давать кнопку: повтор ушёл бы не по тем
#     целям, а операция отчиталась бы об успехе.
OP_REGISTRY: dict[str, dict] = {
    "mass_publish": {
        "description": "Массовая публикация",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "📤",
    },
    "bulk_seo_apply": {
        "description": "Применение SEO по сетке",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🔍",
        # op_worker пишет target=f"ch#{chan_id}" — префикс + числовой id.
        "retry_targets": {"param": "channel_ids", "prefix": "ch#", "kind": "int"},
    },
    "bulk_join": {
        "description": "Массовое вступление в каналы",
        "min_plan": "starter",
        "max_retries": 3,
        "icon": "📥",
        # target = сама ссылка-приглашение, как она пришла в params.
        "retry_targets": {"param": "links", "kind": "str", "alt_params": ["targets"]},
    },
    "bulk_leave": {
        "description": "Массовый выход из каналов",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "📤",
        # target = str(channel) — элемент списка channels как есть.
        "retry_targets": {"param": "channels", "kind": "str"},
    },
    "find_contact": {
        "description": "Поиск потерянного контакта",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🔎",
    },
    "bulk_bot_edit": {
        "description": "Массовое редактирование ботов",
        "min_plan": "pro",
        "max_retries": 2,
        "icon": "🤖",
    },
    "bulk_create_channels": {
        "description": "Массовое создание каналов",
        "min_plan": "pro",
        "max_retries": 1,
        "icon": "📡",
    },
    "bot_factory": {
        "description": "Создание ботов через BotFather",
        "min_plan": "pro",
        "max_retries": 1,
        "icon": "🤖",
    },
    "global_presence_channel": {
        "description": "Global Presence — каналы",
        "min_plan": "pro",
        "max_retries": 2,
        "icon": "🌍",
    },
    "global_presence_group": {
        "description": "Global Presence — группы",
        "min_plan": "pro",
        "max_retries": 2,
        "icon": "🌍",
    },
    "global_presence_bot": {
        "description": "Global Presence — бот",
        "min_plan": "pro",
        "max_retries": 2,
        "icon": "🌍",
    },
    "global_presence_package": {
        "description": "Global Presence — пакет",
        "min_plan": "pro",
        "max_retries": 2,
        "icon": "🌍",
    },
    "gp_bulk_apply": {
        "description": "Проект — пакетное применение оформления",
        "min_plan": "pro",
        "max_retries": 2,
        "icon": "🧰",
    },
    "global_presence_full_package": {
        "description": "Global Presence — полный пакет",
        "min_plan": "enterprise",
        "max_retries": 2,
        "icon": "🌍",
    },
    "strike": {
        "description": "Strike — эшелонированная жалоба",
        "min_plan": "pro",
        "max_retries": 1,
        "icon": "⚡",
    },
    "gift_transfer": {
        "description": "Передача Telegram-подарков",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "🎁",
    },
    "dm_campaign": {
        "description": "DM-кампания",
        "min_plan": "enterprise",
        "max_retries": 1,
        "icon": "📨",
    },
    "network_broadcast": {
        "description": "Сетевая рассылка",
        "min_plan": "enterprise",
        "max_retries": 1,
        "icon": "📢",
    },
    "seed_presence_pack": {
        "description": "Посев постов в Presence Pack",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "🌱",
    },
    "promote_presence_pack": {
        "description": "Назначение бота администратором Presence Pack",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "👑",
    },
    "bulk_edit_channels": {
        "description": "Массовое редактирование каналов",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "✏️",
    },
    "group_import_all": {
        "description": "Импорт групп со всех аккаунтов",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "📥",
    },
    "group_announce": {
        "description": "Объявление во все группы аккаунта",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "📢",
    },
    "bulk_dm_adhoc": {
        "description": "Рассылка личных сообщений",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "📨",
    },
    "bulk_post_to_channel": {
        "description": "Массовая публикация в канал",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "📤",
    },
    "pin_last_post": {
        "description": "Закрепить последний пост канала",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "📌",
    },
    "bulk_update_profile": {
        "description": "Массовое обновление профилей",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "✏️",
    },
    "bulk_chan_exec": {
        "description": "Bulk username/about для каналов",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "✏️",
    },
    "bulk_post_chans": {
        "description": "Публикация поста в каналы аккаунта",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "📤",
    },
    "channel_import_all": {
        "description": "Импорт каналов со всех аккаунтов",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "📡",
    },
    "check_accounts_health": {
        "description": "Проверка статуса всех аккаунтов",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🔍",
    },
    "scan_owned_resources": {
        "description": "Сканирование собственных каналов/групп",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🔎",
    },
    "check_channel_rankings": {
        "description": "Замер позиций каналов/чатов в поиске по ключам",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "📈",
    },
    "scan_owned_bots": {
        "description": "Поиск ботов на аккаунтах флота (@BotFather)",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🤖",
    },
    "connect_discovered_bots": {
        "description": "Подключение найденных на флоте ботов (токены от @BotFather)",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🔌",
    },
    "enable_bot_to_bot": {
        "description": "Включение режима bot-to-bot у сети ботов (@BotFather)",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🕸",
    },
    "reclassify_channels": {
        "description": "Переопределение моей инфраструктуры (убрать чужие каналы)",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🔄",
    },
    "promote_all_admins": {
        "description": "Назначение всех аккаунтов администраторами канала",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "👑",
    },
    "boost_views": {
        "description": "Накрутка просмотров постов",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "👁",
    },
    "boost_reactions": {
        "description": "Накрутка реакций на посты",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "❤️",
    },
    "boost_stories": {
        "description": "Накрутка просмотров историй",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "📖",
    },
    "boost_subscribers": {
        "description": "Накрутка подписчиков/участников",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "👥",
    },
    "boost_bot_starts": {
        "description": "Накрутка стартов в ботах",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "🚀",
    },
    "contacts_sync": {
        # Синхронизация контактов флота в единый хаб. Фоновая, т.к. 20+ аккаунтов
        # инлайн в HTTP-запросе не укладываются в таймаут и обрываются на середине.
        "description": "Синхронизация контактов аккаунтов",
        "max_retries": 1,
        "icon": "🔄",
    },
    "mass_invite": {
        "description": "Массовый инвайт участников в группы",
        "min_plan": "pro",
        "max_retries": 2,
        "icon": "📨",
    },
    "create_chatlist_folder": {
        "description": "Сборка общей папки и экспорт chatlist-ссылки",
        "min_plan": "starter",
        # Экспорт не идемпотентен (каждый прогон плодит ссылку) — без ретраев.
        "max_retries": 0,
        "icon": "📁",
    },
    "bulk_set_profile": {
        "description": "Массовая установка профилей аккаунтов",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🖼",
    },
    "mass_report": {
        "description": "Массовая жалоба на контент",
        "min_plan": "pro",
        "max_retries": 1,
        "icon": "🚩",
    },
    "content_clone": {
        "description": "Клонирование контента между каналами",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "📋",
    },
    "niche_growth_post": {
        "description": "Growth Agent — постинг промо-контента в нишевых группах",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🌱",
    },
    # ── Op-типы с исполнителями в op_worker, ранее не заведённые в реестр ──────
    # Раньше list_active/get_status показывали для них сырой op_type и generic ⚙️
    # вместо человекочитаемого лейбла, а любая постановка через operation_bus.submit
    # падала бы ValueError (см. контракт модуля). Реестр приведён в паритет с
    # диспетчером op_worker (регресс: tests/test_op_registry_dispatch_parity.py).
    "account_warmup": {
        "description": "Прогрев аккаунтов",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🔥",
    },
    "auto_register": {
        "description": "Авторегистрация аккаунтов",
        "min_plan": "pro",
        "max_retries": 1,
        "icon": "📲",
    },
    "reg_check": {
        "description": "Проверка номеров на регистрацию в Telegram",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "✅",
    },
    "phone_check": {
        "description": "Проверка номеров телефонов",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "☎️",
    },
    "parse_audience": {
        "description": "Парсинг аудитории",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "👥",
    },
    "profile_setter": {
        "description": "Настройка профиля аккаунтов",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "👤",
    },
    "create_channel": {
        "description": "Создание канала",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "📡",
    },
    "create_group": {
        "description": "Создание группы",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "👥",
    },
    "channel_add": {
        "description": "Добавление канала под управление",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "➕",
    },
    "quick_post": {
        "description": "Быстрая публикация поста",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "📝",
    },
    "run_broadcast": {
        "description": "Рассылка по аудитории",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "📢",
    },
    "ai_comment": {
        "description": "AI-комментирование",
        "min_plan": "pro",
        "max_retries": 1,
        "icon": "💬",
    },
    "self_promo_blast": {
        "description": "Самопродвижение — рассылка промо",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "📣",
    },
    "clone_adapt": {
        "description": "Клонирование с адаптацией контента",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "🧬",
    },
    "ad_intel_scan": {
        "description": "Разведка рекламы конкурентов",
        "min_plan": "pro",
        "max_retries": 1,
        "icon": "🕵️",
    },
    "compliance_scan": {
        "description": "Проверка ресурсов на соответствие правилам",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🛡️",
    },
    "gift_scan": {
        "description": "Сканирование подарков",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🎁",
    },
    "report_peer": {
        "description": "Жалоба на объект",
        "min_plan": "pro",
        "max_retries": 1,
        "icon": "🚩",
    },
    "leave_all_chats": {
        "description": "Выход из всех чатов",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🚪",
    },
    "read_all_dialogs": {
        "description": "Прочитать все диалоги",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "👁",
    },
    "delete_private_dialogs": {
        "description": "Удаление личных диалогов",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🗑",
    },
    "delete_contacts": {
        "description": "Удаление контактов",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🗑",
    },
    "deploy_network": {
        "description": "Развернуть связку",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🔗",
    },
    "crosspost_run": {
        "description": "Кросспостинг",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🔁",
    },
    "community_add_channel": {
        "description": "Канал ноды-сообщества",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🖥",
    },
    "community_liven": {
        "description": "Оживление ноды флотом",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🏛",
    },
    "community_set_staff": {
        "description": "Роли ноды-сообщества",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🛡",
    },
}


def _coerce_scheduled_for(value):
    """ISO-строка или datetime → tz-aware datetime. None остаётся None.

    ПОЧЕМУ ЭТО НУЖНО. Параметр объявлен как строка, а в запросе стоит
    `$5::timestamptz`. Каст выглядит достаточной защитой, но asyncpg выводит тип
    параметра ИЗ ЗАПРОСА: увидев timestamptz, он требует объект datetime и на
    строке падает `invalid input for query argument $5` ещё до похода в
    Postgres. То есть парсить строку было НЕКОМУ.

    Из-за этого молча не создавалась КАЖДАЯ отложенная операция: запланированные
    посты (`mini_app_api.schedule_post`), отложенные рассылки (`broadcaster`),
    массовая публикация по расписанию (`bot/handlers/mass_publish`) и
    автопродолжение инвайта. Все они передают `.isoformat()`. Обнаружено прогоном
    по настоящей базе — на заглушках пула этого не видно вообще.

    Наивный datetime считаем UTC: `scheduled_for` сравнивается с `now()` в
    timestamptz-колонке, и молчаливый сдвиг на часовой пояс сервера — отдельный
    класс ошибок (см. свод, класс 10).
    """
    if value is None or isinstance(value, datetime):
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"operation_bus: scheduled_for={value!r} — ожидается ISO-8601 или datetime"
        ) from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


async def submit(
    pool: asyncpg.Pool,
    owner_id: int,
    op_type: str,
    params: dict[str, Any],
    *,
    total_items: int = 0,
    scheduled_for: Optional[object] = None,   # ISO-строка или datetime
    template_id: Optional[int] = None,
    max_retries: Optional[int] = None,
    label: Optional[str] = None,
    bypass_plan_check: bool = False,
    dedup_window_sec: int = DEFAULT_DEDUP_WINDOW_SEC,
) -> int:
    """Поставить операцию в очередь. Возвращает op_id.

    Параметры:
      pool         — asyncpg pool
      owner_id     — telegram user_id владельца
      op_type      — тип операции (из OP_REGISTRY)
      params       — словарь параметров операции
      total_items  — общее количество элементов (для прогресс-бара)
      scheduled_for — ISO timestamp запуска (NULL = немедленно)
      template_id  — id шаблона (если применимо)
      max_retries  — переопределить количество повторов (None = из OP_REGISTRY)
      label        — человекочитаемая метка операции в очереди (None = описание
                     из OP_REGISTRY). Нужна, чтобы миграция прямых INSERT на шину
                     не теряла label, который они писали (см. Волна S/1A).
      dedup_window_sec — окно идемпотентности постановки (сек). Повторный сабмит
                     идентичной операции (owner+op_type+params+scheduled_for),
                     пока прежняя ещё pending/running и не старше окна, вернёт её
                     op_id вместо создания дубля. Защищает от двойного тапа и
                     retry после таймаута. 0 = отключить (для намеренных серий
                     идентичных операций).

    Raises:
      ValueError — если op_type не зарегистрирован в OP_REGISTRY
      PlanRequiredError — если у владельца нет подписки под min_plan операции
    """
    if op_type not in OP_REGISTRY:
        raise ValueError(
            f"operation_bus: unknown op_type={op_type!r}. Register in OP_REGISTRY first."
        )

    meta = OP_REGISTRY[op_type]
    # Централизованный тариф-гейт (defense-in-depth поверх гейтов в хендлерах):
    # объявленный min_plan становится авторитетным — free-юзер не поставит
    # платную операцию, даже если какой-то хендлер забыл проверку.
    if not bypass_plan_check:
        await _enforce_min_plan(pool, owner_id, op_type, meta)

    # Предохранитель Ban Weather: если этот тип операции прямо сейчас массово
    # убивает аккаунты владельца — не ставим новые, пока волна не спадёт.
    # Замыкает петлю «исход → причина → защита»: без этого детектор вспышек
    # оставался бы просто дашбордом. Fail-open внутри.
    await _enforce_immunity(pool, owner_id, op_type)

    retries = max_retries if max_retries is not None else meta.get("max_retries", 3)
    op_label = label or meta.get("description") or op_type

    params_json = json.dumps(params, ensure_ascii=False)
    sched = _coerce_scheduled_for(scheduled_for)

    # Идемпотентность постановки. Двойной тап кнопки или retry клиента после
    # таймаута НЕ должны плодить дубль-операцию: 82 вызова submit() идут через
    # этот choke point, поэтому дедуп здесь защищает весь продукт от двойного
    # исполнения (двойная стоимость расходников, удвоенный риск для аккаунтов).
    # pg_advisory_xact_lock сериализует одновременные идентичные сабмиты (закрывает
    # TOCTOU без изменения схемы); внутри окна ищем ещё-в-полёте идентичную
    # операцию. Отличающиеся params (в т.ч. per-account циклы) не дедупятся —
    # ложных слияний нет. Отключается dedup_window_sec=0.
    reused = False
    async with pool.acquire() as conn:
        async with conn.transaction():
            if dedup_window_sec and dedup_window_sec > 0:
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"opbus|{owner_id}|{op_type}|{params_json}|{sched}",
                )
                existing = await conn.fetchval(
                    """SELECT id FROM operation_queue
                       WHERE owner_id = $1 AND op_type = $2
                         AND status IN ('pending', 'running')
                         AND params = $3::jsonb
                         AND scheduled_for IS NOT DISTINCT FROM $4::timestamptz
                         AND created_at > NOW() - make_interval(secs => $5)
                       ORDER BY id DESC LIMIT 1""",
                    owner_id, op_type, params_json, sched, dedup_window_sec,
                )
                if existing is not None:
                    op_id = int(existing)
                    reused = True
            if not reused:
                row = await conn.fetchrow(
                    """INSERT INTO operation_queue
                           (owner_id, op_type, status, params,
                            total_items, done_items,
                            scheduled_for, template_id, max_retries, label, created_at)
                       VALUES ($1, $2, 'pending', $3::jsonb,
                               $4, 0,
                               $5::timestamptz, $6, $7, $8, NOW())
                       RETURNING id""",
                    owner_id, op_type, params_json, total_items, sched,
                    template_id, retries, op_label,
                )
                op_id = int(row["id"])

    if reused:
        log.info(
            "operation_bus: дедуп повторного сабмита op_type=%s owner=%d "
            "(окно %dс) → переиспользую op_id=%d",
            op_type, owner_id, dedup_window_sec, op_id,
        )
        return op_id

    log.info(
        "operation_bus: submitted op_id=%d op_type=%s owner=%d total_items=%d",
        op_id,
        op_type,
        owner_id,
        total_items,
    )
    # Единый choke point: КАЖДАЯ новая операция попадает в память организма
    # (событие op_queued). Пара к op_done из op_worker — полный ЖЦ операции виден
    # мозгу без правки 20 эндпоинтов. Дедуп-повторы op_queued НЕ эмитят (событие
    # только для реально созданной операции). Fail-open — шина не блокер.
    try:
        from services.organism import spine
        await spine.emit(pool, owner_id, "op_queued",
                         {"op_id": op_id, "op_type": op_type,
                          "label": op_label, "total_items": total_items})
    except Exception:
        pass
    return op_id


async def cancel(pool: asyncpg.Pool, op_id: int, owner_id: int) -> bool:
    """Отменить операцию. Возвращает True если операция найдена и отменена.

    Только pending/running операции могут быть отменены.
    Проверяет owner_id для защиты от несанкционированной отмены.
    """
    result = await pool.execute(
        """UPDATE operation_queue
           SET status = 'cancelled', finished_at = NOW()
           WHERE id = $1
             AND owner_id = $2
             AND status IN ('pending', 'running')""",
        op_id,
        owner_id,
    )
    cancelled = str(result).endswith("1")
    if cancelled:
        log.info("operation_bus: op_id=%d cancelled by owner=%d", op_id, owner_id)
    return cancelled


async def get_status(pool: asyncpg.Pool, op_id: int) -> dict | None:
    """Получить статус операции.

    Возвращает dict с полями: id, op_type, status, done_items, total_items,
    created_at, started_at, finished_at, error_msg, result или None если не найдено.
    """
    row = await pool.fetchrow(
        """SELECT id, owner_id, op_type, status,
                  done_items, total_items,
                  created_at, started_at, finished_at,
                  error_msg, result, retry_count, last_error
           FROM operation_queue
           WHERE id = $1""",
        op_id,
    )
    if not row:
        return None

    meta = OP_REGISTRY.get(row["op_type"], {})
    return {
        **dict(row),
        "description": meta.get("description", row["op_type"]),
        "icon": meta.get("icon", "⚙️"),
    }


async def list_active(
    pool: asyncpg.Pool,
    owner_id: int,
    limit: int = 20,
) -> list[dict]:
    """Список активных операций (pending + running) для владельца."""
    rows = await pool.fetch(
        """SELECT id, op_type, status, done_items, total_items,
                  created_at, started_at, scheduled_for
           FROM operation_queue
           WHERE owner_id = $1
             AND status IN ('pending', 'running')
           ORDER BY created_at DESC
           LIMIT $2""",
        owner_id,
        limit,
    )
    result = []
    for row in rows:
        meta = OP_REGISTRY.get(row["op_type"], {})
        result.append(
            {
                **dict(row),
                "description": meta.get("description", row["op_type"]),
                "icon": meta.get("icon", "⚙️"),
            }
        )
    return result


async def list_recent(
    pool: asyncpg.Pool,
    owner_id: int,
    limit: int = 10,
) -> list[dict]:
    """Список завершённых/отменённых операций для истории."""
    rows = await pool.fetch(
        """SELECT id, op_type, status, done_items, total_items,
                  created_at, started_at, finished_at,
                  error_msg, retry_count
           FROM operation_queue
           WHERE owner_id = $1
             AND status IN ('done', 'failed', 'cancelled', 'skipped')
           ORDER BY finished_at DESC NULLS LAST, created_at DESC
           LIMIT $2""",
        owner_id,
        limit,
    )
    result = []
    for row in rows:
        meta = OP_REGISTRY.get(row["op_type"], {})
        result.append(
            {
                **dict(row),
                "description": meta.get("description", row["op_type"]),
                "icon": meta.get("icon", "⚙️"),
            }
        )
    return result


def describe(op_type: str) -> str:
    """Вернуть человекочитаемое описание типа операции."""
    meta = OP_REGISTRY.get(op_type, {})
    icon = meta.get("icon", "⚙️")
    desc = meta.get("description", op_type)
    return f"{icon} {desc}"


# ── Точечный повтор упавших целей ────────────────────────────────────────────
#
# Частичный провал массовой операции без этого механизма означает перезапуск
# ВСЕЙ операции: лишний расход лимитов аккаунтов и повторная обработка уже
# успешных целей (дубли постов, повторные вступления). Данные для точечного
# повтора уже есть — op_worker пишет per-target результат в operation_log.
#
# Ограничение честное и намеренное: механизм работает только для op_type,
# объявивших `retry_targets`. У остальных operation_log.target — это либо
# исполнитель («acc#123»), либо человекочитаемая метка, из которой цель не
# восстановить. Угадывать нельзя: повтор ушёл бы не по тем целям и отчитался
# бы об успехе.


def timeout_for(op_type: str, default: int) -> int:
    """Потолок одного прогона операции в секундах.

    Зачем вообще потолок: исполнитель, зависший на ответе Telegram, держал слот
    параллельности и арендованные аккаунты БЕСКОНЕЧНО. Сторож зависших его не
    трогает (операция числится активной в этом процессе), алерт о застрявших —
    тоже, так что снаружи это невидимо и лечится только рестартом.

    Мусор в реестре не должен снимать защиту: нечитаемое или неположительное
    значение трактуется как «потолка нет в реестре» и берётся общий.
    """
    meta = OP_REGISTRY.get(op_type) or {}
    try:
        value = int(meta.get("timeout_sec") or 0)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def retry_targets_meta(op_type: str) -> dict | None:
    """Описание точечного повтора для op_type. None — повтор не поддержан."""
    meta = (OP_REGISTRY.get(op_type) or {}).get("retry_targets")
    return meta if isinstance(meta, dict) and meta.get("param") else None


def supports_retry_failed(op_type: str) -> bool:
    return retry_targets_meta(op_type) is not None


def parse_log_target(raw: str, meta: dict):
    """`operation_log.target` → элемент списка целей. None — не разобрано.

    Строгий разбор: значение, не соответствующее объявленному формату, ОТБРАСЫВАЕТСЯ.
    Мусор в списке целей хуже, чем более короткий список: исполнитель либо
    молча ничего не сделает, либо ударит не по тому объекту.
    """
    val = (raw or "").strip()
    if not val:
        return None
    prefix = meta.get("prefix") or ""
    if prefix:
        if not val.startswith(prefix):
            return None
        val = val[len(prefix):]
    if meta.get("kind") == "int":
        neg = val.startswith("-")
        digits = val[1:] if neg else val
        if not digits.isdigit():
            return None
        return int(val)
    return val


async def collect_failed_targets(pool: asyncpg.Pool, op_id: int, op_type: str) -> list:
    """Упавшие цели операции, в формате элементов params. Порядок сохраняется.

    Берутся ТОЛЬКО status='error'. Цель, у которой есть хотя бы одна успешная
    запись, исключается: один и тот же канал мог упасть на одном аккаунте и
    пройти на другом — повторять его значит сделать вторую публикацию.
    """
    meta = retry_targets_meta(op_type)
    if not meta:
        return []
    try:
        rows = await pool.fetch(
            "SELECT target, status FROM operation_log "
            " WHERE op_id=$1 AND target IS NOT NULL ORDER BY step_num, id",
            op_id,
        )
    except Exception:
        log.warning("operation_bus: чтение operation_log op=%s не удалось", op_id, exc_info=True)
        return []

    succeeded: set = set()
    failed_ordered: list = []
    seen: set = set()
    for r in rows:
        parsed = parse_log_target(r["target"], meta)
        if parsed is None:
            continue
        key = str(parsed)
        if r["status"] == "ok":
            succeeded.add(key)
        elif r["status"] == "error" and key not in seen:
            seen.add(key)
            failed_ordered.append((key, parsed))
    return [val for key, val in failed_ordered if key not in succeeded]


async def submit_retry_failed(
    pool: asyncpg.Pool, owner_id: int, src_op_id: int
) -> dict:
    """Поставить операцию-повтор только по упавшим целям исходной.

    Возвращает {"ok": bool, "op_id": int|None, "count": int, "reason": str}.
    Причина отказа возвращается текстом, а не скрывается: «повторять нечего» и
    «повтор не поддержан для этого типа» — разные ответы для пользователя.
    """
    row = await pool.fetchrow(
        "SELECT owner_id, op_type, params FROM operation_queue WHERE id=$1", src_op_id
    )
    if not row or row["owner_id"] != owner_id:
        return {"ok": False, "op_id": None, "count": 0, "reason": "Операция не найдена"}

    op_type = row["op_type"]
    meta = retry_targets_meta(op_type)
    if not meta:
        return {
            "ok": False, "op_id": None, "count": 0,
            "reason": "Для этого типа операции точечный повтор не поддержан",
        }

    try:
        params = row["params"] if isinstance(row["params"], dict) else json.loads(row["params"] or "{}")
    except (TypeError, ValueError):
        params = {}

    failed = await collect_failed_targets(pool, src_op_id, op_type)
    if not failed:
        return {"ok": False, "op_id": None, "count": 0, "reason": "Неудавшихся целей не осталось"}

    # Поле целей заменяется, остальные параметры (текст, задержки, аккаунты)
    # переносятся как есть — повтор обязан повторять ту же операцию.
    new_params = dict(params)
    new_params[meta["param"]] = failed
    for alt in meta.get("alt_params") or ():
        # Исполнитель может читать альтернативное имя поля первым: если оставить
        # старое значение, повтор пойдёт по ПОЛНОМУ списку целей.
        new_params.pop(alt, None)
    new_params["retry_of_op"] = src_op_id

    try:
        op_id = await submit(pool, owner_id, op_type, new_params, total_items=len(failed))
    except PlanRequiredError:
        raise
    except Exception as exc:
        log.error("operation_bus: повтор op=%s не поставлен: %s", src_op_id, exc)
        return {"ok": False, "op_id": None, "count": len(failed), "reason": "Не удалось поставить операцию"}

    log.info(
        "operation_bus: retry_failed src=%s → op=%s targets=%d type=%s",
        src_op_id, op_id, len(failed), op_type,
    )
    return {"ok": True, "op_id": op_id, "count": len(failed), "reason": ""}
