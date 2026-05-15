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

    logger.info("✅ CYCLE DONE: %.1fs | Score=%s%%", elapsed, results["health_score"])
    notify(tg_report)
    return results
