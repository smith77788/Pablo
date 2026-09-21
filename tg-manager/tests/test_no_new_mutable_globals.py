"""Храповик на изменяемые глобалы в services/ (находка аудита №3).

ЧТО ЭТО. Разбором AST находятся переменные уровня модуля, которые МУТИРУЮТСЯ из
функций: присваивание через `global`, запись по ключу (`X[k] = v`) и вызовы
меняющих методов (`append`, `add`, `pop`, `clear`, …). Каждая такая переменная —
кусок состояния, живущий в памяти ОДНОГО процесса.

ЗАЧЕМ. Пока продукт был одним процессом, это работало. После разделения на роли
(INFRAGRAM_ROLE=web|worker) и с несколькими репликами такое состояние молча
разъезжается, и цена зависит от того, что в нём лежит:
  • сессии админки держали РЕШЕНИЕ О ДОСТУПЕ → вынесены в БД (schema_v181);
  • предохранитель операций держал ПАУЗУ ФЛОТА → вынесен в БД (schema_v182);
  • заглушка уведомлений экосистем держала ТИШИНУ → вынесена в platform_settings;
  • IPv6-подсеть владельца выбирает ТРАНСПОРТ → праймится в старте операции.
Каждая из этих находок выглядела снаружи как «продукт не работает».

Поэтому здесь не запрет, а КЛАССИФИКАЦИЯ: каждый глобал обязан быть отнесён к
одной из четырёх групп с причиной. Новый глобал без записи в реестре роняет
тест — и автор обязан ответить на один вопрос: «что случится, если это состояние
разойдётся между процессами?».

КАК ДОБАВИТЬ ГЛОБАЛ. Впишите его в подходящую группу с короткой причиной:
  PROCESS_LOCAL   — делить нечего: примитивы asyncio, задачи, пулы, синглтоны.
  CACHE           — производное; расхождение стоит лишней работы или устаревшего
                    показания, но не корректности.
  DB_BACKED       — память лишь кэш, истина в БД.
  KNOWN_DIVERGENCE — расхождение реально стоит денег/качества, но пока принято.
                    Этот список расти НЕ должен (см. последний тест).
"""
from __future__ import annotations

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES = os.path.join(ROOT, "services")

_MUTATORS = {"append", "extend", "add", "update", "pop", "clear", "remove",
             "discard", "setdefault", "insert", "popitem", "sort"}


def _module_globals(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _mutated_in_functions(tree: ast.Module, toplevel: set[str]) -> set[str]:
    found: set[str] = set()
    # 1) явное `global X` + присваивание
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            found.update(n for n in node.names if n in toplevel)
    # 2) мутации контейнера внутри функции; имена, перекрытые локально, не в счёт
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        shadowed: set[str] = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                shadowed.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.arg):
                shadowed.add(node.arg)
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _MUTATORS
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in toplevel
                    and node.func.value.id not in shadowed):
                found.add(node.func.value.id)
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                            and t.value.id in toplevel and t.value.id not in shadowed):
                        found.add(t.value.id)
    return found


def detect() -> set[str]:
    """{'services/файл.py:имя'} — все мутируемые глобалы в services/."""
    out: set[str] = set()
    for dirpath, _dirs, files in os.walk(SERVICES):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            src = open(path, encoding="utf-8", errors="ignore").read()
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            toplevel = _module_globals(tree)
            for name in _mutated_in_functions(tree, toplevel):
                out.add(f"{rel}:{name}")
    return out


# ── Реестр ────────────────────────────────────────────────────────────────────

