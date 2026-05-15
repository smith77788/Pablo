"""🔁 AI Office Cycle — CEO диспетчеризует задачи по департаментам."""
from __future__ import annotations
import json
import logging
import os
import time
from datetime import datetime, timezone

from factory import db
from factory.agents.strategic_core import StrategicCore
from factory.agents.analytics_engine import AnalyticsEngine
from factory.agents.experiment_system import ExperimentSystem
from factory.agents.base import FactoryAgent
from factory.agents.decision_tracker import DecisionTracker
from factory.notifications import notify

logger = logging.getLogger(__name__)

NEVESTY_PRODUCT_NAME = "Nevesty Models Bot"


def _ensure_nevesty_product() -> int:
    existing = db.fetch_one("SELECT id FROM products WHERE name=?", (NEVESTY_PRODUCT_NAME,))
    if existing:
        return existing["id"]
    return db.insert("products", {
        "name": NEVESTY_PRODUCT_NAME,
        "description": "Telegram-бот агентства моделей. Бронирование моделей для мероприятий.",
        "status": "active",
        "source": "manual",
        "category": "marketplace",
        "monetization": "Комиссия с каждого заказа",
        "success_metrics": {"conversion_target": 5.0, "orders_target": 100},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


def _load_dept(name: str):
    """Ленивая загрузка департамента (чтобы не падать при отсутствии ключа)."""
    try:
        if name == "marketing":
            from factory.agents.marketing_dept import MarketingDepartment
            return MarketingDepartment()
        elif name == "product":
            from factory.agents.product_dept import ProductDepartment
            return ProductDepartment()
        elif name == "analytics":
            from factory.agents.analytics_dept import AnalyticsDepartment
            return AnalyticsDepartment()
        elif name == "hr":
            from factory.agents.hr_dept import HRDepartment
            return HRDepartment()
        elif name == "operations":
            from factory.agents.operations_dept import OperationsDepartment
            return OperationsDepartment()
        elif name == "tech":
            from factory.agents.tech_dept import TechDepartment
            return TechDepartment()
        elif name == "sales":
            from factory.agents.sales_dept import SalesDepartment
            return SalesDepartment()
        elif name == "creative":
            from factory.agents.creative_dept import CreativeDepartment
            return CreativeDepartment()
        elif name == "customer_success":
            from factory.agents.customer_success_dept import CustomerSuccessDepartment
            return CustomerSuccessDepartment()
        elif name == "finance":
            from factory.agents.finance_dept import FinanceDepartment
            return FinanceDepartment()
        elif name == "research":
            from factory.agents.research_dept import ResearchDepartment
            return ResearchDepartment()
    except Exception as e:
        logger.warning("Dept %s unavailable: %s", name, e)
    return None


def _send_ceo_memo_to_telegram(memo_text: str, health_score: int, growth_actions: list) -> None:
    """Отправляет CEO Memo в Telegram всем администраторам."""
    try:
        import os
        import requests  # type: ignore
        bot_token = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
        admin_ids_str = os.getenv("ADMIN_TELEGRAM_IDS", "")
        if not bot_token or not admin_ids_str:
            return

        admin_ids = [i.strip() for i in admin_ids_str.split(",") if i.strip()]

        score_emoji = "🟢" if health_score >= 70 else "🟡" if health_score >= 40 else "🔴"
        top_actions = "\n".join([f"• {a.get('action', '')}" for a in growth_actions[:3]])

        text = (
            f"🏭 AI Factory — Еженедельный отчёт CEO\n\n"
            f"{score_emoji} Здоровье бизнеса: {health_score}/100\n\n"
            f"📋 Топ-3 приоритета:\n{top_actions}\n\n"
            f"💡 CEO Memo:\n{memo_text[:500]}"
        )

        for admin_id in admin_ids:
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": admin_id, "text": text},
                timeout=10,
            )
    except Exception as e:
        logger.error("Failed to send CEO memo to Telegram: %s", e)


def _send_telegram_to_admins(text: str) -> bool:
    """Send a message to all admin Telegram IDs using the bot token."""
    import requests  # type: ignore
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '') or os.environ.get('BOT_TOKEN', '')
    admin_ids_str = os.environ.get('ADMIN_TELEGRAM_IDS', '')
    if not token or not admin_ids_str:
        return False

    admin_ids = [aid.strip() for aid in admin_ids_str.split(',') if aid.strip()]
    success = False
    for chat_id in admin_ids:
        try:
            resp = requests.post(
                f'https://api.telegram.org/bot{token}/sendMessage',
                json={'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'},
                timeout=10
            )
            if resp.status_code == 200:
                success = True
        except Exception as e:
            logger.error('[Factory] Telegram notify failed for %s: %s', chat_id, e)
    return success


