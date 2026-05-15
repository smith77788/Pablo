"""🧪 Experiment System — A/B тесты, traffic split, scale/iterate/kill решения."""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone

from factory.agents.base import FactoryAgent
from factory import db

logger = logging.getLogger(__name__)


class ExperimentDesigner(FactoryAgent):
    """Designs A/B experiments for the modeling agency platform."""
    name = "ExperimentDesigner"
    department = "experiments"
    role = "designer"
    system_prompt = """Ты — специалист по A/B тестированию для Telegram-ботов и веб-сервисов.
Разрабатываешь конкретные, измеримые гипотезы для улучшения конверсии.

Формат гипотезы:
{
  "id": "exp_001",
  "hypothesis": "Добавление кнопки 'Быстрая заявка' на главный экран увеличит конверсию на 15%",
  "type": "bot|site|both",
  "metric": "booking_count",
  "variant_a": "текущее состояние",
  "variant_b": "предлагаемое изменение",
  "effort": "low|medium|high",
  "expected_lift": "+10-20%",
  "status": "proposed"
}"""

    def generate_hypotheses(self, context: dict) -> list[dict]:
        """Generate A/B test hypotheses based on context."""
        prompt = f"""Данные по платформе:
- Заявок: {context.get('total_orders', 0)}
- Конверсия: {context.get('conversion_rate', 0)}%
- Активных клиентов: {context.get('active_clients', 0)}

Придумай 3 конкретных A/B эксперимента для улучшения конверсии.
Отвечай JSON массивом из 3 объектов. Каждый объект: hypothesis, type, metric, variant_a, variant_b, effort, expected_lift."""
        result = self.think_json(prompt, context)
        if isinstance(result, list):
            for i, exp in enumerate(result):
                exp['id'] = f'exp_{len(result):03d}_{i}'
                exp['status'] = 'proposed'
            return result
        return []


class ExperimentEvaluator(FactoryAgent):
    """Evaluates running A/B experiments."""
    name = "ExperimentEvaluator"
    department = "experiments"
    role = "evaluator"
    system_prompt = """Ты — аналитик A/B тестов. Оцениваешь результаты экспериментов по метрикам.
Если эксперимент показывает значимый результат — рекомендуешь его применить."""

    def evaluate_experiments(self, experiments: list[dict], metrics: dict) -> list[dict]:
        """Evaluate each experiment and return recommendations."""
        results = []
        for exp in experiments:
            if exp.get('status') not in ('proposed', 'running'):
                continue
            prompt = f"""Эксперимент: {exp.get('hypothesis')}
Метрика: {exp.get('metric')}
Ожидаемый результат: {exp.get('expected_lift')}
Текущие метрики: {json.dumps(metrics, ensure_ascii=False, default=str)}

Стоит ли применить изменение? Отвечай JSON: {{"recommendation": "apply|skip|continue", "reason": "..."}}"""
            rec = self.think_json(prompt)
            if isinstance(rec, dict):
                exp['recommendation'] = rec.get('recommendation', 'continue')
                exp['eval_reason'] = rec.get('reason', '')
            results.append(exp)
        return results

SCALE_THRESHOLD = 5.0   # conversion > 5% → SCALE
KILL_THRESHOLD  = 2.0   # conversion < 2% → KILL


