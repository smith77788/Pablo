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
        "ecosystems": _fv(
            pool, "SELECT COUNT(*) FROM ecosystems WHERE owner_id=$1", uid
        ),
        "parsed_recent": _fv(
            pool,
            "SELECT COUNT(*) FROM parsed_audiences WHERE owner_id=$1 "
            "AND parsed_at > NOW() - INTERVAL '7 days'",
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
    if state.get("channels", 0) >= 2 and state.get("ecosystems", 0) == 0:
        s.append(
            {
                "id": "build_ecosystem",
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
