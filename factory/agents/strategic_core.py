"""🧠 Strategic AI Core — CEO Brain: принимает стратегические решения."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from factory.agents.base import FactoryAgent
from factory import db

logger = logging.getLogger(__name__)

DECISION_TYPES = {
    "create_mvp":   "Создать новый продукт/MVP",
    "scale":        "Масштабировать продукт",
    "kill":         "Закрыть продукт",
    "iterate":      "Улучшить продукт",
    "grow":         "Усилить маркетинг",
    "experiment":   "Запустить A/B эксперимент",
    "optimize":     "Оптимизировать конверсию",
    "monitor":      "Продолжать мониторинг",
}


class StrategicCore(FactoryAgent):
    name = "strategic_core"
    system_prompt = """Ты — CEO Brain AI Startup Factory. Ты принимаешь стратегические решения.

ТВОИ ПРАВИЛА:
1. Максимум 3-5 активных продуктов одновременно
2. Фокус на росте конверсии — это главный KPI
3. При падении revenue → оптимизируй текущие продукты
4. При низком трафике → усиливай growth engine
5. Эксперименты: conversion > 5% → SCALE, < 2% → KILL, иначе → ITERATE
6. Каждое решение должно быть обосновано данными
7. Не создавай новые MVP если текущие не работают

