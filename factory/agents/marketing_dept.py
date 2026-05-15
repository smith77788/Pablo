"""📣 Marketing Department — 5 ролей: контент, viral, SEO, реклама, growth hacks."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from factory.agents.base import FactoryAgent
from factory import db

logger = logging.getLogger(__name__)


class ContentStrategist(FactoryAgent):
    department = "marketing"
    role = "content_strategist"
    name = "content_strategist"
    system_prompt = """Ты — Content Strategist агентства моделей Nevesty Models.
Разрабатываешь контент-стратегию для B2B аудитории (организаторы мероприятий, рекламодатели).
Думаешь о темах, форматах, расписании. Всё на русском."""

    def create_content_plan(self, insights: dict, weeks: int = 2) -> list[dict]:
        return self.think_json(
            f"Создай контент-план на {weeks} недели для агентства моделей.\n"
            "Верни JSON массив постов:\n"
            '[{"day": "Пн", "platform": "instagram|telegram|tiktok", "topic": "...", '
            '"format": "пост|видео|stories|рассылка", "hook": "первая фраза", "cta": "призыв к действию"}]',
            context={"insights": insights},
            max_tokens=2000,
        ) or []


class ViralEngineer(FactoryAgent):
    department = "marketing"
    role = "viral_engineer"
    name = "viral_engineer"
    system_prompt = """Ты — Viral Engineer. Специализируешься на TikTok и Reels для агентства моделей.
Создаёшь сценарии вирусного контента. Знаешь тренды, хуки, форматы. Всё на русском."""

    def generate_video_scripts(self, count: int = 3, trend: str = "") -> list[dict]:
        return self.think_json(
            f"Придумай {count} сценария для TikTok/Reels для агентства моделей.\n"
            f"Тренд/тема: {trend or 'актуальное'}\n"
            "Верни JSON массив:\n"
            '[{"title": "...", "hook": "первые 3 секунды", "script": "сценарий", '
            '"hashtags": ["#tag1"], "music_style": "...", "duration_sec": 30}]',
            max_tokens=2500,
        ) or []


class SEOSpecialist(FactoryAgent):
    department = "marketing"
    role = "seo_specialist"
    name = "seo_specialist"
    system_prompt = """Ты — SEO Specialist для агентства моделей.
Оптимизируешь контент для поиска. Знаешь ключевые слова ниши модельного бизнеса.
Фокус: органический трафик из России. Всё на русском."""

    def generate_seo_cluster(self, main_keyword: str) -> dict:
        return self.think_json(
            f"Создай кластер контента для ключевого слова: '{main_keyword}'\n"
            "Верни JSON:\n"
            '{"main_keyword": "...", "volume": "примерный объём", '
            '"pillar_article": {"title": "...", "meta": "...", "h2s": ["..."]}, '
            '"cluster_articles": [{"title": "...", "keyword": "..."}], '
            '"internal_links": ["..."], "schema_type": "LocalBusiness|Service"}',
            max_tokens=1500,
        ) or {}


class AdCopywriter(FactoryAgent):
    department = "marketing"
    role = "ad_copywriter"
    name = "ad_copywriter"
    system_prompt = """Ты — Ad Copywriter для агентства моделей Nevesty Models.
Пишешь рекламные тексты для Яндекс.Директ, VK Ads, Telegram Ads.
Аудитория: B2B (организаторы мероприятий, бренды). Всё на русском."""

    def generate_ad_set(self, product: dict, goal: str = "заявки") -> list[dict]:
        return self.think_json(
            f"Создай набор рекламных объявлений. Цель: {goal}\n"
            "Верни JSON массив из 5 вариантов:\n"
            '[{"platform": "yandex|vk|telegram", "headline": "до 35 символов", '
            '"text": "до 80 символов", "cta": "кнопка", "usp": "уникальное преимущество"}]',
            context={"product": product},
            max_tokens=1500,
        ) or []


class GrowthHacker(FactoryAgent):
    department = "marketing"
    role = "growth_hacker"
    name = "growth_hacker"
    system_prompt = """Ты — Growth Hacker. Находишь нестандартные способы роста для агентства моделей.
Фокус на быстрых экспериментах с минимальными затратами. B2B аудитория. Всё на русском."""

    def generate_growth_experiments(self, insights: dict, count: int = 5) -> list[dict]:
        return self.think_json(
            f"Придумай {count} growth-экспериментов для агентства моделей.\n"
            "Верни JSON массив:\n"
            '[{"name": "...", "hypothesis": "...", "channel": "...", '
            '"effort": "low|medium|high", "expected_lift": "X%", '
            '"how_to_test": "шаги теста", "success_metric": "как измерить"}]',
            context={"insights": insights},
            max_tokens=2000,
        ) or []


class MarketingDepartment:
    """Координатор маркетинг-департамента — запускает нужные роли."""

    def __init__(self) -> None:
        self.content = ContentStrategist()
        self.viral = ViralEngineer()
        self.seo = SEOSpecialist()
        self.copywriter = AdCopywriter()
        self.growth = GrowthHacker()

    def execute_task(self, task: dict, insights: dict, product_id: int | None = None) -> list[dict]:
        """CEO назначает задачу — департамент разбивает на роли и выполняет."""
        task_type = task.get("action", "")
        saved_actions = []

        if "content" in task_type or "social" in task_type:
            plan = self.content.create_content_plan(insights)
            for item in plan[:5]:
                action_id = db.insert("growth_actions", {
                    "product_id": product_id,
                    "action_type": "content",
                    "channel": item.get("platform", "instagram"),
                    "content": f"[{item.get('day')}] {item.get('topic')}: {item.get('hook')} → {item.get('cta')}",
                    "status": "pending",
                    "priority": 7,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
                saved_actions.append({"type": "content_plan", "_db_id": action_id, **item})

        if "viral" in task_type or "tiktok" in task_type or "video" in task_type:
            scripts = self.viral.generate_video_scripts(count=2)
            for s in scripts:
                action_id = db.insert("growth_actions", {
                    "product_id": product_id,
                    "action_type": "social",
                    "channel": "tiktok",
                    "content": f"{s.get('title')}\n\nХук: {s.get('hook')}\n\n{s.get('script', '')[:300]}",
                    "status": "pending",
                    "priority": 8,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
                saved_actions.append({"type": "viral_script", "_db_id": action_id, **s})

        if "seo" in task_type:
            cluster = self.seo.generate_seo_cluster("аренда моделей для мероприятий")
            if cluster:
                action_id = db.insert("growth_actions", {
                    "product_id": product_id,
                    "action_type": "seo",
                    "channel": "seo",
                    "content": f"SEO кластер: {cluster.get('main_keyword')}\nСтатья: {cluster.get('pillar_article', {}).get('title', '')}",
                    "status": "pending",
                    "priority": 6,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
                saved_actions.append({"type": "seo_cluster", "_db_id": action_id, **cluster})

        if "growth" in task_type or "experiment" in task_type:
            exps = self.growth.generate_growth_experiments(insights, count=3)
            for e in exps:
                action_id = db.insert("growth_actions", {
                    "product_id": product_id,
                    "action_type": "ad",
                    "channel": e.get("channel", "direct"),
                    "content": f"GROWTH EXP: {e.get('name')}\nГипотеза: {e.get('hypothesis')}\nКак тестить: {e.get('how_to_test', '')}",
                    "status": "pending",
                    "priority": 9,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
                saved_actions.append({"type": "growth_experiment", "_db_id": action_id, **e})

        logger.info("[Marketing Dept] Выполнено %d actions для задачи '%s'", len(saved_actions), task_type)
        return saved_actions
