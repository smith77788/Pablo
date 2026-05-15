"""Model Description Agent — generates compelling model bios from parameters."""
from __future__ import annotations
import json
import logging
import sqlite3
from pathlib import Path

from factory.agents.base import FactoryAgent

logger = logging.getLogger(__name__)

NEVESTY_DB = Path(__file__).parent.parent.parent / "nevesty-models" / "data.db"


class ModelDescriptionAgent(FactoryAgent):
    name = "model_description_agent"
    department = "creative"
    role = "ModelDescriptionWriter"
    system_prompt = """Ты — профессиональный копирайтер модельного агентства.
Создаёшь привлекательные, уникальные описания моделей.
Стиль: профессиональный, тёплый, подчёркивающий сильные стороны.
Пишешь на русском языке. Описание должно быть кратким (100-200 слов) и запоминающимся."""

    def generate_bio(self, model: dict) -> str:
        """Generate a compelling bio for a model based on their parameters."""
        params = {
            "name": model.get("name", ""),
            "age": model.get("age"),
            "height": model.get("height"),
            "city": model.get("city", ""),
            "category": model.get("category", ""),
            "hair_color": model.get("hair_color", ""),
            "eye_color": model.get("eye_color", ""),
            "order_count": model.get("order_count", 0),
        }

        prompt = f"""Создай профессиональное описание для модели. Параметры:
{json.dumps(params, ensure_ascii=False)}

Напиши описание (100-200 слов) в 3 абзацах:
1. Вступление: кто она и что делает (включи параметры органично)
2. Специализация и сильные стороны
3. Чем может помочь клиентам

Не используй клише типа "прекрасная", "красивая". Будь конкретным.
Верни ТОЛЬКО текст описания, без JSON."""

        return self.think(prompt, max_tokens=512)

    def generate_missing_bios(self, limit: int = 5) -> list:
        """Find models without bio and generate descriptions for them."""
        if not NEVESTY_DB.exists():
            return []

        results = []
        try:
            conn = sqlite3.connect(str(NEVESTY_DB))
            conn.row_factory = sqlite3.Row
            models = conn.execute(
                "SELECT id, name, age, height, city, category, hair_color, eye_color, "
                "(SELECT COUNT(*) FROM orders WHERE model_id=models.id) as order_count "
                "FROM models WHERE (bio IS NULL OR bio = '') AND available=1 LIMIT ?",
                (limit,)
            ).fetchall()
            conn.close()

            for m in models:
                model_dict = dict(m)
                bio = self.generate_bio(model_dict)
                if bio:
                    results.append({
                        "model_id": model_dict["id"],
                        "model_name": model_dict["name"],
                        "generated_bio": bio,
                    })
        except Exception as e:
            logger.error("DB error: %s", e)

        return results
