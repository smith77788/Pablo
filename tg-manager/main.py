#!/usr/bin/env python3
"""TG Manager — Telegram bot management platform."""

import asyncio
import logging
import os
import ssl
import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from services.pg_fsm_storage import PostgresFSMStorage
from aiogram.types import ErrorEvent, CallbackQuery
from config import BOT_TOKEN
from database.db import create_pool
from services.logger import configure_root_logger, get_logger, log_exc_swallow
from services.error_codes import ErrorCode, from_exception, get_user_message
from services.error_reporting import report_error, get_user_error_message
from services.error_monitor import install_error_monitoring
from bot.middlewares.user_activity import UserActivityLogMiddleware
from bot.middlewares.subscription_gate import SubscriptionGateMiddleware, set_gate_enabled, set_gate_channels
from bot.middlewares.latency import LatencyMiddleware
from bot.utils.button_styles import install_button_style_patch

install_button_style_patch()


def _lazy_handler(module_path: str, attr: str = "router"):
    """Lazy import handler router — defers module load until first access."""
    import importlib as _il
    _mod = _il.import_module(module_path)
    return getattr(_mod, attr)


from bot.handlers import start, bots, edit, audience, webhooks, broadcast, bulk
from bot.handlers import commands as cmd_handler
from bot.handlers import templates as tpl_handler
from bot.handlers import schedule as sch_handler
from bot.handlers import multigeo as multigeo_handler
from bot.handlers import auto_reply as ar_handler
from bot.handlers import stats as stats_handler
from bot.handlers import relay as relay_handler
from bot.handlers import funnels as funnels_handler
from bot.handlers import notes as notes_handler
from bot.handlers import swarm as swarm_handler
from bot.handlers import crm as crm_handler
from bot.handlers import experiments as experiments_handler
from bot.handlers import deeplinks as deeplinks_handler
from bot.handlers import engagement as engagement_handler
from bot.handlers import seo as seo_handler
from bot.handlers import network as network_handler
from bot.handlers import subscription as sub_handler
from bot.handlers import ai_assistant as ai_handler
from bot.handlers import net_broadcast as net_bc_handler
from bot.handlers import network_bulk as net_bulk_handler
from bot.handlers import ranking as ranking_handler
from bot.handlers import accounts as accounts_handler
from bot.handlers import referral as referral_handler
from bot.handlers import channel_ops as channel_ops_handler
from bot.handlers import admin as admin_handler
from bot.handlers import admin_users as admin_users_handler
from bot.handlers import botmother_menu as bm_handler
from bot.handlers import bot_factory as bot_factory_handler
from bot.handlers import group_factory as group_factory_handler
from bot.handlers import mass_ops as mass_ops_handler
from bot.handlers import asset_templates as asset_tpl_handler
from bot.handlers import channel_factory as chan_factory_handler
from bot.handlers import competitors as competitors_handler
from bot.handlers import mass_publish as mass_pub_handler
from bot.handlers import quick_post as quick_post_handler
from bot.handlers import global_presence as global_presence_handler
from bot.handlers import gift_transfer as gift_transfer_handler
from bot.handlers import intent_engine as intent_engine_handler
from bot.handlers import health_dashboard as health_handler
from bot.handlers import proxy_manager as proxy_handler
from bot.handlers import cluster_manager as cluster_handler
from bot.handlers import audience_parser as audience_parser_handler
from bot.handlers import keyword_interceptor as keyword_interceptor_handler
from bot.handlers import chat_guard as chat_guard_handler
from bot.handlers import organism_nudge as organism_nudge_handler
from bot.handlers import managed_bots as managed_bots_handler
from bot.handlers import account_warmup as account_warmup_handler
from bot.handlers import infra_analytics as infra_analytics_handler
from bot.handlers import boost as boost_handler
from bot.handlers import mass_inviter as mass_inviter_handler
from bot.handlers import profile_setter as profile_setter_handler
from bot.handlers import phone_checker as phone_checker_handler
from bot.handlers import reporter_standalone as reporter_handler
from bot.handlers import content_cloner as content_cloner_handler
from bot.handlers import auto_registrar as auto_registrar_handler
from bot.handlers import growth_hub as growth_hub_handler
from bot.handlers import account_cleaner as account_cleaner_handler
from bot.handlers import dm_campaigns as dm_campaigns_handler
from bot.handlers import strike as strike_handler
from bot.handlers import host_server as host_server_handler
from bot.handlers import active_tasks as active_tasks_handler
from bot.handlers import topology as topology_handler
from bot.handlers import presence_pack as presence_pack_handler
from bot.handlers import approval_flow as approval_flow_handler
from bot.handlers import workspaces as workspaces_handler
from bot.handlers import error_report as error_report_handler
from bot.handlers import ecosystems as ecosystems_handler
from bot.handlers import infra_health_center as infra_hc_handler
from bot.handlers import reg_checker as reg_checker_handler
from bot.handlers import promo_platform as promo_handler
from bot.handlers import self_promo as self_promo_handler
from bot.handlers import global_search as global_search_handler
from bot.handlers import ai_commenting as ai_commenting_handler
from bot.handlers import compliance_scan as compliance_scan_handler
from bot.handlers import contacts_hub as contacts_hub_handler
from bot.handlers import business_vault as business_vault_handler
from bot.handlers import ghost_hub as ghost_hub_handler
from bot.handlers import content_mesh_hub as content_mesh_handler
from bot.handlers import clone_adapt_hub as clone_adapt_handler
from bot.handlers import auto_funnel_hub as auto_funnel_handler
from bot.handlers import physics_hub as physics_handler
from bot.handlers import graph_hub as graph_handler
from bot.handlers import api_hub as api_handler
from bot.handlers import compliance_hub as compliance_handler
from bot.handlers import ad_intelligence_hub as ad_intel_handler

from bot.handlers import account_shield_hub as account_shield_handler
from bot.handlers import semantic_memory_hub as semantic_memory_handler
from bot.handlers import persona_hub as persona_handler
from bot.handlers import stars_hub as stars_handler
from bot.handlers import audience_dna_hub as audience_dna_handler
from bot.handlers import narrative_hub as narrative_handler
from bot.handlers import nodes_hub as nodes_handler
from bot.handlers import spintax as spintax_handler
from bot.handlers import about as about_handler
from bot.handlers import fleet_pulse as fleet_pulse_handler
from services import narrative_engine
from services import auto_funnel as auto_funnel_svc
from services import ghost_engine
from services import audience_listener
from services import chat_warmup
from services import content_mesh
from services import physics_engine
from services import graph_engine
from services import scheduler
from services import auto_responder
from services import relay as relay_service
from services import funnel_runner
from services import keyword_watcher
from services import chat_guard_runner
from services import payment_checker
from services import ranking_checker
from services import search_observer
from services import account_monitor
from services import trust_engine
from services import shadowban_monitor
from services import op_worker
from services import behavioral_engine
from services import account_warmer
from services import account_health
from services import activity_engine
from services import payment_webhook
from services import task_registry
from services import drift_detector
from services import deploy_notifier
from services import infra_memory
from services import infra_copilot
from services import ecosystem_copilot
from services import db_maintenance
from services import recovery_engine
from services import anomaly_detector
from services import ecosystem_brain
from services import activity_logger
from services import promo_scheduler

configure_root_logger(
    level=logging.DEBUG if os.environ.get("DEBUG") else logging.INFO,
    use_json=os.environ.get("LOG_FORMAT") == "json",
)
log = get_logger(__name__)

