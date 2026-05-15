"""👥 HR / Model Department — отбор, оценка портфолио, ранжирование моделей."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

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