# Делить нечего: объекты привязаны к процессу/циклу событий по своей природе.
PROCESS_LOCAL: dict[str, str] = {
    "services/wb_chat/login.py:_PENDING": (
        "незавершённый вход: держит ЖИВОЙ transport-объект (сокет), который "
        "нельзя ни сериализовать в БД, ни использовать из другого процесса; "
        "живёт секунды-минуты до ввода кода"),
    "services/account_manager.py:_pending": "клиент Telethon на время логина — объект процесса",
    "services/account_manager.py:_pending_device": "то же, профиль устройства входа",
    "services/account_manager.py:_pending_qr": "то же, QR-логин",
    "services/account_manager.py:_session_inuse": "мьютекс коннекта; межпроцессный арбитр — аренда в БД",
    "services/account_warmer.py:_plan_locks": "asyncio.Lock",
    "services/account_warmer.py:_session_locks": "asyncio.Lock",
    "services/auto_responder.py:_inactivity_sweep_task": "asyncio.Task",
    "services/auto_responder.py:_sales_followup_task": "asyncio.Task",
    "services/auto_responder.py:_cloud_reconcile_task": "asyncio.Task",
    "services/auto_responder.py:_new_user_notify": "троттлинг уведомлений о новых подписчиках по боту (анти-накрутка); best-effort, потеря при рестарте безвредна",
    "services/flood_guard.py:_windows": "скользящее окно скорости новых подписчиков по боту (детектор всплеска); process-local, потеря при рестарте безвредна",
    "services/flood_guard.py:_config_cache": "TTL-кэш конфига защиты по боту; расхождение реплик безвредно (кэш истекает за 30с, источник правды — БД)",
    "services/bg_tasks.py:_bg_tasks": "ссылки на задачи, чтобы их не собрал GC",
    "services/bot_api.py:_semaphore": "asyncio.Semaphore",
    "services/broadcaster.py:_running": "asyncio.Task запущенных рассылок",
    "services/deploy_notifier.py:_REPO_ROOT": "путь на диске этого контейнера",
    "services/error_monitor.py:_default_monitor": "синглтон процесса",
    "services/error_recovery.py:_default_manager": "синглтон процесса",
    "services/error_reporting.py:_default_reporter": "синглтон процесса",
    "services/managed_bot_webhooks.py:_queues": "asyncio.Queue",
    "services/op_worker.py:_active_op_ids": "операции, выполняемые ЭТИМ процессом",
    "services/op_worker.py:_active_op_tasks": "asyncio.Task этого процесса",
    "services/op_worker.py:_accounts_in_use": "быстрый локальный фильтр; арбитр — аренда в БД",
    "services/op_worker.py:_operation_account_locks": "то же, привязка операция→аккаунты",
    "services/op_worker.py:_db_pool": "пул asyncpg привязан к циклу событий",
    "services/op_worker.py:_shutting_down": (
        "ЭТОТ процесс получил SIGTERM и сворачивается (op_worker.shutdown). "
        "Делить нечего и НЕ НУЖНО: флаг означает «я сейчас умру», и сосед, "
        "который жив, обязан продолжать забирать операции из очереди — "
        "разошедшись, он поступит правильно. Общий флаг, наоборот, остановил "
        "бы разбор очереди на всей платформе из-за рестарта одной реплики. "
        "Потеря при рестарте безвредна: новый процесс поднимается с False, и "
        "это ровно то, что нужно"),
    "services/op_circuit_breaker.py:_db_pool": "пул asyncpg привязан к циклу событий",
    "services/organism/spine.py:_SUBS": "подписчики внутрипроцессной шины",
    "services/pacing_engine.py:_engine": "синглтон процесса",
    "services/relay.py:_offsets": "offset getUpdates: один бот опрашивает один процесс",
    "services/session_pool.py:_check_lock": "asyncio.Lock",
    "services/session_pool.py:_registry": "живые клиенты Telethon — объекты процесса",
    "services/task_registry.py:_registry": "asyncio.Task этого процесса",
    "services/token_vault.py:_last_decrypt_warn": "троттл лога сбоя расшифровки; каждая реплика предупреждает независимо — расхождение безвредно",
    "services/token_vault.py:_key_warned": "флаг «уже предупредили о запасном ключе» — один раз на процесс; каждая реплика предупреждает сама, и это желаемое поведение: предупреждение должно быть в логе КАЖДОГО процесса",
}

