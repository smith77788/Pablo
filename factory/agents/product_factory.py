"""📦 Product Factory — генерирует MVP-структуры и бизнес-идеи."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from factory.agents.base import FactoryAgent
from factory import db

logger = logging.getLogger(__name__)


class ProductFactory(FactoryAgent):
    name = "product_factory"
    system_prompt = """Ты — Product Factory AI. Генерируешь бизнес-идеи и MVP-структуры.

ФОРМАТ MVP:
- Чёткое описание продукта
- Целевая аудитория
- 3-5 ключевых функций
- Модель монетизации
- Метрики успеха
- Конкурентные преимущества

КОНТЕКСТ: Работаем в нише модельного бизнеса (модельное агентство Nevesty Models).
Все идеи должны быть связаны с основным бизнесом или усиливать его.

Отвечай только на русском. Будь конкретным."""

    def generate_ideas(self, count: int = 5, context: dict | None = None) -> list[dict]:
        """Generate new product ideas."""
        existing = db.fetch_all("SELECT title FROM ideas ORDER BY created_at DESC LIMIT 20")
        existing_titles = [i["title"] for i in existing]

        ideas_raw = self.think_json(
            f"Сгенерируй {count} новых бизнес-идей для развития агентства моделей. "
            f"Уже есть идеи: {existing_titles[:5]}. Придумай новые.\n"
            "Верни JSON массив:\n"
            "[\n"
            "  {\n"
            '    "title": "название идеи",\n'
            '    "description": "описание в 2-3 предложениях",\n'
            '    "category": "saas|marketplace|service|content|automation",\n'
            '    "priority": 1-10,\n'
            '    "rationale": "почему это нужно бизнесу",\n'
            '    "estimated_effort": "low|medium|high",\n'
            '    "estimated_impact": "low|medium|high"\n'
            "  }\n"
            "]",
            context=context,
            max_tokens=2000,
        )

        if not isinstance(ideas_raw, list):
            return []

        saved = []
        for idea in ideas_raw[:count]:
            if not isinstance(idea, dict) or not idea.get("title"):
                continue
            idea_id = db.insert("ideas", {
                "title": idea["title"],
                "description": idea.get("description", ""),
                "category": idea.get("category", ""),
                "priority": int(idea.get("priority", 5)),
                "status": "new",
                "rationale": idea.get("rationale", ""),
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            idea["_db_id"] = idea_id
            saved.append(idea)
            logger.info("[Product Factory] Idea: %s (priority=%s)", idea["title"], idea.get("priority"))

        return saved

    def create_mvp(self, idea_id: int | None = None, decision: dict | None = None) -> dict:
        """Create a full MVP specification from an idea or decision."""
        idea = None
        if idea_id:
            idea = db.fetch_one("SELECT * FROM ideas WHERE id=?", (idea_id,))

        context = {
            "idea": idea,
            "decision": decision,
            "existing_products": db.fetch_all("SELECT name, category, status FROM products"),
        }

        mvp = self.think_json(
            "Создай полную спецификацию MVP. Верни JSON:\n"
            "{\n"
            '  "name": "название продукта",\n'
            '  "description": "полное описание",\n'
            '  "category": "saas|marketplace|service|content|automation",\n'
            '  "target_audience": "целевая аудитория",\n'
            '  "core_features": ["фича1", "фича2", "фича3"],\n'
            '  "landing_page": {\n'
            '    "headline": "заголовок",\n'
            '    "subheadline": "подзаголовок",\n'
            '    "cta": "кнопка действия",\n'
            '    "sections": ["section1", "section2"]\n'
            "  },\n"
            '  "api_endpoints": ["GET /products", "POST /orders"],\n'
            '  "monetization": "описание монетизации",\n'
            '  "pricing": "цена/тариф",\n'
            '  "success_metrics": {\n'
            '    "conversion_target": 5.0,\n'
            '    "revenue_target": 100000,\n'
            '    "users_target": 1000\n'
            "  },\n"
            '  "competitive_advantage": "в чём уникальность",\n'
            '  "go_to_market": "стратегия выхода на рынок"\n'
            "}",
            context=context,
            max_tokens=2500,
        )

        if not isinstance(mvp, dict) or not mvp.get("name"):
            mvp = {"name": "MVP (не удалось сгенерировать)", "description": "Ошибка генерации"}
            return mvp

        # Save to DB
        product_id = db.insert("products", {
            "name": mvp["name"],
            "description": mvp.get("description", ""),
            "status": "active",
            "source": "factory",
            "category": mvp.get("category", ""),
            "monetization": mvp.get("monetization", ""),
            "success_metrics": mvp.get("success_metrics", {}),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

        # Update idea status
        if idea_id:
            db.execute("UPDATE ideas SET status='building' WHERE id=?", (idea_id,))

        mvp["_product_id"] = product_id
        logger.info("[Product Factory] MVP created: %s (id=%d)", mvp["name"], product_id)
        return mvp