# ── Роль процесса (находка аудита №2) ────────────────────────────────────────
# Один образ — разные процессы. По умолчанию «all»: ровно текущее поведение,
# чтобы включение ролей было ОСОЗНАННЫМ шагом, а не сюрпризом при деплое.
#   all    — бот + HTTP + все фоновые циклы (как сейчас)
#   web    — бот + HTTP, без фоновых циклов
#   worker — только фоновые циклы (операции, флот), без приёма апдейтов
_VALID_ROLES = ("all", "web", "worker")
# ИМЯ С ПРЕФИКСОМ, а не просто ROLE: «ROLE» — слишком общее имя, и если оно уже
# задано в окружении под что-то другое (платформа деплоя, чужой конфиг), все
# фоновые задачи молча выключились бы, а продукт выглядел бы «зависшим».
# Такой сюрприз в проде дороже любого удобства короткого имени.
_ROLE = (os.getenv("INFRAGRAM_ROLE") or "all").strip().lower()
if _ROLE not in _VALID_ROLES:
    log.warning("ROLE=%r неизвестна — работаю как 'all'. Допустимо: %s",
                _ROLE, ", ".join(_VALID_ROLES))
    _ROLE = "all"


async def _global_error_handler(event: ErrorEvent) -> None:
    """Catch any unhandled exception and show it to the user."""
    exc = event.exception

    # Silently ignore known non-actionable Telegram errors
    exc_str = str(exc).lower()
    if "message is not modified" in exc_str or "not modified" in exc_str:
        return
    # Expired callback queries — user clicked an old button, nothing to do
    if "query is too old" in exc_str or "query_id_invalid" in exc_str or "query id is invalid" in exc_str:
        return

    # Get error code and user message.
    # from_exception — модуль-функция services.error_codes, НЕ метод Enum ErrorCode.
    # Раньше вызывалась как атрибут Enum → AttributeError на второй строке
    # глобального обработчика: юзер не получал ⚠️-ответа, report_error не звался.
    error_code = from_exception(exc)
    user_msg = get_user_message(error_code)
    
    # Get user ID for context
    user_id = None
    update = event.update
    if update.callback_query and update.callback_query.from_user:
        user_id = update.callback_query.from_user.id
    elif update.message and update.message.from_user:
        user_id = update.message.from_user.id
    
    # Report error with context
    report_error(
        exc,
        user_id=user_id,
        extra={"source": "global_error_handler"},
    )
    
    log.exception("Unhandled error in update %s (code=%s)", event.update, error_code.value, exc_info=exc)
    
    try:
        if update.callback_query:
            cb: CallbackQuery = update.callback_query
            try:
                await cb.answer(f"⚠️ {user_msg}", show_alert=True)
            except Exception:
                log_exc_swallow(log, "Failed to answer callback_query on error handler")
            try:
                exc_text = (
                    str(exc)[:200]
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                await cb.message.answer(
                    f"⚠️ <b>Ошибка [{error_code.value}]</b>\n\n"
                    f"{user_msg}\n\n"
                    f"<code>{type(exc).__name__}: {exc_text}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(log, "Failed to send error message via callback_query")
        elif update.message:
            try:
                exc_text = (
                    str(exc)[:200]
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                await update.message.answer(
                    f"⚠️ <b>Ошибка [{error_code.value}]</b>\n\n"
                    f"{user_msg}\n\n"
                    f"<code>{type(exc).__name__}: {exc_text}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(log, "Failed to send error message via message")
    except Exception:
        log_exc_swallow(log, "Double-fault in error handler")


_bootstrap_runner = None
# Фаза старта — чтобы «starting» не был молчаливым. Bootstrap-сервер отдаёт её
# текстом: при зависшем деплое сразу видно, ГДЕ застряли (пул/миграции/бот/веб),
# а не безликое "starting". Обновляется по ходу init в main().
_boot_phase = "boot"


def _set_boot_phase(phase: str) -> None:
    global _boot_phase
    _boot_phase = phase
    log.info("boot phase: %s", phase)


async def _start_bootstrap_health_server() -> None:
    """Занять $PORT минимальным health-сервером до тяжёлой инициализации.

    create_pool применяет миграции — на холодной/большой БД это может занять
    десятки секунд; если за это время порт не занят, Railway убивает деплой по
    health-check («Application failed to respond»). Этот сервер отвечает 200 на всё,
    пока идёт инициализация; останавливается перед стартом реального сервера.
    Не переживает рестарт, живёт только на время старта процесса.
    """
    global _bootstrap_runner
    try:
        from aiohttp import web as _web

        _port = int(os.getenv("PORT", os.getenv("WEBHOOK_PORT", "8080")))
        _app = _web.Application()

        async def _ok(_req):
            # Отдаём текущую фазу (не голое "starting") — при зависании видно где.
            return _web.Response(text=f"starting: {_boot_phase}", status=200)

        _app.router.add_route("*", "/{tail:.*}", _ok)
        _bootstrap_runner = _web.AppRunner(_app)
        await _bootstrap_runner.setup()
        await _web.TCPSite(_bootstrap_runner, "0.0.0.0", _port).start()
        log.info("bootstrap health server bound on :%d (heavy init in progress)", _port)
    except Exception as e:
        log.warning("bootstrap health server failed to bind: %s", e)
        _bootstrap_runner = None


async def _stop_bootstrap_health_server() -> None:
    """Освободить $PORT перед стартом реального HTTP-сервера."""
    global _bootstrap_runner
    if _bootstrap_runner is not None:
        try:
            await _bootstrap_runner.cleanup()
        except Exception as e:
            log.warning("bootstrap health server cleanup: %s", e)
        _bootstrap_runner = None


_POOL_ATTEMPTS = 6
_POOL_BACKOFF = (2, 5, 10, 20, 30)      # паузы между попытками, сек


async def _create_pool_resilient():
    """Подключиться к БД, переживая КОРОТКУЮ её недоступность.

    ЗАЧЕМ. Без повторов кратковременный сбой базы (перезапуск инстанса, смена
    лидера, сетевой блип на деплое) валит процесс сразу. Платформа перезапускает
    его — и он падает снова, потому что база всё ещё поднимается. Секунды таких
    падений исчерпывают restartPolicyMaxRetries=10 из railway.json, после чего
    сервис остаётся ЛЕЖАТЬ до ручного деплоя, хотя база вернулась через минуту.

    Здесь мы тратим на ожидание около полутора минут (порт всё это время держит
    bootstrap health-сервер, health-check платформы проходит). Если база не
    вернулась и за это время — падаем честно: обслуживать запросы всё равно
    нечем, и перезапуск процесса уместен.
    """
    last: Exception | None = None
    for attempt in range(1, _POOL_ATTEMPTS + 1):
        try:
            return await create_pool()
        except Exception as exc:
            last = exc
            if attempt >= _POOL_ATTEMPTS:
                break
            delay = _POOL_BACKOFF[min(attempt - 1, len(_POOL_BACKOFF) - 1)]
            log.error(
                "БД недоступна (%s) — попытка %d/%d, повтор через %dс. "
                "Порт занят health-сервером, перезапуск процесса не тратится.",
                exc, attempt, _POOL_ATTEMPTS, delay,
            )
            await asyncio.sleep(delay)
    log.critical("БД недоступна после %d попыток — выходим: %s", _POOL_ATTEMPTS, last)
    raise last  # type: ignore[misc]


def build_dispatcher(storage: BaseStorage) -> Dispatcher:
    """Собирает Dispatcher: middleware, все роутеры, обработчик ошибок.

    Вынесено из main() ровно ради проверяемости. Сборка диспетчера — единственный
    крупный кусок старта, который ничего не ждёт и ни к чему не подключается: ему
    нужно только FSM-хранилище. При этом ошибка здесь фатальна — процесс падает
    ДО dp.start_polling, платформа рапортует об успешном деплое (контейнер-то
    запустился), а бот не отвечает вообще ни на что.

    Пока этот код жил внутри main(), проверить его не мог ни один тест: тесты
    импортируют модули и вызывают функции, а main() не вызывает никто. Так
    2026-09-04 прод пролежал пять часов из-за одной лишней строки
    include_router — при 4540 зелёных тестах и зелёном CI на каждом коммите.

    Теперь это обычная функция, и tests/test_dispatcher_builds.py её вызывает.
    """
    dp = Dispatcher(storage=storage)
    activity_log_middleware = UserActivityLogMiddleware()
    gate_middleware = SubscriptionGateMiddleware()
    latency_middleware = LatencyMiddleware()
    dp.message.outer_middleware(gate_middleware)
    dp.callback_query.outer_middleware(gate_middleware)
    dp.message.outer_middleware(activity_log_middleware)
    dp.callback_query.outer_middleware(activity_log_middleware)
    dp.message.middleware(latency_middleware)
    dp.callback_query.middleware(latency_middleware)

    dp.include_router(bm_handler.router)
    dp.include_router(bot_factory_handler.router)
    dp.include_router(group_factory_handler.router)
    dp.include_router(mass_ops_handler.router)
    dp.include_router(asset_tpl_handler.router)
    dp.include_router(chan_factory_handler.router)
    dp.include_router(intent_engine_handler.router)
    dp.include_router(global_presence_handler.router)
    dp.include_router(gift_transfer_handler.router)
    dp.include_router(ecosystems_handler.router)
    dp.include_router(quick_post_handler.router)
    dp.include_router(mass_pub_handler.router)
    dp.include_router(competitors_handler.router)
    dp.include_router(sub_handler.router)
    dp.include_router(start.router)
    dp.include_router(bots.router)
    dp.include_router(edit.router)
    dp.include_router(audience.router)
    dp.include_router(webhooks.router)
    dp.include_router(broadcast.router)
    dp.include_router(cmd_handler.router)
    dp.include_router(tpl_handler.router)
    dp.include_router(sch_handler.router)
    dp.include_router(bulk.router)
    dp.include_router(multigeo_handler.router)
    dp.include_router(ar_handler.router)
    dp.include_router(stats_handler.router)
    dp.include_router(funnels_handler.router)
    dp.include_router(notes_handler.router)
    dp.include_router(swarm_handler.router)
    dp.include_router(crm_handler.router)
    dp.include_router(experiments_handler.router)
    dp.include_router(deeplinks_handler.router)
    dp.include_router(engagement_handler.router)
    dp.include_router(seo_handler.router)
    dp.include_router(network_handler.router)
    dp.include_router(net_bulk_handler.router)
    dp.include_router(net_bc_handler.router)
    dp.include_router(ai_handler.router)
    dp.include_router(ranking_handler.router)
    from bot.handlers import metrics_dashboard as _metrics_dashboard_handler
    dp.include_router(_metrics_dashboard_handler.router)
    # Экономика флота: модуль лежал невлитым в отдельной ветке — код, тесты и
    # хендлер были написаны, но роутер нигде не регистрировался, поэтому раздел
    # просто не существовал для пользователя.
    from bot.handlers import budget_radar as budget_radar_handler
    dp.include_router(budget_radar_handler.router)
    dp.include_router(accounts_handler.router)
    dp.include_router(referral_handler.router)
    dp.include_router(channel_ops_handler.router)
    dp.include_router(health_handler.router)
    dp.include_router(proxy_handler.router)
    dp.include_router(cluster_handler.router)
    dp.include_router(audience_parser_handler.router)
    dp.include_router(keyword_interceptor_handler.router)
    # Модератор чатов — раньше relay (ловит reply) и общих групповых хендлеров,
    # чтобы системные сообщения/команды модерации обрабатывались первыми; когда
    # чат не под охраной, хендлер поднимает SkipHandler и апдейт идёт дальше.
    dp.include_router(chat_guard_handler.router)
    dp.include_router(organism_nudge_handler.router)
    # Manager Mode — создание дочерних ботов в один тап (апдейт managed_bot).
    dp.include_router(managed_bots_handler.router)
    dp.include_router(account_warmup_handler.router)
    dp.include_router(infra_analytics_handler.router)
    dp.include_router(account_cleaner_handler.router)
    dp.include_router(topology_handler.router)
    dp.include_router(presence_pack_handler.router)
    dp.include_router(dm_campaigns_handler.router)
    dp.include_router(strike_handler.router)
    dp.include_router(host_server_handler.router)
    dp.include_router(active_tasks_handler.router)
    dp.include_router(workspaces_handler.router)
    dp.include_router(approval_flow_handler.router)
    dp.include_router(error_report_handler.router)
    dp.include_router(infra_hc_handler.router)
    dp.include_router(reg_checker_handler.router)
    dp.include_router(boost_handler.router)
    dp.include_router(mass_inviter_handler.router)
    dp.include_router(profile_setter_handler.router)
    dp.include_router(phone_checker_handler.router)
    dp.include_router(reporter_handler.router)
    dp.include_router(content_cloner_handler.router)
    dp.include_router(auto_registrar_handler.router)
    dp.include_router(growth_hub_handler.router)
    dp.include_router(promo_handler.router)
    dp.include_router(self_promo_handler.router)
    dp.include_router(global_search_handler.router)
    dp.include_router(ai_commenting_handler.router)
    dp.include_router(compliance_scan_handler.router)
    dp.include_router(contacts_hub_handler.router)
    dp.include_router(business_vault_handler.router)  # «Хранилище» — business-апдейты
    dp.include_router(ghost_hub_handler.router)
    dp.include_router(content_mesh_handler.router)
    dp.include_router(clone_adapt_handler.router)
    dp.include_router(auto_funnel_handler.router)
    dp.include_router(physics_handler.router)
    dp.include_router(graph_handler.router)
    dp.include_router(api_handler.router)
    dp.include_router(compliance_handler.router)
    dp.include_router(ad_intel_handler.router)

    dp.include_router(account_shield_handler.router)
    dp.include_router(semantic_memory_handler.router)
    dp.include_router(persona_handler.router)
    dp.include_router(stars_handler.router)
    dp.include_router(audience_dna_handler.router)
    dp.include_router(narrative_handler.router)
    dp.include_router(nodes_handler.router)
    dp.include_router(spintax_handler.router)
    dp.include_router(about_handler.router)
    dp.include_router(fleet_pulse_handler.router)
    dp.include_router(relay_handler.router)  # relay last — catches F.reply_to_message
    # admin message handler AFTER relay so FSM handlers take priority
    dp.include_router(admin_users_handler.router)
    dp.include_router(admin_handler.router)
    dp.error.register(_global_error_handler)
    install_error_monitoring(dp)
    return dp


async def main() -> None:
    install_button_style_patch()

    bot_session = AiohttpSession()
    bot_session._connector_init["ssl"] = False

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=bot_session,
    )
    # Health-first: биндим $PORT МИНИМАЛЬНЫМ сервером ДО create_pool (миграции могут
    # быть медленными на холодной/большой БД). Railway health-check проходит сразу —
    # «Application failed to respond» становится невозможным. Реальный сервер займёт
    # порт после готовности (bootstrap останавливается перед его стартом).
    await _start_bootstrap_health_server()
    _set_boot_phase("db-pool+migrations")
    pool = await _create_pool_resilient()
    _set_boot_phase("init")
    # Диагностика подключения к БД — печатает host и число записей в ключевых
    # таблицах в ЛОГ. Нужна при инциденте «данные пропали»: видно, в ту ли базу
    # смотрим и есть ли данные — без SQL-клиента. КРИТИЧНО: только в ФОНЕ и с
    # таймаутом. Синхронно в пути старта она вешала бы бот, если count(*) на
    # большой/залоченной таблице тормозит — старт бота важнее диагностики.
    async def _db_diag() -> None:
        try:
            async def _run():
                async with pool.acquire() as _dc:
                    _dbname = await _dc.fetchval("SELECT current_database()")
                    _host = await _dc.fetchval("SELECT inet_server_addr()::text")
                    _counts = {}
                    for _t in ("tg_accounts", "user_proxies", "operation_queue",
                               "managed_channels", "managed_bots", "users"):
                        try:
                            _counts[_t] = await _dc.fetchval(
                                f"SELECT count(*) FROM {_t}")
                        except Exception:
                            _counts[_t] = "нет таблицы"
                    log.warning("DB-DIAG ▸ database=%s host=%s ▸ %s", _dbname, _host,
                                " ".join(f"{k}={v}" for k, v in _counts.items()))
            await asyncio.wait_for(_run(), timeout=20)
        except Exception:
            log.warning("DB-DIAG: диагностика не отработала (таймаут/ошибка)", exc_info=True)
    asyncio.create_task(_db_diag())
    fsm_storage = await PostgresFSMStorage.create(pool)
    dp = build_dispatcher(fsm_storage)

    # Load persistent platform settings
    from database import db as _db
    from bot.utils.subscription import set_free_mode

    # Под try, как соседние блоки настроек: до старта HTTP-сервера ещё далеко, и
    # исключение здесь убивает процесс раньше, чем поднимется мини-апп.
    try:
        _fm = await _db.get_platform_setting(pool, "free_mode", "false")
        set_free_mode(_fm == "true")
        log.info("Free Mode on startup: %s", "ON" if _fm == "true" else "OFF")
    except Exception:
        log.warning("failed to load free_mode setting", exc_info=True)

    # Тумблер уведомлений о новых пользователях — восстанавливаем из БД
    try:
        from bot.handlers.admin import set_notify_new_users
        _nn = await _db.get_platform_setting(pool, "notify_new_users", "true")
        set_notify_new_users(_nn == "true")
        log.info("Notify-new-users on startup: %s", "ON" if _nn == "true" else "OFF")
    except Exception:
        log.warning("failed to load notify_new_users setting", exc_info=True)

    # Под try, как соседние блоки настроек: до старта HTTP-сервера ещё далеко, и
    # исключение здесь убивает процесс ДО того, как мини-апп начнёт отвечать.
    # Гейт подписки — не то, ради чего стоит терять весь продукт.
    try:
        _gate_val = await _db.get_platform_setting(pool, "gate_enabled", "false")
        set_gate_enabled(_gate_val == "true")
        _gate_chs = await _db.get_subscription_gate_channels(pool)
        set_gate_channels(_gate_chs)
        log.info("Subscription gate on startup: %s (%d channels)",
                 "ON" if _gate_val == "true" else "OFF", len(_gate_chs))
    except Exception:
        log.warning("failed to load subscription gate settings", exc_info=True)

    # AI-ключи из БД (настраиваются из админ-панели) — приоритет над env.
    # Без них narrative/growth/ai-assistant/SEO-AI не работают.
    try:
        from services.ai_providers import set_ai_keys, configured_providers
        from services.token_vault import decrypt_token
        _ai_map = {}
        for _env_name, _skey in (
            ("OPENAI_API_KEY", "ai_openai_key"),
            ("ANTHROPIC_API_KEY", "ai_anthropic_key"),
            ("OPENROUTER_API_KEY", "ai_openrouter_key"),
            ("GROQ_API_KEY", "ai_groq_key"),
            ("GEMINI_API_KEY", "ai_gemini_key"),
        ):
            _val = await _db.get_platform_setting(pool, _skey, "")
            if _val:
                _ai_map[_env_name] = decrypt_token(_val)  # хранится зашифрованным
        # «Claude без ключа» (ambient) — незашифрованный флаг (не секрет).
        if await _db.get_platform_setting(pool, "ai_anthropic_ambient", ""):
            _ai_map["ANTHROPIC_USE_AMBIENT"] = "1"
        if _ai_map:
            set_ai_keys(_ai_map)
        log.info("AI providers on startup: %d configured", len(configured_providers()))
    except Exception:
        log.warning("failed to load AI keys from DB", exc_info=True)

    # Платёжные кошельки из БД (настраиваются из UI) — приоритет над env.
    try:
        from bot.handlers.subscription import set_pay_config
        _pay_map = {}
        for _env_name, _skey in (
            ("TRON_WALLET", "pay_tron_wallet"),
            ("TON_WALLET", "pay_ton_wallet"),
        ):
            _val = await _db.get_platform_setting(pool, _skey, "")
            if _val:
                _pay_map[_env_name] = _val
        if _pay_map:
            set_pay_config(_pay_map)
        log.info("Payment wallets on startup: %d configured from DB", len(_pay_map))
    except Exception:
        log.warning("failed to load payment wallets from DB", exc_info=True)

    # TON_API_KEY / TON_RATE — те же настройки из той же админ-панели
    # («⚙️ Настройка оплаты»), но живут только в os.environ этого процесса:
    # msg_payment_setting_value() пишет их через os.environ[key]=value, а не
    # через _PAY_OVERRIDES (в отличие от кошельков), поэтому и восстанавливать
    # их нужно тем же способом — иначе после каждого рестарта курс/ключ молча
    # откатывались на дефолт из env/.env (TON_RATE по умолчанию 3.0),
    # и платежи считались по устаревшему курсу без единого предупреждения.
    try:
        for _env_name, _skey in (
            ("TON_API_KEY", "pay_ton_api_key"),
            ("TON_RATE", "pay_ton_rate"),
        ):
            _val = await _db.get_platform_setting(pool, _skey, "")
            if _val:
                os.environ[_env_name] = _val
    except Exception:
        log.warning("failed to load TON_API_KEY/TON_RATE from DB", exc_info=True)

    # Self-heal критичных столбцов ДО обслуживания запросов. Запись session_fp/
    # proxy_fp ломается, если столбца ещё нет (лаг применения schema_v143/v146 при
    # деплое) → «нельзя добавить аккаунт/прокси», а без прокси не работает ничего.
    # create_pool применяет схемы, но это belt-and-suspenders на случай сбоя миграции.
    for _ddl in (
        "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS session_fp TEXT",
        "ALTER TABLE user_proxies ADD COLUMN IF NOT EXISTS proxy_fp TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_proxies_owner_fp "
        "ON user_proxies(owner_id, proxy_fp) WHERE proxy_fp IS NOT NULL",
        # Backup Proxy (failover) — колонка нужна до первого запроса (schema_v150).
        "ALTER TABLE user_proxies ADD COLUMN IF NOT EXISTS is_backup BOOLEAN DEFAULT FALSE",
        # Mutual Contacts (взаимные) — колонка нужна до первого uch-запроса (schema_v152).
        "ALTER TABLE unified_contacts ADD COLUMN IF NOT EXISTS is_mutual BOOLEAN DEFAULT FALSE",
        # Честный per-account счётчик Strike в истории (schema_v201) — INSERT в
        # strike_history перечисляет эти колонки; при лаге миграции запись истории
        # падала бы. Аддитивно, DEFAULT 0.
        "ALTER TABLE strike_history ADD COLUMN IF NOT EXISTS accounts_ok INT DEFAULT 0",
        "ALTER TABLE strike_history ADD COLUMN IF NOT EXISTS accounts_flood INT DEFAULT 0",
        "ALTER TABLE strike_history ADD COLUMN IF NOT EXISTS accounts_banned INT DEFAULT 0",
        "ALTER TABLE strike_history ADD COLUMN IF NOT EXISTS accounts_failed INT DEFAULT 0",
        # Внешняя инфраструктура цели (домены/APWG/регистратор) — история их пишет,
        # без колонок запись падала бы при лаге миграции. Аддитивно, DEFAULT 0.
        "ALTER TABLE strike_history ADD COLUMN IF NOT EXISTS infra_domains INT DEFAULT 0",
        "ALTER TABLE strike_history ADD COLUMN IF NOT EXISTS infra_apwg_sent INT DEFAULT 0",
        "ALTER TABLE strike_history ADD COLUMN IF NOT EXISTS infra_registrar_sent INT DEFAULT 0",
        # Device-fingerprint tg_accounts — без них check_accounts_health и другие
        # запросы с device-полями падали при лаге миграции (поздние колонки).
        "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS device_model TEXT",
        "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS system_version TEXT",
        "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS app_version TEXT",
        "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS lang_code TEXT",
        "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS system_lang_code TEXT",
        # CF relay (schema_v153): имя файла v153 было занято двумя агентами, из-за чего
        # раннер миграций пропускал CF-версию (тот же basename уже 'ok') → колонки не
        # было, а get_account_for_telethon/_ACCOUNT_COLS её селектят → падал ВЕСЬ путь
        # загрузки аккаунта (синк контактов «column a.cf_relay_url does not exist»).
        "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS cf_relay_url TEXT",
        # Отметка конфликта сессии (schema_v185). Её читают ОБА монитора здоровья
        # и пассивное самолечение кулдаунов — то есть при лаге миграции ломается
        # ровно то, что возвращает флот в строй, и флот остаётся запаркованным.
        # История с cf_relay_url выше показывает, что лаг здесь не гипотетический.
        "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS session_conflict_at TIMESTAMPTZ",
        # Хартбиты процессов (schema_v186) — страж «одной реплики» пишет сюда на
        # старте; без таблицы guard молча не работал бы (лаг миграции).
        "CREATE TABLE IF NOT EXISTS process_heartbeats ("
        "worker_id TEXT PRIMARY KEY, role TEXT NOT NULL DEFAULT 'all', "
        "started_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "last_seen TIMESTAMPTZ NOT NULL DEFAULT now())",
        # Состояние безопасного инвайтинга по чату (schema_v187) — governor пишет
        # сюда темп/паузы/исходы; без таблицы safe-режим молча не работал бы.
        "CREATE TABLE IF NOT EXISTS chat_invite_state ("
        "chat_key TEXT NOT NULL, owner_id BIGINT NOT NULL, "
        "window_start TIMESTAMPTZ, window_count INTEGER NOT NULL DEFAULT 0, "
        "paused_until TIMESTAMPTZ, pause_reason TEXT, "
        "invited_total INTEGER NOT NULL DEFAULT 0, joined_total INTEGER NOT NULL DEFAULT 0, "
        "left_total INTEGER NOT NULL DEFAULT 0, reported_total INTEGER NOT NULL DEFAULT 0, "
        "flood_hits INTEGER NOT NULL DEFAULT 0, liveness_score DOUBLE PRECISION, "
        "liveness_checked_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "PRIMARY KEY (owner_id, chat_key))",
        # Авто-реабилитация ограниченных аккаунтов (schema_v188) — стейт-машина
        # спам-блок→прогрев→перепроверка→возврат пишет сюда; без таблицы цикл
        # реабилитации молча простаивал бы при лаге миграции.
        "CREATE TABLE IF NOT EXISTS account_rehab_state ("
        "acc_id BIGINT PRIMARY KEY, owner_id BIGINT NOT NULL, "
        "phase TEXT NOT NULL DEFAULT 'appeal', kind TEXT, "
        "attempts INTEGER NOT NULL DEFAULT 0, appeal_count INTEGER NOT NULL DEFAULT 0, "
        "warm_cycles INTEGER NOT NULL DEFAULT 0, warm_actions INTEGER NOT NULL DEFAULT 0, "
        "note TEXT, first_seen TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "last_action_at TIMESTAMPTZ, next_action_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "freed_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())",
        "CREATE INDEX IF NOT EXISTS idx_account_rehab_due "
        "ON account_rehab_state(next_action_at) "
        "WHERE phase IN ('appeal', 'warming', 'recheck')",
        # Метка «об итоге восстановления уже сообщили» (schema_v197). Без неё
        # выборка уведомлений падает и итог восстановления снова становится
        # немым — то есть ровно то, что эта колонка и лечит.
        "ALTER TABLE account_rehab_state ADD COLUMN IF NOT EXISTS notified_phase TEXT",
        # Починка CHECK плана подписки (schema_v189): код перешёл на тариф 'paid',
        # а базовая схема разрешала только старые 'starter'/'pro'/'enterprise' —
        # из-за чего ВСЯ выдача 'paid' (мини-апп, billing, восстановление подписок)
        # падала с CheckViolationError. Разрешаем free/paid + старые имена.
        "ALTER TABLE subscriptions DROP CONSTRAINT IF EXISTS subscriptions_plan_check",
        "ALTER TABLE subscriptions ADD CONSTRAINT subscriptions_plan_check "
        "CHECK (plan = ANY (ARRAY['free', 'paid', 'starter', 'pro', 'enterprise']))",
        # Профильные факты для риск-движка инвайтинга (schema_v160). Их читает
        # flood_engine.account_risk_factors в КАЖДОМ расчёте суточного лимита —
        # при лаге миграции запрос падал бы на каждом батче инвайта.
        "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS is_premium BOOLEAN",
        "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS has_photo BOOLEAN",
        "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS profile_checked_at TIMESTAMPTZ",
        # cf_worker_pool + уникальный индекс (для деплоя CF-пула) — по той же причине.
        "CREATE TABLE IF NOT EXISTS cf_worker_pool ("
        "id SERIAL PRIMARY KEY, owner_id BIGINT NOT NULL, worker_url TEXT NOT NULL, "
        "region TEXT DEFAULT 'auto', status TEXT DEFAULT 'active', "
        "assigned_accounts INTEGER DEFAULT 0, last_used_at TIMESTAMPTZ, "
        "created_at TIMESTAMPTZ DEFAULT NOW())",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_cf_worker_pool_owner_url "
        "ON cf_worker_pool(owner_id, worker_url)",
        # fail_streak: дебаунс «мёртвого» воркера — помечаем down только после N
        # подряд неудачных health-пингов (разовый сетевой блип не двигает аккаунты).
        "ALTER TABLE cf_worker_pool ADD COLUMN IF NOT EXISTS fail_streak INTEGER DEFAULT 0",
        # Привязка бота к аккаунту-создателю (schema_v162). Читается сразу на
        # экране «Боты аккаунта» и пишется Bot Factory — при лаге миграции запрос
        # /account/{id}/bots падал бы с «column acc_id does not exist».
        "ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS acc_id INTEGER",
        "CREATE INDEX IF NOT EXISTS idx_managed_bots_acc_id ON managed_bots(acc_id)",
        # Менеджер по продажам: минимальный заказ/порог доставки по товару и правила
        # заказа на уровне персоны (schema_v205). Их читает build_system_prompt и
        # пишут add_product/update_persona — при лаге миграции путь падал бы с
        # «column min_qty/order_rules does not exist». Аддитивно, безопасные DEFAULT.
        "ALTER TABLE bot_sales_products ADD COLUMN IF NOT EXISTS min_qty INT NOT NULL DEFAULT 1",
        "ALTER TABLE bot_sales_products ADD COLUMN IF NOT EXISTS unit TEXT NOT NULL DEFAULT 'шт'",
        "ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS order_rules TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS payment_details TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE bot_sales_personas ADD COLUMN IF NOT EXISTS payment_via_operator BOOLEAN NOT NULL DEFAULT FALSE",
    ):
        try:
            await pool.execute(_ddl)
        except Exception:
            log.warning("startup self-heal DDL failed: %.90s", _ddl)

    # Init op_worker DB pool and reset stale in_operation flags from previous process
    op_worker.init_op_worker_pool(pool)
    # Тоже под try: разбор залипших флагов прошлого процесса полезен, но ради
    # него нельзя терять весь продукт — аренды всё равно истекают сами по сроку.
    try:
        await op_worker.reset_stale_in_operation(pool)
    except Exception:
        log.warning("startup: не удалось снять залипшие in_operation", exc_info=True)

    # Финализируем «зависшие» парсер-раны (pending/running > 1ч): раньше сбой
    # client.connect() оставлял их в вечном 'pending' — чистим сироты при старте.
    try:
        _stale = await _db.finalize_stale_parser_runs(pool)
        if _stale:
            log.info("startup: finalized %d stale parser_runs → failed", _stale)
    except Exception:
        log.warning("startup: failed to finalize stale parser_runs", exc_info=True)

    # Send deployment notification to admins on startup (detects new deploys)
    asyncio.create_task(deploy_notifier.notify_deploy(pool, bot))

    # Register bot commands (shows in Telegram "/" menu)
    #
    # ПОД TRY — И ЭТО ВАЖНО. Вызов ходит в api.telegram.org и стоит НА ПУТИ К
    # СТАРТУ HTTP-СЕРВЕРА: мини-апп начинает отдаваться примерно на сотню строк
    # ниже. Любая ошибка здесь — сетевой блип, 429, 5xx на стороне Telegram,
    # временно недоступный токен — роняла ВЕСЬ процесс до того, как поднимется
    # веб. Дальше restartPolicyMaxRetries=10 в railway.json: десять падений
    # подряд, и платформа перестаёт перезапускать сервис. Снаружи это
    # «приложение не загружается» — притом что с самим приложением всё в
    # порядке, а меню команд — вещь косметическая и переустановится на
    # следующем старте.
    from aiogram.types import BotCommand

    _commands = (
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="menu", description="🏠 Infragram OS"),
            BotCommand(command="about", description="✨ Что умеет Infragram"),
            BotCommand(command="pulse", description="💓 Пульс флота — кто на паузе и почему"),
            BotCommand(command="budget", description="💸 Экономика флота — где утекает бюджет"),
            BotCommand(command="find", description="🔍 Найти функцию"),
            BotCommand(command="post", description="✍️ Быстрый пост в каналы"),
            BotCommand(command="spin", description="🎲 Spintax — рандомизация текста"),
            BotCommand(command="accounts", description="📱 Мои аккаунты"),
            BotCommand(command="tasks", description="⚡ Активные задачи"),
            BotCommand(command="promo", description="🚀 Продвижение ботов"),
            BotCommand(command="subscription", description="💳 Подписка & Тариф"),
            BotCommand(command="app", description="📱 Открыть Mini App"),
            BotCommand(command="cancel", description="Отменить текущее действие"),
        ]
    )
    _set_boot_phase("bot-commands")
    try:
        await bot.set_my_commands(_commands)
    except Exception:
        log.warning(
            "не удалось зарегистрировать меню команд — продолжаем старт "
            "(меню косметическое, переустановится на следующем запуске)",
            exc_info=True,
        )

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_ctx, limit=200, limit_per_host=50)
    http = aiohttp.ClientSession(connector=connector)

    _svc_stagger_index = 0

    async def _resilient(name: str, fn, *args):
        """Wrap a background service factory with auto-restart on crash.
        fn(*args) is called fresh each restart so the coroutine is never reused.
        Stagger startup so all services don't hit DB simultaneously.

        РОЛЬ ПРОЦЕССА (находка аудита №2). Сейчас бот, HTTP-API на 500+ маршрутов
        и ~49 фоновых циклов делят один интерпретатор и один GIL: потолок
        массовых операций общий на всех клиентов, а сбой в фоне роняет и бота.
        `ROLE` позволяет запустить тот же образ разными процессами:
            all    — всё в одном (ЗНАЧЕНИЕ ПО УМОЛЧАНИЮ, поведение как раньше);
            web    — только HTTP и бот, без фоновых циклов;
            worker — только фоновые циклы, без обслуживания Telegram-апдейтов.
        Гейт стоит ЗДЕСЬ, в одной точке, а не в 49 местах создания задач.
        """
        if _ROLE == "web":
            log.debug("ROLE=web: фоновый сервис %s не запускается", name)
            return
        nonlocal _svc_stagger_index
        _svc_stagger_index += 1
        delay = _svc_stagger_index * 2  # 2s gap between each service
        await asyncio.sleep(delay)
        while True:
            try:
                await fn(*args)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(
                    "Service %s crashed: %s — restarting in 30s", name, e, exc_info=True
                )
                await asyncio.sleep(30)

    async def _web_resilient(name: str, fn, *args):
        """Like _resilient but starts immediately (no stagger) and restarts in 5s.
        Used for the HTTP server which must bind to PORT before Railway health checks.
        """
        while True:
            try:
                await fn(*args)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(
                    "Web service %s crashed: %s — restarting in 5s", name, e, exc_info=True
                )
                await asyncio.sleep(5)

    # Определяем webhook режим ДО старта сервера, чтобы передать dp в payment_webhook
    _webhook_path = "/webhook"
    _webhook_url = os.getenv("WEBHOOK_URL") or None
    _allowed_updates = [
        "message", "callback_query", "inline_query",
        "chosen_inline_result", "pre_checkout_query",
        # «Хранилище» (Echo Vault): без этих типов Telegram НЕ доставит
        # business-апдейты и архив останется пустым.
        "business_connection", "business_message",
        "edited_business_message", "deleted_business_messages",
        # «Модератор чатов»: my_chat_member — чтобы поймать выдачу боту админки
        # (авто-активация охраны); chat_member — трекинг входов/выходов участников.
        "my_chat_member", "chat_member",
        # Manager Mode: managed_bot — Telegram сообщает о создании дочернего бота
        # через нашу ссылку (иначе токен нового бота мы не получим).
        "managed_bot",
    ]

    try:
        # Освобождаем $PORT от bootstrap health-сервера прямо перед стартом реального —
        # окно, когда порт свободен, минимально (мс), Railway health-check его не заметит.
        _set_boot_phase("web-start")
        await _stop_bootstrap_health_server()
        # HTTP server starts FIRST — must bind to PORT immediately for Railway web services.
        # Если задан WEBHOOK_URL — передаём dp чтобы Telegram webhook работал на том же порту.
        # Это предотвращает конфликт двух серверов на одном PORT.
        if _webhook_url:
            asyncio.create_task(_web_resilient(
                "payment_webhook", payment_webhook.run, pool, bot, dp, _webhook_path, http
            ))
        else:
            asyncio.create_task(_web_resilient("payment_webhook", payment_webhook.run, pool, bot))

        # Карта транспорта аккаунтов (релей/прокси) — через _web_resilient, то
        # есть ВО ВСЕХ ролях. Под ROLE=web фоновые циклы не запускаются вовсе, а
        # мини-апп и бот ходят к аккаунтам по своим выборкам, где cf_relay_url
        # почти нигде нет: без карты аккаунт с релеем уйдёт напрямую с host-IP,
        # тогда как операции идут через релей. Два адреса на одну сессию — это
        # AUTH_KEY_DUPLICATED, то есть мёртвый аккаунт.
        from services.account_manager import run_transport_refresh_loop
        asyncio.create_task(
            _web_resilient("account_transport_map", run_transport_refresh_loop, pool))

        # Страж «одной реплики» (ось №3): лимиты флуда живут в памяти процесса, и
        # вторая реплика молча разгоняет темп по флоту → риск бана. Guard бьёт
        # хартбит и громко предупреждает, если реплик больше одной. Во всех ролях.
        from services import replica_guard as _rguard
        asyncio.create_task(_web_resilient(
            "replica_guard", _rguard.run_heartbeat_loop, pool, op_worker._WORKER_ID, _ROLE))

        asyncio.create_task(_resilient("scheduler", scheduler.run, pool, http))
        asyncio.create_task(
            _resilient("auto_responder", auto_responder.run, pool, http, bot)
        )
        asyncio.create_task(_resilient("relay", relay_service.run, pool, http))
        asyncio.create_task(_resilient("funnel_runner", funnel_runner.run, pool, http))
        asyncio.create_task(_resilient("keyword_watcher", keyword_watcher.run, pool, bot))
        # Сметатель капчи «Модератора чатов»: кикает новичков, не прошедших
        # проверку «Я не бот» к дедлайну.
        asyncio.create_task(_resilient("chat_guard_runner", chat_guard_runner.run, pool, bot))
        # Сторож «Хранилища»: замечает, что бизнес-подключение тихо отвалилось,
        # и один раз предупреждает владельца в ЛС (иначе узнаёт, только зайдя в архив).
        from services import vault_watchdog
        asyncio.create_task(_resilient("vault_watchdog", vault_watchdog.run, pool, bot))
        # Сторож прокси: фоновой проверки не было вовсе — is_alive обновлялся
        # только вручную, и молча умерший прокси продолжал раздавать аккаунтам
        # сетевые ошибки. Снаружи это выглядит как «сломались аккаунты».
        from services import proxy_watchdog
        asyncio.create_task(_resilient("proxy_watchdog", proxy_watchdog.run, pool, bot))
        # Пульс организма: сердцебиение — смотрит на мир владельца и проактивно
        # подсказывает срочное в ЛС (с кулдауном). Система «живёт» между сессиями.
        from services.organism import runner as organism_runner
        asyncio.create_task(_resilient("organism_runner", organism_runner.run, pool, bot))
        asyncio.create_task(
            _resilient("payment_checker", payment_checker.run, pool, http, bot)
        )
        asyncio.create_task(
            _resilient("ranking_checker", ranking_checker.run, pool, bot)
        )
        asyncio.create_task(
            _resilient(
                "search_observer", search_observer.run_confirmation_loop, pool, bot
            )
        )
        asyncio.create_task(
            _resilient("account_monitor", account_monitor.run, pool, bot)
        )
        asyncio.create_task(_resilient("trust_engine", trust_engine.run, pool, bot))
        asyncio.create_task(
            _resilient("shadowban_monitor", shadowban_monitor.run, pool, bot)
        )
        asyncio.create_task(_resilient("op_worker", op_worker.run, pool, bot))
        asyncio.create_task(
            _resilient("behavioral_engine", behavioral_engine.run, pool, bot)
        )
        asyncio.create_task(
            # bot нужен циклу, чтобы сообщать о планах, которые он останавливает
            # сам (бан/спам-блок/длинный flood) — раньше это было немым.
            _resilient("account_warmer", account_warmer.run_warmup_loop, pool, 1, bot)
        )
        asyncio.create_task(
            _resilient("account_health", account_health.run_health_check_loop, pool)
        )
        # Авто-реабилитация ограниченных аккаунтов: спам-блок → тихий прогрев →
        # перепроверка → возврат в строй (иначе аккаунт замирал навсегда).
        from services import account_rehab
        asyncio.create_task(
            # bot нужен циклу, чтобы сообщать об итоге: вернувшийся аккаунт
            # иначе будет списан, а застрявший — вечно ждать разбора.
            _resilient("account_rehab", account_rehab.run_rehab_loop, pool, bot)
        )
        # Автобэкап БД во внешнее хранилище (Telegram). После инцидента с потерей
        # эфемерной базы: система сама регулярно снимает дамп и кладёт его ЗА
        # пределы Railway, чтобы потеря тома/проекта не была фатальной.
        from services import db_backup
        asyncio.create_task(
            _resilient("db_backup", db_backup.run_backup_loop, pool, bot)
        )
        asyncio.create_task(
            _resilient("activity_engine", activity_engine.run_activity_loop, pool)
        )
        # payment_webhook already started via _web_resilient above (no stagger)
        asyncio.create_task(_resilient("task_registry", task_registry.run_cleanup_loop))
        # Сессии админки живут в БД (schema_v181); процесс держит лишь кэш —
        # обновляем, чтобы вход в одном процессе был виден остальным.
        from bot.handlers.admin import run_session_admin_refresh
        asyncio.create_task(_resilient("session_admin_refresh", run_session_admin_refresh, pool))
        # proxy_scraper (бесплатный публичный пул) УДАЛЁН: пул нестабилен и блокировал
        # работу. Транспорт — прокси пользователя или прямой host-IP; CF/IPv6 opt-in.
        asyncio.create_task(_resilient("activity_logger", activity_logger.run, pool))
        asyncio.create_task(_resilient("drift_detector", drift_detector.run, pool, bot))
        # Витрина: открывает следующую порцию мест в боевой канал. Без цикла
        # буфер отдал бы только первую волну и замер — люди копятся, а войти
        # некуда.
        from services import showcase_layer as _showcase_layer
        asyncio.create_task(
            _resilient("showcase_waves", _showcase_layer.run, pool, bot))
        asyncio.create_task(
            _resilient("ecosystem_auto_management", ecosystem_brain.run_auto_management, pool, bot)
        )
        asyncio.create_task(
            _resilient("infra_memory", infra_memory.run_flush_loop, pool)
        )
        asyncio.create_task(
            _resilient("infra_copilot", infra_copilot.run_copilot_loop, pool, bot)
        )
        asyncio.create_task(
            _resilient(
                "ecosystem_copilot",
                ecosystem_copilot.run_ecosystem_copilot_loop,
                pool,
                bot,
            )
        )
        asyncio.create_task(_resilient("db_maintenance", db_maintenance.run, pool))
        asyncio.create_task(
            _resilient("recovery_engine", recovery_engine.run_recovery_loop, pool, bot)
        )
        asyncio.create_task(
            _resilient("anomaly_detector", anomaly_detector.run_anomaly_loop, pool, bot)
        )
        from services import follow_checker as _follow_checker
        asyncio.create_task(
            _resilient("follow_checker", _follow_checker.run_follow_checker, pool, bot)
        )
        asyncio.create_task(
            _resilient("promo_scheduler", promo_scheduler.run, pool, bot)
        )
        asyncio.create_task(
            _resilient("ghost_engine", ghost_engine.run, pool, bot)
        )
        asyncio.create_task(
            _resilient("audience_listener", audience_listener.run, pool, bot)
        )
        asyncio.create_task(
            _resilient("chat_warmup", chat_warmup.run, pool, bot)
        )
        asyncio.create_task(
            _resilient("content_mesh", content_mesh.run, pool, bot)
        )
        asyncio.create_task(
            _resilient("auto_funnel", auto_funnel_svc.run, pool, bot)
        )
        asyncio.create_task(
            _resilient("physics_engine", physics_engine.run, pool, bot)
        )
        asyncio.create_task(
            _resilient("graph_engine", graph_engine.run, pool, bot)
        )
        asyncio.create_task(
            _resilient("narrative_engine", narrative_engine.run, pool, bot)
        )
        from services import account_shield as _account_shield
        from services import stars_optimizer as _stars_optimizer
        from services import audience_dna as _audience_dna
        asyncio.create_task(_resilient("account_shield", _account_shield.run, pool, bot))
        asyncio.create_task(_resilient("stars_optimizer", _stars_optimizer.run, pool, bot))
        asyncio.create_task(_resilient("audience_dna", _audience_dna.run, pool, bot))
        from services import cf_pool_manager as _cf_pool_manager
        asyncio.create_task(_resilient("cf_pool_monitor", _cf_pool_manager.run, pool, bot))
        # Ban Weather, Фаза 1 (schema_v146). Триггер БД trg_immunity_capture_status
        # пишет КАЖДУЮ смену acc_status в account_status_events с самого развёртывания
        # схемы, но обработчик никогда не запускался: processed_at оставался NULL
        # навсегда, автопсий не появлялось, а таблица росла. Движок готов и покрыт
        # тестами — ему не хватало ровно этой строки. Fail-soft по устройству:
        # сбой обработки одного события не роняет цикл и не влияет на операции.
        from services import immunity_engine as _immunity_engine
        asyncio.create_task(_resilient("immunity_engine", _immunity_engine.start, pool))
        # Докатить рассылки, оборванные предыдущим рестартом (status running/pending).
        # broadcaster.run пропускает уже доставленных через delivery log — без дублей.
        from services import broadcaster as _broadcaster
        asyncio.create_task(_broadcaster.resume_interrupted(pool))
        # Session Health Monitor — проверка сессий каждые 6 часов
        from services.account_manager import run_session_health_monitor
        asyncio.create_task(_resilient("session_health_monitor", run_session_health_monitor, pool))
        # Pool Monitor — проверка пула соединений каждые 5 минут
        from database.db import run_pool_monitor
        asyncio.create_task(_resilient("pool_monitor", run_pool_monitor, pool))
        log.info("TG Manager started")

        # ── Webhook or long-polling ───────────────────────────────────────────
        if _webhook_url:
            # Webhook handler уже зарегистрирован на payment_webhook сервере (выше).
            # Просто подключаемся к Telegram и ждём — никакого отдельного сервера.
            await bot.set_webhook(
                _webhook_url,
                allowed_updates=_allowed_updates,
                drop_pending_updates=True,
            )
            log.info("Webhook mode: %s (handler on shared HTTP server)", _webhook_url)
            try:
                await asyncio.Event().wait()
            finally:
                try:
                    await bot.delete_webhook()
                except Exception:
                    pass
        elif _ROLE == "worker":
            # Воркер НЕ забирает апдейты: два процесса на одном getUpdates
            # отбирали бы их друг у друга (Telegram отдаёт апдейт ровно одному),
            # и бот отвечал бы через раз. Держим процесс живым — работу делают
            # фоновые циклы.
            log.info("ROLE=worker: поллинг не запускаю, работают фоновые циклы")
            while True:
                await asyncio.sleep(3600)
        else:
            # ПОЛЛИНГ ПЕРЕЗАПУСКАЕМ, А НЕ ПАДАЕМ ВМЕСТЕ С НИМ.
            #
            # Это был последний голый `await` в процессе: любая ошибка сети или
            # ответ Telegram 429/5xx поднимала исключение и завершала процесс —
            # ВМЕСТЕ с уже поднятым HTTP-сервером, то есть с мини-аппом и всем
            # API. Дальше restartPolicyMaxRetries=10 (railway.json): десять
            # падений подряд — и платформа перестаёт перезапускать сервис.
            # Недоступность Telegram на пару минут превращалась в бессрочно
            # лежащее приложение, хотя ни одна его часть не сломана.
            #
            # Все остальные подсистемы давно живут под присмотром (_resilient,
            # _web_resilient); поллинг оставался единственным исключением.
            _poll_fail = 0
            while True:
                try:
                    await dp.start_polling(
                        bot,
                        pool=pool,
                        http=http,
                        drop_pending_updates=True,
                        polling_timeout=30,
                        allowed_updates=_allowed_updates,
                    )
                    _poll_fail = 0          # штатный выход — начинаем счёт заново
                except asyncio.CancelledError:
                    raise                    # остановка процесса — не сбой
                except Exception as _pe:
                    _poll_fail += 1
                    # Пауза растёт до минуты: при недоступном Telegram не долбим
                    # его в цикле, но и не ждём слишком долго после блипа.
                    _delay = min(5 * _poll_fail, 60)
                    log.error(
                        "Поллинг Telegram упал (%s) — перезапуск через %dс "
                        "(попытка %d). HTTP-сервер и мини-апп продолжают работать.",
                        _pe, _delay, _poll_fail, exc_info=True,
                    )
                    await asyncio.sleep(_delay)
    finally:
        await pool.close()
        await http.close()
        await bot.session.close()


if __name__ == "__main__":
    import signal

    # Контейнеры (Railway, Docker, k8s) при рестарте/деплое шлют SIGTERM. По
    # умолчанию Python завершает процесс без раскрутки стека — блок finally в
    # main() (закрытие пула БД, снятие webhook, корректное завершение op_worker)
    # НЕ выполняется. Превращаем SIGTERM в KeyboardInterrupt, чтобы asyncio.run
    # отменил главную задачу и отработал graceful-shutdown, как при Ctrl+C.
    def _graceful_sigterm(_signum, _frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _graceful_sigterm)
    except (ValueError, OSError):
        pass  # не главный поток / платформа без SIGTERM — не критично

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Получен сигнал остановки — завершаемся штатно")
