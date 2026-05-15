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


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 28: Heuristic A/B Experiment System (no DB required)
# ══════════════════════════════════════════════════════════════════════════════

import json as _json
import os as _os
from datetime import datetime as _datetime, timedelta as _timedelta
from typing import Any


EXPERIMENT_TEMPLATES = [
    {
        'id': 'catalog_sort_featured_first',
        'name': 'Сортировка каталога: featured первыми',
        'hypothesis': 'Показ топ-моделей первыми увеличит конверсию на 10%',
        'metric': 'booking_conversion',
        'variants': ['control', 'featured_first'],
        'duration_days': 14,
    },
    {
        'id': 'quick_booking_button',
        'name': 'Кнопка быстрой заявки в главном меню',
        'hypothesis': 'Быстрая заявка увеличит кол-во заявок на 20%',
        'metric': 'orders_count',
        'variants': ['control', 'quick_button'],
        'duration_days': 7,
    },
    {
        'id': 'discount_banner',
        'name': 'Баннер скидки 10% на первый заказ',
        'hypothesis': 'Скидка для новых клиентов увеличит конверсию на 15%',
        'metric': 'new_client_orders',
        'variants': ['control', 'discount_10'],
        'duration_days': 21,
    },
    {
        'id': 'review_prompt_after_booking',
        'name': 'Запрос отзыва сразу после завершения',
        'hypothesis': 'Ранний запрос отзыва увеличит кол-во отзывов на 30%',
        'metric': 'reviews_count',
        'variants': ['control', 'early_prompt'],
        'duration_days': 14,
    },
    {
        'id': 'photo_watermark',
        'name': 'Водяной знак на фото модели',
        'hypothesis': 'Водяной знак повысит доверие и снизит отказы на 5%',
        'metric': 'bounce_rate',
        'variants': ['control', 'watermark'],
        'duration_days': 21,
    },
]


