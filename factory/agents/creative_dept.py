"""🎨 Creative Department — копирайтинг, визуальные концепции, бренд-голос, сторителлинг."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from factory.agents.base import FactoryAgent

logger = logging.getLogger(__name__)


class CopywriterAI(FactoryAgent):
    department = "creative"
    role = "copywriter"
    name = "copywriter_ai"
    system_prompt = """Ты — Copywriter AI агентства моделей Nevesty Models.
Пишешь описания моделей, заголовки и тексты для соцсетей (Instagram, Telegram, ВКонтакте).
Твой стиль: элегантный, притягивающий, с лёгкой интригой.
Текст должен продавать, не звуча как реклама.
Всё на русском языке."""

    def write_content(self, context: dict) -> dict:
        """Пишет тексты для соцсетей и описания моделей."""
        try:
            return self.think_json(
                "Создай контент для агентства моделей Nevesty Models.\n"
                "Верни JSON:\n"
                '{"model_descriptions": ['
                '{"style": "классика|гламур|спорт|бохо", '
                '"headline": "заголовок для модели", '
                '"bio_short": "3-5 предложений описания", '
                '"bio_long": "развёрнутое описание для профиля"}], '
                '"social_posts": ['
                '{"platform": "instagram|telegram|vk", '
                '"format": "пост|сториз|рилс|канал", '
                '"caption": "текст публикации", '
                '"hashtags": ["#тег1", "#тег2"], '
                '"cta": "призыв к действию"}], '
                '"ad_headlines": ["заголовок 1", "заголовок 2", "заголовок 3"], '
                '"tagline_options": ["слоган 1", "слоган 2"]}',
                context=context,
                max_tokens=2000,
            ) or {}
        except Exception as e:
            logger.error("[creative/copywriter] write_content error: %s", e)
            return {}


class VisualConceptor(FactoryAgent):
    department = "creative"
    role = "visual"
    name = "visual_conceptor"
    system_prompt = """Ты — Visual Conceptor агентства моделей Nevesty Models.
Разрабатываешь концепции фотосессий, предлагаешь идеи для визуального контента.
Знаешь тренды индустрии моды и фотографии.
Описываешь идеи детально: локация, свет, реквизит, образы, настроение.
Всё на русском языке."""

    def generate_concepts(self, context: dict) -> dict:
        """Генерирует концепции фотосессий и визуального контента."""
        try:
            return self.think_json(
                "Разработай концепции фотосессий и визуального контента для агентства моделей.\n"
                "Верни JSON:\n"
                '{"photoshoot_concepts": ['
                '{"title": "название концепции", '
                '"mood": "настроение/атмосфера", '
                '"location": "тип локации", '
                '"lighting": "тип освещения", '
                '"styling": "стиль одежды и образа", '
                '"props": ["реквизит 1", "реквизит 2"], '
                '"target_use": "сайт|соцсети|каталог|реклама", '
                '"season": "весна-лето|осень-зима|круглогодично", '
                '"effort": "малый|средний|большой бюджет"}], '
                '"content_calendar": ['
                '{"week": 1, "theme": "тема недели", "content_pieces": 3, '
                '"format": "фото|видео|рилс|карусель"}], '
                '"trending_styles": ["тренд 1", "тренд 2"], '
                '"mood_board_keywords": ["ключевое слово для поиска референсов"]}',
                context=context,
                max_tokens=1800,
            ) or {}
        except Exception as e:
            logger.error("[creative/visual] generate_concepts error: %s", e)
            return {}


class BrandVoiceKeeper(FactoryAgent):
    department = "creative"
    role = "brand_voice"
    name = "brand_voice_keeper"
    system_prompt = """Ты — Brand Voice Keeper агентства моделей Nevesty Models.