class ExperimentSystem(FactoryAgent):
    name = "experiment_system"
    system_prompt = """Ты — Experiment System AI. Управляешь A/B тестами.

ПРАВИЛА:
- conversion > 5% → SCALE (масштабировать вариант B)
- conversion < 2% → KILL (вернуть вариант A или закрыть)
- иначе → ITERATE (улучшить вариант B)

При оценке учитывай:
- Статистическую значимость (минимум 100 конверсий)
- Продолжительность теста (минимум 7 дней)
- Побочные эффекты на другие метрики

Отвечай на русском. Решения должны быть обоснованы данными."""

    def create_experiment(
        self,
        product_id: int,
        name: str,
        hypothesis: str,
        variant_a: str,
        variant_b: str,
        traffic_split: float = 0.5,
    ) -> int:
        """Create a new A/B experiment."""
        exp_id = db.insert("experiments", {
            "product_id": product_id,
            "name": name,
            "hypothesis": hypothesis,
            "variant_a": variant_a,
            "variant_b": variant_b,
            "status": "running",
            "traffic_split": traffic_split,
            "started_at": datetime.now(timezone.utc).isoformat(),
        })
        logger.info("[Experiment] Created: %s (id=%d)", name, exp_id)
        return exp_id

    def auto_create_for_product(self, product_id: int, growth_action: dict | None = None) -> int | None:
        """Automatically create an A/B experiment for a product."""
        product = db.fetch_one("SELECT * FROM products WHERE id=?", (product_id,))
        if not product:
            return None

        context = {
            "product": product,
            "growth_action": growth_action,
            "metrics": db.get_product_metrics(product_id, limit=10),
        }

        spec = self.think_json(
            "Создай спецификацию A/B теста для продукта. Верни JSON:\n"
            "{\n"
            '  "name": "название теста",\n'
            '  "hypothesis": "гипотеза что именно тестируем и почему",\n'
            '  "variant_a": "контрольный вариант (текущий)",\n'
            '  "variant_b": "тестируемый вариант (улучшение)",\n'
            '  "traffic_split": 0.5,\n'
            '  "duration_days": 14\n'
            "}",
            context=context,
            max_tokens=800,
        )

        if not isinstance(spec, dict) or not spec.get("name"):
            return None

        return self.create_experiment(
            product_id=product_id,
            name=spec["name"],
            hypothesis=spec.get("hypothesis", ""),
            variant_a=spec.get("variant_a", "текущий вариант"),
            variant_b=spec.get("variant_b", "тестируемый вариант"),
            traffic_split=float(spec.get("traffic_split", 0.5)),
        )

    def evaluate_experiments(self) -> list[dict]:
        """Evaluate all running experiments and conclude if needed."""
        experiments = db.get_running_experiments()
        results = []

        for exp in experiments:
            conv_a = exp.get("conversion_a", 0) or 0
            conv_b = exp.get("conversion_b", 0) or 0

            # Rule-based decision
            if conv_b >= SCALE_THRESHOLD:
                result = "scale"
                note = f"Вариант B конвертирует {conv_b}% > {SCALE_THRESHOLD}% — масштабируем"
            elif conv_b <= KILL_THRESHOLD and conv_b < conv_a:
                result = "kill"
                note = f"Вариант B конвертирует {conv_b}% < {KILL_THRESHOLD}% — закрываем"
            elif conv_a > 0 or conv_b > 0:
                result = "iterate"
                note = f"Конверсия A={conv_a}% B={conv_b}% — итерируем"
            else:
                # No data yet — skip
                continue

            # Update experiment
            db.execute(
                "UPDATE experiments SET status='concluded', result=?, concluded_at=?, notes=? WHERE id=?",
                (result, datetime.now(timezone.utc).isoformat(), note, exp["id"]),
            )

            # Update product status based on result
            if exp.get("product_id"):
                if result == "scale":
                    db.execute("UPDATE products SET status='scaled', updated_at=? WHERE id=?",
                               (datetime.now(timezone.utc).isoformat(), exp["product_id"]))
                elif result == "kill":
                    db.execute("UPDATE products SET status='killed', updated_at=? WHERE id=?",
                               (datetime.now(timezone.utc).isoformat(), exp["product_id"]))

            results.append({
                "experiment_id": exp["id"],
                "name": exp["name"],
                "result": result,
                "conversion_a": conv_a,
                "conversion_b": conv_b,
                "note": note,
            })
            logger.info("[Experiment] %s → %s (A=%.1f%% B=%.1f%%)", exp["name"], result, conv_a, conv_b)

        return results

    def _rule_based_eval(self, exp: dict) -> str | None:
        """Apply simple rules before calling AI for edge cases."""
        conv_a = float(exp.get("conversion_a") or 0)
        conv_b = float(exp.get("conversion_b") or 0)

        if conv_b > SCALE_THRESHOLD:
            return "scale"
        if conv_b < KILL_THRESHOLD and conv_a >= conv_b:
            return "kill"

        # Check duration: if running > 14 days with no data, kill
        start = exp.get("start_date") or exp.get("created_at") or exp.get("started_at")
        if start:
            try:
                start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
                days = (datetime.now(timezone.utc) - start_dt).days
                if days > 14 and conv_b == 0:
                    return "kill"
            except Exception:
                pass

        return None  # Use AI

    def generate_experiment_report(self) -> dict:
        """Generate a summary of all experiments."""
        all_exps = db.fetch_all(
            "SELECT * FROM experiments ORDER BY created_at DESC LIMIT 20"
        )

        running = [e for e in all_exps if e.get("status") == "running"]
        concluded = [e for e in all_exps if e.get("status") == "concluded"]

        wins = [e for e in concluded if e.get("result") in ("scale", "apply")]
        losses = [e for e in concluded if e.get("result") in ("kill", "reject")]

        return {
            "total_experiments": len(all_exps),
            "running": len(running),
            "concluded": len(concluded),
            "win_rate": round(len(wins) / len(concluded) * 100, 1) if concluded else 0,
            "wins": [{"name": e.get("name"), "result": e.get("result")} for e in wins[:3]],
            "losses": [{"name": e.get("name"), "result": e.get("result")} for e in losses[:3]],
            "running_experiments": [
                {"name": e.get("name"), "metric": e.get("metric"), "started": e.get("created_at")}
                for e in running[:5]
            ],
        }

    def apply_experiment(self, exp_id: int) -> bool:
        """Mark a winning experiment as applied and log the action."""
        try:
            db.run(
                "UPDATE experiments SET status='applied', applied_at=?, notes=COALESCE(notes,'')||? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(),
                 f"\n[AUTO-APPLIED at {datetime.now(timezone.utc).strftime('%Y-%m-%d')}]",
                 exp_id)
            )
            growth_action = {
                "action_type": "apply_experiment",
                "description": f"Applied winning experiment #{exp_id}",
                "status": "pending",
                "priority": 8,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            db.insert("growth_actions", growth_action)
            logger.info("[ExperimentSystem] Applied experiment %d", exp_id)
            return True
        except Exception as e:
            logger.error("[ExperimentSystem] apply_experiment error: %s", e)
            return False

    def update_conversion(self, experiment_id: int, variant: str, conversion: float) -> None:
        """Update conversion rate for an experiment variant."""
        field = "conversion_a" if variant == "a" else "conversion_b"
        db.execute(f"UPDATE experiments SET {field}=? WHERE id=?", (conversion, experiment_id))