def _sync_experiments_to_db(experiments: list, bot_db_path: str | None = None) -> None:
    """Save A/B experiment proposals to nevesty-models DB."""
    import os
    import sqlite3
    db_path = bot_db_path or os.path.join(
        os.path.dirname(__file__), '..', 'nevesty-models', 'data.db'
    )
    if not os.path.exists(db_path):
        logger.warning("Bot DB not found at %s", db_path)
        return
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS ab_experiments (
            id TEXT PRIMARY KEY, hypothesis TEXT, type TEXT DEFAULT 'both',
            metric TEXT, effort TEXT DEFAULT 'medium', expected_lift TEXT,
            status TEXT DEFAULT 'proposed', created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        for exp in experiments:
            cur.execute("""INSERT OR IGNORE INTO ab_experiments
                (id, hypothesis, type, metric, effort, expected_lift, status)
                VALUES (?,?,?,?,?,?,?)""",
                (exp.get('id', f'exp_{id(exp)}'), exp.get('hypothesis', ''),
                 exp.get('type', 'both'), exp.get('metric', ''),
                 exp.get('effort', 'medium'), exp.get('expected_lift', ''),
                 exp.get('status', 'proposed')))
        conn.commit()
        conn.close()
        logger.info("Synced %d experiments to bot DB", len(experiments))
    except Exception as e:
        logger.warning("Failed to sync experiments: %s", e)


def _sync_growth_actions_to_bot_db(growth_actions: list, bot_db_path: str) -> None:
    """Копирует growth actions из factory.db в bot БД для отображения в боте."""
    import os
    import sqlite3 as _sqlite3

    if not os.path.exists(bot_db_path):
        return
    try:
        conn = _sqlite3.connect(bot_db_path)
        conn.execute("""CREATE TABLE IF NOT EXISTS factory_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            priority INTEGER DEFAULT 5,
            department TEXT,
            expected_impact TEXT,
            status TEXT DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        for action in growth_actions:
            conn.execute(
                "INSERT INTO factory_tasks (action, priority, department, expected_impact) VALUES (?,?,?,?)",
                [
                    action.get("action", ""),
                    action.get("priority", 5),
                    action.get("department", ""),
                    action.get("expected_impact", ""),
                ],
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("Failed to sync growth actions: %s", e)


def _save_ceo_memo_to_settings(
    memo: str, health: int, dept_focus: str, experiment: dict, bot_db_path: str
) -> None:
    """Saves CEO memo and related fields to nevesty-models settings table."""
    import os
    import sqlite3 as _sqlite3
    import json as _json

    if not os.path.exists(bot_db_path):
        return
    try:
        conn = _sqlite3.connect(bot_db_path)
        updates = {
            "ceo_memo": memo or "",
            "ceo_health_score": str(health or ""),
            "ceo_department_focus": dept_focus or "",
            "ceo_experiment": _json.dumps(experiment, ensure_ascii=False) if experiment else "",
        }
        for key, value in updates.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [key, value]
            )
        conn.commit()
        conn.close()
        logger.info("Saved CEO memo to settings (health=%s, dept=%s)", health, dept_focus)
    except Exception as e:
        logger.error("Failed to save CEO memo to settings: %s", e)


def _save_cycle_to_history(results: dict) -> None:
    """Save this cycle's results to JSON history file."""
    import json
    from pathlib import Path

    history_dir = Path(__file__).parent / "history"
    history_dir.mkdir(exist_ok=True)

    timestamp = results.get("timestamp") or results.get("cycle_id", "unknown")
    cycle_file = history_dir / f"cycle_{str(timestamp)[:19].replace(':', '-')}.json"

    try:
        with open(cycle_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        logger.info("Cycle saved to %s", cycle_file)

        # Keep only last 30 cycles
        all_cycles = sorted(history_dir.glob("cycle_*.json"))
        for old in all_cycles[:-30]:
            old.unlink()
    except Exception as e:
        logger.warning("Failed to save cycle history: %s", e)


def _load_metrics_trend(metric_key: str, last_n: int = 5) -> list:
    """Load metric trend from last N cycles."""
    import json
    from pathlib import Path

    history_dir = Path(__file__).parent / "history"
    if not history_dir.exists():
        return []

    cycles = sorted(history_dir.glob("cycle_*.json"))[-last_n:]
    trend = []
    for cycle_file in cycles:
        try:
            with open(cycle_file) as f:
                data = json.load(f)
            val = data
            for k in metric_key.split('.'):
                val = val.get(k, {}) if isinstance(val, dict) else None
            if val is not None:
                trend.append({'timestamp': data.get('timestamp') or data.get('cycle_id'), 'value': val})
        except Exception:
            pass
    return trend


def _check_previous_actions_completion(prev_cycle: dict) -> str:
    """Сравнивает рекомендации прошлого цикла с текущими метриками.

    Возвращает текстовый отчёт для CEO о выполнении предыдущих growth_actions.
    """
    if not prev_cycle:
        return "Предыдущий цикл отсутствует — первый запуск."

    try:
        ceo_prev = prev_cycle.get("phases", {}).get("ceo_synthesis", {})
        prev_actions = ceo_prev.get("growth_actions", [])
        prev_focus = ceo_prev.get("weekly_focus", "")
        prev_health = ceo_prev.get("health_score", "?")
        prev_ts = prev_cycle.get("timestamp", "неизвестно")[:19]

        if not prev_actions:
            return f"Цикл {prev_ts}: growth_actions не зафиксированы (health_score={prev_health})."

        lines = [
            f"Прошлый цикл [{prev_ts}]: health_score={prev_health}, фокус='{prev_focus}'",
            f"Запланировано growth_actions: {len(prev_actions)}",
            "",
            "Рекомендации прошлого цикла:",
        ]
        for i, action in enumerate(prev_actions[:5], start=1):
            dept = action.get("department", "?")
            act_text = action.get("action", "—")
            impact = action.get("expected_impact", "?")
            lines.append(f"  {i}. [{dept}] {act_text} (ожидаемый эффект: {impact})")

        lines.append("")
        lines.append(
            "CEO: оцени выполнение каждого из этих действий — что было сделано, "
            "что дало результат, что провалилось и почему."
        )
        return "\n".join(lines)

    except Exception as e:
        logger.warning("_check_previous_actions_completion error: %s", e)
        return "Ошибка при анализе предыдущего цикла."


def _load_last_cycle_from_history() -> dict:
    """Загружает данные предыдущего (предпоследнего) цикла из истории."""
    import json
    from pathlib import Path

    history_dir = Path(__file__).parent / "history"
    if not history_dir.exists():
        return {}

    cycles = sorted(history_dir.glob("cycle_*.json"))
    # Берём предпоследний ([-2]), т.к. текущий ещё пишется
    if len(cycles) < 2:
        return {}

    try:
        with open(cycles[-2]) as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to load prev cycle: %s", e)
        return {}


def _format_weekly_report(cycle_results: list, nevesty_kpis: dict | None = None) -> str:
    """Generate a weekly summary from the last 7 cycle results.

    Args:
        cycle_results: List of recent cycle result dicts loaded from history.
        nevesty_kpis: Current KPIs dict with keys orders_this_week,
                      orders_this_month, revenue_month, avg_check,
                      conversion_rate_pct, avg_rating, health_score.
                      When provided, enriches the report with real business metrics.
    """
    if not cycle_results:
        return "Нет данных за неделю."

    kpis = nevesty_kpis or {}

    total_cycles = len(cycle_results)
    total_phases = sum(len(r.get("phases", {})) for r in cycle_results)
    errors = sum(
        1 for r in cycle_results
        for v in r.get("phases", {}).values()
        if isinstance(v, dict) and v.get("status") == "error"
    )
    success_rate = round((1 - errors / max(total_phases, 1)) * 100)

    # Extract health scores from history for trend
    health_scores = [
        r.get("health_score")
        for r in cycle_results[-7:]
        if isinstance(r.get("health_score"), (int, float))
    ]
    avg_health = round(sum(health_scores) / len(health_scores)) if health_scores else None
    latest_health = health_scores[-1] if health_scores else kpis.get("health_score")
    health_icon = "🟢" if (latest_health or 0) >= 70 else "🟡" if (latest_health or 0) >= 40 else "🔴"

    # Extract top growth action from last cycle
    top_action = ""
    for r in reversed(cycle_results[-7:]):
        ceo = r.get("phases", {}).get("ceo_synthesis", {})
        actions = ceo.get("growth_actions", [])
        if actions and isinstance(actions[0], dict):
            top_action = actions[0].get("action", "") or actions[0].get("title", "")
            if top_action:
                break
        elif actions and isinstance(actions[0], str):
            top_action = actions[0]
            if top_action:
                break

    lines = [
        "📊 ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ CEO",
        f"Циклов выполнено: {total_cycles}",
        f"Фаз обработано: {total_phases}",
        f"Ошибок: {errors} | Успешность: {success_rate}%",
        "",
    ]

    # Business KPIs section (when real data is available)
    orders_week = kpis.get("orders_this_week")
    orders_month = kpis.get("orders_this_month")
    revenue = kpis.get("revenue_month")
    avg_check = kpis.get("avg_check")
    conversion = kpis.get("conversion_rate_pct")
    avg_rating = kpis.get("avg_rating")

    if any(v is not None and v != 0 for v in [orders_week, revenue, conversion]):
        lines.append("📋 БИЗНЕС-МЕТРИКИ НЕДЕЛИ:")
        if orders_week is not None:
            lines.append(f"  Заявок за 7 дней: {orders_week}")
        if orders_month is not None:
            lines.append(f"  Заявок за месяц: {orders_month}")
        if revenue is not None and revenue != 0:
            lines.append(f"  Выручка (30д): {int(revenue):,} ₽".replace(",", " "))
        if avg_check is not None and avg_check != 0:
            lines.append(f"  Средний чек: {int(avg_check):,} ₽".replace(",", " "))
        if conversion is not None:
            lines.append(f"  Конверсия: {conversion}%")
        if avg_rating is not None and avg_rating != 0:
            lines.append(f"  Рейтинг моделей: {avg_rating}/5")
        lines.append("")

    # Factory health score
    if latest_health is not None:
        trend_str = ""
        if avg_health is not None and len(health_scores) > 1:
            delta = latest_health - health_scores[0]
            trend_str = f" (тренд: {'↑' if delta > 0 else '↓' if delta < 0 else '→'}{abs(int(delta))})"
        lines.append(f"{health_icon} Здоровье фабрики: {latest_health}/100{trend_str}")
        lines.append("")

    lines.append("🎯 Ключевые достижения недели:")

    # Collect highlights from each cycle
    highlights: set = set()
    for r in cycle_results[-7:]:
        for phase_name, phase_data in r.get("phases", {}).items():
            if isinstance(phase_data, dict) and phase_data.get("status") == "ok":
                highlights.add(f"✓ {phase_name.replace('_', ' ').title()}")

    lines.extend(list(highlights)[:5])
    lines.append("")

    # Top growth action
    if top_action:
        lines.append("🚀 Главное действие:")
        lines.append(f"  {top_action}")
        lines.append("")

    lines.append("📈 Рекомендации на следующую неделю:")
    lines.append("• Фокус на конверсии заявок")
    lines.append("• Обновление каталога моделей")
    lines.append("• Анализ отзывов клиентов")

    return "\n".join(lines)


def _format_monthly_report(cycle_results: list, db_path: str) -> str:
    """Generate a monthly summary with DB metrics."""
    lines = [
        "📊 ЕЖЕМЕСЯЧНЫЙ ОТЧЁТ CEO",
        f"Всего циклов за месяц: {len(cycle_results)}",
        "",
    ]

    try:
        import sqlite3 as _sqlite3_monthly
        conn = _sqlite3_monthly.connect(db_path)
        # Orders this month
        orders_month = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE created_at >= date('now', '-30 days')"
        ).fetchone()[0]
        # Revenue this month
        revenue = conn.execute(
            "SELECT COALESCE(SUM(budget),0) FROM orders WHERE status IN ('confirmed','completed') "
            "AND created_at >= date('now', '-30 days')"
        ).fetchone()[0]
        # New clients
        new_clients = conn.execute(
            "SELECT COUNT(DISTINCT client_chat_id) FROM orders WHERE created_at >= date('now', '-30 days')"
        ).fetchone()[0]
        conn.close()

        lines += [
            f"📋 Заявок за месяц: {orders_month}",
            f"💰 Выручка: {int(revenue):,} ₽".replace(",", " "),
            f"👥 Новых клиентов: {new_clients}",
            "",
            "🎯 Стратегические цели:",
            "• Увеличить конверсию на 15%",
            "• Расширить каталог на 10+ моделей",
            "• Запустить реферальную программу",
        ]
    except Exception as e:
        lines.append(f"(Ошибка получения метрик: {e})")

    return "\n".join(lines)


def run_phase_ceo_reports(db_path: str, history_path: str | None = None) -> dict:
    """Generate CEO weekly and monthly reports from cycle history."""
    import json as _json_ceo
    import os as _os_ceo
    from pathlib import Path as _Path_ceo

    # Load cycle history from factory/history/ directory
    cycle_results: list = []
    if history_path:
        if _os_ceo.path.exists(history_path):
            try:
                with open(history_path) as f:
                    cycle_results = _json_ceo.load(f)
            except Exception:
                cycle_results = []
    else:
        # Load from standard history directory
        history_dir = _Path_ceo(__file__).parent / "history"
        if history_dir.exists():
            cycles = sorted(history_dir.glob("cycle_*.json"))[-30:]
            for cycle_file in cycles:
                try:
                    with open(cycle_file) as f:
                        data = _json_ceo.load(f)
                    cycle_results.append({
                        "timestamp": data.get("timestamp") or data.get("cycle_id", ""),
                        "phases": data.get("phases", {}),
                    })
                except Exception:
                    pass

    weekly = _format_weekly_report(cycle_results)
    monthly = _format_monthly_report(cycle_results, db_path)

    return {
        "status": "ok",
        "weekly_report": weekly,
        "monthly_report": monthly,
        "weekly_lines": len(weekly.splitlines()),
        "monthly_lines": len(monthly.splitlines()),
        "cycles_loaded": len(cycle_results),
    }


def run_phase_25_channel_publisher(phase24_results: dict) -> dict:
    """Publish one post to Telegram channel if configured."""
    import urllib.request
    import urllib.parse
    import json as json_mod
    import os

    channel_id = os.environ.get('TELEGRAM_CHANNEL_ID', '').strip()
    bot_token = os.environ.get('TELEGRAM_BOT_TOKEN', '') or os.environ.get('BOT_TOKEN', '')

    if not channel_id or not bot_token:
        logger.info("Phase 25: TELEGRAM_CHANNEL_ID or BOT_TOKEN not set, skipping channel publish")
        return {"status": "skipped", "reason": "not_configured"}

    # Pick tips post from phase24 results
    tips_post = phase24_results.get('tips_post', '')
    if not tips_post:
        return {"status": "skipped", "reason": "no_content"}

    # Truncate to 4096 chars (Telegram limit)
    text = tips_post[:4096]

    # Send via Bot API
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json_mod.dumps({
        "chat_id": channel_id,
        "text": text,
        "parse_mode": "HTML"
    }).encode()

    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json_mod.loads(resp.read())
            if result.get('ok'):
                logger.info(f"Phase 25: Published to channel {channel_id}, message_id={result['result']['message_id']}")
                return {"status": "published", "channel": channel_id, "message_id": result['result']['message_id']}
            else:
                logger.warning(f"Phase 25: Telegram returned error: {result}")
                return {"status": "error", "detail": str(result)}
    except Exception as e:
        logger.warning(f"Phase 25: Channel publish failed: {e}")
        return {"status": "error", "detail": str(e)}


def _generate_heuristic_bio(model: dict) -> str:
    """Generate a professional model bio from parameters."""
    name = model.get('name') or 'Модель'
    city = model.get('city') or 'Москва'
    category = model.get('category') or 'events'
    height = model.get('height')
    age = model.get('age')

    cat_desc = {
        'fashion': 'подиумной и fashion-съёмке',
        'commercial': 'коммерческой рекламе и корпоративных мероприятиях',
        'events': 'мероприятиях и деловых встречах',
    }.get(category, 'различных мероприятиях')

    parts = [f"{name} — профессиональная модель из {city}, специализирующаяся на {cat_desc}."]

    if height:
        parts.append(f"Рост: {height} см.")
    if age:
        parts.append(f"Имея богатый опыт работы, она привносит профессионализм и элегантность в каждый проект.")

    parts.append(f"Открыта к новым предложениям и сотрудничеству с ведущими брендами и агентствами.")

    return " ".join(parts)


def run_phase_26_model_bios(db_path: str) -> dict:
    """Auto-generate bios for models missing them (heuristic, no API calls)."""
    import sqlite3
    import os

    if not os.path.exists(db_path):
        return {"status": "skipped", "reason": "db_not_found"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT id, name, age, height, city, category, hair_color, eye_color
            FROM models
            WHERE available=1 AND archived=0
              AND (bio IS NULL OR bio='' OR LENGTH(bio) < 50)
            LIMIT 3
        """)
        models = cur.fetchall()

        if not models:
            conn.close()
            return {"status": "ok", "updated": 0, "message": "All models have bios"}

        updated = 0
        for m in models:
            bio = _generate_heuristic_bio(dict(m))
            cur.execute(
                "UPDATE models SET bio=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                [bio, m['id']]
            )
            updated += 1

        conn.commit()
        conn.close()
        return {"status": "ok", "updated": updated}
    except Exception as e:
        conn.close()
        return {"status": "error", "detail": str(e)}


def run_phase_27_faq_generator(db_path: str) -> dict:
    """Phase 27: FAQ content generation and improvement suggestions."""
    try:
        from factory.agents.faq_generator import FAQGenerator
        gen = FAQGenerator()
        result = gen.run(db_path)
        return {
            'status': 'ok',
            'existing_faq': result['existing_count'],
            'suggestions': len(result['suggestions']),
            'improved': result['improved_count'],
        }
    except Exception as e:
        return {'status': 'error', 'error': str(e)}


def run_phase_28_experiments(history_path: str | None = None) -> dict:
    """Phase 28: A/B Experiment System."""
    try:
        from factory.agents.experiment_system import HeuristicExperimentSystem
        system = HeuristicExperimentSystem(history_path=history_path)
        return system.run_cycle()
    except Exception as e:
        return {'status': 'error', 'error': str(e)}


def run_phase_research(db_path: str) -> dict:
    """Research Department heuristic analysis — market, competitors, trends, insights."""
    try:
        import sqlite3
        from factory.agents.research_department import (
            MarketResearcher, CompetitorAnalyst, TrendSpotter, InsightSynthesizer
        )

        researcher = MarketResearcher()
        analyst = CompetitorAnalyst()
        spotter = TrendSpotter()
        synthesizer = InsightSynthesizer()

        # Query DB for top segment and performance data
        top_segment = "commercial"
        conv_rate = 0.0
        avg_budget = 0.0
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            seg_row = conn.execute(
                "SELECT event_type, COUNT(*) as cnt FROM orders "
                "WHERE created_at >= date('now', '-30 days') AND event_type IS NOT NULL "
                "GROUP BY event_type ORDER BY cnt DESC LIMIT 1"
            ).fetchone()
            if seg_row:
                top_segment = seg_row["event_type"] or "commercial"

            stats_row = conn.execute(
                "SELECT COUNT(CASE WHEN status IN ('confirmed','completed') THEN 1 END) * 1.0 / MAX(COUNT(*), 1) as conv, "
                "AVG(CASE WHEN budget > 0 THEN budget END) as avg_b FROM orders "
                "WHERE created_at >= date('now', '-30 days')"
            ).fetchone()
            if stats_row:
                conv_rate = float(stats_row["conv"] or 0)
                avg_budget = float(stats_row["avg_b"] or 0)
            conn.close()
        except Exception:
            pass

        market = researcher.analyze_market_segment(top_segment)
        gaps = analyst.identify_competitive_gaps(["fashion", "events", "commercial"])
        trends = spotter.get_actionable_trends()[:3]
        insights = synthesizer.synthesize_insights(
            market, gaps, trends,
            {"conversion_rate": conv_rate, "avg_budget": avg_budget}
        )

        return {
            "status": "ok",
            "top_segment": top_segment,
            "market_opportunity_score": market.get("opportunity_score", 0),
            "top_opportunities": insights.get("top_opportunities", []),
            "strategic_alerts": insights.get("strategic_alerts", []),
            "confidence": insights.get("confidence_level", "low"),
            "conv_rate": conv_rate,
            "avg_budget": avg_budget,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def run_phase_finance(db_path: str) -> dict:
    """Finance Department heuristic analysis — revenue forecast, cost analysis, budget plan."""
    try:
        import sqlite3
        from factory.agents.finance_department import FinanceDepartment

        conn = sqlite3.connect(db_path)
        rows = conn.execute("""
            SELECT strftime('%Y-%m', created_at) as month,
                   COALESCE(SUM(budget), 0) as revenue
            FROM orders
            WHERE status IN ('confirmed', 'completed')
            AND created_at >= date('now', '-6 months')
            GROUP BY month
            ORDER BY month
        """).fetchall()
        conn.close()

        revenue_history = [r[1] for r in rows] if rows else [0]
        dept = FinanceDepartment()
        result = dept.run_analysis({'revenue_history': revenue_history, 'costs': {}})
        return {'status': 'ok', **result}
    except Exception as e:
        return {'status': 'error', 'error': str(e)}


# Threshold for auto-apply: variant B must be at least 3% conversion to auto-apply
SCALE_THRESHOLD_AUTO = 3.0


def _auto_apply_successful_experiments(nevesty_id: int) -> list[str]:
    """Check running experiments older than 7 days; if metrics improved, mark as successful.

    Returns list of experiment names that were auto-applied.
    """
    from datetime import timedelta

    applied_names = []
    try:
        running_exps = db.fetch_all(
            "SELECT * FROM experiments WHERE status='running' ORDER BY started_at ASC"
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        for exp in running_exps:
            started_at = exp.get("started_at", "")
            if not started_at or started_at > cutoff:
                # Experiment hasn't been running for 7+ days
                continue

            conv_a = exp.get("conversion_a", 0) or 0
            conv_b = exp.get("conversion_b", 0) or 0

            # Determine if variant B shows improvement vs variant A
            improved = False
            if conv_b > 0 and conv_a > 0 and conv_b > conv_a:
                improved = True
            elif conv_b >= SCALE_THRESHOLD_AUTO:
                improved = True

            if improved:
                note = (
                    f"Авто-применення: вариант B конвертирует {conv_b:.1f}% "
                    f"vs A {conv_a:.1f}% після 7+ днів тесту"
                )
                db.execute(
                    "UPDATE experiments SET status='concluded', result='successful', "
                    "concluded_at=?, notes=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(), note, exp["id"]),
                )
                db.insert("growth_actions", {
                    "product_id": exp.get("product_id") or nevesty_id,
                    "action_type": "experiment_auto_apply",
                    "channel": "internal",
                    "content": f"✅ Эксперимент [{exp['name']}] применён автоматически. {note}",
                    "status": "done",
                    "priority": 8,
                    "outcome": "success",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
                applied_names.append(exp["name"])
                logger.info(
                    "[AutoApply] Experiment '%s' (id=%d) marked successful: conv_a=%.1f%%, conv_b=%.1f%%",
                    exp["name"], exp["id"], conv_a, conv_b,
                )
    except Exception as e:
        logger.error("_auto_apply_successful_experiments error: %s", e)

    return applied_names


def _maybe_generate_weekly_summary(cycle_id: str, results: dict) -> dict | None:
    """Generate a weekly factory summary if none has been created in the last 7 days.

    Saves to factory_reports table and returns a summary dict, or None if skipped.
    """
    import datetime as _dt

    try:
        period_key = _dt.date.today().strftime("%G-W%V")  # ISO week, e.g. '2026-W20'

        # Check if a weekly summary for this week already exists
        existing = db.fetch_one(
            "SELECT id FROM factory_reports WHERE report_type='weekly' AND period_key=?",
            (period_key,)
        )
        if existing:
            logger.info("[WeeklySummary] Already exists for %s — skipping", period_key)
            return None

        # Also check if last summary was within 7 days (guard for mid-week runs)
        last_summary = db.fetch_one(
            "SELECT created_at FROM factory_reports WHERE report_type='weekly' "
            "ORDER BY created_at DESC LIMIT 1"
        )
        if last_summary:
            last_ts_str = last_summary.get("created_at", "")
            try:
                last_ts = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
                days_since = (datetime.now(timezone.utc) - last_ts).days
                if days_since < 7:
                    logger.info(
                        "[WeeklySummary] Last summary was %d days ago — skipping", days_since
                    )
                    return None
            except Exception:
                pass

        # Gather data for the summary
        health_score = results.get("health_score", 50)
        health_trend = _load_metrics_trend("health_score", last_n=5)
        trend_values = [c.get("value") for c in health_trend if c.get("value") is not None]

        recent_decisions = db.get_recent_ceo_decisions(limit=5)
        top_growth_actions = db.fetch_all(
            "SELECT action_type, content, outcome, created_at FROM growth_actions "
            "WHERE outcome='success' OR (status='done' AND created_at >= date('now', '-7 days')) "
            "ORDER BY priority DESC LIMIT 5"
        )

        # Identify best-performing department from current cycle phases
        dept_phases = {
            k: v for k, v in results.get("phases", {}).items()
            if k not in ("analytics", "ceo", "ceo_synthesis", "departments", "ideas",
                         "experiment_auto_apply", "weekly_factory_summary", "monthly_report",
                         "experiment_tracking", "ab_experiments", "content_generation")
        }

        top_dept = ""
        if dept_phases:
            # Prefer phase with most roles_used
            best = max(
                dept_phases.items(),
                key=lambda kv: len(kv[1].get("roles_used", [])) if isinstance(kv[1], dict) else 0,
                default=(None, None),
            )
            if best[0]:
                top_dept = best[0]

        summary_data = {
            "period_key": period_key,
            "health_score": health_score,
            "health_trend": trend_values,
            "top_department": top_dept,
            "top_growth_actions": [
                ga.get("content", "")[:100] for ga in top_growth_actions
            ],
            "ceo_decisions_count": len(recent_decisions),
            "last_ceo_focus": recent_decisions[0].get("weekly_focus", "") if recent_decisions else "",
        }

        db.insert("factory_reports", {
            "report_type": "weekly",
            "period_key": period_key,
            "report_json": json.dumps(summary_data, ensure_ascii=False, default=str),
        })

        logger.info(
            "[WeeklySummary] Generated for %s: health=%s, top_dept=%s, actions=%d",
            period_key, health_score, top_dept, len(top_growth_actions),
        )
        return summary_data

    except Exception as e:
        logger.error("_maybe_generate_weekly_summary error: %s", e)
        return None


def get_monthly_metrics(data_db) -> dict:
    """Get order metrics for current month from data.db."""
    import datetime as _dt
    try:
        month_start = _dt.date.today().replace(day=1).isoformat()
        row = data_db.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status='new' THEN 1 ELSE 0 END) as new_orders
            FROM orders
            WHERE created_at >= ?
        """, (month_start,)).fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}


def get_top_models(data_db, limit: int = 5) -> list:
    """Get top models by orders this month."""
    import datetime as _dt
    try:
        month_start = _dt.date.today().replace(day=1).isoformat()
        rows = data_db.execute("""
            SELECT m.name, COUNT(o.id) as order_count
            FROM orders o
            JOIN models m ON o.model_id = m.id
            WHERE o.created_at >= ?
            GROUP BY m.id ORDER BY order_count DESC LIMIT ?
        """, (month_start, limit)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_revenue_trend(data_db) -> list:
    """Get last 3 months order counts for trend."""
    try:
        rows = data_db.execute("""
            SELECT strftime('%Y-%m', created_at) as month, COUNT(*) as cnt
            FROM orders
            WHERE created_at >= date('now', '-3 months')
            GROUP BY month ORDER BY month
        """).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _notify_admins_telegram(briefing: dict, decisions: list, cycle_id: str) -> None:
    """Send factory cycle summary to all Telegram admins via notify.js."""
    import os
    import subprocess

    health = briefing.get('health_score', '?')
    focus = briefing.get('next_cycle_focus') or briefing.get('ceo_department_focus', '?')
    summary = briefing.get('summary', '')[:200]
    actions = len([d for d in decisions if d.get('type') in ('grow', 'create_mvp', 'scale')])

    msg = (
        f"🏭 Factory цикл завершён\n"
        f"📊 Health: {health}/100 | Focus: {focus}\n"
        f"📋 Решений: {len(decisions)} | Активных action: {actions}\n"
        f"💡 {summary}"
    )

    notify_script = os.path.join(os.path.dirname(__file__), '..', 'nevesty-models', 'tools', 'notify.js')
    notify_script = os.path.abspath(notify_script)

    if os.path.exists(notify_script):
        try:
            subprocess.run(
                ['node', notify_script, '--from', 'AI Factory', msg],
                cwd=os.path.dirname(notify_script),
                timeout=10,
                capture_output=True
            )
        except Exception:
            pass  # Don't fail the cycle on notification error


def run_cycle() -> dict:
    """Один полный цикл AI-офиса. Возвращает сводку."""
    cycle_start = time.time()
    cycle_id = datetime.now(timezone.utc).isoformat()
    summary_lines = []

    logger.info("=" * 60)
    logger.info("🏢 AI OFFICE CYCLE: %s", cycle_id)
    logger.info("=" * 60)

    # Check if real Anthropic API key is configured — phases that call Claude are skipped without it
    from factory.agents.base import API_AVAILABLE as _api_available
    if not _api_available:
        logger.warning(
            "⚠️  No real ANTHROPIC_API_KEY detected — AI department phases will be skipped. "
            "Only heuristic phases will run (fast cycle)."
        )
    else:
        logger.info("✅ Anthropic API key detected — full AI cycle enabled.")

    db.init_db()
    nevesty_id = _ensure_nevesty_product()

    # ════════════════════════════════════════════════════════════════
    # PHASE 0 — DECISION TRACKING: load last cycle's factory_tasks
    # ════════════════════════════════════════════════════════════════
    previous_decisions: list = []
    decision_tracker = DecisionTracker()
    decision_accountability_report = ""
    try:
        import sqlite3 as _sqlite3_dt
        import os as _os_dt
        _bot_db = _os_dt.path.join(_os_dt.path.dirname(__file__), '..', 'nevesty-models', 'nevesty.db')
        _bot_db = _os_dt.path.abspath(_bot_db)
        if _os_dt.path.exists(_bot_db):
            _conn_dt = _sqlite3_dt.connect(_bot_db)
            _conn_dt.row_factory = _sqlite3_dt.Row
            _rows = _conn_dt.execute(
                "SELECT action, status, created_at FROM factory_tasks "
                "WHERE status IN ('done', 'in_progress', 'pending') "
                "ORDER BY created_at DESC LIMIT 5"
            ).fetchall()
            _conn_dt.close()
            previous_decisions = [dict(r) for r in _rows]
            _summary = decision_tracker.get_execution_summary(previous_decisions)
            decision_accountability_report = decision_tracker.generate_accountability_report(_summary)
            logger.info(
                "[Phase0] Decision tracking: done=%s, in_progress=%s, pending=%s, rate=%.0f%%",
                _summary["done_count"], _summary["in_progress_count"],
                _summary["pending_count"], _summary["execution_rate"] * 100,
            )
    except Exception as _e_dt:
        logger.warning("[Phase0] Decision tracking unavailable: %s", _e_dt)

    db.insert("cycles", {
        "id": cycle_id,
        "phase": "started",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "summary": "Цикл запущен",
    })

    # ════════════════════════════════════════════════════════════════
    # PHASE 5.7 — NEVESTY METRICS: collect real KPIs from Nevesty DB
    # ════════════════════════════════════════════════════════════════
    from factory.agents.metrics_collector import MetricsCollector
    _mc = MetricsCollector()
    nevesty_kpis_raw = _mc.collect_all()
    nevesty_kpis = {
        'orders_this_week': nevesty_kpis_raw.get('orders_week', 0),
        'orders_this_month': nevesty_kpis_raw.get('orders_month', 0),
        'conversion_rate_pct': nevesty_kpis_raw.get('conversion_rate', 0),
        'revenue_month': nevesty_kpis_raw.get('revenue_month', 0),
        'avg_check': nevesty_kpis_raw.get('avg_check', 0),
        'pipeline_value': nevesty_kpis_raw.get('pipeline_value', 0),
        'models_active': nevesty_kpis_raw.get('models_total', 0),
        'clients_total': nevesty_kpis_raw.get('clients_unique', 0),
        'repeat_client_rate': round(
            nevesty_kpis_raw.get('clients_repeat', 0) /
            max(nevesty_kpis_raw.get('clients_unique', 1), 1) * 100, 1
        ),
        'avg_rating': nevesty_kpis_raw.get('avg_rating', 0),
        'db_connected': nevesty_kpis_raw.get('db_available', False),
    }
    logger.info(
        "[Phase5.7] Nevesty KPIs: orders_week=%s, conversion=%.1f%%, revenue_month=%s, db=%s",
        nevesty_kpis['orders_this_week'],
        nevesty_kpis['conversion_rate_pct'],
        nevesty_kpis['revenue_month'],
        nevesty_kpis['db_connected'],
    )
    # Save metrics snapshot to factory DB for historical tracking
    try:
        db.run(
            "INSERT OR IGNORE INTO metrics_snapshots (cycle_id, data, collected_at) VALUES (?, ?, ?)",
            (cycle_id, json.dumps(nevesty_kpis_raw, default=str), nevesty_kpis_raw.get('collected_at', ''))
        )
    except Exception:
        pass  # metrics snapshot is non-critical

    results = {
        "cycle_id": cycle_id,
        "timestamp": cycle_id,
        "phases": {},
        "decisions": [],
        "new_actions": 0,
        "experiments_concluded": 0,
        "health_score": 50,
        "nevesty_kpis": nevesty_kpis,
    }

    # ════════════════════════════════════════════════════════════════
    # PHASE 1 — ANALYTICS DEPT: собирает данные и инсайты
    # ════════════════════════════════════════════════════════════════
    logger.info("\n📊 ANALYTICS DEPT")
    insights = {"health_score": 50, "recommended_focus": "conversion"}
    all_metrics = {}
    try:
        analytics_engine = AnalyticsEngine()
        all_metrics = analytics_engine.collect_all_metrics()
        # Enrich all_metrics with real Nevesty KPIs from MetricsCollector
        all_metrics['nevesty_kpis'] = nevesty_kpis
        all_metrics['nevesty_kpis_raw'] = nevesty_kpis_raw
        raw_insights = analytics_engine.analyze(all_metrics)
        analytics_engine.persist_nevesty_metrics(nevesty_id, all_metrics.get("nevesty_models", {}))

        # Расширенный анализ через Analytics Department (только с реальным API ключом)
        running_exps = db.get_running_experiments()
        analytics_dept = _load_dept("analytics") if _api_available else None
        if analytics_dept:
            dept_insights = analytics_dept.run_full_analysis(all_metrics, running_exps)
            insights = {**raw_insights, **dept_insights}
            results["experiments_concluded"] = len([
                e for e in dept_insights.get("experiment_evaluations", [])
                if e.get("decision") in ("scale", "kill")
            ])
        else:
            insights = raw_insights

        results["health_score"] = insights.get("health_score", 50)
        results["phases"]["analytics"] = {
            "health_score": insights.get("health_score"),
            "top_problem": insights.get("top_problem"),
            "focus": insights.get("recommended_focus"),
        }
        summary_lines.append(f"📊 Health Score: {insights.get('health_score')}%")
        summary_lines.append(f"🎯 Фокус: {insights.get('recommended_focus', '—')}")
        logger.info("Analytics: score=%s, focus=%s", insights.get("health_score"), insights.get("recommended_focus"))
    except Exception as e:
        logger.error("Analytics phase error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 2 — CEO CORE: стратегические решения и диспетчеризация
    # ════════════════════════════════════════════════════════════════
    logger.info("\n🧠 CEO CORE")
    decisions = []
    try:
        # Build execution context from previous cycle's factory_tasks
        executed = [t.get("action", "") for t in previous_decisions if t.get("status") == "done"]
        pending = [t.get("action", "") for t in previous_decisions if t.get("status") == "in_progress"]
        ceo_prev_context = {
            "executed_last_cycle": executed,
            "still_in_progress": pending,
            "accountability_report": decision_accountability_report,
        }
        ceo = StrategicCore()
        decisions, _ = ceo.decide(insights, {**all_metrics, "previous_decisions": ceo_prev_context})
        results["decisions"] = decisions
        results["phases"]["ceo"] = {
            "decisions_count": len(decisions),
            "prev_executed_count": len(executed),
            "prev_pending_count": len(pending),
        }
        summary_lines.append(f"🧠 Решений CEO: {len(decisions)}")
        if decision_accountability_report:
            summary_lines.append(decision_accountability_report.strip())
        for d in decisions:
            logger.info("  [CEO] %s → %s", d.get("type"), d.get("rationale", "")[:60])
    except Exception as e:
        logger.error("CEO phase error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 3 — DEPARTMENTS EXECUTION: CEO диспетчеризует задачи
    # ════════════════════════════════════════════════════════════════
    logger.info("\n🏢 DEPARTMENTS EXECUTION")

    dept_marketing = _load_dept("marketing") if _api_available else None
    dept_product = _load_dept("product") if _api_available else None
    dept_hr = _load_dept("hr") if _api_available else None
    experiment_sys = ExperimentSystem()

    total_new_actions = 0
    new_experiments = 0
    new_mvps = 0

    for decision in decisions:
        dtype = decision.get("type")
        product_id = decision.get("product_id") or nevesty_id
        product = db.fetch_one("SELECT * FROM products WHERE id=?", (product_id,))
        task = {"action": decision.get("action", dtype), "decision": decision}

        try:
            if dtype == "create_mvp":
                from factory.agents.product_factory import ProductFactory
                pf = ProductFactory()
                ideas = pf.generate_ideas(count=3, context={"decision": decision})
                if ideas:
                    mvp = pf.create_mvp(idea_id=ideas[0]["_db_id"], decision=decision)
                    if mvp.get("_product_id"):
                        new_mvps += 1
                        summary_lines.append(f"📦 MVP создан: {mvp['name']}")

            elif dtype in ("grow", "optimize"):
                if dept_marketing:
                    task["action"] = "growth_experiment content seo"
                    actions = dept_marketing.execute_task(task, insights, product_id)
                    total_new_actions += len(actions)
                elif dept_product:
                    task["action"] = "ux conversion"
                    actions = dept_product.execute_task(task, insights, product)
                    total_new_actions += len(actions)

            elif dtype == "experiment":
                exp_id = experiment_sys.auto_create_for_product(product_id, decision)
                if exp_id:
                    new_experiments += 1
                    summary_lines.append(f"🧪 Эксперимент запущен (id={exp_id})")

            elif dtype == "scale":
                db.execute("UPDATE products SET status='scaled', updated_at=? WHERE id=?",
                           (datetime.now(timezone.utc).isoformat(), product_id))
                summary_lines.append(f"🚀 Продукт {product_id} → SCALE")

            elif dtype == "kill":
                db.execute("UPDATE products SET status='killed', updated_at=? WHERE id=?",
                           (datetime.now(timezone.utc).isoformat(), product_id))
                summary_lines.append(f"💀 Продукт {product_id} → KILL")

            elif dtype == "iterate":
                if dept_product:
                    task["action"] = "roadmap iterate ux"
                    actions = dept_product.execute_task(task, insights, product)
                    total_new_actions += len(actions)

            if "_db_id" in decision:
                db.execute("UPDATE decisions SET executed=1 WHERE id=?", (decision["_db_id"],))

        except Exception as e:
            logger.error("Dept execution error [%s]: %s", dtype, e)

    # Fallback: если нет решений — маркетинг всё равно работает
    if total_new_actions == 0:
        try:
            if dept_marketing:
                focus = insights.get("recommended_focus", "conversion")
                fallback_task = {"action": f"content growth seo {focus}"}
                actions = dept_marketing.execute_task(fallback_task, insights, nevesty_id)
                total_new_actions += len(actions)
        except Exception as e:
            logger.error("Marketing fallback error: %s", e)

    # HR Department — ранжирование моделей раз в цикл
    try:
        if dept_hr:
            hr_actions = dept_hr.run_model_optimization(nevesty_id)
            total_new_actions += len(hr_actions)
    except Exception as e:
        logger.error("HR dept error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 5 — OPERATIONS + TECH DEPARTMENTS
    # ════════════════════════════════════════════════════════════════
    logger.info("\n⚙️ OPERATIONS + TECH DEPTS")

    # Operations Department
    try:
        ops = _load_dept("operations") if _api_available else None
        if ops:
            ops_result = ops.execute_task(
                ceo_decision.get("focus", "optimize operations") if (ceo_decision := next(iter(decisions), {})) else "optimize operations",
                context if (context := {"insights": insights, "metrics": all_metrics}) else {},
            )
            logger.info("[Phase5] Operations: roles_used=%s", ops_result.get("roles_used", []))
            results["phases"]["operations"] = {
                "roles_used": ops_result.get("roles_used", []),
                "timestamp": ops_result.get("timestamp"),
            }
            summary_lines.append(f"⚙️ Operations: {', '.join(ops_result.get('roles_used', []))}")
    except Exception as e:
        logger.error("Operations dept phase error: %s", e)

    # Tech Department
    try:
        tech = _load_dept("tech") if _api_available else None
        if tech:
            tech_focus = next(iter(decisions), {}).get("focus", "improve system")
            tech_result = tech.execute_task(
                tech_focus,
                {"insights": insights, "metrics": all_metrics},
            )
            logger.info("[Phase5] Tech: roles_used=%s", tech_result.get("roles_used", []))
            results["phases"]["tech"] = {
                "roles_used": tech_result.get("roles_used", []),
                "timestamp": tech_result.get("timestamp"),
            }
            summary_lines.append(f"🛠️ Tech: {', '.join(tech_result.get('roles_used', []))}")
    except Exception as e:
        logger.error("Tech dept phase error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 6 — SALES + CREATIVE + CUSTOMER SUCCESS DEPARTMENTS
    # ════════════════════════════════════════════════════════════════
    logger.info("\n💼 SALES + CREATIVE + CUSTOMER SUCCESS DEPTS")

    # Sales Department
    try:
        sales = _load_dept("sales") if _api_available else None
        if sales:
            sales_focus = next(iter(decisions), {}).get("focus", "lead qualification pricing")
            sales_result = sales.execute_task(
                sales_focus,
                {"insights": insights, "metrics": all_metrics},
            )
            logger.info("[Phase6] Sales: roles_used=%s", sales_result.get("roles_used", []))
            results["phases"]["sales"] = {
                "roles_used": sales_result.get("roles_used", []),
                "timestamp": sales_result.get("timestamp"),
            }
            summary_lines.append(f"💼 Sales: {', '.join(sales_result.get('roles_used', []))}")
    except Exception as e:
        logger.error("Sales dept phase error: %s", e)

    # Creative Department
    try:
        creative = _load_dept("creative") if _api_available else None
        if creative:
            creative_focus = next(iter(decisions), {}).get("focus", "content storytelling brand")
            creative_result = creative.execute_task(
                creative_focus,
                {"insights": insights, "metrics": all_metrics},
            )
            logger.info("[Phase6] Creative: roles_used=%s", creative_result.get("roles_used", []))
            results["phases"]["creative"] = {
                "roles_used": creative_result.get("roles_used", []),
                "timestamp": creative_result.get("timestamp"),
            }
            summary_lines.append(f"🎨 Creative: {', '.join(creative_result.get('roles_used', []))}")
    except Exception as e:
        logger.error("Creative dept phase error: %s", e)

    # Customer Success Department
    try:
        cs = _load_dept("customer_success") if _api_available else None
        if cs:
            cs_focus = next(iter(decisions), {}).get("focus", "retention onboarding upsell")
            cs_result = cs.execute_task(
                cs_focus,
                {"insights": insights, "metrics": all_metrics},
            )
            logger.info("[Phase6] CustomerSuccess: roles_used=%s", cs_result.get("roles_used", []))
            results["phases"]["customer_success"] = {
                "roles_used": cs_result.get("roles_used", []),
                "timestamp": cs_result.get("timestamp"),
            }
            summary_lines.append(f"🤝 CustomerSuccess: {', '.join(cs_result.get('roles_used', []))}")
    except Exception as e:
        logger.error("Customer Success dept phase error: %s", e)

    results["new_actions"] = total_new_actions
    results["phases"]["departments"] = {
        "new_mvps": new_mvps,
        "new_actions": total_new_actions,
        "new_experiments": new_experiments,
    }
    summary_lines.append(f"💡 Новых action items: {total_new_actions}")

    # ════════════════════════════════════════════════════════════════
    # PHASE 6b: Sales + Creative + Customer Success (simple/no-API)
    # ════════════════════════════════════════════════════════════════
    logger.info("\n🏭 PHASE 6b: Sales + Creative + CustomerSuccess (heuristic)")
    try:
        from factory.agents.sales_department import SalesDepartment as _SalesDeptSimple
        from factory.agents.creative_department import CreativeDepartment as _CreativeDeptSimple
        from factory.agents.customer_success_department import CustomerSuccessDepartment as _CSDeptSimple

        _sales_simple = _SalesDeptSimple()
        _creative_simple = _CreativeDeptSimple()
        _cs_simple = _CSDeptSimple()

        # Run a quick analysis cycle (no external calls)
        _guidelines = _creative_simple.get_brand_voice_guidelines()
        _log_phase = lambda label, msg: logger.info("[Phase6b] %s: %s", label, msg)
        _log_phase('Sales+Creative+CS', f"Brand voice: {_guidelines.get('tone', 'N/A')}")

        # Generate social caption for a recent event
        _caption_result = _creative_simple.generate_social_caption("fashion", "Москва")
        _log_phase('CreativeDept', f"Social caption generated ({len(_caption_result)} chars)")

        # Generate promo text for a current promotion
        _promo_result = _creative_simple.generate_promo_text(discount=15, validity_days=7)
        _log_phase('CreativeDept', f"Promo text generated ({len(_promo_result)} chars)")

        results["phases"]["sales_creative_cs_simple"] = {
            "brand_voice_tone": _guidelines.get("tone", "N/A"),
            "caption": _caption_result,
            "caption_len": len(_caption_result),
            "promo": _promo_result,
            "promo_len": len(_promo_result),
            "status": "ok",
        }
        summary_lines.append(
            f"🏭 Sales+Creative+CS heuristic: brand_tone={_guidelines.get('tone', 'N/A')[:40]}, "
            f"caption={len(_caption_result)}chars, promo={len(_promo_result)}chars"
        )
    except Exception as _e6b:
        logger.error("Phase 6b error: %s", _e6b)

    # ════════════════════════════════════════════════════════════════
    # PHASE 7 — FINANCE + RESEARCH DEPARTMENTS
    # ════════════════════════════════════════════════════════════════
    logger.info("\n💰 FINANCE + RESEARCH DEPTS")
    dept_context = {"insights": insights, "metrics": all_metrics}

    # Finance Department
    try:
        finance = _load_dept("finance") if _api_available else None
        if finance:
            finance_focus = "прогноз выручки бюджет расходы оптимизация"
            finance_result = finance.execute_task(finance_focus, dept_context)
            logger.info("[Phase7] Finance: roles_used=%s", finance_result.get("roles_used", []))
            results["phases"]["finance"] = {
                "roles_used": finance_result.get("roles_used", []),
                "result": finance_result.get("result", {}),
                "timestamp": finance_result.get("timestamp"),
            }
            summary_lines.append(f"💰 Finance: {', '.join(finance_result.get('roles_used', []))}")
    except Exception as e:
        logger.error("Finance dept phase error: %s", e)

    # Research Department
    try:
        research = _load_dept("research") if _api_available else None
        if research:
            research_focus = "рынок конкуренты тренды инсайты рекомендации"
            research_result = research.execute_task(research_focus, dept_context)
            logger.info("[Phase7] Research: roles_used=%s", research_result.get("roles_used", []))
            results["phases"]["research"] = {
                "roles_used": research_result.get("roles_used", []),
                "result": research_result.get("result", {}),
                "timestamp": research_result.get("timestamp"),
            }
            summary_lines.append(f"🔬 Research: {', '.join(research_result.get('roles_used', []))}")
    except Exception as e:
        logger.error("Research dept phase error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 12 — SALES DEPARTMENT (все 4 агента индивидуально)
    # ════════════════════════════════════════════════════════════════
    logger.info("\n💼 PHASE 12: SALES DEPARTMENT (4 agents)")
    sales_agents_results = {}
    if not _api_available:
        logger.info("[Phase12] Skipped — no real API key")
    else:
        try:
            from factory.agents.sales_dept import LeadQualifier, ProposalWriter, FollowUpSpecialist, PricingNegotiator

            sales_agent_context = {"insights": insights, "metrics": all_metrics}

            try:
                lead_qualifier = LeadQualifier()
                lq_result = lead_qualifier.run(sales_agent_context)
                sales_agents_results["lead_qualifier"] = lq_result
                logger.info("[Phase12] LeadQualifier: insights=%d", len(lq_result.get("insights", [])))
            except Exception as e:
                logger.error("[Phase12] LeadQualifier error: %s", e)
                sales_agents_results["lead_qualifier"] = {}

            try:
                proposal_writer = ProposalWriter()
                pw_result = proposal_writer.run(sales_agent_context)
                sales_agents_results["proposal_writer"] = pw_result
                logger.info("[Phase12] ProposalWriter: insights=%d", len(pw_result.get("insights", [])))
            except Exception as e:
                logger.error("[Phase12] ProposalWriter error: %s", e)
                sales_agents_results["proposal_writer"] = {}

            try:
                followup_specialist = FollowUpSpecialist()
                fu_result = followup_specialist.run(sales_agent_context)
                sales_agents_results["followup_specialist"] = fu_result
                logger.info("[Phase12] FollowUpSpecialist: insights=%d", len(fu_result.get("insights", [])))
            except Exception as e:
                logger.error("[Phase12] FollowUpSpecialist error: %s", e)
                sales_agents_results["followup_specialist"] = {}

            try:
                pricing_negotiator = PricingNegotiator()
                pn_result = pricing_negotiator.run(sales_agent_context)
                sales_agents_results["pricing_negotiator"] = pn_result
                logger.info("[Phase12] PricingNegotiator: insights=%d", len(pn_result.get("insights", [])))
            except Exception as e:
                logger.error("[Phase12] PricingNegotiator error: %s", e)
                sales_agents_results["pricing_negotiator"] = {}

            results["phases"]["sales_agents"] = {
                "agents": list(sales_agents_results.keys()),
                "results": sales_agents_results,
            }
            active_agents = [k for k, v in sales_agents_results.items() if v]
            summary_lines.append(f"💼 Sales Agents (Phase 12): {', '.join(active_agents)}")
        except Exception as e:
            logger.error("Phase 12 Sales agents error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 13 — CUSTOMER SUCCESS DEPARTMENT (все 4 агента индивидуально)
    # ════════════════════════════════════════════════════════════════
    logger.info("\n🤝 PHASE 13: CUSTOMER SUCCESS DEPARTMENT (4 agents)")
    cx_agents_results = {}
    if not _api_available:
        logger.info("[Phase13] Skipped — no real API key")
    else:
        try:
            from factory.agents.customer_success_dept import (
                OnboardingSpecialist, RetentionAnalyst, FeedbackCollector, UpsellAdvisor
            )

            cx_agent_context = {"insights": insights, "metrics": all_metrics}

            try:
                onboarding = OnboardingSpecialist()
                ob_result = onboarding.run(cx_agent_context)
                cx_agents_results["onboarding_specialist"] = ob_result
                logger.info("[Phase13] OnboardingSpecialist: insights=%d", len(ob_result.get("insights", [])))
            except Exception as e:
                logger.error("[Phase13] OnboardingSpecialist error: %s", e)
                cx_agents_results["onboarding_specialist"] = {}

            try:
                retention = RetentionAnalyst()
                ra_result = retention.run(cx_agent_context)
                cx_agents_results["retention_analyst"] = ra_result
                logger.info("[Phase13] RetentionAnalyst: insights=%d", len(ra_result.get("insights", [])))
            except Exception as e:
                logger.error("[Phase13] RetentionAnalyst error: %s", e)
                cx_agents_results["retention_analyst"] = {}

            try:
                feedback = FeedbackCollector()
                fb_result = feedback.run(cx_agent_context)
                cx_agents_results["feedback_collector"] = fb_result
                logger.info("[Phase13] FeedbackCollector: insights=%d", len(fb_result.get("insights", [])))
            except Exception as e:
                logger.error("[Phase13] FeedbackCollector error: %s", e)
                cx_agents_results["feedback_collector"] = {}

            try:
                upsell = UpsellAdvisor()
                ua_result = upsell.run(cx_agent_context)
                cx_agents_results["upsell_advisor"] = ua_result
                logger.info("[Phase13] UpsellAdvisor: insights=%d", len(ua_result.get("insights", [])))
            except Exception as e:
                logger.error("[Phase13] UpsellAdvisor error: %s", e)
                cx_agents_results["upsell_advisor"] = {}

            results["phases"]["cx_agents"] = {
                "agents": list(cx_agents_results.keys()),
                "results": cx_agents_results,
            }
            active_cx = [k for k, v in cx_agents_results.items() if v]
            summary_lines.append(f"🤝 CX Agents (Phase 13): {', '.join(active_cx)}")
        except Exception as e:
            logger.error("Phase 13 Customer Success agents error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 14 — CONTENT GENERATION: Telegram posts & model bios
    # ════════════════════════════════════════════════════════════════
    logger.info("\n✍️ PHASE 14: CONTENT GENERATION")
    try:
        from factory.agents.content_generator import ContentGenerator
        content_gen = ContentGenerator()
        content_result = content_gen.run()

        posts_count = len(content_result.get("generated_posts", []))
        if posts_count > 0:
            summary_lines.append(f"✍️ Контент: {posts_count} поста сгенерировано")
            # Save generated posts to factory DB as growth actions
            for post in content_result.get("generated_posts", []):
                if post.get("text"):
                    db.insert("growth_actions", {
                        "product_id": nevesty_id,
                        "action_type": "Telegram пост: " + post.get("post_type", "general"),
                        "channel": "telegram",
                        "content": post.get("text", "")[:500],
                        "status": "pending",
                        "priority": 5,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    })
        results["phases"]["content_generation"] = {
            "posts_generated": posts_count,
            "weekly_plan_days": len(content_result.get("weekly_plan", [])),
        }
        logger.info("[Phase14] Content: posts=%d, plan_days=%d", posts_count, len(content_result.get("weekly_plan", [])))
    except Exception as e:
        logger.error("Phase 14 content generation error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 15 — EXPERIMENT TRACKING: evaluate past experiments
    # ════════════════════════════════════════════════════════════════
    logger.info("\n🔬 PHASE 15: EXPERIMENT TRACKING")
    try:
        from factory.agents.experiment_tracker import ExperimentTracker
        tracker = ExperimentTracker()
        exp_results = tracker.run()
        results["phases"]["experiment_tracking"] = exp_results
        if exp_results.get("evaluated", 0) > 0:
            logger.info(
                "[Phase15] Evaluated %d experiments: %d success, %d fail",
                exp_results["evaluated"], exp_results["success"], exp_results["fail"],
            )
            summary_lines.append(
                f"🔬 Experiments: evaluated={exp_results['evaluated']}, "
                f"success={exp_results['success']}, fail={exp_results['fail']}"
            )

        # Check running experiments and update with latest Nevesty metrics
        try:
            active_exps = tracker.get_active_experiments()
            metric_updates = []
            if active_exps and nevesty_kpis:
                orders_count = float(nevesty_kpis.get("orders_this_month", 0))
                for exp in active_exps[:3]:  # check top 3 running experiments
                    if orders_count > 0:
                        update_res = tracker.record_metric_result(
                            exp["id"], "orders_month", orders_count
                        )
                        metric_updates.append(update_res)
                        # Notify admins when an experiment concludes
                        if update_res.get('status') in ('success', 'failed'):
                            status_emoji = '✅' if update_res['status'] == 'success' else '❌'
                            tg_text = (
                                f"{status_emoji} <b>Эксперимент завершён</b>\n"
                                f"Статус: {update_res['status']}\n"
                                f"До: {update_res.get('metric_before', '?')} | "
                                f"После: {update_res.get('metric_after', '?')}"
                            )
                            try:
                                _send_telegram_to_admins(tg_text)
                                logger.info(
                                    "[Phase15] Experiment %s concluded (%s), Telegram sent",
                                    exp.get('id'), update_res['status'],
                                )
                            except Exception as _tg_e:
                                logger.warning("[Phase15] Telegram notify for experiment failed: %s", _tg_e)
            logger.info(
                "[Phase15] Active experiments: %d checked, %d updated with real metrics",
                len(active_exps), len(metric_updates),
            )
            results["phases"]["experiment_tracking"]["active_checked"] = len(active_exps)
            results["phases"]["experiment_tracking"]["metric_updates"] = metric_updates
        except Exception as _me:
            logger.warning("[Phase15] Metric update for experiments skipped: %s", _me)
    except Exception as e:
        results["phases"]["experiment_tracking"] = {"error": str(e)}
        logger.error("Phase 15 experiment tracking error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 22 — SALES DEPT v2: новые агенты с именами (Алиса, Михаил, Екатерина, Дмитрий)
    # ════════════════════════════════════════════════════════════════
    logger.info("\n💼 PHASE 22: SALES DEPT v2 (Алиса/Михаил/Екатерина/Дмитрий)")
    sales_v2_results = {}
    try:
        from factory.agents.sales import (
            LeadQualifierAgent, ProposalWriterAgent,
            FollowUpSpecialistAgent, PricingNegotiatorAgent,
        )
        sales_v2_context = {"insights": insights, "metrics": all_metrics}

        for AgentClass in [LeadQualifierAgent, ProposalWriterAgent, FollowUpSpecialistAgent, PricingNegotiatorAgent]:
            try:
                _agent = AgentClass()
                _result = _agent.run(sales_v2_context)
                sales_v2_results[_agent.role] = _result
                logger.info("[Phase22] %s (%s): ok", _agent.role, _agent.name)
            except Exception as _ae:
                logger.error("[Phase22] %s error: %s", AgentClass.__name__, _ae)
                sales_v2_results[AgentClass.role] = {}

        results["phases"]["sales_v2"] = {
            "agents": list(sales_v2_results.keys()),
            "results": sales_v2_results,
        }
        active_v2 = [k for k, v in sales_v2_results.items() if v]
        summary_lines.append(f"💼 Sales v2 (Phase 22): {', '.join(active_v2)}")
    except Exception as e:
        logger.error("Phase 22 Sales v2 error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 23 — CREATIVE DEPT v2: новые агенты с именами (Анастасия, Артём, Мария, Ольга)
    # ════════════════════════════════════════════════════════════════
    logger.info("\n🎨 PHASE 23: CREATIVE DEPT v2 (Анастасия/Артём/Мария/Ольга)")
    creative_v2_results = {}
    try:
        from factory.agents.creative import (
            CopywriterAgent, VisualConceptorAgent,
            BrandVoiceKeeperAgent, StorytellingAgent as StorytellingAgentV2,
        )
        creative_v2_context = {"insights": insights, "metrics": all_metrics}

        for AgentClass in [CopywriterAgent, VisualConceptorAgent, BrandVoiceKeeperAgent, StorytellingAgentV2]:
            try:
                _agent = AgentClass()
                _result = _agent.run(creative_v2_context)
                creative_v2_results[_agent.role] = _result
                logger.info("[Phase23] %s (%s): ok", _agent.role, _agent.name)
            except Exception as _ae:
                logger.error("[Phase23] %s error: %s", AgentClass.__name__, _ae)
                creative_v2_results[AgentClass.role] = {}

        results["phases"]["creative_v2"] = {
            "agents": list(creative_v2_results.keys()),
            "results": creative_v2_results,
        }
        active_cv2 = [k for k, v in creative_v2_results.items() if v]
        summary_lines.append(f"🎨 Creative v2 (Phase 23): {', '.join(active_cv2)}")
    except Exception as e:
        logger.error("Phase 23 Creative v2 error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 8 — CEO SYNTHESIS: синтез всех департаментов
    # ════════════════════════════════════════════════════════════════
    logger.info("\n🏆 CEO SYNTHESIS")
    try:
        # Собираем краткие итоги всех департаментов (включая Phase 12 и 13)
        all_department_results = {}
        for dept_name, dept_data in results["phases"].items():
            if dept_name not in ("analytics", "ceo", "departments", "ideas"):
                all_department_results[dept_name] = dept_data

        # Явно включаем результаты Phase 12/13 в контекст CEO
        if sales_agents_results:
            all_department_results["sales_agents_phase12"] = {
                "agents": list(sales_agents_results.keys()),
                "summary": {
                    agent: {
                        "insights": data.get("insights", [])[:2],
                        "priority": data.get("priority"),
                    }
                    for agent, data in sales_agents_results.items() if data
                },
            }
        if cx_agents_results:
            all_department_results["cx_agents_phase13"] = {
                "agents": list(cx_agents_results.keys()),
                "summary": {
                    agent: {
                        "insights": data.get("insights", [])[:2],
                        "priority": data.get("priority"),
                    }
                    for agent, data in cx_agents_results.items() if data
                },
            }

        # Извлекаем previous_growth_actions из последнего цикла для CEO tracking
        prev_cycle_data = _load_last_cycle_from_history()
        previous_growth_actions = []
        if prev_cycle_data:
            prev_ceo = prev_cycle_data.get("phases", {}).get("ceo_synthesis", {})
            previous_growth_actions = prev_ceo.get("growth_actions", [])

        # Load last 3 CEO decisions from DB for decision continuity tracking
        prev_ceo_decisions = db.get_recent_ceo_decisions(limit=3)
        prev_decisions_str = ""
        if prev_ceo_decisions:
            lines = ["ПРЕДЫДУЩИЕ РЕШЕНИЯ CEO (последние 3 цикла):"]
            for dec in prev_ceo_decisions:
                ts = dec.get("created_at", "")[:16]
                hs = dec.get("health_score", "?")
                focus = dec.get("weekly_focus", "—")
                dept = dec.get("department_focus", "—")
                text_preview = (dec.get("decision_text") or "")[:200]
                lines.append(
                    f"  [{ts}] health={hs}, focus='{focus}', dept_focus='{dept}'\n"
                    f"    Меморандум: {text_preview}..."
                )
            lines.append(
                "\nCEO: проверь — выполнены ли предыдущие решения, изменился ли курс и почему."
            )
            prev_decisions_str = "\n".join(lines)

        class CEOSynthesisAgent(FactoryAgent):
            department = "ceo"
            role = "ceo_synthesis"
            name = "ceo_synthesis"
            system_prompt = (
                "Ты — CEO агентства моделей Nevesty Models. "
                "Отвечаешь за стратегическое направление компании. "
                "Принимаешь решения на основе данных всех 13 фаз: аналитики, маркетинга, "
                "продаж (Phase 12: LeadQualifier, ProposalWriter, FollowUpSpecialist, PricingNegotiator), "
                "Customer Success (Phase 13: OnboardingSpecialist, RetentionAnalyst, FeedbackCollector, UpsellAdvisor), "
                "операций, технологий, финансов, исследований и HR. "
                "По итогам каждого цикла ты ОБЯЗАН: "
                "1) Оценить насколько были выполнены growth_actions прошлого цикла (action_completion_score 0-10). "
                "2) Выбрать один департамент для фокуса следующего цикла (department_focus). "
                "3) Предложить один A/B тест с гипотезой и метрикой успеха (experiment_proposal). "
                "4) Указать приоритетный KPI и эксперимент следующего цикла. "
                "Мыслишь чётко, расставляешь приоритеты, даёшь конкретные указания команде. "
                "Всё на русском языке."
            )

        ceo_agent = CEOSynthesisAgent()

        # Load trend data and previous cycle for CEO context
        prev_cycles = _load_metrics_trend('phases.ceo_synthesis.health_score', last_n=3)
        trend_info = f"Тренд health_score за последние циклы: {[c.get('value') for c in prev_cycles]}" if prev_cycles else "Первый цикл (история отсутствует)"

        prev_actions_report = _check_previous_actions_completion(prev_cycle_data)

        # Формируем строку о previous_growth_actions для CEO tracking
        prev_growth_str = ""
        if previous_growth_actions:
            prev_growth_str = (
                f"\n\nPREVIOUS GROWTH ACTIONS (из прошлого цикла, {len(previous_growth_actions)} шт.):\n"
                + json.dumps(previous_growth_actions[:5], ensure_ascii=False, indent=2, default=str)
                + "\nОцени выполнение каждого (action_completion_score: 0=ничего не выполнено, 10=всё выполнено)."
            )

        context_str = json.dumps(all_department_results, ensure_ascii=False, indent=2, default=str)

        memo_prompt = f"""Ты — CEO модельного агентства. Напиши ЕЖЕНЕДЕЛЬНЫЙ МЕМОРАНДУМ на русском языке.

Данные этого цикла (включая Phase 12: Sales agents, Phase 13: CX agents):
{context_str}

{trend_info}

Анализ предыдущего цикла:
{prev_actions_report}

Структура меморандума (строго следуй):
## 📊 Ключевые метрики
- 3 главных показателя этой недели

## 🎯 Главное решение недели
- Одно конкретное действие которое нужно выполнить

## 🧪 Эксперимент следующего цикла
- Один конкретный эксперимент с гипотезой и метрикой успеха

## 📈 Приоритетный KPI
- Один KPI с текущим значением и целевым

## 🔍 Причина успеха/неудачи прошлого цикла
- Одна конкретная причина с объяснением

## ⚠️ Риски
- Главный риск (1 пункт)

## 🚀 Возможности
- Главная возможность (1 пункт)

## 📋 Задачи команде
- 3 конкретных задачи для операционной команды

Пиши кратко, по делу, как настоящий CEO. Максимум 400 слов."""

        ceo_synthesis_prompt = (
            "Ты — CEO агентства моделей Nevesty Models. Получи отчёты всех 13 фаз и сделай выводы.\n\n"
            "ИНСТРУКЦИЯ ДЛЯ CEO MEMO:\n"
            + memo_prompt
            + "\n\nОТЧЁТЫ ВСЕХ ДЕПАРТАМЕНТОВ (Phase 1-13):\n"
            + context_str
            + "\n\nАНАЛИЗ ПРЕДЫДУЩЕГО ЦИКЛА:\n"
            + prev_actions_report
            + prev_growth_str
            + ("\n\n" + prev_decisions_str if prev_decisions_str else "")
            + ("\n\nОТЧЁТ ПО ВЫПОЛНЕНИЮ factory_tasks (DecisionTracker):\n" + decision_accountability_report if decision_accountability_report else "")
            + "\n\nВерни JSON:\n"
            '{\n'
            '  "health_score": 75,\n'
            '  "weekly_focus": "Улучшить конверсию из просмотра каталога в заявку",\n'
            '  "action_completion_score": 7,\n'
            '  "department_focus": "sales",\n'
            '  "experiment_proposal": {"hypothesis": "Если добавить кнопку быстрого заказа в каталоге, конверсия вырастет на 20%", "success_metric": "конверсия каталог→заявка > 5%", "department": "product"},\n'
            '  "next_cycle_experiment": {"hypothesis": "...", "metric": "...", "department": "..."},\n'
            '  "priority_kpi": {"name": "конверсия в заявку", "current": "2%", "target": "4%"},\n'
            '  "prev_cycle_lesson": "Одна причина успеха или неудачи прошлого цикла",\n'
            '  "growth_actions": [\n'
            '    {"priority": 1, "action": "...", "department": "marketing", "expected_impact": "высокий"},\n'
            '    {"priority": 2, "action": "...", "department": "sales", "expected_impact": "средний"}\n'
            '  ],\n'
            '  "ceo_memo": "Еженедельный меморандум CEO со структурой из инструкции выше...",\n'
            '  "risks": ["Риск 1", "Риск 2"],\n'
            '  "opportunities": ["Возможность 1", "Возможность 2"]\n'
            '}'
        )

        ceo_synthesis = ceo_agent.think_json(ceo_synthesis_prompt, max_tokens=2500)

        if ceo_synthesis and isinstance(ceo_synthesis, dict):
            # Обновляем health_score если CEO дал оценку
            if "health_score" in ceo_synthesis:
                results["health_score"] = ceo_synthesis["health_score"]

            results["phases"]["ceo_synthesis"] = ceo_synthesis

            # Сохраняем CEO Weekly Memo в БД как growth_action
            memo_text = ceo_synthesis.get("ceo_memo", "")
            weekly_focus = ceo_synthesis.get("weekly_focus", "")
            action_completion_score = ceo_synthesis.get("action_completion_score")
            department_focus = ceo_synthesis.get("department_focus", "")
            experiment_proposal = ceo_synthesis.get("experiment_proposal", {})

            if memo_text or weekly_focus:
                db.insert("growth_actions", {
                    "product_id": nevesty_id,
                    "action_type": "ceo_memo",
                    "channel": "internal",
                    "content": json.dumps({
                        "type": "CEO Weekly Memo",
                        "cycle_id": cycle_id,
                        "health_score": ceo_synthesis.get("health_score"),
                        "weekly_focus": weekly_focus,
                        "memo": memo_text,
                        "action_completion_score": action_completion_score,
                        "department_focus": department_focus,
                        "experiment_proposal": experiment_proposal,
                        "next_cycle_experiment": ceo_synthesis.get("next_cycle_experiment", {}),
                        "priority_kpi": ceo_synthesis.get("priority_kpi", {}),
                        "prev_cycle_lesson": ceo_synthesis.get("prev_cycle_lesson", ""),
                        "growth_actions": ceo_synthesis.get("growth_actions", []),
                        "risks": ceo_synthesis.get("risks", []),
                        "opportunities": ceo_synthesis.get("opportunities", []),
                    }, ensure_ascii=False),
                    "status": "done",
                    "priority": 10,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })

            summary_lines.append(f"🏆 CEO Health Score: {ceo_synthesis.get('health_score', '—')}%")
            summary_lines.append(f"🎯 CEO Фокус: {weekly_focus[:80] if weekly_focus else '—'}")
            growth_actions_list = ceo_synthesis.get("growth_actions", [])
            actions_count = len(growth_actions_list)
            summary_lines.append(f"📋 CEO Growth Actions: {actions_count}")

            # New CEO intelligence fields
            next_exp = ceo_synthesis.get("next_cycle_experiment", {})
            priority_kpi = ceo_synthesis.get("priority_kpi", {})
            prev_lesson = ceo_synthesis.get("prev_cycle_lesson", "")
            if action_completion_score is not None:
                summary_lines.append(f"✅ Выполнение прошлых задач: {action_completion_score}/10")
            if department_focus:
                summary_lines.append(f"🏢 Фокус-департамент: {department_focus}")
            if experiment_proposal:
                summary_lines.append(f"🧪 A/B тест: {experiment_proposal.get('hypothesis', '—')[:60]}")
            if next_exp:
                summary_lines.append(f"🔬 Эксперимент цикла: {next_exp.get('hypothesis', '—')[:60]}")
            if priority_kpi:
                summary_lines.append(f"📈 KPI: {priority_kpi.get('name', '—')} → {priority_kpi.get('target', '—')}")
            if prev_lesson:
                summary_lines.append(f"🔍 Урок: {prev_lesson[:80]}")

            logger.info(
                "[CEOSynthesis] health=%s, focus=%s, actions=%s, completion=%s, dept_focus=%s, experiment=%s",
                ceo_synthesis.get("health_score"),
                weekly_focus[:60] if weekly_focus else "—",
                actions_count,
                action_completion_score,
                department_focus,
                experiment_proposal.get("hypothesis", "—")[:50] if experiment_proposal else "—",
            )

            # Sync growth actions to bot DB and send CEO memo to Telegram
            final_health = ceo_synthesis.get("health_score", results["health_score"])
            bot_db = "/home/user/Pablo/nevesty-models/data.db"
            _sync_growth_actions_to_bot_db(growth_actions_list, bot_db)
            _save_ceo_memo_to_settings(
                memo_text, final_health, department_focus,
                experiment_proposal, bot_db
            )
            _send_ceo_memo_to_telegram(memo_text, final_health, growth_actions_list)

            # Save CEO decision to DB for future decision-tracking context
            try:
                active_depts = [
                    k for k in results.get("phases", {})
                    if k not in ("analytics", "ceo", "ceo_synthesis", "departments", "ideas")
                ]
                db.save_ceo_decision(
                    cycle_id=cycle_id,
                    decision_text=memo_text,
                    health_score=int(final_health) if final_health is not None else 50,
                    departments_active=active_depts,
                    weekly_focus=weekly_focus,
                    department_focus=department_focus,
                    experiment_proposal=experiment_proposal if isinstance(experiment_proposal, dict) else {},
                )
                logger.info("[CEOSynthesis] Decision saved to ceo_decisions table")
            except Exception as _dbe:
                logger.warning("[CEOSynthesis] Failed to save CEO decision: %s", _dbe)
    except Exception as e:
        logger.error("CEO Synthesis phase error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 5.2 — CEO WEEKLY REPORT + EXPERIMENT PROPOSALS (БЛОК 5.3, 5.4)
    # ════════════════════════════════════════════════════════════════
    logger.info("\n📊 PHASE 5.2: CEO WEEKLY REPORT + EXPERIMENT PROPOSALS")

    # CEO weekly report (once per week — Mondays)
    try:
        if datetime.now(timezone.utc).weekday() == 0:  # Monday
            weekly = ceo.generate_weekly_report(nevesty_kpis_raw)
            if weekly.get('status') != 'already_generated':
                db.insert("growth_actions", {
                    "product_id": nevesty_id,
                    "action_type": "weekly_report",
                    "channel": "internal",
                    "content": f"Weekly: {weekly.get('headline', '')}",
                    "status": "pending",
                    "priority": 8,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
                results["phases"]["ceo_weekly_report"] = weekly
                summary_lines.append(f"📊 CEO Weekly: {weekly.get('headline', '')[:60]}")
                logger.info("[Phase5.2] CEO weekly report: trend=%s", weekly.get("key_metric_trend"))

                # Send CEO weekly report to Telegram admins
                try:
                    headline = weekly.get('headline', 'CEO Weekly Report')
                    focus = weekly.get('focus_next_week', '')
                    concerns = ', '.join(weekly.get('concerns', []))

                    tg_text = (
                        f"<b>📊 CEO Weekly Report — Week {weekly.get('week', '?')}</b>\n\n"
                        f"<b>{headline}</b>\n\n"
                        f"🎯 <b>Фокус следующей недели:</b> {focus}\n"
                        f"⚠️ <b>Риски:</b> {concerns or 'нет'}\n\n"
                        f"<i>Сгенерировано AI Factory</i>"
                    )
                    _send_telegram_to_admins(tg_text)
                    logger.info("[Phase5.2] CEO weekly report sent to Telegram admins")
                except Exception as _tg_e:
                    logger.warning("[Phase5.2] Failed to send CEO weekly report to Telegram: %s", _tg_e)
    except Exception as e:
        logger.error("Phase 5.2 CEO weekly report error: %s", e)

    # CEO experiment proposals (every 3rd cycle)
    try:
        cycle_count_row = db.fetch_one("SELECT COUNT(*) as c FROM cycles")
        if cycle_count_row and cycle_count_row.get('c', 0) % 3 == 0:
            proposed = ceo.propose_experiments(nevesty_kpis_raw)
            saved_count = 0
            for exp in proposed[:2]:
                try:
                    db.execute(
                        "INSERT OR IGNORE INTO experiments (hypothesis, metric, status, created_at) "
                        "VALUES (?, ?, 'proposed', datetime('now'))",
                        (exp.get('hypothesis', ''), exp.get('metric', 'conversion_rate')),
                    )
                    saved_count += 1
                except Exception:
                    pass
            results["phases"]["ceo_experiment_proposals"] = {"proposed": len(proposed), "saved": saved_count}
            summary_lines.append(f"🧪 CEO эксперименты: {len(proposed)} предложено")
            logger.info("[Phase5.2] CEO proposed %d experiments, saved %d", len(proposed), saved_count)
    except Exception as e:
        logger.error("Phase 5.2 CEO experiment proposals error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 5.3 — EXPERIMENT AUTO-APPLY: detect & promote successful experiments
    # ════════════════════════════════════════════════════════════════
    logger.info("\n🚀 PHASE 5.3: EXPERIMENT AUTO-APPLY")
    try:
        _auto_applied = _auto_apply_successful_experiments(nevesty_id)
        if _auto_applied:
            results["phases"]["experiment_auto_apply"] = {"applied": _auto_applied}
            for _exp_name in _auto_applied:
                summary_lines.append(f"✅ Эксперимент [{_exp_name}] применён автоматически")
            logger.info("[Phase5.3] Auto-applied experiments: %s", _auto_applied)
    except Exception as e:
        logger.error("Phase 5.3 experiment auto-apply error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 5.4 — WEEKLY FACTORY SUMMARY (every 7 days)
    # ════════════════════════════════════════════════════════════════
    logger.info("\n📅 PHASE 5.4: WEEKLY FACTORY SUMMARY")
    try:
        _weekly_summary_result = _maybe_generate_weekly_summary(cycle_id, results)
        if _weekly_summary_result:
            results["phases"]["weekly_factory_summary"] = _weekly_summary_result
            summary_lines.append(f"📅 Weekly Factory Summary сгенерирован ({_weekly_summary_result.get('period_key', '')})")
            logger.info("[Phase5.4] Weekly summary generated: %s", _weekly_summary_result.get("period_key"))
    except Exception as e:
        logger.error("Phase 5.4 weekly factory summary error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 4 — IDEAS (если мало)
    # ════════════════════════════════════════════════════════════════
    logger.info("\n💡 IDEAS")
    try:
        ideas_count = db.fetch_one("SELECT COUNT(*) as n FROM ideas WHERE status='new'")["n"]
        if ideas_count < 5:
            from factory.agents.product_factory import ProductFactory
            pf = ProductFactory()
            new_ideas = pf.generate_ideas(count=3, context={"insights": insights})
            results["phases"]["ideas"] = {"new": len(new_ideas)}
            if new_ideas:
                summary_lines.append(f"💡 Идей сгенерировано: {len(new_ideas)}")
    except Exception as e:
        logger.error("Ideas phase error: %s", e)

    # Generate A/B hypotheses via ExperimentDesigner and sync to bot DB
    try:
        from factory.agents.experiment_system import ExperimentDesigner
        designer = ExperimentDesigner()
        nevesty_metrics = all_metrics.get("nevesty_models", {})
        exp_context = {
            "total_orders": nevesty_metrics.get("total_orders", 0),
            "conversion_rate": nevesty_metrics.get("conversion_rate", 0),
            "active_clients": nevesty_metrics.get("active_clients", 0),
            "health_score": results.get("health_score", 50),
        }
        ab_hypotheses = designer.generate_hypotheses(exp_context)
        if ab_hypotheses:
            bot_db = "/home/user/Pablo/nevesty-models/data.db"
            _sync_experiments_to_db(ab_hypotheses, bot_db)
            results["phases"]["ab_experiments"] = {"generated": len(ab_hypotheses)}
            summary_lines.append(f"🧪 A/B гипотез: {len(ab_hypotheses)}")
            logger.info("[IDEAS] Generated %d A/B hypotheses", len(ab_hypotheses))
    except Exception as e:
        logger.error("A/B experiment generation error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 9 — IDEAS DEPARTMENT: creative brainstorming & gamification
    # ════════════════════════════════════════════════════════════════
    logger.info("\n🧠 IDEAS DEPARTMENT")
    try:
        from agents.ideas_dept import FeatureInventor, TrendAnalystIdeas, UserJourneyMapper, GamificationDesigner

        inventor = FeatureInventor()
        trend_analyst = TrendAnalystIdeas()
        journey_mapper = UserJourneyMapper()
        gamification = GamificationDesigner()

        ideas_prompt = f"""Контекст платформы:
- Telegram-бот для бронирования моделей агентства
- Сайт с каталогом, формой бронирования, личным кабинетом
- AI Factory для автономной генерации идей
- Текущая статистика: {results.get('analytics', {})}

Придумай 5 новых функций которые:
1. Улучшат конверсию из просмотра в заявку
2. Повысят возврат клиентов
3. Упростят работу администраторов

Отвечай JSON массивом: [{{"feature": "...", "for": "bot|site|both", "effort": "low|medium|high", "impact": "low|medium|high", "description": "..."}}]"""

        new_ideas = inventor.think_json(ideas_prompt, {"insights": insights})
        trends = trend_analyst.think(
            f"Назови 3 актуальных тренда в моделинг-индустрии и как их внедрить. Контекст: {results.get('analytics', {})}",
            {"insights": insights},
        )
        friction = journey_mapper.think(
            "Опиши 3 точки трения в процессе бронирования модели через Telegram-бот. Как их устранить?",
            {"insights": insights},
        )
        gamif = gamification.think_json(
            'Предложи 3 механики лояльности для клиентов модельного агентства. JSON: [{"mechanic": ..., "description": ...}]',
            {"insights": insights},
        )

        ideas_result = {
            "new_features": new_ideas if isinstance(new_ideas, list) else [],
            "industry_trends": trends,
            "friction_points": friction,
            "gamification": gamif if isinstance(gamif, list) else [],
        }
        results["phases"]["ideas_dept"] = ideas_result

        feature_count = len(ideas_result["new_features"])
        if feature_count:
            summary_lines.append(f"🧠 IDEAS: {feature_count} новых идей сгенерировано")
        logger.info("[Phase9] IDEAS dept: features=%s, gamif=%s", feature_count, len(ideas_result["gamification"]))
    except Exception as e:
        logger.error("IDEAS dept phase error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 16 — MONTHLY CEO REPORT (runs on day 1-3 of month if not yet generated)
    # ════════════════════════════════════════════════════════════════
    logger.info("\n📅 PHASE 16: MONTHLY CEO REPORT")
    try:
        import datetime as _dt
        import sqlite3 as _sqlite3

        _today = _dt.date.today()
        _current_month = _today.strftime('%Y-%m')

        # Check if monthly report already exists for this month
        _existing_monthly = db.fetch_one(
            "SELECT id FROM monthly_reports WHERE month = ?", (_current_month,)
        )

        _should_run_monthly = (_today.day == 1) or (_existing_monthly is None and _today.day <= 3)

        if _should_run_monthly and _existing_monthly is None:
            # Open data.db for metric helpers
            _bot_db_path = "/home/user/Pablo/nevesty-models/data.db"
            _data_conn = None
            if os.path.exists(_bot_db_path):
                _data_conn = _sqlite3.connect(_bot_db_path)
                _data_conn.row_factory = _sqlite3.Row

            _monthly_data = {
                'month': _current_month,
                'orders': get_monthly_metrics(_data_conn) if _data_conn else {},
                'top_models': get_top_models(_data_conn) if _data_conn else [],
                'revenue_trend': get_revenue_trend(_data_conn) if _data_conn else [],
            }
            if _data_conn:
                _data_conn.close()

            # Use the CEOSynthesisAgent that was created above (reuse if available)
            class _MonthlyCEOAgent(FactoryAgent):
                department = "ceo"
                role = "ceo_monthly"
                name = "ceo_monthly"
                system_prompt = (
                    "Ты — CEO агентства моделей Nevesty Models. "
                    "Генерируешь ежемесячный стратегический отчёт на основе данных за месяц. "
                    "Анализируй итоги месяца: заявки, конверсию, выручку, топ-моделей. "
                    "Давай стратегические приоритеты на следующий месяц. "
                    "Пиши структурированно, на русском языке."
                )

            _monthly_ceo = _MonthlyCEOAgent()

            _monthly_prompt = (
                f"Генерируй ежемесячный CEO-отчёт за {_current_month}.\n"
                f"Данные: {json.dumps(_monthly_data, ensure_ascii=False, default=str)}\n\n"
                f"Включи:\n"
                f"1) Итоги месяца (заявки, конверсия, выручка)\n"
                f"2) Топ-3 инсайта\n"
                f"3) Стратегические приоритеты на следующий месяц\n"
                f"4) Что сработало хорошо\n"
                f"5) Что нужно улучшить"
            )

            _monthly_report = _monthly_ceo.think(_monthly_prompt, context=_monthly_data)

            db.execute(
                "INSERT INTO monthly_reports (month, report_json) VALUES (?, ?)",
                (_current_month, json.dumps(
                    {'report': _monthly_report, **_monthly_data},
                    ensure_ascii=False, default=str
                ))
            )

            results["phases"]["monthly_report"] = {
                "month": _current_month,
                "generated": True,
                "orders_total": _monthly_data.get("orders", {}).get("total", 0),
                "top_models_count": len(_monthly_data.get("top_models", [])),
            }
            summary_lines.append(f"📅 Monthly CEO Report сгенерирован за {_current_month}")
            logger.info("[Phase16] Monthly CEO report generated for %s", _current_month)
        elif _existing_monthly:
            logger.info("[Phase16] Monthly report for %s already exists — skipping", _current_month)
            results["phases"]["monthly_report"] = {"month": _current_month, "generated": False, "reason": "already_exists"}
        else:
            logger.info("[Phase16] Skipping monthly report (day=%s)", _today.day)
            results["phases"]["monthly_report"] = {"month": _current_month, "generated": False, "reason": "not_due"}
    except Exception as e:
        logger.error("Phase 16 monthly CEO report error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 17 — CUSTOMER SUCCESS ANALYSIS (weekly, Tuesdays)
    # ════════════════════════════════════════════════════════════════
    logger.info("\n🤝 PHASE 17: CUSTOMER SUCCESS ANALYSIS (Tuesdays)")
    try:
        import datetime as _dt17
        _today17 = _dt17.date.today()
        if _today17.weekday() == 1:  # Tuesday
            import sqlite3 as _sqlite3_17
            _bot_db_17 = "/home/user/Pablo/nevesty-models/data.db"
            _data_db_17 = None
            if os.path.exists(_bot_db_17):
                _data_db_17 = _sqlite3_17.connect(_bot_db_17)
                _data_db_17.row_factory = _sqlite3_17.Row

            from factory.agents.customer_success import OnboardingSpecialist as _CS_Onboarding, \
                RetentionAnalyst as _CS_Retention, FeedbackCollector as _CS_Feedback
            cs17_results = []
            for _AgentCls in [_CS_Onboarding, _CS_Retention, _CS_Feedback]:
                try:
                    _agent = _AgentCls()
                    _result = _agent.run(data_db=_data_db_17)
                    cs17_results.append(_result)
                    logger.info("[Phase17] %s completed", _AgentCls.__name__)
                except Exception as _ae:
                    logger.error("[Phase17] %s error: %s", _AgentCls.__name__, _ae)

            if _data_db_17:
                _data_db_17.close()

            results["phases"]["customer_success_weekly"] = {
                "agents_run": len(cs17_results),
                "roles": [r.get("role") for r in cs17_results],
            }
            summary_lines.append(f"🤝 CS Weekly (Phase 17): {len(cs17_results)} агента")
            logger.info("[Phase17] Customer Success analysis: %d agents ran", len(cs17_results))
        else:
            logger.info("[Phase17] Skipping Customer Success weekly analysis (not Tuesday, weekday=%d)", _today17.weekday())
    except Exception as e:
        logger.error("Phase 17 Customer Success weekly error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 18 — FINANCE ANALYSIS (weekly, Thursdays)
    # ════════════════════════════════════════════════════════════════
    logger.info("\n💰 PHASE 18: FINANCE ANALYSIS (Thursdays)")
    try:
        import datetime as _dt18
        _today18 = _dt18.date.today()
        if _today18.weekday() == 3:  # Thursday
            import sqlite3 as _sqlite3_18
            _bot_db_18 = "/home/user/Pablo/nevesty-models/data.db"
            _data_db_18 = None
            if os.path.exists(_bot_db_18):
                _data_db_18 = _sqlite3_18.connect(_bot_db_18)
                _data_db_18.row_factory = _sqlite3_18.Row

            from factory.agents.finance import RevenueForecaster as _Fin_Revenue, \
                PricingStrategist as _Fin_Pricing
            finance18_results = []
            for _AgentCls in [_Fin_Revenue, _Fin_Pricing]:
                try:
                    _agent = _AgentCls()
                    _result = _agent.run(data_db=_data_db_18)
                    finance18_results.append(_result)
                    logger.info("[Phase18] %s completed", _AgentCls.__name__)
                except Exception as _ae:
                    logger.error("[Phase18] %s error: %s", _AgentCls.__name__, _ae)

            if _data_db_18:
                _data_db_18.close()

            results["phases"]["finance_weekly"] = {
                "agents_run": len(finance18_results),
                "roles": [r.get("role") for r in finance18_results],
            }
            summary_lines.append(f"💰 Finance Weekly (Phase 18): {len(finance18_results)} агента")
            logger.info("[Phase18] Finance analysis: %d agents ran", len(finance18_results))
        else:
            logger.info("[Phase18] Skipping Finance weekly analysis (not Thursday, weekday=%d)", _today18.weekday())
    except Exception as e:
        logger.error("Phase 18 Finance weekly error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 19 — RESEARCH ANALYSIS (weekly, Wednesdays)
    # ════════════════════════════════════════════════════════════════
    logger.info("\n🔬 PHASE 19: RESEARCH ANALYSIS (Wednesdays)")
    try:
        import datetime as _dt19
        _today19 = _dt19.date.today()
        if _today19.weekday() == 2:  # Wednesday
            import sqlite3 as _sqlite3_19
            _bot_db_19 = "/home/user/Pablo/nevesty-models/data.db"
            _data_db_19 = None
            if os.path.exists(_bot_db_19):
                _data_db_19 = _sqlite3_19.connect(_bot_db_19)
                _data_db_19.row_factory = _sqlite3_19.Row

            from factory.agents.research import MarketResearcher as _Res_Market, \
                TrendSpotter as _Res_Trend, InsightSynthesizer as _Res_Insight
            research19_results = []
            for _AgentCls in [_Res_Market, _Res_Trend, _Res_Insight]:
                try:
                    _agent = _AgentCls()
                    _result = _agent.run(data_db=_data_db_19)
                    research19_results.append(_result)
                    logger.info("[Phase19] %s completed", _AgentCls.__name__)
                except Exception as _ae:
                    logger.error("[Phase19] %s error: %s", _AgentCls.__name__, _ae)

            if _data_db_19:
                _data_db_19.close()

            results["phases"]["research_weekly"] = {
                "agents_run": len(research19_results),
                "roles": [r.get("role") for r in research19_results],
            }
            summary_lines.append(f"🔬 Research Weekly (Phase 19): {len(research19_results)} агента")
            logger.info("[Phase19] Research analysis: %d agents ran", len(research19_results))
        else:
            logger.info("[Phase19] Skipping Research weekly analysis (not Wednesday, weekday=%d)", _today19.weekday())
    except Exception as e:
        logger.error("Phase 19 Research weekly error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 20: EXPERIMENT SYSTEM (Mondays)
    # ════════════════════════════════════════════════════════════════
    logger.info("\n🧪 PHASE 20: EXPERIMENT SYSTEM (Mondays)")
    try:
        import datetime as _dt20
        _today20 = _dt20.date.today()
        weekday = _today20.weekday()
        if weekday == 0:  # Monday
            logger.info("Phase 20: Experiment System")
            try:
                from factory.agents.experiments import ExperimentProposer, ExperimentTracker, ResultAnalyzer
                for AgentClass in [ExperimentProposer, ExperimentTracker, ResultAnalyzer]:
                    _agent20 = AgentClass()
                    _result20 = _agent20.run()
                    results.setdefault('experiments', []).append({
                        'role': AgentClass.role,
                        'result': (_result20[:200] if _result20 else ''),
                    })
                logger.info("Phase 20 complete: Experiment System")
                summary_lines.append(f"🧪 Experiment System (Phase 20): 3 агента")
            except Exception as _e20_inner:
                logger.error("Phase 20 inner error: %s", _e20_inner)
        else:
            logger.info("[Phase20] Skipping Experiment System (not Monday, weekday=%d)", weekday)
    except Exception as e:
        logger.error("Phase 20 error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 21: CEO WEEKLY SUMMARY (Fridays)
    # ════════════════════════════════════════════════════════════════
    logger.info("\n📊 PHASE 21: CEO WEEKLY SUMMARY (Fridays)")
    try:
        import datetime as _dt21
        import sqlite3 as _sqlite3_21
        _today21 = _dt21.date.today()
        _weekday21 = _today21.weekday()
        if _weekday21 == 4:  # Friday
            logger.info("Phase 21: CEO Weekly Summary")
            try:
                # Get week stats from data.db
                weekly_stats = {}
                try:
                    _bot_db_21 = "/home/user/Pablo/nevesty-models/data.db"
                    if os.path.exists(_bot_db_21):
                        _conn21 = _sqlite3_21.connect(_bot_db_21)
                        _c21 = _conn21.cursor()
                        _c21.execute("""
                            SELECT
                                COUNT(*) as total,
                                SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
                                SUM(CASE WHEN status='new' THEN 1 ELSE 0 END) as new_orders
                            FROM orders WHERE created_at >= datetime('now', '-7 days')
                        """)
                        _r21 = _c21.fetchone()
                        if _r21:
                            weekly_stats = {
                                'total': _r21[0] or 0,
                                'completed': _r21[1] or 0,
                                'new': _r21[2] or 0,
                            }
                        _conn21.close()
                except Exception as _e21_db:
                    logger.warning("[Phase21] Could not read data.db: %s", _e21_db)

                class _WeeklyCEOAgent(FactoryAgent):
                    department = "ceo"
                    role = "ceo_weekly"
                    name = "ceo_weekly"
                    system_prompt = (
                        "Ты — CEO агентства моделей Nevesty Models. "
                        "Генерируешь еженедельный стратегический отчёт. "
                        "Будь краток и actionable. Пиши на русском."
                    )

                _weekly_ceo = _WeeklyCEOAgent()
                _ceo_prompt21 = (
                    f"Weekly report for Nevesty Models modeling agency.\n"
                    f"This week: {weekly_stats.get('total', 0)} total orders, "
                    f"{weekly_stats.get('completed', 0)} completed, "
                    f"{weekly_stats.get('new', 0)} new.\n"
                    f"Provide a brief strategic assessment and 3 priorities for next week. "
                    f"Be concise and actionable."
                )
                _weekly_summary21 = _weekly_ceo.think(_ceo_prompt21)

                # Store in factory.db via db module
                db.execute(
                    "INSERT INTO agent_reports (agent_name, department, report_type, summary, cycle_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, datetime('now'))",
                    ('StrategicCore', 'ceo', 'weekly_summary',
                     str(_weekly_summary21)[:2000], cycle_id)
                )
                results['weekly_ceo_summary'] = str(_weekly_summary21)[:500]
                summary_lines.append("📊 CEO Weekly Summary (Phase 21): готово")
                logger.info("Phase 21 complete: CEO Weekly Summary")
            except Exception as _e21_inner:
                logger.error("Phase 21 inner error: %s", _e21_inner)
        else:
            logger.info("[Phase21] Skipping CEO Weekly Summary (not Friday, weekday=%d)", _weekday21)
    except Exception as e:
        logger.error("Phase 21 error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 22 — FINANCE DEPARTMENT ANALYSIS (heuristic, every cycle)
    # ════════════════════════════════════════════════════════════════
    logger.info("\n💰 PHASE 22: FINANCE DEPARTMENT ANALYSIS")
    try:
        from factory.agents.finance_department import (
            RevenueForecaster as _FD_Forecaster,
            CostOptimizer as _FD_Optimizer,
            PricingStrategist as _FD_Strategist,
            BudgetPlanner as _FD_Planner,
        )
        import sqlite3 as _sqlite3_22
        import datetime as _dt22

        _forecaster22 = _FD_Forecaster()
        _optimizer22 = _FD_Optimizer()
        _strategist22 = _FD_Strategist()
        _planner22 = _FD_Planner()

        # --- Collect order data from nevesty-models DB ---
        _orders_history22: list = []
        _bot_db_22 = "/home/user/Pablo/nevesty-models/data.db"
        if os.path.exists(_bot_db_22):
            try:
                _conn22 = _sqlite3_22.connect(_bot_db_22)
                _conn22.row_factory = _sqlite3_22.Row
                _rows22 = _conn22.execute(
                    "SELECT strftime('%Y-%m', created_at) AS month, "
                    "SUM(COALESCE(budget,0)) AS revenue, COUNT(*) AS cnt "
                    "FROM orders WHERE status='completed' "
                    "GROUP BY month ORDER BY month DESC LIMIT 6"
                ).fetchall()
                _orders_history22 = [dict(r) for r in _rows22]
                _conn22.close()
            except Exception as _db22_e:
                logger.warning("[Phase22] DB read error: %s", _db22_e)

        # --- Revenue forecast ---
        _revenue_forecast22 = _forecaster22.forecast_monthly_revenue(_orders_history22)
        logger.info(
            "[Phase22] Revenue forecast: %.0f (%s, %s)",
            _revenue_forecast22.get("forecast", 0),
            _revenue_forecast22.get("trend"),
            _revenue_forecast22.get("confidence"),
        )

        # --- Cost structure analysis (use illustrative fixed costs if no real data) ---
        _fixed_costs22: dict = {
            "hosting": 5_000,
            "sms_notifications": 2_000,
            "accounting": 3_000,
        }
        _cost_analysis22 = _optimizer22.analyze_cost_structure(_fixed_costs22)
        logger.info("[Phase22] Cost analysis: total=%.0f, suggestions=%d",
                    _cost_analysis22.get("total", 0), len(_cost_analysis22.get("suggestions", [])))

        # --- Seasonal pricing check ---
        _current_month22 = _dt22.date.today().month
        _seasonal_mult22 = _strategist22.get_seasonal_multiplier(_current_month22)
        logger.info("[Phase22] Seasonal multiplier for month %d: %.2f", _current_month22, _seasonal_mult22)

        # --- Budget plan ---
        _forecast_val22 = _revenue_forecast22.get("forecast", 150_000.0)
        _budget22 = _planner22.create_monthly_budget(
            revenue_forecast=_forecast_val22,
            fixed_costs=_fixed_costs22,
        )
        logger.info("[Phase22] Budget plan: total=%.0f, surplus=%.0f",
                    _budget22.get("total_budget", 0), _budget22.get("surplus", 0))

        results["phases"]["finance_department"] = {
            "revenue_forecast": _revenue_forecast22.get("forecast", 0),
            "trend": _revenue_forecast22.get("trend"),
            "confidence": _revenue_forecast22.get("confidence"),
            "cost_total": _cost_analysis22.get("total", 0),
            "cost_suggestions": len(_cost_analysis22.get("suggestions", [])),
            "seasonal_multiplier": _seasonal_mult22,
            "budget_surplus": _budget22.get("surplus", 0),
        }
        summary_lines.append(
            f"💰 Finance Dept (Phase 22): forecast={_revenue_forecast22.get('forecast', 0):.0f}₽, "
            f"trend={_revenue_forecast22.get('trend')}, season={_seasonal_mult22:.2f}x"
        )
    except Exception as e:
        logger.error("Phase 22 Finance Department error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # Phase 23: Research Department Analysis
    # ════════════════════════════════════════════════════════════════
    try:
        from factory.agents.research_department import (
            MarketResearcher, CompetitorAnalyst, TrendSpotter, InsightSynthesizer
        )
        _researcher23 = MarketResearcher()
        _analyst23 = CompetitorAnalyst()
        _spotter23 = TrendSpotter()
        _synthesizer23 = InsightSynthesizer()

        # Determine top segment from orders
        _top_segment23 = "commercial"
        try:
            if db_conn:
                _seg_row = db_conn.execute(
                    "SELECT event_type, COUNT(*) as cnt FROM orders "
                    "WHERE created_at >= date('now', '-30 days') AND event_type IS NOT NULL "
                    "GROUP BY event_type ORDER BY cnt DESC LIMIT 1"
                ).fetchone()
                if _seg_row:
                    _top_segment23 = _seg_row["event_type"] or "commercial"
        except Exception:
            pass

        _market23 = _researcher23.analyze_market_segment("commercial")
        _gaps23 = _analyst23.identify_competitive_gaps(["fashion", "events", "commercial"])
        _trends23 = _spotter23.get_actionable_trends()[:3]

        _conv_rate23 = 0.0
        _avg_budget23 = 0.0
        try:
            if db_conn:
                _stats_row = db_conn.execute(
                    "SELECT COUNT(CASE WHEN status IN ('confirmed','completed') THEN 1 END) * 1.0 / MAX(COUNT(*), 1) as conv, "
                    "AVG(CASE WHEN budget > 0 THEN budget END) as avg_b FROM orders "
                    "WHERE created_at >= date('now', '-30 days')"
                ).fetchone()
                if _stats_row:
                    _conv_rate23 = float(_stats_row["conv"] or 0)
                    _avg_budget23 = float(_stats_row["avg_b"] or 0)
        except Exception:
            pass

        _insights23 = _synthesizer23.synthesize_insights(
            _market23, _gaps23, _trends23,
            {"conversion_rate": _conv_rate23, "avg_budget": _avg_budget23}
        )
        results["phases"]["research_department"] = {
            "top_segment": _top_segment23,
            "market_opportunity_score": _market23.get("opportunity_score", 0),
            "top_opportunities": _insights23.get("top_opportunities", []),
            "strategic_alerts": _insights23.get("strategic_alerts", []),
            "confidence": _insights23.get("confidence_level", "low"),
        }
        summary_lines.append(
            f"🔬 Research Dept (Phase 23): segment={_top_segment23}, "
            f"opp_score={_market23.get('opportunity_score', 0)}, alerts={len(_insights23.get('strategic_alerts', []))}"
        )
    except Exception as e:
        logger.error("Phase 23 Research Department error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # Phase 24: Channel Content Generation
    # ════════════════════════════════════════════════════════════════
    try:
        from factory.agents.channel_content import ChannelContentGenerator
        _channel24 = ChannelContentGenerator()

        # Pick monthly stats from earlier phases if available
        _phase_stats24 = results.get("phases", {})
        _orders_count24 = 0
        _models_count24 = 0
        try:
            if db_conn:
                _ord_row = db_conn.execute(
                    "SELECT COUNT(*) as cnt FROM orders WHERE created_at >= date('now', '-30 days')"
                ).fetchone()
                _mod_row = db_conn.execute(
                    "SELECT COUNT(*) as cnt FROM models WHERE available=1 AND archived=0"
                ).fetchone()
                _orders_count24 = _ord_row["cnt"] if _ord_row else 0
                _models_count24 = _mod_row["cnt"] if _mod_row else 0
        except Exception:
            pass

        _stats_post24 = _channel24.generate_stats_post({
            "total_orders": _orders_count24,
            "active_models": _models_count24,
            "cities_served": 1,
            "avg_rating": 5.0,
        })
        _tips_post24 = _channel24.generate_tips_post("choosing_model")
        _calendar24 = _channel24.get_content_calendar(weeks=2)

        results["phases"]["channel_content"] = {
            "stats_post_chars": _stats_post24["char_count"],
            "tips_post_chars": _tips_post24["char_count"],
            "tips_post": _tips_post24["text"],
            "calendar_posts_scheduled": len(_calendar24),
            "next_post_format": _calendar24[0]["format"] if _calendar24 else "model_spotlight",
        }
        summary_lines.append(
            f"📢 Channel Content (Phase 24): {len(_calendar24)} posts scheduled, "
            f"next={_calendar24[0]['format'] if _calendar24 else 'n/a'}"
        )
    except Exception as e:
        logger.error("Phase 24 Channel Content error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 25 — CHANNEL PUBLISHER: publish one post to Telegram channel
    # ════════════════════════════════════════════════════════════════
    logger.info("\n📢 PHASE 25: CHANNEL PUBLISHER")
    try:
        _phase24_results = results.get("phases", {}).get("channel_content", {})
        _phase25_result = run_phase_25_channel_publisher(_phase24_results)
        results["phases"]["channel_publisher"] = _phase25_result
        _p25_status = _phase25_result.get("status", "unknown")
        if _p25_status == "published":
            summary_lines.append(
                f"📢 Channel Publisher (Phase 25): published msg_id={_phase25_result.get('message_id')}"
            )
        elif _p25_status == "skipped":
            summary_lines.append(
                f"📢 Channel Publisher (Phase 25): skipped ({_phase25_result.get('reason', '')})"
            )
        else:
            summary_lines.append(f"📢 Channel Publisher (Phase 25): {_p25_status}")
        logger.info("[Phase25] Channel publish result: %s", _phase25_result)
    except Exception as e:
        logger.error("Phase 25 Channel Publisher error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 26 — MODEL BIO GENERATOR: auto-generate bios for models
    # ════════════════════════════════════════════════════════════════
    logger.info("\n📝 PHASE 26: MODEL BIO GENERATOR")
    try:
        _phase26 = run_phase_26_model_bios("/home/user/Pablo/nevesty-models/data.db")
        results["phases"]["model_bios"] = _phase26
        summary_lines.append(
            f"📝 Model Bios (Phase 26): {_phase26.get('status', 'unknown')}, updated={_phase26.get('updated', 0)}"
        )
        logger.info("[Phase26] Model bios result: %s", _phase26)
    except Exception as e:
        logger.error("Phase 26 Model Bios error: %s", e)

    # ════════════════════════════════════════════════════════════════
    logger.info("\n❓ PHASE 27: FAQ GENERATOR")
    # Phase 27: FAQ Generator
    try:
        phase27_result = run_phase_27_faq_generator("/home/user/Pablo/nevesty-models/data.db")
        results["phases"]["faq_generator"] = phase27_result
        summary_lines.append(f"Phase27 FAQ: {phase27_result.get('suggestions', 0)} new suggestions")
    except Exception as e:
        results["phases"]["faq_generator"] = {"status": "error", "error": str(e)}

    # ════════════════════════════════════════════════════════════════
    logger.info("\n🧪 PHASE 28: A/B EXPERIMENT SYSTEM")
    # Phase 28: A/B Experiment System
    try:
        phase28_result = run_phase_28_experiments()
        results["phases"]["experiment_system"] = phase28_result
        summary_lines.append(f"Phase28 Experiments: {phase28_result.get('active_count', 0)} active, {phase28_result.get('proposed_count', 0)} proposed")
    except Exception as e:
        results["phases"]["experiment_system"] = {"status": "error", "error": str(e)}

    # ════════════════════════════════════════════════════════════════
    # PHASE 5.3 (БЛОК 5.3) — CEO WEEKLY + MONTHLY REPORTS (heuristic)
    # ════════════════════════════════════════════════════════════════
    logger.info("\n📊 PHASE 5.3: CEO WEEKLY + MONTHLY REPORTS")
    try:
        _ceo_reports = run_phase_ceo_reports(
            db_path="/home/user/Pablo/nevesty-models/data.db",
        )
        results["ceo_weekly_report"] = _ceo_reports.get("weekly_report", "")
        results["ceo_monthly_report"] = _ceo_reports.get("monthly_report", "")
        results["phases"]["ceo_reports_block53"] = {
            "status": _ceo_reports.get("status"),
            "cycles_loaded": _ceo_reports.get("cycles_loaded", 0),
            "weekly_lines": _ceo_reports.get("weekly_lines", 0),
            "monthly_lines": _ceo_reports.get("monthly_lines", 0),
        }
        summary_lines.append(
            f"📊 CEO Reports: weekly {_ceo_reports.get('weekly_lines', 0)} lines, "
            f"monthly {_ceo_reports.get('monthly_lines', 0)} lines"
        )
        logger.info(
            "[Phase5.3] CEO Reports: weekly=%d lines, monthly=%d lines, cycles=%d",
            _ceo_reports.get("weekly_lines", 0),
            _ceo_reports.get("monthly_lines", 0),
            _ceo_reports.get("cycles_loaded", 0),
        )
    except Exception as e:
        results["ceo_reports_error"] = str(e)
        logger.error("Phase 5.3 CEO Reports error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # CYCLE COMPLETE
    # ════════════════════════════════════════════════════════════════
    elapsed = round(time.time() - cycle_start, 1)
    results["elapsed_s"] = elapsed
    results["summary"] = "\n".join(summary_lines)

    icon = "💚" if results["health_score"] >= 70 else "🟡" if results["health_score"] >= 50 else "🔴"

    # Build enriched Telegram report with business metrics
    nm_metrics = raw_insights.get("nevesty_models", {})
    metric_lines = []
    if nm_metrics.get("orders_7d") is not None:
        growth = nm_metrics.get("orders_growth_pct", 0)
        growth_str = f"({'↑' if growth >= 0 else '↓'}{abs(growth):.0f}%)"
        metric_lines.append(f"📋 Заявок за 7д: {nm_metrics['orders_7d']} {growth_str}")
    if nm_metrics.get("revenue_30d"):
        metric_lines.append(f"💰 Выручка (30д): {nm_metrics['revenue_30d']:,} ₽")
    if nm_metrics.get("repeat_client_rate_pct") is not None:
        metric_lines.append(f"🔁 Повторные: {nm_metrics['repeat_client_rate_pct']}%")
    if nm_metrics.get("reviews_avg_rating"):
        metric_lines.append(f"⭐ Рейтинг: {nm_metrics['reviews_avg_rating']}/5")

    dept_focus = results.get("ceo_department_focus", "")
    focus_line = f"\n🎯 Фокус: {dept_focus}" if dept_focus else ""

    tg_report = (
        f"{icon} AI Office цикл завершён\n"
        f"Health: {results['health_score']}% | {elapsed}с{focus_line}\n\n"
        + ("\n".join(metric_lines) + "\n\n" if metric_lines else "")
        + "\n".join(summary_lines[:6])
    )

    db.execute(
        "UPDATE cycles SET phase='done', summary=?, health_score=?, decisions_count=?, "
        "actions_count=?, duration_s=?, finished_at=? WHERE id=?",
        (
            results["summary"][:500],
            results["health_score"],
            len(decisions),
            total_new_actions,
            elapsed,
            datetime.now(timezone.utc).isoformat(),
            cycle_id,
        ),
    )

    results["duration_seconds"] = elapsed

    logger.info("✅ CYCLE DONE: %.1fs | Score=%s%%", elapsed, results["health_score"])
    notify(tg_report)
    _save_cycle_to_history(results)

    # Send summary via notify.js (uses bot token from .env, consistent with other notifications)
    _notify_admins_telegram(results, decisions, cycle_id)

    # Notify admin via bot after cycle completes (supplementary direct API call)
    try:
        import json as _json_notify
        import re as _re_notify
        import urllib.request as _urllib_notify
        _bot_token_notify = os.getenv('BOT_TOKEN') or os.getenv('TELEGRAM_BOT_TOKEN')
        _admin_ids_raw_notify = os.getenv('ADMIN_TELEGRAM_IDS', '')
        if _bot_token_notify and _admin_ids_raw_notify:
            _admin_ids_notify = [x.strip() for x in _admin_ids_raw_notify.split(',') if x.strip()]
            _dept_count_notify = len([
                k for k in results
                if k not in ('cycle_id', 'started_at', 'completed_at', 'weekly_ceo_summary',
                             'experiments', 'phases', 'health_score', 'elapsed_s',
                             'duration_seconds', 'summary', 'ceo_department_focus')
            ])
            _summary_lines_notify = [
                f"🏭 Factory цикл завершено",
                f"Health: {results.get('health_score', 0)}% | {elapsed}с",
            ]
            if 'weekly_ceo_summary' in results:
                _ceo_excerpt = results['weekly_ceo_summary'][:200]
                _summary_lines_notify.append(f"CEO: {_ceo_excerpt}")
            _msg_notify = '\n'.join(_summary_lines_notify)
            for _admin_id_notify in _admin_ids_notify:
                try:
                    _req_notify = _urllib_notify.Request(
                        f'https://api.telegram.org/bot{_bot_token_notify}/sendMessage',
                        data=_json_notify.dumps({
                            'chat_id': _admin_id_notify,
                            'text': _msg_notify[:4096],
                        }).encode(),
                        headers={'Content-Type': 'application/json'},
                    )
                    _urllib_notify.urlopen(_req_notify, timeout=5)
                except Exception:
                    pass
    except Exception:
        pass

    # ─── Record last-run timestamp in bot_settings for monitoring ────────────
    try:
        import sqlite3 as _sqlite3_lr
        import os as _os_lr
        _bot_db_lr = _os_lr.path.abspath(
            _os_lr.path.join(_os_lr.path.dirname(__file__), '..', 'nevesty-models', 'nevesty.db')
        )
        if _os_lr.path.exists(_bot_db_lr):
            _conn_lr = _sqlite3_lr.connect(_bot_db_lr)
            _ts_lr = datetime.now(timezone.utc).isoformat()
            _conn_lr.execute(
                "INSERT OR REPLACE INTO bot_settings (key, value, updated_at) VALUES ('factory_last_cycle', ?, CURRENT_TIMESTAMP)",
                [_ts_lr]
            )
            _conn_lr.commit()
            _conn_lr.close()
            logger.info("[LastRun] factory_last_cycle written to bot_settings: %s", _ts_lr)
    except Exception as _e_lr:
        logger.warning("[LastRun] Failed to write factory_last_cycle: %s", _e_lr)

    return results