# Производное состояние: расхождение стоит лишней работы или устаревшего
# показания на экране, но не корректности и не доступа.
CACHE: dict[str, str] = {
    "services/account_health.py:_health_cache": "здоровье аккаунта, пересчитывается",
    "services/account_manager.py:_GET_ME_CACHE": "get_me с TTL 5 мин",
    "services/account_manager.py:_ACC_TRANSPORT": (
        "релей/прокси аккаунта; истина в tg_accounts, авторитетно перечитывается "
        "на старте операции — добирает поля, которых нет в выборке вызывающего"),
    "services/account_manager.py:_OWNER_IPV6_SUBNET": "праймится из БД на старте операции",
    "services/account_manager.py:_OWNER_PROXY_POLICY": "праймится из БД на старте операции",
    "services/account_manager.py:_proxy_stats": "статистика прокси; сводная — в infra_memory",
    "services/brand_injection.py:_plan_cache": "тариф бота, читается из БД",
    "services/brand_injection.py:_user_plan_cache": "тариф пользователя, читается из БД",
    "services/broadcaster.py:_bot_tier_cache": "тариф бота с TTL",
    "services/contacts_hub/search_engine.py:_cache": "кэш результатов поиска",
    "services/fleet_governor.py:_CACHE": "множитель темпа с TTL",
    "services/mass_inviter_engine.py:_ENTITY_CACHE": (
        "разрешённая сущность группы по (acc_id, group_ref) — чтобы уже вступивший "
        "аккаунт не дёргал флуд-лимитированный CheckChatInviteRequest на каждый батч; "
        "best-effort, access_hash пер-аккаунтный и стабильный, потеря при рестарте "
        "безвредна (перерезолвится)"),
    "services/wb_chat/drivers/mock.py:SENT_OUTBOX": (
        "исходящие mock-драйвера — существует только в тестах и локальной отладке, "
        "в проде драйвер real"),
    "services/op_worker.py:_DISPATCH": (
        "таблица «op_type → исполнитель»; собирается один раз из кода модуля и "
        "дальше только читается — расходиться между процессами нечему"),
    "services/geo_tempo.py:_CC_TZ": "страна→таймзона, справочник",
    "services/geo_tempo.py:_ZONE_WARNED": "де-дуп WARN о нерезолвящейся зоне, process-local",
    "services/audience_listener.py:_listening": "acc_id->TelegramClient подключённых слушателей, "
        "process-local; корректность держит op_worker.try_claim_account/release_accounts (в БД), "
        "не этот словарь — при рестарте переустановится со следующего tick()",
    "services/invite_engine.py:_link_cache": "инвайт-ссылки, перевыпускаются",
    "services/mini_app_api.py:_bot_username_cache": "имя бота, неизменно",
    "services/mini_app_api.py:_cache": "ответы API с TTL",
    "services/op_worker.py:_cancel_cache": "флаг отмены с коротким TTL; истина в БД",
    "services/op_worker.py:_eta_data": "оценка времени, отображение",
    "services/op_worker.py:_progress_milestones": "пороги отчёта о прогрессе",
    "services/organism/runner.py:_last_prune": "троттл ретеншена журнала",
    "services/proxy_selector.py:_datacenter_ip_cache": "признак датацентрового IP",
    "services/railway_api.py:_discovered": "id сервисов Railway, читаются по API",
}

# Память — только кэш; истина лежит в БД и перечитывается.
DB_BACKED: dict[str, str] = {
    "services/ecosystem_copilot.py:_snooze_until": "platform_settings, перечитывается циклом",
    "services/infra_copilot.py:_snooze_until": "platform_settings, перечитывается циклом",
    "services/flood_engine.py:_flood_state": "гидрируется из БД на старте операции",
    "services/flood_engine.py:_hydrated": "отметка «уже гидрировали» для _flood_state",
    "services/infra_memory.py:_account_memory": "infra_memory_accounts, флаш приростом",
    "services/infra_memory.py:_proxy_memory": "infra_memory_proxies, флаш приростом",
    "services/infra_memory.py:_dirty_account_keys": "очередь флаша",
    "services/infra_memory.py:_dirty_proxy_keys": "очередь флаша",
    "services/op_circuit_breaker.py:_circuit_breaker_state": "op_circuit_breaker, решение под блокировкой строки",
}

# Расхождение реально стоит качества, но пока принято. СПИСОК НЕ ДОЛЖЕН РАСТИ.
KNOWN_DIVERGENCE: dict[str, str] = {
    "services/op_worker.py:_alerted_stuck_ops": "то же, дедуп «операция зависла»",
    "services/recovery_engine.py:_last_account_recovery":
        "троттл восстановления: реплики могут повторить попытку",
    "services/recovery_engine.py:_last_proxy_recovery": "то же для прокси",
    "services/op_worker.py:_owner_semaphores":
        "потолок параллельности на владельца считается в каждом процессе → N×",
    "services/auto_responder.py:_cycle_rule_counts": "лимит срабатываний правила за круг → N×",
    "services/auto_responder.py:_dead_token_cooldown": "пауза по мёртвому токену → повторные попытки",
    "services/chat_guard.py:_FLOOD": "антифлуд чата считается по процессу → порог мягче",
    "services/resource_selector.py:_account_usage": "равномерность ротации аккаунтов",
    "services/session_simulator.py:_adaptive_data": "адаптация поведения, копится заново",
    "services/ai_providers.py:_KEY_OVERRIDES": "ключ, заданный в рантайме, виден одному процессу",
    "services/managed_bot_webhooks.py:_secret_map":
        "секрет вебхука зарегистрирован в одном процессе (есть запасной путь)",
    "services/metrics.py:_counters": "метрики процесса; агрегируются на стороне сборщика",
    "services/metrics.py:_gauges": "то же: значения процесса, суммирует сборщик",
    "services/metrics.py:_sums": "то же: гистограммы процесса, суммирует сборщик",
    "services/proxy_scraper.py:_pool_updated_at": "остаток удалённого free-pool",
    "services/proxy_scraper.py:_valid_pool": "остаток удалённого free-pool",
}

