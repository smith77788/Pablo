"""Mini App API — aiohttp routes for the Telegram Mini App + SSE real-time updates."""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import json as _json  # модульный алиас: ряд эндпоинтов используют _json без локального import
import logging
import os
import re  # модульный: часть хендлеров зовёт re.split/re.sub без локального import
import time
import urllib.parse
from collections import deque
from typing import Any

import asyncpg
from aiohttp import web

from services import account_status as _acc_status
from services import operation_bus as _obus
from services import op_status
from services.mini_app_auth import (validate_init_data, make_token, parse_token,
                                    signing_secret_ok)
from services.secret_masking import redact_secrets, scrub_payload
from services import mini_app_host_server
from services import mini_app_vault
from services import mini_app_ranking
from services.contacts_hub import segments as _segments
from services import mini_app_bots
from services import mini_app_invite
from services import mini_app_channels
from services import mini_app_monitor
from services import mini_app_operations
from services import mini_app_dashboard
from services import mini_app_dm
from services import mini_app_cf
from services import mini_app_content
from services import mini_app_presence
from services import mini_app_cloud
from services import mini_app_mp
from services import mini_app_networks
from services import mini_app_fleet
from services import mini_app_rankings
from services import mini_app_links
from services import mini_app_deals_board
from services import mini_app_sales
from services import mini_app_va
from services import mini_app_warmup
from services import mini_app_accounts
from services import mini_app_uch
from services import mini_app_proxies
from services import mini_app_workflows
from services import mini_app_community
from services import mini_app_funnels
from services import mini_app_admin
from services import mini_app_broadcast
from services import mini_app_stars
from services import mini_app_protection
from services import mini_app_growth
from services import mini_app_parser
from services import mini_app_workspace
from services.security import (
    check_rate_limit,
    rate_limit_response,
    validate_integer,
    validate_string,
    sanitize_search_query,
    check_sql_suspicious,
    escape_html,
    escape_json_value,
    security_middleware,
    # Использовались в хендлерах, но НЕ импортировались: добавление бота по токену
    # и разбор start-параметра падали NameError → 500 на каждом вызове.
    validate_bot_token,
    validate_start_param,
)

log = logging.getLogger(__name__)

# CRM-статусы жизненного цикла аккаунта (ручная воронка оператора, аналог
# «ПЕРЕМЕСТИТЬ В СТАТУС» Telegram Expert). Отдельно от acc_status (техздоровье).
# Ключ хранится в tg_accounts.stage; подпись/эмодзи — на клиенте.
ACCOUNT_STAGES = {"new", "warming", "ready", "in_work", "resting", "frozen", "reserve"}

# Срезы списка аккаунтов — ОДИН список на все входы.
#
# Был рукописный в каждом хендлере, и `proxy_down` (аккаунты, простаивающие
# из-за мёртвого прокси) в них не попал, хотя билдер WHERE его умеет, а на
# экране есть плитка, которая его ставит. Несовпадение не отвергало запрос, а
# молча подменяло срез на «все»: плитка показывала пять аккаунтов и открывала
# пятьсот, а «применить ко всему срезу» ставило массовую операцию по всему
# флоту вместо тех пяти. Список сверяется с ветками билдера тестом
# tests/test_account_filter_is_not_silently_widened.py.
ACCOUNT_FILTERS = ("all", "active", "cooldown", "banned", "spamblock",
                   "proxy_down", "dead")

# Статусы operation_queue, которые вправе прийти фильтром с фронта. Колонка —
# обычный TEXT без CHECK, поэтому список живёт здесь и его сторожит тест
# tests/test_miniapp_paused_operations.py.
#
# Собирается из модели состояний (services/op_status), а НЕ выписывается руками.
# Рукописный список уже дважды разъезжался с реальностью, и оба раза владелец
# видел одно и то же — «Неизвестный статус операции» (400) на срез, который в
# очереди есть:
#
#   * `partial` — недоведённая работа получила собственное терминальное
#     состояние, и частично выполненные операции нельзя было ни отфильтровать,
#     ни открыть по прямой ссылке с этим срезом;
#   * `waiting_approval` — этот статус ставит database/db.py при постановке
#     операции, требующей подтверждения, а разобрать очередь по нему было
#     нельзя с самого начала.
#
# Поэтому исключений тут нет: допускается всё, что знает модель состояний.
# Статус, которого в очереди фактически не бывает, отдаст пустой список — это
# честно и дешевле, чем ещё один рукописный список, который снова отстанет.
OPERATION_STATUSES = frozenset(op_status.IN_FLIGHT | op_status.TERMINAL)

# Условие «аккаунт готов к работе» — одно на весь продукт. Поднято из
# замыкания `setup_routes`, потому что блок аккаунтов уехал в
# `services/mini_app_accounts.py`, а второй копии этого условия быть не должно:
# именно из-за расхождения счётчик «готовых» когда-то считал мёртвые аккаунты.
ACTIVE_ACC_SQL = (
    "is_active AND session_str IS NOT NULL "
    "AND COALESCE(acc_status,'ok') NOT IN ("
    + _acc_status.sql_dead_list() + ") "
    "AND (cooldown_until IS NULL OR cooldown_until <= now())")

_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 30  # секунд
try:
    _DASHBOARD_QUERY_TIMEOUT = float(os.getenv("MINIAPP_DASHBOARD_QUERY_TIMEOUT", "8"))
except (TypeError, ValueError):
    _DASHBOARD_QUERY_TIMEOUT = 8.0
_DASHBOARD_QUERY_TIMEOUT = min(max(_DASHBOARD_QUERY_TIMEOUT, 1.0), 25.0)
# Потолок числа записей кэша ответов: ключ включает user id и query string,
# так что без потолка словарь растёт с каждым новым пользователем/набором
# параметров и держит тела ответов в памяти до перезапуска процесса.
_CACHE_MAX_ENTRIES = 512

# Инлайн-миграции схемы (self-heal при старте mini_app через on_startup).
# Единый источник истины: применяются в проде _apply_inline_migrations, а в
# e2e-тестах — тем же списком, чтобы тестовая БД совпадала с продовой схемой
# (schema_v*.sql их НЕ содержат).
INLINE_MIGRATIONS: list[str] = [
    "ALTER TABLE operation_queue ADD COLUMN IF NOT EXISTS label TEXT",
    # v231: описание сети — форма его спрашивала, хранить было негде.
    "ALTER TABLE network_instances ADD COLUMN IF NOT EXISTS description TEXT",
    # v215: связывание устройства — автономный вход вне Telegram (PWA/Android).
    """CREATE TABLE IF NOT EXISTS device_pairings (
        id             BIGSERIAL PRIMARY KEY,
        owner_id       BIGINT NOT NULL,
        code           TEXT,
        code_expires_at TIMESTAMPTZ,
        token_fp       TEXT,
        device_label   TEXT DEFAULT '',
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        paired_at      TIMESTAMPTZ,
        last_seen_at   TIMESTAMPTZ,
        revoked_at     TIMESTAMPTZ
    )""",
    "CREATE INDEX IF NOT EXISTS idx_device_pairings_code ON device_pairings(code) WHERE code IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_device_pairings_owner ON device_pairings(owner_id)",
    "CREATE INDEX IF NOT EXISTS idx_device_pairings_fp ON device_pairings(token_fp) WHERE token_fp IS NOT NULL",
    # v216: замер позиций каналов/чатов в поиске Telegram по ключам.
    """CREATE TABLE IF NOT EXISTS channel_tracked_keywords (
        id          BIGSERIAL PRIMARY KEY,
        owner_id    BIGINT  NOT NULL,
        channel_id  BIGINT  NOT NULL,
        keyword     TEXT    NOT NULL,
        is_active   BOOLEAN NOT NULL DEFAULT TRUE,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (channel_id, keyword)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chkw_owner ON channel_tracked_keywords(owner_id)",
    "CREATE INDEX IF NOT EXISTS idx_chkw_channel ON channel_tracked_keywords(channel_id)",
    """CREATE TABLE IF NOT EXISTS channel_search_rankings (
        id          BIGSERIAL PRIMARY KEY,
        keyword_id  BIGINT  NOT NULL REFERENCES channel_tracked_keywords(id) ON DELETE CASCADE,
        channel_id  BIGINT  NOT NULL,
        position    INTEGER,
        checked_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chrank_keyword ON channel_search_rankings(keyword_id, checked_at DESC)",
    "ALTER TABLE self_promo_templates ADD COLUMN IF NOT EXISTS owner_id BIGINT",
    # Сигнал активности канала (для ecosystem auto_remove + аналитики):
    # проставляется при реальной публикации (op_worker._exec_mass_publish).
    "ALTER TABLE managed_channels ADD COLUMN IF NOT EXISTS last_post_at TIMESTAMPTZ",
    # Пол участника аудитории ('m'|'f'|NULL) — таргетинг рассылок по полу
    # (services/gender_classifier.py размечает по имени).
    "ALTER TABLE parsed_audiences ADD COLUMN IF NOT EXISTS gender TEXT",
    # Пол аккаунта (локальная категоризация оператора; не поле Telegram-профиля).
    "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS gender TEXT",
    # «Модератор чатов» (Chatkeeper-подобный): чаты под охраной + предупреждения.
    """CREATE TABLE IF NOT EXISTS guard_chats (
        id BIGSERIAL PRIMARY KEY,
        owner_id BIGINT NOT NULL,
        chat_id BIGINT NOT NULL,
        title TEXT DEFAULT '',
        username TEXT DEFAULT '',
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        settings JSONB NOT NULL DEFAULT '{}'::jsonb,
        added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (chat_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_guard_chats_owner ON guard_chats(owner_id)",
    """CREATE TABLE IF NOT EXISTS guard_warnings (
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        warns INT NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (chat_id, user_id)
    )""",
    # Антибот-капча: новички, ожидающие прохождения (persist для сметателя).
    """CREATE TABLE IF NOT EXISTS guard_captcha_pending (
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        captcha_msg_id BIGINT DEFAULT 0,
        expires_at TIMESTAMPTZ NOT NULL,
        action TEXT NOT NULL DEFAULT 'kick',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (chat_id, user_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_guard_captcha_expires ON guard_captcha_pending(expires_at)",
    # Manager Mode: происхождение подключённого бота (manual|managed).
    "ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS created_via TEXT DEFAULT 'manual'",
    # tg_accounts — добавляем поля если отсутствуют
    "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS trust_score REAL",
    "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS acc_status TEXT DEFAULT 'active'",
    "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS cooldown_until TIMESTAMPTZ",
    # user_proxies — полная схема
    """CREATE TABLE IF NOT EXISTS user_proxies (
        id SERIAL PRIMARY KEY,
        owner_id BIGINT NOT NULL,
        label TEXT DEFAULT '',
        proxy_url TEXT NOT NULL,
        proxy_type TEXT DEFAULT 'socks5',
        is_active BOOLEAN DEFAULT true,
        is_alive BOOLEAN DEFAULT true,
        last_check TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT now(),
        UNIQUE(owner_id, proxy_url)
    )""",
    # auto_funnels — создаём если нет
    """CREATE TABLE IF NOT EXISTS auto_funnels (
        id SERIAL PRIMARY KEY,
        owner_id BIGINT NOT NULL,
        bot_id BIGINT,
        name TEXT NOT NULL,
        target_segment TEXT DEFAULT 'all',
        enabled BOOLEAN DEFAULT true,
        created_at TIMESTAMPTZ DEFAULT now(),
        updated_at TIMESTAMPTZ DEFAULT now()
    )""",
    "ALTER TABLE auto_funnels ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now()",
    """CREATE TABLE IF NOT EXISTS auto_funnel_steps (
        id SERIAL PRIMARY KEY,
        funnel_id INTEGER NOT NULL,
        step_num INTEGER DEFAULT 1,
        message_text TEXT,
        delay_hours INTEGER DEFAULT 0,
        completed BOOLEAN DEFAULT false
    )""",
    "ALTER TABLE auto_funnel_steps ADD COLUMN IF NOT EXISTS step_num INTEGER DEFAULT 1",
    """CREATE TABLE IF NOT EXISTS auto_funnel_runs (
        id SERIAL PRIMARY KEY,
        funnel_id INTEGER NOT NULL,
        user_id BIGINT,
        status TEXT DEFAULT 'active',
        started_at TIMESTAMPTZ DEFAULT now()
    )""",
    # platform_users — settings_json column
    "ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS settings_json TEXT",
    # account_warmup_plans — создаём если нет
    """CREATE TABLE IF NOT EXISTS account_warmup_plans (
        id             BIGSERIAL PRIMARY KEY,
        owner_id       BIGINT NOT NULL,
        account_id     BIGINT NOT NULL,
        plan_type      TEXT NOT NULL DEFAULT 'standard',
        current_day    INT  DEFAULT 0,
        target_days    INT  DEFAULT 14,
        daily_actions  INT  DEFAULT 5,
        status         TEXT DEFAULT 'active',
        started_at     TIMESTAMPTZ DEFAULT now(),
        completed_at   TIMESTAMPTZ,
        last_action_at TIMESTAMPTZ,
        meta           JSONB DEFAULT '{}'
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_warmup_account ON account_warmup_plans(account_id)",
    # v196: память плана о том, ПОЧЕМУ он стоит. Без этих колонок экран прогрева
    # показывал «🟢 Активен · День 3/21» на аккаунте, который умер на второй день,
    # а паузу движка (бан/спам-блок) было не отличить от паузы человека.
    "ALTER TABLE account_warmup_plans ADD COLUMN IF NOT EXISTS pause_reason TEXT",
    "ALTER TABLE account_warmup_plans ADD COLUMN IF NOT EXISTS pause_detail TEXT",
    "ALTER TABLE account_warmup_plans ADD COLUMN IF NOT EXISTS paused_at TIMESTAMPTZ",
    "ALTER TABLE account_warmup_plans ADD COLUMN IF NOT EXISTS pause_notified_at TIMESTAMPTZ",
    "ALTER TABLE account_warmup_plans ADD COLUMN IF NOT EXISTS last_skip_reason TEXT",
    "ALTER TABLE account_warmup_plans ADD COLUMN IF NOT EXISTS last_skip_at TIMESTAMPTZ",
    # v200: боты, найденные на аккаунтах флота (скан через @BotFather).
    # Найденный ≠ подключённый: у найденного есть username и аккаунт-владелец,
    # но нет токена, поэтому в managed_bots (token UNIQUE NOT NULL) он не лежит.
    """CREATE TABLE IF NOT EXISTS discovered_bots (
        id BIGSERIAL PRIMARY KEY, owner_id BIGINT NOT NULL, acc_id BIGINT,
        username TEXT NOT NULL, linked_bot_id BIGINT,
        first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (owner_id, username))""",
    "CREATE INDEX IF NOT EXISTS idx_discovered_bots_owner ON discovered_bots(owner_id, last_seen DESC)",
    # v199: общие папки как канал инвайтинга.
    """CREATE TABLE IF NOT EXISTS chatlist_folders (
        id BIGSERIAL PRIMARY KEY, owner_id BIGINT NOT NULL, acc_id BIGINT,
        title TEXT NOT NULL, chat_ids BIGINT[] NOT NULL DEFAULT '{}',
        chat_count INTEGER NOT NULL DEFAULT 0, filter_id INTEGER,
        invite_link TEXT, invite_slug TEXT, status TEXT NOT NULL DEFAULT 'draft',
        error TEXT, instance_id BIGINT, join_count INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_chatlist_folders_owner ON chatlist_folders(owner_id, created_at DESC)",
    # v210: «Нотариус» — независимое заверение рекламных размещений. Панель
    # наблюдателей фиксирует хронологию поста и подписывает протокол.
    """CREATE TABLE IF NOT EXISTS notary_watches (
        id BIGSERIAL PRIMARY KEY, owner_id BIGINT NOT NULL,
        channel_ref TEXT NOT NULL, channel_title TEXT, msg_id BIGINT,
        advertiser TEXT, price_paid NUMERIC(12,2), currency TEXT DEFAULT 'USD',
        promised_from TIMESTAMPTZ NOT NULL DEFAULT now(),
        promised_until TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL DEFAULT 'active', verdict TEXT,
        first_seen_at TIMESTAMPTZ, last_seen_at TIMESTAMPTZ,
        absent_since TIMESTAMPTZ, views_first INTEGER, views_last INTEGER,
        checks_done INTEGER NOT NULL DEFAULT 0,
        next_check_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        cert_sig TEXT, cert_issued_at TIMESTAMPTZ, note TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_notary_watches_owner ON notary_watches(owner_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_notary_watches_due ON notary_watches(next_check_at) WHERE status = 'active'",
    """CREATE TABLE IF NOT EXISTS notary_observations (
        id BIGSERIAL PRIMARY KEY,
        watch_id BIGINT NOT NULL REFERENCES notary_watches(id) ON DELETE CASCADE,
        observed_at TIMESTAMPTZ NOT NULL DEFAULT now(), state TEXT NOT NULL,
        views INTEGER, acc_id BIGINT, geo TEXT, detail TEXT, sig TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_notary_obs_watch ON notary_observations(watch_id, observed_at)",
    # v211: Virtual Layer — динамические состояния и виртуальные события.
    # Надстройка над unified_contacts.stage: состояние с уверенностью, распадом
    # и историей, адресуемое на любом уровне (user|bot|campaign|network).
    """CREATE TABLE IF NOT EXISTS virtual_states (
        owner_id BIGINT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
        state_key TEXT NOT NULL, value TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0.5,
        source TEXT, expires_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (owner_id, entity_type, entity_id, state_key))""",
    "CREATE INDEX IF NOT EXISTS idx_vstates_owner_key ON virtual_states(owner_id, entity_type, state_key, value)",
    "CREATE INDEX IF NOT EXISTS idx_vstates_expiry ON virtual_states(expires_at) WHERE expires_at IS NOT NULL",
    """CREATE TABLE IF NOT EXISTS virtual_state_history (
        id BIGSERIAL PRIMARY KEY, owner_id BIGINT NOT NULL, entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL, state_key TEXT NOT NULL, from_value TEXT,
        to_value TEXT NOT NULL, reason TEXT, confidence REAL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_vstate_hist_entity ON virtual_state_history(owner_id, entity_type, entity_id, state_key, created_at)",
    # v212: Bot Mesh — координация задач между ботами (bot-to-bot). Гашение петель
    # на нашей стороне: глубина, дедлайн, дедуп шага (UNIQUE), цикл в трассе.
    """CREATE TABLE IF NOT EXISTS bot_mesh_tasks (
        task_id TEXT PRIMARY KEY, owner_id BIGINT NOT NULL, origin_bot BIGINT,
        route JSONB NOT NULL DEFAULT '[]', step INTEGER NOT NULL DEFAULT 0,
        depth INTEGER NOT NULL DEFAULT 0, trace JSONB NOT NULL DEFAULT '[]',
        payload JSONB NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'running',
        drop_reason TEXT, deadline_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_bot_mesh_owner ON bot_mesh_tasks(owner_id, created_at DESC)",
    """CREATE TABLE IF NOT EXISTS bot_mesh_hops (
        id BIGSERIAL PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES bot_mesh_tasks(task_id) ON DELETE CASCADE,
        step INTEGER NOT NULL, from_bot BIGINT, to_bot BIGINT, capability TEXT,
        outcome TEXT, reason TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_bot_mesh_hops_task ON bot_mesh_hops(task_id, created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_bot_mesh_hop_step ON bot_mesh_hops(task_id, step, to_bot)",
    # v213: массовое включение bot-to-bot. Отметка состояния, чтобы не гонять
    # BotFather повторно и видеть, кто готов к сети.
    "ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS b2b_enabled BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS b2b_checked_at TIMESTAMPTZ",
    # v198: здоровье управляемых ботов. Без этих колонок бот с отозванным
    # токеном выглядел «активным», молча не отвечал подписчикам, и знал об
    # этом только серверный лог.
    "ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS last_error TEXT",
    # Сценарий и бот: модалка создания показывает выбор бота и присылает
    # bot_id, а колонки под него не было — выбор выбрасывался молча.
    "ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS bot_id BIGINT",
    "ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS last_error_at TIMESTAMPTZ",
    "ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS fail_streak INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS last_ok_at TIMESTAMPTZ",
    "ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS dead_notified_at TIMESTAMPTZ",
    # v64 columns — safe to run repeatedly via ADD COLUMN IF NOT EXISTS.
    # TEXT, not FLOAT: schema_v64.sql (the actual authoritative migration,
    # applied first in any real deploy) declares it TEXT DEFAULT NULL — this
    # mirror had drifted from it and would have created the wrong column type
    # on any environment where this ran before schema_v64.sql.
    "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS warmup_level TEXT DEFAULT NULL",
    "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS last_warmup_at TIMESTAMPTZ",
    # crm_deals: fix stage CHECK constraint (schema had 'new/contacted/qualified',
    # API and JS use 'lead/contact/proposal/negotiation')
    "ALTER TABLE crm_deals ALTER COLUMN stage SET DEFAULT 'lead'",
    "ALTER TABLE crm_deals DROP CONSTRAINT IF EXISTS crm_deals_stage_check",
    "ALTER TABLE crm_deals ADD CONSTRAINT crm_deals_stage_check "
    "CHECK (stage IN ('lead','contact','proposal','negotiation','won','lost'))",
    # Воркфлоу: automation_workflows, workflow_step_runs и
    # workflow_step_logs здесь больше НЕ заводятся. Это был отдельный,
    # более ранний набор таблиц под сценарии — три таблицы и три индекса
    # создавались на каждом старте, и за всё время ни одна строка в них не
    # записывалась и не читалась: ни один запрос в проекте их не упоминает.
    # Живая модель сценариев другая — workflow_definitions и workflow_runs
    # (schema_v183.sql, schema_v250_workflow_bot.sql,
    # services/workflow_engine.py). Мёртвый набор не просто занимал старт:
    # он ловил на себя следующего читателя, который ищет «workflow» и
    # пишет запрос к не той таблице. Уже существующие пустые таблицы не
    # удаляем: DROP необратим, а вреда от них теперь нет.
    # Сокращатель ссылок («bit.ly для себя»): код → целевой URL + счётчик кликов,
    # срок действия (expires_at) и теги (как у bit.ly).
    """CREATE TABLE IF NOT EXISTS short_links (
        code TEXT PRIMARY KEY,
        owner_id BIGINT NOT NULL,
        target_url TEXT NOT NULL,
        title TEXT,
        clicks BIGINT NOT NULL DEFAULT 0,
        disabled BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_click_at TIMESTAMPTZ,
        expires_at TIMESTAMPTZ,
        tags TEXT
    )""",
    "ALTER TABLE short_links ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
    "ALTER TABLE short_links ADD COLUMN IF NOT EXISTS tags TEXT",
    "CREATE INDEX IF NOT EXISTS idx_short_links_owner ON short_links(owner_id, created_at DESC)",
]


# Ключ инлайн-миграции в журнале `schema_migrations`: отпечаток самого текста.
# Префикс отделяет её от файлов `schema_vN.sql`, у которых там имя файла.
_INLINE_KEY_PREFIX = "inline:"


def inline_migration_key(stmt: str) -> str:
    """Отпечаток оператора, по которому он числится применённым.

    Пробелы и переводы строк нормализуются: переформатировали отступы в списке
    — ключ не поменялся, и оператор не поедет заново. Поменяли текст по смыслу
    — ключ другой, и оператор применится.
    """
    return _INLINE_KEY_PREFIX + hashlib.sha256(
        " ".join(stmt.split()).encode("utf-8")).hexdigest()[:32]


async def apply_inline_migrations(pool, stmts: list[str] | None = None) -> dict[str, int]:
    """Накатить инлайн-миграции на старте — один раз, а не на каждый запуск.

    Что было не так. Список гнался целиком при КАЖДОМ старте веб-процесса: 75
    отдельных `pool.execute`, из них 30 `ALTER TABLE`. Два следствия, и оба
    больно:

    1. `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` берёт `ACCESS EXCLUSIVE` даже
       когда колонка уже есть и делать нечего. Потолка ожидания блокировки на
       этом пути НЕ БЫЛО (`SET lock_timeout` в `create_pool` действует на одно
       соединение, которое он сам взял, а не на пул), поэтому обычный деплой
       мог встать насмерть: новый контейнер ждёт блокировку, которую держит
       читающая транзакция старого, а за этим ожиданием выстраиваются все
       читатели. Снаружи — «приложение не загружается».
    2. Каждый оператор брал из пула своё соединение и возвращал, то есть 75
       заходов туда-обратно на пустом месте.

    Что теперь. Одно соединение на весь накат, с потолком ожидания блокировки, и
    журнал `schema_migrations` — тот же, по которому пропускаются файлы
    `schema_vN.sql`. Применённый оператор в журнале, на следующем старте он
    пропускается, и обычный деплой не берёт ни одной блокировки.

    Fail-open по журналу: не смогли его прочитать — играем весь список, как
    раньше. Неудавшийся оператор в журнал НЕ пишется, поэтому повторится на
    следующем старте сам — это и есть прежний self-heal.

    `INFRAGRAM_INLINE_REPLAY=1` прогоняет всё заново, не глядя на журнал: нужно,
    если кто-то руками снёс колонку и схему надо вылечить.
    """
    if stmts is None:
        stmts = INLINE_MIGRATIONS
    keys = [inline_migration_key(s) for s in stmts]
    replay = (os.getenv("INFRAGRAM_INLINE_REPLAY") or "").strip().lower() in (
        "1", "true", "yes")
    # Одно соединение на весь накат. У заглушки пула в тестах отдельных
    # соединений нет — тогда льём прямо через неё: потолок блокировки там не
    # нужен, а ронять старт из-за формы заглушки нельзя.
    try:
        ctx = pool.acquire()
    except Exception:
        ctx = None
    if ctx is None or not hasattr(ctx, "__aenter__"):
        return await _run_inline_migrations(pool, stmts, keys, replay)
    async with ctx as conn:
        return await _run_inline_migrations(conn, stmts, keys, replay)


async def _run_inline_migrations(conn, stmts: list[str], keys: list[str],
                                 replay: bool) -> dict[str, int]:
    """Тело наката на УЖЕ ВЗЯТОМ соединении. Отдельно — чтобы `async with` не
    дублировал десяток строк ради двух способов добыть соединение."""
    # Локальный импорт: модульного импорта database.db здесь нет намеренно
    # (циклическая зависимость), остальной файл тоже импортирует его по месту.
    from database.db import apply_schema_lock_timeout, is_lock_timeout

    applied = failed = skipped = 0
    # Потолок ставится на ЭТО соединение: без него DDL ждёт блокировку
    # бесконечно и кладёт старт целиком.
    await apply_schema_lock_timeout(conn)
    done: set[str] = set()
    if not replay:
        try:
            rows = await conn.fetch(
                "SELECT filename FROM schema_migrations "
                "WHERE status = 'ok' AND filename = ANY($1::text[])", keys)
            done = {r["filename"] for r in rows}
        except Exception:
            # Журнала ещё нет (первый старт) или он недоступен — играем всё.
            done = set()
    for stmt, key in zip(stmts, keys):
        if key in done:
            skipped += 1
            continue
        try:
            await conn.execute(stmt)
        except Exception as exc:
            failed += 1
            if is_lock_timeout(str(exc)):
                # Таблица занята (обычно умирающий контейнер предыдущего
                # деплоя). Не повторяем здесь: старт важнее, а в журнал
                # оператор не попал — доиграется на следующем запуске.
                log.warning("инлайн-миграция ждала блокировку и не дождалась, "
                            "повторится на следующем старте: %.80s", stmt[:80])
            else:
                log.exception("инлайн-миграция не применилась: %.80s", stmt[:80])
            continue
        applied += 1
        try:
            await conn.execute(
                """INSERT INTO schema_migrations(filename, status, error_count, applied_at)
                   VALUES($1, 'ok', 0, now())
                   ON CONFLICT (filename) DO UPDATE SET status = 'ok',
                       error_count = 0, applied_at = now()""", key)
        except Exception:
            # Не записали — оператор просто прогонится ещё раз. Он
            # идемпотентен, так что это не ошибка, а лишняя работа.
            log.debug("не смог отметить инлайн-миграцию в журнале", exc_info=True)
    log.info("Инлайн-миграции: применено %d, пропущено %d (уже в журнале), ошибок %d",
             applied, skipped, failed)
    return {"applied": applied, "skipped": skipped, "failed": failed}

# Заголовки, которые НЕЛЬЗЯ переносить из снимка в новый ответ: их считает сам
# aiohttp под конкретное соединение. Перенос Content-Length/Transfer-Encoding
# ломает кадрирование ответа.
_CACHE_SKIP_HEADERS = {
    "content-length", "content-type", "transfer-encoding",
    "date", "server", "connection", "keep-alive",
}


def _cache_snapshot(resp: Any) -> tuple | None:
    """Снимок ответа для кэша: (status, body, content_type, charset, headers).

    Кладём в кэш ДАННЫЕ, а не объект ``web.Response``. Один и тот же объект
    ответа нельзя отдать дважды: aiohttp при первой отдаче выставляет ему
    ``_eof_sent``, и на втором запросе ``prepare()`` возвращает None, тело не
    пишется — клиент просто ВИСНЕТ до таймаута. Кэшируется только обычный
    ``web.Response`` с готовым телом и статусом 200: стримы (SSE) и ошибки
    кэшировать нельзя — 401/500 залипли бы на весь TTL.
    """
    if not isinstance(resp, web.Response) or resp.status != 200:
        return None
    body = resp.body
    if not isinstance(body, (bytes, bytearray)):
        return None  # StreamResponse / file sender / пустое тело
    headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() not in _CACHE_SKIP_HEADERS
    }
    return (resp.status, bytes(body), resp.content_type, resp.charset, headers)


def _cache_replay(snap: tuple) -> web.Response:
    """Собрать НОВЫЙ ответ из снимка — каждый запрос получает свой объект."""
    status, body, content_type, charset, headers = snap
    return web.Response(
        body=body, status=status, content_type=content_type,
        charset=charset, headers=headers,
    )


def _cache_prune(now: float, max_ttl: float = _CACHE_TTL) -> None:
    """Выбросить протухшее и, если всё ещё тесно, самое старое.

    Ключ кэша включает user id и query string, поэтому без вычистки словарь рос
    бы неограниченно (по записи на каждого пользователя × каждый набор
    параметров) и удерживал тела ответов в памяти процесса навсегда.
    """
    for k in [k for k, (ts, _) in _cache.items() if now - ts >= max_ttl]:
        _cache.pop(k, None)
    if len(_cache) > _CACHE_MAX_ENTRIES:
        for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[
            : len(_cache) - _CACHE_MAX_ENTRIES
        ]:
            _cache.pop(k, None)


def _cache_drop_user(uid) -> int:
    """Снять закэшированные ответы одного пользователя. Возвращает сколько снято.

    Ключ записи — "имя_обработчика:uid:строка_запроса", и у одного списка записей
    столько, сколько страниц и срезов человек успел открыть. Сравниваем ровно
    средний отрезок, а не ищем ":uid:" подстрокой: такая подстрока может
    встретиться и внутри чужой строки запроса.
    """
    if not uid:
        return 0
    want = str(uid)
    dead = [k for k in _cache
            if (parts := k.split(":", 2)) and len(parts) >= 2 and parts[1] == want]
    for k in dead:
        _cache.pop(k, None)
    return len(dead)


def _cached(key: str, ttl: int = _CACHE_TTL):
    """Кэш ОБЩИХ (не привязанных к пользователю) ответов под фиксированным ключом.

    Ключ не содержит user id — применять только там, где ответ одинаков для
    всех. Для пользовательских данных есть _cached_user.
    """
    def wrapper(fn):
        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            now = time.time()
            hit = _cache.get(key)
            if hit and now - hit[0] < ttl:
                return _cache_replay(hit[1])
            result = await fn(*args, **kwargs)
            snap = _cache_snapshot(result)
            if snap is not None:
                _cache[key] = (now, snap)
                _cache_prune(now, ttl)
            return result
        return wrapped
    return wrapper


def _cached_user(ttl: int = _CACHE_TTL):
    """Кэш ответа по user id И параметрам запроса.

    В ключ обязательно входит query string: `bots`/`channels` принимают
    limit/offset, и без этого вторая страница отдавала бы закэшированную первую.
    """
    def wrapper(fn):
        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            request = args[0] if args else kwargs.get('request')
            uid = _get_uid(request) if request is not None else None
            if not uid:
                return await fn(*args, **kwargs)
            try:
                qs = request.query_string or ""
            except Exception:
                qs = ""
            key = f"{fn.__name__}:{uid}:{qs}"
            now = time.time()
            hit = _cache.get(key)
            if hit and now - hit[0] < ttl:
                return _cache_replay(hit[1])
            result = await fn(*args, **kwargs)
            snap = _cache_snapshot(result)
            if snap is not None:
                _cache[key] = (now, snap)
                _cache_prune(now, ttl)
            return result
        return wrapped
    return wrapper


# Показывается вместо входа, когда на сервере нет ключа подписи. Продолжать
# при пустом BOT_TOKEN нельзя: ключ подписи вывелся бы из пустой строки, то
# есть стал бы общеизвестным, и вход подделал бы кто угодно (см.
# services/mini_app_auth.signing_secret_ok).
#
# Статус 403, а не 5xx: на 502/503 фронт показывает «сервис временно
# недоступен» и прячет настоящую причину (tests/test_miniapp_no_gateway_status).
# И не 401: на него фронт уходит в повторный вход по кругу, а тут повтор не
# поможет — нужно поправить переменную окружения.
_NO_SIGNING_SECRET = (
    "Вход недоступен: на сервере не задан BOT_TOKEN. "
    "Укажите его в переменных окружения и перезапустите приложение."
)


def _bot_token() -> str:
    return os.getenv("BOT_TOKEN", os.getenv("MANAGER_BOT_TOKEN", ""))


_bot_username_cache: str | None = None


async def _resolve_bot_username() -> str:
    """Реальный username системного бота. Сначала env BOT_USERNAME, иначе get_me()
    (с кэшем), чтобы фронт не подставлял хардкод вроде @botmother_bot."""
    global _bot_username_cache
    env_u = os.getenv("BOT_USERNAME", "").lstrip("@").strip()
    if env_u:
        return env_u
    if _bot_username_cache is not None:
        return _bot_username_cache
    token = _bot_token()
    if not token:
        _bot_username_cache = ""
        return ""
    try:
        from aiogram import Bot as _Bot
        _b = _Bot(token=token)
        try:
            me = await _b.get_me()
            _bot_username_cache = (me.username or "").lstrip("@")
        finally:
            try:
                await _b.session.close()
            except Exception as e:
                log.warning("_resolve_bot_username session.close: %s", e)
    except Exception as e:
        log.warning("_resolve_bot_username failed: %s", e)
        _bot_username_cache = ""
    return _bot_username_cache


def _json_resp(data: Any, status: int = 200) -> web.Response:
    # Последний барьер: секрет не уходит в ответ, даже если о нём не подумали в
    # обработчике. Все ответы API идут через эту функцию, поэтому достаточно
    # одного места: `SELECT *` по таблице с сессиями или прокси, написанный
    # завтра, отдаст клиенту «***», а не доступ к аккаунту. Чистка по имени
    # ключа и по форме значения — подробности в services/secret_masking.py.
    data = scrub_payload(data)
    return web.Response(
        text=json.dumps(data, ensure_ascii=False, default=str),
        content_type="application/json",
        status=status,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
        },
    )


# Что видит человек, когда на сервере сломалось. Раньше здесь был `str(exc)`:
# внутренний текст исключения, по-английски — «'NoneType' object has no attribute
# 'id'», «relation "vault_messages" does not exist». Владелец английского не
# читает вовсе, а действовать по такому тексту нельзя и тому, кто читает.
# Подробности уходят в лог, наружу — одна русская фраза.
_INTERNAL_ERROR = "Внутренняя ошибка сервера. Повторите позже."

# Нечитаемое тело запроса — вина клиента, и отвечать на неё надо 400.
#
# Тело читают около 250 изменяющих ручек, и ни одна не разбирает ошибку
# разбора: `await request.json()` на мусоре или на ПУСТОМ теле бросает
# JSONDecodeError, и дальше это ловит либо общий `except` ручки, либо слой
# ниже — в обоих случаях владелец получает «Что-то пошло не так, попробуйте
# ещё раз». Совет неверный: повтор того же запроса даст то же самое. Плюс на
# каждый такой заход в журнал уезжает полная трассировка, и настоящие
# пятисотки тонут среди чужих опечаток. И 500 для клиента значит «повтори
# позже», а 400 — «не повторяй».
#
# Чинить это в 250 ручках нельзя: класс останется открытым для 251-й.
# Поэтому решение стоит слоем — там же, где уже стоит страховка от
# необработанных исключений.
#
# Отличить вину клиента от своей обязательно, и на глаз это не делается:
# JSONDecodeError прилетает и изнутри — из jsonb-поля базы, из ответа чужого
# API. Ответить на это «ты прислал мусор» значило бы соврать. Признак точный:
# исключение ВЫЛЕТЕЛО ИЗ разбора тела, то есть его трассировка проходит через
# `Request.json`/`Request.post` самого aiohttp. Тело при этом не перечитывается:
# перечитывание в обработчике исключения — лишний ввод-вывод на пути ошибки.
_BAD_BODY = ("Запрос не разобран: приложение отправило неполные данные. "
             "Закройте и откройте мини-апп, затем повторите.")

_BODY_READERS = ("json", "post")


def bad_body_response(request, exc: BaseException):
    """Ответ 400, если тело прислано сломанным; иначе None.

    Ручек, читающих тело, около 250, и у многих свой общий `except`, который
    перехватывает ошибку раньше страховочного слоя. Поэтому решение зовут и
    оттуда — но текст сообщения и журнальный след лежат ЗДЕСЬ, в одном месте:
    иначе 28 копий разойдутся, и часть из них начнёт писать в журнал тело
    запроса, в котором бывают токены.

    В журнал уходит метод и путь, без трассировки: сломан запрос, а не сервер,
    и трассировка тут лишь прячет настоящие пятисотки.
    """
    if not body_came_broken_from_the_client(exc):
        return None
    log.warning("API: нечитаемое тело запроса %s %s",
                request.method, request.rel_url.path)
    return _err(_BAD_BODY, 400)


def body_came_broken_from_the_client(exc: BaseException) -> bool:
    """Исключение вылетело из разбора ТЕЛА запроса, а не из наших данных."""
    if not isinstance(exc, (ValueError, UnicodeDecodeError)):
        return False
    tb = exc.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        if (code.co_name in _BODY_READERS
                and "web_request" in code.co_filename):
            return True
        tb = tb.tb_next
    return False


def _err(msg: str, status: int = 400) -> web.Response:
    # Единственная дверь наружу для текста ошибки, а текст этот чаще всего —
    # str(исключения). Ошибки asyncpg несут строку подключения, ошибки Telethon
    # — токен, ошибки прокси — логин с паролем. Чистим здесь, чтобы утечка не
    # зависела от того, вспомнил ли автор конкретного из 500 мест про секреты.
    msg = redact_secrets(msg) if msg else msg
    body = {"error": msg}
    # Единый маркер пейволла: ЛЮБОЙ 403 «требуется подписка» помечаем, чтобы фронт
    # показал экран апгрейда (ценность + 1 тап к оформлению) вместо сухого тоста —
    # момент отказа доступа это лучшая точка конверсии. Один источник маркировки на
    # все 20+ мест платного гейта (все ходят через _err).
    if status == 403 and msg and "подписк" in str(msg).lower():
        body["paywall"] = True
        body["code"] = "subscription_required"
    return _json_resp(body, status)


# ── Потолок времени на один запрос API ───────────────────────────────────────
# У aiohttp НЕТ таймаута на обработчик, и у продукта его не было ни в одном
# слое: запрос, зависший внутри (половинчатый прокси, который принял TCP и
# молчит; блокировка в базе; внешний сервис без ответа), висел БЕСКОНЕЧНО.
# Для владельца это вечный спиннер в мини-аппе, а для сервера — накопление
# живых соединений и занятых корутин: достаточно десятка таких запросов, чтобы
# API перестал отвечать всем остальным. Поштучные `asyncio.wait_for` на вызовы
# Telegram это не закрывают: они стоят там, где кто-то про них вспомнил.
_REQUEST_TIMEOUT_DEFAULT = 90.0

# Чего потолок НЕ касается:
#   /api/miniapp/events — SSE-поток живёт минутами ПО ЗАМЫСЛУ;
#   /miniapp, /.well-known — статику отдаёт сам aiohttp;
#   скачивание файла из облака — обработчик собирает куски ИЗ TELEGRAM, и для
#   крупного файла это законно дольше потолка (экспорт в CSV/JSON, наоборот,
#   идёт только из базы и под потолок попадает);
#   multipart — загрузка файла с мобильной сети честно идёт долго, её
#   ограничивает лимит размера, а не секунды.
#
# Отдача ТЕЛА ответа потолком не ограничена в любом случае: aiohttp отправляет
# его после возврата из цепочки middleware, поэтому большой `Response`/
# `FileResponse` на медленном канале не обрывается.
_NO_TIMEOUT_PREFIXES = ("/api/miniapp/events", "/miniapp", "/.well-known")
_NO_TIMEOUT_SUFFIXES = ("/download",)


def va_post_link(username, channel_id, msg_id) -> tuple[str | None, bool]:
    """Ссылка на пост В КАНАЛЕ по данным канала. Возвращает (ссылка, приватный?).

    Публичный канал (есть @username) → t.me/<username>/<msg_id>.
    Приватный → t.me/c/<internal>/<msg_id> (откроется у участника); peer id
    вида -100XXXXXXXXXX приводим к внутреннему XXXXXXXXXX. Нет msg_id (старый
    пост до фикса bulk-публикации) → ссылки нет.
    """
    if not msg_id:
        return None, False
    try:
        mid = int(msg_id)
    except (TypeError, ValueError):
        return None, False
    if username:
        return f"https://t.me/{username}/{mid}", False
    if channel_id:
        try:
            s = str(abs(int(channel_id)))
        except (TypeError, ValueError):
            return None, True
        if s.startswith("100") and len(s) > 3:
            s = s[3:]
        return f"https://t.me/c/{s}/{mid}", True
    return None, True


def request_timeout_seconds() -> float:
    """Потолок в секундах: `MINIAPP_REQUEST_TIMEOUT`, иначе 90.

    Значение вне 5…600 — почти наверняка опечатка (0.5 положил бы весь API,
    86400 равносилен отсутствию потолка), поэтому берём умолчание и говорим
    об этом в лог, а не молча подчиняемся.
    """
    raw = (os.getenv("MINIAPP_REQUEST_TIMEOUT") or "").strip()
    if not raw:
        return _REQUEST_TIMEOUT_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        log.warning("MINIAPP_REQUEST_TIMEOUT=%r не число — беру %.0f с",
                    raw, _REQUEST_TIMEOUT_DEFAULT)
        return _REQUEST_TIMEOUT_DEFAULT
    if not 5.0 <= value <= 600.0:
        log.warning("MINIAPP_REQUEST_TIMEOUT=%s вне диапазона 5…600 — беру %.0f с",
                    raw, _REQUEST_TIMEOUT_DEFAULT)
        return _REQUEST_TIMEOUT_DEFAULT
    return value


def make_request_timeout_middleware(timeout: float):
    """Middleware, прерывающая запрос, который идёт дольше `timeout`.

    Ставится САМОЙ ВНЕШНЕЙ: иначе зависнуть успеет то, что лежит снаружи
    (сжатие, гейт тарифа, сторож частоты), и потолок ничего не закроет.

    Отдаём 504 и говорим владельцу по-русски, что запрос прерван, а не
    оставляем спиннер. Отмена обработчика — не новое для продукта поведение:
    aiohttp отменяет обработчик и при обрыве соединения клиентом, то есть код
    и так обязан переживать отмену на любом шаге.
    """

    @web.middleware
    async def request_timeout_middleware(request: web.Request, handler):
        path = request.rel_url.path
        if path.startswith(_NO_TIMEOUT_PREFIXES) or path.endswith(_NO_TIMEOUT_SUFFIXES):
            return await handler(request)
        if (request.content_type or "").startswith("multipart/"):
            return await handler(request)
        try:
            return await asyncio.wait_for(handler(request), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("API: %s %s прерван по потолку %.0f с",
                        request.method, path, timeout)
            return _err(
                "Запрос выполнялся слишком долго и был прерван. "
                "Повторите попытку позже.", 504)

    return request_timeout_middleware


# Единый нормализатор номера (мини-апп + бот) — принять «+7 932 726 5344» и т.п.
from services.phone_utils import normalize_phone  # noqa: E402


# Единый маппинг UI-операции редактирования канала → op воркера bulk_chan_exec.
# Используется и в channel_edit (одиночный), и в channels_mass (массовый) —
# один источник истины, чтобы наборы не разъехались.
_CHANNEL_EDIT_OPS = {"title": "chan_title", "about": "chan_about", "username": "chan_uname"}


def channel_edit_worker_op(op) -> str | None:
    """UI-op ('title'|'about'|'username') → воркер-op, либо None если не поддержан."""
    return _CHANNEL_EDIT_OPS.get(op)


# Действия, после которых канала (или поста, или участия аккаунта) больше нет:
# UI-op → op воркера bulk_chan_exec. Отдельно от _CHANNEL_EDIT_OPS намеренно:
# правки метаданных обратимы и идут без подтверждения, эти — нет.
_CHANNEL_MANAGE_OPS = {"delete": "chan_delete", "clear": "chan_clear", "leave": "chan_leave"}
# Удаление канала и очистку постов не вернуть — для них владелец набирает слово.
_CHANNEL_IRREVERSIBLE_OPS = frozenset({"delete", "clear"})
CHANNEL_CONFIRM_WORD = "УДАЛИТЬ"


def managed_channel_role(ch) -> str:
    """Роль аккаунта в канале — как в списке каналов (owner/admin/member/unknown).
    По ней карточка предлагает «Удалить в Telegram» создателю и «Покинуть» чужому."""
    if ch.get("is_creator"):
        return "owner"
    if ch.get("is_admin"):
        return "admin"
    return "member" if ch.get("is_admin") is False else "unknown"


def channel_manage_worker_op(op) -> str | None:
    """UI-op ('delete'|'clear'|'leave') → воркер-op, либо None если не поддержан."""
    return _CHANNEL_MANAGE_OPS.get(op)


def channel_manage_confirmed(op, body) -> bool:
    """Подтвердил ли владелец действие. Необратимое требует набранного слова
    «УДАЛИТЬ» в body.confirm — случайный тап или старый клиент его не пришлёт."""
    if op not in _CHANNEL_IRREVERSIBLE_OPS:
        return True
    raw = str((body or {}).get("confirm") or "").strip()
    return raw.upper() == CHANNEL_CONFIRM_WORD


def channel_clear_limit(body) -> int:
    """Сколько последних постов удалить: 0 — все, -1 — значение нечитаемо.

    Нечитаемое число нельзя молча превращать в 0: 0 означает «все посты»,
    и опечатка в поле стёрла бы канал целиком вместо десятка постов."""
    raw = (body or {}).get("clear_limit")
    if raw in (None, "", 0, "0"):
        return 0
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return -1
    return n if 0 < n <= 100000 else -1


def _proxy_display_host(raw: str) -> str:
    """Чистый выходной host прокси для показа в транспорте аккаунта.

    urlparse.hostname единообразен и с креденшелами, и без них; `split('@')[-1]`
    для прокси без auth возвращал весь URL со схемой/портом. Креденшелы никогда не
    показываем. Fallback на хвост после '@' для нераспознанных строк.
    """
    if not raw or not isinstance(raw, str):
        return ""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(raw if "://" in raw else "//" + raw)
        return parsed.hostname or raw.split("@")[-1]
    except (ValueError, TypeError):
        return raw.split("@")[-1]


# SSRF-гард — единственная реализация в services/security.py. Здесь была своя,
# на регулярках, и она пропускала канонический IPv6 (`[::ffff:127.0.0.1]`, ULA,
# link-local): два гарда с разной строгостью хуже одного, потому что усиливают
# всегда не тот.
from services.security import is_safe_public_url, resolve_url_is_public  # noqa: E402,F401


_SCHEDULE_REPEAT_MIN = {"none": 0, "daily": 1440, "weekly": 10080}


def schedule_repeat_minutes(kind) -> int:
    """Период повтора расписания в минутах. none/неизвестное → 0 (одноразовое).
    daily → 1440, weekly → 10080. Чистая функция — тестируема."""
    return _SCHEDULE_REPEAT_MIN.get(str(kind or "none").lower(), 0)


def parse_proxy_type(proxy_url: str) -> str | None:
    """Определяет тип прокси по схеме URL. None — если схема не поддержана.

    Единый источник истины для add_proxy и import_proxies. Поддержка:
    socks5://, socks4://, http://. Чистая функция — тестируема.
    """
    u = (proxy_url or "").strip().lower()
    if u.startswith("socks5://"):
        return "socks5"
    if u.startswith("socks4://"):
        return "socks4"
    if u.startswith("http://"):
        return "http"
    return None


def _reject_proxy_reason(purl: str) -> str | None:
    """Единая проверка одного прокси-URL для add_proxy и import_proxies.

    Возвращает код причины отказа или None если приемлем:
      "too_long"  — длиннее 500 символов;
      "internal"  — loopback/внутренний адрес (SSRF-гигиена);
      "scheme"    — не socks5/socks4/http.
    Чистая функция — один источник правды, чтобы одиночный и массовый ввод
    отбраковывали одно и то же (раньше add_proxy не проверял длину/loopback).
    """
    p = (purl or "").strip()
    if len(p) > 500:
        return "too_long"
    if any(h in p.lower() for h in ("localhost", "127.0.0.1", "0.0.0.0", "::1")):
        return "internal"
    if not parse_proxy_type(p):
        return "scheme"
    return None


PROXY_IMPORT_BATCH = 500   # сколько прокси сервер берёт за один запрос


def _collect_valid_proxies(
        raw: str, limit: int = PROXY_IMPORT_BATCH) -> tuple[list[str], int, int]:
    """Разобрать вставленный список прокси для массового импорта.

    Возвращает (уникальные валидные URL в порядке появления, число пропущенных
    невалидных, число непустых строк ЗА потолком). Третье число раньше не
    считалось вовсе: вставка на 800 прокси добавляла первые 500, экран говорил
    «Добавлено: 500» и очищал поле — остальные 300 пропадали без следа, и узнать
    о них владельцу было негде. Правила (паритет с add_proxy + защита): пустые строки не
    считаются; длиннее 500 символов, loopback/внутренние адреса и неподдержанные
    схемы — пропускаются (skipped); дубли в пределах вставки схлопываются по
    детерминированному proxy_fingerprint (тот же ключ, что у одиночного добавления
    — иначе один и тот же прокси задвоился бы между путями). Чистая функция —
    тестируема без БД и без шифрования (fp не требует pycryptodome для plaintext).
    """
    import re as _re
    from services.token_vault import proxy_fingerprint
    skipped = 0
    seen: set[str] = set()
    out: list[str] = []
    overflow = 0
    _parts = _re.split(r"[\n,;]+", raw or "")
    for _i, line in enumerate(_parts):
        purl = line.strip()
        if not purl:
            continue
        if _reject_proxy_reason(purl) is not None:
            skipped += 1
            continue
        fp = proxy_fingerprint(purl)
        if fp in seen:
            continue
        seen.add(fp)
        out.append(purl)
        if len(out) >= limit:
            overflow = sum(1 for _r in _parts[_i + 1:] if _r.strip())
            break
    return out, skipped, overflow


# Единый источник фильтров parsed_audiences (парсер-вью + инвайт + DM) —
# вынесен в services/audience_filters.py, чтобы все три места применяли одно и то же.
from services.audience_filters import parsed_audience_filters  # noqa: E402,F401


def _spintax_pack(templates: list[str], random_sample: bool = False) -> list[dict[str, Any]]:
    """Список шаблонов → элементы для фронта: шаблон, пример, предупреждения.

    По умолчанию пример — «скелет» (первые варианты групп = исходный текст), он
    всегда читается чисто. Случайную комбинацию отдаём только при явном запросе
    раскрутки (``random_sample=True``).
    """
    from services import spintax_service

    items: list[dict[str, Any]] = []
    for tpl in templates:
        try:
            if random_sample:
                sample = spintax_service.expand_template(tpl)
            else:
                sample = spintax_service.first_option_render(tpl)
        except Exception:
            sample = ""
        # Сколько РАЗНЫХ сообщений даёт шаблон — произведение размеров групп.
        # Ради этого числа spintax в продукте и существует: одинаковый текст,
        # разосланный сотне людей, Telegram ловит как спам. Экран его не
        # показывал, и «хороший» шаблон было не отличить от шаблона с двумя
        # группами по два слова. Потолок — чтобы длинный шаблон не выдал
        # астрономическое число, которое ничего не значит.
        groups = spintax_service.count_group_variants(tpl)
        combos = 1
        for g in groups:
            combos *= max(1, int(g))
            if combos > 10 ** 9:
                combos = 10 ** 9
                break
        items.append(
            {
                "template": tpl,
                "sample": sample,
                "warnings": spintax_service.quality_warnings(tpl),
                "groups": len(groups),
                "combos": combos,
                "combos_capped": combos >= 10 ** 9,
            }
        )
    return items


def _get_uid(request: web.Request) -> int | None:
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else request.query.get("token")
    if not token:
        return None
    return parse_token(token, _bot_token())


def _admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS", "")
    return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}


def _is_admin(uid: int | None) -> bool:
    if not uid:
        return False
    # Check permanent admin IDs from environment
    if uid in _admin_ids():
        return True
    # Check session admins (authenticated via ADMIN_SECRET in bot)
    try:
        from bot.handlers.admin import _session_admins
        if uid in _session_admins:
            return True
    except Exception as e:
        log.warning("_is_admin import session_admins: %s", e)
    return False


def _wants_platform_scope(request, uid: int | None) -> bool:
    """Нужен ли МЕЖТЕНАНТНЫЙ (платформенный) срез аккаунтов вместо своих.

    Раньше любой админ ВСЕГДА видел/оперировал по всем аккаунтам платформы —
    из-за этого два админ-профиля показывали один и тот же список (все 25), а
    масс-операции/«проверить все» шли по чужим аккаунтам. Теперь платформенный
    срез — это ЯВНЫЙ опт-ин (`?scope=platform` в query или `{"scope":"platform"}`
    в теле), и только для админа. По умолчанию — свои аккаунты (owner-scoped),
    даже у админа. Каждый профиль видит СВОИ подключённые аккаунты."""
    if not _is_admin(uid):
        return False
    scope = ""
    try:
        scope = (request.rel_url.query.get("scope") or "").strip().lower()
    except Exception:
        scope = ""
    return scope in ("all", "platform")


def _body_wants_platform_scope(uid: int | None, body: dict) -> bool:
    """То же, что _wants_platform_scope, но для POST-тела (масс-операции/проверка).
    Платформенный срез только у админа и только по явному флагу в теле."""
    if not _is_admin(uid):
        return False
    scope = str((body or {}).get("scope") or "").strip().lower()
    return scope in ("all", "platform")


def _jlist(val) -> list:
    """Safe JSONB→list: asyncpg may return already-parsed list or a JSON string."""
    if isinstance(val, list):
        return val
    if val is None:
        return []
    try:
        return json.loads(val) or []
    except Exception:
        return []


# Период опроса живого потока. Константа, а не число в цикле: от неё зависит и
# нагрузка на базу (запросы на каждое открытое приложение), и то, как быстро
# поток замечает истекший токен — обе величины стоит видеть в одном месте.
SSE_TICK_SECONDS = 15


# Какие каналы человек вообще видит: свои, из своих экосистем и из рабочих
# пространств, где он состоит. Условие жило в файле дважды — в выборке страницы
# и в подсчёте общего числа — и уже начинало расходиться. Теперь один текст на
# оба запроса и на счётчики срезов: разъехаться нечему.
_CHANNELS_VISIBLE_SQL = """
    owner_id=$1
    OR channel_id IN (
        SELECT em2.object_id FROM ecosystem_members em2
        JOIN ecosystems e ON e.id=em2.ecosystem_id
        WHERE em2.object_type='channel'
          AND (e.owner_id=$1
               OR e.id IN (SELECT ecosystem_id FROM ecosystem_members WHERE owner_id=$1))
    )
    OR id IN (
        SELECT DISTINCT mc.id FROM managed_channels mc
        JOIN workspaces w ON w.owner_id=mc.owner_id
        JOIN workspace_members wm ON wm.workspace_id=w.id
        WHERE wm.user_id=$1
    )
"""

# Какие боты человек видит: свои, из своих экосистем и из рабочих пространств.
# Условие тоже жило в файле дважды — в выборке списка и в подсчёте общего числа.
# Удалённый бот (deleted_at) не виден никому: ни в списке, ни по прямой ссылке —
# в отличие от поставленного на паузу, который остаётся в списке с кнопкой
# «Вернуть в работу». Скобки вокруг OR обязательны: AND связывает крепче.
_BOTS_VISIBLE_SQL = """
    mb.deleted_at IS NULL AND (
    mb.added_by=$1
    OR mb.bot_id IN (
        SELECT DISTINCT eb.bot_id FROM ecosystem_bots eb
        JOIN ecosystems e ON e.id=eb.ecosystem_id
        WHERE e.owner_id=$1
           OR e.id IN (SELECT ecosystem_id FROM ecosystem_members WHERE owner_id=$1)
    )
    OR mb.bot_id IN (
        SELECT DISTINCT b.bot_id FROM managed_bots b
        JOIN workspaces w ON w.owner_id=b.added_by
        JOIN workspace_members wm ON wm.workspace_id=w.id
        WHERE wm.user_id=$1
    ))
"""

async def _user_can_use_bot(pool, uid: int, bot_id: int) -> bool:
    """Доступен ли пользователю этот бот: свой, из экосистемы или пространства.

    Проверка доступа лежала в файле ЧЕТЫРЬМЯ дословно одинаковыми копиями — по
    одной на эндпоинт. Разъедься любая, и ровно этот эндпоинт начал бы отдавать
    чужого бота, а остальные три продолжали бы отказывать правильно: дыра,
    которую снаружи почти нечем заметить. Теперь копия одна, и она же — условие
    видимости списка, так что «вижу в списке» и «могу открыть» не разъедутся.

    При ошибке базы `_safe_count` отдаёт 0, то есть доступ закрывается, а не
    открывается: для проверки прав это единственный безопасный исход.
    """
    return bool(await _safe_count(pool,
        f"""SELECT COUNT(*) FROM managed_bots mb
            WHERE mb.bot_id=$2 AND ({_BOTS_VISIBLE_SQL})""", uid, bot_id))


# Группа и канал различаются полем type, владение — is_creator/is_admin.
_CHANNEL_IS_GROUP_SQL = "type IN ('supergroup','group','chat')"

# Один и тот же канал лежит в managed_channels ОТДЕЛЬНОЙ строкой у каждого
# владельца, а условие видимости выше тянет и чужие строки — через экосистемы и
# рабочие пространства. Поэтому список каналов показывал общий канал ДВАЖДЫ, и
# у одной из копий были чужие название и права: экран писал владельцу «админ»
# там, где он не админ. Проверено на живой базе: один канал, две строки.
#
# `SELECT DISTINCT` это не лечило и лечить не мог: он различает строки по ВСЕМУ
# списку столбцов, а у копий отличаются и title, и is_admin, и added_at. Рядом
# стоявший счётчик давно считал `COUNT(DISTINCT channel_id)`, то есть шапка
# говорила «40», а список отдавал 41 строку — число и его же список расходились.
# И сортировка `member_count DESC, channel_id` на таких копиях вырождается:
# ключ равен у обеих, то есть страницы могли повторить канал и потерять другой.
#
# Здесь выбирается РОВНО ОДНА строка на канал, и своя — предпочтительнее чужой
# (`owner_id = $1` первым в сортировке DISTINCT ON), чтобы права и название
# были те, которые касаются смотрящего. После этого channel_id в выдаче
# уникален, список совпадает со своим счётчиком, а сортировка полна.
_CHANNELS_ONE_ROW_SQL = f"""
    SELECT DISTINCT ON (channel_id)
           channel_id, username, title,
           COALESCE(members_count, 0) AS member_count,
           type, added_at, is_admin, is_creator, owner_id
      FROM managed_channels
     WHERE ({_CHANNELS_VISIBLE_SQL})
     ORDER BY channel_id, (owner_id = $1) DESC, id
"""


def _channel_filter_sql(kind: str | None, role: str | None,
                        search: str | None, next_param: int) -> tuple[str, list]:
    """Доп. условия среза и поиска + их параметры.

    Поиск и срезы считались НА КЛИЕНТЕ, по уже загруженным страницам: у кого
    каналов больше страницы, поиск по каналу с четвёртой страницы не находил
    ничего, а чип «👑 Мои 500» показывал не сколько их, а сколько успело
    загрузиться. Поэтому и то и другое переехало в запрос.
    """
    parts: list[str] = []
    args: list = []
    if kind == "group":
        parts.append(_CHANNEL_IS_GROUP_SQL)
    elif kind == "channel":
        parts.append("NOT " + _CHANNEL_IS_GROUP_SQL)
    if role == "mine":
        parts.append("is_creator IS TRUE")
    elif role == "foreign":
        parts.append("is_admin IS FALSE")
    if search:
        parts.append(
            f"(title ILIKE ${next_param} OR username ILIKE ${next_param} "
            f"OR channel_id::text ILIKE ${next_param})")
        args.append(f"%{search}%")
    return ("".join(" AND " + p for p in parts), args)


def _cloud_safe_name(name: str | None) -> str:
    """Имя файла, безопасное для манифеста и для заголовка ответа.

    Имя приходит от пользователя и потом уезжает в `Content-Disposition`. Перевод
    строки в нём ломает весь ответ, а слэш и `..` — это попытка управлять путём у
    того, кто сохраняет файл. Режем управляющие символы и разделители пути; сами
    буквы не трогаем, имена у нас русские.
    """
    raw = (name or "").strip()
    cleaned = "".join(ch for ch in raw if ch.isprintable() and ch not in "\r\n")
    # rstrip, а не strip: точка В НАЧАЛЕ — это обычное скрытое имя (.gitignore),
    # а вот «..» и хвостовые точки — путь и ловушка Windows.
    cleaned = cleaned.replace("\\", "/").rsplit("/", 1)[-1].strip().rstrip(". ")
    return cleaned[:200] or "file"


def _content_disposition(name: str) -> str:
    """`Content-Disposition` с именем, которое переживёт кириллицу.

    Значения заголовков по RFC — latin-1, а aiohttp пишет строку как UTF-8 байты:
    браузер читает их как latin-1 и показывает «Ð¾Ñ‚Ñ‡Ñ‘Ñ‚.xlsx» вместо «отчёт.xlsx».
    Поэтому имя отдаём дважды: ASCII-заглушка для старых клиентов и `filename*`
    (RFC 5987) с процентным кодированием — его понимают все актуальные браузеры.
    """
    safe = _cloud_safe_name(name)
    ascii_name = safe.encode("ascii", "replace").decode("ascii").replace('"', "")
    quoted = urllib.parse.quote(safe, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


def _resolve_best_plan(get_plan_result, exp_plan, pu_plan) -> str:
    """Наивысший тариф среди источников подписки.

    Подписка может жить в РАЗНЫХ таблицах: `subscriptions` (оплата/бот) и
    `platform_users.current_plan` (ручная выдача админом), а per-process кеш
    get_plan мог устареть. Прежняя логика была `plan = get_plan; if not plan:
    fallback` — но "free" ИСТИННО, поэтому фолбэк на platform_users НЕ срабатывал:
    Enterprise, выданный только в platform_users, показывался как Free.

    Каждый аргумент уже отфильтрован по сроку в своём запросе (exp_plan — из
    активной неистёкшей подписки; pu_plan — из platform_users, не истёкший).
    Пустые/None игнорируются. Возвращает канонический тариф (free/paid).
    """
    from bot.utils.subscription import coerce_plan as _cp, PLAN_LEVELS as _PL
    best = "free"
    for cand in (get_plan_result, exp_plan, pu_plan):
        if not cand:
            continue
        try:
            cc = _cp(cand)
        except Exception:
            continue
        if _PL.get(cc, 0) > _PL.get(best, 0):
            best = cc
    return best


def _vault_export_html(data: dict) -> str:
    """Простой самодостаточный HTML-экспорт архива (для скачивания/печати)."""
    import html as _html
    parts = ["<!doctype html><meta charset='utf-8'><title>Экспорт хранилища</title>",
             "<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:760px;"
             "margin:0 auto;padding:16px;background:#0f1115;color:#e6e6e6}"
             "h2{border-bottom:1px solid #333;padding-bottom:6px;margin-top:28px}"
             ".m{margin:6px 0;padding:8px 12px;border-radius:10px;background:#1b1f27}"
             ".out{background:#2a3550;margin-left:40px}.d{color:#f77;text-decoration:line-through}"
             ".meta{font-size:11px;color:#888;margin-top:3px}</style>",
             "<h1>🗄 Экспорт хранилища</h1>"]
    # Молча урезанный экспорт хуже отсутствующего: человек будет думать, что
    # сохранил всю переписку. Пишем предупреждение прямо в файл.
    if data.get("truncated"):
        parts.append(
            "<p style='background:#4a2b12;border:1px solid #a15c1e;padding:10px 12px;"
            "border-radius:10px;font-size:13px'>⚠️ Экспорт неполный: сохранены "
            f"{data.get('exported', 0)} самых свежих сообщений из {data.get('total', 0)}. "
            "Выгрузите отдельные чаты, чтобы получить их целиком.</p>")
    for c in data.get("chats", []):
        parts.append(f"<h2>{_html.escape(str(c.get('peer_name') or c.get('chat_id')))}</h2>")
        for m in c.get("messages", []):
            cls = "m out" if m.get("direction") == "out" else "m"
            txt = _html.escape(m.get("text") or m.get("media") or "—")
            if m.get("deleted"):
                txt = f"<span class='d'>{txt}</span>"
            marks = " · ".join(x for x in (
                "изменено" if m.get("edited") else "", "удалено" if m.get("deleted") else "") if x)
            meta = _html.escape((m.get("date") or "")) + (f" · {marks}" if marks else "")
            parts.append(f"<div class='{cls}'>{txt}<div class='meta'>{meta}</div></div>")
    return "\n".join(parts)


def _csv_resp(filename: str, header: list[str], rows: list[list]) -> web.Response:
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    buf.write("﻿")  # BOM для корректного Excel UTF-8
    w = _csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(["" if c is None else c for c in r])
    import re as _re
    safe_name = _re.sub(r'[^a-zA-Z0-9_\-.]', '_', filename)
    return web.Response(
        text=buf.getvalue(),
        content_type="text/csv",
        charset="utf-8",
        # Выгрузку CORS не открываем: в ней телефоны и имена, а скачивает её
        # сам мини-апп со своего адреса (переходом, не чужим fetch).
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
        },
    )


def _geo_code(raw) -> str:
    """Код страны из запроса: две латинские буквы или UNKNOWN, иначе пусто."""
    cc = str(raw or "").strip().upper()[:7]
    if cc == "UNKNOWN":
        return cc
    return cc if len(cc) == 2 and cc.isalpha() and cc.isascii() else ""


def _list_limit(request: web.Request, default: int, maximum: int) -> int:
    """Потолок списка: по умолчанию короткий, по просьбе экрана — больший.

    Экран сначала берёт первую страницу, а когда владелец нажимает «Показать
    ещё», просит тот же список с большим потолком. Без этого списки молча
    обрывались: тридцать из восьмидесяти выглядели как «всё, что есть».
    """
    raw = request.rel_url.query.get("limit")
    if raw is None:
        return default
    return min(validate_integer(raw, min_val=1, max_val=maximum) or default, maximum)


async def cf_resolve_creds(pool, uid: int) -> dict:
    """Доступы Cloudflare владельца: сперва из его БД (заданы в приложении,
    шифр), иначе — из env (обратная совместимость с Railway-переменными).

    На уровне модуля, а не замыканием внутри `setup_routes`: доступы читает и
    вынесенный `services/mini_app_cf.py`, и сводка о прокси в этом файле, а
    замыкание импортировать нельзя. Токен наружу не отдаётся — в ответ уходит
    только `has_token`.
    """
    from database import db
    creds = await db.get_cf_credentials(pool, uid) or {}
    return {
        "api_token": creds.get("api_token") or os.getenv("CF_API_TOKEN", ""),
        "account_id": creds.get("account_id") or os.getenv("CF_ACCOUNT_ID", ""),
        "subdomain": creds.get("subdomain") or os.getenv("CF_WORKERS_SUBDOMAIN", ""),
        "has_token": bool(creds.get("has_token") or os.getenv("CF_API_TOKEN")),
    }


def int_field(value, title: str, *, default: int | None = None,
              min_val: int | None = None, max_val: int | None = None) -> int:
    """Целое число из тела запроса — с РУССКОЙ причиной отказа.

    `int(d["price_cents"])` внутри try, чей `except ValueError as e` отдаёт
    экрану `str(e)`, показывает владельцу «invalid literal for int() with
    base 10: 'abc'». Владелец английского не читает (CLAUDE.md, первая
    строка), и в этой фразе нет ни имени поля, ни того, что сделать. Проверено
    по реальному пути: так отвечали четыре ручки продаж.

    Фильтровать текст исключения по признаку «есть кириллица» нельзя:
    Python вставляет в сообщение САМО значение, поэтому на русском вводе
    («дорого») английская фраза проходит такой фильтр насквозь. Поэтому
    причина пишется здесь, где поле ещё известно по имени, а не угадывается
    потом по тексту.

    `default=None` значит «поле обязательно»; пустая строка и отсутствие
    значения — это «поля нет», а не ноль.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is None:
            raise ValueError(f"{title}: нужно число")
        return default
    if isinstance(value, bool):
        raise ValueError(f"{title}: нужно число")
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{title}: нужно целое число, а не «{str(value)[:40]}»")
    if min_val is not None and n < min_val:
        raise ValueError(f"{title}: не меньше {min_val}")
    if max_val is not None and n > max_val:
        raise ValueError(f"{title}: не больше {max_val}")
    return n


def _text(value, max_len: int | None = None) -> str:
    """Текст из тела запроса. Не строка — приводим, а не падаем.

    `(body.get("text") or "").strip()` — самый частый способ прочитать текст в
    этом файле, и на нестроковом значении он кидает AttributeError НАРУЖУ
    обработчика: ответ уходит не русским JSON, а страницей aiohttp «Internal
    Server Error», которую экран разобрать не может. Пробой тела по 417
    изменяющим ручкам нашёл девять таких мест.

    `False`/`True` — не текст: логическое значение в текстовом поле значит
    «поля нет», а не строку «False».
    """
    if value is None or isinstance(value, bool):
        return ""
    s = str(value).strip()
    return s[:max_len] if max_len else s


def _str_list(value) -> list[str]:
    """Список строк из тела запроса: строка — разбить, не список — пусто.

    `for u in (body.get("usernames") or [])` на числе даёт TypeError наружу
    обработчика — см. `_text`.
    """
    if isinstance(value, str):
        return [p for p in re.split(r"[\s,;]+", value) if p]
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


from urllib.parse import urlparse as _urlparse  # noqa: E402


# ── Сокращатель ссылок: адрес короткой ссылки и свои хосты ────────────────
#
# Модульные и чистые (зависят только от request), поэтому остались здесь, а
# ручки `/api/miniapp/links/*` уехали в `services/mini_app_links.py`: их
# зовёт и публичный редирект `/{code}`, который остался в монолите.
def _short_base(request: web.Request) -> str:
    """Базовый адрес короткой ссылки: env SHORT_LINK_BASE (напр.
    https://infragram.app) либо домен текущего запроса (учитывая прокси
    Railway через X-Forwarded-*)."""
    base = (os.getenv("SHORT_LINK_BASE") or "").strip().rstrip("/")
    if base:
        return base
    _proto = (request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
              or request.scheme or "https")
    _host = (request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
             or request.headers.get("Host", "") or request.host or "")
    return f"{_proto}://{_host}" if _host else ""


def _self_hosts(request: web.Request) -> set[str]:
    """Наши собственные хосты — чтобы не дать завернуть ссылку на наш же
    редирект (бесконечная петля)."""
    hosts: set[str] = set()
    for h in (os.getenv("SHORT_LINK_BASE"),
              request.headers.get("X-Forwarded-Host"),
              request.headers.get("Host"), request.host):
        if not h:
            continue
        _n = _urlparse(h if "://" in h else "http://" + h).hostname
        if _n:
            hosts.add(_n.lower())
    return hosts


def _list_offset(request: web.Request, maximum: int = 10_000_000) -> int:
    """Сдвиг страницы. Мусор и отрицательное — ноль, а не ошибка запроса.

    `OFFSET -5` Postgres не принимает, и запрос падает пятисоткой: экран
    показывает «внутренняя ошибка сервера» там, где достаточно начать с начала.
    """
    raw = request.rel_url.query.get("offset")
    if raw is None:
        return 0
    return validate_integer(raw, min_val=0, max_val=maximum) or 0


class UploadTooLarge(ValueError):
    """Загрузка превысила предел ещё в процессе чтения."""


async def read_upload(part, max_bytes: int) -> bytes:
    """Прочитать поле multipart кусками, остановившись на пределе.

    Разница с `await part.read()` не в итоговой проверке длины, а в том, что
    проверять уже поздно: `read()` сначала втягивает поле ЦЕЛИКОМ в память, и
    только потом хендлер смотрит на `len(data)`. Предел «25 МБ» при этом
    объявлен, но не действует: владелец с правом загрузки может отправить
    гигабайт, процесс мини-аппа его забуферизует и шлюз ляжет для ВСЕХ
    владельцев, а в ответ тот всё равно получит своё «файл слишком большой».

    Здесь предел действует на чтении: как только в буфере больше `max_bytes`,
    чтение прекращается и остаток тела не читается.
    """
    buf = bytearray()
    while True:
        chunk = await part.read_chunk(65536)
        if not chunk:
            break
        buf += chunk
        if len(buf) > max_bytes:
            raise UploadTooLarge("Файл слишком большой")
    return bytes(buf)


async def read_upload_text(part, max_bytes: int = 64 * 1024) -> str:
    """Прочитать ТЕКСТОВОЕ поле multipart с пределом на чтении.

    `await part.text()` — тот же неограниченный `read()` под капотом, только
    незаметнее: по коду `(await field.text())[:1024]` выглядит как «берём
    тысячу символов», а на самом деле поле сначала втягивается целиком, и лишь
    потом обрезается. Подпись к медиа, идентификатор аккаунта, имя файла в
    облаке — всё это поля, которые владелец заполняет парой слов, а предел у
    них был только после чтения.

    Отличие от `part.text()`: переносное кодирование части (base64 и прочее)
    здесь не раскрывается — `read_chunk` отдаёт байты как есть. Для полей
    формы, которые заполняет мини-апп, его не бывает.
    """
    raw = await read_upload(part, max_bytes)
    charset = "utf-8"
    getter = getattr(part, "get_charset", None)
    if callable(getter):
        try:
            charset = getter(default="utf-8") or "utf-8"
        except Exception:
            charset = "utf-8"
    try:
        return raw.decode(charset, "replace")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")


async def account_health_summary(pool, uid: int) -> dict:
    """Сводка риск-пульса аккаунтов владельца (fail-soft: ошибка → нули).

    Поднята на уровень модуля из замыкания `setup_routes`: её читают и дашборд
    (уехавший в `services/mini_app_dashboard.py`), и экран здоровья аккаунтов в
    монолите, а второй источник правды о пульсе плодить нельзя — CLAUDE.md.
    """
    try:
        from services.infra_memory import get_account_health
        return (await get_account_health(pool, uid))["summary"]
    except Exception:
        return {"healthy": 0, "at_risk": 0, "quarantine": 0, "total": 0}


async def _safe_count(pool: asyncpg.Pool, query: str, *args) -> int:
    try:
        return int(await pool.fetchval(query, *args) or 0)
    except Exception as e:
        log.warning("_safe_count error: %s | query=%.120s", e, query)
        return 0


async def _ecosystem_is_mine(pool, ecosystem_id, uid: int) -> bool:
    """Принадлежит ли экосистема этому пользователю.

    Экосистема — структура доступа: что в ней лежит, видно её владельцу
    (см. _BOTS_VISIBLE_SQL и _CHANNELS_VISIBLE_SQL). Номер экосистемы в
    привязках приходит из тела запроса, то есть целиком со стороны клиента,
    поэтому писать в неё без этой проверки нельзя: посторонний дописывал
    объект в ЧУЖУЮ экосистему, и тот появлялся в списках её владельца.

    Fail-closed: ошибка базы или нечисловой номер — «не моя». Для гейта прав
    это единственный безопасный исход (так же ведёт себя _user_can_use_bot).
    """
    try:
        eco_id = int(ecosystem_id)
    except (TypeError, ValueError):
        return False
    return bool(await _safe_count(
        pool, "SELECT COUNT(*) FROM ecosystems WHERE id=$1 AND owner_id=$2",
        eco_id, uid))


# Проверка прав аккаунта в чате идёт через реальное подключение к Telegram:
# секунды в норме и до таймаута на мёртвой сессии. Последовательный обход
# восьми аккаунтов складывал эти секунды в одну HTTP-паузу, и в худшем случае
# (8 × 25 с) запрос гарантированно обрывался по таймауту раньше, чем успевал
# ответить. Идём управляемо параллельно: одновременных подключений немного,
# чтобы не расширять след флота, но общее ожидание больше не сумма, а максимум
# по пачке.
_ADMIN_SCAN_TIMEOUT = 25
_ADMIN_SCAN_CONCURRENCY = max(1, int(os.getenv("ADMIN_SCAN_CONCURRENCY", "4") or 4))
# Одновременных расчётов ёмкости аккаунта (запросы в БД). Держим ниже
# размера пула asyncpg, чтобы оценка не забирала соединения у остальных.
_PREFLIGHT_CONCURRENCY = max(1, int(os.getenv("PREFLIGHT_CONCURRENCY", "8") or 8))
# Ниже этого размера сжимать ответ невыгодно: заголовок и работа
# компрессора съедают выигрыш, а пакет всё равно уходит одним куском.
_COMPRESS_MIN_BYTES = max(256, int(os.getenv("API_COMPRESS_MIN_BYTES", "1024") or 1024))

# Потолок одновременных живых потоков событий (SSE) на одного владельца. Поток
# держится ОТКРЫТЫМ, пока открыто приложение, и каждые 15 секунд ходит в базу.
# Без потолка достаточно открыть их пачкой (лимит частоты пропускает 120
# запросов в минуту) — и процесс держит сотню вечных соединений, каждое со
# своими запросами: база и память выедаются, а закрыть их снаружи нечем.
# Мини-апп держит РОВНО ОДИН поток на вкладку (старый закрывается перед
# открытием нового), поэтому 8 — это с большим запасом над живым сценарием
# «телефон плюс несколько вкладок».
_SSE_MAX_PER_USER = max(1, int(os.getenv("SSE_MAX_STREAMS_PER_USER", "8") or 8))

# Счётчик живых потоков по владельцу. Строго process-local и таким должен быть:
# соединение физически живёт в ЭТОМ процессе, и считать его в общей БД
# бессмысленно — при перезапуске процесса все его потоки умирают вместе с ним.
_sse_streams: dict[int, int] = {}


async def _scan_admin_status(rows: list, group: str, *,
                             timeout: int = _ADMIN_SCAN_TIMEOUT,
                             concurrency: int = _ADMIN_SCAN_CONCURRENCY) -> list[dict]:
    """Права каждого аккаунта в чате, в ПОРЯДКЕ строк.

    Аккаунт, который не ответил или упал, даёт пустой словарь — вызывающий
    отличает его по отсутствию "ok". Порядок сохраняется, поэтому «первый
    подходящий» остаётся тем же аккаунтом, что и при последовательном обходе.
    """
    if not rows:
        return []
    from services.mass_inviter_engine import channel_admin_status

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(r) -> dict:
        async with sem:
            try:
                st = await asyncio.wait_for(
                    channel_admin_status(r["session_str"], dict(r), group),
                    timeout=timeout)
                return st if isinstance(st, dict) else {}
            except asyncio.TimeoutError:
                log.warning("_scan_admin_status: аккаунт %s не ответил за %ss",
                            r.get("id"), timeout)
                return {}
            except Exception:
                log.debug("_scan_admin_status: аккаунт %s упал", r.get("id"),
                          exc_info=True)
                return {}

    return list(await asyncio.gather(*(_one(r) for r in rows)))


async def _safe_fetch(pool: asyncpg.Pool, query: str, *args) -> list:
    try:
        rows = await pool.fetch(query, *args)
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("_safe_fetch error: %s | query=%.120s", e, query)
        return []


async def _apply_proxy_moves(pool: asyncpg.Pool, uid: int,
                             moves: list[tuple[int, int]]) -> int:
    """Переселить аккаунты на новые прокси ОДНИМ запросом. Вернуть число переехавших.

    Раньше здесь был UPDATE на каждый аккаунт: на флоте в сотни аккаунтов это
    сотни последовательных round-trip внутри одного HTTP-запроса. Пары
    (аккаунт, прокси) уезжают в Postgres массивами и джойнятся через unnest.
    owner_id в WHERE обязателен — чужой аккаунт переселить нельзя даже при
    подделанном плане переезда.
    """
    if not moves:
        return 0
    accs = [int(a) for a, _p in moves]
    pids = [int(p) for _a, p in moves]
    rows = await pool.fetch(
        """UPDATE tg_accounts a SET proxy_id = m.pid
             FROM unnest($1::bigint[], $2::bigint[]) AS m(acc_id, pid)
            WHERE a.id = m.acc_id AND a.owner_id = $3
         RETURNING a.id""",
        accs, pids, uid)
    return len(rows or [])


async def alive_accounts(pool: asyncpg.Pool, uid: int,
                         wanted: list[int] | None = None) -> list[int]:
    """Свои активные аккаунты с сессией; пустой wanted = все.

    На уровне модуля, а не замыканием: тот же список нужен ручкам контактов,
    вынесенным в services/mini_app_uch.py.
    """
    rows = await _safe_fetch(
        pool,
        "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active "
        "AND session_str IS NOT NULL"
        + (" AND id = ANY($2::bigint[])" if wanted else "")
        + " ORDER BY id",
        *([uid, wanted] if wanted else [uid]),
    )
    return [int(r["id"]) for r in (rows or [])]


async def _safe_fetchrow(pool: asyncpg.Pool, query: str, *args) -> dict | None:
    try:
        row = await pool.fetchrow(query, *args)
        return dict(row) if row else None
    except Exception as e:
        log.warning("_safe_fetchrow error: %s | query=%.120s", e, query)
        return None


async def _safe_fetchval(pool: asyncpg.Pool, query: str, *args):
    """Скалярный fetchval с проглатыванием ошибки (→ None). Использовался в 7
    местах (дашборд/аудитория/статы), но НЕ был определён в этом модуле (только в
    op_worker) → NameError валил эндпоинты аналитики/аудитории в 500 («name
    '_safe_fetchval' is not defined»)."""
    try:
        return await pool.fetchval(query, *args)
    except Exception as e:
        log.warning("_safe_fetchval error: %s | query=%.120s", e, query)
        return None


async def _dashboard_query(name: str, awaitable, *, timeout: float | None = None):
    """Изолировать метрику: одна зависшая выборка не задерживает весь dashboard."""
    deadline = _DASHBOARD_QUERY_TIMEOUT if timeout is None else timeout
    try:
        return await asyncio.wait_for(awaitable, timeout=deadline)
    except asyncio.TimeoutError:
        log.warning("Mini App dashboard query timed out: %s after %.1fs", name, deadline)
        return None
    except Exception:
        log.exception("Mini App dashboard query failed: %s", name)
        return None


def _dna_to_dict(dna: Any) -> dict:
    """Serialize an AudienceDNA dataclass instance to a JSON-friendly dict."""
    return {
        "bot_id": dna.bot_id,
        "owner_id": dna.owner_id,
        "peak_hours": list(dna.peak_hours),
        "peak_days": list(dna.peak_days),
        "best_content_types": list(dna.best_content_types),
        "avg_engagement_rate": dna.avg_engagement_rate,
        "churn_risk_pct": dna.churn_risk_pct,
        "top_topics": list(dna.top_topics),
        "total_users_analyzed": dna.total_users_analyzed,
        "computed_at": dna.computed_at.isoformat() if dna.computed_at else None,
    }


_STATS_KEYS = ("bots", "channels", "subscribers", "campaigns_active",
               "accounts", "ops_running", "funnels_active")

# Счётчики дашборда. Скоуп аккаунтов ДОЛЖЕН совпадать с экраном «Аккаунты»:
# там админ видит все аккаунты платформы (межтенантно), а обычный пользователь
# — свои. Иначе на дашборде «Аккаунтов: 0», а на экране — «25» (админ владеет
# 0, но видит все). Счётчик — ВСЕГО в области видимости, а не только активные.
_STATS_SQL = {
    "bots": "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1",
    "channels": "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1",
    "subscribers": ("SELECT COUNT(DISTINCT bu.user_id) FROM bot_users bu "
                    "JOIN managed_bots mb ON mb.bot_id=bu.bot_id WHERE mb.added_by=$1"),
    "campaigns_active": ("SELECT COUNT(*) FROM dm_campaigns "
                         "WHERE owner_id=$1 AND status='running'"),
    "accounts": "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1",
    "ops_running": ("SELECT COUNT(*) FROM operation_queue "
                    "WHERE owner_id=$1 AND status='running'"),
    # funnels.bot_id → managed_bots.added_by = owner
    "funnels_active": ("SELECT COUNT(*) FROM funnel_subscriptions fs "
                       "JOIN funnels f ON f.id=fs.funnel_id "
                       "JOIN managed_bots mb ON mb.bot_id=f.bot_id "
                       "WHERE mb.added_by=$1 AND COALESCE(fs.completed, false)=false"),
}
_STATS_SQL_ADMIN_ACCOUNTS = "SELECT COUNT(*) FROM tg_accounts"


async def _stats(pool: asyncpg.Pool, uid: int, admin: bool = False) -> dict:
    """Счётчики дашборда ОДНИМ запросом.

    Семь COUNT независимы, но платит за них не только открытие дашборда: поток
    событий зовёт _stats каждые 15 секунд на КАЖДОЕ открытое приложение. Семь
    round-trip подряд — это семь ожиданий сети; семь параллельных — это семь
    соединений из пула одновременно на одного пользователя, и при нескольких
    открытых приложениях пул (по умолчанию 20) начинает быть узким местом.
    Скалярные подзапросы дают и одно ожидание, и одно соединение.

    Если объединённый запрос почему-то не прошёл (нет таблицы после неудачной
    миграции), считаем по одному: один сломанный счётчик не должен обнулять
    весь дашборд.
    """
    parts = dict(_STATS_SQL)
    if admin:
        parts["accounts"] = _STATS_SQL_ADMIN_ACCOUNTS
    combined = "SELECT " + ", ".join(
        f"({parts[k]}) AS {k}" for k in _STATS_KEYS)
    try:
        row = await pool.fetchrow(combined, uid)
        if row is not None:
            return {k: int(row[k] or 0) for k in _STATS_KEYS}
    except Exception:
        log.warning("_stats: объединённый запрос не прошёл, считаю по одному",
                    exc_info=True)

    # Межтенантный счётчик аккаунтов $1 не использует — передавать его нельзя,
    # asyncpg отклонит лишний параметр и счётчик молча обнулится.
    values = await asyncio.gather(
        *(_safe_count(pool, parts[k], *(() if "$1" not in parts[k] else (uid,)))
          for k in _STATS_KEYS))
    return dict(zip(_STATS_KEYS, values))


# ── Ядра массовых действий (переиспользуются эндпоинтами и Copilot-apply) ──────
# Вынесены на уровень модуля, чтобы «применить в один клик» из подсказок и
# существующие эндпоинты вызывали одну и ту же проверенную логику (DRY).

_WARMUP_BULK_LIMIT = 500


async def _warmup_bulk_core(
    pool: asyncpg.Pool,
    uid: int,
    plan_type: str = "standard",
    profile_type: str = "mixed",
    niche: str = "general",
) -> dict:
    """Массовый прогрев: планы для ВСЕХ подходящих аккаунтов (активные, с сессией,
    не мёртвые, без активного плана). Фоновый run_warmup_loop подхватит новые
    active-планы.

    Отбор и запись — общая функция `account_warmer.create_warmup_plans_bulk`:
    её же зовёт кнопка «Создать планы для всех» в боте. Своя копия отбора в
    боте не исключала идущие планы и обнуляла им день.

    Бросает PermissionError (нет подписки / Ban Weather) до любой записи.
    """
    from services.account_warmer import create_warmup_plans_bulk

    # Потолок пакета — 500 планов за нажатие; сколько осталось без плана,
    # функция отдаёт в `left`, и экран это показывает.
    return await create_warmup_plans_bulk(
        pool, uid, plan_type=plan_type, profile_type=profile_type, niche=niche,
        limit=_WARMUP_BULK_LIMIT)


async def _check_all_proxies_core(pool: asyncpg.Pool, uid: int) -> dict:
    """Проверить все прокси владельца (ограниченная конкурентность)."""
    rows = await _safe_fetch(pool,
        "SELECT id, proxy_url FROM user_proxies WHERE owner_id=$1 LIMIT 200", uid)
    if not rows:
        return {"ok": True, "checked": 0, "alive": 0}
    from services.proxy_selector import probe_proxy
    sem = asyncio.Semaphore(10)
    verdicts: dict[int, bool] = {}

    async def _one(r):
        async with sem:
            res = await probe_proxy(r["proxy_url"])
            alive = bool(res.get("ok"))
            verdicts[int(r["id"])] = alive
            return alive

    try:
        results = await asyncio.gather(*[_one(r) for r in rows],
                                       return_exceptions=True)
    finally:
        # Итоги — ОДНИМ запросом, а не по одному на прокси: у владельца их до
        # двухсот, и двести отдельных UPDATE на каждую проверку — это двести
        # обращений к базе там, где достаточно одного. Запись в finally, чтобы
        # проверенное не пропало, если запрос прервали на середине.
        await _persist_proxy_verdicts(pool, uid, verdicts)
    alive = sum(1 for x in results if x is True)
    return {"ok": True, "checked": len(rows), "alive": alive}


async def _admin_broadcast_core(pool: asyncpg.Pool, uid: int, text: str) -> dict:
    """Сообщение ВСЕМ пользователям платформы. Пишет след в журнал админа.

    Это самое далеко идущее админское действие продукта: одно нажатие пишет
    каждому живому пользователю, и отменить это нечем. Выдача подписки, бан и
    разбан след в admin_audit_log оставляли, а рассылка — нет: кто и что
    разослал, восстановить было неоткуда. Запись идёт и когда рассылка упала на
    середине — тогда в ней видно, сколько успело уйти.
    """
    from database.db import log_admin_action

    # Не шлём забаненным.
    users = await pool.fetch(
        "SELECT user_id FROM platform_users WHERE COALESCE(is_banned, false) = false")
    if not users:
        await log_admin_action(pool, uid, "admin_broadcast", None,
                               {"recipients": 0, "sent": 0, "failed": 0,
                                "text": text[:500]})
        return {"ok": True, "sent": 0, "failed": 0}

    from aiogram import Bot as _Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from config import BOT_TOKEN as _tok
    # ОДИН Bot на всю рассылку (раньше создавался на каждого юзера —
    # утечка aiohttp-сессий). Закрываем сессию в finally.
    _b = _Bot(token=_tok, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    sent = 0
    failed = 0
    try:
        for u in users:
            try:
                await _b.send_message(u["user_id"], text)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
    finally:
        # Журнал пишем в finally: обрыв на середине (передеплой, таймаут шлюза)
        # — это НЕ повод потерять запись о том, что рассылка уже пошла людям.
        await log_admin_action(pool, uid, "admin_broadcast", None,
                               {"recipients": len(users), "sent": sent,
                                "failed": failed, "text": text[:500]})
        try:
            await _b.session.close()
        except Exception as e:
            log.warning("admin_broadcast session.close: %s", e)
    return {"ok": True, "sent": sent, "failed": failed}


async def _retry_sources(
    pool: asyncpg.Pool, uid: int, op_ids: list[int]
) -> dict[int, dict]:
    """Параметры операций для повтора — одним запросом, id → строка.

    Раньше на каждую операцию шёл свой запрос: до 25 обращений к базе подряд на
    одно нажатие «повторить недоведённые», и владелец ждал их все. Скоуп по
    owner_id остаётся: чужую операцию повторить нельзя.
    """
    if not op_ids:
        return {}
    rows = await _safe_fetch(pool,
        "SELECT id, op_type, params, label, total_items FROM operation_queue "
        "WHERE id = ANY($1::bigint[]) AND owner_id=$2", op_ids, uid)
    return {int(r["id"]): r for r in (rows or [])}


async def _persist_proxy_verdicts(
    pool: asyncpg.Pool, uid: int, verdicts: dict[int, bool]
) -> int:
    """Записать живость проверенных прокси одним запросом. Возвращает сколько."""
    if not verdicts:
        return 0
    ids = list(verdicts.keys())
    flags = [verdicts[i] for i in ids]
    try:
        done = await pool.fetch(
            """UPDATE user_proxies p SET is_alive = v.alive, last_check = now()
                 FROM unnest($1::bigint[], $2::boolean[]) AS v(pid, alive)
                WHERE p.id = v.pid AND p.owner_id = $3
             RETURNING p.id""",
            ids, flags, uid)
        return len(done or [])
    except Exception as e:
        log.warning("check_all_proxies: запись итогов не прошла: %s", e)
        return 0


async def _retry_failed_ops_core(pool: asyncpg.Pool, uid: int, hours: int = 24) -> dict:
    """Перезапустить недоведённые операции владельца за последние `hours` часов.

    Логика живёт в `operation_bus.resubmit_unfinished` — одна дверь на все
    поверхности (мини-апп и бот), чтобы «повторить упавшие» нигде не превращался
    в сырой сброс строки мимо гейта тарифа и предохранителя."""
    return await _obus.resubmit_unfinished(pool, uid, hours)


async def _build_ecosystem_core(pool: asyncpg.Pool, uid: int) -> dict:
    """Авто-создать экосистему из каналов владельца: одна экосистема + все каналы
    как участники (канонический путь — ecosystem_members через add_member, не
    параллельная ecosystem_channels; см. AUDIT_LEDGER). Только группировка в БД,
    без Telegram-действий, обратимо (можно удалить)."""
    rows = await _safe_fetch(pool,
        "SELECT channel_id FROM managed_channels WHERE owner_id=$1 AND channel_id IS NOT NULL LIMIT 500",
        uid)
    ch_ids = [int(r["channel_id"]) for r in (rows or [])]
    from services import ecosystem_brain as _eb
    eco_id = await _eb.create_ecosystem(
        pool, uid, "Моя экосистема",
        description="Создана автоматически из ваших каналов", ecosystem_type="custom")
    added = 0
    for cid in ch_ids:
        try:
            await _eb.add_member(pool, eco_id, uid, "channel", cid)
            added += 1
        except Exception as e:
            log.debug("build_ecosystem add_member cid=%s: %s", cid, e)
    return {"ok": True, "ecosystem_id": eco_id, "channels": added}


async def _apply_next_action(pool: asyncpg.Pool, uid: int, action_id: str) -> dict:
    """Единая точка «применить подсказку Copilot в один клик». Маппит id подсказки
    на безопасное owner-scoped действие. Возвращает {"ok": True, "message": ...}
    или {"error": ...} для подсказки без действия. Клиент присылает только id —
    произвольную операцию с клиента вызвать нельзя. Тестируется напрямую."""
    if action_id in ("warmup_cold_accounts", "warmup_after_register"):
        r = await _warmup_bulk_core(pool, uid)
        if r.get("started"):
            return {"ok": True, "message": f"Прогрев запущен для {r['started']} аккаунтов"}
        return {"ok": True, "message": r.get("note") or "Нет подходящих аккаунтов"}

    if action_id == "replace_dead_proxies":
        r = await _check_all_proxies_core(pool, uid)
        return {"ok": True,
                "message": f"Проверено прокси: {r.get('checked', 0)}, живых: {r.get('alive', 0)}"}

    if action_id == "review_failed_ops":
        r = await _retry_failed_ops_core(pool, uid)
        msg = f"Перезапущено операций: {r.get('retried', 0)}"
        if r.get("skipped"):
            msg += f" (массовых публикаций пропущено: {r['skipped']} — повторите точечно из карточки)"
        return {"ok": True, "message": msg}

    if action_id == "assign_proxies":
        # Назначаем прокси аккаунтам без него — ЧЕРЕЗ вылизанный
        # proxy_rotation.apply_rotation (FOR UPDATE, инъективный план, вычитание
        # занятых чужими прокси). Логику изоляции сами НЕ пишем.
        rows = await _safe_fetch(pool,
            "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE "
            "AND proxy_id IS NULL LIMIT 500", uid)
        ids = [int(r["id"]) for r in (rows or [])]
        if not ids:
            return {"ok": True, "message": "Все аккаунты уже с прокси"}
        from services import proxy_rotation as _pr
        r = await _pr.apply_rotation(pool, uid, account_ids=ids)
        msg = f"Назначено прокси: {r.get('rotated', 0)}"
        if r.get("skipped_no_proxy"):
            msg += f" (не хватило прокси для {r['skipped_no_proxy']} — добавьте прокси в пул)"
        # Иначе «не хватило прокси» при полном пуле выглядит беспричинным.
        if r.get("skipped_dead"):
            msg += f" · {r['skipped_dead']} прокси не взяты: не отвечают"
        return {"ok": True, "message": msg}

    if action_id == "build_ecosystem":
        r = await _build_ecosystem_core(pool, uid)
        return {"ok": True, "message": f"Экосистема создана из {r.get('channels', 0)} каналов"}

    if action_id == "check_account_health":
        # Проверяем ВСЕ свои аккаунты, включая неактивные — смысл проверки в т.ч.
        # вернуть в строй ошибочно отключённые (фильтр is_active это ломал).
        rows = await _safe_fetch(pool,
            "SELECT id FROM tg_accounts WHERE owner_id=$1", uid)
        ids = [int(r["id"]) for r in (rows or [])]
        if not ids:
            return {"ok": True, "message": "Нет аккаунтов для проверки"}
        op_id = await _obus.submit(
            pool, uid, "check_accounts_health",
            {"account_ids": ids, "check_spambot": True},
            total_items=len(ids), label=f"Проверка · аккаунтов: {len(ids)}")
        return {"ok": True, "op_id": op_id,
                "message": f"Проверка здоровья запущена для {len(ids)} аккаунтов"}

    return {"error": "Для этой подсказки нет действия в один клик"}


def setup_routes(app: web.Application, pool: asyncpg.Pool) -> None:
    # Паузы моделей ИИ по лимитам — общие с фоновым циклом (services/llm_gate).
    from services import llm_gate
    llm_gate.attach(pool)

    @web.middleware
    async def plan_gate_middleware(request, handler):
        """Единая страховка ответов API: отказ по тарифу → 403, любое иное
        необработанное исключение → чистый JSON-500 (а не сырой HTML-краш,
        который мини-апп не распарсит).

        `operation_bus.submit` поднимает PlanRequiredError (наследник
        PermissionError), когда у владельца нет нужной подписки — это НЕ сбой
        сервера. Штатные HTTP-ответы (404/редиректы) пропускаем без изменений.
        Хендлеры по-прежнему ловят ошибки сами; middleware — гарантия, что даже
        хендлер БЕЗ try/except не покажет пользователю «внутреннюю ошибку».
        """
        try:
            return await handler(request)
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except web.HTTPException:
            raise  # штатные HTTP-ответы (404/302/…) — не глушим
        except Exception:
            log.exception("unhandled API error: %s %s", request.method, request.rel_url.path)
            return _err(_INTERNAL_ERROR, 500)

    @web.middleware
    async def compress_middleware(request: web.Request, handler) -> web.StreamResponse:
        """Сжимать крупные JSON-ответы API.

        Мини-апп открывают с телефона по мобильной сети, а списки аккаунтов,
        каналов и ботов отдаются целиком, до нескольких сотен килобайт. Статику
        (index.html) сжимали и раньше, а вот ответы API уходили как есть, хотя
        сжимаются они лучше разметки — на однотипном JSON это раз в десять
        меньше трафика и ровно во столько же раз меньше ожидания на плохой
        связи.

        Трогаем только обычные ответы с готовым телом: поток событий (SSE)
        сжимать нельзя — ответ должен уходить клиенту по кускам, а не
        накапливаться в буфере компрессора. Мелочь тоже не трогаем: на коротком
        теле сжатие стоит дороже, чем экономит.
        """
        response = await handler(request)
        try:
            if (isinstance(response, web.Response)
                    and isinstance(response.body, (bytes, bytearray))
                    and len(response.body) >= _COMPRESS_MIN_BYTES
                    and not response.headers.get("Content-Encoding")
                    and "gzip" in request.headers.get("Accept-Encoding", "").lower()):
                # ЯВНО gzip. `enable_compression()` без аргумента выбирает
                # кодирование сам, и выбирает по порядку своего словаря —
                # первым там стоит deflate. То есть условие выше пускало
                # только клиентов, согласных на gzip, а ответ уходил deflate:
                # клиенту, который объявил ровно `Accept-Encoding: gzip`,
                # приходило тело в кодировании, на которое он не соглашался.
                # Браузер такое переварит, а любой нормальный HTTP-клиент
                # (скрипт владельца, мониторинг) — нет. Плюс сырой deflate в
                # вебе реализован вразнобой, gzip — безопасный выбор.
                response.enable_compression(web.ContentCoding.gzip)
                response.headers["Vary"] = "Accept-Encoding"
        except Exception:
            log.debug("compress_middleware: сжатие пропущено", exc_info=True)
        return response

    # Кэш ответов живёт 30 секунд — и этого хватало, чтобы человек не увидел
    # результата собственного действия. Форма добавления бота стоит НА экране
    # ботов, то есть список гарантированно уже в кэше: вставил токен → «✅ Бот
    # добавлен!» → список перезагрузился закэшированным ответом ДО добавления →
    # бота в списке нет. Вывод отсюда один: не получилось, — и токен вставляли
    # снова. То же на выключении бота («убран из работы», а он в строю) и на
    # создании канала.
    #
    # Сбрасываем в одном месте, а не в каждом обработчике: разослать вызов по
    # двум десяткам мутаций — значит однажды забыть его в двадцать первой.
    #
    # Сносим кэш и на тех POST, которые ничего не меняют (опрос QR-входа стучит
    # раз в три секунды). Это сознательно: перебдеть стоит один лишний запрос в
    # базу, недобдеть — человек не видит своего действия. К тому же опрос QR
    # идёт на экране добавления аккаунта, где списков на виду нет, так что
    # промахиваться этому сбросу попросту не по чему.
    _MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    @web.middleware
    async def cache_invalidate_middleware(request: web.Request, handler):
        """Изменил данные — свои закэшированные ответы больше не действительны."""
        response = await handler(request)
        if request.method in _MUTATING_METHODS:
            try:
                # Только удачные: отказ по тарифу и ошибка ничего не изменили,
                # сбрасывать кэш из-за них — лишняя работа на каждом отказе.
                if 200 <= int(getattr(response, "status", 500)) < 400:
                    _cache_drop_user(_get_uid(request))
            except Exception:
                log.debug("сброс кэша после изменения не удался", exc_info=True)
        return response

    @web.middleware
    async def unhandled_error_middleware(request: web.Request, handler):
        """Исключение, выпавшее из обработчика, — русская ошибка, а не пустая 500.

        Весь контракт мини-аппа: ответ — JSON, текст ошибки — по-русски.
        Обработчик, у которого исключение вылетело НАРУЖУ (а не было поймано
        своим try), ломает этот контракт: aiohttp отдаёт свою страницу 500 с
        английским «Internal Server Error», экран не может её разобрать и
        показывает владельцу пустоту или сырой текст.

        Пробой тела запроса по 417 изменяющим ручкам нашёл девять таких:
        поле, которое ждали строкой, приходило числом — `(x or '').strip()` на
        целом даёт AttributeError, а список, собранный из числа, —
        TypeError. Чинить это по месту в девяти ручках значит оставить класс
        открытым для десятой: исключение может выпасть из любой. Поэтому
        страховка стоит слоем — один ответ, один лог, один путь.

        Это НЕ замена обработке ошибок в ручках: своя обработка знает, что
        именно пошло не так, и говорит это владельцу. Слой — последняя сетка.

        `web.HTTPException` пропускаем: это намеренный ответ (редирект, 404
        статики), а не сбой. `CancelledError` — тоже не сбой: так выглядит
        обрыв соединения клиентом и срабатывание потолка времени.
        """
        try:
            return await handler(request)
        except (web.HTTPException, asyncio.CancelledError):
            raise
        except Exception as exc:
            bad = bad_body_response(request, exc)
            if bad is not None:
                return bad
            log.exception("API: необработанное исключение в %s %s",
                          request.method, request.rel_url.path)
            try:
                report_error_safe(request)
            except Exception:
                log.debug("отчёт об ошибке не отправлен", exc_info=True)
            return _err(_INTERNAL_ERROR, 500)

    def report_error_safe(request: web.Request) -> None:
        from services.error_reporting import report_error
        import sys
        exc = sys.exc_info()[1]
        if exc is not None:
            report_error(exc, extra={"context": f"miniapp {request.method} "
                                                f"{request.rel_url.path}"})

    # Самым внешним: статус, который реально увидел человек, складывается уже
    # после plan_gate (он превращает отказ по подписке в 403).
    # Потолок времени — САМЫМ ВНЕШНИМ слоем (aiohttp строит цепочку так, что
    # первый в списке оказывается снаружи): он должен накрывать и остальные
    # middleware, а не только обработчик.
    app.middlewares.append(make_request_timeout_middleware(request_timeout_seconds()))
    # Сразу под потолком времени: накрывает и остальные middleware, и все ручки.
    app.middlewares.append(unhandled_error_middleware)
    app.middlewares.append(cache_invalidate_middleware)
    app.middlewares.append(compress_middleware)
    app.middlewares.append(plan_gate_middleware)
    # Apply security middleware (rate limiting + security headers)
    app.middlewares.append(security_middleware())

    async def _apply_inline_migrations(application: web.Application) -> None:
        await apply_inline_migrations(pool)

    app.on_startup.append(_apply_inline_migrations)

    async def handle_options(request: web.Request) -> web.Response:
        return web.Response(headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
        })

    # ── Auth ────────────────────────────────────────────────────────────────

    async def auth(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        init_data = validate_string(body.get("initData"), max_len=8192)
        if not init_data:
            return _err("Нет данных входа Telegram")
        bot_token = _bot_token()
        if not signing_secret_ok(bot_token):
            return _err(_NO_SIGNING_SECRET, 403)
        user = validate_init_data(init_data, bot_token)
        if not user:
            return _err("Подпись Telegram не подтвердилась — откройте приложение заново", 401)
        token = make_token(user["user_id"], bot_token)
        return _json_resp({"token": token, "user": user})

    # ── Связывание устройства (автономный вход вне Telegram: PWA/Android) ─────

    async def pair_device(request: web.Request) -> web.Response:
        """Обмен одноразового кода связывания (из бота) на токен устройства.
        Публичный: кода достаточно, он одноразовый и короткоживущий."""
        from services import device_pairing
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        code = validate_string(body.get("code"), max_len=32)
        if not code:
            return _err("Введите код связывания")
        label = validate_string(body.get("label"), max_len=80) or ""
        if not signing_secret_ok(_bot_token()):
            return _err(_NO_SIGNING_SECRET, 403)
        redeemed = await device_pairing.redeem_code(pool, code, label=label)
        if not redeemed:
            return _err("Код неверный или истёк — возьмите новый в боте", 401)
        token = device_pairing.make_device_token(redeemed["owner_id"], _bot_token())
        await device_pairing.attach_token_fp(
            pool, redeemed["id"], device_pairing.token_fingerprint(token))
        return _json_resp({"device_token": token})

    async def pair_exchange(request: web.Request) -> web.Response:
        """Обмен долгоживущего токена устройства на обычный сессионный токен —
        дальше приложение работает как Telegram-мини-апп. Публичный: доступ даёт
        сам токен устройства (подписан, с проверкой отзыва по отпечатку)."""
        from services import device_pairing
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        dev = validate_string(body.get("device_token"), max_len=256)
        if not dev:
            return _err("Нет токена устройства")
        bot_token = _bot_token()
        if not signing_secret_ok(bot_token):
            return _err(_NO_SIGNING_SECRET, 403)
        uid = device_pairing.parse_device_token(dev, bot_token)
        if not uid:
            return _err("Устройство не привязано — свяжите заново", 401)
        fp = device_pairing.token_fingerprint(dev)
        if not await device_pairing.device_active(pool, fp):
            return _err("Устройство отозвано — свяжите заново", 401)
        await device_pairing.touch_device(pool, fp)
        return _json_resp({"token": make_token(uid, bot_token),
                           "user": {"user_id": uid}})

    async def devices_list(request: web.Request) -> web.Response:
        """Список связанных устройств владельца (для управления/отзыва).
        Без кэша: список должен обновляться сразу после отзыва."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services import device_pairing
        rows = await device_pairing.list_devices(pool, uid)
        return _json_resp({"devices": [
            {"id": r["id"], "label": r.get("device_label") or "Устройство",
             "paired_at": str(r["paired_at"]) if r.get("paired_at") else None,
             "last_seen_at": str(r["last_seen_at"]) if r.get("last_seen_at") else None,
             "revoked": bool(r.get("revoked_at"))}
            for r in rows]})

    async def devices_revoke(request: web.Request) -> web.Response:
        """Отозвать связанное устройство владельца — его токен перестанет
        обмениваться на сессию. Мутирующий POST — без кэша."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        try:
            pairing_id = int(body.get("id"))
        except (TypeError, ValueError):
            return _err("Неверный идентификатор устройства")
        from services import device_pairing
        ok = await device_pairing.revoke_device(pool, uid, pairing_id)
        return _json_resp({"ok": ok})

    async def factory_name_preview(request: web.Request) -> web.Response:
        """Предпросмотр генерируемых имён и @юзернеймов ДО создания ресурсов.
        Чистый расчёт (name_variator), без БД и без Telegram — чтобы владелец
        видел, что именно получится, и правил шаблон заранее."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            data = {}
        from services import name_variator
        title = validate_string(data.get("title"), max_len=128) or ""
        uname_tpl = validate_string(data.get("username_template"), max_len=64) or ""
        for_bot = bool(data.get("for_bot"))
        n = min(max(int(validate_integer(data.get("count", 6), min_val=1, max_val=12) or 6), 1), 12)
        titles = name_variator.generate_titles(title, n) if title else []
        usernames = []
        if uname_tpl:
            usernames = name_variator.generate_usernames(
                uname_tpl, n, require_suffix=("bot" if for_bot else None))
        items = []
        for i in range(max(len(titles), len(usernames))):
            items.append({
                "title": titles[i] if i < len(titles) else "",
                "username": ("@" + usernames[i]) if i < len(usernames) else "",
            })
        return _json_resp({"items": items})

    # ── Замер позиций каналов/чатов в поиске ─────────────────────────────────





    # ── Dashboard ────────────────────────────────────────────────────────────



    # ── Автоответы бота ──────────────────────────────────────────────────────
    # Сами ручки ботов уехали в services/mini_app_bots.py; здесь осталось то,
    # что делят с ними другие блоки.

    async def toggle_auto_reply(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            reply_id = int(request.match_info["reply_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор ответа", 400)
        try:
            row = await pool.fetchrow(
                """UPDATE auto_replies SET is_active = NOT is_active
                   WHERE id=$1 AND bot_id IN (SELECT bot_id FROM managed_bots WHERE added_by=$2)
                   RETURNING id, is_active""",
                reply_id, uid)
            if not row:
                return _err("Не найдено", 404)
            return _json_resp({"ok": True, "is_active": row["is_active"]})
        except Exception:
            log.exception("toggle_auto_reply")
            return _err("Не удалось переключить", 500)

    async def delete_auto_reply(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            reply_id = int(request.match_info["reply_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор ответа", 400)
        try:
            await pool.execute(
                """DELETE FROM auto_replies WHERE id=$1
                   AND bot_id IN (SELECT bot_id FROM managed_bots WHERE added_by=$2)""",
                reply_id, uid)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("delete_auto_reply")
            return _err("Не удалось удалить", 500)



    # ── Broadcast ────────────────────────────────────────────────────────────








    # ── Channels ─────────────────────────────────────────────────────────────


    # ── Campaigns / Funnels ──────────────────────────────────────────────────

    async def campaigns(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            """SELECT id, name, status, target_type,
                      sent_count, fail_count AS failed_count,
                      total_targets, created_at
               FROM dm_campaigns WHERE owner_id=$1
               ORDER BY created_at DESC LIMIT 20""", uid)
        total = await _safe_count(pool,
            "SELECT COUNT(*) FROM dm_campaigns WHERE owner_id=$1", uid)
        return _json_resp({"campaigns": rows, "total": int(total or 0)})


    # ── Accounts ─────────────────────────────────────────────────────────────

    # Условие «аккаунт реально может пойти в работу» — ОДНО на срез списка
    # «Активные», на счётчик готовых и на масс-операции по этому срезу.
    #
    # Оно обязано совпадать с тем, кого берёт дверь выбора
    # аккаунтов (`resource_selector.select_all_active`): иначе экран обещает
    # флот, которого нет. Раньше он исключал только 'banned' и 'spamblock',
    # поэтому `deactivated`, `session_expired`, `deleted`, `frozen` и аккаунты
    # БЕЗ СЕССИИ считались готовыми — это и есть жалоба владельца «количество
    # аккаунтов и готовых не верное»: операции такой аккаунт не берут ни одна.
    # Набор мёртвых статусов — из единственного места (account_status).











    # ── Operations ───────────────────────────────────────────────────────────












    # ── Deeplinks ────────────────────────────────────────────────────────────



    async def delete_deeplink(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            link_id = int(request.match_info["link_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор ссылки", 400)
        try:
            await pool.execute(
                """DELETE FROM bot_deep_links WHERE id=$1
                   AND bot_id IN (SELECT bot_id FROM managed_bots WHERE added_by=$2)""",
                link_id, uid)
            return _json_resp({"ok": True})
        except Exception as e:
            log.warning("delete_deeplink link=%d uid=%d: %s", link_id, uid, e)
            return _err("Не удалось удалить", 500)

    # ── Engagement segments ───────────────────────────────────────────────────


    # ── Bot Notes ─────────────────────────────────────────────────────────────



    # ── Bot Commands ───────────────────────────────────────────────────────────





    # ── Bot Stats (detailed) ───────────────────────────────────────────────────


    # ── Profile Setter ─────────────────────────────────────────────────────────

    async def profile_setter_status(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            total = await pool.fetchval(
                "SELECT COUNT(*) FROM tg_accounts "
                "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
                "AND (cooldown_until IS NULL OR cooldown_until < NOW())",
                uid,
            )
        except Exception:
            log.exception("profile_setter_status uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"available_accounts": int(total or 0)})

    async def profile_setter_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            op = validate_string(body.get("op"), max_len=20)
            acc_count = validate_integer(body.get("acc_count", 0), min_val=0) or 0
        except Exception:
            return _err("Неверное тело запроса", 400)
        if op not in ("name", "avatar", "2fa"):
            return _err("Операция: имя, аватар или двухфакторная защита", 400)
        if acc_count < 0:
            return _err("Число аккаунтов не может быть отрицательным", 400)
        try:
            total = await pool.fetchval(
                "SELECT COUNT(*) FROM tg_accounts "
                "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
                "AND (cooldown_until IS NULL OR cooldown_until < NOW())",
                uid,
            )
        except Exception:
            log.exception("profile_setter_submit fetchval uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        total = int(total or 0)
        use = min(acc_count, total) if acc_count > 0 else total
        if use == 0:
            return _err("Нет доступных аккаунтов", 400)
        try:
            rows = await pool.fetch(
                "SELECT id FROM tg_accounts "
                "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
                "AND (cooldown_until IS NULL OR cooldown_until < NOW()) "
                "ORDER BY trust_score DESC NULLS LAST LIMIT $2",
                uid, use,
            )
        except Exception:
            log.exception("profile_setter_submit fetch uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        account_ids = [r["id"] for r in rows]
        params: dict = {"op": op, "account_ids": account_ids}
        if op == "name":
            params["name_data"] = {
                "first_name": str(body.get("first_name", "")).strip()[:64],
                "last_name": str(body.get("last_name", "")).strip()[:64],
                "about": str(body.get("about", "")).strip()[:70],
            }
        elif op == "avatar":
            url = str(body.get("avatar_url", "")).strip()
            if not url.startswith("http"):
                return _err("Ссылка на аватар должна начинаться с http", 400)
            params["avatar_url"] = url
        elif op == "2fa":
            new_pass = str(body.get("new_password", "")).strip()
            if len(new_pass) < 4:
                return _err("Пароль минимум 4 символа", 400)
            params["new_password"] = new_pass
            params["current_password"] = str(body.get("current_password", "")).strip()
            params["hint"] = str(body.get("hint", "")).strip()
        label_map = {"name": "Имя/Bio", "avatar": "Аватар", "2fa": "2FA пароль"}
        label = f"Сеттер: {label_map.get(op, op)} × {len(account_ids)} акк."
        try:
            op_id = await _obus.submit(
                pool, uid, "bulk_set_profile", params,
                total_items=len(account_ids), label=label)
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("profile_setter_submit insert uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True, "op_id": op_id, "label": label, "count": len(account_ids)})

    # ── Account Cleaner ────────────────────────────────────────────────────────

    async def cleaner_accounts(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT id, phone, first_name,
                          (SELECT COUNT(*) FROM managed_channels WHERE acc_id=tg_accounts.id) AS asset_count
                   FROM tg_accounts
                   WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL AND session_str <> ''
                   ORDER BY added_at""",
                uid,
            )
        except Exception:
            log.exception("cleaner_accounts uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"accounts": [
            {
                "id": r["id"],
                "phone": r["phone"] or "",
                "name": r["first_name"] or r["phone"] or str(r["id"]),
                "asset_count": int(r["asset_count"] or 0),
            }
            for r in rows
        ]})

    async def cleaner_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            op = validate_string(body.get("op"), max_len=30)
            account_id = validate_integer(body.get("account_id"), min_val=1)
        except Exception:
            return _err("Неверное тело запроса", 400)
        if not op or not account_id:
            return _err("Неверное тело запроса", 400)
        if op not in ("leave_all_chats", "delete_contacts"):
            return _err("Операция: выйти из всех чатов или удалить контакты", 400)
        try:
            row = await pool.fetchrow(
                "SELECT id FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND session_str IS NOT NULL",
                account_id, uid,
            )
        except Exception:
            log.exception("cleaner_submit fetchrow uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        if not row:
            return _err("Аккаунт не найден или нет сессии", 404)
        label_map = {"leave_all_chats": "Выход из чатов", "delete_contacts": "Удаление контактов"}
        label = f"Cleaner: {label_map[op]} акк #{account_id}"
        try:
            op_id = await _obus.submit(
                pool, uid, op, {"account_id": account_id},
                total_items=1, label=label)
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("cleaner_submit insert uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True, "op_id": op_id, "label": label})

    # ── Topology Map ──────────────────────────────────────────────────────────



    # ── Infra Analytics ────────────────────────────────────────────────────────

    async def infra_analytics_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            active_accs = await pool.fetchval(
                "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE", uid
            )
        except Exception as e:
            log.warning("infra_analytics active_accs uid=%d: %s", uid, e)
            active_accs = 0
        try:
            flood_24h = await pool.fetchval(
                """SELECT COUNT(*) FROM account_flood_log fl
                   JOIN tg_accounts ta ON ta.id=fl.account_id
                   WHERE ta.owner_id=$1 AND fl.created_at > NOW()-INTERVAL '24h'""",
                uid,
            )
        except Exception as e:
            log.warning("infra_analytics flood_24h uid=%d: %s", uid, e)
            flood_24h = 0
        try:
            ops_24h = await pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND created_at > NOW()-INTERVAL '24h'",
                uid,
            )
        except Exception as e:
            log.warning("infra_analytics ops_24h uid=%d: %s", uid, e)
            ops_24h = 0
        try:
            warmup_active = await pool.fetchval(
                """SELECT COUNT(*) FROM account_warmup_plans wp
                   WHERE wp.owner_id=$1 AND wp.status='active'""",
                uid,
            )
        except Exception as e:
            log.warning("infra_analytics warmup uid=%d: %s", uid, e)
            warmup_active = 0
        try:
            pools = await pool.fetch(
                """SELECT pool, COUNT(*) AS cnt FROM tg_accounts
                   WHERE owner_id=$1 AND is_active=TRUE AND pool IS NOT NULL
                   GROUP BY pool ORDER BY cnt DESC LIMIT 10""",
                uid,
            )
        except Exception as e:
            log.warning("infra_analytics pools uid=%d: %s", uid, e)
            pools = []
        try:
            audit_rows = await pool.fetch(
                """SELECT action, target, result, occurred_at
                   FROM operation_audit WHERE owner_id=$1
                   ORDER BY occurred_at DESC LIMIT 10""",
                uid,
            )
        except Exception as e:
            log.warning("infra_analytics audit uid=%d: %s", uid, e)
            audit_rows = []
        return _json_resp({
            "active_accounts": int(active_accs or 0),
            "flood_24h": int(flood_24h or 0),
            "ops_24h": int(ops_24h or 0),
            "warmup_active": int(warmup_active or 0),
            "pools": [dict(p) for p in pools],
            "audit": [
                {**dict(a), "occurred_at": a["occurred_at"].isoformat() if a["occurred_at"] else None}
                for a in audit_rows
            ],
        })

    # ── Reporter (Report users) ────────────────────────────────────────────────

    async def new_users(request: web.Request) -> web.Response:
        """Лента новых подписчиков по всем ботам владельца (надёжный фид —
        не зависит от доставки push-уведомлений)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        lim = _list_limit(request, 100, 2000)
        rows = await _safe_fetch(pool,
            """SELECT bu.user_id, bu.username, bu.first_name, bu.first_seen,
                      mb.username AS bot_username
               FROM bot_users bu
               JOIN managed_bots mb ON mb.bot_id = bu.bot_id
               WHERE mb.added_by = $1 AND bu.user_id > 0
               ORDER BY bu.first_seen DESC NULLS LAST, bu.user_id DESC,
                        bu.bot_id DESC
               LIMIT $2 OFFSET $3""", uid, lim, _list_offset(request))
        total = await _safe_count(pool,
            """SELECT COUNT(*) FROM bot_users bu
               JOIN managed_bots mb ON mb.bot_id = bu.bot_id
               WHERE mb.added_by = $1 AND bu.user_id > 0""", uid)
        return _json_resp({"users": rows or [], "total": total})

    async def new_users_export(request: web.Request) -> web.Response:
        """Экспорт ленты новых подписчиков (CSV) по всем ботам владельца."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            """SELECT bu.user_id, bu.username, bu.first_name, bu.first_seen,
                      mb.username AS bot_username
               FROM bot_users bu
               JOIN managed_bots mb ON mb.bot_id = bu.bot_id
               WHERE mb.added_by = $1 AND bu.user_id > 0
               ORDER BY bu.first_seen DESC NULLS LAST
               LIMIT 10000""", uid)
        data = [[r.get("user_id"), r.get("username"), r.get("first_name"),
                 r.get("first_seen"), r.get("bot_username")] for r in (rows or [])]
        return _csv_resp("subscribers.csv",
                         ["user_id", "username", "first_name", "first_seen", "bot"], data)

    async def platform_new_users(request: web.Request) -> web.Response:
        """Лента новых пользователей системного бота @MEXAHI3MBOT (платформа).
        Только для администраторов платформы (ADMIN_IDS)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        if not _is_admin(uid):
            return _err("Только для администраторов платформы", 403)
        rows = await _safe_fetch(pool,
            """SELECT user_id, username, first_name,
                      COALESCE(current_plan,'free') AS plan,
                      COALESCE(registered_at, first_seen, last_seen) AS joined_at,
                      last_seen
               FROM platform_users
               WHERE user_id > 0
               ORDER BY COALESCE(registered_at, first_seen, last_seen) DESC NULLS LAST
               LIMIT 200""", uid)
        total = await _safe_count(pool, "SELECT COUNT(*) FROM platform_users WHERE user_id > 0")
        today = await _safe_count(pool,
            "SELECT COUNT(*) FROM platform_users WHERE COALESCE(registered_at, first_seen, last_seen) >= CURRENT_DATE")
        return _json_resp({"users": rows or [], "total": total, "today": today})

    async def platform_new_users_export(request: web.Request) -> web.Response:
        """Экспорт пользователей платформы (CSV). Только для администраторов."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        if not _is_admin(uid):
            return _err("Только для администраторов платформы", 403)
        rows = await _safe_fetch(pool,
            """SELECT user_id, username, first_name,
                      COALESCE(current_plan,'free') AS plan,
                      COALESCE(registered_at, first_seen, last_seen) AS joined_at,
                      last_seen
               FROM platform_users
               WHERE user_id > 0
               ORDER BY COALESCE(registered_at, first_seen, last_seen) DESC NULLS LAST
               LIMIT 50000""", uid)
        data = [[r.get("user_id"), r.get("username"), r.get("first_name"),
                 r.get("plan"), r.get("joined_at"), r.get("last_seen")] for r in (rows or [])]
        return _csv_resp("platform_users.csv",
                         ["user_id", "username", "first_name", "plan", "joined_at", "last_seen"], data)





    async def global_search(request: web.Request) -> web.Response:
        """Global Search — глобальный поиск публичных каналов/групп/ботов/юзеров
        по строке-запросу (Telethon contacts.SearchRequest). Раздел 12 паритета TE.
        Исполняется инлайн реальным аккаунтом владельца, результат — сразу."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        query = sanitize_search_query(body.get("query") or "")
        if not query:
            return _err("Укажите поисковый запрос", 400)
        try:
            limit = max(1, min(validate_integer(body.get("limit") or 20, min_val=1, max_val=50) or 20, 50))
        except (TypeError, ValueError):
            limit = 20
        # Аккаунт: указанный (грузим канонически по id) либо один лучший через
        # флуд-осознанный выбор (одна дверь), а не «первый по last_used».
        acc_id = body.get("account_id")
        if acc_id:
            from database import db as _db
            acc = await _db.get_account_for_telethon(pool, int(acc_id), uid)
        else:
            from services import resource_selector as _rsel
            acc = await _rsel.select_account(pool, uid, action_type="parse", min_trust_score=0.0)
        if not acc or not acc.get("session_str"):
            return _err("Нет активного аккаунта для поиска — добавьте аккаунт", 400)
        try:
            from services import global_search_engine as gse
            res = await asyncio.wait_for(
                gse.search_public(acc["session_str"], query, limit, _acc=dict(acc), pool=pool),
                timeout=40,
            )
            if res.get("ok"):
                return _json_resp({"ok": True, "results": res.get("results", []), "query": query})
            return _err(res.get("error") or "Поиск не выполнен", 400)
        except asyncio.TimeoutError:
            return _err("Аккаунт не ответил за 40с — проверьте прокси/сессию", 400)
        except Exception as exc:
            log.exception("global_search uid=%d q=%r", uid, query)
            return _err(f"Ошибка: {str(exc)[:140]}", 400)

    async def find_contact(request: web.Request) -> web.Response:
        """Поиск потерянного контакта по имени профиля + началу @username.
        Ставит фоновую операцию find_contact (нативный поиск + перебор @prefix+цифры).
        body: {name, username_prefix, digits(1-4), account_ids?, offset?}. Результат —
        в отчёте операции (перебор до 10k вариантов не влезает в инлайн-запрос)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        name = sanitize_search_query(body.get("name") or "")
        prefix = sanitize_search_query(body.get("username_prefix") or "").lstrip("@")
        if not name:
            return _err("Укажите имя профиля (напр. Oracle)", 400)
        if not prefix:
            return _err("Укажите начало @username (напр. Smile)", 400)
        try:
            digits = validate_integer(body.get("digits"), min_val=1, max_val=4)
        except (TypeError, ValueError):
            digits = None
        if not digits:
            return _err("Число цифр после префикса — 1–4", 400)
        try:
            offset = validate_integer(body.get("offset") or 0, min_val=0, max_val=100000) or 0
        except (TypeError, ValueError):
            offset = 0
        account_ids = [int(x) for x in (body.get("account_ids") or []) if str(x).isdigit()]
        params = {"name": name, "username_prefix": prefix, "digits": int(digits),
                  "account_ids": account_ids, "offset": int(offset)}
        try:
            op_id = await _obus.submit(
                pool, uid, "find_contact", params, total_items=0,
                label=f"Поиск контакта @{prefix}*")
            return _json_resp({"ok": True, "op_id": op_id})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("find_contact uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)













    # ── Живая консоль аккаунта (диалоги / история / отправка / контакты) ──────














    async def diag(request: web.Request) -> web.Response:
        """Сквозная диагностика исполнения: креды/транспорт, аккаунты, очередь,
        живой тест подключения одного аккаунта (тот же путь, что у операций)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        import time as _t
        try:
            from config import TG_API_ID, CF_RELAY_URL, TG_PROXY
        except Exception:
            TG_API_ID, CF_RELAY_URL, TG_PROXY = 0, "", ""
        report: dict = {
            "env": {
                "api_configured": bool(TG_API_ID),
                "cf_relay": bool(CF_RELAY_URL),
                "tg_proxy": bool(TG_PROXY),
            },
            "accounts": {}, "queue": {}, "live_test": {},
        }
        try:
            a = await pool.fetchrow(
                "SELECT COUNT(*) AS total, "
                "COUNT(*) FILTER (WHERE is_active) AS active, "
                "COUNT(*) FILTER (WHERE session_str IS NOT NULL AND session_str<>'') AS with_session, "
                "COUNT(*) FILTER (WHERE proxy_id IS NOT NULL) AS with_proxy "
                "FROM tg_accounts WHERE owner_id=$1", uid)
            report["accounts"] = {k: int(v or 0) for k, v in dict(a).items()} if a else {}
        except Exception as e:
            report["accounts"] = {"error": str(e)[:120]}
        try:
            q = await pool.fetchrow(
                "SELECT COUNT(*) FILTER (WHERE status='pending') AS pending, "
                "COUNT(*) FILTER (WHERE status='running') AS running, "
                "COUNT(*) FILTER (WHERE status='done' AND finished_at>now()-interval '24 hours') AS done_24h, "
                # Недоведённые операции не должны выпадать из сводки за сутки:
                # ими закрывается всё, что оборвалось на половине.
                "COUNT(*) FILTER (WHERE status='partial' AND finished_at>now()-interval '24 hours') AS partial_24h, "
                "COUNT(*) FILTER (WHERE status='failed' AND finished_at>now()-interval '24 hours') AS failed_24h "
                "FROM operation_queue WHERE owner_id=$1", uid)
            report["queue"] = {k: int(v or 0) for k, v in dict(q).items()} if q else {}
        except Exception as e:
            report["queue"] = {"error": str(e)[:120]}
        try:
            row = await pool.fetchrow(
                "SELECT a.id, a.session_str, a.first_name, a.phone, a.device_model, a.system_version, "
                "a.app_version, a.lang_code, a.system_lang_code, p.proxy_url "
                "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
                "WHERE a.owner_id=$1 AND a.is_active=TRUE AND a.session_str IS NOT NULL "
                "ORDER BY a.trust_score DESC NULLS LAST LIMIT 1", uid)
            if not row:
                report["live_test"] = {"ok": False, "reason": "Нет активного аккаунта с сессией для теста"}
            else:
                from services.account_manager import check_account_status_full
                t0 = _t.monotonic()
                res = await check_account_status_full(
                    row["session_str"], _acc=dict(row), check_spambot=False)
                report["live_test"] = {
                    "ok": res.get("status") == "active",
                    "account": str(row["first_name"] or row["phone"] or row["id"]),
                    "status": res.get("status"),
                    "reason": (res.get("reason") or "")[:200],
                    "latency_ms": round((_t.monotonic() - t0) * 1000),
                    "via_proxy": bool(row["proxy_url"]),
                }
        except Exception as e:
            report["live_test"] = {"ok": False, "reason": f"Ошибка теста подключения: {str(e)[:160]}"}
        return _json_resp(report)




    async def boost_submit(request: web.Request) -> web.Response:
        """Накрутка: просмотры / реакции / сторис / подписчики / старты в ботах."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        btype = validate_string(body.get("type"), max_len=20)
        if btype not in ("views", "reactions", "stories", "subscribers", "bot_starts"):
            return _err("Неверный тип накрутки", 400)
        try:
            acc_count = validate_integer(body.get("acc_count") or 0, min_val=0) or 0
        except (TypeError, ValueError):
            acc_count = 0

        # Подбор аккаунтов как в боте: активные, без кулдауна, по trust_score.
        rows = await _safe_fetch(pool,
            "SELECT id FROM tg_accounts "
            "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
            "AND (cooldown_until IS NULL OR cooldown_until < NOW()) "
            "ORDER BY trust_score DESC NULLS LAST LIMIT $2",
            uid, acc_count if acc_count > 0 else 1000)
        account_ids = [r["id"] for r in (rows or [])]
        if not account_ids:
            return _err("Нет доступных аккаунтов", 400)

        def _parse_ids(raw: str) -> list[int]:
            out: list[int] = []
            for part in str(raw or "").replace(" ", "").split(","):
                if not part:
                    continue
                if "-" in part:
                    try:
                        a, b = part.split("-", 1)
                        a, b = int(a), int(b)
                        if 0 < b - a <= 1000:
                            out.extend(range(a, b + 1))
                    except ValueError:
                        continue
                else:
                    try:
                        out.append(int(part))
                    except ValueError:
                        continue
            # dedup, preserve order
            seen: set[int] = set()
            return [x for x in out if not (x in seen or seen.add(x))]

        if btype == "views":
            channel = (body.get("channel") or "").strip()
            msg_ids = _parse_ids(body.get("msg_ids"))
            if not channel or not msg_ids:
                return _err("Укажите канал и ID сообщений", 400)
            op_type = "boost_views"
            params = {"channel": channel, "msg_ids": msg_ids, "account_ids": account_ids}
            total = len(account_ids) * len(msg_ids)
            label = f"Просмотры: {channel} × {len(msg_ids)} × {len(account_ids)} акк."
        elif btype == "reactions":
            channel = (body.get("channel") or "").strip()
            try:
                msg_id = int(body.get("msg_id") or 0)
            except (TypeError, ValueError):
                msg_id = 0
            emoji = (body.get("emoji") or "👍").strip() or "👍"
            if not channel or not msg_id:
                return _err("Укажите канал и ID сообщения", 400)
            op_type = "boost_reactions"
            params = {"channel": channel, "msg_id": msg_id, "emoji": emoji, "account_ids": account_ids}
            total = len(account_ids)
            label = f"Реакции {emoji}: {channel} × {len(account_ids)} акк."
        elif btype == "stories":
            target = (body.get("target") or "").strip()
            if not target:
                return _err("Укажите цель (@username)", 400)
            op_type = "boost_stories"
            params = {"target": target, "account_ids": account_ids}
            total = len(account_ids)
            label = f"Сторис: {target} × {len(account_ids)} акк."
        elif btype == "subscribers":
            from services.boost_engine import parse_channel_ref
            target = parse_channel_ref((body.get("target") or "").strip())
            premium_only = bool(body.get("premium_only"))
            if not target:
                return _err("Укажите канал/группу (@username или t.me/link)", 400)
            op_type = "boost_subscribers"
            params = {"target": target, "account_ids": account_ids, "premium_only": premium_only}
            total = len(account_ids)
            prem_suffix = " (только Premium)" if premium_only else ""
            label = f"Подписчики/участники: {target} × {len(account_ids)} акк.{prem_suffix}"
        else:  # bot_starts
            from services.boost_engine import parse_channel_ref
            bot_username = parse_channel_ref((body.get("bot_username") or "").strip()).lstrip("@")
            payload = (body.get("payload") or "").strip() or None
            premium_only = bool(body.get("premium_only"))
            if not bot_username:
                return _err("Укажите @username бота", 400)
            op_type = "boost_bot_starts"
            params = {
                "bot_username": bot_username,
                "payload": payload,
                "account_ids": account_ids,
                "premium_only": premium_only,
            }
            total = len(account_ids)
            prem_suffix = " (только Premium)" if premium_only else ""
            label = f"Старты в боте: @{bot_username} × {len(account_ids)} акк.{prem_suffix}"

        try:
            if op_type in ("boost_subscribers", "boost_bot_starts"):
                from services import operation_bus
                op_id = await operation_bus.submit(pool, uid, op_type, params, total_items=total)
            else:
                op_id = await _obus.submit(
                    pool, uid, op_type, params, total_items=total, label=label)
            return _json_resp({"ok": True, "op_id": op_id, "label": label, "accounts": len(account_ids)})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("boost_submit uid=%d type=%s", uid, btype)
            return _err(_INTERNAL_ERROR, 500)


    async def ai_comment_submit(request: web.Request) -> web.Response:
        """AI Commenting: контекстные LLM-комментарии под постами целевых каналов.
        body: {channels: [ref...], niche?, tone?, acc_count?}"""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        raw_ch = body.get("channels") or []
        if isinstance(raw_ch, str):
            raw_ch = re.split(r"[\s,]+", raw_ch)
        channels = [str(c).strip().lstrip("@") for c in raw_ch if str(c).strip()][:50]
        niche = validate_string(body.get("niche"), max_len=200) or ""
        from services.ai_comment_engine import COMMENT_TONES
        tone = (body.get("tone") or "neutral").strip()
        if tone not in COMMENT_TONES:
            tone = "neutral"
        try:
            acc_count = max(1, min(validate_integer(body.get("acc_count") or 3, min_val=1, max_val=10) or 3, 10))
        except (TypeError, ValueError):
            acc_count = 3
        if not channels:
            return _err("Укажите хотя бы один канал", 400)
        from services import content_safety
        _v = await content_safety.enforce(pool, uid, niche or "comment", "", surface="ai_comment")
        if _v.blocked:
            return _err("Контент заблокирован политикой платформы", 403)
        has_acc = await _safe_count(pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL",
            uid)
        if not has_acc:
            return _err("Нет активных аккаунтов", 400)
        try:
            label = f"AI-комментинг: {len(channels)} каналов"
            op_id = await _obus.submit(
                pool, uid, "ai_comment",
                {"channels": channels, "niche": niche, "tone": tone, "acc_count": acc_count},
                total_items=len(channels), label=label)
            return _json_resp({"ok": True, "op_id": op_id, "label": label, "channels": len(channels)})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("ai_comment_submit uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def compliance_scan_submit(request: web.Request) -> web.Response:
        """Resource Compliance Scan: read-only проверка ресурсов на запрещённую
        тематику (CSAM/террор). Собирает досье, ничего не постит/не сносит.
        body: {resources: [ref...], per_resource_limit?, acc_count?}"""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        raw = body.get("resources") or []
        if isinstance(raw, str):
            raw = re.split(r"[\s,]+", raw)
        resources = [str(c).strip().lstrip("@") for c in raw if str(c).strip()][:100]
        if not resources:
            return _err("Укажите хотя бы один ресурс для проверки", 400)
        try:
            per_limit = max(1, min(validate_integer(body.get("per_resource_limit") or 50, min_val=1, max_val=200) or 50, 200))
        except (TypeError, ValueError):
            per_limit = 50
        try:
            acc_count = max(1, min(validate_integer(body.get("acc_count") or 2, min_val=1, max_val=10) or 2, 10))
        except (TypeError, ValueError):
            acc_count = 2
        has_acc = await _safe_count(pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL",
            uid)
        if not has_acc:
            return _err("Нет активных аккаунтов", 400)
        try:
            label = f"Проверка на запрещёнку: {len(resources)} ресурсов"
            op_id = await _obus.submit(
                pool, uid, "compliance_scan",
                {"resources": resources, "per_resource_limit": per_limit, "acc_count": acc_count},
                total_items=len(resources), label=label)
            return _json_resp({"ok": True, "op_id": op_id, "label": label, "resources": len(resources)})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("compliance_scan_submit uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)


    async def reporter_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        target = validate_string(body.get("target"), max_len=200)
        reason = validate_string(body.get("reason"), max_len=50) or "spam"
        acc_count = validate_integer(body.get("acc_count", 5), min_val=1, max_val=100) or 5
        if not target:
            return _err("Укажите цель репортинга", 400)
        try:
            label = f"Репорт {target} ({reason}) × {acc_count} аккаунтов"
            op_id = await _obus.submit(
                pool, uid, "report_peer", {"target": target, "reason": reason},
                total_items=acc_count, label=label)
            return _json_resp({"ok": True, "op_id": op_id, "label": label})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("reporter_submit uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    # Экран «Быстрый пост» слит с единой публикацией (см.
    # tests/test_publish_module_unified.py о том, почему он один), а эта ручка
    # была его единственной дверью и больше ничьей: ops типа quick_post заводила
    # только она. Сам тип операции ЖИВ — op_worker и operation_bus исполняют и
    # показывают уже стоящие в очереди и историю; удалена только ручка.


    # ── Bot Factory Overview ───────────────────────────────────────────────────

    async def bot_factory_status(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            total = await pool.fetchval("SELECT COUNT(*) FROM managed_bots WHERE added_by=$1", uid)
            active = await pool.fetchval("SELECT COUNT(*) FROM managed_bots WHERE added_by=$1 AND is_active=TRUE", uid)
            recent = await pool.fetch(
                """SELECT bot_id, username, first_name, is_active, added_at
                   FROM managed_bots WHERE added_by=$1
                   ORDER BY added_at DESC LIMIT 10""",
                uid,
            )
            return _json_resp({
                "total": total, "active": active,
                "recent": [
                    {**dict(r), "added_at": r["added_at"].isoformat() if r["added_at"] else None}
                    for r in recent
                ],
            })
        except Exception:
            log.exception("bot_factory_status uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def ai_status(request: web.Request) -> web.Response:
        """Статус AI для ВЛАДЕЛЬЦА (раньше был только у админа) — чтобы понимать,
        почему AI-автоответчик молчит. Быстрый: без live-пинга, только какие
        провайдеры настроены (ключи в env, глобальные)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services.ai_providers import configured_providers
            provs = configured_providers()
            claude_on = False
            try:
                from services import ai_claude
                claude_on = ai_claude.enabled()
            except Exception:
                claude_on = False
            # Модель Groq, зафиксированная для разогрева чатов (переопр. env).
            try:
                from services.chat_warmup import GROQ_WARMUP_MODEL
            except Exception:
                GROQ_WARMUP_MODEL = "llama-3.3-70b-versatile"
            return _json_resp({
                "configured": bool(provs) or claude_on,
                "claude": claude_on,
                "warmup_groq_model": GROQ_WARMUP_MODEL,
                "providers": [{"name": p.name, "model": (p.models[0] if p.models else None)}
                              for p in provs],
            })
        except Exception:
            log.exception("ai_status uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def ai_test(request: web.Request) -> web.Response:
        """Live-проверка AI провайдеров для владельца (реюз ping_providers).
        Показывает, отвечает ли каждый провайдер и за сколько мс."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services.ai_providers import ping_providers
            results = await ping_providers()
            return _json_resp({
                "any_ok": any(r.get("ok") for r in results),
                "providers": results,
            })
        except Exception:
            log.exception("ai_test uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)


    async def bot_factory_create(request: web.Request) -> web.Response:
        """Создание ботов со всеми настройками из Mini App."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        token = validate_string(data.get("token"), max_len=200)
        name_template = validate_string(data.get("name"), max_len=128)
        uname_template = validate_string(data.get("username"), max_len=32)
        description = validate_string(data.get("description"), max_len=512)
        short_desc = validate_string(data.get("short_description"), max_len=120)
        # ВАЖНО: этот путь регистрирует ОДИН уже существующий токен и применяет
        # оформление через Bot API — BotFather здесь не участвует. Поэтому
        # account_id («аккаунт для BotFather») и count принимать нельзя: раньше
        # они читались и молча игнорировались, а count ещё и возвращался в ответе,
        # создавая иллюзию, что количество учтено. Массовое создание НОВЫХ ботов
        # через BotFather — отдельная операция op_type="bot_factory" (в боте).
        ecosystem_id = data.get("ecosystem_id")
        if not token:
            return _err("Укажите токен бота", 400)
        if not name_template:
            return _err("имя бота обязательно", 400)
        if not validate_bot_token(token):
            return _err("Неверный формат токена", 400)
        try:
            import aiohttp as _aio
            async with _aio.ClientSession() as _http:
                async with _http.get(
                    f"https://api.telegram.org/bot{token}/getMe",
                    timeout=_aio.ClientTimeout(total=10),
                ) as _resp:
                    me = await _resp.json()
            if not me.get("ok"):
                return _err(f"Telegram отказал: {me.get('description', 'неверный токен')}", 400)
            bot_info = me["result"]
            bot_id = bot_info["id"]
            from database import db as _db
            already_owned = await _safe_count(pool,
                "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid)
            if not already_owned:
                from bot.utils.subscription import get_bot_limit, get_effective_bot_count
                _lim = await get_bot_limit(pool, uid)
                if await get_effective_bot_count(pool, uid) >= _lim:
                    return _err(f"Достигнут лимит ботов ({_lim})", 403)
            result = await _db.add_bot(pool, token, bot_id, bot_info.get("username", ""), bot_info.get("first_name", ""), uid)
            if result == "taken":
                return _err("Этот бот уже добавлен другим пользователем", 409)
            if result is False:
                return _json_resp({"ok": True, "already_exists": True, "bot_id": bot_id})
            # Применяем настройки к боту через Telegram API
            applied = []
            if name_template:
                try:
                    async with _aio.ClientSession() as _http:
                        await _http.get(f"https://api.telegram.org/bot{token}/setMyName?name={_aio.helpers.quote(name_template)}", timeout=_aio.ClientTimeout(total=10))
                    applied.append("name")
                except Exception as e:
                    log.warning("bot_factory_create setMyName: %s", e)
            if description:
                try:
                    async with _aio.ClientSession() as _http:
                        await _http.get(f"https://api.telegram.org/bot{token}/setMyDescription?description={_aio.helpers.quote(description)}", timeout=_aio.ClientTimeout(total=10))
                    applied.append("description")
                except Exception as e:
                    log.warning("bot_factory_create setMyDescription: %s", e)
            if short_desc:
                try:
                    async with _aio.ClientSession() as _http:
                        await _http.get(f"https://api.telegram.org/bot{token}/setMyShortDescription?short_description={_aio.helpers.quote(short_desc)}", timeout=_aio.ClientTimeout(total=10))
                    applied.append("short_description")
                except Exception as e:
                    log.warning("bot_factory_create setMyShortDescription: %s", e)
            # Привязка к экосистеме. Номер приходит из тела запроса, то есть
            # целиком со стороны клиента, а экосистема — структура доступа:
            # всё, что в ней лежит, видно её владельцу (_BOTS_VISIBLE_SQL).
            # Без проверки владения посторонний дописывал бота в ЧУЖУЮ
            # экосистему, и тот появлялся в списке ботов её владельца. Это тот
            # же гейт, что в ecosystem_brain.add_member, только для другой
            # таблицы — там он есть, здесь его не было.
            if ecosystem_id:
                try:
                    if not await _ecosystem_is_mine(pool, ecosystem_id, uid):
                        from database.db import record_manual_action

                        await record_manual_action(
                            pool, uid, "ecosystem_foreign_link_refused",
                            target=f"eco:{ecosystem_id} bot:{bot_id}",
                            result="refused")
                        return _err("Экосистема не найдена", 404)
                    await pool.execute(
                        "INSERT INTO ecosystem_bots (ecosystem_id, bot_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        int(ecosystem_id), bot_id,
                    )
                    applied.append("ecosystem")
                except Exception as e:
                    log.warning("bot_factory_create ecosystem link: %s", e)
            return _json_resp({
                "ok": True, "bot_id": bot_id,
                "username": bot_info.get("username", ""),
                "first_name": bot_info.get("first_name", ""),
                "applied_settings": applied,
            })
        except Exception:
            log.exception("bot_factory_create uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def bot_factory_create_new(request: web.Request) -> web.Response:
        """Реальное создание НОВЫХ ботов через @BotFather (паритет с ботом).

        В отличие от bot_factory_create (регистрация уже готового токена), здесь
        выбранный аккаунт РЕАЛЬНО переписывается с @BotFather и создаёт ботов
        пачкой. Контракт параметров совпадает с ботовым путём
        (bot/handlers/bot_factory.py), чтобы не плодить второй источник правды.
        Проверку тарифа делает сама шина (_enforce_min_plan в operation_bus).
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)

        # ── Мульти-аккаунт: создаём ботов через @BotFather у КАЖДОГО выбранного
        # аккаунта. Тот же op bot_factory — при наличии account_ids исполнитель
        # уходит в multi-путь (_exec_bot_factory_multi). ──
        raw_ids = data.get("account_ids")
        if raw_ids:
            try:
                acc_ids = [int(x) for x in raw_ids][:200]
            except (TypeError, ValueError):
                return _err("Список аккаунтов должен быть списком чисел", 400)
            acc_ids = [a for a in acc_ids if a > 0]
            if not acc_ids:
                return _err("Выберите хотя бы один аккаунт", 400)
            bot_name = validate_string(data.get("name_template") or data.get("bot_name"), max_len=128)
            if not bot_name:
                return _err("Укажите шаблон имени бота", 400)
            base_username = validate_string(data.get("uname_template") or data.get("base_username"), max_len=32) or ""
            bot_count = min(max(validate_integer(data.get("count", 1), min_val=1, max_val=10) or 1, 1), 10)
            bf_name_mode = (data.get("name_mode") or "keywords")
            if bf_name_mode not in ("keywords", "num"):
                bf_name_mode = "keywords"
            rows = await _safe_fetch(
                pool,
                "SELECT id FROM tg_accounts WHERE owner_id=$1 AND id = ANY($2::bigint[]) "
                "AND is_active=TRUE AND session_str IS NOT NULL",
                uid, acc_ids)
            owned = [int(r["id"]) for r in (rows or [])]
            if not owned:
                return _err("Нет доступных активных аккаунтов из выбранных", 400)
            total = len(owned) * bot_count
            try:
                from services import operation_bus
                op_id = await operation_bus.submit(
                    pool, uid, "bot_factory",
                    {
                        "account_ids": owned,
                        "bot_count": bot_count,
                        "bot_name": bot_name,
                        "base_username": base_username,
                        "name_mode": bf_name_mode,
                    },
                    total_items=total,
                    label=f"Фабрика ботов: {total} шт. на {len(owned)} акк.",
                )
                return _json_resp({"ok": True, "op_id": op_id, "count": total})
            except PermissionError as exc:
                return _err(str(exc) or "Требуется подписка", 403)
            except Exception:
                log.exception("bot_factory_create_new(multi) uid=%d", uid)
                return _err(_INTERNAL_ERROR, 500)

        acc_id = validate_integer(data.get("acc_id"), min_val=1)
        if not acc_id:
            return _err("Выберите аккаунт, который будет писать в @BotFather", 400)
        name_tpl = validate_string(data.get("name_template"), max_len=128)
        if not name_tpl:
            return _err("Укажите шаблон имени бота", 400)
        uname_tpl = validate_string(data.get("uname_template"), max_len=32) or ""
        count = min(max(validate_integer(data.get("count", 1), min_val=1, max_val=10) or 1, 1), 10)

        # Аккаунт должен принадлежать владельцу и быть живым — иначе операция
        # молча встанет в очередь и провалится уже в воркере.
        acc = await _safe_fetchrow(
            pool,
            "SELECT id, is_active, session_str FROM tg_accounts WHERE id=$1 AND owner_id=$2",
            int(acc_id), uid,
        )
        if not acc:
            return _err("Аккаунт не найден", 404)
        if not acc["is_active"] or not acc["session_str"]:
            return _err("Аккаунт неактивен или без сессии — выберите другой", 400)

        try:
            from services import operation_bus
            op_id = await operation_bus.submit(
                pool, uid, "bot_factory",
                {
                    "acc_id": int(acc_id),
                    "count": count,
                    "name_template": name_tpl,
                    "uname_template": uname_tpl,
                },
                total_items=count,
                label=f"Фабрика ботов: {count} шт. через аккаунт #{acc_id}",
            )
            return _json_resp({"ok": True, "op_id": op_id, "count": count})
        except PermissionError as exc:
            # operation_bus.PlanRequiredError наследует PermissionError:
            # отказ по тарифу отдаём честным 403, а не сырым 500.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("bot_factory_create_new uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)




    async def _import_all_op(request: web.Request, kind: str) -> web.Response:
        """Импорт каналов/групп со ВСЕХ активных аккаунтов (паритет с ботом).

        Операция неразрушающая: обнаруживает уже существующие ресурсы аккаунтов
        и заводит их в БД, в Telegram ничего не меняет — поэтому подтверждения
        не требует. Контракт совпадает с ботовым: {"account_ids": [...]},
        total_items = число аккаунтов.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(
            pool,
            "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active "
            "AND session_str IS NOT NULL ORDER BY id",
            uid,
        )
        acc_ids = [int(r["id"]) for r in (rows or [])]
        if not acc_ids:
            return _err("Нет активных аккаунтов с сессией", 400)
        op_type = "channel_import_all" if kind == "channels" else "group_import_all"
        what = "каналов" if kind == "channels" else "групп"
        try:
            from services import operation_bus
            op_id = await operation_bus.submit(
                pool, uid, op_type,
                {"account_ids": acc_ids},
                total_items=len(acc_ids),
                label=f"Импорт {what} · аккаунтов: {len(acc_ids)}",
            )
            return _json_resp({"ok": True, "op_id": op_id, "accounts": len(acc_ids)})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("_import_all_op(%s) uid=%d", kind, uid)
            return _err(_INTERNAL_ERROR, 500)

    # Поля массового редактирования — ровно те, что понимают исполнители.
    # Источник правды: _exec_bulk_edit_channels / _exec_bulk_bot_edit /
    # _exec_bulk_update_profile. Принять поле, которое исполнитель не знает,
    # значит «тихий успех»: операция пройдёт, а эффекта не будет.
    _BULK_EDIT_FIELDS = {
        "channels": {"title"},
        "bots": {"name", "desc", "short_desc", "commands"},
        "profile": {"username", "first_name", "last_name", "about"},
    }

    async def _bulk_edit_op(request: web.Request, kind: str) -> web.Response:
        """Массовое редактирование каналов / ботов / профилей (паритет с ботом)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)

        field = str(data.get("field") or "").strip()
        allowed = _BULK_EDIT_FIELDS[kind]
        if field not in allowed:
            return _err(f"Недопустимое поле. Доступны: {', '.join(sorted(allowed))}", 400)
        value = validate_string(data.get("value"), max_len=512)
        if not value:
            return _err("Введите новое значение", 400)
        if check_sql_suspicious(value):
            return _err("Недопустимые символы в значении", 400)

        if kind == "channels":
            if len(value) > 128:
                return _err("Название канала — до 128 символов", 400)
            from services import channel_change_approval
            acc_ids = [int(x) for x in (data.get("account_ids") or []) if str(x).isdigit()]
            targets = await _safe_fetch(
                pool,
                "SELECT DISTINCT ON (mc.channel_id) mc.channel_id,mc.acc_id,mc.title,"
                "mc.access_hash,mc.username FROM managed_channels mc JOIN tg_accounts a "
                "ON a.id=mc.acc_id AND a.owner_id=mc.owner_id WHERE mc.owner_id=$1 "
                "AND a.is_active AND a.session_str IS NOT NULL"
                + (" AND mc.acc_id=ANY($2::bigint[])" if acc_ids else "")
                + " ORDER BY mc.channel_id,mc.id",
                *([uid, acc_ids] if acc_ids else [uid]),
            )
            pairs = [dict(row) for row in targets or []]
            if not pairs:
                return _err("Нет каналов с доступными аккаунтами", 400)
            approval_token = str(data.get("approval_token") or "")
            if not approval_token:
                try:
                    token = channel_change_approval.issue(uid, value, pairs)
                except channel_change_approval.ApprovalError as exc:
                    # Единственная причина — на сервере нет ключа подписи
                    # (BOT_TOKEN). Это поломка окружения, а не ошибка владельца,
                    # поэтому она идёт в лог; наружу — внятная русская фраза, а
                    # не текст исключения и не 503: «Сервис недоступен» фронт
                    # показывает вместо причины, и владелец думает, что упал весь
                    # продукт, тогда как не работает одно подтверждение.
                    log.error("_bulk_edit_op: подтверждение состава не выдано: %s", exc)
                    return _err(
                        "Подтверждение изменения сейчас не выдаётся: на сервере "
                        "не настроен ключ подписи. Передайте это администратору.",
                        500)
                return _json_resp({
                    "ok": True, "confirmation_required": True, "approval_token": token,
                    "total": len(pairs), "field": field,
                    "sample": [{"channel_id": str(item["channel_id"]),
                                "old": item.get("title") or "", "new": value}
                               for item in pairs[:20]],
                })
            try:
                channel_change_approval.verify(
                    approval_token, uid, value, pairs,
                )
            except channel_change_approval.ApprovalError as exc:
                return _err(str(exc), 409)
            params = {
                "op": "chan_title",
                "value": value,
                "base_uname": value,
                "channel_acc_pairs": pairs,
                "approval_token": approval_token,
            }
            total = len(pairs)
            label = f"Подтверждённые названия: {total} каналов"
        elif kind == "bots":
            # Контракт бота: боты не перечисляются, операция берёт всех своих.
            cnt = await _safe_count(
                pool, "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1 AND is_active", uid)
            if not cnt:
                return _err("У вас нет активных ботов", 400)
            params = {"field": field, "value": value}
            total = int(cnt)
            label = f"Массовое изменение ботов: {field} ({cnt} шт.)"
        else:
            acc_ids = [int(x) for x in (data.get("account_ids") or []) if str(x).isdigit()]
            rows = await _safe_fetch(
                pool,
                "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active "
                "AND session_str IS NOT NULL"
                + (" AND id = ANY($2::bigint[])" if acc_ids else "")
                + " ORDER BY id",
                *([uid, acc_ids] if acc_ids else [uid]),
            )
            ids = [int(r["id"]) for r in (rows or [])]
            if not ids:
                return _err("Нет доступных активных аккаунтов", 400)
            params = {"account_ids": ids, "field": field, "value": value}
            total = len(ids)
            what = "каналов" if kind == "channels" else "профилей"
            label = f"Массовое изменение {what}: {field} ({total} акк.)"

        op_type = {"channels": "bulk_chan_exec", "bots": "bulk_bot_edit",
                   "profile": "bulk_update_profile"}[kind]
        try:
            from services import operation_bus
            op_id = await operation_bus.submit(
                pool, uid, op_type, params, total_items=total, label=label)
            return _json_resp({"ok": True, "op_id": op_id, "total": total, "field": field})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("_bulk_edit_op(%s) uid=%d", kind, uid)
            return _err(_INTERNAL_ERROR, 500)

    async def _alive_accounts(uid: int, wanted: list[int] | None = None) -> list[int]:
        return await alive_accounts(pool, uid, wanted)











    async def onboarding_status(request: web.Request) -> web.Response:
        """Чеклист новичка: от подключения аккаунта до ПЕРВОГО РЕЗУЛЬТАТА
        (services/onboarding.py). Скрывается, когда результат получен. Read-only,
        кроме разовых записей: выдача пробного периода и фиксация активации.
        Fail-open: при сбое — без чеклиста, но не блок домашнего экрана."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services import onboarding
            return _json_resp(await onboarding.load_checklist(pool, uid))
        except Exception:
            log.exception("onboarding_status uid=%s", uid)
            # fail-open: активированным считаем, чтобы не мешать (чеклист скрыт)
            return _json_resp({"activated": True, "steps": [], "done": 0,
                               "total": 0, "next_step": None})

















    async def dm_preview(request: web.Request) -> web.Response:
        """Как будет выглядеть сообщение у получателя.

        Рассылка в ЛС уходит живым людям и не отменяется, а автор до нажатия
        видел только исходный шаблон: что именно развернёт spintax и что
        подставится вместо {name}, было известно лишь после отправки.

        Рендер идёт ТЕМИ ЖЕ функциями и в ТОМ ЖЕ порядке, что и в исполнителе
        (dm_engine: personalize → expand_spintax). Иначе предпросмотр стал бы
        вторым источником правды и показывал бы не то, что уйдёт.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        text = validate_string(data.get("text"), max_len=4096)
        if not text:
            return _err("Введите текст сообщения", 400)
        name = validate_string(data.get("name"), max_len=64) or "Анна"
        try:
            from services.dm_engine import expand_spintax, personalize
        except Exception:  # pragma: no cover - импорт движка
            log.exception("dm_preview import uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        target = {"first_name": name, "username": name.lower()}
        # Показываем три варианта, а решаем «текст одинаковый для всех» по
        # восьми: на шаблоне из двух вариантов три совпавших броска выпадают в
        # четверти случаев, и предупреждение врало бы на здоровом тексте.
        # Восемь бросков снижают это примерно до одного случая из ста двадцати.
        _DRAWS = 8
        draws = []
        for _ in range(_DRAWS):
            try:
                draws.append(expand_spintax(personalize(text, target)))
            except Exception as exc:
                return _err(f"Шаблон не разворачивается: {exc}", 400)
        # Показать стараемся РАЗНЫЕ броски: три одинаковых из восьми разных
        # выглядели бы как отсутствие spintax.
        variants, seen = [], set()
        for d in draws:
            if d not in seen:
                seen.add(d)
                variants.append(d)
            if len(variants) == 3:
                break
        while len(variants) < 3 and draws:
            variants.append(draws[len(variants) % len(draws)])
        return _json_resp({
            "variants": variants,
            "distinct": len(set(draws)),
            "same_for_everyone": len(set(draws)) == 1,
            "personalized": "{" in text and personalize(text, target) != text,
            "name_used": name,
        })

    async def dm_adhoc_send(request: web.Request) -> web.Response:
        """Разовая рассылка в ЛС по списку @username (паритет с ботом)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)

        _cleaned = [u.lstrip("@") for u in _str_list(data.get("usernames"))]
        # Дедуп на входе: два одинаковых ЛС одному человеку — самая заметная
        # спам-сигнатура, и без этого возвращаемое число получателей врало бы.
        _seen: set[str] = set()
        _uniq: list[str] = []
        for _u in _cleaned:
            _k = _u.lower()
            if _k in _seen:
                continue
            _seen.add(_k)
            _uniq.append(_u)
        duplicates = len(_cleaned) - len(_uniq)
        # Потолок был молчаливым: вставив 2000 адресатов, пользователь получал
        # отправку по 1000 и ни слова об этом.
        _LIMIT = 1000
        truncated = max(0, len(_uniq) - _LIMIT)
        usernames = _uniq[:_LIMIT]
        if not usernames:
            return _err("Укажите хотя бы один @username", 400)
        text = validate_string(data.get("text"), max_len=4096)
        if not text:
            return _err("Введите текст сообщения", 400)
        if check_sql_suspicious(text):
            return _err("Недопустимые символы в тексте", 400)
        # Минимум 1с (не 0): «максимально быстро», но база остаётся ненулевой —
        # губернатор флота стретчит её МУЛЬТИПЛИКАТИВНО (base × mult), и при 0 он
        # уже ничего не затормозит. FloodWait/карантин тормозят независимо.
        delay = min(max(validate_integer(data.get("delay", 30), min_val=1, max_val=600) or 30, 1), 600)
        # Потолок сообщений с одного аккаунта за эту рассылку. Раньше его не
        # было вовсе: 1000 получателей на двух аккаунтах = по 500 ЛС с каждого.
        per_account_cap = None
        if data.get("per_account_cap") not in (None, "", 0):
            per_account_cap = min(max(
                validate_integer(data.get("per_account_cap"), min_val=1, max_val=500) or 1,
                1), 500)

        acc_ids = await _alive_accounts(
            uid, [int(x) for x in (data.get("account_ids") or []) if str(x).isdigit()])
        if not acc_ids:
            return _err("Нет доступных активных аккаунтов", 400)

        try:
            from services import operation_bus
            op_id = await operation_bus.submit(
                pool, uid, "bulk_dm_adhoc",
                {"account_ids": acc_ids, "usernames": usernames,
                 "text": text, "delay": delay,
                 "per_account_cap": per_account_cap},
                total_items=len(usernames),
                label=f"Рассылка ЛС · получателей: {len(usernames)} × акк.: {len(acc_ids)}",
            )
            return _json_resp({"ok": True, "op_id": op_id,
                               "recipients": len(usernames), "accounts": len(acc_ids),
                               "duplicates": duplicates, "truncated": truncated,
                               # Отправка последовательная: срок задаёт задержка,
                               # число аккаунтов его не сокращает.
                               "eta_seconds": int(len(usernames) * delay)})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("dm_adhoc_send uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def mass_targets(request: web.Request) -> web.Response:
        """Сколько адресатов получит массовое действие — ДО нажатия.

        Подтверждения этих действий спрашивали «во ВСЕ каналы этого аккаунта?»,
        «во ВСЕ группы?», «всем пользователям?» — и ни одно не называло числа.
        Масштаб становился известен только из тоста ПОСЛЕ запуска, когда посты
        уже ушли в Telegram, а сообщения — живым людям. Каждый запрос ниже
        скопирован с того места, где действие выбирает себе цели: иначе
        подтверждение обещало бы один набор, а ушло бы в другой.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        kind = (request.query.get("op") or "").strip()

        if kind == "bulk_post_chans":
            # Тот же набор, что channels_bulk_post без channel_ids.
            acc_id = validate_integer(request.query.get("acc_id"), min_val=1)
            if not acc_id:
                return _err("Выберите аккаунт", 400)
            total = await _safe_count(pool,
                "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1 AND acc_id=$2",
                uid, int(acc_id))
            rows = await _safe_fetch(pool,
                "SELECT channel_id, username, title FROM managed_channels "
                "WHERE owner_id=$1 AND acc_id=$2 "
                "ORDER BY member_count DESC NULLS LAST, channel_id DESC LIMIT 8",
                uid, int(acc_id))
        elif kind == "group_announce":
            # Тот же набор и та же таксономия типов, что в groups_announce.
            acc_id = validate_integer(request.query.get("acc_id"), min_val=1)
            if not acc_id:
                return _err("Выберите аккаунт", 400)
            total = await _safe_count(pool,
                "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1 AND acc_id=$2 "
                "AND type IN ('megagroup','supergroup','group','chat')",
                uid, int(acc_id))
            rows = await _safe_fetch(pool,
                "SELECT channel_id, username, title FROM managed_channels "
                "WHERE owner_id=$1 AND acc_id=$2 "
                "AND type IN ('megagroup','supergroup','group','chat') "
                "ORDER BY member_count DESC NULLS LAST, channel_id DESC LIMIT 8",
                uid, int(acc_id))
        elif kind == "join_all":
            # Тот же отбор, что invite_join_all, и тот же его потолок: без него
            # человек с тремя сотнями аккаунтов согласился бы на «всеми
            # активными», а вступили бы двести.
            total = await _safe_count(pool,
                "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active "
                "AND session_str IS NOT NULL", uid)
            return _json_resp({"ok": True, "op": kind, "total": int(total or 0),
                               "cap": 200, "sample": []})
        elif kind == "warmup_bulk":
            # Тот же отбор, что _warmup_bulk_core: активные, с сессией, не под
            # блокировкой и ещё БЕЗ активного плана. Общее число активных
            # аккаунтов здесь назвало бы набор заметно шире реального.
            total = await _safe_count(pool,
                """SELECT COUNT(*) FROM tg_accounts a
                   WHERE a.owner_id=$1 AND a.is_active=TRUE AND a.session_str IS NOT NULL
                     AND """ + _acc_status.sql_not_dead("a.acc_status") + """
                     AND NOT EXISTS (
                         SELECT 1 FROM account_warmup_plans wp
                         WHERE wp.account_id=a.id AND wp.status='active')""", uid)
            return _json_resp({"ok": True, "op": kind, "total": int(total or 0),
                               "cap": 500, "sample": []})
        elif kind == "admin_broadcast":
            # Тот же набор, что admin_broadcast: живым людям, кроме забаненных.
            if not _is_admin(uid):
                return _err("Раздел только для владельца платформы", 403)
            total = await _safe_count(pool,
                "SELECT COUNT(*) FROM platform_users "
                "WHERE COALESCE(is_banned, false) = false")
            rows = []
        else:
            return _err("Неизвестное массовое действие: " + kind[:40], 400)

        sample = [{"channel_id": int(r["channel_id"]) if r["channel_id"] is not None else None,
                   "username": r["username"], "title": r["title"]}
                  for r in (rows or [])]
        return _json_resp({"ok": True, "op": kind, "total": int(total or 0), "sample": sample})


    _REPORT_REASONS = {"spam", "violence", "porn", "drugs", "fake", "personal", "other"}

    async def mass_report_submit(request: web.Request) -> web.Response:
        """Массовая жалоба на цель (паритет с ботом).

        Самая баноопасная операция после Strike: жалоба идёт с реальных аккаунтов.
        Поэтому — гейт давления инфраструктуры перед постановкой (как у bulk_join),
        а карантин аккаунтов уважает сам исполнитель. В отличие от ботового пути,
        ставим через operation_bus (бот делает прямой INSERT — обход шины).
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)

        target = validate_string(data.get("target"), max_len=256)
        if not target:
            return _err("Укажите цель жалобы (@username или ссылку)", 400)
        reason = str(data.get("reason") or "spam")
        if reason not in _REPORT_REASONS:
            return _err(f"Недопустимая причина. Доступны: {', '.join(sorted(_REPORT_REASONS))}", 400)
        mode = "msg" if str(data.get("mode")) == "msg" else "peer"
        report_text = validate_string(data.get("report_text"), max_len=512) or ""
        msg_ids = [int(x) for x in (data.get("msg_ids") or []) if str(x).isdigit()][:50]

        acc_ids = await _alive_accounts(
            uid, [int(x) for x in (data.get("account_ids") or []) if str(x).isdigit()])
        if not acc_ids:
            return _err("Нет доступных активных аккаунтов", 400)

        try:
            from services import infra_orchestrator
            ready, reason_txt = await infra_orchestrator.is_ready_for_op(pool, uid, "mass_report")
            if not ready:
                return _err(f"Инфраструктура перегружена: {reason_txt}", 429)
        except Exception:
            log.warning("mass_report_submit: pressure check failed", exc_info=True)

        try:
            from services import operation_bus
            op_id = await operation_bus.submit(
                pool, uid, "mass_report",
                {"mode": mode, "target": target, "reason": reason,
                 "report_text": report_text, "msg_ids": msg_ids,
                 "account_ids": acc_ids},
                total_items=len(acc_ids),
                label=f"Жалобы: {target} [{reason}] × {len(acc_ids)} акк.",
            )
            return _json_resp({"ok": True, "op_id": op_id, "accounts": len(acc_ids)})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("mass_report_submit uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    # Эти четыре ручки ОСТАЮТСЯ в монолите, хотя их блоки уже вынесены
    # (mini_app_channels, mini_app_bots, mini_app_accounts). Они — тонкие
    # обёртки над ОБЩИМИ замыканиями `_bulk_edit_op` и `_import_all_op`:
    # одно ядро на каналы, ботов и профили аккаунтов, одно — на каналы и
    # группы. Увезти обёртку значит либо увезти ядро (а его зовут ручки из
    # трёх разных блоков), либо развести ядро по копиям — то есть завести
    # второй источник правды там, где массовая правка и так самая дорогая
    # ошибка. Поэтому они здесь намеренно, а не «недовынесены».
    async def channels_bulk_edit(request: web.Request) -> web.Response:
        return await _bulk_edit_op(request, "channels")

    async def bots_bulk_edit(request: web.Request) -> web.Response:
        return await _bulk_edit_op(request, "bots")

    async def accounts_bulk_profile(request: web.Request) -> web.Response:
        return await _bulk_edit_op(request, "profile")


    async def groups_announce(request: web.Request) -> web.Response:
        """Объявление во все группы аккаунта (паритет с ботом).

        Контракт совпадает с ботовым: {"acc_id", "text"}, total_items = число
        групп аккаунта. Группы считаем по той же таксономии типов, что и бот.
        Текст поддерживает spintax — исполнитель разворачивает его НА КАЖДУЮ
        группу отдельно (anti-detection: одинаковый текст в десятки групп —
        палевная сигнатура).
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)

        acc_id = validate_integer(data.get("acc_id"), min_val=1)
        if not acc_id:
            return _err("Выберите аккаунт", 400)
        text = validate_string(data.get("text"), max_len=4096)
        if not text:
            return _err("Введите текст объявления", 400)
        if check_sql_suspicious(text):
            return _err("Недопустимые символы в тексте", 400)

        acc = await _safe_fetchrow(
            pool,
            "SELECT id, is_active, session_str FROM tg_accounts WHERE id=$1 AND owner_id=$2",
            int(acc_id), uid,
        )
        if not acc:
            return _err("Аккаунт не найден", 404)
        if not acc["is_active"] or not acc["session_str"]:
            return _err("Аккаунт неактивен или без сессии — выберите другой", 400)

        # Честный total: если групп нет, операцию не ставим — иначе пользователь
        # увидел бы «запущено» и молчаливый ноль в итоге.
        cnt = await _safe_count(
            pool,
            "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1 AND acc_id=$2 "
            "AND type IN ('megagroup','supergroup','group','chat')",
            uid, int(acc_id),
        )
        if not cnt:
            return _err("У этого аккаунта нет групп — сначала импортируйте их", 400)

        try:
            from services import operation_bus
            op_id = await operation_bus.submit(
                pool, uid, "group_announce",
                {"acc_id": int(acc_id), "text": text},
                total_items=int(cnt),
                label=f"Объявление · групп: {cnt} · аккаунт #{acc_id}",
            )
            return _json_resp({"ok": True, "op_id": op_id, "groups": int(cnt)})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("groups_announce uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def channels_import_all(request: web.Request) -> web.Response:
        return await _import_all_op(request, "channels")

    async def groups_import_all(request: web.Request) -> web.Response:
        return await _import_all_op(request, "groups")





    # ── Persona Hub ───────────────────────────────────────────────────────────

    async def persona_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT pp.id, pp.persona_name, pp.bio, pp.age, pp.speech_style,
                          pp.tone, pp.niche, pp.is_active, pp.created_at, pp.interests,
                          ta.phone, ta.first_name, ta.username
                   FROM persona_profiles pp
                   LEFT JOIN tg_accounts ta ON ta.id=pp.account_id
                   WHERE pp.owner_id=$1 ORDER BY pp.created_at DESC""",
                uid,
            )
            return _json_resp({"personas": [
                {
                    **dict(r),
                    "interests": list(r.get("interests") or []),
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in rows
            ]})
        except Exception:
            log.exception("persona_list uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def persona_toggle(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            persona_id = int(request.match_info["persona_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор персоны", 400)
        try:
            row = await pool.fetchrow(
                "SELECT id, is_active FROM persona_profiles WHERE id=$1 AND owner_id=$2", persona_id, uid
            )
            if not row:
                return _err("Не найдено", 404)
            new_val = not row["is_active"]
            await pool.execute(
                "UPDATE persona_profiles SET is_active=$1 WHERE id=$2 AND owner_id=$3", new_val, persona_id, uid
            )
            return _json_resp({"is_active": new_val})
        except Exception:
            log.exception("persona_toggle uid=%d persona=%d", uid, persona_id)
            return _err(_INTERNAL_ERROR, 500)

    async def persona_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            persona_id = int(request.match_info["persona_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор персоны", 400)
        try:
            await pool.execute(
                "DELETE FROM persona_profiles WHERE id=$1 AND owner_id=$2", persona_id, uid
            )
            return _json_resp({"ok": True})
        except Exception:
            log.exception("persona_delete uid=%d persona=%d", uid, persona_id)
            return _err(_INTERNAL_ERROR, 500)

    async def persona_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        name = validate_string(body.get("persona_name"), max_len=100)
        if not name:
            return _err("Имя персоны обязательно", 400)
        VALID_STYLES = {"formal", "casual", "expert", "friendly", "sarcastic", "neutral"}
        speech_style = validate_string(body.get("speech_style"), max_len=20) or "casual"
        if speech_style not in VALID_STYLES:
            speech_style = "casual"
        try:
            interests_raw = body.get("interests", "")
            if isinstance(interests_raw, list):
                interests = [validate_string(i, max_len=50) or "" for i in interests_raw]
                interests = [i for i in interests if i]
            else:
                interests = [validate_string(t, max_len=50) or "" for t in str(interests_raw).split(",")]
                interests = [i for i in interests if i]
            age = validate_integer(body.get("age") or 25, min_val=18, max_val=80) or 25
            row = await pool.fetchrow(
                """INSERT INTO persona_profiles
                   (owner_id, persona_name, bio, age, interests, speech_style, tone, niche, backstory, is_active)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,TRUE)
                   RETURNING id, persona_name, is_active""",
                uid, name,
                (body.get("bio") or "").strip() or None,
                age,
                interests,
                speech_style,
                (body.get("tone") or "positive").strip(),
                (body.get("niche") or "").strip() or None,
                (body.get("backstory") or "").strip() or None,
            )
            return _json_resp({"ok": True, "persona": dict(row)})
        except Exception:
            log.exception("persona_create uid=%d", uid)
            return _err("Ошибка создания персоны", 500)

    # ── Auto Registrar ─────────────────────────────────────────────────────────

    async def autoreg_status(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            total = await pool.fetchval(
                "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1", uid
            )
            recent = await pool.fetch(
                """SELECT id, phone, first_name, acc_status, added_at
                   FROM tg_accounts WHERE owner_id=$1
                   ORDER BY added_at DESC LIMIT 10""",
                uid,
            )
            pending_ops = await pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND op_type='auto_register' AND status='pending'",
                uid,
            )
            return _json_resp({
                "total_accounts": total,
                "pending_registrations": pending_ops,
                "recent": [
                    {**dict(r), "added_at": r["added_at"].isoformat() if r["added_at"] else None}
                    for r in recent
                ],
            })
        except Exception:
            log.exception("autoreg_status uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def autoreg_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        try:
            count = max(1, min(50, validate_integer(body.get("count") or 1, min_val=1, max_val=50) or 1))
        except (TypeError, ValueError):
            return _err("Некорректное количество", 400)
        country = validate_string(body.get("country"), max_len=4) or "RU"
        # Проверяем, что SMS-сервис настроен (иначе операция гарантированно упадёт)
        try:
            from bot.handlers.auto_registrar import _get_sms_client
            _client, _service = await _get_sms_client(pool)
        except Exception:
            _client, _service = None, "?"
        if not _client:
            return _err(f"SMS-сервис не настроен ({_service}). Обратитесь к администратору платформы.", 400)
        try:
            op_id = await _obus.submit(
                pool, uid, "auto_register", {"count": count, "country": country},
                total_items=count, label=f"Авторег {count} акк. ({country})")
            return _json_resp({"ok": True, "op_id": op_id, "count": count})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("autoreg_submit uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    # ── Phone Checker ─────────────────────────────────────────────────────────

    _PHONES_RAW_MAX = 20000    # потолок размера вставки; режется ТОЛЬКО по разделителю
    _PHONES_BATCH = 500        # сколько номеров уходит в одну операцию проверки

    async def phone_check_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        raw = body.get("phones")
        raw = raw.strip() if isinstance(raw, str) else ""
        if not raw:
            return _err("Укажите номера телефонов", 400)
        # Раньше вставку резал validate_string(max_len=10000) — ПОСЕРЕДИНЕ номера:
        # обрубок «+7916123» проходил фильтр длины и уезжал на проверку как ДРУГОЙ
        # номер, то есть ответ приходил про чужой телефон. Режем только по границе
        # разделителя, а что не влезло — честно считаем.
        if len(raw) > _PHONES_RAW_MAX:
            head = raw[:_PHONES_RAW_MAX]
            edge = max(head.rfind("\n"), head.rfind(","))
            if edge <= 0:
                return _err("Номера идут без разделителей — поставьте запятую или "
                            "перевод строки", 400)
            raw = head[:edge]
        parts = [p.strip() for p in raw.replace(",", "\n").split("\n") if p.strip()]
        cleaned = [re.sub(r'[^\d+\-() ]', '', p) for p in parts]
        valid = [p for p in cleaned if len(p) >= 5]
        invalid = len(parts) - len(valid)
        # Дубль номера — это лишний запрос в Telegram по тому же человеку: платим
        # лимитом и риском дважды. Сравниваем по цифрам, а не по написанию.
        seen: set[str] = set()
        uniq: list[str] = []
        for ph in valid:
            key = re.sub(r"\D", "", ph)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(ph)
        duplicates = len(valid) - len(uniq)
        if not uniq:
            return _err("Нет валидных номеров", 400)
        phones = uniq[:_PHONES_BATCH]
        truncated = len(uniq) - len(phones)
        try:
            label = f"Проверка {len(phones)} номеров"
            op_id = await _obus.submit(
                pool, uid, "phone_check", {"phones": phones},
                total_items=len(phones), label=label)
            return _json_resp({"ok": True, "op_id": op_id, "label": label,
                               "count": len(phones), "total": len(uniq),
                               "truncated": truncated, "duplicates": duplicates,
                               "invalid": invalid})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("phone_check_submit uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    # ── Referral Dashboard ────────────────────────────────────────────────────

    async def referral_overview_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            ref_code_row = await pool.fetchrow(
                "SELECT code FROM platform_referral_codes WHERE user_id=$1", uid
            )
            amb_row = await pool.fetchrow(
                """SELECT a.tier_key, a.total_commission,
                          COALESCE(t.commission_pct, 0) AS commission_rate,
                          COALESCE(t.tier_name, 'Базовый') AS tier_name
                   FROM ambassador_status a
                   LEFT JOIN ambassador_tiers t ON t.tier_key = a.tier_key
                   WHERE a.user_id=$1""",
                uid,
            )
            referral_count = await pool.fetchval(
                "SELECT COUNT(*) FROM platform_referrals WHERE referrer_id=$1", uid
            )
            paid_count = await pool.fetchval(
                """SELECT COUNT(*) FROM platform_referrals r
                   WHERE r.referrer_id=$1 AND r.paid_at IS NOT NULL""",
                uid,
            )
            top_refs = await pool.fetch(
                """SELECT u.user_id AS id, u.username, r.created_at, r.activated_at,
                          r.paid_at
                   FROM platform_referrals r
                   JOIN platform_users u ON u.user_id=r.referred_id
                   WHERE r.referrer_id=$1
                   ORDER BY r.created_at DESC LIMIT 20""",
                uid,
            )
            # Голый код без ссылки бесполезен: непонятно, куда его вводить.
            # Бот («/referral») давно отдаёт t.me/<бот>?start=<код>, а мини-апп
            # показывал только сам код и писал «Поделитесь кодом!».
            code = ref_code_row["code"] if ref_code_row else None
            ref_link = None
            if code:
                bot_u = await _resolve_bot_username()
                if bot_u:
                    ref_link = f"https://t.me/{bot_u}?start={code}"
            return _json_resp({
                "ref_code": code,
                "ref_link": ref_link,
                "tier": amb_row["tier_key"] if amb_row else "basic",
                "tier_name": amb_row["tier_name"] if amb_row else "Базовый",
                "commission_rate": float(amb_row["commission_rate"] or 0) if amb_row else 0,
                "total_earned": float(amb_row["total_commission"] or 0) if amb_row else 0,
                "referral_count": int(referral_count or 0),
                "paid_count": int(paid_count or 0),
                "referrals": [
                    {
                        "id": r["id"], "username": r["username"],
                        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                        # Три разных состояния, а отдавали два. Экран ждал
                        # 'paid' и не получал его НИКОГДА: оплативший реферал
                        # показывался как «Зарегистрирован», хотя счётчик
                        # «Оплативших» рядом считал его по paid_at.
                        "status": ("paid" if r["paid_at"]
                                   else "active" if r["activated_at"] else "pending"),
                    }
                    for r in top_refs
                ],
            })
        except Exception:
            log.exception("referral_overview_detail uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    # ── AI Memory ─────────────────────────────────────────────────────────────




    # ── Nodes Hub (Forum Workspaces) ───────────────────────────────────────────

    async def nodes_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            nodes = await pool.fetch(
                """SELECT n.id, n.tg_chat_id, n.node_type, n.name, n.is_active, n.created_at,
                          COUNT(t.id) FILTER (WHERE t.status='open') AS open_threads,
                          COUNT(t.id) AS total_threads
                   FROM bm_telegram_nodes n
                   LEFT JOIN bm_node_threads t ON t.node_id=n.id
                   WHERE n.owner_id=$1
                   GROUP BY n.id ORDER BY n.created_at DESC""",
                uid,
            )
            return _json_resp({"nodes": [
                {**dict(n), "created_at": n["created_at"].isoformat() if n["created_at"] else None}
                for n in nodes
            ]})
        except Exception:
            log.exception("nodes_list uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def node_threads(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            node_id = int(request.match_info["node_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор узла", 400)
        try:
            node = await pool.fetchrow(
                "SELECT id, name FROM bm_telegram_nodes WHERE id=$1 AND owner_id=$2",
                node_id, uid,
            )
            if not node:
                return _err("Не найдено", 404)
            threads = await pool.fetch(
                """SELECT id, tg_thread_id, entity_type, entity_id, topic_name,
                          status, created_at
                   FROM bm_node_threads WHERE node_id=$1 ORDER BY status, created_at DESC""",
                node_id,
            )
            return _json_resp({
                "node": dict(node),
                "threads": [
                    {**dict(t), "created_at": t["created_at"].isoformat() if t["created_at"] else None}
                    for t in threads
                ],
            })
        except Exception:
            log.exception("node_threads uid=%d node=%d", uid, node_id)
            return _err(_INTERNAL_ERROR, 500)

    async def node_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        tg_chat_id_raw = data.get("tg_chat_id")
        node_type = str(data.get("node_type", "workspace")).strip()
        name = str(data.get("name", "")).strip()
        if not tg_chat_id_raw or not name:
            return _err("Укажите идентификатор чата и название", 400)
        if node_type not in ("proxies", "accounts", "tasks", "alerts", "workspace"):
            node_type = "workspace"
        try:
            tg_chat_id = int(str(tg_chat_id_raw).replace("-100", "-100").strip())
        except (ValueError, TypeError):
            return _err("Идентификатор чата должен быть числом (например -1001234567890)", 400)
        try:
            nid = await pool.fetchval(
                """INSERT INTO bm_telegram_nodes (owner_id, tg_chat_id, node_type, name)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (owner_id, tg_chat_id, node_type) DO UPDATE
                     SET name=EXCLUDED.name, is_active=TRUE
                   RETURNING id""",
                uid, tg_chat_id, node_type, name,
            )
            return _json_resp({"id": nid, "ok": True})
        except Exception:
            log.exception("node_create uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def node_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            node_id = int(request.match_info["node_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор узла", 400)
        try:
            await pool.execute(
                "DELETE FROM bm_telegram_nodes WHERE id=$1 AND owner_id=$2", node_id, uid
            )
            return _json_resp({"ok": True})
        except Exception:
            log.exception("node_delete uid=%d node=%d", uid, node_id)
            return _err(_INTERNAL_ERROR, 500)

    # ── Gift Transfer ──────────────────────────────────────────────────────────



    # ── Передача подарков ────────────────────────────────────────────────────
    #
    # Экран назывался «Перевод подарков» и показывал два числа: подсистема
    # (планы, позиции, отчёты, тип операции `gift_transfer` в реестре шины)
    # существовала целиком, но дойти до неё можно было только через бота.








    # ── Mass Inviter ───────────────────────────────────────────────────────────

    async def mass_inviter_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        group = (body.get("group") or "").strip()
        # Форма ссылки проверяется ЗДЕСЬ: иначе опечатка уезжает в очередь,
        # клеймит аккаунты, подключается ими и падает на резолве — прогон и
        # суточные лимиты сгорают на том, что видно на входе.
        from services.mass_inviter_engine import validate_group_ref as _vgr
        _gok, _gwhy = _vgr(group)
        if not _gok:
            return _err(_gwhy, 400)
        source = body.get("source", "parsed")
        if source not in ("parsed", "crm", "bot_users", "import_list", "segment"):
            return _err("Неизвестный источник аудитории", 400)
        account_ids = body.get("account_ids") or []
        if account_ids:
            req_ids = [int(x) for x in account_ids if str(x).isdigit()]
            # Фильтруем по владельцу (как в accounts_mass) — не полагаемся только
            # на повторный скоуп в executor.
            owned = await _safe_fetch(pool,
                "SELECT id FROM tg_accounts WHERE owner_id=$1 AND id = ANY($2::bigint[])",
                uid, req_ids)
            account_ids = [int(r["id"]) for r in (owned or [])]
            if not account_ids:
                return _err("Аккаунты не найдены", 404)
        # Свой список: разбираем вставленный текст на user_refs (@username/id) и
        # phones общими парсерами движка — раньше UI давал только 3 таблицы-источника.
        user_refs: list = []
        phones: list = []
        if source == "import_list":
            from services.mass_inviter_engine import (split_invite_targets,
                                                      parse_user_refs, parse_phones)
            # Список файлом (Mini App) присылает уже разобранные массивы — берём их
            # напрямую; иначе разбираем вставленный текст import_list.
            _ur_in = body.get("user_refs")
            _ph_in = body.get("phones")
            if isinstance(_ur_in, list) or isinstance(_ph_in, list):
                user_refs = parse_user_refs("\n".join(str(x) for x in (_ur_in or [])))
                phones = parse_phones("\n".join(str(x) for x in (_ph_in or [])))
            else:
                raw = body.get("import_list") or ""
                # Взаимоисключающий разбор: «+» ИЛИ 11+ цифр → телефон, иначе @username/ID.
                # split_invite_targets — единый источник правды для классификации.
                user_refs, phones = split_invite_targets(str(raw))
            if not user_refs and not phones:
                return _err("Список пуст или невалиден", 400)
        # Настройки безопасности/темпа.
        pace = (body.get("pace") or "normal").strip()
        # "auto" — темп считает движок по состоянию флота за сегодня, а не три
        # числа, выбранные вслепую (flood_engine.auto_strategy).
        if pace not in ("slow", "normal", "fast", "auto"):
            pace = "normal"
        def _clamp(v, lo, hi, d):
            try:
                return max(lo, min(hi, int(v)))
            except (TypeError, ValueError):
                return d
        batch_size = _clamp(body.get("batch_size"), 1, 20, 5) if body.get("batch_size") is not None else 5
        max_invites = _clamp(body.get("max_invites"), 0, 100000, 0) if body.get("max_invites") is not None else 0
        per_account_limit = _clamp(body.get("per_account_limit"), 0, 10000, 0) if body.get("per_account_limit") is not None else 0
        try:
            label = f"Массовый инвайт → {group}"
            params = {"group": group, "source": source, "pace": pace, "batch_size": batch_size}
            # Безопасный режим: governor уровня чата (частота/паузы/стоп на
            # мёртвом чате). Флаг независим от способа добавления.
            if str(body.get("safe_mode")).lower() in ("1", "true", "yes", "on"):
                params["safe_mode"] = True
            # «Мать-Дочка»: инвайт идёт в одноразовую дочернюю группу с редиректом
            # на боевую — риск бана инвайта поглощает расходник, а не боевой канал.
            if str(body.get("use_daughter_groups")).lower() in ("1", "true", "yes", "on"):
                params["use_daughter_groups"] = True
            # Витрина: буфер МЕЖДУ дочерней и боевым каналом. «Мать-Дочка» снимает
            # бан за инвайт, но приглашённые идут в мать по одной ссылке — канал
            # получает всплеск вступлений. Витрина копит их и пускает волнами по
            # ссылкам с лимитом мест. Осмысленна только вместе с дочерними
            # группами, поэтому исполнитель требует обе.
            if str(body.get("use_showcase")).lower() in ("1", "true", "yes", "on"):
                params["use_showcase"] = True
            # Переливание по резерву каналов: канал упёрся в лимит приглашённых
            # или поймал флуд — исполнитель берёт следующий пустой канал из
            # резерва, оформляет его так же (описание/пост) и приглашает туда
            # оставшихся. Резерв — только свои каналы с привязанным аккаунтом-
            # админом: в чужой канал оформить и пригласить нечем.
            from services import invite_overflow as _ovf
            _rids = _ovf.clean_reserve_ids(body.get("reserve_channels"))
            if _rids:
                _own = await _safe_fetch(pool,
                    "SELECT DISTINCT channel_id FROM managed_channels WHERE owner_id=$1 "
                    "AND channel_id = ANY($2::bigint[]) AND acc_id IS NOT NULL",
                    uid, _rids)
                _own_set = {int(r["channel_id"]) for r in (_own or [])}
                _rids = [c for c in _rids if c in _own_set]
                if _rids:
                    params["reserve_channels"] = _rids
            if body.get("per_channel_limit") is not None:
                _pcl = _clamp(body.get("per_channel_limit"), 0, 100000, 0)
                if _pcl:
                    params["per_channel_limit"] = _pcl
            _cs_in = body.get("channel_setup")
            _cs = _ovf.clean_setup(_cs_in)
            if _cs:
                _cs["apply_main"] = bool(_cs_in.get("apply_main", True))
                params["channel_setup"] = _cs
            # «Из парсера»: инвайтим ИМЕННО этот запуск (parse_run_id), симметрично
            # DM-мосту. Без него source=parsed брал всю аудиторию парсера.
            _pr = body.get("parse_run_id")
            if source == "parsed" and _pr:
                try:
                    params["parse_run_id"] = int(_pr)
                except (TypeError, ValueError):
                    pass
            # «Сегмент контактов»: единый движок сегментов как источник инвайта —
            # тот же таргетинг, что у рассылки (сохранённый срез или фильтры).
            if source == "segment":
                _ssid = body.get("saved_segment_id")
                if _ssid:
                    try:
                        params["saved_segment_id"] = int(_ssid)
                    except (TypeError, ValueError):
                        pass
                _sf = body.get("segment_filters")
                if isinstance(_sf, dict):
                    from services.contacts_hub.repository import _clean_filters
                    params["segment_filters"] = _clean_filters(_sf)
            # Фильтры аудитории (только с username / не бот / premium / активные) —
            # применяются в _exec_mass_invite теми же условиями, что парсер-вью.
            _af_in = body.get("aud_filters") or {}
            if isinstance(_af_in, dict):
                _af = {k: True for k in ("with_username", "not_bot", "premium", "active", "with_phone")
                       if str(_af_in.get(k)).lower() in ("1", "true", "yes", "on")}
                if _af:
                    params["aud_filters"] = _af
            if user_refs:
                params["user_refs"] = user_refs
            if phones:
                params["phones"] = phones
            if max_invites:
                params["max_invites"] = max_invites
            if per_account_limit:
                params["per_account_limit"] = per_account_limit
            # Режим «один проход»: закрыть аудиторию за один прогон до потолка ёмкости,
            # полагаясь на реальные сигналы флуда, а не на предсказанный суточный лимит.
            # Осознанно повышает риск — фронт подтверждает отдельно.
            if bool(body.get("one_pass")):
                params["one_pass"] = True
            # Паритет с ботом: способ инвайта (direct/admin/link) и режим объёма.
            _im = str(body.get("invite_method") or "direct").strip().lower()
            if _im not in ("direct", "admin", "link"):
                _im = "direct"
            params["invite_method"] = _im
            # Финальный фолбэк «недостижимым — ссылка в ЛС»: включён в исполнителе
            # по умолчанию (как и promote_trick) — отключить явным False.
            # Неприменим при invite_method="link" (там и так все получают ссылку) —
            # исполнитель сам это учитывает, здесь просто пробрасываем флаг.
            if body.get("link_fallback") is False:
                params["link_fallback"] = False
            # Текст сообщения нужен и методу "link", и фолбэку — оба шлют ссылку в
            # ЛС тем же текстом. Раньше поле пробрасывалось ТОЛЬКО для метода
            # "link" — фолбэк для direct/admin слал бы дефолтный текст всем разом.
            if body.get("link_message") and (
                    _im == "link" or body.get("link_fallback") is not False):
                params["link_message"] = str(body.get("link_message"))[:500]
            # Режим объёма на аккаунт: "progressive" — по возрасту/доверию аккаунта.
            if str(body.get("volume_mode") or "").strip().lower() == "progressive":
                params["volume_mode"] = "progressive"
            # Повторный инвайт уже приглашённым. Исполнитель этот флаг читает и
            # прямо советует его в итоге («или отключите дедуп»), но передать его
            # было неоткуда: ни одна поверхность не давала контрола. Совет,
            # указывающий на несуществующую настройку, хуже отсутствия совета.
            if body.get("skip_invited") is False:
                params["skip_invited"] = False
            # Автовыдача админки инвайтерам и промоут-трюк включены в исполнителе
            # по умолчанию и МЕНЯЮТ состав админов чата: первая делает админами
            # аккаунты оператора, второй на секунды выдаёт админку постороннему.
            # Отключить их было нечем — теперь можно.
            if body.get("auto_promote") is False:
                params["auto_promote"] = False
            if body.get("promote_trick") is False:
                params["promote_trick"] = False
            # Пакетное добавление одним запросом — обкатка на части флота.
            # Явный True/False; отсутствие ключа = как решит окружение.
            if body.get("bulk_api") is not None:
                params["bulk_api"] = bool(body.get("bulk_api"))
            # Принудительно взять РИСКОВЫЕ аккаунты («если очень нужно»): снимает
            # мягкие гейты кулдауна и карантина риск-пульса. Осознанный выбор
            # владельца — по умолчанию OFF (безопасно). Банённые/spamblock всё
            # равно не берутся: их форсить бессмысленно.
            if body.get("include_risky") is True:
                params["include_risky"] = True
            if account_ids:
                params["account_ids"] = account_ids
            # Шов «Инвайт → Welcome»: приветствие вступившим одной транзакцией.
            # A/B welcome: welcome_variants[] (2–4) делятся по вступившим (ab_engine).
            _wt = validate_string(body.get("welcome_message"), max_len=2048)
            from services import ab_engine as _ab
            _wvars = _ab.clean_variants(body.get("welcome_variants"))
            _wvars = [v for v in _wvars if not check_sql_suspicious(v)]
            if _wvars and len(_wvars) >= 2:
                params["welcome"] = {"variants": _wvars,
                                     "delay": _clamp(body.get("welcome_delay"), 5, 600, 45)}
            elif _wt and not check_sql_suspicious(_wt):
                params["welcome"] = {"text": _wt,
                                     "delay": _clamp(body.get("welcome_delay"), 5, 600, 45)}
            # operation_bus.submit() — не сырой INSERT: раньше здесь молча
            # обходились план-гейт (mass_invite требует "pro"), дедуп повторной
            # постановки и предохранитель Ban Weather (при шторме банов по этому
            # op_type новые прогоны блокируются). except PermissionError ниже уже
            # был написан в расчёте на PlanRequiredError от submit(), но раньше
            # submit() здесь не вызывался — ветка была мертва.

            total_items = (len(user_refs) + len(phones)) if (user_refs or phones) else 1
            op_id = await _obus.submit(
                pool, uid, "mass_invite", params,
                total_items=total_items, label=label,
            )
            return _json_resp({"ok": True, "op_id": op_id, "label": label})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("mass_inviter_submit uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)









    # ── Ghost Engine ───────────────────────────────────────────────────────────








    # ── Bot Webhook ────────────────────────────────────────────────────────────



    # ── Reg Checker (Registration Date) ───────────────────────────────────────

    async def reg_check_history(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT entity_id, entity_type, entity_name, username,
                          reg_date, method, checked_at
                   FROM reg_check_cache WHERE checked_by=$1
                   ORDER BY checked_at DESC, entity_id DESC, entity_type
                   LIMIT $2 OFFSET $3""",
                uid, _list_limit(request, 30, 1000), _list_offset(request),
            )
            total = await _safe_count(pool,
                "SELECT COUNT(*) FROM reg_check_cache WHERE checked_by=$1", uid)
            return _json_resp({"total": total, "checks": [
                {
                    **dict(r),
                    "reg_date": r["reg_date"].isoformat() if r["reg_date"] else None,
                    "checked_at": r["checked_at"].isoformat() if r["checked_at"] else None,
                }
                for r in rows
            ]})
        except Exception:
            log.exception("reg_check_history uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def reg_check_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        target = (body.get("target") or "").strip()
        if not target:
            return _err("Укажите цель проверки", 400)
        try:
            label = f"Проверка даты регистрации: {target}"
            op_id = await _obus.submit(
                pool, uid, "reg_check", {"target": target}, total_items=1, label=label)
            return _json_resp({"ok": True, "op_id": op_id, "label": label})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("reg_check_submit uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    # ── DM Campaigns ───────────────────────────────────────────────────────────

    async def dm_campaigns_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT id, name, status, sent_count, fail_count, total_targets,
                          text_template, target_type, params, created_at
                   FROM dm_campaigns WHERE owner_id=$1
                   ORDER BY created_at DESC LIMIT 50""",
                uid,
            )
            total = await _safe_count(pool,
                "SELECT COUNT(*) FROM dm_campaigns WHERE owner_id=$1", uid)
            # Срок считаем на сервере тем же движком, что задаёт паузы между
            # отправками: раньше на экране было только «0/5000 отправлено», и
            # пользователь не знал, что это недели, а не часы.
            from services.dm_engine import estimate_duration_seconds as _eta
            out = []
            for r in rows:
                d = dict(r)
                _p = d.get("params") or {}
                if isinstance(_p, str):
                    try:
                        _p = _json.loads(_p)
                    except Exception:
                        _p = {}
                d["params"] = _p if isinstance(_p, dict) else {}
                d["pace"] = d["params"].get("pace") or "normal"
                _remaining = max(
                    0,
                    int(d.get("total_targets") or 0)
                    - int(d.get("sent_count") or 0)
                    - int(d.get("fail_count") or 0),
                )
                d["remaining"] = _remaining
                d["eta_seconds"] = _eta(d.get("target_type"), d["pace"], _remaining)
                d["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
                out.append(d)
            return _json_resp({"campaigns": out, "total": int(total or 0)})
        except Exception:
            log.exception("dm_campaigns_list uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def dm_campaign_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        name = _text(body.get("name"))
        text = _text(body.get("text"))
        target_type = body.get("target_type", "bot_users")
        target_id = body.get("target_id")
        if not name or not text:
            return _err("Заполните название и текст", 400)
        # Полный список типов таргета, поддержанных dm_engine._get_targets.
        # 'crm' читает ЛЕГАСИ-таблицу crm_contacts, которую не показывает ни один
        # экран — оставлен рабочим только ради уже созданных кампаний. Новые
        # кампании целятся в 'contacts' (то, что пользователь видит в «Контактах»).
        _ALLOWED_TARGETS = {
            "all_bots", "bot_users", "cohort", "crm", "contacts", "segment",
            "parsed_audience", "import_list",
        }
        if target_type not in _ALLOWED_TARGETS:
            return _err(f"target_type должен быть одним из: {', '.join(sorted(_ALLOWED_TARGETS))}", 400)
        # Сегмент CRM как аудитория: проверяем владение ДО создания кампании —
        # иначе кампания создастся и молча уйдёт в пустоту на этапе резолва.
        _segment_filters = None
        if target_type == "segment":
            if not target_id:
                return _err("Для сегмента выберите сохранённый сегмент", 400)
            try:
                _seg_id = int(target_id)
            except (TypeError, ValueError):
                return _err("Неверный идентификатор цели", 400)
            try:
                from services.contacts_hub import repository as _repo
                _segment_filters = await _repo.get_segment_filters(pool, uid, _seg_id)
            except Exception:
                log.exception("dm campaign: segment lookup uid=%d seg=%s", uid, target_id)
                return _err("Не удалось прочитать сегмент", 500)
            if _segment_filters is None:
                return _err("Сегмент не найден", 404)
        # IDOR-защита: при таргете на конкретного бота проверяем владение.
        if target_type in ("bot_users", "cohort") and target_id:
            try:
                _bid = int(target_id)
            except (TypeError, ValueError):
                return _err("Неверный идентификатор цели", 400)
            owns_bot = await _safe_count(pool,
                "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", _bid, uid)
            if not owns_bot:
                return _err("Бот не найден", 404)
        if target_type == "cohort" and not target_id:
            return _err("Для когорты выберите бота", 400)
        # Когорта по активности (совпадает с dm_engine._get_targets).
        _COHORT_SQL = {
            "hot":  "ua.last_seen >= now() - INTERVAL '1 day'",
            "warm": "ua.last_seen >= now() - INTERVAL '7 days' AND ua.last_seen < now() - INTERVAL '1 day'",
            "cold": "ua.last_seen >= now() - INTERVAL '30 days' AND ua.last_seen < now() - INTERVAL '7 days'",
            "lost": "ua.last_seen < now() - INTERVAL '30 days'",
        }
        cohort_type = None
        if target_type == "cohort":
            cohort_type = (body.get("cohort_type") or "warm").strip()
            if cohort_type not in _COHORT_SQL:
                cohort_type = "warm"
        # import_list: свой список получателей (user_id / @username), разбор общим
        # хелпером dm_engine — раньше этот тип таргета движок умел, но UI не давал.
        import_list = []
        if target_type == "import_list":
            from services.dm_engine import parse_import_list
            import_list = parse_import_list(body.get("import_list"))
            if not import_list:
                return _err("Список получателей пуст или невалиден", 400)
        # Calculate total_targets depending on type
        total_targets = 0
        try:
            if target_type == "bot_users" and target_id:
                total_targets = await _safe_count(pool,
                    "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=true "
                    "AND is_blocked=false AND suspect=false", int(target_id))
            elif target_type == "all_bots":
                total_targets = await _safe_count(pool,
                    """SELECT COUNT(DISTINCT bu.user_id) FROM bot_users bu
                       JOIN managed_bots mb ON mb.bot_id=bu.bot_id
                       WHERE mb.added_by=$1 AND bu.is_active=true
                         AND bu.is_blocked=false AND bu.suspect=false""", uid)
            elif target_type == "cohort" and target_id:
                total_targets = await _safe_count(pool,
                    f"""SELECT COUNT(*) FROM user_activity ua
                        JOIN managed_bots mb ON mb.bot_id=ua.bot_id AND mb.added_by=$2
                        WHERE ua.bot_id=$1 AND {_COHORT_SQL[cohort_type]}""",
                    int(target_id), uid)
            elif target_type == "crm":
                total_targets = await _safe_count(pool,
                    "SELECT COUNT(*) FROM crm_contacts WHERE owner_id=$1 AND tg_user_id > 0", uid)
            elif target_type == "segment" and _segment_filters is not None:
                from services.contacts_hub import repository as _repo
                total_targets = await _repo.count_segment(pool, uid, _segment_filters)
            elif target_type == "contacts":
                # Тем же счётчиком, что показывает экран «Контакты», — иначе
                # обещанное число получателей разошлось бы с видимым списком.
                from services.contacts_hub import repository as _repo
                total_targets = await _repo.count_segment(pool, uid, {})
            elif target_type == "parsed_audience":
                if target_id:
                    total_targets = await _safe_count(pool,
                        "SELECT COUNT(*) FROM parsed_audiences WHERE owner_id=$1 AND parse_run_id=$2 AND tg_user_id > 0",
                        uid, int(target_id))
                else:
                    total_targets = await _safe_count(pool,
                        "SELECT COUNT(*) FROM parsed_audiences WHERE owner_id=$1 AND tg_user_id > 0", uid)
            elif target_type == "import_list":
                total_targets = len(import_list)
        except Exception:
            total_targets = 0
        # Темп рассылки (params.pace): slow безопаснее, fast быстрее (риск бана),
        # max — максимально быстро (минимум пауз, предохранители флуда/карантина
        # не отключаются), auto — движок считает темп по состоянию флота за
        # сегодня (flood_engine), паритет с масс-инвайтом.
        pace = (body.get("pace") or "normal").strip()
        if pace not in ("slow", "normal", "fast", "max", "auto"):
            pace = "normal"
        _params = {"pace": pace}
        # Тихие часы: по умолчанию включены (ночная рассылка живым людям — низкий
        # отклик и жалобы на спам). Пишем в params только явное отключение,
        # чтобы движок с его умолчанием остался источником правды.
        if body.get("quiet_hours") is False:
            _params["quiet_hours"] = False
        # Чем заменить {name}, если у получателя нет ни имени, ни ника. Движок
        # читал этот параметр, но задать его было нечем — настройка без ввода.
        _nf = (body.get("name_fallback") or "").strip()
        if _nf:
            _params["name_fallback"] = _nf[:32]
        if cohort_type:
            _params["cohort_type"] = cohort_type
        if import_list:
            _params["import_list"] = import_list
        # Медиа (фото/видео/док по URL) — send_dm отправит как файл с подписью.
        media_url = (body.get("media_url") or "").strip()
        if media_url:
            if not is_safe_public_url(media_url):
                return _err("Нужен публичный https URL медиа", 400)
            _params["media_url"] = media_url
        # Дневной лимит отправок на аккаунт (защита от бана). Клампим 1..200.
        if body.get("per_account_daily") is not None:
            try:
                _pad = int(body["per_account_daily"])
                if _pad > 0:
                    _params["per_account_daily"] = max(1, min(200, _pad))
            except (TypeError, ValueError):
                return _err("Дневной лимит на аккаунт должен быть числом", 400)
        params_json = _json.dumps(_params)
        try:
            try:
                row = await pool.fetchrow(
                    """INSERT INTO dm_campaigns(owner_id, name, text_template, target_type, target_id, total_targets, params)
                       VALUES($1,$2,$3,$4,$5,$6,$7::jsonb) RETURNING id""",
                    uid, name, text, target_type, int(target_id) if target_id else None, total_targets, params_json,
                )
            except asyncpg.UndefinedColumnError:
                row = await pool.fetchrow(
                    """INSERT INTO dm_campaigns(owner_id, name, text_template, target_type, target_id, total_targets)
                       VALUES($1,$2,$3,$4,$5,$6) RETURNING id""",
                    uid, name, text, target_type, int(target_id) if target_id else None, total_targets,
                )
            return _json_resp({"id": row["id"], "total_targets": total_targets})
        except Exception:
            log.exception("dm_campaign_create uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)







    # ── Account Warmup ─────────────────────────────────────────────────────────



    # ── Восстановление аккаунтов (авто-реабилитация) ───────────────────────────
    # Разрыв: модуль работал сутками и был невидим. Аккаунт со спам-блоком
    # пропадал из операций, фоновый цикл его вытягивал (или сдавался и ставил
    # в очередь «нужен ручной разбор»), а в интерфейсе не было ни экрана, ни
    # строчки — при том что подпись раздела обещала «восстановление».

    # ── Общие папки (Shared Folders) как канал инвайтинга ──────────────────────





    # ── Virtual Layer: обзор состояний и виртуальных событий ──────────────

    async def vlayer_overview(request: web.Request) -> web.Response:
        """Сводка вычислительного слоя: распределение воронки, «горячие»
        сущности и лента виртуальных событий (что слой понял о поведении)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services import virtual_layer as _vl
        from services.organism import spine as _spine
        try:
            ov = await _vl.overview(pool, uid)
        except Exception:
            log.exception("vlayer_overview uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        events = []
        try:
            raw = await _spine.recent_events(
                pool, uid, kinds=_vl.VIRTUAL_EVENT_KINDS, hours=168, limit=40)
            for e in raw:
                p = e.get("payload") or {}
                events.append({
                    "kind": e["kind"],
                    "label": _vl.EVENT_LABEL.get(e["kind"], e["kind"]),
                    "entity_type": p.get("entity_type"),
                    "entity_id": p.get("entity_id"),
                    "at": e["at"].isoformat() if e.get("at") else None,
                })
            # Лента говорила «🔥 Намерение купить · #<uuid>» — по такой строке
            # не понять, о ком речь. Имена берём одной пачкой на весь список.
            names = await _vl.resolve_names(pool, uid, events)
            for e in events:
                name = names.get(str(e.get("entity_id")))
                if name:
                    e["name"] = name
        except Exception:
            log.debug("vlayer_overview events failed uid=%s", uid)
        cooling = ov.get("cooling") or []
        for c in cooling:
            if c.get("expires_at") is not None:
                try:
                    c["expires_at"] = c["expires_at"].isoformat()
                except (AttributeError, ValueError):
                    c["expires_at"] = None
        return _json_resp({"ok": True, "funnel": ov["funnel"], "hot": ov["hot"],
                           "ladder": ov.get("ladder") or [], "cooling": cooling,
                           "total": ov["total"], "audience": ov.get("audience"),
                           "bots": ov.get("bots") or [], "events": events,
                           "bouncing": ov.get("bouncing") or []})

    async def vlayer_mark_purchased(request: web.Request) -> web.Response:
        """Владелец отмечает «купил» прямо из модели поведения.

        Оплата часто проходит мимо системы (перевод, наличные), и тогда
        модель продолжала считать покупателя «ходит по кругу» и напоминать о
        нём каждые несколько часов. Заодно ставим стадию CRM «Выиграно»,
        чтобы карточка контакта и модель говорили одно и то же.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services import virtual_layer as _vl
        etype = (request.match_info.get("entity_type") or "").strip()
        eid = (request.match_info.get("entity_id") or "").strip()
        if etype != _vl.USER:
            return _err("Отметить покупку можно только у человека", 400)
        if not eid or len(eid) > 128:
            return _err("Неверный идентификатор", 400)
        # Чужой или несуществующий контакт не трогаем: id приходит из адреса.
        try:
            known = await pool.fetchval(
                "SELECT 1 FROM unified_contacts WHERE owner_id=$1 AND id::text=$2",
                uid, eid)
        except Exception:
            known = None
        if not known:
            return _err("Контакт не найден", 404)
        try:
            await _vl.mark_purchased(pool, uid, _vl.USER, eid, source="owner")
        except Exception:
            log.exception("vlayer_mark_purchased uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        try:
            from services.contacts_hub.crm_engine import upsert_crm
            await upsert_crm(pool, uid, eid, {"stage": "won"})
        except Exception:
            log.debug("vlayer_mark_purchased crm failed uid=%s", uid, exc_info=True)
        return _json_resp({"ok": True})

    async def vlayer_entity(request: web.Request) -> web.Response:
        """История переходов одной сущности: «заходил и уходил» видно только так.

        Текущее значение отвечает «где человек сейчас», но не «как он сюда
        пришёл». Третий заход по кругу и первый подъём выглядят одинаково, а
        дожимать их надо по-разному.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services import virtual_layer as _vl
        etype = (request.match_info.get("entity_type") or "").strip()
        eid = (request.match_info.get("entity_id") or "").strip()
        if etype not in (_vl.USER, _vl.BOT, _vl.CAMPAIGN, _vl.NETWORK,
                         _vl.CONVERSATION):
            return _err("Неизвестный тип сущности", 400)
        if not eid or len(eid) > 128:
            return _err("Неверный идентификатор", 400)
        try:
            data = await _vl.entity_history(pool, uid, etype, eid)
        except Exception:
            log.exception("vlayer_entity uid=%s type=%s", uid, etype)
            return _err(_INTERNAL_ERROR, 500)
        data["transitions"] = [
            {**t, "at": t["at"].isoformat() if t.get("at") else None}
            for t in data["transitions"]]
        # current.expires_at — datetime (распад-таймер карточки): сериализуем в ISO,
        # иначе _json_resp упал бы на нём и экран остался без состояния.
        _cur = data.get("current")
        if isinstance(_cur, dict) and _cur.get("expires_at") is not None:
            try:
                _cur["expires_at"] = _cur["expires_at"].isoformat()
            except (AttributeError, ValueError):
                _cur["expires_at"] = None
        if not data["transitions"] and not data["current"]:
            return _err("По этой сущности слой ещё ничего не знает", 404)
        return _json_resp({"ok": True, **data})

    async def vlayer_segment(request: web.Request) -> web.Response:
        """Список людей на одной ступени воронки — чтобы число на экране
        открывалось списком тех самых людей, а не оставалось числом."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services import virtual_layer as _vl
        value = (request.match_info.get("value") or "").strip()
        if value not in (_vl.LADDER + [_vl.LOST]):
            return _err("Неизвестная ступень", 400)
        try:
            data = await _vl.segment(pool, uid, value)
        except Exception:
            log.exception("vlayer_segment uid=%s value=%s", uid, value)
            return _err(_INTERNAL_ERROR, 500)
        for p in data.get("people", []):
            if p.get("expires_at") is not None:
                try:
                    p["expires_at"] = p["expires_at"].isoformat()
                except (AttributeError, ValueError):
                    p["expires_at"] = None
        return _json_resp({"ok": True, **data})

    async def vlayer_analytics(request: web.Request) -> web.Response:
        """Тренды слоя: конверсия воронки, моментум 7д и лучшие источники.

        Не «где аудитория сейчас» (это overview), а КАК она движется: сколько
        интереса доходит до «готов купить», греемся мы или остываем за неделю,
        какой бот/источник довёл больше людей до верхних ступеней. Read-only.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services import virtual_layer as _vl
        try:
            return _json_resp({"ok": True, **await _vl.analytics(pool, uid)})
        except Exception:
            log.exception("vlayer_analytics uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    # ── «Нотариус»: заверение рекламных размещений ────────────────────────














    async def rehab_overview_ep(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services import account_rehab as _rh

        try:
            counts = await _rh.rehab_overview(pool, uid)
        except Exception:
            log.warning("rehab_overview uid=%d", uid, exc_info=True)
            counts = {"in_progress": 0, "freed": 0, "stuck": 0, "gone": 0, "by_phase": {}}
        lim = _list_limit(request, 100, 500)
        off = _list_offset(request)
        accounts = await _rh.list_for_owner(pool, uid, limit=lim, offset=off)
        # Итог берём из той же сводки, что и плитки сверху — лишнего запроса не
        # нужно: `by_phase` считает ВСЕ строки реабилитации. Если сводка не
        # получилась, итог не меньше того, что уже на экране.
        total = max(sum(int(v) for v in (counts.get("by_phase") or {}).values()),
                    off + len(accounts))
        return _json_resp({"ok": True, **counts, "accounts": accounts,
                           "total": total,
                           "page": {"limit": lim, "offset": off,
                                    "has_more": off + len(accounts) < total}})

    async def rehab_action(request: web.Request) -> web.Response:
        """Ручное управление восстановлением одного аккаунта.

        `now` — не ждать бэк-офф (он доходит до 72 часов, а человек может уже
        знать, что блок снят); `off` — не тратить действия флота на аккаунт,
        который решено заменить; `restart` — второй шанс тому, что автоматика
        отдала на ручной разбор.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор аккаунта", 400)
        action = (request.match_info.get("action") or "").lower()
        from services import account_rehab as _rh

        if action == "now":
            ok = await _rh.force_now(pool, uid, acc_id)
            if not ok:
                return _err("Аккаунт не в активной фазе восстановления", 404)
            return _json_resp({"ok": True, "note": "Следующий шаг — в ближайшем цикле"})
        if action in ("off", "restart"):
            ok = await _rh.set_enabled(pool, uid, acc_id, action == "restart")
            if not ok:
                return _err("Аккаунт не найден в восстановлении", 404)
            return _json_resp({"ok": True,
                               "phase": "appeal" if action == "restart" else "off"})
        return _err("Неизвестное действие", 400)







    # ── A/B Experiments ────────────────────────────────────────────────────────









    # ── Health Dashboard ───────────────────────────────────────────────────────

    async def health_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            stats = await pool.fetchrow(
                """SELECT
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE acc_status='active') AS active,
                   COUNT(*) FILTER (WHERE acc_status IN ('banned','deactivated')) AS banned,
                   COUNT(*) FILTER (WHERE cooldown_until > now()) AS cooling,
                   COUNT(*) FILTER (WHERE trust_score IS NOT NULL AND trust_score < 0.4) AS low_trust
                   FROM tg_accounts WHERE owner_id=$1""",
                uid,
            )
            flood_7d = await pool.fetchval(
                """SELECT COUNT(*) FROM account_flood_log afl
                   JOIN tg_accounts ta ON ta.id=afl.account_id
                   WHERE ta.owner_id=$1 AND afl.created_at > now()-interval '7 days'""",
                uid,
            )
            warmup_active = await pool.fetchval(
                "SELECT COUNT(*) FROM account_warmup_plans WHERE owner_id=$1 AND status='active'", uid
            )
            # Recent flood events.
            # Со строки флуд-события владелец идёт РАЗБИРАТЬ ПРИЧИНУ — а причина
            # (что за операция флудила, её статус и ошибка) живёт в карточке
            # ОПЕРАЦИИ, не аккаунта. Раньше строка вела в openAccount — тупик по
            # сути жалобы («переводит в карточку аккаунта вместо операции с
            # возможностью исправить причину ошибки»). Поэтому отдаём:
            #   • operation_id + op_exists — чтобы фронт открыл карточку операции
            #     (openOpDetail), а в аккаунт падал только когда операции уже нет
            #     (retention вычистил operation_queue) или флуд писался без неё;
            #   • человекочитаемое имя операции (label/op_type вместо сырого
            #     action_type) и её статус — на экране было беднее некуда.
            # LEFT JOIN скоупим по владельцу: чужую операцию показывать нельзя.
            events = await pool.fetch(
                """SELECT ta.id AS account_id, afl.operation_id,
                          (oq.id IS NOT NULL) AS op_exists,
                          COALESCE(oq.label, oq.op_type, afl.operation) AS operation,
                          oq.status AS op_status,
                          afl.flood_seconds, afl.created_at, ta.phone, ta.first_name
                   FROM account_flood_log afl
                   JOIN tg_accounts ta ON ta.id=afl.account_id
                   LEFT JOIN operation_queue oq
                          ON oq.id = afl.operation_id AND oq.owner_id = ta.owner_id
                   WHERE ta.owner_id=$1
                   ORDER BY afl.created_at DESC LIMIT 10""",
                uid,
            )
            events_total = await pool.fetchval(
                """SELECT COUNT(*) FROM account_flood_log afl
                   JOIN tg_accounts ta ON ta.id=afl.account_id
                   WHERE ta.owner_id=$1""",
                uid,
            )
            return _json_resp({
                "total": stats["total"], "active": stats["active"],
                "banned": stats["banned"], "cooling": stats["cooling"],
                "low_trust": stats["low_trust"], "flood_7d": flood_7d,
                "warmup_active": warmup_active,
                "events_total": events_total or 0,
                "events": [
                    {**dict(e), "created_at": e["created_at"].isoformat() if e["created_at"] else None}
                    for e in events
                ],
            })
        except Exception:
            log.exception("health_overview uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    # ── Account Shield ─────────────────────────────────────────────────────────

    async def shield_config_save(request: web.Request) -> web.Response:
        """Сохранить настройки щита. Писателя у shield_configs в мини-аппе не
        было вовсе: экран показывал пороги, менять их было нечем."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        fields = {}
        for key in ("risk_threshold", "ban_prob_threshold"):
            if key in data and data[key] is not None:
                try:
                    v = float(data[key])
                except (TypeError, ValueError):
                    return _err(f"Некорректное значение: {key}")
                if not 0.0 <= v <= 1.0:
                    return _err("Порог должен быть от 0 до 1")
                fields[key] = v
        if "cool_duration_hours" in data and data["cool_duration_hours"] is not None:
            hours = validate_integer(data["cool_duration_hours"], min_val=1, max_val=168)
            if hours is None:
                return _err("Пауза — от 1 до 168 часов")
            fields["cool_duration_hours"] = hours
        for key in ("auto_pause", "notify_admin"):
            if key in data and data[key] is not None:
                fields[key] = bool(data[key])
        if not fields:
            return _err("Нечего сохранять")
        try:
            from services.account_shield import save_shield_config
            cfg = await save_shield_config(pool, uid, **fields)
        except Exception:
            log.exception("shield_config_save uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True, "config": {
            "risk_threshold": cfg.risk_threshold,
            "ban_prob_threshold": cfg.ban_prob_threshold,
            "auto_pause": cfg.auto_pause,
            "notify_admin": cfg.notify_admin,
            "cool_duration_hours": cfg.cool_duration_hours,
        }})

    async def shield_summary(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            row = await pool.fetchrow(
                """SELECT
                   COUNT(*) FILTER (WHERE a.is_active = TRUE) AS total_active,
                   COUNT(*) FILTER (WHERE a.is_active = FALSE AND a.cooldown_until > NOW()) AS cooling,
                   COUNT(*) FILTER (WHERE r.risk_score >= 0.7) AS threatened,
                   COUNT(*) FILTER (WHERE r.ban_probability >= 0.5) AS high_ban,
                   COUNT(*) FILTER (WHERE r.risk_score < 0.7 AND (r.ban_probability < 0.5 OR r.ban_probability IS NULL)) AS ok_count
                   FROM tg_accounts a
                   LEFT JOIN account_risk_scores r ON r.account_id = a.id
                   WHERE a.owner_id = $1""",
                uid,
            )
            # account_id — чтобы строка истории открывала карточку аккаунта.
            history = await pool.fetch(
                """SELECT a.id AS account_id, sa.action, sa.risk_score,
                          sa.ban_probability, sa.created_at, a.phone, a.first_name
                   FROM shield_actions sa
                   JOIN tg_accounts a ON a.id = sa.account_id
                   WHERE sa.owner_id=$1 ORDER BY sa.created_at DESC LIMIT 20""",
                uid,
            )
            history_total = await pool.fetchval(
                "SELECT COUNT(*) FROM shield_actions WHERE owner_id=$1", uid)
            cfg = await pool.fetchrow(
                "SELECT * FROM shield_configs WHERE owner_id=$1", uid
            )
        except Exception:
            log.exception("shield_summary uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({
            "stats": {k: int(row[k] or 0) for k in ("total_active","cooling","threatened","high_ban","ok_count")} if row else {},
            "config": {
                "risk_threshold": float(cfg["risk_threshold"]) if cfg else 0.7,
                "ban_prob_threshold": float(cfg["ban_prob_threshold"]) if cfg else 0.5,
                "auto_pause": bool(cfg["auto_pause"]) if cfg else True,
                "notify_admin": bool(cfg["notify_admin"]) if cfg else True,
                "cool_duration_hours": int(cfg["cool_duration_hours"]) if cfg else 24,
            },
            "history_total": history_total or 0,
            "history": [
                {
                    "account_id": r["account_id"],
                    "action": r["action"], "risk": float(r["risk_score"] or 0),
                    "ban_prob": float(r["ban_probability"] or 0),
                    "name": r["first_name"] or r["phone"] or "",
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in history
            ],
        })

    # ── Ad Intelligence ────────────────────────────────────────────────────────

    async def ad_intel_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            top = await pool.fetch(
                """SELECT channel_username, channel_title, subscribers, quality_score, er_rate, ad_price_est
                   FROM ad_placements WHERE owner_id=$1 ORDER BY quality_score DESC LIMIT 10""",
                uid,
            )
            total = await pool.fetchval("SELECT COUNT(*) FROM ad_placements WHERE owner_id=$1", uid)
            advertisers = await pool.fetch(
                """SELECT advertiser_username, placements_count, last_seen_at
                   FROM ad_advertisers WHERE owner_id=$1 ORDER BY last_seen_at DESC LIMIT 10""",
                uid,
            )
            # Итоги по ВСЕМ рекламодателям, а не по top-10: фронт рисует ими KPI-плитки
            # «Рекламодателей» и «Размещений». Без этих ключей обе плитки всегда
            # показывали 0 при реальных данных (нечестная статистика).
            adv_totals = await pool.fetchrow(
                """SELECT COUNT(*) AS advertisers,
                          COALESCE(SUM(placements_count), 0) AS placements
                   FROM ad_advertisers WHERE owner_id=$1""",
                uid,
            )
        except Exception:
            log.exception("ad_intel_overview uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        avg_score = sum(r["quality_score"] or 0 for r in top) / max(len(top), 1)
        return _json_resp({
            "total_channels": int(total or 0),
            "total_advertisers": int((adv_totals or {}).get("advertisers") or 0),
            "total_placements": int((adv_totals or {}).get("placements") or 0),
            "avg_quality": round(avg_score, 1),
            "top_channels": [
                {
                    "username": r["channel_username"] or "",
                    "title": r["channel_title"] or "",
                    "subscribers": r["subscribers"] or 0,
                    "quality_score": round(float(r["quality_score"] or 0), 1),
                    "er_rate": round(float(r["er_rate"] or 0), 2),
                    "ad_price_est": r["ad_price_est"] or 0,
                }
                for r in top
            ],
            "top_advertisers": [
                {
                    "username": r["advertiser_username"] or "",
                    "placements": r["placements_count"] or 0,
                    "last_seen": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
                }
                for r in advertisers
            ],
        })

    async def ad_intel_add_channel(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            channel = str(body.get("channel", "")).strip().lstrip("@")
        except Exception:
            return _err("Неверное тело запроса", 400)
        if not channel:
            return _err("Укажите канал", 400)
        label = f"Разведка рекламы @{channel}"
        try:
            op_id = await _obus.submit(
                pool, uid, "ad_intel_scan", {"channel": channel},
                total_items=1, label=label)
            return _json_resp({"ok": True, "op_id": op_id, "label": label})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("ad_intel_add_channel uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    # ── Network / Cluster Overview ─────────────────────────────────────────────




    # ── Relay (Inbox) ─────────────────────────────────────────────────────────


    async def relay_session_messages(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            session_id = int(request.match_info["session_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор сессии", 400)
        # Verify ownership via join
        try:
            row = await pool.fetchrow(
                """SELECT rs.id FROM relay_sessions rs
                   JOIN managed_bots mb ON mb.bot_id=rs.bot_id
                   WHERE rs.id=$1 AND mb.added_by=$2""",
                session_id, uid,
            )
        except Exception:
            log.exception("relay_session_messages ownership uid=%d sess=%d", uid, session_id)
            return _err(_INTERNAL_ERROR, 500)
        if not row:
            return _err("Не найдено", 404)
        try:
            msgs = await pool.fetch(
                "SELECT id, direction, text, created_at FROM relay_messages "
                "WHERE session_id=$1 ORDER BY created_at ASC, id "
                "LIMIT $2 OFFSET $3",
                session_id, _list_limit(request, 100, 2000), _list_offset(request),
            )
            total = await _safe_count(pool,
                "SELECT COUNT(*) FROM relay_messages WHERE session_id=$1", session_id)
        except Exception:
            log.exception("relay_session_messages msgs uid=%d sess=%d", uid, session_id)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"total": total, "messages": [
            {
                "id": r["id"],
                "direction": r["direction"],
                "text": r["text"] or "",
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in msgs
        ]})


    # ── API Keys (API Hub) ─────────────────────────────────────────────────────

    async def api_keys_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT id, name, key_prefix, is_active, created_at, last_used_at
                   FROM api_keys WHERE user_id=$1 ORDER BY created_at DESC""",
                uid,
            )
        except Exception:
            log.exception("api_keys_list uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"keys": [
            {
                "id": r["id"], "name": r["name"] or "",
                "prefix": r["key_prefix"] or "",
                "is_active": bool(r["is_active"]),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None,
            }
            for r in rows
        ]})

    async def revoke_api_key(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            key_id = int(request.match_info["key_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор ключа", 400)
        try:
            res = await pool.execute(
                "UPDATE api_keys SET is_active=FALSE WHERE id=$1 AND user_id=$2",
                key_id, uid,
            )
            if res == "UPDATE 0":
                return _err("Не найдено", 404)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("revoke_api_key uid=%d key=%d", uid, key_id)
            return _err(_INTERNAL_ERROR, 500)

    async def create_api_key(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = (body.get("name") or "Mini App Key").strip()[:64]
        import secrets
        raw_key = secrets.token_urlsafe(32)
        prefix = raw_key[:8]
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        try:
            row = await pool.fetchrow(
                """INSERT INTO api_keys(user_id, key_hash, key_prefix, name)
                   VALUES($1,$2,$3,$4) RETURNING id""",
                uid, key_hash, prefix, name,
            )
            return _json_resp({"ok": True, "id": row["id"], "key": raw_key, "prefix": prefix, "name": name})
        except Exception:
            log.exception("create_api_key uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    # ── Multigeo (per-language bot profile) ───────────────────────────────────



    # ── Strike history ─────────────────────────────────────────────────────────


    # ── Strike: status + launch ────────────────────────────────────────────────




    # ── Audience Parser (read-only history) ───────────────────────────────────





    # ── CRM Deals ─────────────────────────────────────────────────────────────





    # ── Workspaces ─────────────────────────────────────────────────────────────








    # ── Promo Platform ────────────────────────────────────────────────────────






    # ── Error Reports ─────────────────────────────────────────────────────────

    async def submit_error_report(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            description = str(body.get("description", "")).strip()
            context = body.get("context", None)
        except Exception:
            return _err("Неверное тело запроса", 400)
        if len(description) < 10:
            return _err("Описание слишком короткое (мин. 10 символов)", 400)
        if len(description) > 2000:
            return _err("Описание слишком длинное (макс. 2000 символов)", 400)
        try:
            report_id = await pool.fetchval(
                """INSERT INTO error_reports(user_id, description, context, status)
                   VALUES($1,$2,$3,'new') RETURNING id""",
                uid, description,
                __import__("json").dumps(context) if context else None,
            )
        except Exception:
            log.exception("submit_error_report uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True, "report_id": report_id})

    async def my_error_reports(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                "SELECT id, description, status, created_at, updated_at, notes "
                "FROM error_reports "
                "WHERE user_id=$1 ORDER BY created_at DESC LIMIT 20",
                uid,
            )
        except Exception:
            log.exception("my_error_reports uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        # Описание целиком, а не первые 120 символов: отправитель не мог
        # перечитать, что именно он написал, — а именно это и нужно, когда
        # отчёт обрастает ответом. `notes` — единственный канал ответа
        # обратно: разбор отчёта писали туда, и он никогда не доходил.
        return _json_resp({"reports": [
            {
                "id": r["id"],
                "description": r["description"] or "",
                "status": r["status"] or "new",
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
                "answer": (r.get("notes") or "").strip() or None,
            }
            for r in rows
        ]})

    # ── Bot toggle / edit ─────────────────────────────────────────────────────


    # ── Funnel Steps ──────────────────────────────────────────────────────────






    # ── Competitors ────────────────────────────────────────────────────────────

    async def competitors_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            """SELECT id, username, label, channel_id, last_members, last_checked, created_at
               FROM competitors WHERE owner_id=$1
               ORDER BY created_at DESC, id DESC LIMIT $2 OFFSET $3""",
            uid, _list_limit(request, 50, 1000), _list_offset(request))
        total = await _safe_count(pool,
            "SELECT COUNT(*) FROM competitors WHERE owner_id=$1", uid)
        return _json_resp({"competitors": rows, "total": total})

    async def add_competitor(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        username = (body.get("username") or "").strip().lstrip("@").lower()
        label = (body.get("label") or "").strip() or None
        if not username:
            return _err("Нужно имя пользователя")
        if len(username) > 50:
            return _err("Имя пользователя слишком длинное")
        try:
            row = await pool.fetchrow(
                """INSERT INTO competitors(owner_id, username, label) VALUES($1,$2,$3)
                   ON CONFLICT(owner_id, username) DO UPDATE SET label=EXCLUDED.label
                   RETURNING id""",
                uid, username, label)
            return _json_resp({"ok": True, "id": row["id"]})
        except Exception:
            log.exception("add_competitor uid=%d", uid)
            return _err("Не удалось добавить конкурента", 500)

    async def delete_competitor(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            comp_id = int(request.match_info["comp_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор конкурента", 400)
        try:
            await pool.execute(
                "DELETE FROM competitors WHERE id=$1 AND owner_id=$2", comp_id, uid)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("delete_competitor")
            return _err("Не удалось удалить", 500)

    # ── Network Broadcast (all bots) ──────────────────────────────────────────

    async def network_broadcast(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        text = _text(body.get("text"))
        if not text:
            return _err("Нужен текст")
        if len(text) > 4096:
            return _err("Текст длиннее 4096 символов")
        # Опциональный выбор ботов: bot_ids → рассылка только выбранным
        sel_ids = [int(x) for x in (body.get("bot_ids") or []) if str(x).lstrip("-").isdigit()]
        if sel_ids:
            bots = await _safe_fetch(pool,
                """SELECT mb.bot_id, COUNT(bu.user_id) FILTER (WHERE bu.is_active=true) AS active_subs
                   FROM managed_bots mb
                   LEFT JOIN bot_users bu ON bu.bot_id=mb.bot_id
                   WHERE mb.added_by=$1 AND mb.is_active=true AND mb.bot_id = ANY($2::bigint[])
                   GROUP BY mb.bot_id""", uid, sel_ids)
        else:
            bots = await _safe_fetch(pool,
                """SELECT mb.bot_id, COUNT(bu.user_id) FILTER (WHERE bu.is_active=true) AS active_subs
                   FROM managed_bots mb
                   LEFT JOIN bot_users bu ON bu.bot_id=mb.bot_id
                   WHERE mb.added_by=$1 AND mb.is_active=true
                   GROUP BY mb.bot_id""", uid)
        if not bots:
            return _err("Активных ботов нет")
        total_recipients = sum(b["active_subs"] or 0 for b in bots)
        lang = body.get("lang", "")
        # Инлайн-кнопки (необязательно)
        _buttons = []
        for b in (body.get("buttons") or [])[:10]:
            try:
                bt = str(b.get("text") or "").strip()[:64]
                bu = str(b.get("url") or "").strip()
            except Exception:
                continue
            if bt and bu.lower().startswith(("http://", "https://")):
                _buttons.append({"text": bt, "url": bu})
        if sel_ids:
            segment = "selected_bots"
            op_params = {"text": text, "segment": segment, "lang": lang,
                         "selected_bot_ids": [int(b["bot_id"]) for b in bots]}
        else:
            segment = body.get("segment", "all_each")
            op_params = {"text": text, "segment": segment, "lang": lang}
        if _buttons:
            op_params["buttons"] = _buttons
        label = f"Рассылка по сети: {text[:40]}{'…' if len(text) > 40 else ''}"
        try:
            op_id = await _obus.submit(
                pool, uid, "network_broadcast", op_params,
                total_items=total_recipients, label=label)
            return _json_resp({
                "ok": True,
                "op_id": op_id,
                "total_recipients": total_recipients,
                "broadcasts_created": len(bots),
                "label": label,
            })
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) или предохранитель
            # Ban Weather (ImmunityBlockedError) — это НЕ сбой: честный 403 с
            # причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("network_broadcast op_queue uid=%d", uid)
            return _err("Не удалось поставить рассылку в очередь", 500)

    # ── CRM Contacts ─────────────────────────────────────────────────────────





    # ── Keywords / Search Rankings ────────────────────────────────────────────

    async def keywords(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            """SELECT tk.id, tk.keyword, mb.username AS bot_username, mb.bot_id,
                      tk.is_active, tk.created_at,
                      (SELECT sr.position FROM search_rankings sr
                       WHERE sr.keyword_id=tk.id ORDER BY sr.checked_at DESC LIMIT 1) AS last_position,
                      (SELECT sr.checked_at FROM search_rankings sr
                       WHERE sr.keyword_id=tk.id ORDER BY sr.checked_at DESC LIMIT 1) AS last_checked
               FROM tracked_keywords tk
               JOIN managed_bots mb ON mb.bot_id=tk.bot_id
               WHERE tk.owner_id=$1
               ORDER BY tk.created_at DESC, tk.id DESC LIMIT $2 OFFSET $3""",
            uid, _list_limit(request, 50, 1000), _list_offset(request))
        total = await _safe_count(pool,
            "SELECT COUNT(*) FROM tracked_keywords tk JOIN managed_bots mb "
            "ON mb.bot_id=tk.bot_id WHERE tk.owner_id=$1", uid)
        return _json_resp({"keywords": rows, "total": total})

    async def add_keyword(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        bot_id = body.get("bot_id")
        keyword = (body.get("keyword") or "").strip().lower()
        if not bot_id or not keyword:
            return _err("Нужны идентификатор бота и ключевое слово")
        if len(keyword) > 100:
            return _err("Ключевое слово слишком длинное")
        try:
            bot_id_int = int(bot_id)
        except (TypeError, ValueError):
            return _err("Неверный идентификатор бота")
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id_int, uid)
        if not owns:
            return _err("Бот не найден", 404)
        try:
            row = await pool.fetchrow(
                """INSERT INTO tracked_keywords(bot_id, owner_id, keyword)
                   VALUES($1,$2,$3) ON CONFLICT(bot_id, keyword) DO UPDATE SET is_active=true
                   RETURNING id""",
                bot_id_int, uid, keyword)
            return _json_resp({"ok": True, "id": row["id"]})
        except Exception:
            log.exception("add_keyword uid=%d", uid)
            return _err("Не удалось добавить ключевое слово", 500)

    async def delete_keyword(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            kw_id = int(request.match_info["kw_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор ключевого слова", 400)
        try:
            await pool.execute(
                "DELETE FROM tracked_keywords WHERE id=$1 AND owner_id=$2", kw_id, uid)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("delete_keyword")
            return _err("Не удалось удалить", 500)

    # ── Account Warmup control ────────────────────────────────────────────────



    # ── Schedules ────────────────────────────────────────────────────────────



    async def cancel_schedule(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            sch_id = int(request.match_info["sch_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор расписания", 400)
        try:
            row = await pool.fetchrow(
                """UPDATE scheduled_broadcasts SET status='cancelled'
                   WHERE id=$1 AND created_by=$2 AND status='pending'
                   RETURNING id""", sch_id, uid)
            if not row:
                return _err("Не найдено или уже выполнено", 404)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("cancel_schedule")
            return _err("Не удалось отменить", 500)

    # ── Templates ────────────────────────────────────────────────────────────




    # ── Mass Publish ─────────────────────────────────────────────────────────

    async def mass_publish_targets(request: web.Request) -> web.Response:
        """Сколько каналов реально получат массовую публикацию — и какие именно.

        Экран обещал «опубликовать во ВСЕ каналы» и спрашивал подтверждение, ни
        разу не назвав число: человек жал необратимую кнопку, не зная, три у него
        канала или триста. Считаем ровно тем же запросом, что и mass_publish
        ниже (managed_channels по owner_id), иначе подпись под кнопкой обещала бы
        один набор, а публикация ушла бы в другой: список /channels шире — он
        добавляет каналы экосистем и рабочих пространств, которых массовая
        публикация не касается.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            sample_limit = min(validate_integer(request.query.get("sample", "12"),
                                                min_val=0, max_val=100) or 0, 100)
        except (ValueError, TypeError):
            sample_limit = 12
        total = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", uid)
        sample = []
        if sample_limit:
            # Порядок детерминированный: «тест на N каналов» должен брать тот же
            # первый N, что человек увидел в подтверждении, а не случайную
            # выборку от запроса к запросу.
            rows = await _safe_fetch(pool,
                """SELECT channel_id, username, title,
                          COALESCE(members_count, 0) AS member_count
                   FROM managed_channels WHERE owner_id=$1
                   ORDER BY added_at DESC NULLS LAST, channel_id DESC
                   LIMIT $2""", uid, sample_limit)
            for r in (rows or []):
                sample.append({
                    "channel_id": int(r["channel_id"]) if r["channel_id"] is not None else None,
                    "username": r["username"],
                    "title": r["title"],
                    "member_count": int(r["member_count"] or 0),
                })
        return _json_resp({"ok": True, "total": int(total or 0), "sample": sample})

    async def mass_publish(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        text = validate_string(body.get("text"), max_len=4096)
        delay = validate_integer(body.get("delay", 30), min_val=-1, max_val=300) or 30
        channel_ids = body.get("channel_ids")  # optional list; None = all channels
        if not text:
            return _err("Нужен текст")
        from services import postbot as _postbot
        if _postbot.has_explicit_intent(text) and not _postbot.parse_reference(text):
            return _err("Неверный формат: укажите @PostBot и один код публикации", 400)
        if check_sql_suspicious(text):
            return _err("Недопустимые символы в тексте")
        if delay not in (5, 30, 60, -1):
            delay = 30
        # Validate channel IDs if provided
        if channel_ids:
            try:
                channel_ids = [int(x) for x in channel_ids if str(x).lstrip("-").isdigit()]
            except (TypeError, ValueError):
                return _err("Неверные идентификаторы каналов", 400)
        # Count target channels
        if channel_ids:
            total = len(channel_ids)
        else:
            total = await _safe_count(pool,
                "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", uid)
        if total == 0:
            return _err("Нет каналов для публикации")
        # Расписание (необязательно): ISO-время запуска в будущем. Фронт присылает
        # UTC ISO (new Date(local).toISOString()); воркер уважает scheduled_for.
        scheduled_for = None
        _sched_raw = body.get("scheduled_for")
        if _sched_raw:
            import datetime as _dt
            try:
                _sd = _dt.datetime.fromisoformat(str(_sched_raw).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return _err("Некорректное время публикации", 400)
            if _sd.tzinfo is None:
                _sd = _sd.replace(tzinfo=_dt.timezone.utc)
            if _sd <= _dt.datetime.now(_dt.timezone.utc):
                return _err("Время публикации должно быть в будущем", 400)
            scheduled_for = _sd.isoformat()
        params = {"text": text, "delay": delay, "owner_id": uid}
        if channel_ids:
            params["channel_ids"] = channel_ids
            # Явно выбранные группы не должны отсекаться фильтром «только
            # каналы» исполнителя: автопостинг из карточки шлёт target=all.
            if body.get("target") in ("all", "channels", "groups"):
                params["target"] = body["target"]
        # Рекуррентная публикация (0 = разовая). Умел её только «Быстрый пост»,
        # слитый в этот экран; при слиянии повтор потерялся, хотя воркер его
        # поддерживает — после успешного прогона сам ставит следующий запуск.
        try:
            _rmin = max(0, min(int(body.get("repeat_interval_min") or 0), 60 * 24 * 30))
        except (TypeError, ValueError):
            _rmin = 0
        if _rmin > 0:
            params["repeat_interval_min"] = _rmin
            _rc = body.get("repeat_count")
            if _rc is not None:
                try:
                    params["repeat_count"] = max(1, min(int(_rc), 1000))
                except (TypeError, ValueError):
                    pass
        # Рубрика поста (контент-микс редактора): только из рубрик владельца,
        # иначе история заполнится опечатками и микс перестанет что-то значить.
        _pillar = str(body.get("pillar") or "").strip()
        if _pillar:
            from services import channel_brain_store as _cbs
            _brain = await _cbs.get_profile(pool, uid, _cbs.OWNER_DEFAULT_KEY)
            if not _brain or _pillar not in _brain.pillars:
                return _err("Такой рубрики нет в правилах редактора", 400)
            params["pillar"] = _pillar
        try:
            from services.operation_bus import submit
            op_id = await submit(pool, uid, "mass_publish", params, total_items=total,
                                 scheduled_for=scheduled_for)
            return _json_resp({"ok": True, "op_id": op_id, "total": total,
                               "scheduled_for": scheduled_for})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("mass_publish uid=%d", uid)
            return _err("Не удалось поставить массовую публикацию в очередь", 500)

    # ── Проверка ограничений/теневого бана наших каналов/чатов/ботов ───────
    async def check_owned_restrictions_targets(request: web.Request) -> web.Response:
        """Сколько НАШИХ сущностей проверит массовая проверка ограничений.

        Каналы/чаты — по owner_id, боты — по added_by (на managed_bots нет
        owner_id, тот же скоуп, что и в topology_nodes). Число нужно человеку
        ДО запуска: он должен видеть, три у него сущности или триста.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        channels = await _safe_count(pool,
            "SELECT COUNT(DISTINCT channel_id) FROM managed_channels WHERE owner_id=$1", uid)
        bots = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1", uid)
        return _json_resp({"ok": True, "channels": int(channels or 0),
                           "bots": int(bots or 0),
                           "total": int(channels or 0) + int(bots or 0)})

    async def check_owned_restrictions(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        # Необязательные списки: проверить только выбранные сущности. Пусто = все.
        channel_ids = body.get("channel_ids")
        bot_ids = body.get("bot_ids")
        params: dict = {"owner_id": uid}
        if channel_ids:
            try:
                params["channel_ids"] = [int(x) for x in channel_ids
                                         if str(x).lstrip("-").isdigit()]
            except (TypeError, ValueError):
                return _err("Неверные идентификаторы каналов", 400)
        if bot_ids:
            try:
                params["bot_ids"] = [int(x) for x in bot_ids
                                     if str(x).lstrip("-").isdigit()]
            except (TypeError, ValueError):
                return _err("Неверные идентификаторы ботов", 400)
        # Число сущностей = total операции (для честного счётчика прогресса).
        if params.get("channel_ids") is not None or params.get("bot_ids") is not None:
            total = len(params.get("channel_ids") or []) + len(params.get("bot_ids") or [])
        else:
            ch = await _safe_count(pool,
                "SELECT COUNT(DISTINCT channel_id) FROM managed_channels WHERE owner_id=$1", uid)
            bt = await _safe_count(pool,
                "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1", uid)
            total = int(ch or 0) + int(bt or 0)
        if total == 0:
            return _err("Нет каналов/чатов/ботов для проверки")
        try:
            from services.operation_bus import submit
            op_id = await submit(pool, uid, "check_owned_restrictions", params,
                                 total_items=total)
            return _json_resp({"ok": True, "op_id": op_id, "total": total})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("check_owned_restrictions uid=%d", uid)
            return _err("Не удалось поставить проверку в очередь", 500)

    async def check_bot_geoban_targets(request: web.Request) -> web.Response:
        """Сколько ПУБЛИЧНЫХ ботов (с @username) можно проверить на гео-бан, и в
        скольких различимых странах есть аккаунты (гео-бан виден только при ≥2)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        bots = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_bots "
            "WHERE added_by=$1 AND username IS NOT NULL AND username <> ''", uid)
        geos = await _safe_count(pool,
            """SELECT COUNT(DISTINCT UPPER(p.geo_country))
                 FROM tg_accounts a
                 JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE
                WHERE a.owner_id=$1 AND a.is_active=TRUE
                  AND p.geo_country IS NOT NULL AND p.geo_country <> ''""", uid)
        return _json_resp({"ok": True, "bots": int(bots or 0),
                           "geos": int(geos or 0)})

    async def check_bot_geoban(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        params: dict = {"owner_id": uid}
        bot_ids = body.get("bot_ids")
        if bot_ids:
            try:
                params["bot_ids"] = [int(x) for x in bot_ids
                                     if str(x).lstrip("-").isdigit()]
            except (TypeError, ValueError):
                return _err("Неверные идентификаторы ботов", 400)
        if params.get("bot_ids") is not None:
            total = len(params["bot_ids"])
        else:
            total = int(await _safe_count(pool,
                "SELECT COUNT(*) FROM managed_bots "
                "WHERE added_by=$1 AND username IS NOT NULL AND username <> ''", uid) or 0)
        if total == 0:
            return _err("Нет публичных ботов (с @username) для проверки гео-бана")
        try:
            from services.operation_bus import submit
            op_id = await submit(pool, uid, "check_bot_geoban", params, total_items=total)
            return _json_resp({"ok": True, "op_id": op_id, "total": total})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("check_bot_geoban uid=%d", uid)
            return _err("Не удалось поставить проверку в очередь", 500)

    # ── Редактор канала (Virtual Channel Administrator) ────────────────────
    # Правила редактора задавались только в коде (save_profile), а подсказка
    # перед публикацией жила лишь в боте: в Mini App — главном интерфейсе —
    # пост уходил во все каналы без единой проверки на повтор и правила.

    async def _editorial_key(request: web.Request, uid: int) -> str | None:
        """Ключ политики: ?channel=<id> — правила своего канала (виртуальный
        администратор канала), без него — общие правила владельца. None — чужой канал."""
        from services import channel_brain_store as _cbs
        raw = (request.query.get("channel") or "").strip()
        if not raw:
            return _cbs.OWNER_DEFAULT_KEY
        try:
            cid = int(raw)
        except ValueError:
            return None
        from services import channel_admin as _ca
        return str(cid) if await _ca.channel_row(pool, uid, cid) else None

    async def editorial_policy_get(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services import channel_brain_store as _cbs
        key = await _editorial_key(request, uid)
        if key is None:
            return _err("Канал не найден среди ваших каналов", 404)
        brain = await _cbs.get_profile(pool, uid, key)
        return _json_resp({"ok": True, "policy": _cbs.to_public(brain),
                           "next_pillar": await _editorial_next_pillar(uid, brain)})

    async def _editorial_next_pillar(uid: int, brain) -> str | None:
        """Какую рубрику публиковать следующей (контент-микс) или None."""
        if brain is None or not brain.pillars:
            return None
        from services import content_memory as _cm
        recent = await _cm.recent_pillars_for_owner(pool, uid)
        return brain.plan_next([p for p in recent if p in brain.pillars])

    async def editorial_policy_save(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        from services import channel_brain_store as _cbs
        key = await _editorial_key(request, uid)
        if key is None:
            return _err("Канал не найден среди ваших каналов", 404)
        clean, errors = _cbs.validate_policy(body)
        pillars = weights = None
        if isinstance(body, dict) and "pillars" in body:
            pillars, weights, p_err = _cbs.validate_pillars(body.get("pillars"))
            errors += p_err
        if errors:
            return _err("; ".join(errors[:5]), 400)
        # Что запрос не прислал (рубрики, режим автономности), сохраняем как
        # было, а не затираем значениями по умолчанию.
        prev = await _cbs.get_profile(pool, uid, key)
        if pillars is None:
            pillars = prev.pillars if prev else None
            weights = prev.mix_weights if prev else None
        row_id = await _cbs.save_profile(
            pool, uid, key,
            brand_rules=clean["brand_rules"],
            dup_threshold=clean["dup_threshold"],
            pillars=pillars,
            mix_weights=weights,
            # Режим редактора: если запрос прислал новый — применяем его, иначе
            # сохраняем прежний. Раньше здесь всегда стоял prev/"manual", поэтому
            # включить блокировку из интерфейса было нельзя вовсе.
            autonomy_mode=clean.get("autonomy_mode",
                                    prev.autonomy_mode if prev else "manual"),
            max_streak=prev.max_streak if prev else 2,
        )
        if row_id is None:
            log.warning("editorial_policy_save failed uid=%s", uid)
            return _err("Не удалось сохранить правила, попробуйте ещё раз", 500)
        log.info("editorial policy saved uid=%s words=%d openings=%d",
                 uid, len(clean["brand_rules"]["forbidden_words"]),
                 len(clean["brand_rules"]["banned_openings"]))
        brain = await _cbs.get_profile(pool, uid, key)
        return _json_resp({"ok": True, "policy": _cbs.to_public(brain),
                           "next_pillar": await _editorial_next_pillar(uid, brain)})

    async def editorial_review_draft(request: web.Request) -> web.Response:
        """Совет редактора по черновику перед публикацией. Не блокирует её."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        text = validate_string(body.get("text"), max_len=4096)
        if not text:
            return _err("Нужен текст")
        from services import editorial_review as _er
        verdict = await _er.review_draft(pool, uid, text)
        return _json_resp({"ok": True, **_er.verdict_to_public(verdict)})

    # ── Виртуальный администратор канала (свой на каждый канал) ─────────────
    # Вся логика — services/channel_admin (её же зовёт бот); здесь только вход.






















    # Каналы-образцы: добавить (и сразу изучить), изучить заново, убрать.



    # ── Proxies ──────────────────────────────────────────────────────────────


    # ── Analytics ────────────────────────────────────────────────────────────


    # ── Subscription ─────────────────────────────────────────────────────────

    async def subscription(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        # Тариф живёт в РАЗНЫХ источниках, и любой из них может быть истиной:
        #   • get_plan       — админ→paid ИЛИ активная подписка (кеш, может устареть);
        #   • subscriptions  — оплата/бот (активная не истёкшая строка);
        #   • platform_users — ручная выдача админом (current_plan, не истёкший).
        # Раньше здесь был фолбэк `if not plan:` на platform_users — но get_plan
        # возвращает "free" (истинную строку), поэтому ветка НЕ срабатывала: тариф,
        # выданный только в platform_users (напр. enterprise от админа), показывался
        # как Free. Берём НАИВЫСШИЙ тариф среди источников — как это уже делает
        # дашборд через тот же _resolve_best_plan.
        gp_plan = None
        try:
            from bot.utils.subscription import get_plan as _gp
            gp_plan = await _gp(pool, uid)
        except Exception:
            gp_plan = None
        expires = None
        exp_plan = None
        try:
            srow = await pool.fetchrow(
                "SELECT plan, expires_at FROM subscriptions "
                "WHERE user_id=$1 AND is_active=true AND expires_at > now() "
                "ORDER BY expires_at DESC LIMIT 1", uid)
            if srow:
                exp_plan = srow["plan"]
                expires = str(srow["expires_at"])
        except Exception as e:
            log.warning("subscription fetch subscriptions: %s", e)
        pu_plan = None
        pu_expires = None
        try:
            prow = await pool.fetchrow(
                "SELECT current_plan, plan_expires_at FROM platform_users "
                "WHERE user_id=$1 AND (plan_expires_at IS NULL OR plan_expires_at > now())", uid)
            if prow:
                pu_plan = prow["current_plan"]
                pu_expires = prow["plan_expires_at"]
        except Exception as e:
            log.warning("subscription fetch platform_users: %s", e)
        # Наивысший тариф среди всех источников (канон free/paid).
        plan = _resolve_best_plan(gp_plan, exp_plan, pu_plan)
        # Срок: активная подписка приоритетна, иначе — из ручной выдачи.
        if not expires and plan != "free" and pu_expires:
            expires = str(pu_expires)
        return _json_resp({
            "plan": plan,
            "expires_at": expires,
            "is_active": plan != "free",
        })

    async def user_settings_get(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        settings = {
            "notif_ops": True,
            "notif_pay": True,
            "notif_report": False,
            "notif_error": True,
            "utc_logs": False,
            "lang": "ru",
        }
        try:
            row = await pool.fetchrow(
                "SELECT settings_json FROM platform_users WHERE user_id=$1", uid)
            if row and row["settings_json"]:
                settings.update(_json.loads(row["settings_json"]))
        except Exception as e:
            log.warning("user_settings_get: %s", e)
        # Тумблеры уведомлений отражают РЕАЛЬНЫЙ источник правды (notification_settings),
        # а не что записал мини-апп — иначе тумблер мог «врать» относительно бота.
        try:
            from database import db as _db
            _ns = await _db.get_notification_settings(pool, uid)
            settings["notif_ops"] = bool(_ns.get("op_complete", True))
            settings["notif_error"] = bool(_ns.get("restriction", True))
            # new_user и position_change — реальные, читаемые notify_if_enabled
            # преференции, которые мини-апп раньше не показывал: отключить спам
            # «новый подписчик» / «позиция изменилась» было неоткуда.
            settings["notif_new_user"] = bool(_ns.get("new_user", True))
            settings["notif_position"] = bool(_ns.get("position_change", True))
        except Exception as _e:
            log.debug("user_settings_get notif overlay uid=%d: %s", uid, _e)
        return _json_resp(settings)

    async def user_settings_save(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
            settings_json = _json.dumps(data)
            await pool.execute(
                """UPDATE platform_users SET settings_json=$1 WHERE user_id=$2""",
                settings_json, uid)
            # Немедленно применяем выбор политики прокси (не ждём старта операции).
            try:
                from services import account_manager as _am
                _am.set_owner_proxy_policy(uid, data.get("proxy_policy"))
            except Exception:
                pass
            # Тумблеры уведомлений — в ЕДИНЫЙ источник правды notification_settings
            # (его читает notify_if_enabled). Иначе тумблеры мини-аппа были «тихим
            # успехом»: сохранялись в settings_json, но бот их не читал. Маппинг:
            # notif_ops→op_complete, notif_error→restriction+flood_warning.
            try:
                _op = bool(data.get("notif_ops", True))
                # Имя _err занято функцией ответа об ошибке (_err(msg, status)).
                # Локальная переменная с тем же именем делала _err локальным на всю
                # user_settings_save: ветка 401 падала с UnboundLocalError, а ветка
                # 500 — с TypeError «bool не вызывается». Оба отказа уходили
                # пользователю сырым 500.
                _notif_err = bool(data.get("notif_error", True))
                # new_user/position_change сохраняем, только если фронт их прислал:
                # иначе старый клиент, не знающий про эти поля, молча сбрасывал бы
                # их в дефолт при каждом сохранении настроек.
                _nu = bool(data.get("notif_new_user", True))
                _pos = bool(data.get("notif_position", True))
                _has_extra = "notif_new_user" in data or "notif_position" in data
                if _has_extra:
                    await pool.execute(
                        "INSERT INTO notification_settings(user_id, op_complete, restriction, "
                        "flood_warning, new_user, position_change) "
                        "VALUES($1,$2,$3,$3,$4,$5) "
                        "ON CONFLICT(user_id) DO UPDATE SET op_complete=$2, restriction=$3, "
                        "flood_warning=$3, new_user=$4, position_change=$5, updated_at=now()",
                        uid, _op, _notif_err, _nu, _pos,
                    )
                else:
                    await pool.execute(
                        "INSERT INTO notification_settings(user_id, op_complete, restriction, flood_warning) "
                        "VALUES($1,$2,$3,$3) "
                        "ON CONFLICT(user_id) DO UPDATE SET op_complete=$2, restriction=$3, "
                        "flood_warning=$3, updated_at=now()",
                        uid, _op, _notif_err,
                    )
            except Exception as _ne:
                log.warning("settings→notification_settings sync failed uid=%d: %s", uid, _ne)
            return _json_resp({"ok": True})
        except Exception as e:
            _bad = bad_body_response(request, e)
            if _bad is not None:
                return _bad
            log.warning("user_settings_save uid=%d: %s", uid, e)
            return _err("Не удалось сохранить", 500)

    async def payments_history(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT plan, period_months, amount_usd, currency, status, created_at
                   FROM payments
                   WHERE user_id=$1
                   ORDER BY created_at DESC
                   LIMIT 20""",
                uid,
            )
            return _json_resp([dict(r) for r in rows])
        except Exception as e:
            log.warning("payments_history uid=%d: %s", uid, e)
            return _json_resp([])

    async def referral(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        row = await _safe_fetchrow(pool,
            "SELECT code, total_clicks FROM platform_referral_codes WHERE user_id=$1", uid)
        count = await _safe_count(pool,
            "SELECT COUNT(*) FROM platform_referrals WHERE referrer_id=$1", uid)
        return _json_resp({
            "code": row["code"] if row else None,
            "total_clicks": row["total_clicks"] if row else 0,
            "total_referrals": count,
        })

    # ── Asset Templates ──────────────────────────────────────────────────────

    async def asset_templates_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        asset_type = request.rel_url.query.get("type")
        lim = _list_limit(request, 50, 1000)
        try:
            if asset_type:
                rows = await pool.fetch(
                    "SELECT id, asset_type, name, created_at FROM asset_templates "
                    "WHERE owner_id=$1 AND asset_type=$2 "
                    "ORDER BY created_at DESC, id DESC LIMIT $3 OFFSET $4",
                    uid, asset_type, lim, _list_offset(request),
                )
                total = await _safe_count(pool,
                    "SELECT COUNT(*) FROM asset_templates WHERE owner_id=$1 AND asset_type=$2",
                    uid, asset_type)
            else:
                rows = await pool.fetch(
                    "SELECT id, asset_type, name, created_at FROM asset_templates "
                    "WHERE owner_id=$1 ORDER BY created_at DESC, id DESC "
                    "LIMIT $2 OFFSET $3",
                    uid, lim, _list_offset(request),
                )
                total = await _safe_count(pool,
                    "SELECT COUNT(*) FROM asset_templates WHERE owner_id=$1", uid)
            # Было — голый массив; экран показывал первые 50 как «всё, что есть».
            # Теперь ответ несёт настоящий итог (потребитель один — mini_app).
            return _json_resp({"items": [dict(r) for r in rows], "total": total})
        except Exception:
            log.exception("asset_templates_list uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)



    # ── Infra Health Center ───────────────────────────────────────────────────

    async def infra_health_resolve(request: web.Request) -> web.Response:
        """Снять аномалию с активных.

        Экран «Здоровье инфраструктуры» показывал находки anomaly_detector и
        только. Разобраться с ними можно было лишь из бота
        (`infra_health_center`), а в мини-аппе оповещение висело до тех пор,
        пока детектор сам не перестанет его находить.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        anom_id = validate_integer(request.match_info.get("anomaly_id"), min_val=1)
        if anom_id is None:
            return _err("Неверный идентификатор оповещения")
        from services import anomaly_detector
        ok = await anomaly_detector.resolve_anomaly(pool, anom_id, uid)
        if not ok:
            return _err("Оповещение не найдено или уже снято", 404)
        return _json_resp({"ok": True})

    async def infra_health_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        # Раньше экран читал только infrastructure_alerts. Эту таблицу в
        # продукте НИКТО не заполняет: её создают (schema_v84), читают и чистят
        # по сроку, а INSERT в неё нет ни одного. Настоящие находки копит
        # anomaly_detector в anomaly_events — каждые пять минут фоновым циклом,
        # и бот в «Здоровье инфраструктуры» показывает именно их. Поэтому
        # мини-апп писал «Инфраструктура работает нормально» ровно тогда, когда
        # бот на том же материале показывал критические аномалии.
        alerts: list[dict] = []
        try:
            from services import anomaly_detector

            for a in await anomaly_detector.get_active_anomalies(pool, uid):
                alerts.append({
                    "id": a.get("id"),
                    "alert_type": a.get("anomaly_type") or "anomaly",
                    "severity": a.get("severity") or "warning",
                    "title": a.get("title") or "",
                    "description": a.get("description") or "",
                    "target_type": a.get("detector"),
                    "affected_count": a.get("affected_count"),
                    "is_active": True,
                    "first_seen_at": a.get("detected_at"),
                    "resolved_at": None,
                })
        except Exception:
            log.exception("infra_health_overview anomalies uid=%d", uid)
        try:
            recovery = await pool.fetch(
                "SELECT id, recovery_type, target_type, trigger, action, status, "
                "severity, created_at, completed_at "
                "FROM recovery_events WHERE owner_id=$1 "
                "ORDER BY created_at DESC LIMIT 20",
                uid,
            )
        except Exception:
            log.exception("infra_health_overview recovery uid=%d", uid)
            recovery = []
        return _json_resp({
            "alerts": alerts,
            "recovery": [dict(r) for r in recovery],
        })

    # ── Swarm ─────────────────────────────────────────────────────────────────

    async def swarm_metrics(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """
                SELECT mb.bot_id, mb.username, mb.first_name, mb.bot_role, mb.cluster,
                       mb.swarm_weight, mb.swarm_enabled,
                       bm.ctr, bm.conversion_rate, bm.retention_d1, bm.retention_d7, bm.score,
                       bm.updated_at
                FROM managed_bots mb
                LEFT JOIN bot_metrics bm ON bm.bot_id = mb.bot_id
                WHERE mb.added_by=$1
                ORDER BY COALESCE(bm.score, 0) DESC
                LIMIT 30
                """,
                uid,
            )
            return _json_resp([dict(r) for r in rows])
        except Exception:
            log.exception("swarm_metrics uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    # ── Presence Packs ────────────────────────────────────────────────────────


    async def presence_packs_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                "SELECT id, name, description, target_url, target_label, bot_id "
                "FROM presence_packs WHERE owner_id=$1 ORDER BY id DESC "
                "LIMIT $2 OFFSET $3",
                uid, _list_limit(request, 30, 1000), _list_offset(request),
            )
            total = await _safe_count(pool,
                "SELECT COUNT(*) FROM presence_packs WHERE owner_id=$1", uid)
            # Было — голый массив; плитка «N пакетов» считала показанное, а не всё.
            return _json_resp({"items": [dict(r) for r in rows], "total": total})
        except Exception:
            log.exception("presence_packs_list uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)








    # ── Global Presence ───────────────────────────────────────────────────────




    async def geo_presets(request: web.Request) -> web.Response:
        """Список гео-пресетов (страны/города мира) для Global Presence.
        ?cities=<key> — вернуть города конкретного пресета для выбора галочками."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services.geo_data import GEO_PRESETS, preset_city_options
        want = (request.rel_url.query.get("cities") or "").strip()
        if want:
            if want not in GEO_PRESETS:
                return _err("Неизвестный пресет", 404)
            return _json_resp({"key": want, "cities": preset_city_options(want)})
        return _json_resp({"presets": [
            {"key": k, "label": v.get("label", k), "count": v.get("count", len(v.get("cities", [])))}
            for k, v in GEO_PRESETS.items()
        ]})



    async def global_presence_bulk_apply(request: web.Request) -> web.Response:
        """Пакетное оформление объектов проекта: описания и/или аватары.

        Значение вычисляется для каждого объекта отдельно (по его городу, роли
        и seed), поэтому «обновить описания» не превращает сеть в одинаковые
        карточки — этим операция и отличается от общего bulk-редактирования.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            plan_id = int(request.match_info["plan_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор плана", 400)

        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        action = (body.get("action") or "both").strip()
        if action not in ("about", "avatar", "both"):
            return _err("Действие: описания, аватары или и то и другое", 400)
        about_template = (body.get("about_template") or "").strip()

        try:
            plan = await pool.fetchrow(
                "SELECT id FROM global_presence_plans WHERE id=$1 AND owner_id=$2",
                plan_id,
                uid,
            )
            if not plan:
                return _err("План не найден", 404)

            n_targets = await pool.fetchval(
                "SELECT COUNT(*) FROM global_presence_targets "
                " WHERE plan_id=$1 AND status='done' AND asset_type<>'bot'",
                plan_id,
            )
            if not n_targets:
                return _err("В проекте нет созданных объектов для применения", 400)

            params = {"plan_id": plan_id, "action": action}
            if about_template:
                params["about_template"] = about_template[:500]
            if action in ("avatar", "both"):
                # Без сдвига seed перерисовка вернула бы те же картинки:
                # генерация детерминирована.
                import random as _rnd

                params["seed_shift"] = _rnd.randrange(1, 10**6)

            from services import operation_bus

            op_id = await operation_bus.submit(
                pool, uid, "gp_bulk_apply", params, total_items=n_targets
            )
            return _json_resp(
                {"ok": True, "op_id": op_id, "plan_id": plan_id, "targets": n_targets}
            )
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("global_presence_bulk_apply uid=%d plan=%d", uid, plan_id)
            return _err(_INTERNAL_ERROR, 500)

    # ── Mass Ops ──────────────────────────────────────────────────────────────

    async def mass_ops_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """
                SELECT id, op_type, status, done_items, total_items, created_at, finished_at
                FROM operation_queue
                WHERE owner_id=$1 AND op_type LIKE 'mass_%'
                ORDER BY created_at DESC LIMIT 30
                """,
                uid,
            )
            return _json_resp([dict(r) for r in rows])
        except Exception:
            log.exception("mass_ops_overview uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    # ── Ecosystems ────────────────────────────────────────────────────────────









    # ── Channel Factory ───────────────────────────────────────────────────────

    async def channel_factory_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Неверное тело запроса", 400)
        title = (body.get("title") or "").strip()
        about = (body.get("about") or "").strip()
        account_id = body.get("account_id")
        if not title:
            return _err("Нужен заголовок")
        if not account_id:
            return _err("Нужен идентификатор аккаунта")
        try:
            acc = await pool.fetchrow(
                "SELECT id FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND is_active=TRUE",
                int(account_id), uid,
            )
            if not acc:
                return _err("Аккаунт не найден или неактивен", 404)
            from bot.utils.subscription import get_channel_limit, get_effective_channel_count
            _lim = await get_channel_limit(pool, uid)
            if await get_effective_channel_count(pool, uid) >= _lim:
                return _err(f"Достигнут лимит каналов ({_lim}) для вашего тарифа. Оформите подписку для снятия ограничений.", 403)
            op_id = await _obus.submit(
                pool, uid, "create_channel",
                {"title": title, "about": about, "account_id": account_id},
                total_items=1, label=f"Создать канал: {title}")
            return _json_resp({"ok": True, "op_id": op_id})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("channel_factory_submit uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def channel_factory_recent(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                "SELECT id, title, username, type, added_at FROM managed_channels "
                "WHERE owner_id=$1 ORDER BY added_at DESC, id DESC "
                "LIMIT $2 OFFSET $3",
                uid, _list_limit(request, 20, 1000), _list_offset(request),
            )
            total = await _safe_count(pool,
                "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", uid)
            # Было — голый массив; экран показывал 20 последних как весь список.
            return _json_resp({"items": [dict(r) for r in rows], "total": total})
        except Exception:
            log.exception("channel_factory_recent uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    # ── Physics Hub ──────────────────────────────────────────────────────────

    async def physics_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """
                SELECT ars.account_id, ta.phone, ta.username, ta.first_name,
                       ars.risk_score, ars.ban_probability, ars.flood_rate_1h,
                       ars.ops_24h, ars.last_flood_at, ars.computed_at
                FROM account_risk_scores ars
                JOIN tg_accounts ta ON ta.id = ars.account_id
                WHERE ta.owner_id = $1
                ORDER BY ars.risk_score DESC NULLS LAST
                LIMIT 30
                """,
                uid,
            )
            return _json_resp([dict(r) for r in rows])
        except Exception:
            log.exception("physics_overview uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def physics_account_telemetry(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            account_id = int(request.match_info["account_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор аккаунта", 400)
        try:
            owner = await pool.fetchval(
                "SELECT owner_id FROM tg_accounts WHERE id=$1", account_id
            )
            if owner != uid:
                return _err("Это не ваш ресурс", 403)
            rows = await pool.fetch(
                """
                SELECT op_type, outcome, COUNT(*) AS cnt,
                       AVG(flood_wait_s) AS avg_flood, AVG(duration_ms) AS avg_dur
                FROM op_telemetry
                WHERE account_id=$1 AND created_at > NOW() - INTERVAL '24 hours'
                GROUP BY op_type, outcome ORDER BY cnt DESC LIMIT 20
                """,
                account_id,
            )
            return _json_resp([dict(r) for r in rows])
        except Exception:
            log.exception("physics_account_telemetry uid=%d acc=%d", uid, account_id)
            return _err(_INTERNAL_ERROR, 500)

    # ── Graph Hub ─────────────────────────────────────────────────────────────

    async def graph_stats(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            # Filter via owner's known channels (graph_nodes has no owner_id)
            stats = await pool.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM graph_nodes gn
                     WHERE EXISTS (
                         SELECT 1 FROM managed_channels mc
                         WHERE mc.owner_id=$1
                           AND (mc.username = gn.username OR mc.channel_id::text = gn.entity_id)
                     )) AS nodes,
                    (SELECT COUNT(*) FROM graph_edges ge
                     JOIN graph_nodes na ON na.id = ge.from_node
                     WHERE EXISTS (
                         SELECT 1 FROM managed_channels mc
                         WHERE mc.owner_id=$1
                           AND (mc.username = na.username OR mc.channel_id::text = na.entity_id)
                     )) AS edges,
                    (SELECT COUNT(*) FROM audience_overlaps ao
                     JOIN graph_nodes na ON na.id = ao.node_a
                     WHERE ao.overlap_pct > 0.1
                       AND EXISTS (
                           SELECT 1 FROM managed_channels mc
                           WHERE mc.owner_id=$1
                             AND (mc.username = na.username OR mc.channel_id::text = na.entity_id)
                       )) AS strong_overlaps
                """,
                uid,
            )
            return _json_resp(dict(stats) if stats else {"nodes": 0, "edges": 0, "strong_overlaps": 0})
        except Exception:
            log.exception("graph_stats uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def graph_overlaps(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """
                SELECT ao.overlap_pct, ao.shared_users, ao.computed_at,
                       na.title AS title_a, na.username AS username_a,
                       nb.title AS title_b, nb.username AS username_b
                FROM audience_overlaps ao
                JOIN graph_nodes na ON na.id = ao.node_a
                JOIN graph_nodes nb ON nb.id = ao.node_b
                WHERE ao.overlap_pct > 0.05
                  AND EXISTS (
                      SELECT 1 FROM managed_channels mc
                      WHERE mc.owner_id=$1
                        AND (mc.username = na.username OR mc.username = nb.username
                             OR mc.channel_id::text = na.entity_id OR mc.channel_id::text = nb.entity_id)
                  )
                ORDER BY ao.overlap_pct DESC LIMIT 20
                """,
                uid,
            )
            return _json_resp([dict(r) for r in rows])
        except Exception:
            log.exception("graph_overlaps uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    # ── Compliance Hub ────────────────────────────────────────────────────────

    async def compliance_overview(request: web.Request) -> web.Response:
        """Шапка и записи журнала соответствия.

        Словарь исходов — в `compliance_engine`: держать его в тексте запроса
        уже стоило экрану правды (считались `success` / `ban` / `flood_wait`,
        которых не пишет никто, и все счётчики были нулями при полном журнале).
        Срез по группе исхода и страницы — чтобы «Рисковых 12» вело к этим
        двенадцати, а не оставалось числом.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        group = (request.query.get("group") or "").strip().lower()
        if group not in ("ok", "partial", "risk", "neutral"):
            group = ""
        limit = _list_limit(request, 20, 100)
        try:
            offset = max(0, int(request.query.get("offset", "0")))
        except (TypeError, ValueError):
            offset = 0
        try:
            from services import compliance_engine
            totals = await compliance_engine.get_totals(pool, uid)
            recent = await compliance_engine.get_recent(
                pool, uid, limit=limit, offset=offset, group=group or None)
            recent_total = await compliance_engine.count_recent(
                pool, uid, group=group or None)
            # Подписанный отчёт за 30 дней (успешность, охват типов/аккаунтов).
            report = await compliance_engine.get_report(pool, uid, days=30)
            return _json_resp({
                "totals": totals,
                "recent": recent,
                "recent_total": recent_total,
                "group": group,
                "report": report or {},
            })
        except Exception:
            log.exception("compliance_overview uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def compliance_export(request: web.Request) -> web.Response:
        """Текстовый отчёт соответствия за период (для выгрузки/аудита)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            days = int(request.query.get("days", "30"))
        except (TypeError, ValueError):
            days = 30
        days = max(1, min(days, 365))
        try:
            from services import compliance_engine
            text = await compliance_engine.export_text(pool, uid, days=days)
            return _json_resp({"text": text, "days": days})
        except Exception:
            log.exception("compliance_export uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    # ── Content Cloner ───────────────────────────────────────────────────────

    async def content_cloner_history(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                "SELECT id, op_type, status, params, COALESCE(label, op_type) AS label, created_at FROM operation_queue "
                "WHERE owner_id=$1 AND op_type='content_clone' ORDER BY created_at DESC LIMIT 20",
                uid,
            )
            return _json_resp([dict(r) for r in rows])
        except Exception:
            log.exception("content_cloner_history uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def content_cloner_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        source = (body.get("source") or "").strip()
        if not source:
            return _err("Нужен источник")
        account_id = body.get("account_id")
        # Resolve the worker's contract here: _exec_content_clone expects source_ref,
        # a list of target channels and an explicit account list. The Mini App only
        # collects a source, so derive the rest — clone the source channel's recent
        # posts into the user's own managed channels using an active account.
        if account_id:
            try:
                account_ids = [int(account_id)]
            except (TypeError, ValueError):
                account_ids = []
        else:
            acc_rows = await _safe_fetch(pool,
                "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE "
                "AND session_str IS NOT NULL "
                "AND " + _acc_status.sql_not_dead() + " "
                "ORDER BY last_used DESC NULLS LAST LIMIT 1", uid)
            account_ids = [r["id"] for r in acc_rows]
        if not account_ids:
            return _err("Нет активного аккаунта с сессией для клонирования", 400)
        # Куда клонировать. Экран отправлял только источник, и операция уходила
        # ВО ВСЕ каналы владельца разом, молча: выбора на экране не было, а
        # публикация наружу — необратима. Теперь цели можно назвать явно, и
        # «во все» остаётся только как осознанный выбор на экране.
        want_ids = [i for i in (
            validate_integer(x, min_val=-(2 ** 63), max_val=2 ** 63 - 1)
            for x in (body.get("channel_ids") or [])[:500]) if i is not None]
        if want_ids:
            chan_rows = await _safe_fetch(pool,
                "SELECT username, channel_id FROM managed_channels "
                "WHERE owner_id=$1 AND channel_id = ANY($2::bigint[])", uid, want_ids)
            if len(chan_rows or []) != len(set(want_ids)):
                return _err("Среди выбранных каналов есть чужой или удалённый", 400)
        else:
            chan_rows = await _safe_fetch(pool,
                "SELECT username, channel_id FROM managed_channels WHERE owner_id=$1", uid)
        target_refs = [
            ("@" + r["username"]) if r["username"] else r["channel_id"]
            for r in chan_rows
        ]
        if not target_refs:
            return _err("Нет управляемых каналов — добавьте канал, куда клонировать контент", 400)
        msg_count = validate_integer(body.get("msg_count", 10), min_val=1, max_val=100) or 10
        try:
            op_id = await _obus.submit(
                pool, uid, "content_clone", {
                    "source": source,          # kept for history display (JS reads payload.source)
                    "source_ref": source,      # read by _exec_content_clone
                    "target_refs": target_refs,
                    "account_ids": account_ids,
                    "mode": "forward",
                    "msg_count": msg_count,
                },
                total_items=len(target_refs),
                label=f"Клонировать контент: {source} → {len(target_refs)} канал(ов)")
            return _json_resp({"ok": True, "op_id": op_id})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("content_cloner_submit uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    # ── Clone Adapt ───────────────────────────────────────────────────────────


    # ── Content Mesh ──────────────────────────────────────────────────────────

    async def content_meshes_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """
                SELECT cm.id, cm.name, cm.enabled, cm.source_channel, cm.delay_minutes,
                       COUNT(DISTINCT mt.id) AS targets_count,
                       COUNT(mq.id) FILTER (WHERE mq.status='pending') AS pending_posts,
                       cm.created_at
                FROM content_meshes cm
                LEFT JOIN mesh_targets mt ON mt.mesh_id = cm.id
                LEFT JOIN mesh_queue mq ON mq.mesh_id = cm.id
                WHERE cm.owner_id=$1
                GROUP BY cm.id, cm.name, cm.enabled, cm.source_channel,
                         cm.delay_minutes, cm.created_at
                ORDER BY cm.id DESC
                """,
                uid,
            )
            return _json_resp([dict(r) for r in rows])
        except Exception:
            log.exception("content_meshes_list uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)












    # ── Narrative Engine ──────────────────────────────────────────────────────






    # ── Spintax ──────────────────────────────────────────────────────────────



    # ── Self Promo ───────────────────────────────────────────────────────────









    # ── Semantic Memory ───────────────────────────────────────────────────────




    # ── Audience DNA ─────────────────────────────────────────────────────────





    # ── Auto Funnels ──────────────────────────────────────────────────────────












    # ── SSE ──────────────────────────────────────────────────────────────────

    async def events(request: web.Request) -> web.StreamResponse:
        uid = _get_uid(request)
        if not uid:
            return web.Response(status=401, text="Unauthorized")
        if _sse_streams.get(uid, 0) >= _SSE_MAX_PER_USER:
            log.warning("events: у владельца %s уже %d живых потоков — отказ",
                        uid, _sse_streams.get(uid, 0))
            return web.Response(
                status=429,
                text=(f"Слишком много одновременных подключений к потоку "
                      f"событий (предел {_SSE_MAX_PER_USER}). Закройте лишние "
                      f"вкладки приложения."),
                headers={"Retry-After": "30",
                         "Access-Control-Allow-Origin": "*"})
        response = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        })
        await response.prepare(request)
        # Считаем поток ПОСЛЕ успешного prepare, а не до: если соединение
        # оборвалось на рукопожатии, prepare бросает исключение, до finally
        # ниже дело не доходит, и счёт остался бы навсегда завышенным — пара
        # таких обрывов, и владелец упёрся бы в потолок без единого живого
        # потока. Гонка «два одновременных открытия прошли проверку» здесь
        # безвредна: потолок защищает от сотни соединений, а не от девятого.
        _sse_streams[uid] = _sse_streams.get(uid, 0) + 1

        # Последний отправленный текст по каждому событию. Поток шлёт полные
        # снимки каждые 15 секунд, и раньше они уходили даже когда ничего не
        # изменилось: на мобильной сети это постоянный трафик и постоянные
        # перерисовки экрана на пустом месте. Теперь одинаковый снимок не
        # отправляется повторно — соединение держит keepalive-строка.
        _last_sent: dict[str, str] = {}

        async def push(event: str, data: Any, *, only_if_changed: bool = False) -> None:
            payload = json.dumps(data, ensure_ascii=False, default=str)
            if only_if_changed and _last_sent.get(event) == payload:
                return
            _last_sent[event] = payload
            await response.write(f"event: {event}\ndata: {payload}\n\n".encode())

        async def fetch_activity() -> list:
            try:
                rows = await pool.fetch(
                    """SELECT COALESCE(label, op_type) AS action,
                              status, created_at, done_items, total_items
                       FROM operation_queue WHERE owner_id=$1
                       ORDER BY created_at DESC LIMIT 10""",
                    uid)
                def _map(r):
                    s = r["status"]
                    return {
                        "action": r["action"],
                        "status": ("completed" if s == "done" else "running" if s == "running" else "error" if s == "failed" else s),
                        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                        "detail": (f'{r["done_items"]}/{r["total_items"]}' if (r["total_items"] or 0) > 0 else None),
                    }
                return [_map(r) for r in rows]
            except Exception:
                return []

        async def fetch_op_progress() -> list:
            """Fetch running operations with progress for real-time updates."""
            try:
                rows = await pool.fetch(
                    f"""SELECT id, op_type, COALESCE(label, op_type) AS label,
                              total_items, done_items, status,
                              {op_status.sql_error_reason()} AS error_msg
                       FROM operation_queue WHERE owner_id=$1 AND status='running'
                       ORDER BY created_at DESC""",
                    uid)
                result = []
                for r in rows:
                    total = r["total_items"] or 0
                    done = r["done_items"] or 0
                    pct = int(done * 100 / total) if total > 0 else 0
                    result.append({
                        "id": r["id"],
                        "op_type": r["op_type"],
                        "label": r["label"],
                        "total": total,
                        "done": done,
                        "pct": pct,
                        "status": r["status"],
                    })
                return result
            except Exception:
                return []

        # Завершённые операции, о которых уже сказали. Раньше набор ЦЕЛИКОМ
        # очищался на 500 записях — а запрос ниже возвращает всё, что
        # завершилось за последние 30 минут, поэтому сразу после очистки те же
        # операции уезжали клиенту повторно как «только что завершилась».
        # Теперь вытесняем по одной, самые давние.
        _seen_completed: set[int] = set()
        _seen_order: deque[int] = deque()

        async def fetch_completed_ops() -> list:
            # `partial` — терминальный исход недоведённой работы (op_status.py), и
            # именно он требует от владельца решения. В этом потоке его не было:
            # операция, закрытая «частично», не давала ни плашки итога, ни
            # быстрых действий, ни перечитывания списка — она просто исчезала из
            # прогресса, а итог доходил только в чат бота. Плашка на неполном
            # исходе уже верна: `ok = status === 'done'`, то есть ⚠️ и «Повторить».
            try:
                rows = await pool.fetch(
                    """SELECT id, op_type, COALESCE(label, op_type) AS label,
                              total_items, done_items, status, error_msg,
                              COALESCE(result->>'summary', result->>'reason') AS summary
                       FROM operation_queue WHERE owner_id=$1
                         AND status IN ('done','partial','failed')
                         AND finished_at > now() - make_interval(mins => 30)
                       ORDER BY finished_at DESC LIMIT 20""",
                    uid)
                return [dict(r) for r in rows]
            except Exception:
                return []

        async def completed_quick_actions(op: dict) -> list:
            """Быстрые действия по итогу — только для НОВОГО итога, не каждый такт."""
            try:
                from services import op_chain as _oc, op_quick_actions as _qa

                row = await pool.fetchrow(
                    "SELECT params, result FROM operation_queue WHERE id=$1 AND owner_id=$2",
                    op["id"], uid)
                if not row:
                    return []
                return _qa.suggest(op["op_type"], op["status"], _oc.as_dict(row["result"]),
                                   op_id=op["id"], params=_oc.as_dict(row["params"]))
            except Exception:
                log.debug("miniapp/events: быстрые действия op=%s недоступны", op.get("id"))
                return []

        # Живой поток статистики совпадает по скоупу с дашбордом/экраном
        # «Аккаунты»: owner-scoped по умолчанию, платформа — только ?scope=platform.
        _adm = _wants_platform_scope(request, uid)
        try:
            data = await _stats(pool, uid, _adm)
            await push("stats", data)
            await push("activity", {"items": await fetch_activity()})
            await push("op_progress", {"items": await fetch_op_progress()})
            while True:
                await asyncio.sleep(SSE_TICK_SECONDS)
                # Токен сверяется НЕ только на входе. Поток живёт столько, сколько
                # открыто приложение, а токен — два часа; без повторной проверки
                # соединение, открытое с ещё живым токеном, продолжало отдавать
                # статистику, активность и ход операций и после его истечения.
                # Проверка — HMAC над строкой, без похода в базу.
                if _get_uid(request) != uid:
                    # Клиенту говорим причину: иначе EventSource будет вечно
                    # ломиться с тем же протухшим токеном и получать 401.
                    try:
                        await push("auth_expired", {"reason": "token_expired"})
                    except Exception:
                        pass
                    break
                # Четыре выборки идут ПО ОЧЕРЕДИ, а не одним заходом. Раньше
                # здесь был gather — он ускоряет такт, но такту спешить некуда
                # (раз в SSE_TICK_SECONDS). Зато одновременных соединений он брал
                # вчетверо больше, а такты разных приложений выстраиваются в ряд
                # после каждого передеплоя: все открытые приложения
                # переподключаются одновременно и дальше тикают синхронно. Восемь
                # потоков (потолок на владельца) по четыре соединения — это 32 при
                # пуле в 20, то есть все остальные запросы владельца в этот момент
                # ждут в очереди. Суммарная нагрузка на базу от порядка не
                # меняется, меняется только пик.
                data = await _stats(pool, uid, _adm)
                activity = await fetch_activity()
                progress = await fetch_op_progress()
                completed = await fetch_completed_ops()
                await push("stats", data, only_if_changed=True)
                await push("activity", {"items": activity}, only_if_changed=True)
                await push("op_progress", {"items": progress}, only_if_changed=True)
                for op in completed:
                    op_id = op["id"]
                    if op_id not in _seen_completed:
                        _seen_completed.add(op_id)
                        _seen_order.append(op_id)
                        await push("op_complete", {
                            "id": op_id,
                            "op_type": op["op_type"],
                            "label": op["label"],
                            "status": op["status"],
                            "total": op["total_items"],
                            "done": op["done_items"],
                            "error_msg": op.get("error_msg"),
                            # Итог операции — тот же текст, что уходит в бот.
                            # Без него мини-апп мог сказать только «завершилась»,
                            # но не «что получилось».
                            "summary": op.get("summary"),
                            # Следующий шаг прямо на плашке итога — в т.ч.
                            # связка с другим модулем (services/op_chain).
                            "quick_actions": await completed_quick_actions(op),
                        })
                while len(_seen_order) > 500:
                    _seen_completed.discard(_seen_order.popleft())
                await response.write(b": keepalive\n\n")
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        except Exception:
            log.exception("miniapp/events uid=%d", uid)
        finally:
            # Поток закрылся — освобождаем место под потолком. Именно finally:
            # обычный выход отсюда это отмена задачи (клиент закрыл вкладку), и
            # без finally счётчик бы только рос, пока владелец не упёрся бы в
            # потолок на пустом месте.
            left = _sse_streams.get(uid, 1) - 1
            if left > 0:
                _sse_streams[uid] = left
            else:
                _sse_streams.pop(uid, None)
        return response

    # ── Сокращатель ссылок: публичный редирект ─────────────────────────────────
    #
    # Ручки `/api/miniapp/links/*` уехали в `services/mini_app_links.py`. Здесь
    # остался редирект: он не часть API мини-аппа, а публичный вход по короткому
    # коду, и живёт на корне (`/{code}`) вместе с остальными корневыми путями.
    from services import link_shortener as _lnk

    async def short_link_redirect(request: web.Request) -> web.Response:
        """Публичный редирект: короткая ссылка → 302 на цель + учёт клика."""
        code = request.match_info.get("code", "")
        target = await _lnk.resolve(pool, code)
        if not target:
            return web.Response(status=404, text="Ссылка не найдена или отключена",
                                content_type="text/plain", charset="utf-8")
        raise web.HTTPFound(target)

    # ── Route registration ────────────────────────────────────────────────────

    app.router.add_options("/api/miniapp/{path:.*}", handle_options)
    app.router.add_get("/s/{code}", short_link_redirect)
    app.router.add_post("/api/miniapp/auth", auth)
    app.router.add_post("/api/miniapp/pair", pair_device)
    app.router.add_post("/api/miniapp/pair/exchange", pair_exchange)
    app.router.add_get("/api/miniapp/devices", devices_list)
    app.router.add_post("/api/miniapp/devices/revoke", devices_revoke)
    app.router.add_post("/api/miniapp/factory/name_preview", factory_name_preview)
    # Bots
    app.router.add_post("/api/miniapp/bot_factory/create", bot_factory_create)
    app.router.add_post("/api/miniapp/bot_factory/create_new", bot_factory_create_new)
    app.router.add_post("/api/miniapp/channels/import_all", channels_import_all)
    app.router.add_post("/api/miniapp/groups/import_all", groups_import_all)
    app.router.add_get("/api/miniapp/mass_targets", mass_targets)
    app.router.add_post("/api/miniapp/groups/announce", groups_announce)
    app.router.add_get("/api/miniapp/onboarding", onboarding_status)
    app.router.add_post("/api/miniapp/dm/preview", dm_preview)
    app.router.add_post("/api/miniapp/dm/adhoc_send", dm_adhoc_send)
    app.router.add_post("/api/miniapp/mass_report", mass_report_submit)
    app.router.add_post("/api/miniapp/channels/bulk_edit", channels_bulk_edit)
    app.router.add_post("/api/miniapp/bots/bulk_edit", bots_bulk_edit)
    app.router.add_post("/api/miniapp/accounts/bulk_profile", accounts_bulk_profile)
    app.router.add_put("/api/miniapp/auto_reply/{reply_id}/toggle", toggle_auto_reply)
    app.router.add_delete("/api/miniapp/auto_reply/{reply_id}", delete_auto_reply)
    # Broadcasts
    # Channels
    # Campaigns / Funnels
    app.router.add_get("/api/miniapp/campaigns", campaigns)
    # Accounts
    # Channels
    # Bot subscribers
    # DM campaigns
    # Operations
    # Bot toggle
    # Funnel steps
    # Competitors
    app.router.add_get("/api/miniapp/competitors", competitors_list)
    app.router.add_post("/api/miniapp/competitor", add_competitor)
    app.router.add_delete("/api/miniapp/competitor/{comp_id}", delete_competitor)
    # Network broadcast
    app.router.add_post("/api/miniapp/network_broadcast", network_broadcast)
    # CRM
    # Audience
    # Keywords / Search Rankings
    app.router.add_get("/api/miniapp/keywords", keywords)
    app.router.add_post("/api/miniapp/keyword", add_keyword)
    app.router.add_delete("/api/miniapp/keyword/{kw_id}", delete_keyword)
    # Account Warmup control
    # Schedules
    app.router.add_post("/api/miniapp/schedule/{sch_id}/cancel", cancel_schedule)
    # Templates
    # Mass Publish
    app.router.add_get("/api/miniapp/mass_publish/targets", mass_publish_targets)
    app.router.add_post("/api/miniapp/mass_publish", mass_publish)
    app.router.add_get("/api/miniapp/check_owned_restrictions/targets", check_owned_restrictions_targets)
    app.router.add_post("/api/miniapp/check_owned_restrictions", check_owned_restrictions)
    app.router.add_get("/api/miniapp/check_bot_geoban/targets", check_bot_geoban_targets)
    app.router.add_post("/api/miniapp/check_bot_geoban", check_bot_geoban)
    app.router.add_get("/api/miniapp/editorial/policy", editorial_policy_get)
    app.router.add_put("/api/miniapp/editorial/policy", editorial_policy_save)
    app.router.add_post("/api/miniapp/editorial/review", editorial_review_draft)
    # ── Прокси ───────────────────────────────────────────────────────────────
    mini_app_proxies.setup_routes(app, pool)
    mini_app_uch.setup_routes(app, pool)
    mini_app_bots.setup_routes(app, pool)
    mini_app_invite.setup_routes(app, pool)
    mini_app_channels.setup_routes(app, pool)
    mini_app_operations.setup_routes(app, pool)
    mini_app_dashboard.setup_routes(app, pool)
    mini_app_dm.setup_routes(app, pool)
    mini_app_warmup.setup_routes(app, pool)
    mini_app_accounts.setup_routes(app, pool)
    mini_app_links.setup_routes(app, pool)
    mini_app_deals_board.setup_routes(app, pool)
    mini_app_fleet.setup_routes(app, pool)
    mini_app_rankings.setup_routes(app, pool)
    mini_app_sales.setup_routes(app, pool)
    mini_app_networks.setup_routes(app, pool)
    mini_app_mp.setup_routes(app, pool)
    mini_app_cf.setup_routes(app, pool)
    mini_app_content.setup_routes(app, pool)
    mini_app_presence.setup_routes(app, pool)
    mini_app_cloud.setup_routes(app, pool)
    mini_app_va.setup_routes(app, pool)
    mini_app_monitor.setup_routes(app, pool)
    # Analytics
    # Subscription
    app.router.add_get("/api/miniapp/subscription", subscription)
    # Settings
    app.router.add_get("/api/miniapp/settings", user_settings_get)
    app.router.add_post("/api/miniapp/settings", user_settings_save)
    # Payments history
    app.router.add_get("/api/miniapp/payments", payments_history)
    app.router.add_get("/api/miniapp/referral", referral)
    # Deeplinks
    app.router.add_delete("/api/miniapp/deeplink/{link_id}", delete_deeplink)
    # Engagement segments
    # Bot Stats (detailed)
    # Profile Setter
    app.router.add_get("/api/miniapp/profile_setter/status", profile_setter_status)
    app.router.add_post("/api/miniapp/profile_setter", profile_setter_submit)
    # Bot Notes
    # Bot Commands
    # Account Shield
    app.router.add_get("/api/miniapp/shield", shield_summary)
    app.router.add_post("/api/miniapp/shield/config", shield_config_save)
    # Ad Intelligence
    app.router.add_get("/api/miniapp/ad_intel", ad_intel_overview)
    app.router.add_post("/api/miniapp/ad_intel/channel", ad_intel_add_channel)
    # Network / Cluster
    # Relay (Inbox)
    app.router.add_get("/api/miniapp/relay/session/{session_id}/messages", relay_session_messages)
    # API Keys
    app.router.add_get("/api/miniapp/api_keys", api_keys_list)
    app.router.add_post("/api/miniapp/api_key", create_api_key)
    app.router.add_delete("/api/miniapp/api_key/{key_id}", revoke_api_key)
    # Strike history
    # Host-Server: маркетплейс инфраструктуры вынесен в свой модуль.
    mini_app_host_server.setup_routes(app, pool)
    # Audience Parser
    # CRM Deals
    # Workspaces
    # Promo Platform
    # Error Reports
    app.router.add_post("/api/miniapp/error_report", submit_error_report)
    app.router.add_get("/api/miniapp/error_reports", my_error_reports)
    # Account Cleaner
    app.router.add_get("/api/miniapp/cleaner/accounts", cleaner_accounts)
    app.router.add_post("/api/miniapp/cleaner/submit", cleaner_submit)
    # DM Campaigns
    app.router.add_get("/api/miniapp/dm_campaigns", dm_campaigns_list)
    app.router.add_post("/api/miniapp/dm_campaigns", dm_campaign_create)

    # ── Единый модуль рассылок (Кампании) ─────────────────────────────────────
    # Один запуск по каналам/чатам/ботам/ДМ сразу: спек веером раздаётся
    # существующим движкам под общим campaign_id (services/campaign_hub).


    # Account Warmup
    app.router.add_get("/api/miniapp/vlayer/overview", vlayer_overview)
    app.router.add_get("/api/miniapp/vlayer/entity/{entity_type}/{entity_id}",
                       vlayer_entity)
    app.router.add_post("/api/miniapp/vlayer/entity/{entity_type}/{entity_id}/purchased",
                        vlayer_mark_purchased)
    app.router.add_get("/api/miniapp/vlayer/analytics", vlayer_analytics)
    app.router.add_get("/api/miniapp/vlayer/segment/{value}", vlayer_segment)
    app.router.add_get("/api/miniapp/rehab", rehab_overview_ep)
    app.router.add_post("/api/miniapp/rehab/{acc_id}/{action}", rehab_action)
    # A/B Experiments
    # Health Dashboard
    app.router.add_get("/api/miniapp/health", health_overview)
    # Topology Map

    # ── Provider Marketplace (блок 1: провайдеры + каталог) ──────────────────
    from services import provider_marketplace as _mp
















    # ── Sales Persona (сущность бота — живой менеджер по продажам) ────────────
    from services import bot_sales_persona as _bsp















    # ── База знаний (FAQ) ──────────────────────────────────────────────────



    # ── Эталонные примеры (few-shot) ───────────────────────────────────────


    # ── Способы доставки ───────────────────────────────────────────────────


    # ── Промокоды ──────────────────────────────────────────────────────────












    # Infra Analytics
    app.router.add_get("/api/miniapp/infra", infra_analytics_overview)
    # Reporter
    app.router.add_get("/api/miniapp/diag", diag)
    app.router.add_get("/api/miniapp/new_users", new_users)
    app.router.add_get("/api/miniapp/new_users/export", new_users_export)
    app.router.add_get("/api/miniapp/platform_users", platform_new_users)
    app.router.add_get("/api/miniapp/platform_users/export", platform_new_users_export)
    # Алиас: фронт (autoregProCheckAll) зовёт /accounts/check_all (был 404);
    # accounts_check и так проверяет ВСЕ аккаунты владельца.
    app.router.add_post("/api/miniapp/global_search", global_search)
    app.router.add_post("/api/miniapp/find_contact", find_contact)
    # Живая консоль аккаунта: диалоги / история / отправка / контакты.
    mini_app_vault.setup_routes(app, pool)
    app.router.add_post("/api/miniapp/boost", boost_submit)
    app.router.add_post("/api/miniapp/ai_comment", ai_comment_submit)
    app.router.add_post("/api/miniapp/compliance_scan", compliance_scan_submit)
    app.router.add_post("/api/miniapp/reporter", reporter_submit)
    # Quick Post
    # SEO
    app.router.add_get("/api/miniapp/ai/status", ai_status)
    app.router.add_post("/api/miniapp/ai/test", ai_test)
    # Bot Factory
    app.router.add_get("/api/miniapp/bot_factory", bot_factory_status)
    # Persona Hub
    app.router.add_get("/api/miniapp/personas", persona_list)
    app.router.add_post("/api/miniapp/persona", persona_create)
    app.router.add_put("/api/miniapp/persona/{persona_id}/toggle", persona_toggle)
    app.router.add_delete("/api/miniapp/persona/{persona_id}", persona_delete)
    # Auto Registrar
    app.router.add_get("/api/miniapp/autoreg", autoreg_status)
    app.router.add_post("/api/miniapp/autoreg", autoreg_submit)
    # Phone Checker
    app.router.add_post("/api/miniapp/phone_check", phone_check_submit)
    # Referral
    app.router.add_get("/api/miniapp/referral/detail", referral_overview_detail)
    # AI Memory
    # Nodes Hub
    app.router.add_get("/api/miniapp/nodes", nodes_list)
    app.router.add_post("/api/miniapp/node", node_create)
    app.router.add_delete("/api/miniapp/node/{node_id}", node_delete)
    app.router.add_get("/api/miniapp/node/{node_id}/threads", node_threads)
    # Ноды-комьюнити: mini-Discord для аудитории — вынесено в свой модуль.
    mini_app_community.setup_routes(app, pool)
    mini_app_funnels.setup_routes(app, pool)
    mini_app_admin.setup_routes(app, pool)
    mini_app_broadcast.setup_routes(app, pool)
    mini_app_stars.setup_routes(app, pool)
    mini_app_protection.setup_routes(app, pool)
    mini_app_growth.setup_routes(app, pool)
    mini_app_parser.setup_routes(app, pool)
    mini_app_workspace.setup_routes(app, pool)
    # Gift Transfer
    # Mass Inviter
    app.router.add_post("/api/miniapp/mass_invite", mass_inviter_submit)
    # Stars Hub
    # Ghost Engine
    # Bot Webhook
    # Reg Checker
    app.router.add_get("/api/miniapp/reg_check/history", reg_check_history)
    app.router.add_post("/api/miniapp/reg_check", reg_check_submit)
    # Asset Templates
    app.router.add_get("/api/miniapp/asset_templates", asset_templates_list)
    # Infra Health Center
    app.router.add_get("/api/miniapp/infra_health", infra_health_overview)
    app.router.add_post("/api/miniapp/infra_health/anomaly/{anomaly_id}/resolve", infra_health_resolve)
    # Swarm
    app.router.add_get("/api/miniapp/swarm", swarm_metrics)
    # Presence Packs
    app.router.add_get("/api/miniapp/presence_packs", presence_packs_list)
    # Global Presence
    app.router.add_get("/api/miniapp/geo_presets", geo_presets)
    app.router.add_post(
        "/api/miniapp/global_presence/{plan_id}/bulk_apply", global_presence_bulk_apply
    )
    # Mass Ops
    app.router.add_get("/api/miniapp/mass_ops", mass_ops_overview)
    # Ecosystems
    # Channel Factory
    app.router.add_post("/api/miniapp/channel_factory/submit", channel_factory_submit)
    app.router.add_get("/api/miniapp/channel_factory/recent", channel_factory_recent)
    # Physics Hub
    app.router.add_get("/api/miniapp/physics", physics_overview)
    app.router.add_get("/api/miniapp/physics/{account_id}/telemetry", physics_account_telemetry)
    # Graph Hub
    app.router.add_get("/api/miniapp/graph", graph_stats)
    app.router.add_get("/api/miniapp/graph/overlaps", graph_overlaps)
    # Compliance Hub
    app.router.add_get("/api/miniapp/compliance", compliance_overview)
    app.router.add_get("/api/miniapp/compliance/export", compliance_export)
    # Content Cloner
    app.router.add_get("/api/miniapp/content_cloner/history", content_cloner_history)
    app.router.add_post("/api/miniapp/content_cloner/submit", content_cloner_submit)
    # Clone Adapt
    # Content Mesh
    app.router.add_get("/api/miniapp/content_meshes", content_meshes_list)
    # Narrative Engine
    # Spintax
    # Self Promo
    # Semantic Memory
    # Audience DNA
    # Auto Funnels
    # ── Circuit Breaker & Adaptive Pacing ─────────────────────────────────────
    async def circuit_breaker_status(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services import op_worker as _opw
            # Общее состояние из БД: роль web не запускает операции, и в её
            # памяти счётчик всегда пуст — читая кэш, мини-апп показывал бы
            # «всё в порядке», пока воркер стоит на паузе.
            status = await _opw.circuit_breaker_status(uid)
            return _json_resp(status)
        except Exception:
            log.exception("circuit_breaker_status")
            return _err(_INTERNAL_ERROR, 500)


    async def ecosystem_recommendations(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services import ecosystem_brain as _eb
            recs = await _eb.get_ecosystem_recommendations(pool, uid)
            return _json_resp({"recommendations": recs})
        except Exception:
            log.exception("ecosystem_recommendations")
            return _err(_INTERNAL_ERROR, 500)


    async def next_actions(request: web.Request) -> web.Response:
        """Copilot «Что делать дальше» — контекстные подсказки следующего шага
        на реальном состоянии владельца (аккаунты/прокси/операции/боты/аудитория)
        и его последних операциях. Каждая подсказка несёт ссылку в нужный раздел
        мини-аппа, чтобы не искать его вручную."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services import next_actions as _na
            try:
                limit = int(request.query.get("limit", "5"))
            except (TypeError, ValueError):
                limit = 5
            limit = max(1, min(limit, 8))
            actions = await _na.compute_next_actions(pool, uid, limit=limit)
            return _json_resp({"actions": actions})
        except Exception as e:
            # Копайлот — вспомогательный: его сбой не должен ломать главный экран.
            log.warning("next_actions endpoint failed uid=%s: %s", uid, e)
            return _json_resp({"actions": []})

    async def next_actions_apply(request: web.Request) -> web.Response:
        """Применить подсказку Copilot в один клик. Клиент присылает только `id`
        подсказки — сервер сам решает, какое безопасное owner-scoped действие
        выполнить (защита от вызова произвольных операций с клиента)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        action_id = (body.get("id") or "").strip()
        try:
            result = await _apply_next_action(pool, uid, action_id)
            if result.get("error"):
                return _err(result["error"], 400)
            return _json_resp(result)
        except PermissionError as exc:
            # Нет подписки или предохранитель Ban Weather — честный отказ с
            # причиной, а не «не удалось».
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("next_actions_apply id=%s uid=%s", action_id, uid)
            return _err("Не удалось применить действие", 500)

    async def op_chain_launch(request: web.Request) -> web.Response:
        """Запустить связку «итог операции → следующий модуль» в одно нажатие.

        Клиент присылает только номер исходной операции и id шага; цели шаг
        берёт из сохранённого итога этой операции владельца (services/op_chain),
        та же функция стоит за кнопкой в уведомлении бота.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            op_id = int(body.get("op_id") or 0)
        except (TypeError, ValueError):
            op_id = 0
        step = str(body.get("step") or "").strip()[:32]
        if op_id <= 0 or not step:
            return _err("Не указаны операция и шаг", 400)
        from services import op_chain as _oc

        r = await _oc.launch(pool, uid, op_id, step)
        if not r.get("ok"):
            return _err(r.get("reason") or "Шаг не запустился", 409)
        return _json_resp(r)

    async def dashboard_realtime(request: web.Request) -> web.Response:
        """Analytics Dashboard — операторская сводка на РЕАЛЬНЫХ данных, которые
        система собирает: аккаунты, каналы, аудитория ботов, операции + дневные
        тайм-серии (аудитория/операции/успешность из bot_users и operation_audit).
        Просмотров/подписчиков каналов система не собирает — этих метрик тут нет
        (не выдумываем), вместо них показываем реальную операционную активность."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rng = request.query.get("range", "7d")
        days = {"24h": 1, "7d": 7, "30d": 30, "90d": 90}.get(rng, 7)
        try:
            # ── KPI (реальные) ──────────────────────────────────────────────
            acc = await _safe_fetchrow(pool,
                "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE is_active) AS active "
                "FROM tg_accounts WHERE owner_id=$1", uid)
            acc_total = int(acc["total"]) if acc else 0
            acc_active = int(acc["active"]) if acc else 0
            ch_total = int(await _safe_fetchval(pool,
                "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", uid) or 0)
            aud_row = await _safe_fetchrow(pool,
                """SELECT COUNT(DISTINCT bu.user_id) AS total,
                          COUNT(DISTINCT bu.user_id) FILTER (
                              WHERE bu.first_seen > NOW() - INTERVAL '7 days') AS new_7d
                   FROM bot_users bu JOIN managed_bots mb ON mb.bot_id=bu.bot_id
                   WHERE mb.added_by=$1 AND bu.is_active=TRUE""", uid)
            aud_total = int(aud_row["total"]) if aud_row else 0
            aud_new_7d = int(aud_row["new_7d"]) if aud_row else 0
            ops_row = await _safe_fetchrow(pool,
                """SELECT COUNT(*) AS total,
                          COUNT(*) FILTER (WHERE action ILIKE '%post%') AS posts
                   FROM operation_audit WHERE owner_id=$1
                     AND occurred_at > NOW() - ($2 * INTERVAL '1 day')""", uid, days)
            ops_total = int(ops_row["total"]) if ops_row else 0
            ops_posts = int(ops_row["posts"]) if ops_row else 0

            # ── Дневные тайм-серии (реальные, с заполнением пропусков нулями) ──
            def _fill(rows, key="d", val="c"):
                by_day = {}
                for r in (rows or []):
                    dd = r[key]
                    if dd is not None:
                        by_day[dd.date() if hasattr(dd, "date") else dd] = float(r[val] or 0)
                from datetime import date, timedelta
                today = date.today()
                out = []
                for i in range(days - 1, -1, -1):
                    out.append({"value": by_day.get(today - timedelta(days=i), 0)})
                return out
            aud_series = await _safe_fetch(pool,
                """SELECT date_trunc('day', bu.first_seen) AS d, COUNT(*) AS c
                   FROM bot_users bu JOIN managed_bots mb ON mb.bot_id=bu.bot_id
                   WHERE mb.added_by=$1 AND bu.first_seen > NOW() - ($2 * INTERVAL '1 day')
                   GROUP BY 1 ORDER BY 1""", uid, days)
            ops_series = await _safe_fetch(pool,
                """SELECT date_trunc('day', occurred_at) AS d, COUNT(*) AS c
                   FROM operation_audit WHERE owner_id=$1
                     AND occurred_at > NOW() - ($2 * INTERVAL '1 day')
                   GROUP BY 1 ORDER BY 1""", uid, days)
            succ_series = await _safe_fetch(pool,
                """SELECT date_trunc('day', occurred_at) AS d,
                          ROUND(100.0*COUNT(*) FILTER (WHERE result='success')
                                /NULLIF(COUNT(*),0), 1) AS c
                   FROM operation_audit WHERE owner_id=$1
                     AND occurred_at > NOW() - ($2 * INTERVAL '1 day')
                   GROUP BY 1 ORDER BY 1""", uid, days)

            # ── Топ каналов по операционной активности (реально) ──────────────
            top_rows = await _safe_fetch(pool,
                """SELECT mc.title, mc.username, mc.channel_id,
                          (SELECT COUNT(*) FROM operation_audit oa
                           WHERE oa.owner_id=$1 AND oa.target IS NOT NULL
                             AND (oa.target = mc.username OR oa.target = mc.channel_id::text)
                          ) AS activity
                   FROM managed_channels mc WHERE mc.owner_id=$1
                   ORDER BY activity DESC, mc.added_at DESC LIMIT 10""", uid)
            top_channels = [{
                "name": r["title"] or r["username"] or str(r["channel_id"]),
                "username": r["username"] or "",
                "subscribers": int(r["activity"] or 0),  # операц. активность канала
                "views": 0, "growth": 0,
            } for r in (top_rows or [])]

            act_rows = await _safe_fetch(pool,
                "SELECT action, result, target, occurred_at FROM operation_audit "
                "WHERE owner_id=$1 ORDER BY occurred_at DESC LIMIT 15", uid)
            def _atype(a: str) -> str:
                a = (a or "").lower()
                if "post" in a: return "post"
                if "join" in a or "sub" in a or "invite" in a: return "sub"
                if "leave" in a or "unsub" in a: return "unsub"
                return "other"
            recent_activity = [{
                "type": _atype(r["action"]),
                "text": f"{r['action']}"
                        + (f" → {r['target']}" if r["target"] else "")
                        + (f" ({r['result']})" if r["result"] and r["result"] != "success" else ""),
                "timestamp": r["occurred_at"].isoformat() if r["occurred_at"] else None,
            } for r in (act_rows or [])]

            return _json_resp({
                # KPI (реальные) — фронт-плитки перелейблены под них
                "total_subscribers": aud_total,      # 👥 Аудитория
                "total_channels": ch_total,          # 📡 Каналы
                "total_posts": ops_posts,            # 📝 Посты (постинг-операции)
                "total_accounts": acc_total,         # 📱 Аккаунты
                "active_accounts": acc_active,
                "total_operations": ops_total,       # ⚡ Операций за период
                "total_views": ops_total,            # (совместимость со старой плиткой)
                "growth_7d": aud_new_7d,             # 📈 Новая аудитория 7д
                # Тайм-серии (реальные, заполнены по дням)
                "subs_history": _fill(aud_series),          # Аудитория/день
                "views_history": _fill(ops_series),         # Операции/день
                "engagement_history": _fill(succ_series),   # Успешность %/день
                "top_channels": top_channels,
                "recent_activity": recent_activity,
                "range": rng,
                # Приборный щиток организма: пульс здоровья + SEO + гео (реальные,
                # fail-soft — сбой любого органа не роняет дашборд).
                "account_health": await _account_health_summary(uid),
                "seo": await _seo_vitals(uid),
                "geo": await _geo_vitals(uid),
            })
        except Exception:
            log.exception("dashboard_realtime uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def _seo_vitals(uid: int) -> dict:
        """SEO-орган: ключи на отслеживании + непринятые авто-подсказки реоптимизации
        (выход decision-фазы ranking-петли: ranking_checker→bot_reoptimizer)."""
        out = {"tracked_keywords": 0, "pending_suggestions": 0}
        try:
            out["tracked_keywords"] = int(await _safe_fetchval(pool,
                "SELECT COUNT(*) FROM tracked_keywords WHERE owner_id=$1 AND is_active=TRUE",
                uid) or 0)
        except Exception:
            pass
        try:
            out["pending_suggestions"] = int(await _safe_fetchval(pool,
                "SELECT COUNT(*) FROM bot_seo_suggestions "
                "WHERE owner_id=$1 AND applied_at IS NULL", uid) or 0)
        except Exception:
            pass
        return out

    async def _geo_vitals(uid: int) -> dict:
        """Гео-орган: планы глобального присутствия (draft/running/done)."""
        try:
            row = await _safe_fetchrow(pool,
                "SELECT COUNT(*) AS total, "
                "COUNT(*) FILTER (WHERE status='done') AS done, "
                "COUNT(*) FILTER (WHERE status='running') AS running "
                "FROM global_presence_plans WHERE owner_id=$1", uid)
            return {"plans": int(row["total"]) if row else 0,
                    "done": int(row["done"]) if row else 0,
                    "running": int(row["running"]) if row else 0}
        except Exception:
            return {"plans": 0, "done": 0, "running": 0}


    async def _account_health_summary(uid: int) -> dict:
        """Сводка риск-пульса — делегат к модульной `account_health_summary`."""
        return await account_health_summary(pool, uid)


    async def audience_analytics(request: web.Request) -> web.Response:
        """Audience Analytics — owner-агрегат аудитории ботов в форме экрана
        s-audience-analytics. Сегменты — по активности пользователей ботов
        владельца; heatmap — по часам активности (реальные данные из bot_users)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            total = await _safe_fetchval(pool,
                """SELECT COUNT(DISTINCT bu.user_id) FROM bot_users bu
                   JOIN managed_bots mb ON mb.bot_id=bu.bot_id
                   WHERE mb.added_by=$1 AND bu.is_active=TRUE""", uid) or 0
            active = await _safe_fetchval(pool,
                """SELECT COUNT(DISTINCT bu.user_id) FROM bot_users bu
                   JOIN managed_bots mb ON mb.bot_id=bu.bot_id
                   WHERE mb.added_by=$1 AND bu.is_active=TRUE
                     AND bu.last_seen > NOW() - INTERVAL '7 days'""", uid) or 0
            total = int(total); active = int(active)
            new_7d = await _safe_fetchval(pool,
                """SELECT COUNT(DISTINCT bu.user_id) FROM bot_users bu
                   JOIN managed_bots mb ON mb.bot_id=bu.bot_id
                   WHERE mb.added_by=$1 AND bu.first_seen > NOW() - INTERVAL '7 days'""", uid) or 0
            dormant = max(total - active, 0)
            # Сегменты считает services/audience_analytics одним запросом по
            # всем ботам владельца: «Лидеры», «Постоянные», «Под риском»,
            # «Спящие», «Новые» — с порогами по числу сообщений, а не просто
            # «активен / не активен». Раньше экран показывал три грубые доли,
            # посчитанные тут же, а настоящая сегментация лежала мёртвым кодом:
            # её запросы падали на несуществующей колонке.
            _COLORS = {"Лидеры": "#34c759", "Постоянные": "#0a84ff",
                       "Под риском": "#ff9f0a", "Спящие": "#8e8e93",
                       "Новые": "#af52de"}
            segments = []
            try:
                from services.audience_analytics import segment_audience
                from services.audience_analytics import SEGMENT_NAMES
                # Ключ сегмента нужен экрану, чтобы спросить «кто в нём»:
                # по русскому имени маршрут искать нельзя.
                _key_by_name = {v: k for k, v in SEGMENT_NAMES.items()}
                for sg in await segment_audience(pool, uid):
                    segments.append({
                        "key": _key_by_name.get(sg.name),
                        "name": sg.name, "count": sg.user_count,
                        "percentage": sg.percentage, "growth_pct": None,
                        "avg_activity": (f"{sg.avg_engagement:.0f} сообщ."
                                         if sg.avg_engagement else ""),
                        "color": _COLORS.get(sg.name, "#0a84ff"),
                        "description": sg.description,
                    })
            except Exception:
                log.exception("audience_analytics segments uid=%s", uid)
            avg_eng = round(active / total * 100, 1) if total else 0.0
            hm_rows = await _safe_fetch(pool,
                """SELECT EXTRACT(HOUR FROM bu.last_seen)::int AS h, COUNT(*) AS c
                   FROM bot_users bu JOIN managed_bots mb ON mb.bot_id=bu.bot_id
                   WHERE mb.added_by=$1 AND bu.last_seen IS NOT NULL
                   GROUP BY 1 ORDER BY 1""", uid)
            heatmap = [{"hour": int(r["h"]), "value": int(r["c"])} for r in (hm_rows or [])]
            insights = []
            if total:
                # Ключ `description`, а не `text`: экран читает именно его,
                # и из-за расхождения каждый инсайт показывался одним
                # заголовком с пустой строкой под ним.
                insights.append({"type": "activity", "impact": "neutral",
                    "title": "Аудитория", "description":
                    f"Всего {total}, активных за 7 дней — {active} "
                    f"({str(avg_eng).replace('.', ',')}%)."})
                if dormant:
                    insights.append({"type": "churn", "impact": "negative",
                        "title": "Реактивация", "description":
                        f"{dormant} спящих — кандидаты на прогрев или рассылку."})
            if segments:
                _in_seg = sum(sg["count"] for sg in segments)
                insights.append({"type": "segment", "impact": "neutral",
                    "title": "Как считаются сегменты", "description":
                    f"По {_in_seg} подписчикам, у которых есть записанная "
                    f"активность. Доли внутри сегментов считаются от них, а не "
                    f"от общего числа."})
            return _json_resp({
                "total_users": total, "active_users": active,
                "avg_engagement": avg_eng, "segments": segments,
                "insights": insights, "heatmap": heatmap,
            })
        except Exception:
            log.exception("audience_analytics uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def audience_segment_members(request: web.Request) -> web.Response:
        """Кто именно в сегменте аудитории.

        Экран показывал «Спящие · 1 240» и на этом заканчивался: ни увидеть
        этих людей, ни забрать их номера для рассылки было нечем.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services import audience_analytics as _aa
        key = validate_string(request.query.get("key"), 32, required=False) or ""
        if key not in _aa.SEGMENT_PREDICATES:
            return _err("Неизвестный сегмент", 400)
        limit = _list_limit(request, 100, 500)
        rows = await _aa.segment_members(pool, uid, key, limit)
        # Экран существует, чтобы забрать номера сегмента для рассылки. Без
        # полного числа и список, и кнопка «Скопировать номера» молчали о
        # потолке: в буфер уезжала первая сотня из тысячи с лишним.
        total = await _aa.segment_member_count(pool, uid, key)
        ids = [int(r["user_id"]) for r in rows if r.get("user_id") is not None]
        names: dict[int, dict] = {}
        if ids:
            # Имя подписчика лежит в bot_users; скоуп по ботам владельца,
            # иначе подтянулось бы имя из чужого бота.
            nr = await _safe_fetch(
                pool,
                """SELECT DISTINCT ON (bu.user_id) bu.user_id, bu.first_name, bu.username
                     FROM bot_users bu
                     JOIN managed_bots mb ON mb.bot_id = bu.bot_id
                    WHERE mb.added_by=$1 AND bu.user_id = ANY($2::bigint[])
                 ORDER BY bu.user_id, bu.last_seen DESC NULLS LAST""",
                uid, ids)
            names = {int(r["user_id"]): dict(r) for r in (nr or [])}
        items = []
        for r in rows:
            uid_ = int(r["user_id"]) if r.get("user_id") is not None else None
            n = names.get(uid_, {})
            items.append({
                "user_id": uid_,
                "name": n.get("first_name"),
                "username": n.get("username"),
                "msgs": int(r.get("msgs") or 0),
                "last_seen": r["last_seen"].isoformat() if r.get("last_seen") else None,
            })
        return _json_resp({"key": key, "name": _aa.SEGMENT_NAMES.get(key, key),
                           "items": items, "shown": len(items),
                           "total": total, "limit": limit})













    app.router.add_get("/api/miniapp/circuit_breaker", circuit_breaker_status)
    app.router.add_get("/api/miniapp/dashboard_realtime", dashboard_realtime)
    # Регистрация ПОСЛЕ определения обработчика: setup_routes — обычная функция,
    # ссылка на ещё не созданную локальную функцию роняет сборку всего сервера
    # (так 2026-09-04 лёг мини-апп целиком).
    app.router.add_get("/api/miniapp/next_actions", next_actions)
    app.router.add_post("/api/miniapp/next_actions/apply", next_actions_apply)
    app.router.add_post("/api/miniapp/op_chain/launch", op_chain_launch)
    app.router.add_get("/api/miniapp/audience_analytics", audience_analytics)
    app.router.add_get("/api/miniapp/audience_analytics/segment", audience_segment_members)
    app.router.add_get("/api/miniapp/ecosystem_recommendations", ecosystem_recommendations)

    # Team & Audit
    async def team_members(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            # Только команда самого пользователя: он сам + участники рабочих
            # пространств, которыми он владеет или в которых состоит.
            # НЕЛЬЗЯ отдавать весь список platform_users — это утечка данных.
            # platform_users не имеет created_at/last_active_at — реальные колонки
            # registered_at/last_seen (schema_v39). Раньше этот запрос падал
            # `column "created_at" does not exist` и экран «Команда» отдавал 500.
            # Алиасим, чтобы фронт-контракт (created_at/last_active_at) не менялся.
            rows = await pool.fetch(
                """SELECT DISTINCT pu.user_id, pu.username, pu.first_name,
                          pu.current_plan,
                          pu.registered_at AS created_at,
                          pu.last_seen AS last_active_at
                   FROM platform_users pu
                   WHERE pu.user_id = $1
                      OR pu.user_id IN (
                          SELECT wm.user_id
                          FROM workspace_members wm
                          JOIN workspaces w ON w.id = wm.workspace_id
                          WHERE w.owner_id = $1
                             OR w.id IN (
                                 SELECT workspace_id FROM workspace_members
                                 WHERE user_id = $1
                             )
                      )
                   ORDER BY last_active_at DESC NULLS LAST
                   LIMIT 50""",
                uid,
            )
            return _json_resp({"members": [dict(r) for r in rows]})
        except Exception:
            log.exception("team_members")
            return _err(_INTERNAL_ERROR, 500)

    async def audit_trail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            # Только операции самого пользователя (oq.owner_id = uid).
            # Без этого фильтра юзер видел бы аудит ВСЕХ владельцев — утечка.
            rows = await pool.fetch(
                "SELECT ol.op_id, ol.step_num, ol.target, ol.status, ol.message, ol.created_at, "
                "oq.owner_id, oq.op_type, oq.label "
                "FROM operation_log ol JOIN operation_queue oq ON oq.id = ol.op_id "
                "WHERE oq.owner_id = $1 AND ol.created_at > NOW() - INTERVAL '7 days' "
                "ORDER BY ol.created_at DESC LIMIT 200",
                uid,
            )
            return _json_resp({"entries": [dict(r) for r in rows]})
        except Exception:
            log.exception("audit_trail")
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/team/members", team_members)
    app.router.add_get("/api/miniapp/audit", audit_trail)

    # ── Session Import ───────────────────────────────────────────────────────

    async def import_sessions_api(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            raw = data.get("sessions", "")
            try:
                proxy_id = int(data["proxy_id"]) if data.get("proxy_id") else None
            except (TypeError, ValueError):
                proxy_id = None
            # Сырой адрес от клиента принимаем ТОЛЬКО когда прокси не выбран
            # из сохранённых: это случай «вставил новый прокси в форму и не
            # нажал Добавить». Если прокси выбран по id, адрес берётся из
            # хранилища владельца — клиенту незачем ни получать, ни присылать
            # логин с паролем, и подменить адрес у выбранного прокси нельзя.
            proxy = None if proxy_id else data.get("proxy_url")
            from services.session_importer import import_sessions
            # proxy_id закрепляется за аккаунтом (изоляция), проверяется на владельца
            # внутри import_sessions.
            result = await import_sessions(pool, uid, raw, proxy, proxy_id=proxy_id)
            return _json_resp(result)
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            # Раньше сырой текст исключения уходил клиенту и никуда не писался:
            # пользователю — непонятная внутренняя строка, разработчику — ничего.
            log.exception("import_sessions uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_post("/api/miniapp/import_sessions", import_sessions_api)

    # ── Добавление аккаунта: интерактивные способы входа (как в боте) ──────────
    # Раньше в мини-аппе был ТОЛЬКО импорт строки сессии. Добавляем вход по номеру
    # (код + 2FA) и по QR — те же примитивы account_manager, что и в боте.
    # Полученная сессия сохраняется через db.add_tg_account (шифрование+дедуп),
    # прокси закрепляется за аккаунтом (изоляция с первого шага).















    async def team_audit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid or not _is_admin(uid): return _err("Раздел только для владельца платформы", 403)
        try:
            rows = await pool.fetch(
                """SELECT ol.op_id, ol.target, ol.status, ol.message, ol.created_at,
                          oq.owner_id, oq.op_type, oq.label
                   FROM operation_log ol
                   JOIN operation_queue oq ON oq.id = ol.op_id
                   WHERE ol.created_at > NOW() - INTERVAL '7 days'
                   ORDER BY ol.created_at DESC LIMIT 200"""
            )
            return _json_resp({"entries": [dict(r) for r in rows]})
        except Exception:
            log.exception("team_audit")
            return _err(_INTERNAL_ERROR, 500)

    async def user_activity_log(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT ol.target, ol.status, ol.message, ol.created_at, oq.op_type
                   FROM operation_log ol
                   JOIN operation_queue oq ON oq.id = ol.op_id
                   WHERE oq.owner_id = $1
                   ORDER BY ol.created_at DESC LIMIT 50""",
                uid
            )
            return _json_resp({"log": [dict(r) for r in rows]})
        except Exception:
            log.exception("user_activity_log")
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/team/audit", team_audit)
    app.router.add_get("/api/miniapp/my_activity", user_activity_log)

    # ── Unified Contacts Hub ──────────────────────────────────────────────────








    # ── Сегмент → действие флотом ────────────────────────────────────────────
    _SEG_DM_CHUNK = _segments.SEG_DM_CHUNK
    # Потолок, резолвер среза и гейт давления — в services/contacts_hub/segments.py:
    # тем же срезом пользуются рассылка по кампании и оценка аудитории инвайта, а
    # замыкание внутри setup_routes им было видно только по счастливой случайности
    # (вызов происходит уже после сборки роутов). Заодно резолвер теперь отдаёт
    # НАСТОЯЩИЙ размер среза, а не длину обрезанной выдачи.
    _SEG_MAX = _segments.SEG_MAX

    def _segment_filters(src: dict) -> dict:
        return _segments.segment_filters(src)

    async def _segment_resolve(uid: int, body: dict) -> tuple[list, int]:
        """Контакты сегмента и сколько их в срезе ВСЕГО (может быть больше выдачи)."""
        return await _segments.resolve(pool, uid, body, limit=_SEG_MAX)

    async def _segment_contacts(uid: int, body: dict) -> list:
        rows, _total = await _segment_resolve(uid, body)
        return rows

    async def _segment_pressure_ok(uid: int, op_name: str) -> tuple[bool, str]:
        return await _segments.pressure_ok(pool, uid, op_name)




    async def ab_results(request: web.Request) -> web.Response:
        """Сводка последнего A/B-теста рассылки: доставка по вариантам + победитель.

        Группирует операции bulk_dm_adhoc по ab_batch (последний батч владельца),
        считает доставку (done_items) против отправленных (total_items) на вариант
        и отдаёт победителя через ab_engine (z-тест). «Конверсия» здесь —
        доля успешной доставки; для более глубоких метрик нужен трекинг ответов.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            batch = await pool.fetchval(
                "SELECT (params->>'ab_batch')::bigint AS b FROM operation_queue "
                "WHERE owner_id=$1 AND op_type='bulk_dm_adhoc' "
                "AND params->>'ab_batch' IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1", uid)
            if not batch:
                return _json_resp({"batch": None, "variants": [], "winner": None})
            rows = await pool.fetch(
                "SELECT params->>'ab_variant' AS variant, "
                "       COALESCE(SUM(total_items),0) AS sent, "
                "       COALESCE(SUM(done_items),0) AS delivered "
                "FROM operation_queue "
                "WHERE owner_id=$1 AND op_type='bulk_dm_adhoc' "
                "  AND (params->>'ab_batch')::bigint=$2 "
                "GROUP BY params->>'ab_variant' ORDER BY variant", uid, int(batch))
            from services import ab_engine
            variants = []
            stats = []
            for idx, r in enumerate(rows):
                sent = int(r["sent"] or 0)
                delivered = int(r["delivered"] or 0)
                variants.append({"label": r["variant"] or "—", "sent": sent,
                                 "delivered": delivered,
                                 "rate": round(delivered / sent * 100, 1) if sent else 0.0})
                stats.append({"variant": idx, "sent": sent, "converted": delivered})
            win = ab_engine.pick_winner(stats)
            winner_label = None
            if win.get("winner") is not None and 0 <= win["winner"] < len(variants):
                winner_label = variants[win["winner"]]["label"]
            leader_label = None
            if win.get("leader") is not None and 0 <= win["leader"] < len(variants):
                leader_label = variants[win["leader"]]["label"]
            return _json_resp({"batch": int(batch), "variants": variants,
                               "winner": winner_label, "leader": leader_label,
                               "confident": win.get("confident", False)})
        except Exception:
            log.exception("ab_results uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def ab_followup(request: web.Request) -> web.Response:
        """Follow-up по победителю A/B: дослать ПОБЕДИВШИЙ текст получателям
        проигравших вариантов (они получили худшую копию). Победителей не
        трогаем. Тем самым «догреваем» аудиторию лучшим сообщением без ручного
        копипаста. Тот же bulk_dm_adhoc под губернатором/флуд-темпом.

        Тело: {"batch": <ab_batch>|null (по умолчанию последний),
        "require_confident": bool (по умолчанию false — хватает лидера)}.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        require_confident = bool(body.get("require_confident"))
        try:
            batch = body.get("batch")
            if batch:
                batch = int(batch)
            else:
                batch = await pool.fetchval(
                    "SELECT (params->>'ab_batch')::bigint FROM operation_queue "
                    "WHERE owner_id=$1 AND op_type='bulk_dm_adhoc' "
                    "AND params->>'ab_batch' IS NOT NULL "
                    "ORDER BY created_at DESC LIMIT 1", uid)
            if not batch:
                return _err("Нет A/B-теста для follow-up — сначала запустите A/B-рассылку", 400)
            # Собираем по каждому варианту: текст, метку, получателей и доставку.
            # done_items — «конверсия» (доля успешной доставки), по ней и победитель.
            rows = await pool.fetch(
                "SELECT params->>'ab_variant' AS variant, "
                "       COALESCE(SUM(total_items),0) AS sent, "
                "       COALESCE(SUM(done_items),0) AS delivered "
                "FROM operation_queue "
                "WHERE owner_id=$1 AND op_type='bulk_dm_adhoc' "
                "  AND (params->>'ab_batch')::bigint=$2 "
                "GROUP BY params->>'ab_variant' ORDER BY variant", uid, int(batch))
            if not rows:
                return _err("A/B-тест не найден", 404)
            from services import ab_engine
            stats = []
            labels = []
            for idx, r in enumerate(rows):
                labels.append(r["variant"] or "—")
                stats.append({"variant": idx, "sent": int(r["sent"] or 0),
                              "converted": int(r["delivered"] or 0)})
            win = ab_engine.pick_winner(stats)
            if require_confident and not win.get("confident"):
                return _err("Победитель ещё не статзначим — дождитесь выборки или снимите строгость", 409)
            widx = win.get("winner") if win.get("winner") is not None else win.get("leader")
            if widx is None or not (0 <= widx < len(labels)):
                return _err("Победитель не определён — недостаточно данных", 409)
            winner_label = labels[widx]
            # Текст + получатели каждого варианта из params операций батча.
            op_rows = await pool.fetch(
                "SELECT params->>'ab_variant' AS variant, params->>'text' AS text, "
                "       params->'usernames' AS usernames, params->'account_ids' AS account_ids "
                "FROM operation_queue "
                "WHERE owner_id=$1 AND op_type='bulk_dm_adhoc' "
                "  AND (params->>'ab_batch')::bigint=$2", uid, int(batch))
            by_variant: dict = {}
            acc_ids: list = []
            for r in op_rows:
                lab = r["variant"] or "—"
                d = by_variant.setdefault(lab, {"label": lab, "text": r["text"] or "", "recipients": []})
                try:
                    us = _json.loads(r["usernames"]) if isinstance(r["usernames"], str) else (r["usernames"] or [])
                except Exception:
                    us = []
                d["recipients"].extend([u for u in us if u])
                if not acc_ids:
                    try:
                        a = _json.loads(r["account_ids"]) if isinstance(r["account_ids"], str) else (r["account_ids"] or [])
                        acc_ids = [int(x) for x in a if str(x).isdigit()]
                    except Exception:
                        pass
            plan = ab_engine.plan_winner_followup(list(by_variant.values()), winner_label)
            targets = plan["targets"]
            if not targets:
                return _err(plan.get("reason") or "Некому досылать follow-up", 400)
            # Аккаунты: живые из батча, иначе — любые живые владельца.
            acc_ids = await _alive_accounts(uid, acc_ids)
            if not acc_ids:
                arows = await pool.fetch(
                    "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active "
                    "AND session_str IS NOT NULL", uid)
                acc_ids = await _alive_accounts(uid, [int(r["id"]) for r in (arows or [])])
            if not acc_ids:
                return _err("Нет живых аккаунтов для рассылки", 400)
            ready, reason = await _segment_pressure_ok(uid, "bulk_dm_adhoc")
            if not ready:
                return _err(f"Инфраструктура перегружена: {reason}", 429)
            from services import operation_bus
            op_ids = []
            fu_batch = int(time.time() * 1000)
            for i in range(0, len(targets), _SEG_DM_CHUNK):
                chunk = targets[i:i + _SEG_DM_CHUNK]
                if not chunk:
                    continue
                oid = await operation_bus.submit(
                    pool, uid, "bulk_dm_adhoc",
                    {"account_ids": acc_ids, "usernames": chunk, "text": plan["winner_text"],
                     "delay": 45, "ab_variant": f"followup:{winner_label}", "ab_batch": fu_batch},
                    total_items=len(chunk),
                    label=f"Follow-up победителем ({winner_label}) ч.{i // _SEG_DM_CHUNK + 1}: {len(chunk)}")
                op_ids.append(oid)
            try:
                from services.organism import spine
                await spine.emit(pool, uid, "ab_followup", {
                    "batch": int(batch), "winner": winner_label,
                    "targets": len(targets), "ops": len(op_ids)})
            except Exception:
                pass
            return _json_resp({"ok": True, "winner": winner_label,
                               "confident": win.get("confident", False),
                               "targets": len(targets), "op_ids": op_ids,
                               "loser_variants": plan["loser_variants"],
                               "accounts": len(acc_ids)})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("ab_followup uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)


























































    # ── Telegram-облако (шифрованное распределённое хранилище файлов) ──────────






















    # Регистрация обязана идти ПОСЛЕ определения хендлера: setup_routes —
    # обычная функция, и ссылка на ещё не созданную локальную функцию даёт
    # UnboundLocalError. Раньше эта строка стояла на 600 строк выше — сервер
    # не собирался вообще, мини-апп отвечал «Application failed to respond».
    app.router.add_get("/api/miniapp/ab/results", ab_results)
    app.router.add_post("/api/miniapp/ab/followup", ab_followup)

    # ── CF Pool Manager ──────────────────────────────────────────────────────







    async def transport_get(request: web.Request) -> web.Response:
        """Текущий способ получения IP на аккаунт: прокси / IPv6 / CF-релей / прямое.
        Отдаёт что задано и какой приоритет реально сработает."""
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from database import db
            n_proxies = int(await _safe_fetchval(pool,
                "SELECT COUNT(*) FROM user_proxies WHERE owner_id=$1 AND is_active=TRUE",
                uid) or 0)
            n_bound = int(await _safe_fetchval(pool,
                "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE "
                "AND proxy_id IS NOT NULL", uid) or 0)
            ipv6 = await db.get_ipv6_subnet(pool, uid)
            c = await cf_resolve_creds(pool, uid)
            cf_ready = bool(c["has_token"] and c["account_id"])
            # приоритет для аккаунта без своего прокси
            if ipv6:
                default_mode = "ipv6"
            elif cf_ready:
                default_mode = "cf_relay"
            else:
                default_mode = "direct"
            return _json_resp({
                "proxies": n_proxies, "accounts_with_proxy": n_bound,
                "ipv6_subnet": ipv6, "cf_ready": cf_ready,
                "default_mode_for_naked": default_mode,
            })
        except Exception:
            log.exception("transport_get uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def transport_ipv6_save(request: web.Request) -> web.Response:
        """Сохранить/выключить IPv6-подсеть владельца (уникальный IP без прокси).
        body: {subnet: '2a01:...::/64' | ''}. Пустая = выключить."""
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        try:
            from database import db
            subnet = (data.get("subnet") or "").strip()
            probe = None
            if subnet and not data.get("force"):
                # Без routed-подсети bind падает, и каждый аккаунт молча уходил
                # бы на прямой host-IP: включённый IPv6 изображал бы изоляцию,
                # которой нет. Проверяем хост до сохранения; force — сохранить
                # всё равно (например, подсеть ещё настраивают).
                import ipaddress
                try:
                    ipaddress.ip_network(subnet, strict=False)
                except ValueError:
                    return _err("Неверная подсеть. Пример: 2a01:4f8:abcd::/48", 400)
                from services.ipv6_egress import probe_subnet
                probe = await asyncio.get_running_loop().run_in_executor(
                    None, probe_subnet, subnet)
                if not probe.get("ok"):
                    msg = "IPv6-подсеть не работает на этом сервере"
                    if probe.get("hint"):
                        msg += ": " + probe["hint"]
                    return _json_resp({"ok": False, "error": msg, "probe": probe},
                                      status=400)
            norm = await db.set_ipv6_subnet(pool, uid, subnet)
            return _json_resp({"ok": True, "ipv6_subnet": norm, "probe": probe})
        except ValueError as ve:
            return _err(str(ve), 400)
        except Exception:
            log.exception("transport_ipv6_save uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/transport", transport_get)
    app.router.add_post("/api/miniapp/transport/ipv6", transport_ipv6_save)

    # Позиции в поиске — вынесено в свой модуль.
    mini_app_ranking.setup_routes(app, pool)
    from services import mini_app_va_media
    mini_app_va_media.setup_routes(app, pool, _get_uid)

    # ── Network Builder ──────────────────────────────────────────────────────








    # ── Сценарии автоматизации (воркфлоу) ────────────────────────────────────
    mini_app_workflows.setup_routes(app, pool)

    # ── Audience Analytics ─────────────────────────────────────────────────

    async def _owns_bot(uid: int, bot_id: int) -> bool:
        """Бот принадлежит этому владельцу?

        Раньше проверки не было: аналитика аудитории считалась по любому
        bot_id из адреса, и чужую аудиторию мог посмотреть кто угодно, зная
        номер бота. Сам модуль аналитики по владельцу не фильтрует — значит
        фильтровать обязан вход.
        """
        return bool(await _safe_fetchval(
            pool, "SELECT 1 FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
            bot_id, uid))

    async def audience_analyze(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        if not await _owns_bot(uid, bot_id):
            return _err("Бот не найден", 404)
        try:
            from services.audience_analytics import analyze_audience
            overview = await analyze_audience(pool, uid, bot_id)
            if not overview:
                return _err("Нет данных для анализа", 404)
            return _json_resp({
                "total_subscribers": overview.total_subscribers,
                "active_users": overview.active_users,
                "active_rate": overview.active_rate,
                "peak_hour": overview.peak_hour,
                "peak_day": overview.peak_day,
                "retention_7d": overview.retention_7d,
                "retention_30d": overview.retention_30d,
                "avg_session_duration_min": overview.avg_session_duration_min,
            })
        except Exception:
            log.exception("audience_analyze uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)

    async def audience_segment(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        if not await _owns_bot(uid, bot_id):
            return _err("Бот не найден", 404)
        try:
            from services.audience_analytics import segment_audience
            segments = await segment_audience(pool, uid, bot_id)
            return _json_resp({"segments": [
                {
                    "name": s.name,
                    "user_count": s.user_count,
                    "percentage": s.percentage,
                    "avg_engagement": s.avg_engagement,
                    "description": s.description,
                    "tags": s.tags,
                }
                for s in segments
            ]})
        except Exception:
            log.exception("audience_segment uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/audience/analyze/{bot_id}", audience_analyze)
    app.router.add_get("/api/miniapp/audience/segment/{bot_id}", audience_segment)

    # ── Analytics Dashboard ───────────────────────────────────────────────





    # SSE
    app.router.add_get("/api/miniapp/events", events)

    # ── Diagnostics ──────────────────────────────────────────────────────────
    async def api_health(request: web.Request) -> web.Response:
        """Публичный health endpoint — только статус БД, без sensitive данных.

        Текст исключения наружу НЕ отдаём: ошибки asyncpg несут в себе хост,
        порт, имя базы и пользователя, а эндпоинт открыт без авторизации.
        Подробности уходят в лог, наружу — факт «база недоступна».
        """
        checks = {}
        try:
            await pool.fetchval("SELECT 1")
            checks["db"] = "ok"
        except Exception:
            log.exception("api_health: база недоступна")
            checks["db"] = "error"
        checks["status"] = "ok" if checks.get("db") == "ok" else "degraded"
        return _json_resp(checks)
    app.router.add_get("/api/miniapp/sys_health", api_health)

    # ── Admin endpoints (только для админов) ────────────────────────────────













    async def miniapp_config(request: web.Request) -> web.Response:
        """Public config endpoint — no auth required. Returns bot info for frontend."""
        bot_username = await _resolve_bot_username()
        mini_app_url = os.getenv("MINI_APP_URL", "")
        try:
            from config import PLAN_PRICES_USD, PERIOD_DISCOUNTS
            paid_price = int(PLAN_PRICES_USD.get("paid", 29))
            period_discounts = {str(k): v for k, v in PERIOD_DISCOUNTS.items()}
        except Exception:
            paid_price = 29
            period_discounts = {"1": 0, "3": 10, "6": 15, "12": 20}
        # Что даёт тариф — из тех же значений, по которым гейтится доступ.
        # Написанный руками список на экране разошёлся с ними: бесплатному
        # обещали «50 аккаунтов» при фактическом нуле и «базовые рассылки»,
        # которые на деле платные, а платному — «500 аккаунтов» вместо
        # безлимита.
        try:
            from bot.utils import tariffs as _tariffs
            plans = _tariffs.plan_matrix()
        except Exception:
            log.debug("miniapp_config: матрица тарифов недоступна", exc_info=True)
            plans = []
        return _json_resp({
            "bot_username": bot_username,
            "mini_app_url": mini_app_url,
            "platform": "Infragram OS",
            "version": "2.0",
            "paid_price": paid_price,
            "period_discounts": period_discounts,
            "plans": plans,
        })

    app.router.add_get("/api/miniapp/config", miniapp_config)

    async def prom_metrics(request: web.Request) -> web.Response:
        """Метрики процесса в формате Prometheus (находка аудита №6).

        Без авторизации намеренно: это стандартный scrape-endpoint, и он не
        отдаёт ни пользовательских данных, ни секретов — только счётчики
        операций, флудов, смертей сессий и насыщения пула. Закрывать его надо
        на уровне сети (внутренний порт/файрвол), а не токеном, иначе ни один
        сборщик его не прочитает.
        """
        from services import metrics as _m
        return web.Response(text=_m.render(),
                            content_type="text/plain", charset="utf-8")

    app.router.add_get("/metrics", prom_metrics)

    _static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mini_app")
    _index_path = os.path.join(_static_dir, "index.html")
    # PWA: .webmanifest иначе отдаётся octet-stream и часть браузеров ворчит.
    import mimetypes as _mt
    _mt.add_type("application/manifest+json", ".webmanifest")
    # Сжатый мини-апп в памяти: {"key": (mtime, size), "body": bytes}.
    # Пересобирается сам, когда файл меняется (деплой) — ключ перестаёт совпадать.
    _MINIAPP_GZ: dict = {}
    if os.path.isdir(_static_dir):
        async def serve_index(request: web.Request) -> web.Response:
            """Отдать мини-апп сжатым, с ETag на повторное открытие.

            index.html — 1,2+ МБ, и отдавался КАЖДОМУ открытию целиком и без
            сжатия. Пользователи открывают приложение с телефона по мобильной
            сети — это худший случай для лишнего мегабайта. gzip сокращает
            ~в 4,7 раза; сжимаем ОДИН раз в память (ключ — mtime+размер файла),
            а не на каждый запрос: процесс один на всё, тратить его CPU на
            повторное сжатие того же файла нельзя.
            """
            try:
                st = os.stat(_index_path)
            except OSError:
                return web.FileResponse(_index_path)   # пусть aiohttp отдаст 404
            key = (int(st.st_mtime), int(st.st_size))
            etag = f'W/"{key[0]:x}-{key[1]:x}"'

            # Повторное открытие: браузер прислал тот же ETag — тело не гоним.
            if request.headers.get("If-None-Match") == etag:
                return web.Response(status=304, headers={"ETag": etag})

            cached = _MINIAPP_GZ.get("key") == key and _MINIAPP_GZ.get("body")
            if not cached:
                try:
                    import gzip as _gzip
                    with open(_index_path, "rb") as _f:
                        _MINIAPP_GZ["body"] = _gzip.compress(_f.read(), 6)
                    _MINIAPP_GZ["key"] = key
                except OSError:
                    return web.FileResponse(_index_path)

            headers = {
                "ETag": etag,
                # Всегда перепроверять: мини-апп деплоится часто, а показывать
                # устаревший интерфейс из кэша хуже, чем один запрос с 304.
                "Cache-Control": "no-cache",
            }
            if "gzip" in (request.headers.get("Accept-Encoding") or ""):
                headers["Content-Encoding"] = "gzip"
                return web.Response(body=_MINIAPP_GZ["body"], headers=headers,
                                    content_type="text/html", charset="utf-8")
            return web.FileResponse(_index_path, headers=headers)

        async def assetlinks(request: web.Request) -> web.Response:
            """Digital Asset Links для Android-приложения (TWA): связывает домен с
            APK по отпечатку ключа подписи, чтобы приложение открывалось БЕЗ
            адресной строки Chrome. Содержимое задаётся переменной окружения
            ANDROID_ASSETLINKS (JSON из сборки APK). Не задано — 404, это норма."""
            raw = os.getenv("ANDROID_ASSETLINKS", "").strip()
            if not raw:
                return web.Response(status=404, text="assetlinks not configured")
            return web.Response(body=raw.encode(), content_type="application/json")

        app.router.add_get("/.well-known/assetlinks.json", assetlinks)
        app.router.add_get("/miniapp", serve_index)
        app.router.add_get("/miniapp/", serve_index)
        app.router.add_static("/miniapp", _static_dir, show_index=False)
        log.info("Mini App static served from %s at /miniapp", _static_dir)
    else:
        log.warning("mini_app/ directory not found — static serving skipped")

    # Красивый корневой редирект короткой ссылки: infragram.app/<код>.
    # РЕГИСТРИРУЕТСЯ ПОСЛЕДНИМ — это одно-сегментный динамический роут, и он
    # перехватил бы любой конкретный путь, объявленный ПОЗЖЕ. Все наши точные
    # пути (/, /health, /miniapp, /api/*, /webhook/*, /metrics, /.well-known/*,
    # /s/<code>) уже объявлены выше и матчатся раньше; зарезервированные слова
    # внутри link_shortener.resolve дополнительно отдают 404. Не добавляйте после
    # этой строки новых одно-сегментных корневых роутов, не вынеся их выше.
    app.router.add_get("/{code:" + _lnk.ROUTE_CODE_PATTERN + "}", short_link_redirect)
