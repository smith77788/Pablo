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
        # Текст исключения — это РОВНО то, что увидит человек: сорок с лишним
        # хендлеров мини-аппа отдают его как `_err(str(exc), 403)`. Пока он был
        # английским («operation 'mass_invite' requires plan 'paid'»), владелец,
        # который по-английски не читает, получал непонятную техническую строку.
        # Хуже того, единый маркер пейволла в `_err` ставится по слову
        # «подписка» — без него отказ показывался сухим тостом вместо экрана
        # оформления, то есть в лучшей точке конверсии путь к оплате обрывался.
        # Тип операции и требуемый тариф остаются полями исключения — для логов.
        what = (OP_REGISTRY.get(op_type) or {}).get("description") or op_type
        super().__init__(f"Операция «{what}» доступна по подписке")


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
#     "per_account": True — цель отрабатывает КАЖДЫЙ аккаунт операции, то есть
#         одна и та же цель попадает в лог несколько раз, по разу на аккаунт.
#         Тогда успех одного аккаунта НЕ закрывает цель: у остальных работа
#         осталась. Без этого признака повтор молча терял такие цели.
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
        # target = str(dialog["id"]) — id канала, одинаково в успехе и в
        # ошибке. В канал публикует ОДИН аккаунт (первый здоровый из тех, кто
        # им управляет), поэтому успех закрывает цель окончательно —
        # per_account здесь был бы вреден: повтор дал бы второй пост.
        # Пустой channel_ids означает «во все управляемые каналы»; повтор
        # проставляет список упавших и тем самым сужает операцию.
        "retry_targets": {"param": "channel_ids", "kind": "int"},
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
        # per_account: вступают ВСЕ аккаунты операции, и в лог по одной ссылке
        # идёт строка на каждый. Успех одного аккаунта не означает, что вступили
        # остальные.
        "retry_targets": {"param": "links", "kind": "str", "alt_params": ["targets"],
                          "per_account": True},
    },
    "bulk_leave": {
        "description": "Массовый выход из каналов",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "📤",
        # target = str(channel) — элемент списка channels как есть.
        # per_account: выходят ВСЕ аккаунты операции (цикл accounts × channels),
        # поэтому на один канал приходится строка на каждый аккаунт.
        "retry_targets": {"param": "channels", "kind": "str", "per_account": True},
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
        # target = str(ch_id) — элемент channel_ids как есть. Постит один
        # аккаунт (acc_id в params), то есть цель отрабатывается один раз:
        # per_account здесь не нужен.
        "retry_targets": {"param": "channel_ids", "kind": "int"},
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
    "check_owned_restrictions": {
        "description": "Проверка каналов/чатов/ботов на ограничения и теневой бан",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🛡",
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
        # target = сама ссылка на канал-приёмник, как она пришла в params.
        # На цель приходится одно действие (аккаунт выбирается внутри), поэтому
        # успех закрывает её окончательно.
        "retry_targets": {"param": "target_refs", "kind": "str"},
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
        # Тот же исполнитель, что у mass_publish: target = str(channel_id).
        "retry_targets": {"param": "channel_ids", "kind": "int"},
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


# Из каких состояний операцию можно отменить. Поверхности просят разные наборы:
# мини-апп — ещё и 'paused' (иначе приостановленную операцию нельзя было ни
# отменить, ни снять поштучно), экран апрува — 'waiting_approval'.
CANCELLABLE = ("pending", "running")


async def cancel(
    pool: asyncpg.Pool, op_id: int, owner_id: int,
    allow: "tuple[str, ...]" = CANCELLABLE,
) -> bool:
    """Отменить операцию. Возвращает True если операция найдена и отменена.

    ЕДИНСТВЕННАЯ ДВЕРЬ ОТМЕНЫ — как submit() для постановки. Раньше её не было:
    отмена жила пятью сырыми UPDATE по разным поверхностям (мини-апп, два экрана
    бота, экран апрува, массовые операции), и каждая расходилась с остальными:

      * три из пяти не ставили `finished_at` вовсе — у отменённой операции не
        было времени завершения, то есть «сколько шла» и «когда закончилась» не
        мог ответить никто;
      * одна не фильтровала статус совсем и могла отменить уже завершённую;
      * ни одна не дописывала `result` и не объявляла исход, поэтому отмена из
        очереди не попадала ни на график исходов, ни в подписанный аудит-трейл,
        ни в память организма — хотя `op_queued` из submit() обещает полный
        жизненный цикл.

    `allow` — из каких состояний отмена разрешена этой поверхности; сужать и
    расширять набор можно, но проверка статуса остаётся внутри запроса, а не на
    стороне вызывающего (между SELECT и UPDATE операция успевает стартовать).

    Проверяет owner_id для защиты от несанкционированной отмены.
    """
    # Старый статус нужен, чтобы знать, успел ли воркер взять операцию. CTE с
    # FOR UPDATE делает это в том же запросе: отдельным SELECT'ом до UPDATE
    # ответ мог бы устареть ровно в ту миллисекунду, когда поллер её подхватил.
    row = await pool.fetchrow(
        """WITH before AS (
               SELECT id, status FROM operation_queue
                WHERE id = $1 AND owner_id = $2
                  FOR UPDATE
           )
           UPDATE operation_queue q
              SET status = 'cancelled', finished_at = NOW()
             FROM before b
            WHERE q.id = b.id
              AND b.status = ANY($3::text[])
        RETURNING b.status AS was_status, q.op_type, q.params""",
        op_id,
        owner_id,
        [str(x) for x in allow],
    )
    cancelled = row is not None
    if not cancelled:
        return False
    log.info("operation_bus: op_id=%d cancelled by owner=%d", op_id, owner_id)
    # ОЖИДАЮЩУЮ операцию закрываем до конца здесь же. Запущенную закроет
    # op_worker._finish_cancelled_op: он увидит 'cancelled' и допишет result,
    # метрику, событие и подпись. А ожидающую воркер не подхватит НИКОГДА —
    # значит, без этой ветки она навсегда остаётся с result=NULL, без исхода на
    # графике, без подписи в аудит-трейле и без события `op_done`. Пара к
    # `op_queued` из submit() обещает полный жизненный цикл в памяти организма,
    # и ровно на отменённой из очереди операции цикл не замыкался.
    if str(row["was_status"]) == "running":
        return True
    await _close_cancelled_pending(pool, op_id, owner_id,
                                   str(row["op_type"] or "unknown"), row["params"])
    return True


async def _close_cancelled_pending(
    pool: asyncpg.Pool, op_id: int, owner_id: int, op_type: str, params,
) -> None:
    """Дописать итог отменённой ОЖИДАЮЩЕЙ операции. Никогда не бросает.

    Счётчики берём из журнала целей: «ожидающая» не значит «ничего не делала».
    Операция возвращается в очередь после флуд-паузы, рестарта воркера и
    занятого флота, поэтому на момент отмены у неё могут быть взятые цели — и
    для владельца именно их число отвечает на вопрос «продолжать ли с нуля».
    """
    try:
        from services import op_status
        from services import op_worker as _ow

        if isinstance(params, str):
            try:
                params = json.loads(params)
            except (ValueError, TypeError):
                params = None
        if not isinstance(params, dict):
            params = {}
        ok_n, failed_n = await _ow._journal_counters(pool, op_id)
        summary = (f"🚫 Отменена из очереди, успело {ok_n}"
                   if ok_n else "🚫 Отменена до запуска")
        result = {"status": op_status.CANCELLED, "ok": ok_n, "failed": failed_n,
                  "total": ok_n + failed_n, "summary": summary,
                  "duration_s": 0.0, "op_type": op_type}
        await pool.execute(
            "UPDATE operation_queue SET result=$2::jsonb, acct_wait_since=NULL "
            "WHERE id=$1 AND status='cancelled'",
            op_id, json.dumps(result, ensure_ascii=False),
        )
        await _ow._announce_op_outcome(
            pool, op_id, owner_id, op_type, params, op_status.CANCELLED,
            ok_n, failed_n, summary)
    except Exception as e:
        log.warning("operation_bus: итог отмены op=%d не записан: %s", op_id, e)


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
    """Список завершённых/отменённых операций для истории.

    `partial` обязан быть в списке. Это терминальный статус недоведённой работы
    (services/op_status.py), и без него операция, взявшая часть целей, не
    попадала НИКУДА: в активных её нет (не pending/running), в истории тоже —
    владелец видел, как операция просто исчезает.
    """
    rows = await pool.fetch(
        """SELECT id, op_type, status, done_items, total_items,
                  created_at, started_at, finished_at,
                  error_msg, retry_count
           FROM operation_queue
           WHERE owner_id = $1
             AND status IN ('done', 'partial', 'failed', 'cancelled', 'skipped')
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


# Насколько глубоко идём по цепочке повторов в каждую сторону, и сколько
# операций одной семьи читаем максимум. Пределы нужны на случай битых данных,
# чтобы обход не стал бесконечным.
RETRY_CHAIN_MAX = 10
RETRY_FAMILY_MAX = 50


async def retry_family_ids(pool: asyncpg.Pool, op_id: int) -> list[int]:
    """Операции, чей журнал относится к этой работе: сама, её предки и их потомки.

    ЗАЧЕМ ПРЕДКИ. Ключи идемпотентности исполнителя читают operation_log по
    op_id, а кнопка «Повторить» ставит НОВУЮ операцию с новым id: журнал у неё
    пустой, и без ссылки на предка исполнитель прошёл бы весь список целей
    заново. Ссылка — `retry_of_op` в params.

    ЗАЧЕМ ПОТОМКИ. Исходная операция остаётся недоведённой НАВСЕГДА: её статус
    не меняется от того, что повтор доделал работу, и кнопка повтора на её
    экране никуда не девается. Второе нажатие ставило повтор, который видел
    только журнал предка и ничего не знал о работе ПЕРВОГО повтора — каналы,
    опубликованные первым, получали второй пост. По дереву семьи оба повтора
    видят работу друг друга.

    Порядок: сама операция, затем предки снизу вверх, затем потомки. Никогда не
    бросает: не прочитали — работаем по тому, что успели собрать, то есть как
    раньше.
    """
    family = [int(op_id)]
    seen = {int(op_id)}
    cur = int(op_id)

    for _ in range(RETRY_CHAIN_MAX):
        try:
            row = await pool.fetchrow(
                "SELECT params->>'retry_of_op' AS src FROM operation_queue WHERE id=$1",
                cur,
            )
        except Exception:
            log.warning("operation_bus: цепочка повторов op=%s не прочитана", cur, exc_info=True)
            return family
        if not row:
            break
        try:
            parent = int(row["src"])
        except (KeyError, TypeError, ValueError, IndexError):
            break
        if parent in seen:
            break
        seen.add(parent)
        family.append(parent)
        cur = parent

    frontier = list(family)
    for _ in range(RETRY_CHAIN_MAX):
        if not frontier or len(family) >= RETRY_FAMILY_MAX:
            break
        try:
            rows = await pool.fetch(
                "SELECT id FROM operation_queue "
                " WHERE params->>'retry_of_op' = ANY($1::text[]) ORDER BY id",
                [str(i) for i in frontier],
            )
        except Exception:
            log.warning("operation_bus: повторы op=%s не прочитаны", op_id, exc_info=True)
            break
        nxt: list[int] = []
        for r in (rows or []):
            try:
                child = int(r["id"])
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            if child in seen:
                continue
            seen.add(child)
            family.append(child)
            nxt.append(child)
            if len(family) >= RETRY_FAMILY_MAX:
                break
        frontier = nxt

    return family


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

    Берутся ТОЛЬКО status='error'.

    Что считать «уже сделанной» целью, зависит от того, КТО её отрабатывает.

    Операция вида «одна цель — одно действие» (публикация, SEO по сетке): цель,
    у которой есть хотя бы одна успешная запись, исключается. Один и тот же
    канал мог упасть на одном аккаунте и пройти на другом — повторять его
    значит сделать вторую публикацию.

    Операция вида «каждый аккаунт отрабатывает каждую цель» (bulk_join,
    bulk_leave — цикл accounts × targets): то же правило ТЕРЯЕТ РАБОТУ. Если из
    пяти аккаунтов в канал вступил один, а четверо упали, канал считался
    закрытым и в повтор не попадал — четыре аккаунта так и оставались снаружи,
    причём операция отчитывалась, что повторять нечего. Такие типы помечены
    per_account, и успех одного аккаунта у них цель не закрывает.
    """
    meta = retry_targets_meta(op_type)
    if not meta:
        return []
    # Журнал берём по ВСЕЙ семье повторов, а не по одному op_id: работу могла
    # сделать как исходная операция, так и любой из её повторов (см.
    # retry_family_ids). Иначе второе нажатие «Повторить» на исходной операции
    # отдавало бы цели, которые первый повтор уже закрыл.
    try:
        rows = await pool.fetch(
            "SELECT target, status FROM operation_log "
            " WHERE op_id = ANY($1::bigint[]) AND target IS NOT NULL "
            " ORDER BY step_num, id",
            await retry_family_ids(pool, op_id),
        )
    except Exception:
        log.warning("operation_bus: чтение operation_log op=%s не удалось", op_id, exc_info=True)
        return []

    per_account = bool(meta.get("per_account"))

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
    if per_account:
        # Успех одного аккаунта не закрывает цель для остальных: повторяем всё,
        # что где-то упало. Дубль здесь безобиден — аккаунт, который уже вступил
        # или вышел, получает no-op, а тот, что упал, доделывает работу.
        return [val for _key, val in failed_ordered]
    return [val for key, val in failed_ordered if key not in succeeded]


# Ключи params, которые делают операцию ПОВТОРЯЮЩЕЙСЯ: интервал круга, остаток
# кругов и счётчик неудач подряд. Повтор обязан их выбрасывать — см.
# params_for_retry.
RECURRENCE_KEYS: tuple[str, ...] = (
    "repeat_interval_min",
    "repeat_count",
    "_recurring_fail_streak",
)


def params_for_retry(src_op_id: int, params: dict | None) -> dict:
    """params для операции-повтора: ссылка на журнал предка, без клона расписания.

    Ссылка `retry_of_op` нужна, чтобы исполнитель увидел журнал уже сделанной
    работы — он лежит под СТАРЫМ id (op_worker.journal_op_ids). Без неё повтор
    начинает со свежим пустым журналом и честно проходит весь список целей
    заново: рассылка, вставшая на 203 адресатах из 380, присылает этим 203
    второе одинаковое сообщение.

    Расписание повтор НЕ наследует, и это вторая половина дела. Повтор копировал
    params целиком, вместе с `repeat_interval_min`, поэтому каждое нажатие
    «Повторить» на круге автопостинга заводило ВТОРУЮ цепочку с тем же
    интервалом: первая продолжала идти сама (неудачный круг её больше не
    обрывает — op_worker._reschedule_recurring), а повтор запускал параллельную.
    Каналы получали посты вдвое чаще, следующее нажатие — вчетверо, и остановить
    это можно было только вручную, отменяя операции по одной. Массовый повтор
    «перезапустить все недоведённые» берёт до 25 операций за раз, то есть один
    тап мог размножить расписание в 25 цепочек: для флота это прямой путь в бан,
    ровно та частота публикаций, от которой оберегает весь пейсинг продукта.

    Смысл кнопки — «сделай эту работу ещё раз», а не «заведи новое расписание».
    Живое расписание в повторе не нуждается: неудачный круг его не обрывает.
    Отменённое расписание не воскрешаем намеренно — отмена означает «стоп», и
    возвращать его должен владелец там, где он его заводил.

    Счётчик неудач подряд тоже не наследуется: он описывает историю ТОЙ цепочки,
    а не этой работы, и с ним повтор мог упереться в потолок серии с первого
    круга.

    Исходный словарь не меняется: вызывающий обычно держит его, чтобы ответить
    владельцу.
    """
    out = dict(params or {})
    out["retry_of_op"] = int(src_op_id)
    for key in RECURRENCE_KEYS:
        out.pop(key, None)
    return out


def drops_recurrence(params: dict | None) -> bool:
    """Было ли у операции собственное расписание — то есть повтор его отбросит.

    Нужно, чтобы сказать владельцу правду: он нажал «Повторить» на круге
    автопостинга и получит ОДИН запуск, а не новое расписание. Нечитаемое
    значение интервала расписанием не считается.
    """
    try:
        return int((params or {}).get("repeat_interval_min") or 0) > 0
    except (TypeError, ValueError):
        return False


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
    new_params = params_for_retry(src_op_id, params)
    new_params[meta["param"]] = failed
    for alt in meta.get("alt_params") or ():
        # Исполнитель может читать альтернативное имя поля первым: если оставить
        # старое значение, повтор пойдёт по ПОЛНОМУ списку целей.
        new_params.pop(alt, None)

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


async def closed_targets_count(pool: asyncpg.Pool, op_id: int) -> int:
    """Сколько РАЗНЫХ целей уже закрыто успехом по всей семье повторов.

    Нужно, чтобы повтор целиком получил ЧЕСТНЫЙ размер работы. Без этого он
    наследовал `total_items` предка: операция на 380 целей, где 203 уже сделаны,
    получала повтор с потолком 380, закрывала оставшиеся 177 — и по недобору
    прогресса (177 из 380) объявлялась «частично выполненной», хотя сделала всё.
    Дальше её снова предлагали повторить: круг из ложных «недоведено».

    Цели, закрытые ОШИБКОЙ, незакрытыми и считаются: их повтор как раз и должен
    попробовать снова.

    0 — журнала нет (этот тип операции его не пишет) или чтение не удалось;
    вызывающий тогда остаётся при размере предка, как было раньше.
    """
    try:
        ids = await retry_family_ids(pool, op_id)
        row = await pool.fetchrow(
            "SELECT count(DISTINCT target) AS n FROM operation_log "
            " WHERE op_id = ANY($1::bigint[]) AND target IS NOT NULL "
            "   AND status = 'ok'",
            [int(i) for i in ids] or [int(op_id)])
        return int(row["n"] or 0) if row else 0
    except Exception as exc:
        log.debug("closed_targets_count op=%s: %s", op_id, exc)
        return 0


async def _retry_total_items(pool: asyncpg.Pool, op_id: int, src_total) -> int:
    """Размер повтора: сколько целей предка ещё не закрыто успехом."""
    try:
        total = int(src_total or 0)
    except (TypeError, ValueError):
        total = 0
    if total <= 0:
        return total
    done = await closed_targets_count(pool, op_id)
    return max(0, total - done) if done else total


async def resubmit_unfinished(
    pool: asyncpg.Pool, owner_id: int, hours: int = 24, limit: int = 100
) -> dict:
    """Перепоставить недоведённые операции владельца за последние `hours` часов.

    Единственная дверь для «повторить упавшие» на ВСЕХ поверхностях. Бот делал
    это сырым UPDATE: `status='pending', retry_count=0, done_items=0` прямо по
    `operation_queue`. Такой сброс — это постановка операции в работу (поллер
    тут же её забирает), но мимо гейта тарифа, предохранителя Ban Weather и
    дедупа двойного тапа; храповик сырых INSERT'ов его не ловил, потому что
    INSERT'а там нет. Вторая кнопка делала это сразу по ВСЕМ упавшим операциям
    владельца без потолка — включая `mass_invite`, самую баноопасную операцию
    продукта. Заодно `done_items=0` стирал память о сделанном: журнал целей
    оставался, и операция навсегда показывала «обработано меньше, чем сделано».

    Повтор безопасен: дедуп инвайта живёт по (owner_id, group_key), а не по
    номеру операции, поэтому уже приглашённые второй раз не получат инвайт.
    `mass_publish` пропускаем — его безопасный повтор только по упавшим каналам
    делается точечно, иначе успешные каналы получили бы дубль поста.

    Возвращает {"ok": True, "retried": int, "skipped": int}.
    """
    from services import op_status as _ost

    rows = await pool.fetch(
        f"""SELECT id, op_type, params, label, total_items FROM operation_queue
           WHERE owner_id=$1 AND status IN {_ost.sql_unfinished_list()}
             AND created_at > NOW() - ($2 * INTERVAL '1 hour')
           ORDER BY created_at DESC LIMIT {int(limit)}""",
        owner_id, hours)
    retried, skipped, dropped = 0, 0, 0
    for r in (rows or []):
        if r["op_type"] == "mass_publish":
            skipped += 1
            continue
        params = r["params"]
        if isinstance(params, str):
            try:
                params = json.loads(params or "{}")
            except (TypeError, ValueError):
                params = {}
        if not isinstance(params, dict):
            params = {}
        # params ТОЛЬКО через params_for_retry: он добавляет ссылку на журнал
        # предка (иначе повтор пройдёт весь список заново и пришлёт уже
        # обработанным целям второе такое же сообщение) и выбрасывает расписание
        # (иначе один тап по «повторить» заводит ВТОРУЮ цепочку автопостинга с
        # тем же интервалом — каналы получают посты вдвое чаще, а для флота это
        # прямой путь в бан).
        if drops_recurrence(params):
            dropped += 1
        try:
            _left = await _retry_total_items(pool, int(r["id"]), r["total_items"])
            await submit(pool, owner_id, r["op_type"],
                         params_for_retry(int(r["id"]), params),
                         total_items=_left, label=r["label"])
            retried += 1
        except PlanRequiredError:
            skipped += 1
        except Exception as exc:
            log.debug("resubmit_unfinished op_type=%s: %s", r["op_type"], exc)
    return {"ok": True, "retried": retried, "skipped": skipped,
            "recurrence_dropped": dropped}


async def resubmit_one(pool: asyncpg.Pool, owner_id: int, op_id: int) -> dict:
    """Повторить одну недоведённую операцию — через шину, а не сбросом строки.

    Сначала пробуем точечный повтор только по упавшим целям: он дешевле и
    безопаснее. Тип без точечного повтора перепоставляем целиком — исходная
    строка при этом остаётся с ЧЕСТНОЙ историей, вместо того чтобы терять свои
    счётчики и причину под сбросом в 'pending'.

    Возвращает {"ok": bool, "op_id": int|None, "count": int, "reason": str}.
    """
    from services import op_status as _ost

    row = await pool.fetchrow(
        "SELECT owner_id, op_type, params, label, total_items, status "
        "FROM operation_queue WHERE id=$1", op_id)
    if not row or int(row["owner_id"]) != int(owner_id):
        return {"ok": False, "op_id": None, "count": 0,
                "reason": "Операция не найдена"}
    if row["status"] not in _ost.UNFINISHED:
        return {"ok": False, "op_id": None, "count": 0,
                "reason": "Повторять нечего: операция не завершилась неудачей"}

    # Точечный повтор — только если упавшие цели ЕСТЬ. Их отсутствие не значит
    # «повторять нечего»: недоведённая операция могла остановиться до того, как
    # дошла до остальных целей (кончились аккаунты, флуд, лимит) — тогда у неё
    # ноль упавших и сотня нетронутых, и продолжить её нужно целиком. Повтор
    # целиком безопасен: уже взятые цели исполнитель пропустит.
    if retry_targets_meta(row["op_type"]):
        res = await submit_retry_failed(pool, owner_id, op_id)
        if res.get("ok"):
            return res

    if row["op_type"] == "mass_publish":
        return {"ok": False, "op_id": None, "count": 0,
                "reason": "Для публикации повторяются только упавшие каналы — "
                          "из карточки операции"}

    params = row["params"]
    if isinstance(params, str):
        try:
            params = json.loads(params or "{}")
        except (TypeError, ValueError):
            params = {}
    if not isinstance(params, dict):
        params = {}
    # Размер повтора — остаток, а не потолок предка: иначе повтор, доделавший
    # всё, объявлялся бы «частично выполненным» по недобору прогресса.
    total = await _retry_total_items(pool, op_id, row["total_items"])
    if int(row["total_items"] or 0) > 0 and total == 0:
        return {"ok": False, "op_id": None, "count": 0,
                "reason": "Повторять нечего: все цели уже закрыты"}
    # Та же причина, что в resubmit_unfinished: ссылка на журнал предка плюс
    # отброшенное расписание. Точечный повтор выше идёт тем же путём — внутри
    # submit_retry_failed.
    _dropped = drops_recurrence(params)
    new_id = await submit(pool, owner_id, row["op_type"],
                          params_for_retry(op_id, params),
                          total_items=total, label=row["label"])
    return {"ok": True, "op_id": new_id, "count": total, "reason": "",
            "dropped_recurrence": _dropped}


# ── Признак жизни исполнителя операций ───────────────────────────────────────
#
# ЧТО ЛОМАЛОСЬ. Остановку исполнителя операций не замечал НИКТО. Поллер живёт в
# `op_worker.run`, и присмотр за ним (`service_supervisor.supervise`) при падении
# пишет строку в лог и через 30 секунд пробует снова — вечно. Если падение
# повторяемое (сорванная миграция, недоступная колонка, исчерпанный пул), продукт
# оказывается в таком состоянии: процесс жив, бот отвечает, мини-апп открывается,
# хартбит процесса (`process_heartbeats`) исправно обновляется отдельным
# сервисом — а очередь не разбирается вообще. Для владельца это выглядит так:
# каждая запущенная операция навсегда остаётся «ожидает», без причины и без
# срока. Ровно то, про что код флуд-паузы прямо пишет, что молчание обходится
# дороже сообщения: владелец начинает отменять, перезапускать и ставить новые
# операции — то есть наращивать очередь, которую некому разобрать.
#
# Своего сторожа у воркера для этого случая нет по определению: `_watchdog_alerts`
# живёт внутри того же цикла и умирает вместе с ним. Проверено на 29.09.2026:
# ни один сервис вне `op_worker` не смотрел, разбирается ли очередь
# (`account_monitor`, `infra_advisor`, `anomaly_detector`, `fleet_doctor` считают
# только количества и возраст строк, а не факт исполнения).
#
# ПОЧЕМУ ХАРТБИТ, А НЕ ВЫВОД ПО ОЧЕРЕДИ. Соблазн определить остановку по самим
# данным («есть готовые pending, но ни одна не в running») не работает: очередь
# честно стоит и при полном потолке параллельности, и при достигнутом лимите на
# владельца, и при открытом предохранителе, а осиротевшие строки в 'running'
# после падения процесса наоборот маскируют остановку навсегда. Отличить это от
# мёртвого исполнителя по одной таблице нельзя, поэтому поллер сам отмечается:
# отметка есть и свежая — исполнитель работает, какой бы длинной ни была очередь.
#
# Отметка живёт в platform_settings (key-value), а не в новой таблице: рост схемы
# и без того помечен техдолгом, а признак ровно один и глобальный. Возраст
# считается по `updated_at` на стороне БД — часы процесса в расчёт не входят.

POLLER_MARK_KEY = "op_worker_poll_at"

# Сколько молчания поллера считать остановкой. Цикл воркера — 10 секунд, отметка
# обновляется раз в ~30 секунд, значит пять минут это десять пропущенных отметок:
# столько подряд не теряется ни на паузе GC, ни на медленном запросе.
POLLER_SILENCE_S = 5 * 60


async def mark_poller_alive(pool, worker_id: str = "") -> None:
    """Отметить, что цикл исполнителя операций только что прошёл круг.

    Никогда не бросает: признак жизни не имеет права уронить сам цикл, который
    он описывает. Значение — id процесса, чтобы по отметке было видно, КТО её
    поставил (при разносе ролей web/worker поллер только один).
    """
    try:
        await pool.execute(
            """INSERT INTO platform_settings (key, value, updated_at)
                    VALUES ($1, $2, now())
               ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = now()""",
            POLLER_MARK_KEY,
            str(worker_id or "")[:200],
        )
    except Exception as exc:
        log.debug("operation_bus: отметка жизни поллера не записана: %s", exc)


async def queue_frozen(pool, silence_s: int = 0) -> Optional[dict]:
    """«Очередь не разбирается»: работа готова к запуску, а исполнитель молчит.

    Возвращает None, когда всё в порядке, иначе словарь:

        {"silence_s": float|None,   # сколько молчит поллер (None — отметки нет)
         "pending":   int,          # сколько операций ждут дольше порога
         "oldest_min": int,         # сколько минут ждёт самая старая
         "owners":    {owner_id: count}}

    Условие срабатывания — И, а не ИЛИ: отметка поллера старше порога (или её
    нет вовсе) И при этом есть хотя бы одна операция, которая дольше того же
    порога ГОТОВА к запуску. Второе условие обязательно: мёртвый исполнитель при
    пустой очереди никому пока не мешает, а сообщение о нём было бы шумом на
    каждом деплое. Вместе они дают утверждение, которое нельзя оспорить: работа
    ждёт десять минут, и за это время никто ни разу не заглянул в очередь.

    «Готова к запуску» — ровно те условия, по которым операцию берёт поллер
    (см. _process_pending): pending, срок наступил, подтверждение не требуется.
    Отложенная операция (флуд-пауза, «продолжим завтра», повтор после cooldown)
    ждёт своего `scheduled_for` и остановкой не считается.

    Отсутствие отметки трактуется как молчание, а не как «неизвестно»: иначе
    исполнитель, который не поднялся НИ РАЗУ (неверная роль процесса, падение на
    первой же строке `run`), остался бы невидимым навсегда. Ложной тревоги это не
    даёт — здоровый поллер ставит отметку в первые полминуты работы, то есть
    задолго до того, как какая-нибудь операция прождёт пять минут.

    Fail-open: любая ошибка запроса → None. Сломанный детектор не имеет права
    объявить аварию, которой нет.
    """
    limit_s = int(silence_s or POLLER_SILENCE_S)
    try:
        rows = await pool.fetch(
            """
            WITH mark AS (
                SELECT EXTRACT(EPOCH FROM (now() - updated_at)) AS silence_s
                  FROM platform_settings WHERE key = $1
            ),
            due AS (
                SELECT owner_id,
                       COUNT(*) AS cnt,
                       MAX(EXTRACT(EPOCH FROM (now() - GREATEST(created_at,
                           COALESCE(scheduled_for, created_at))))) AS wait_s
                  FROM operation_queue
                 WHERE status = 'pending'
                   AND requires_approval IS NOT TRUE
                   AND GREATEST(created_at, COALESCE(scheduled_for, created_at))
                       < now() - make_interval(secs => $2)
                 GROUP BY owner_id
            )
            SELECT due.owner_id, due.cnt, due.wait_s,
                   (SELECT silence_s FROM mark) AS silence_s
              FROM due
            """,
            POLLER_MARK_KEY,
            float(limit_s),
        )
    except Exception as exc:
        log.debug("operation_bus: проверка разбора очереди не удалась: %s", exc)
        return None

    if not rows:
        return None
    silence = rows[0]["silence_s"]
    if silence is not None and float(silence) < limit_s:
        return None

    owners = {int(r["owner_id"]): int(r["cnt"] or 0) for r in rows}
    return {
        "silence_s": None if silence is None else float(silence),
        "pending": sum(owners.values()),
        "oldest_min": int(max(float(r["wait_s"] or 0) for r in rows) // 60),
        "owners": owners,
    }