REGISTRY: dict[str, str] = {}
for _group in (PROCESS_LOCAL, CACHE, DB_BACKED, KNOWN_DIVERGENCE):
    REGISTRY.update(_group)


# ── Тесты ─────────────────────────────────────────────────────────────────────

def test_every_mutable_global_is_classified():
    """Храповик: новый изменяемый глобал обязан получить группу и причину."""
    unknown = sorted(detect() - set(REGISTRY))
    assert not unknown, (
        "новое состояние в памяти процесса без классификации:\n  "
        + "\n  ".join(unknown)
        + "\n\nОтветьте на один вопрос: что случится, если это разойдётся между "
          "процессами (роли web/worker, несколько реплик)?\n"
          "  • корректность/доступ  → в БД, как platform_admin_sessions и "
          "op_circuit_breaker;\n"
          "  • иначе                → впишите в PROCESS_LOCAL / CACHE / "
          "DB_BACKED / KNOWN_DIVERGENCE в этом файле с причиной."
    )


def test_registry_has_no_stale_entries():
    """Реестр не должен превращаться в кладбище: удалили глобал — уберите запись."""
    found = detect()
    stale = sorted(k for k in REGISTRY if k not in found)
    assert not stale, (
        "в реестре есть записи о том, чего в коде уже нет:\n  " + "\n  ".join(stale))


def test_every_entry_carries_a_reason():
    empty = sorted(k for k, v in REGISTRY.items() if len(v.strip()) < 10)
    assert not empty, f"записи без внятной причины: {empty}"


def test_no_duplicate_classification():
    """Один глобал — одна группа: иначе классификация ничего не значит."""
    seen: dict[str, str] = {}
    dupes = []
    for gname, group in (("PROCESS_LOCAL", PROCESS_LOCAL), ("CACHE", CACHE),
                         ("DB_BACKED", DB_BACKED),
                         ("KNOWN_DIVERGENCE", KNOWN_DIVERGENCE)):
        for key in group:
            if key in seen:
                dupes.append(f"{key}: {seen[key]} и {gname}")
            seen[key] = gname
    assert not dupes, dupes


def test_known_divergence_does_not_grow():
    """Принятый долг не должен расти: новое состояние с ценой — сразу в БД."""
    assert len(KNOWN_DIVERGENCE) <= 16, (
        f"список принятых расхождений вырос до {len(KNOWN_DIVERGENCE)}. "
        "Это не место для новых записей — выносите состояние в БД. "
        "Уменьшили список? Опустите и порог, чтобы храповик не откатывался."
    )


def test_db_backed_entries_really_touch_the_database():
    """Пометка DB_BACKED — обещание. Проверяем, что модуль правда ходит в БД."""
    liars = []
    for key in DB_BACKED:
        rel, _name = key.rsplit(":", 1)
        src = open(os.path.join(ROOT, rel), encoding="utf-8", errors="ignore").read()
        if not any(w in src for w in ("pool.execute", "pool.fetch", "conn.execute",
                                      "conn.fetch", "set_platform_setting")):
            liars.append(key)
    assert not liars, (
        "помечено как «истина в БД», но обращений к БД в модуле нет: " + str(liars))


def test_detector_actually_detects():
    """Детектор, который ничего не находит, — зелёный и бесполезный."""
    found = detect()
    assert len(found) > 40, f"детектор нашёл всего {len(found)} — похоже, он сломан"
    assert "services/op_circuit_breaker.py:_circuit_breaker_state" in found
    assert "services/infra_memory.py:_account_memory" in found


def test_detector_ignores_constants_and_locals():
    """Ложные срабатывания обесценивают храповик: его начнут обходить."""
    tree = ast.parse(
        "X = {}\n"
        "LIMIT = 5\n"
        "def f():\n"
        "    y = {}\n"
        "    y['a'] = 1\n"
        "    return LIMIT\n"
        "def g():\n"
        "    X = {}\n"          # локальная тень — не мутация глобала
        "    X['a'] = 1\n")
    assert _mutated_in_functions(tree, _module_globals(tree)) == set()

    tree2 = ast.parse("X = {}\ndef f():\n    X['a'] = 1\n")
    assert _mutated_in_functions(tree2, _module_globals(tree2)) == {"X"}
