"""Copilot «Что делать дальше» — контекстные подсказки следующих действий.

Закрывает жалобу пользователей: «система не предлагает дальнейшие возможности
основываясь на прежних действиях... приходится переключаться между разделами».

Движок читает РЕАЛЬНОЕ состояние владельца (аккаунты, прокси, операции, боты,
подписчики, воронки, авто-ответы, экосистемы, собранная аудитория) и его
последние операции из `operation_queue`, затем выдаёт приоритезированный список
конкретных следующих шагов. Каждый шаг несёт ссылку в нужный раздел мини-аппа
(`nav` — вкладка, `fn` — глобальная JS-функция открытия экрана), поэтому
пользователю не нужно искать раздел вручную.

Только реальные данные и реальные таблицы — никаких выдуманных метрик
(см. tg-manager/CLAUDE.md, ловушки #1/#3). Ничего не пишет в БД, состояние
подсказок не хранит — «отложить» реализуется на клиенте (localStorage).
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg

log = logging.getLogger(__name__)

# Порог «холодного»/рискового аккаунта по trust_score. Новые аккаунты имеют
# trust_score=NULL → COALESCE(...,1.0), поэтому под порог попадают только реально
# просевшие по доверию аккаунты, а не свежедобавленные (как в dashboard KPI).
_LOW_TRUST = 0.5


async def _fv(pool: asyncpg.Pool, q: str, *args):
    """fetchval, который не роняет весь копайлот из-за одной битой таблицы."""
    try:
        return await pool.fetchval(q, *args)
    except Exception as e:  # pragma: no cover - защита от рассинхрона схемы
        log.debug("next_actions fetchval failed: %s", e)
        return None


async def _fr(pool: asyncpg.Pool, q: str, *args):
    try:
        return await pool.fetch(q, *args)
    except Exception as e:  # pragma: no cover
        log.debug("next_actions fetch failed: %s", e)
        return []


async def _gather_state(pool: asyncpg.Pool, uid: int) -> dict:
    """Собирает состояние владельца одним параллельным раундом запросов."""
    q = {
        "acc_active": _fv(
            pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=true",
            uid,
        ),
        "acc_low_trust": _fv(
            pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=true "
            "AND COALESCE(trust_score, 1.0) < $2",
            uid,
            _LOW_TRUST,
        ),
        "acc_no_proxy": _fv(
            pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=true "
            "AND proxy_id IS NULL",
            uid,
        ),
        "acc_expired": _fv(
            pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=true "
            "AND acc_status='session_expired'",
            uid,
        ),
        "proxies_total": _fv(
            pool, "SELECT COUNT(*) FROM user_proxies WHERE owner_id=$1", uid
        ),
        "proxies_dead": _fv(
            pool,
            "SELECT COUNT(*) FROM user_proxies WHERE owner_id=$1 "
            "AND is_alive=false AND last_check IS NOT NULL",
            uid,
        ),
        "ops_failed_24h": _fv(
            pool,
            "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 "
            "AND status='failed' AND created_at > NOW() - INTERVAL '24 hours'",
            uid,
        ),
        "ops_pending": _fv(
            pool,
            "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND status='pending'",
            uid,
        ),
        "bots": _fv(
            pool, "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1", uid
        ),
        # Найденные на флоте, но ещё не подключённые боты. Без этого «0 ботов»
        # при живом флоте вело только в парсер, хотя боты могли уже существовать
        # на самих аккаунтах и их достаточно подключить.
        "bots_discovered_pending": _fv(
            pool,
            "SELECT COUNT(*) FROM discovered_bots "
            "WHERE owner_id=$1 AND linked_bot_id IS NULL",
            uid,
        ),
        "subscribers": _fv(
            pool,
            "SELECT COUNT(DISTINCT bu.user_id) FROM bot_users bu "
            "JOIN managed_bots mb ON mb.bot_id=bu.bot_id "
            "WHERE mb.added_by=$1 AND bu.is_active=true",
            uid,
        ),
        "dm_running": _fv(
            pool,
            "SELECT COUNT(*) FROM dm_campaigns WHERE owner_id=$1 AND status='running'",
            uid,
        ),
        "funnels": _fv(
            pool,
            "SELECT COUNT(*) FROM funnels f JOIN managed_bots mb ON mb.bot_id=f.bot_id "
            "WHERE mb.added_by=$1",
            uid,
        ),
        "auto_rules": _fv(
            pool,
            "SELECT COUNT(*) FROM automation_rules ar "
            "JOIN managed_bots mb ON mb.bot_id=ar.bot_id "
            "WHERE mb.added_by=$1 AND ar.is_active=true",
            uid,
        ),
        "channels": _fv(
            pool, "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", uid
        ),
        "channels_silent": _fv(
            pool,
            "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1 "
            "AND last_post_at IS NOT NULL AND last_post_at < NOW() - INTERVAL '7 days'",
            uid,
        ),
        "ecosystems": _fv(
            pool, "SELECT COUNT(*) FROM ecosystems WHERE owner_id=$1", uid
        ),
        "parsed_recent": _fv(
            pool,
            "SELECT COUNT(*) FROM parsed_audiences WHERE owner_id=$1 "
            "AND parsed_at > NOW() - INTERVAL '7 days'",
            uid,
        ),
        # ── Общие папки (chatlist_folders) ────────────────────────────────
        "folders_total": _fv(
            pool, "SELECT COUNT(*) FROM chatlist_folders WHERE owner_id=$1", uid
        ),
        # Папка без ссылки — мёртвый артефакт: экспорт сессией не удался или
        # завис в draft. Окно в 10 минут отсекает те, что создаются прямо сейчас.
        "folders_no_link": _fv(
            pool,
            "SELECT COUNT(*) FROM chatlist_folders WHERE owner_id=$1 "
            "AND invite_link IS NULL AND created_at < NOW() - INTERVAL '10 minutes'",
            uid,
        ),
        # ── Связки (network_builder) ──────────────────────────────────────
        "nets_total": _fv(
            pool, "SELECT COUNT(*) FROM network_instances WHERE owner_id=$1", uid
        ),
        "nets_empty": _fv(
            pool,
            "SELECT COUNT(*) FROM network_instances i WHERE i.owner_id=$1 "
            "AND NOT EXISTS (SELECT 1 FROM network_nodes n WHERE n.instance_id=i.id)",
            uid,
        ),
        # Узлы в 'pending' = связка нарисована, но не развёрнута: каналы/чаты
        # ещё не созданы и админки не назначены.
        "net_nodes_pending": _fv(
            pool,
            "SELECT COUNT(*) FROM network_nodes n "
            "JOIN network_instances i ON i.id=n.instance_id "
            "WHERE i.owner_id=$1 AND COALESCE(n.status,'pending')='pending'",
            uid,
        ),
        # ── Хранилище (Vault) ─────────────────────────────────────────────
        "vault_conn": _fv(
            pool,
            "SELECT COUNT(*) FROM business_connections WHERE owner_id=$1 "
            "AND is_enabled=true",
            uid,
        ),
        "vault_conn_off": _fv(
            pool,
            "SELECT COUNT(*) FROM business_connections WHERE owner_id=$1 "
            "AND is_enabled=false",
            uid,
        ),
        "vault_rules": _fv(
            pool,
            "SELECT COUNT(*) FROM vault_intent_rules WHERE owner_id=$1 AND is_active=true",
            uid,
        ),
        "vault_intents_7d": _fv(
            pool,
            "SELECT COUNT(*) FROM vault_intent_hits WHERE owner_id=$1 "
            "AND created_at > NOW() - INTERVAL '7 days'",
            uid,
        ),
        "recent_ops": _fr(
            pool,
            "SELECT op_type, status FROM operation_queue WHERE owner_id=$1 "
            "ORDER BY created_at DESC LIMIT 10",
            uid,
        ),
    }
    keys = list(q.keys())
    res = await asyncio.gather(*q.values(), return_exceptions=True)
    out: dict = {}
    for k, v in zip(keys, res):
        if isinstance(v, Exception):
            out[k] = 0 if k != "recent_ops" else []
        elif k == "recent_ops":
            out[k] = [dict(r) for r in (v or [])]
        else:
            out[k] = int(v or 0)
    return out


def _recent_op_types(state: dict) -> set[str]:
    return {
        (r.get("op_type") or "").lower()
        for r in state.get("recent_ops", [])
        if r.get("op_type")
    }


def _any_match(op_types: set[str], *needles: str) -> bool:
    return any(any(n in ot for n in needles) for ot in op_types)


def build_suggestions(state: dict) -> list[dict]:
    """Чистая функция: состояние → приоритезированные подсказки. Без БД —
    тестируется напрямую. Каждая подсказка: id, priority, icon, title, reason,
    cta, nav (вкладка|None), fn (JS-функция открытия|None)."""
    s: list[dict] = []
    acc = state.get("acc_active", 0)
    op_types = _recent_op_types(state)

    # 0. Совсем пустой аккаунт — первый шаг онбординга.
    if acc == 0:
        s.append(
            {
                "id": "add_first_account",
                "priority": 100,
                "icon": "📱",
                "title": "Добавьте первый аккаунт",
                "reason": "Без Telegram-аккаунтов недоступны массовые операции — "
                "начните с импорта сессии или авторегистрации.",
                "cta": "Добавить аккаунт",
                "nav": "accounts",
                "fn": None,
            }
        )
        # На пустом аккаунте остальные подсказки бессмысленны — но всё же
        # подскажем собрать аудиторию, если есть боты.

    # 1. Упавшие операции за сутки — разобрать причины (наивысший приоритет
    #    среди «рабочих», т.к. это прямая жалоба «падения операций»).
    if state.get("ops_failed_24h", 0) > 0:
        n = state["ops_failed_24h"]
        s.append(
            {
                "id": "review_failed_ops",
                "apply": "Перезапустить упавшие",
                "priority": 96,
                "icon": "❌",
                "title": f"Разберите {n} упавших операций за сутки",
                "reason": "Операции завершились ошибкой — откройте журнал, чтобы "
                "увидеть причину и перезапустить.",
                "cta": "Открыть операции",
                "nav": None,
                "fn": "openOps",
            }
        )

    # 2. КОНТЕКСТ: недавно собрали аудиторию → следующий логичный шаг — инвайт
    #    (ровно «дальнейшие возможности на основе прежних действий»).
    if state.get("parsed_recent", 0) > 0 and acc > 0:
        prio = 90 if not _any_match(op_types, "invite") else 60
        s.append(
            {
                "id": "invite_parsed_audience",
                "priority": prio,
                "icon": "🎯",
                "title": "Пригласите собранную аудиторию",
                "reason": f"За неделю собрано {state['parsed_recent']} контактов — "
                "запустите массовый инвайт в ваш канал или группу.",
                "cta": "Открыть инвайт",
                "nav": None,
                "fn": "openMassInvite",
            }
        )

    # 2.5. Сессии устарели (AuthKeyUnregistered при синке контактов и т.п.) — релог.
    #      Критично: пока не переавторизовать, операции с этими аккаунтами падают.
    if state.get("acc_expired", 0) > 0:
        n = state["acc_expired"]
        s.append(
            {
                "id": "relog_expired",
                "priority": 94,
                "icon": "🔑",
                "title": f"Переавторизуйте {n} аккаунт(ов)",
                "reason": "Сессии устарели — Telegram не признаёт ключ авторизации. "
                "Пока не сделаете релог, операции с этими аккаунтами будут падать.",
                "cta": "Открыть аккаунты",
                "nav": "accounts",
                "fn": None,
            }
        )

    # 3. Аккаунты без прокси — риск для изоляции (продукт про anti-detection).
    if state.get("acc_no_proxy", 0) > 0 and state.get("proxies_total", 0) >= 0:
        n = state["acc_no_proxy"]
        s.append(
            {
                "id": "assign_proxies",
                "apply": "Назначить прокси",
                "priority": 85,
                "icon": "🛡️",
                "title": f"Назначьте прокси {n} аккаунтам",
                "reason": "Аккаунты без выделенного IP делят отпечаток — это главный "
                "сигнал для бана. Назначьте прокси для изоляции.",
                "cta": "Открыть прокси",
                "nav": None,
                "fn": "openProxies",
            }
        )

    # 4. Мёртвые прокси — заменить.
    if state.get("proxies_dead", 0) > 0:
        n = state["proxies_dead"]
        s.append(
            {
                "id": "replace_dead_proxies",
                "apply": "Проверить все прокси",
                "priority": 83,
                "icon": "🔌",
                "title": f"{n} прокси не отвечают",
                "reason": "Проверка показала мёртвые прокси — замените их, иначе "
                "операции на этих IP будут падать.",
                "cta": "Проверить пул",
                "nav": None,
                "fn": "openProxyPool",
            }
        )

    # 5. Холодные/просевшие аккаунты — прогрев.
    if state.get("acc_low_trust", 0) > 0:
        n = state["acc_low_trust"]
        s.append(
            {
                "id": "warmup_cold_accounts",
                "apply": "Прогреть все",
                "priority": 78,
                "icon": "🔥",
                "title": f"Прогрейте {n} рисковых аккаунтов",
                "reason": "У аккаунтов просел trust — прогрев имитирует живое "
                "поведение и снижает риск блокировки.",
                "cta": "Открыть прогрев",
                "nav": None,
                "fn": "openWarmup",
            }
        )

    # 6. Есть подписчики, но ни одной активной рассылки — деньги на столе.
    if (
        state.get("bots", 0) > 0
        and state.get("subscribers", 0) > 0
        and state.get("dm_running", 0) == 0
    ):
        s.append(
            {
                "id": "reach_subscribers",
                "priority": 72,
                "icon": "📨",
                "title": "Запустите рассылку по подписчикам",
                "reason": f"У вас {state['subscribers']} подписчиков и нет активных "
                "рассылок — вовлеките их сообщением или воронкой.",
                "cta": "Создать рассылку",
                "nav": None,
                "fn": "openBotBroadcast",
            }
        )

    # 7. Есть боты, но нет воронок — автоматизация работает без участия.
    if state.get("bots", 0) > 0 and state.get("funnels", 0) == 0:
        s.append(
            {
                "id": "setup_funnel",
                "priority": 62,
                "icon": "🔄",
                "title": "Настройте авто-воронку",
                "reason": "Воронка ведёт нового подписчика по цепочке сообщений "
                "автоматически — работает 24/7 без вашего участия.",
                "cta": "Создать воронку",
                "nav": None,
                "fn": "openAutoFunnels",
            }
        )

    # 8. Есть боты, но нет авто-ответов.
    if state.get("bots", 0) > 0 and state.get("auto_rules", 0) == 0:
        s.append(
            {
                "id": "setup_autoresponder",
                "priority": 58,
                "icon": "🤖",
                "title": "Включите авто-ответы",
                "reason": "Авто-ответы реагируют на сообщения и ключевые слова "
                "мгновенно, даже когда вы офлайн.",
                "cta": "Настроить авто-ответы",
                "nav": None,
                "fn": "openArScreen",
            }
        )

    # 9. Несколько каналов, но не объединены в экосистему.
    # КОНТЕКСТ: каналы постили, но замолчали 7+ дн. — теряют охваты/вовлечённость.
    # Только для «затихших» (last_post_at был), не для только что добавленных.
    if state.get("channels_silent", 0) > 0:
        n = state["channels_silent"]
        s.append(
            {
                "id": "revive_silent_channels",
                "priority": 55,
                "icon": "🔔",
                "title": f"{n} каналов замолчали на неделю",
                "reason": "Каналы без новых постов теряют охваты и вовлечённость — "
                "алгоритм реже показывает. Опубликуйте пост или включите автопостинг.",
                "cta": "Открыть каналы",
                "nav": None,
                "fn": "openChannels",
            }
        )

    if state.get("channels", 0) >= 2 and state.get("ecosystems", 0) == 0:
        s.append(
            {
                "id": "build_ecosystem",
                "apply": "Создать из каналов",
                "priority": 52,
                "icon": "🌐",
                "title": f"Объедините {state['channels']} каналов в экосистему",
                "reason": "Экосистема показывает здоровье, риски и связи всех "
                "ресурсов в одном месте — не нужно проверять каждый по отдельности.",
                "cta": "Открыть экосистемы",
                "nav": None,
                "fn": "openEcosystems",
            }
        )

    # 9b. Найдены боты на флоте, но не подключены — самый короткий путь к
    # рабочим ботам: они уже существуют, осталось забрать токены у BotFather.
    _disc = state.get("bots_discovered_pending", 0)
    if _disc > 0:
        s.append(
            {
                "id": "connect_discovered_bots",
                "priority": 82,
                "icon": "🔌",
                "title": f"Подключите {_disc} найденных ботов",
                "reason": "Эти боты уже есть на ваших аккаунтах — Infragram заберёт "
                "их токены у @BotFather, и ими можно будет управлять.",
                "cta": "Подключить",
                "nav": None,
                "fn": "connectDiscoveredBots",
            }
        )

    # 9c. Флот есть, ботов нет и скан ещё не находил — боты могли быть созданы
    # с этих же аккаунтов, а прежняя подсказка вела в парсер мимо этого пути.
    if acc > 0 and state.get("bots", 0) == 0 and _disc == 0:
        s.append(
            {
                "id": "scan_fleet_bots",
                "priority": 70,
                "icon": "🔍",
                "title": "Проверьте флот на ботов",
                "reason": f"У вас {acc} аккаунтов и ни одного подключённого бота. "
                "Боты, созданные с этих аккаунтов, находятся сканом через @BotFather.",
                "cta": "Найти на флоте",
                "nav": None,
                "fn": "scanFleetBots",
            }
        )

    # 10. Есть аккаунты, но пусто по аудитории/ботам — предложить сбор аудитории.
    if (
        acc > 0
        and state.get("bots", 0) == 0
        and state.get("subscribers", 0) == 0
        and state.get("parsed_recent", 0) == 0
    ):
        s.append(
            {
                "id": "collect_audience",
                "priority": 48,
                "icon": "🧲",
                "title": "Соберите первую аудиторию",
                "reason": "Спарсите участников тематических чатов и каналов — это "
                "основа для инвайтов и рассылок.",
                "cta": "Открыть парсер",
                "nav": None,
                "fn": "openParser",
            }
        )

    # 11. КОНТЕКСТ: только что регистрировали/создавали аккаунты → канонический
    #     следующий шаг — прогрев (новые аккаунты имеют trust=NULL и не попадают
    #     под правило #5). Не дублируем, если warmup_cold_accounts уже добавлен.
    if (
        acc > 0
        and state.get("acc_low_trust", 0) == 0
        and _any_match(op_types, "auto_register", "reg_check", "bot_factory")
    ):
        s.append(
            {
                "id": "warmup_after_register",
                "apply": "Прогреть все",
                "priority": 76,
                "icon": "🔥",
                "title": "Прогрейте новые аккаунты",
                "reason": "Вы недавно создавали аккаунты — прогрев сразу после "
                "регистрации имитирует живое поведение и снижает риск бана.",
                "cta": "Открыть прогрев",
                "nav": None,
                "fn": "openWarmup",
            }
        )

    # 12. КОНТЕКСТ: пригласили аудиторию → следующий шаг вовлечения — рассылка
    #     (продолжение сценария). Не дублируем reach_subscribers (та про ботов).
    if (
        acc > 0
        and _any_match(op_types, "mass_invite")
        and not any(x["id"] == "reach_subscribers" for x in s)
    ):
        s.append(
            {
                "id": "engage_after_invite",
                "priority": 68,
                "icon": "📨",
                "title": "Вовлеките приглашённую аудиторию",
                "reason": "Вы недавно приглашали пользователей — закрепите результат "
                "приветственным сообщением или постом.",
                "cta": "Создать рассылку",
                "nav": None,
                "fn": "openBotBroadcast",
            }
        )

    # ── Папки, связки и Хранилище ─────────────────────────────────────────
    # Эти три модуля жили в копайлоте немыми: пользователь не узнавал ни о
    # сломанном артефакте (папка без ссылки), ни о неразвёрнутой связке, ни о
    # том, что архив перестал пополняться. Подсказки только по РЕАЛЬНЫМ
    # состояниям — «подключите Хранилище» всем подряд не показываем: это
    # требует Telegram Business и превращается в вечный баннер.

    # 14. Папка без ссылки — экспорт не удался или завис. Артефакт есть, толку
    #     нет: раздавать нечего.
    if state.get("folders_no_link", 0) > 0:
        n = state["folders_no_link"]
        s.append(
            {
                "id": "fix_folder_link",
                "priority": 66,
                "icon": "📂",
                "title": f"{n} папок без ссылки",
                "reason": "Папка собрана, но chatlist-ссылку выгрузить не удалось — "
                "раздавать нечего. Повторите экспорт другим аккаунтом.",
                "cta": "Открыть папки",
                "nav": None,
                "fn": "openFolders",
            }
        )

    # 15. Связка нарисована, но не развёрнута: узлы ждут создания каналов и
    #     назначения админок. Без этого связка — просто схема.
    if state.get("net_nodes_pending", 0) > 0:
        n = state["net_nodes_pending"]
        s.append(
            {
                "id": "deploy_network",
                "priority": 64,
                "icon": "🧩",
                "title": f"Разверните связку: {n} узлов ждут",
                "reason": "Узлы связки в состоянии «ожидает» — каналы и чаты ещё не "
                "созданы, админки не назначены. Разверните, чтобы схема заработала.",
                "cta": "Открыть связки",
                "nav": None,
                "fn": "openNetworkBuilder",
            }
        )

    # 16. Есть каналы, но ни одной общей папки — самый короткий путь раздать
    #     сразу всю связку одной ссылкой вместо N приглашений.
    if state.get("channels", 0) >= 2 and state.get("folders_total", 0) == 0:
        s.append(
            {
                "id": "create_shared_folder",
                "priority": 50,
                "icon": "🗂",
                "title": f"Соберите {state['channels']} каналов в общую папку",
                "reason": "Одна ссылка на папку добавляет человеку сразу всю связку "
                "каналов и чатов — вместо отдельного приглашения в каждый.",
                "cta": "Открыть папки",
                "nav": None,
                "fn": "openFolders",
            }
        )

    # 17. Связка создана, но пуста — узлы не добавлены.
    if state.get("nets_empty", 0) > 0:
        s.append(
            {
                "id": "fill_empty_network",
                "priority": 46,
                "icon": "🧩",
                "title": "Связка пуста — добавьте узлы",
                "reason": "Связка заведена, но в ней нет ни каналов, ни чатов, ни "
                "ботов. Добавьте узлы и рёбра, иначе разворачивать нечего.",
                "cta": "Открыть связки",
                "nav": None,
                "fn": "openNetworkBuilder",
            }
        )

    # 18. Есть и каналы, и боты, но они не связаны между собой.
    if (
        state.get("channels", 0) >= 1
        and state.get("bots", 0) >= 1
        and state.get("nets_total", 0) == 0
    ):
        s.append(
            {
                "id": "build_first_network",
                "priority": 44,
                "icon": "🕸",
                "title": "Свяжите бота с каналом",
                "reason": "Бот и канал работают порознь. Связка делает бота "
                "администратором канала и настраивает кросспостинг между ними.",
                "cta": "Открыть связки",
                "nav": None,
                "fn": "openNetworkBuilder",
            }
        )

    # 19. Хранилище отключено — архив молча перестал пополняться. Тихая потеря
    #     данных: переписка удаляется у собеседника и не остаётся нигде.
    if state.get("vault_conn_off", 0) > 0 and state.get("vault_conn", 0) == 0:
        s.append(
            {
                "id": "vault_reconnect",
                "priority": 74,
                "icon": "🗄",
                "title": "Хранилище отключено",
                "reason": "Подключение бизнес-аккаунта выключено — архив не "
                "пополняется, удалённые сообщения больше не сохраняются.",
                "cta": "Открыть Хранилище",
                "nav": None,
                "fn": "openVault",
            }
        )

    # 20. Сенсор поймал намерения — это горячие контакты, а не строчки журнала.
    if state.get("vault_intents_7d", 0) > 0:
        n = state["vault_intents_7d"]
        s.append(
            {
                "id": "review_intent_hits",
                "priority": 80,
                "icon": "🔥",
                "title": f"{n} сигналов о намерении за неделю",
                "reason": "Сенсор поймал в переписке фразы-намерения — это готовые к "
                "разговору контакты. Проверьте журнал, пока сигнал свежий.",
                "cta": "Открыть сенсор",
                "nav": None,
                "fn": "openIntentSensor",
            }
        )

    # 21. Хранилище работает, но правил нет — архив копится, сигналы никто не
    #     ловит: модуль наполовину выключен.
    if state.get("vault_conn", 0) > 0 and state.get("vault_rules", 0) == 0:
        s.append(
            {
                "id": "setup_intent_rules",
                "priority": 56,
                "icon": "🎣",
                "title": "Включите сенсор намерений",
                "reason": "Хранилище пишет переписку, но правил нет — «сколько стоит» "
                "и «готов купить» проходят мимо. Правило само двигает контакт по CRM.",
                "cta": "Настроить правила",
                "nav": None,
                "fn": "openIntentSensor",
            }
        )

    # 13. FALLBACK: активный пользователь без срочных подсказок всё равно должен
    #     видеть полезный следующий шаг (ТЗ: система всегда предлагает дальнейшие
    #     возможности). Проверка здоровья аккаунтов уместна всегда.
    if acc > 0 and not s:
        s.append(
            {
                "id": "check_account_health",
                "apply": "Проверить здоровье",
                "priority": 20,
                "icon": "💚",
                "title": "Проверьте здоровье аккаунтов",
                "reason": "Срочных задач нет — хороший момент оценить здоровье и "
                "риски аккаунтов, чтобы предупредить блокировки заранее.",
                "cta": "Открыть здоровье",
                "nav": None,
                "fn": "openHealth",
            }
        )

    # Дедуп по id, сортировка по приоритету (убыв.), затем по стабильному id.
    seen: set[str] = set()
    uniq: list[dict] = []
    for item in sorted(s, key=lambda x: (-x["priority"], x["id"])):
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        uniq.append(item)
    return uniq


async def compute_next_actions(
    pool: asyncpg.Pool, uid: int, limit: int = 5
) -> list[dict]:
    """Точка входа для API: состояние владельца → до `limit` подсказок."""
    try:
        state = await _gather_state(pool, uid)
    except Exception as e:  # pragma: no cover
        log.warning("next_actions state gather failed for uid=%s: %s", uid, e)
        return []
    return build_suggestions(state)[: max(0, limit)]
