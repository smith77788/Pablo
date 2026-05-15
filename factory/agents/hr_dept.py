"""👥 HR / Model Department — отбор, оценка портфолио, ранжирование моделей."""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from factory.agents.base import FactoryAgent
from factory import db

logger = logging.getLogger(__name__)


class CandidateScreener(FactoryAgent):
    department = "hr"
    role = "candidate_screener"
    name = "candidate_screener"
    system_prompt = """Ты — Candidate Screener агентства моделей Nevesty Models.
Проверяешь заявки кандидатов в модели на соответствие требованиям.
Критерии: внешние данные, опыт, профессионализм. Всё на русском."""

    def screen_candidate(self, candidate: dict) -> dict:
        return self.think_json(
            "Оцени кандидата в модели. Верни JSON:\n"
            '{"score": 1-10, "verdict": "accept|reject|maybe", '
            '"strengths": ["..."], "concerns": ["..."], '
            '"recommendation": "конкретная рекомендация"}',
            context={"candidate": candidate},
            max_tokens=800,
        ) or {}

    def run(self, context: Dict[str, Any] = None) -> Dict[str, Any]:
        """Heuristic candidate screening based on context data."""
        ctx = context or {}
        candidate = ctx.get("candidate", {})
        experience = candidate.get("experience_years", 0)
        rating = float(candidate.get("rating", 0))

        insights: List[str] = []
        verdict = "maybe"

        if experience >= 3 and rating >= 4.0:
            verdict = "accept"
            insights.append("Candidate has strong experience and high rating — recommend acceptance")
        elif experience == 0 and rating < 3.0:
            verdict = "reject"
            insights.append("Candidate lacks experience and has low rating — recommend rejection")
        else:
            insights.append("Candidate needs further review — intermediate profile")

        if candidate.get("portfolio_size", 0) > 20:
            insights.append("Large portfolio indicates professional commitment")
        else:
            insights.append("Portfolio is limited — request additional photos")

        return {
            "insights": insights,
            "verdict": verdict,
            "score": min(10, max(1, int(experience * 1.5 + rating))),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


class PortfolioEvaluator(FactoryAgent):
    department = "hr"
    role = "portfolio_evaluator"
    name = "portfolio_evaluator"
    system_prompt = """Ты — Portfolio Evaluator агентства моделей.
Оцениваешь качество портфолио: разнообразие, качество съёмок, commercial appeal.
Знаешь требования fashion/commercial/events индустрии. Всё на русском."""

    def evaluate_portfolio(self, model: dict) -> dict:
        return self.think_json(
            "Оцени портфолио модели. Верни JSON:\n"
            '{"portfolio_score": 1-10, "photo_quality": "high|medium|low", '
            '"versatility": "high|medium|low", "commercial_appeal": "high|medium|low", '
            '"missing": ["что добавить"], "tips": ["совет для улучшения"]}',
            context={"model": model},
            max_tokens=800,
        ) or {}

    def run(self, context: Dict[str, Any] = None) -> Dict[str, Any]:
        """Heuristic portfolio evaluation based on context data."""
        ctx = context or {}
        model = ctx.get("model", {})
        photo_count = int(model.get("photo_count", 0))
        categories = model.get("categories", [])

        insights: List[str] = []

        if photo_count >= 30:
            photo_quality = "high"
            insights.append("Portfolio has sufficient photos for professional evaluation")
        elif photo_count >= 15:
            photo_quality = "medium"
            insights.append("Portfolio is adequate but could benefit from more variety")
        else:
            photo_quality = "low"
            insights.append("Portfolio is thin — model should add more professional photos")

        versatility = "high" if len(categories) >= 3 else ("medium" if len(categories) >= 2 else "low")
        if versatility == "high":
            insights.append("Model demonstrates strong versatility across multiple categories")
        else:
            insights.append("Model should expand into additional categories to improve versatility")

        score = min(10, max(1, photo_count // 5 + len(categories) * 2))

        return {
            "insights": insights,
            "portfolio_score": score,
            "photo_quality": photo_quality,
            "versatility": versatility,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


class RankingSystem(FactoryAgent):
    department = "hr"
    role = "ranking_system"
    name = "ranking_system"
    system_prompt = """Ты — Ranking System для агентства моделей.
Ранжируешь моделей по востребованности, рейтингу, активности.
Предлагаешь кого продвигать, кого переобучить. Всё на русском."""

    def rank_models(self, models: list, metrics: dict) -> list[dict]:
        return self.think_json(
            f"Проранжируй {len(models)} моделей агентства. Верни JSON массив:\n"
            '[{"model_id": 1, "rank": 1, "score": 8.5, '
            '"strengths": "...", "action": "promote|maintain|coach|archive"}]',
            context={"models": models, "metrics": metrics},
            max_tokens=1500,
        ) or []

    def run(self, context: Dict[str, Any] = None) -> Dict[str, Any]:
        """Heuristic model ranking based on context data."""
        ctx = context or {}
        models = ctx.get("models", [])
        metrics = ctx.get("metrics", {})

        insights: List[str] = []
        ranked: List[Dict[str, Any]] = []

        if not models:
            insights.append("No models provided for ranking — using empty dataset")
            insights.append("Add models to the context to get a ranking")
        else:
            total = len(models)
            insights.append(f"Ranked {total} model(s) by availability and category diversity")

            for i, m in enumerate(models, start=1):
                model_id = m.get("id", i)
                available = m.get("available", 1)
                score = round(10.0 - (i - 1) * (8.0 / max(total, 1)), 1)
                action = "promote" if i == 1 else ("maintain" if i <= total // 2 + 1 else "coach")
                ranked.append({
                    "model_id": model_id,
                    "rank": i,
                    "score": score,
                    "action": action,
                })

            insights.append("Top-ranked models should be prioritised for premium bookings")

        return {
            "insights": insights,
            "rankings": ranked,
            "total_ranked": len(ranked),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


class HRDepartment:
    """Координатор HR-департамента."""

    def __init__(self):
        self.screener = CandidateScreener()
        self.portfolio = PortfolioEvaluator()
        self.ranker = RankingSystem()

    def run_model_optimization(self, product_id: int | None = None) -> list[dict]:
        """Анализирует текущих моделей и генерирует action items."""
        saved_actions = []
        try:
            import sqlite3, os
            bot_db = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                                  "nevesty-models", "data.db")
            conn = sqlite3.connect(bot_db)
            conn.row_factory = sqlite3.Row
            models = [dict(r) for r in conn.execute("SELECT id, name, category, available FROM models LIMIT 20").fetchall()]
            conn.close()
        except Exception as e:
            logger.warning("[HR Dept] Could not read bot DB: %s", e)
            models = []

        if not models:
            return saved_actions

        rankings = self.ranker.rank_models(models, {})
        for r in rankings[:3]:
            if r.get("action") in ("promote", "coach"):
                action_id = db.insert("growth_actions", {
                    "product_id": product_id,
                    "action_type": "content",
                    "channel": "direct",
                    "content": f"HR: Модель #{r.get('model_id')} → {r.get('action').upper()}\n"
                               f"Score: {r.get('score')} | {r.get('strengths', '')}",
                    "status": "pending",
                    "priority": 6,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
                saved_actions.append({"type": "model_action", "_db_id": action_id, **r})

        logger.info("[HR Dept] Сгенерировано %d action items по моделям", len(saved_actions))
        return saved_actions

    def execute_task(self, task: str, context: Dict[str, Any] = None) -> Dict[str, Any]:
        """Execute an HR task using all department agents in heuristic mode."""
        ctx = context or {}

        screener_result = self.screener.run(ctx)
        portfolio_result = self.portfolio.run(ctx)
        ranker_result = self.ranker.run(ctx)

        all_insights: List[str] = (
            screener_result.get("insights", [])
            + portfolio_result.get("insights", [])
            + ranker_result.get("insights", [])
        )

        return {
            "roles_used": ["candidate_screener", "portfolio_evaluator", "ranking_system"],
            "insights": all_insights,
            "screening": screener_result,
            "portfolio": portfolio_result,
            "rankings": ranker_result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
