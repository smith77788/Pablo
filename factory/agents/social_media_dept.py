"""Social Media Department — Instagram & Telegram content factory."""

from __future__ import annotations
from datetime import datetime, timedelta
from factory.agents.base import FactoryAgent


class InstagramContentAgent(FactoryAgent):
    """Generates Instagram content: captions, hashtags, story scripts."""

    department = "social_media"
    role = "instagram_content"

    def generate_model_spotlight(self, model: dict) -> dict:
        params = []
        if model.get("height"):
            params.append(f"рост {model['height']} см")
        if model.get("age"):
            params.append(f"{model['age']} лет")
        if model.get("category"):
            cats = {"fashion": "фэшн", "commercial": "коммерческая", "events": "события"}
            params.append(cats.get(model["category"], model["category"]))

        name = model.get("name", "Модель")
        city = model.get("city", "")
        param_str = ", ".join(params)
        city_tag = f"#{city.replace(' ', '')}" if city else ""

        caption = (
            f"✨ Представляем {name}\n\n"
            f"{'🔹 ' + param_str + chr(10) if param_str else ''}"
            f"{'📍 ' + city + chr(10) if city else ''}"
            f"\nДоступна для: фотосессий, мероприятий, рекламных съёмок\n\n"
            f"📩 Забронировать: ссылка в шапке профиля\n\n"
            f"#невестымодели #модель #агентствомоделей {city_tag} "
            f"#modelagency #fashionmodel #commercialmodel"
        )
        return {
            "type": "post",
            "caption": caption,
            "hashtags": "#невестымодели #модель #агентствомоделей #modelagency",
            "suggested_time": self._best_posting_time(),
        }

    def generate_agency_promo(self) -> dict:
        caption = (
            "💎 Nevesty Models — профессиональное агентство моделей\n\n"
            "✅ Фэшн-показы\n"
            "✅ Рекламные съёмки\n"
            "✅ Корпоративные мероприятия\n"
            "✅ Промо и BTL\n\n"
            "Более 50 моделей в каталоге\n"
            "Опыт работы с ведущими брендами\n\n"
            "📩 Оставьте заявку — ответим в течение 1 часа!\n\n"
            "#модели #агентство #nevesty #невесты #мероприятие #фотосессия"
        )
        return {
            "type": "post",
            "caption": caption,
            "hashtags": "#агентствомоделей #nevesty #невестымодели",
            "suggested_time": self._best_posting_time(),
        }

    def generate_week_content_plan(self, models: list[dict]) -> list[dict]:
        plan = []
        base_date = datetime.now()
        for i, model in enumerate(models[:7]):
            post_date = base_date + timedelta(days=i)
            post_time = post_date.replace(hour=18, minute=0, second=0)
            content = self.generate_model_spotlight(model)
            content["scheduled_at"] = post_time.isoformat()
            content["model_id"] = model.get("id")
            plan.append(content)
        return plan

    def _best_posting_time(self) -> str:
        now = datetime.now()
        target = now.replace(hour=18, minute=0, second=0)
        if target <= now:
            target += timedelta(days=1)
        return target.isoformat()

    def _heuristic_execute(self, task: dict) -> dict:
        action = task.get("action", "generate_promo")
        if action == "generate_model_spotlight":
            model = task.get("model", {"name": "Анна", "height": 174, "age": 24, "category": "fashion"})
            return self.generate_model_spotlight(model)
        elif action == "generate_week_plan":
            models = task.get("models", [])
            return {"week_plan": self.generate_week_content_plan(models)}
        else:
            return self.generate_agency_promo()

    def execute_task(self, task: dict) -> dict:
        try:
            return self._heuristic_execute(task)
        except Exception as e:
            return {"error": str(e), "agent": self.role}


class InstagramAnalyticsAgent(FactoryAgent):
    """Analyzes Instagram performance and recommends improvements."""

    department = "social_media"
    role = "instagram_analytics"

    def analyze_heuristic(self) -> dict:
        return {
            "best_posting_hours": [9, 12, 18, 20],
            "best_days": ["Вт", "Ср", "Пт"],
            "recommended_hashtags": [
                "#модель", "#невестымодели", "#агентствомоделей",
                "#fashionmodel", "#modellife", "#фотосессия",
            ],
            "content_recommendations": [
                "Stories с опросами увеличивают engagement на 20%",
                "Публикации с моделями в 'behind the scenes' получают +35% лайков",
                "Reels получают в 3x больше охвата чем обычные посты",
            ],
            "weekly_post_target": 5,
            "stories_per_day": 3,
        }

    def _heuristic_execute(self, task: dict) -> dict:
        return self.analyze_heuristic()

    def execute_task(self, task: dict) -> dict:
        try:
            return self._heuristic_execute(task)
        except Exception as e:
            return {"error": str(e), "agent": self.role}


class SocialMediaDepartment:
    """Facade for the Social Media department."""

    def __init__(self):
        self.content = InstagramContentAgent()
        self.analytics = InstagramAnalyticsAgent()

    def execute_task(self, task: dict) -> dict:
        results = {}
        action = task.get("action", "analyze")
        if action in ("generate_content", "generate_week_plan"):
            results["content"] = self.content.execute_task(task)
        results["analytics"] = self.analytics.execute_task(task)
        return results