class HeuristicExperimentSystem:
    """Proposes, runs, and evaluates A/B experiments heuristically (no DB required)."""

    def __init__(self, history_path: str | None = None) -> None:
        self.history_path = history_path or '/tmp/experiment_history.json'
        self._history: list[dict] = self._load_history()

    def _load_history(self) -> list[dict]:
        if _os.path.exists(self.history_path):
            try:
                with open(self.history_path) as f:
                    data = _json.load(f)
                    return list(data) if isinstance(data, list) else []
            except Exception:
                pass
        return []

    def _save_history(self) -> None:
        try:
            dir_path = _os.path.dirname(self.history_path)
            if dir_path:
                _os.makedirs(dir_path, exist_ok=True)
            with open(self.history_path, 'w') as f:
                _json.dump(self._history[-50:], f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def propose_experiments(self) -> list[dict[str, Any]]:
        """Return experiments not yet run or completed."""
        run_ids = {h['experiment_id'] for h in self._history if h.get('status') in ('running', 'completed')}
        return [e for e in EXPERIMENT_TEMPLATES if e['id'] not in run_ids]

    def start_experiment(self, experiment_id: str) -> dict[str, Any]:
        """Start an experiment by ID."""
        tmpl = next((e for e in EXPERIMENT_TEMPLATES if e['id'] == experiment_id), None)
        if not tmpl:
            return {'status': 'error', 'error': f'Unknown experiment: {experiment_id}'}

        record = {
            'experiment_id': experiment_id,
            'name': tmpl['name'],
            'hypothesis': tmpl['hypothesis'],
            'metric': tmpl['metric'],
            'variants': tmpl['variants'],
            'status': 'running',
            'started_at': _datetime.utcnow().isoformat(),
            'ends_at': (_datetime.utcnow() + _timedelta(days=int(str(tmpl['duration_days'])))).isoformat(),
            'results': {},
        }
        self._history.append(record)
        self._save_history()
        return {'status': 'started', 'experiment': record}

    def evaluate_experiment(self, experiment_id: str, metrics: dict[str, float]) -> dict[str, Any]:
        """Evaluate results of a running experiment."""
        record = next((h for h in self._history if h['experiment_id'] == experiment_id), None)
        if not record:
            return {'status': 'error', 'error': 'Experiment not found'}

        control = metrics.get('control', 0)
        variant = metrics.get(record['variants'][-1], 0)

        if control <= 0:
            improvement = 0.0
        else:
            improvement = round((variant - control) / control * 100, 1)

        winner = 'variant' if improvement > 5 else 'control' if improvement < -2 else 'inconclusive'

        record['results'] = {
            'control': control,
            'variant': variant,
            'improvement_pct': improvement,
            'winner': winner,
        }
        record['status'] = 'completed'
        self._save_history()

        return {'status': 'evaluated', 'winner': winner, 'improvement_pct': improvement}

    def get_active_experiments(self) -> list[dict]:
        return [h for h in self._history if h.get('status') == 'running']

    def get_completed_experiments(self) -> list[dict]:
        return [h for h in self._history if h.get('status') == 'completed']

    def generate_report(self) -> str:
        """Generate a human-readable experiment report."""
        active = self.get_active_experiments()
        completed = self.get_completed_experiments()
        proposed = self.propose_experiments()

        lines = ['🧪 ОТЧЁТ ПО ЭКСПЕРИМЕНТАМ', '']

        if active:
            lines.append(f'🔄 Активных: {len(active)}')
            for e in active:
                lines.append(f'  • {e["name"]} (до {e["ends_at"][:10]})')

        if completed:
            lines.append(f'\n✅ Завершённых: {len(completed)}')
            for e in completed:
                r = e.get('results', {})
                lines.append(f'  • {e["name"]}: {r.get("winner", "?")} ({r.get("improvement_pct", 0):+.1f}%)')

        if proposed:
            lines.append(f'\n💡 Предложено к запуску: {len(proposed)}')
            for e in proposed[:3]:
                lines.append(f'  • {e["name"]}')

        return '\n'.join(lines)

    def run_cycle(self) -> dict[str, Any]:
        """Run one cycle: auto-propose and start one new experiment if none active."""
        active = self.get_active_experiments()
        proposed = self.propose_experiments()
        started = None

        if not active and proposed:
            result = self.start_experiment(proposed[0]['id'])
            started = result.get('experiment', {}).get('name')

        return {
            'status': 'ok',
            'active_count': len(self.get_active_experiments()),
            'completed_count': len(self.get_completed_experiments()),
            'proposed_count': len(proposed),
            'started_experiment': started,
            'report': self.generate_report(),
        }


# ══════════════════════════════════════════════════════════════════════════════
# БЛОК 5.3 — CEO Intelligence: experiment proposals + delegation (no DB)
# ══════════════════════════════════════════════════════════════════════════════

import pathlib as _pathlib
import datetime as _dt_mod

_EXPERIMENTS_DB_PATH = _pathlib.Path(__file__).parent.parent / "experiments.json"

CEO_EXPERIMENT_IDEAS = [
    {
        "id": "exp_001",
        "name": "Booking form length",
        "hypothesis": "Shorter booking form (3 fields) increases conversion by 20%",
        "metric": "booking_completion_rate",
        "variants": ["A: 6 fields (current)", "B: 3 fields (name, phone, date)"],
        "duration_days": 14,
    },
    {
        "id": "exp_002",
        "name": "Welcome message tone",
        "hypothesis": "Friendly emoji-rich greeting increases first booking rate",
        "metric": "first_booking_rate",
        "variants": ["A: formal greeting", "B: emoji-rich casual greeting"],
        "duration_days": 7,
    },
    {
        "id": "exp_003",
        "name": "Featured model placement",
        "hypothesis": "Showing top models first increases average budget by 15%",
        "metric": "average_budget",
        "variants": ["A: random order", "B: featured first"],
        "duration_days": 14,
    },
    {
        "id": "exp_004",
        "name": "Response time notification",
        "hypothesis": "Showing '1 hour response time' increases booking start rate",
        "metric": "booking_start_rate",
        "variants": ["A: no promise", "B: '⚡ Ответим за 1 час'"],
        "duration_days": 7,
    },
    {
        "id": "exp_005",
        "name": "Price display",
        "hypothesis": "Showing price range upfront reduces abandoned bookings",
        "metric": "booking_completion_rate",
        "variants": ["A: price revealed at end", "B: 'от 5000₽' shown in catalog"],
        "duration_days": 14,
    },
]


_CEO_EXP_STORE_PATH = _pathlib.Path(__file__).parent.parent / "ceo_experiments.json"


class CEOExperimentSystem:
    """CEO experiment proposals and tracking system (heuristic, no DB required)."""

    EXPERIMENT_IDEAS = CEO_EXPERIMENT_IDEAS

    # Thresholds matching StrategicCore / ExperimentSystem rules
    SCALE_THRESHOLD = 5.0   # improvement_pct > 5 → scale (variant wins)
    KILL_THRESHOLD  = -2.0  # improvement_pct < -2 → kill (control wins)

    def __init__(self, store_path: str | None = None) -> None:
        self._store_path = _pathlib.Path(store_path) if store_path else _CEO_EXP_STORE_PATH
        self._store: dict = self._load_store()

    # ── persistence ──────────────────────────────────────────────────

    def _load_store(self) -> dict:
        try:
            if self._store_path.exists():
                data = _json.loads(self._store_path.read_text(encoding='utf-8'))
                return dict(data) if isinstance(data, dict) else {"active": [], "results": [], "history": []}
        except Exception:
            pass
        return {"active": [], "results": [], "history": []}

    def _save_store(self) -> None:
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            self._store_path.write_text(
                _json.dumps(self._store, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
        except Exception:
            pass

    # ── core API ─────────────────────────────────────────────────────

    def propose_experiment(self, context: dict | None = None) -> dict:
        """Propose next experiment to run based on what is not yet active."""
        import random as _random
        active = self.get_active_experiments()
        active_ids = {e['id'] for e in active}
        available = [e for e in self.EXPERIMENT_IDEAS if e['id'] not in active_ids]

        if not available:
            return {"status": "all_running", "message": "All experiments already running"}

        chosen = _random.choice(available)
        now = _dt_mod.datetime.now()
        return {
            "status": "proposed",
            "experiment": chosen,
            "start_date": now.isoformat(),
            "end_date": (now + _dt_mod.timedelta(days=int(str(chosen['duration_days'])))).isoformat(),
        }

    def propose_hypothesis(self, context: dict | None = None) -> dict:
        """Propose a concrete A/B hypothesis based on context KPIs.

        Args:
            context: dict with optional keys: conversion_rate, avg_check,
                     orders_total, repeat_rate, top_problem.
        Returns:
            dict with keys: id, hypothesis, metric, variant_a, variant_b,
                            duration_days, expected_lift_pct, status.
        """
        ctx = context or {}
        conversion = ctx.get('conversion_rate', 0)
        orders = ctx.get('orders_total', 0)
        repeat = ctx.get('repeat_rate', 0)
        problem = ctx.get('top_problem', '')

        # Rule-based hypothesis selection
        if conversion < 2:
            idea = {
                "id": f"hyp_{_dt_mod.datetime.now().strftime('%Y%m%d_%H%M%S')}",
                "hypothesis": "Добавление кнопки 'Быстрая заявка' на главный экран увеличит конверсию",
                "metric": "booking_completion_rate",
                "variant_a": "Текущая форма заявки (6 полей)",
                "variant_b": "Сокращённая форма (имя + телефон + дата)",
                "duration_days": 14,
                "expected_lift_pct": 25,
                "status": "proposed",
            }
        elif orders < 5:
            idea = {
                "id": f"hyp_{_dt_mod.datetime.now().strftime('%Y%m%d_%H%M%S')}",
                "hypothesis": "Показ ответного времени '⚡ Ответим за 1 час' увеличит начало заявок",
                "metric": "booking_start_rate",
                "variant_a": "Без обещания ответа",
                "variant_b": "С баннером '⚡ Ответим за 1 час'",
                "duration_days": 7,
                "expected_lift_pct": 15,
                "status": "proposed",
            }
        elif repeat < 0.1:
            idea = {
                "id": f"hyp_{_dt_mod.datetime.now().strftime('%Y%m%d_%H%M%S')}",
                "hypothesis": "Email-напоминание через 30 дней повысит повторные заказы",
                "metric": "repeat_rate",
                "variant_a": "Без напоминания",
                "variant_b": "Email на 30-й день после заказа",
                "duration_days": 30,
                "expected_lift_pct": 12,
                "status": "proposed",
            }
        else:
            idea = {
                "id": f"hyp_{_dt_mod.datetime.now().strftime('%Y%m%d_%H%M%S')}",
                "hypothesis": "Показ цены 'от X₽' в каталоге моделей снизит отказы от бронирования",
                "metric": "booking_completion_rate",
                "variant_a": "Цена раскрывается в конце формы",
                "variant_b": "Цена 'от 5000₽' показана в карточке модели",
                "duration_days": 14,
                "expected_lift_pct": 8,
                "status": "proposed",
            }

        if problem:
            idea["context_problem"] = problem

        # Persist to store
        self._store.setdefault("history", []).append({
            "type": "proposed_hypothesis",
            "idea": idea,
            "timestamp": _dt_mod.datetime.now().isoformat(),
        })
        self._save_store()
        return idea

    def track_result(self, experiment_id: str, metric: str, value: float) -> dict:
        """Record a metric value for an experiment variant.

        Args:
            experiment_id: The experiment ID string.
            metric: Metric name, e.g. 'a_rate' or 'b_rate'.
            value: Observed metric value (e.g. conversion %).
        Returns:
            dict with experiment_id, metric, value, timestamp.
        """
        record = {
            "experiment_id": experiment_id,
            "metric": metric,
            "value": value,
            "timestamp": _dt_mod.datetime.now().isoformat(),
        }
        self._store.setdefault("results", []).append(record)
        self._save_store()
        return record

    def get_winning_variant(self, experiment_id: str) -> dict:
        """Determine winning variant for a given experiment.

        Looks up all tracked results for this experiment_id, computes
        improvement_pct, and returns scale/iterate/kill recommendation.

        Returns:
            dict with keys: experiment_id, winner (A|B|inconclusive),
                            improvement_pct, recommendation (scale|iterate|kill),
                            a_rate, b_rate.
        """
        results = [r for r in self._store.get("results", []) if r["experiment_id"] == experiment_id]
        a_vals = [r["value"] for r in results if r["metric"] == "a_rate"]
        b_vals = [r["value"] for r in results if r["metric"] == "b_rate"]

        a_rate = sum(a_vals) / len(a_vals) if a_vals else 0.0
        b_rate = sum(b_vals) / len(b_vals) if b_vals else 0.0

        if a_rate > 0:
            improvement_pct = round((b_rate - a_rate) / a_rate * 100, 1)
        else:
            improvement_pct = 0.0

        if improvement_pct > self.SCALE_THRESHOLD:
            winner = "B"
            recommendation = "scale"
        elif improvement_pct < self.KILL_THRESHOLD:
            winner = "A"
            recommendation = "kill"
        else:
            winner = "inconclusive"
            recommendation = "iterate"

        return {
            "experiment_id": experiment_id,
            "winner": winner,
            "improvement_pct": improvement_pct,
            "recommendation": recommendation,
            "a_rate": a_rate,
            "b_rate": b_rate,
            "samples": {"a": len(a_vals), "b": len(b_vals)},
        }

    def track_results(self, experiment_id: str, metrics: dict) -> dict:
        """Track experiment results and determine winner (legacy + persist).

        Args:
            experiment_id: Experiment ID string.
            metrics: dict with keys a_rate, b_rate (conversion percentages).
        Returns:
            dict with winner, improvement, recommendation.
        """
        a_rate = metrics.get("a_rate", 0)
        b_rate = metrics.get("b_rate", 0)

        # Persist individual readings
        if a_rate:
            self.track_result(experiment_id, "a_rate", a_rate)
        if b_rate:
            self.track_result(experiment_id, "b_rate", b_rate)

        winner_info = self.get_winning_variant(experiment_id)
        return {
            "experiment_id": experiment_id,
            "metrics": metrics,
            "winner": winner_info["winner"],
            "improvement": abs(b_rate - a_rate),
            "improvement_pct": winner_info["improvement_pct"],
            "recommendation": winner_info["recommendation"],
            "timestamp": _dt_mod.datetime.now().isoformat(),
        }

    def get_active_experiments(self) -> list[dict]:
        """Load active experiments from JSON file."""
        self._store = self._load_store()  # refresh from disk
        return list(self._store.get('active', []))

    def generate_report(self, context: dict | None = None) -> str:
        """Generate experiment status report."""
        active = self.get_active_experiments()
        results = self._store.get("results", [])
        ideas = self.EXPERIMENT_IDEAS[:3]

        lines = ["📊 *ЭКСПЕРИМЕНТЫ*\n"]

        if active:
            lines.append(f"Активных: {len(active)}")
        else:
            lines.append("Активных экспериментов: 0")

        if results:
            exp_ids = list({r["experiment_id"] for r in results})
            lines.append(f"\n📈 Данные по {len(exp_ids)} экспериментам")

        lines.append("\n💡 Предложения:")
        for idea in ideas:
            lines.append(f"• {idea.get('name', '')}: {str(idea.get('hypothesis', ''))[:60]}...")

        return "\n".join(lines)


_CEO_DELEGATION_STORE_PATH = _pathlib.Path(__file__).parent.parent / "ceo_delegation.json"


class CEODelegation:
    """CEO delegation system — tracks focus departments per cycle with persistence."""

    DEPARTMENTS = [
        'marketing', 'sales', 'product', 'analytics',
        'operations', 'hr', 'tech', 'creative', 'finance',
    ]

    def __init__(self, store_path: str | None = None) -> None:
        self._store_path = _pathlib.Path(store_path) if store_path else _CEO_DELEGATION_STORE_PATH
        self._store: dict = self._load_store()
        self._current_focus: str | None = self._store.get("current_focus")

    # ── persistence ──────────────────────────────────────────────────

    def _load_store(self) -> dict:
        try:
            if self._store_path.exists():
                data = _json.loads(self._store_path.read_text(encoding='utf-8'))
                return dict(data) if isinstance(data, dict) else {"current_focus": None, "decisions_history": [], "task_outcomes": []}
        except Exception:
            pass
        return {"current_focus": None, "decisions_history": [], "task_outcomes": []}

    def _save_store(self) -> None:
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            self._store["current_focus"] = self._current_focus
            # Keep last 50 decisions to avoid unbounded growth
            self._store["decisions_history"] = self._store.get("decisions_history", [])[-50:]
            self._store_path.write_text(
                _json.dumps(self._store, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
        except Exception:
            pass

    # ── core API ─────────────────────────────────────────────────────

    def delegate_focus(self, kpis: dict | None = None) -> dict:
        """Decide which department to focus on next cycle based on KPIs."""
        import random as _random
        ctx = kpis or {}
        orders_total = ctx.get('orders_total', 0)
        conversion = ctx.get('conversion_rate', 0)
        health_score = ctx.get('health_score', 50)

        # Priority: fix the worst problem first
        if conversion < 0.3:
            focus = 'sales'
            reason = 'Конверсия критически низкая — фокус на продажах'
        elif orders_total < 10:
            focus = 'marketing'
            reason = 'Мало заявок — нужно усилить маркетинг'
        elif health_score < 40:
            focus = 'operations'
            reason = f'Health score {health_score} — фокус на операционной стабильности'
        else:
            # Rotate between growth departments, avoiding last focus
            last_focus = self._current_focus
            options = [d for d in ['product', 'analytics', 'creative', 'tech'] if d != last_focus]
            focus = _random.choice(options)
            reason = f'Бизнес стабилен — фокус на росте через {focus}'

        decision = {
            "focus_department": focus,
            "reason": reason,
            "cycle": _dt_mod.datetime.now().isoformat(),
            "kpis_snapshot": {
                "conversion_rate": conversion,
                "orders_total": orders_total,
                "health_score": health_score,
            },
            "priority_tasks": self._get_priority_tasks(focus),
            "outcome": None,  # filled in later via mark_outcome()
        }
        self._current_focus = focus
        self._store.setdefault("decisions_history", []).append(decision)
        self._save_store()
        return decision

    def mark_outcome(self, cycle_ts: str, outcome: str, notes: str = "") -> bool:
        """Mark the outcome of a previous delegation decision.

        Args:
            cycle_ts: The 'cycle' timestamp string from the delegation record.
            outcome: 'done', 'partial', or 'missed'.
            notes: Optional explanation of what was/wasn't done.
        Returns:
            True if record was found and updated.
        """
        for dec in self._store.get("decisions_history", []):
            if dec.get("cycle") == cycle_ts:
                dec["outcome"] = outcome
                dec["outcome_notes"] = notes
                dec["outcome_at"] = _dt_mod.datetime.now().isoformat()
                self._save_store()
                return True
        return False

    def _get_priority_tasks(self, department: str) -> list[str]:
        tasks = {
            'sales': ['Improve follow-up messages', 'Reduce response time', 'Add pricing info'],
            'marketing': ['Post to Telegram channel', 'Update model descriptions', 'SEO improvements'],
            'product': ['Improve booking UX', 'Add wishlist feature', 'Better search'],
            'analytics': ['Track conversion funnel', 'Cohort analysis', 'Revenue forecasting'],
            'creative': ['New post templates', 'Model description upgrades', 'FAQ refresh'],
            'finance': ['Budget planning', 'Revenue forecast', 'Cost analysis'],
            'operations': ['Reduce response time', 'Improve scheduling', 'Quality control'],
            'hr': ['Model ranking', 'Performance evaluation', 'Talent scouting'],
            'tech': ['Performance optimization', 'Security audit', 'API improvements'],
        }
        return tasks.get(department, ['General improvements'])

    def get_focus_report(self) -> str:
        """Return a human-readable focus report."""
        if not self._current_focus:
            return "Фокус не установлен"
        history = self._store.get("decisions_history", [])
        last = history[-1] if history else {}
        tasks_str = "\n".join(f"  • {t}" for t in last.get("priority_tasks", []))
        return (
            f"🎯 Текущий фокус: {self._current_focus}\n"
            f"📋 Задачи:\n{tasks_str}"
        )

    def check_previous_decisions(self) -> dict:
        """Compute real fulfillment rate from persisted decision outcomes.

        Returns:
            dict with total_decisions, done, partial, missed,
                  fulfillment_rate (0.0–1.0), summary.
        """
        history = self._store.get("decisions_history", [])
        total = len(history)
        done = sum(1 for d in history if d.get("outcome") == "done")
        partial = sum(1 for d in history if d.get("outcome") == "partial")
        missed = sum(1 for d in history if d.get("outcome") == "missed")
        untracked = total - done - partial - missed

        # Weighted fulfillment: done=1.0, partial=0.5, missed/untracked=0.0
        if total > 0:
            fulfillment_rate = round((done + 0.5 * partial) / total, 2)
        else:
            fulfillment_rate = 0.0

        return {
            "total_decisions": total,
            "done": done,
            "partial": partial,
            "missed": missed,
            "untracked": untracked,
            "fulfillment_rate": fulfillment_rate,
            "summary": (
                f"Принято решений: {total}. "
                f"Выполнено: {done}, частично: {partial}, "
                f"пропущено: {missed}, без статуса: {untracked}. "
                f"Выполнение: {int(fulfillment_rate * 100)}%"
            ),
        }
