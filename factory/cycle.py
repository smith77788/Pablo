"""🔁 AI Office Cycle — CEO диспетчеризует задачи по департаментам."""
from __future__ import annotations
import json
import logging
import time
from datetime import datetime, timezone

from factory import db
from factory.agents.strategic_core import StrategicCore
from factory.agents.analytics_engine import AnalyticsEngine
from factory.agents.experiment_system import ExperimentSystem
from factory.agents.base import FactoryAgent
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


def run_cycle() -> dict:
    """Один полный цикл AI-офиса. Возвращает сводку."""
    cycle_start = time.time()
    cycle_id = datetime.now(timezone.utc).isoformat()
    summary_lines = []

    logger.info("=" * 60)
    logger.info("🏢 AI OFFICE CYCLE: %s", cycle_id)
    logger.info("=" * 60)

    db.init_db()
    nevesty_id = _ensure_nevesty_product()

    db.insert("cycles", {
        "id": cycle_id,
        "phase": "started",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "summary": "Цикл запущен",
    })

    results = {
        "cycle_id": cycle_id,
        "timestamp": cycle_id,
        "phases": {},
        "decisions": [],
        "new_actions": 0,
        "experiments_concluded": 0,
        "health_score": 50,
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
        raw_insights = analytics_engine.analyze(all_metrics)
        analytics_engine.persist_nevesty_metrics(nevesty_id, all_metrics.get("nevesty_models", {}))

        # Расширенный анализ через Analytics Department (если есть ключ)
        running_exps = db.get_running_experiments()
        analytics_dept = _load_dept("analytics")
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
        ceo = StrategicCore()
        decisions, _ = ceo.decide(insights, all_metrics)
        results["decisions"] = decisions
        results["phases"]["ceo"] = {"decisions_count": len(decisions)}
        summary_lines.append(f"🧠 Решений CEO: {len(decisions)}")
        for d in decisions:
            logger.info("  [CEO] %s → %s", d.get("type"), d.get("rationale", "")[:60])
    except Exception as e:
        logger.error("CEO phase error: %s", e)

    # ════════════════════════════════════════════════════════════════
    # PHASE 3 — DEPARTMENTS EXECUTION: CEO диспетчеризует задачи
    # ════════════════════════════════════════════════════════════════
    logger.info("\n🏢 DEPARTMENTS EXECUTION")

    dept_marketing = _load_dept("marketing")
    dept_product = _load_dept("product")
    dept_hr = _load_dept("hr")
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
        ops = _load_dept("operations")
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
        tech = _load_dept("tech")
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
        sales = _load_dept("sales")
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
        creative = _load_dept("creative")
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
        cs = _load_dept("customer_success")
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
    # PHASE 7 — FINANCE + RESEARCH DEPARTMENTS
    # ════════════════════════════════════════════════════════════════
    logger.info("\n💰 FINANCE + RESEARCH DEPTS")
    dept_context = {"insights": insights, "metrics": all_metrics}

    # Finance Department
    try:
        finance = _load_dept("finance")
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
        research = _load_dept("research")
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
    # PHASE 8 — CEO SYNTHESIS: синтез всех департаментов
    # ════════════════════════════════════════════════════════════════
    logger.info("\n🏆 CEO SYNTHESIS")
    try:
        # Собираем краткие итоги всех департаментов
        all_department_results = {}
        for dept_name, dept_data in results["phases"].items():
            if dept_name not in ("analytics", "ceo", "departments", "ideas"):
                all_department_results[dept_name] = dept_data

        class CEOSynthesisAgent(FactoryAgent):
            department = "ceo"
            role = "ceo_synthesis"
            name = "ceo_synthesis"
            system_prompt = (
                "Ты — CEO агентства моделей Nevesty Models. "
                "Принимаешь стратегические решения на основе данных всех департаментов. "
                "Мыслишь чётко, расставляешь приоритеты, даёшь конкретные указания команде. "
                "Всё на русском языке."
            )

        ceo_agent = CEOSynthesisAgent()

        # Load trend data for CEO context
        prev_cycles = _load_metrics_trend('phases.ceo_synthesis.health_score', last_n=3)
        trend_info = f"Тренд health_score за последние циклы: {[c.get('value') for c in prev_cycles]}" if prev_cycles else "Первый цикл (история отсутствует)"

        context_str = json.dumps(all_department_results, ensure_ascii=False, indent=2, default=str)

        memo_prompt = f"""Ты — CEO модельного агентства. Напиши ЕЖЕНЕДЕЛЬНЫЙ МЕМОРАНДУМ на русском языке.

Данные этого цикла:
{context_str}

{trend_info}

Структура меморандума (строго следуй):
## 📊 Ключевые метрики
- 3 главных показателя этой недели

## 🎯 Главное решение недели
- Одно конкретное действие которое нужно выполнить

## ⚠️ Риски
- Главный риск (1 пункт)

## 🚀 Возможности
- Главная возможность (1 пункт)

## 📋 Задачи команде
- 3 конкретных задачи для операционной команды

Пиши кратко, по делу, как настоящий CEO. Максимум 300 слов."""

        ceo_synthesis_prompt = (
            "Ты — CEO агентства моделей Nevesty Models. Получи отчёты всех департаментов и сделай выводы.\n\n"
            "ИНСТРУКЦИЯ ДЛЯ CEO MEMO:\n"
            + memo_prompt
            + "\n\nОТЧЁТЫ ДЕПАРТАМЕНТОВ:\n"
            + context_str
            + "\n\nВерни JSON:\n"
            '{\n'
            '  "health_score": 75,\n'
            '  "weekly_focus": "Улучшить конверсию из просмотра каталога в заявку",\n'
            '  "growth_actions": [\n'
            '    {"priority": 1, "action": "...", "department": "marketing", "expected_impact": "высокий"},\n'
            '    {"priority": 2, "action": "...", "department": "sales", "expected_impact": "средний"}\n'
            '  ],\n'
            '  "ceo_memo": "Еженедельный меморандум CEO со структурой из инструкции выше...",\n'
            '  "risks": ["Риск 1", "Риск 2"],\n'
            '  "opportunities": ["Возможность 1", "Возможность 2"]\n'
            '}'
        )

        ceo_synthesis = ceo_agent.think_json(ceo_synthesis_prompt, max_tokens=2000)

        if ceo_synthesis and isinstance(ceo_synthesis, dict):
            # Обновляем health_score если CEO дал оценку
            if "health_score" in ceo_synthesis:
                results["health_score"] = ceo_synthesis["health_score"]

            results["phases"]["ceo_synthesis"] = ceo_synthesis

            # Сохраняем CEO Weekly Memo в БД как growth_action
            memo_text = ceo_synthesis.get("ceo_memo", "")
            weekly_focus = ceo_synthesis.get("weekly_focus", "")
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
            logger.info(
                "[CEOSynthesis] health=%s, focus=%s, actions=%s",
                ceo_synthesis.get("health_score"),
                weekly_focus[:60] if weekly_focus else "—",
                actions_count,
            )

            # Sync growth actions to bot DB and send CEO memo to Telegram
            final_health = ceo_synthesis.get("health_score", results["health_score"])
            bot_db = "/home/user/Pablo/nevesty-models/data.db"
            _sync_growth_actions_to_bot_db(growth_actions_list, bot_db)
            _send_ceo_memo_to_telegram(memo_text, final_health, growth_actions_list)
    except Exception as e:
        logger.error("CEO Synthesis phase error: %s", e)

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
    # CYCLE COMPLETE
    # ════════════════════════════════════════════════════════════════
    elapsed = round(time.time() - cycle_start, 1)
    results["elapsed_s"] = elapsed
    results["summary"] = "\n".join(summary_lines)

    icon = "💚" if results["health_score"] >= 70 else "🟡" if results["health_score"] >= 50 else "🔴"
    tg_report = (
        f"{icon} AI Office цикл завершён\n"
        f"Health: {results['health_score']}% | {elapsed}с\n\n"
        + "\n".join(summary_lines[:8])
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
    return results