Следишь за единым стилем коммуникации агентства во всех каналах.
Формируешь и защищаешь tone of voice бренда: элегантный, профессиональный, доступный.
Указываешь на несоответствия стилю и предлагаешь корректировки.
Всё на русском языке."""

    def audit_brand_voice(self, context: dict) -> dict:
        """Анализирует единство бренд-голоса и предлагает рекомендации."""
        try:
            return self.think_json(
                "Проведи аудит бренд-голоса агентства моделей Nevesty Models.\n"
                "Верни JSON:\n"
                '{"brand_voice_profile": {'
                '"personality_traits": ["черта 1", "черта 2", "черта 3"], '
                '"tone": "формальный|полуформальный|дружелюбный|экспертный", '
                '"language_level": "простой|средний|сложный", '
                '"forbidden_phrases": ["фраза которую нельзя использовать"], '
                '"must_use_phrases": ["обязательные формулировки"]}, '
                '"consistency_issues": ['
                '{"channel": "telegram|instagram|сайт", '
                '"issue": "описание несоответствия", '
                '"correction": "как исправить"}], '
                '"style_guide_rules": ['
                '{"rule": "правило", "example_wrong": "неправильно", '
                '"example_right": "правильно"}], '
                '"overall_brand_health": "сильный|требует работы|слабый"}',
                context=context,
                max_tokens=1500,
            ) or {}
        except Exception as e:
            logger.error("[creative/brand_voice] audit_brand_voice error: %s", e)
            return {}


class StorytellingAgent(FactoryAgent):
    department = "creative"
    role = "storytelling"
    name = "storytelling_agent"
    system_prompt = """Ты — Storytelling Agent агентства моделей Nevesty Models.
Создаёшь истории успеха, кейсы клиентов и нарративы о моделях.
Умеешь находить эмоциональный крючок в каждой истории.
Трансформируешь скучные факты в захватывающие рассказы, которые продают.
Всё на русском языке."""

    def create_stories(self, context: dict) -> dict:
        """Создаёт истории успеха и кейсы клиентов."""
        try:
            return self.think_json(
                "Создай истории успеха и кейсы для агентства моделей Nevesty Models.\n"
                "Верни JSON:\n"
                '{"success_stories": ['
                '{"title": "заголовок истории", '
                '"hero": "клиент|модель|агентство", '
                '"situation": "ситуация до", '
                '"challenge": "проблема которую нужно было решить", '
                '"solution": "как помогло агентство", '
                '"result": "конкретный результат с цифрами", '
                '"emotional_hook": "эмоциональная деталь", '
                '"format": "текст|видео|кейс|отзыв"}], '
                '"model_spotlights": ['
                '{"angle": "угол истории о модели", '
                '"narrative_arc": "завязка-развитие-кульминация", '
                '"key_message": "главный посыл"}], '
                '"brand_story_elements": ['
                '{"element": "элемент истории бренда", "use_in": "где использовать"}]}',
                context=context,
                max_tokens=2000,
            ) or {}
        except Exception as e:
            logger.error("[creative/storytelling] create_stories error: %s", e)
            return {}


class CreativeDepartment:
    """Координатор креативного департамента."""

    def __init__(self):
        self.copywriter = CopywriterAI()
        self.visual = VisualConceptor()
        self.brand_voice = BrandVoiceKeeper()
        self.storytelling = StorytellingAgent()

    def execute_task(self, task: str, context: dict) -> dict:
        """Диспетчер по ключевым словам задачи."""
        task_lower = task.lower()
        result_data = {}
        roles_used = []

        try:
            if any(kw in task_lower for kw in ("копирайт", "copy", "текст", "пост", "описан", "caption", "соцсет")):
                result_data["content"] = self.copywriter.write_content(context)
                roles_used.append("copywriter")
        except Exception as e:
            logger.error("[CreativeDept] copywriter error: %s", e)

        try:
            if any(kw in task_lower for kw in ("визуал", "visual", "фото", "концепц", "photo", "concept", "контент")):
                result_data["visual"] = self.visual.generate_concepts(context)
                roles_used.append("visual")
        except Exception as e:
            logger.error("[CreativeDept] visual error: %s", e)

        try:
            if any(kw in task_lower for kw in ("бренд", "brand", "голос", "voice", "стиль", "tone", "коммуникац")):
                result_data["brand_voice"] = self.brand_voice.audit_brand_voice(context)
                roles_used.append("brand_voice")
        except Exception as e:
            logger.error("[CreativeDept] brand_voice error: %s", e)

        try:
            if any(kw in task_lower for kw in ("истор", "story", "кейс", "case", "сторителл", "нарратив")) \
                    or not roles_used:
                result_data["stories"] = self.storytelling.create_stories(context)
                roles_used.append("storytelling")
        except Exception as e:
            logger.error("[CreativeDept] storytelling error: %s", e)

        output = {
            "department": "creative",
            "task": task,
            "result": result_data,
            "roles_used": roles_used,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        logger.info("[CreativeDept] Задача '%s' выполнена. Ролей задействовано: %d", task, len(roles_used))
        return output