Отвечай только на русском. Решения должны быть конкретными и actionable."""

    MAX_ACTIVE_PRODUCTS = 5

    def decide(self, analytics_insights: dict, all_metrics: dict) -> list[dict]:
        """Generate strategic decisions based on analytics."""
        active_products = db.get_active_products()
        running_experiments = db.get_running_experiments()
        recent_decisions = db.get_recent_decisions(10)
        pending_actions = db.get_pending_growth_actions(5)
        ideas = db.fetch_all("SELECT * FROM ideas WHERE status='new' ORDER BY priority DESC LIMIT 5")

        context = {
            "analytics_insights": analytics_insights,
            "all_metrics": all_metrics,
            "active_products_count": len(active_products),
            "active_products": [{"id": p["id"], "name": p["name"], "category": p["category"]} for p in active_products],
            "running_experiments": len(running_experiments),
            "recent_decisions": [d["decision_type"] for d in recent_decisions[-5:]],
            "pending_growth_actions": len(pending_actions),
            "top_ideas": [{"id": i["id"], "title": i["title"], "priority": i["priority"]} for i in ideas],
            "max_active_products": self.MAX_ACTIVE_PRODUCTS,
        }

        decisions_raw = self.think_json(
            "На основе данных сгенерируй список стратегических решений. Верни JSON массив:\n"
            "[\n"
            "  {\n"
            '    "type": "create_mvp|scale|kill|iterate|grow|experiment|optimize|monitor",\n'
            '    "product_id": null_или_id,\n'
            '    "priority": 1-10,\n'
            '    "rationale": "обоснование решения",\n'
            '    "action": "конкретное действие которое нужно выполнить",\n'
            '    "expected_impact": "ожидаемый результат"\n'
            "  }\n"
            "]\n"
            "Максимум 5 решений. Только те которые реально нужны сейчас.",
            context=context,
            max_tokens=1500,
        )

        if not isinstance(decisions_raw, list):
            decisions_raw = []

        decisions = []
        cycle_id = datetime.now(timezone.utc).isoformat()

        for d in decisions_raw[:5]:
            if not isinstance(d, dict) or "type" not in d:
                continue
            if d["type"] not in DECISION_TYPES:
                continue

            dec_id = db.insert("decisions", {
                "cycle_id": cycle_id,
                "decision_type": d["type"],
                "product_id": d.get("product_id"),
                "rationale": d.get("rationale", ""),
                "payload": d,
                "executed": 0,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            d["_db_id"] = dec_id
            decisions.append(d)
            logger.info("[CEO] Decision: %s — %s", d["type"], d.get("rationale", "")[:60])

        return decisions, cycle_id

    def synthesize_dept_reports(self, dept_results: dict) -> dict:
        """Synthesize department reports into a strategic briefing and delegation plan."""
        summary_parts = []
        for dept, result in dept_results.items():
            if isinstance(result, dict):
                for role, data in result.items():
                    if isinstance(data, dict) and 'result' in data:
                        summary_parts.append(f"[{dept}/{role}]: {str(data['result'])[:200]}")
                    elif isinstance(data, str):
                        summary_parts.append(f"[{dept}/{role}]: {data[:200]}")
            elif isinstance(result, str):
                summary_parts.append(f"[{dept}]: {result[:200]}")

        dept_summary = "\n".join(summary_parts[:20]) if summary_parts else "No department reports available"

        recent_decisions = db.get_recent_decisions(5)
        prev_decisions_str = ", ".join(d.get("decision_type", "") for d in recent_decisions) if recent_decisions else "none"

        briefing = self.think_json(
            "Ты CEO. Проанализируй отчёты всех департаментов и создай стратегическое резюме. Верни JSON:\n"
            "{\n"
            '  "key_insights": ["insight1", "insight2", "insight3"],\n'
            '  "priority_issues": ["issue1", "issue2"],\n'
            '  "next_cycle_focus": "название департамента который нужно усилить",\n'
            '  "recommended_action": "конкретное действие на следующую неделю",\n'
            '  "health_score": 0-100,\n'
            '  "summary": "2-3 предложения с итогом"\n'
            "}",
            context={
                "department_reports": dept_summary,
                "previous_decisions": prev_decisions_str,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            max_tokens=800,
        )

        if not isinstance(briefing, dict):
            briefing = {
                "key_insights": [],
                "priority_issues": [],
                "next_cycle_focus": "marketing",
                "recommended_action": "Continue current strategy",
                "health_score": 70,
                "summary": "CEO synthesis unavailable",
            }

        cycle_id = datetime.now(timezone.utc).isoformat()
        db.save_ceo_decision(
            cycle_id=cycle_id,
            decision_text=briefing.get("summary", ""),
            metadata=briefing,
        )
        logger.info("[CEO] Synthesis: health=%s focus=%s", briefing.get("health_score"), briefing.get("next_cycle_focus"))
        return briefing

    def generate_weekly_report_text(self) -> str:
        """Generate a weekly summary report from recent decisions (returns plain text)."""
        recent_decisions = db.get_recent_decisions(20)
        dec_types = {}
        for d in recent_decisions:
            t = d.get("decision_type", "other")
            dec_types[t] = dec_types.get(t, 0) + 1

        dec_summary = "\n".join(f"- {t}: {c} раз" for t, c in dec_types.items()) if dec_types else "Нет решений"

        report = self.think(
            "Ты CEO. Составь краткий недельный отчёт (5-8 пунктов).\n"
            "Структура: что было сделано, ключевые метрики, проблемы, план на следующую неделю.\n"
            "Отвечай на русском, деловой стиль, конкретно.",
            context={"decisions_this_week": dec_summary},
            max_tokens=500,
        )
        return report or "Weekly report unavailable"

    def generate_weekly_report(self, metrics: dict | None = None) -> dict:
        """Generate a strategic weekly report dict with headline, highlights, concerns.

        Args:
            metrics: KPI dict with keys like orders_week, revenue_month,
                     conversion_rate, clients_new_month, avg_rating.
                     If None, falls back to an empty metrics dict.
        Returns:
            dict with keys: week, headline, highlights, concerns,
                            focus_next_week, key_metric_trend
        """
        import json
        if metrics is None:
            metrics = {}

        week_str = datetime.now(timezone.utc).strftime('%Y-W%U')

        # Check if already generated this week
        try:
            existing = db.fetch_one(
                "SELECT id FROM ceo_decisions WHERE cycle_id = ?",
                (f"weekly_{week_str}",)
            )
            if existing:
                return {"status": "already_generated", "week": week_str}
        except Exception:
            pass

        prompt = (
            f"Создай еженедельный стратегический отчёт для модельного агентства Nevesty Models.\n\n"
            f"Метрики за неделю:\n"
            f"- Заявок: {metrics.get('orders_week', 0)}\n"
            f"- Выручка: {metrics.get('revenue_month', 0)} руб.\n"
            f"- Конверсия: {metrics.get('conversion_rate', 0)}%\n"
            f"- Новых клиентов: {metrics.get('clients_new_month', 0)}\n"
            f"- Средний рейтинг: {metrics.get('avg_rating', 0)}\n\n"
            f'Верни JSON:\n{{"week": "{week_str}",\n'
            f'  "headline": "Одна строка резюме недели",\n'
            f'  "highlights": ["достижение 1", "достижение 2"],\n'
            f'  "concerns": ["проблема 1"],\n'
            f'  "focus_next_week": "главный приоритет",\n'
            f'  "key_metric_trend": "positive|negative|stable"}}'
        )

        try:
            result = self.think(prompt, max_tokens=600)
            import re
            m = re.search(r'\{.*\}', result, re.DOTALL)
            if m:
                report = json.loads(m.group())
                # Save to ceo_decisions for deduplication
                try:
                    db.execute(
                        "INSERT INTO ceo_decisions "
                        "(cycle_id, decision_text, created_at) "
                        "VALUES (?, ?, datetime('now'))",
                        (f"weekly_{week_str}", json.dumps(report, ensure_ascii=False)),
                    )
                except Exception:
                    pass
                logger.info("[CEO] Weekly report generated: trend=%s", report.get("key_metric_trend"))
                return report
        except Exception as _e:
            logger.warning("[CEO] generate_weekly_report fallback: %s", _e)

        return {
            "week": week_str,
            "headline": "Стабильная работа агентства",
            "highlights": ["Заявки поступают", "Клиенты довольны"],
            "concerns": [],
            "focus_next_week": "Увеличить количество заявок",
            "key_metric_trend": "stable",
        }

    def propose_experiments(self, metrics: dict | None = None) -> list:
        """Propose A/B experiments based on current metrics.

        Args:
            metrics: KPI dict with keys conversion_rate, avg_check, clients_repeat.
        Returns:
            List of up to 3 experiment dicts with keys:
            hypothesis, metric, control, variant, duration_days, expected_lift_pct
        """
        import json, re
        if metrics is None:
            metrics = {}

        prompt = (
            f"На основе метрик агентства предложи 3 A/B эксперимента для роста:\n"
            f"Конверсия: {metrics.get('conversion_rate', 0)}%\n"
            f"Средний чек: {metrics.get('avg_check', 0)} руб.\n"
            f"Повторные клиенты: {metrics.get('clients_repeat', 0)}\n\n"
            f"Формат каждого эксперимента — JSON:\n"
            f'{{"hypothesis": "...", "metric": "conversion_rate|avg_check|repeat_rate", '
            f'"control": "текущий вариант", "variant": "тестируемый вариант",'
            f'"duration_days": 14, "expected_lift_pct": 10}}\n\n'
            f"Верни JSON массив из 3 экспериментов."
        )
        try:
            result = self.think(prompt, max_tokens=800)
            m = re.search(r'\[.*\]', result, re.DOTALL)
            if m:
                experiments = json.loads(m.group())
                # Persist proposals to factory DB experiments table
                for exp in experiments[:3]:
                    try:
                        db.execute(
                            "INSERT OR IGNORE INTO experiments "
                            "(hypothesis, metric, status, created_at) "
                            "VALUES (?, ?, 'proposed', datetime('now'))",
                            (exp.get('hypothesis', 'Test'), exp.get('metric', 'conversion_rate')),
                        )
                    except Exception:
                        pass
                logger.info("[CEO] Proposed %d experiments", len(experiments[:3]))
                return experiments[:3]
        except Exception as _e:
            logger.warning("[CEO] propose_experiments fallback: %s", _e)

        return [
            {
                "hypothesis": "Добавить кнопку быстрого звонка в каталоге",
                "metric": "conversion_rate",
                "control": "без кнопки",
                "variant": "с кнопкой звонка",
                "duration_days": 14,
                "expected_lift_pct": 8,
            },
            {
                "hypothesis": "Показывать цену в описании модели",
                "metric": "avg_check",
                "control": "без цены",
                "variant": "с ценой 'от X руб.'",
                "duration_days": 14,
                "expected_lift_pct": 5,
            },
            {
                "hypothesis": "Email-напоминание через 30 дней",
                "metric": "repeat_rate",
                "control": "без напоминания",
                "variant": "с email на 30-й день",
                "duration_days": 30,
                "expected_lift_pct": 12,
            },
        ]

    def generate_monthly_report(self) -> dict:
        """Generate a monthly strategic report with KPIs and roadmap."""
        recent_decisions = db.get_recent_decisions(50)
        dec_types: dict[str, int] = {}
        for d in recent_decisions:
            t = d.get("decision_type", "other")
            dec_types[t] = dec_types.get(t, 0) + 1

        active_products = db.get_active_products()
        running_experiments = db.get_running_experiments()

        report = self.think_json(
            "Ты CEO. Составь ежемесячный стратегический отчёт. Верни JSON:\n"
            "{\n"
            '  "period": "Месяц ГГГГ",\n'
            '  "executive_summary": "2-3 предложения итога месяца",\n'
            '  "kpis": {"total_decisions": N, "experiments_run": N, "active_products": N},\n'
            '  "achievements": ["достижение 1", "достижение 2", "достижение 3"],\n'
            '  "challenges": ["проблема 1", "проблема 2"],\n'
            '  "next_month_goals": ["цель 1", "цель 2", "цель 3"],\n'
            '  "strategic_direction": "куда движемся в следующем месяце",\n'
            '  "health_trend": "improving|stable|declining"\n'
            "}",
            context={
                "decisions_by_type": dec_types,
                "active_products": len(active_products),
                "running_experiments": len(running_experiments),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            max_tokens=700,
        )

        if not isinstance(report, dict):
            report = {
                "period": datetime.now(timezone.utc).strftime("%B %Y"),
                "executive_summary": "Monthly report unavailable",
                "kpis": {"total_decisions": len(recent_decisions), "experiments_run": len(running_experiments), "active_products": len(active_products)},
                "achievements": [],
                "challenges": [],
                "next_month_goals": [],
                "strategic_direction": "Continue current strategy",
                "health_trend": "stable",
            }

        cycle_id = f"monthly_{datetime.now(timezone.utc).strftime('%Y_%m')}"
        db.save_ceo_decision(
            cycle_id=cycle_id,
            decision_text=report.get("executive_summary", ""),
            metadata={"type": "monthly_report", **report},
        )
        logger.info("[CEO] Monthly report: trend=%s direction=%s", report.get("health_trend"), report.get("strategic_direction", "")[:40])
        return report

    def track_decision_execution(self, decision_id: int) -> dict:
        """Check if a previous decision has been executed and its impact."""
        decisions = db.fetch_all(
            "SELECT * FROM decisions WHERE id=? LIMIT 1", (decision_id,)
        )
        if not decisions:
            return {"error": "Decision not found"}

        decision = decisions[0]
        status = self.think_json(
            "Оцени выполнение стратегического решения. Верни JSON:\n"
            '{"executed": true/false, "impact": "описание влияния", "completion_pct": 0-100, "blockers": []}',
            context={
                "decision_type": decision.get("decision_type"),
                "rationale": decision.get("rationale"),
                "payload": decision.get("payload"),
                "created_at": decision.get("created_at"),
            },
            max_tokens=300,
        )
        if isinstance(status, dict):
            db.run(
                "UPDATE decisions SET executed=? WHERE id=?",
                (1 if status.get("executed") else 0, decision_id),
            )
        return status or {"executed": False, "impact": "Unknown", "completion_pct": 0, "blockers": []}

    def propose_ab_experiment(self, context: dict) -> dict:
        """Propose a new A/B experiment based on current performance."""
        experiment = self.think_json(
            "Предложи конкретный A/B эксперимент для агентства моделей. Верни JSON:\n"
            "{\n"
            '  "name": "название эксперимента",\n'
            '  "hypothesis": "гипотеза что проверяем",\n'
            '  "variant_a": "контрольная версия",\n'
            '  "variant_b": "тестируемая версия",\n'
            '  "metric": "что измеряем (конверсия/CTR/retention)",\n'
            '  "duration_days": 14,\n'
            '  "expected_lift_pct": 15\n'
            "}",
            context=context,
            max_tokens=400,
        )
        if not isinstance(experiment, dict):
            experiment = {
                "name": "Homepage CTA Test",
                "hypothesis": "Изменение цвета кнопки увеличит CTR",
                "variant_a": "Золотая кнопка 'Забронировать'",
                "variant_b": "Белая кнопка 'Выбрать модель'",
                "metric": "CTR на booking страницу",
                "duration_days": 14,
                "expected_lift_pct": 20,
            }
        logger.info("[CEO] Proposed experiment: %s", experiment.get("name"))
        return experiment

    def generate_weekly_summary(self, db_path: str, dept_results: list) -> dict:
        """CEO synthesizes department results and DB metrics into weekly strategy.

        Args:
            db_path: Path to the SQLite database (nevesty-models data.db or factory).
            dept_results: List of department result dicts from the current cycle.
        Returns:
            dict with week_orders, week_completed, completion_rate,
                  next_focus, decision, dept_highlights.
        """
        import sqlite3

        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*) as orders,
                       SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed
                FROM orders WHERE created_at >= datetime('now', '-7 days')
            """)
            row = cur.fetchone()
            conn.close()
            orders_week = row[0] if row and row[0] else 0
            completed_week = row[1] if row and row[1] else 0
        except Exception:
            orders_week, completed_week = 0, 0

        completion_rate = round(completed_week / max(orders_week, 1) * 100, 1)

        # CEO decision logic: what to focus next based on outcomes
        if completed_week == 0 and orders_week == 0:
            focus_dept = "marketing"
            decision_text = "Нет заявок за неделю — критический фокус на маркетинге"
        elif completion_rate < 50:
            focus_dept = "operations"
            decision_text = f"Выполнено только {completion_rate}% заявок — фокус на операциях"
        elif orders_week < 3:
            focus_dept = "marketing"
            decision_text = f"Мало заявок ({orders_week}) — нужно усилить маркетинг"
        else:
            focus_dept = "product"
            decision_text = f"Выполнено {completed_week}/{orders_week} заявок — фокус на улучшении продукта"

        # Extract dept highlights (top insight per dept)
        dept_highlights = []
        for item in (dept_results or []):
            if isinstance(item, dict):
                dept = item.get("department") or item.get("dept", "")
                insight = item.get("insight") or item.get("summary") or item.get("result", "")
                if dept and insight:
                    dept_highlights.append({"dept": dept, "insight": str(insight)[:100]})

        summary = {
            "week_orders": orders_week,
            "week_completed": completed_week,
            "completion_rate": completion_rate,
            "next_focus": focus_dept,
            "decision": decision_text,
            "dept_highlights": dept_highlights[:5],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

        logger.info(
            "[CEO] Weekly summary: orders=%d, completed=%d, focus=%s",
            orders_week, completed_week, focus_dept,
        )
        return summary

    def delegate_next_cycle(self, dept_results: dict) -> str:
        """Decide which department gets priority focus for the next cycle.

        Scans department results for the lowest score / worst health and returns
        a human-readable delegation directive for Telegram / CEO memo.

        Args:
            dept_results: mapping of department name → result dict.
                          Each result may carry a 'score' (0-100) and/or 'status'
                          ('ok', 'warning', 'error').
        Returns:
            Delegation string, e.g. "Приоритет следующего цикла: sales (требует внимания)"
        """
        worst_dept: str | None = None
        worst_score = 999

        status_weight = {"error": 0, "warning": 40, "ok": 100}

        for dept, result in dept_results.items():
            if not isinstance(result, dict):
                continue
            # Prefer an explicit numeric score; fall back to status → implicit score
            score = result.get("score")
            if score is None:
                score = status_weight.get(result.get("status", "ok"), 100)
            try:
                score = int(score)
            except (TypeError, ValueError):
                score = 100

            if score < worst_score:
                worst_score = score
                worst_dept = dept

        if worst_dept and worst_score < 70:
            directive = f"Приоритет следующего цикла: {worst_dept} (требует внимания, score={worst_score})"
        elif worst_dept:
            directive = f"Все департаменты работают штатно. Рекомендуемый фокус: {worst_dept} для проактивного роста"
        else:
            directive = "Все департаменты работают штатно"

        logger.info("[CEO] Delegation: %s", directive)
        return directive

    def track_decisions(self, last_n: int = 10) -> list[dict]:
        """Return recent decisions with their execution status for CEO tracking.

        Args:
            last_n: How many recent decisions to fetch.
        Returns:
            List of decision dicts with keys: id, decision_type, rationale,
            executed, created_at.
        """
        rows = db.get_recent_decisions(last_n)
        tracking = []
        for row in rows:
            tracking.append({
                "id": row.get("id"),
                "decision_type": row.get("decision_type"),
                "rationale": (row.get("rationale") or "")[:120],
                "executed": bool(row.get("executed")),
                "created_at": row.get("created_at"),
            })
        if tracking:
            executed_count = sum(1 for t in tracking if t["executed"])
            logger.info(
                "[CEO] Decision tracking: %d/%d executed",
                executed_count, len(tracking),
            )
        return tracking

    def evaluate_experiment(self, experiment: dict) -> str:
        """Evaluate a running experiment and decide scale/iterate/kill."""
        conv_a = experiment.get("conversion_a", 0)
        conv_b = experiment.get("conversion_b", 0)

        # Rule-based first
        if conv_b > 5.0:
            result = "scale"
        elif conv_b < 2.0 and conv_a >= conv_b:
            result = "kill"
        else:
            result = "iterate"

        # AI confirmation for edge cases
        if 2.0 <= conv_b <= 5.0:
            ai_result = self.think_json(
                "Оцени результат A/B теста и реши: scale, iterate или kill?",
                context=experiment,
                max_tokens=256,
            )
            if isinstance(ai_result, dict) and ai_result.get("result") in ("scale", "iterate", "kill"):
                result = ai_result["result"]

        return result
