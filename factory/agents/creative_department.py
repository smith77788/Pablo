"""
Creative Department — Copywriting, brand voice, storytelling.

Heuristic (no API calls) version with sub-agent classes.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Dict, Any


# ──────────────────────────────────────────────────────────────────────────────
# Sub-agent heuristic stubs
# ──────────────────────────────────────────────────────────────────────────────

class CopywriterAI:
    """Heuristic copywriter — generates text without API calls."""

    department = "creative"
    role = "copywriter"
    name = "copywriter_ai"

    def run(self, context: dict | None) -> dict:
        """Return heuristic insights and recommendations."""
        return {
            "insights": [
                "Контент агентства должен отражать элегантность и профессионализм бренда.",
                "Регулярные посты в соцсетях увеличивают вовлечённость аудитории.",
            ],
            "recommendations": [
                "Использовать сторителлинг в описаниях моделей.",
                "Добавить призыв к действию в каждый пост.",
            ],
            "priority": 7,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def generate_social_caption(self, event_type: str, model_name: str) -> str:
        """Delegate to shared helper."""
        return _generate_social_caption(event_type, model_name)

    def generate_promo_text(self, discount: int, validity_days: int) -> str:
        """Delegate to shared helper."""
        return _generate_promo_text(discount, validity_days)


class VisualConceptor:
    """Heuristic visual conceptor — proposes photo/content ideas without API calls."""

    department = "creative"
    role = "visual"
    name = "visual_conceptor"

    def run(self, context: dict | None) -> dict:
        """Return heuristic insights and recommendations."""
        return {
            "insights": [
                "Визуальная идентичность бренда должна быть единой во всех каналах.",
                "Тренд на лёгкую, воздушную фотографию растёт в fashion-сегменте.",
            ],
            "recommendations": [
                "Проводить фотосессии в светлых, минималистичных интерьерах.",
                "Включить рилс-формат в контент-план для Instagram.",
            ],
            "priority": 6,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


class BrandVoiceKeeper:
    """Heuristic brand voice keeper — checks tone consistency without API calls."""

    department = "creative"
    role = "brand_voice"
    name = "brand_voice_keeper"

    def run(self, context: dict | None) -> dict:
        """Return heuristic insights and recommendations."""
        return {
            "insights": [
                "Тон коммуникаций должен быть единым на всех платформах.",
                "Отдельные посты нарушают style guide (слишком разговорный тон).",
            ],
            "recommendations": [
                "Создать чек-лист для проверки текстов перед публикацией.",
                "Ввести запрещённые слова в редакционную политику.",
            ],
            "priority": 5,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def get_brand_voice_guidelines(self) -> Dict[str, Any]:
        """Return brand voice guidelines dict."""
        return _get_brand_voice_guidelines()


class StorytellingAgent:
    """Heuristic storytelling agent — creates narrative templates without API calls."""

    department = "creative"
    role = "storytelling"
    name = "storytelling_agent"

    def run(self, context: dict | None) -> dict:
        """Return heuristic insights and recommendations."""
        return {
            "insights": [
                "Истории успеха клиентов — самый конвертирующий формат контента.",
                "Нарративы о моделях повышают доверие к агентству.",
            ],
            "recommendations": [
                "Собирать отзывы клиентов после каждого мероприятия.",
                "Публиковать одну историю успеха в месяц.",
            ],
            "priority": 6,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ──────────────────────────────────────────────────────────────────────────────
# Shared helpers (extracted so both sub-agents and CreativeDepartment can reuse)
# ──────────────────────────────────────────────────────────────────────────────

def _generate_social_caption(event_type: str, model_name: str) -> str:
    event_type = event_type or "мероприятие"
    model_name = model_name or "наша модель"

    captions = {
        "корпоратив": (
            f"✨ Блестящий {event_type} завершён! "
            f"{model_name} создала незабываемую атмосферу для гостей. "
            f"Доверяйте профессионалам — выбирайте Nevesty Models. 🌟"
        ),
        "свадьба": (
            f"💍 Особенный день стал ещё красивее! "
            f"{model_name} помогла создать волшебную атмосферу. "
            f"Nevesty Models — для самых важных моментов в вашей жизни."
        ),
        "фотосессия": (
            f"📸 Новые кадры — новые эмоции! "
            f"{model_name} блестяще справилась с фотосессией. "
            f"Смотрите, как профессионализм превращается в искусство. ✨"
        ),
    }

    et_lower = event_type.lower()
    for key, caption in captions.items():
        if key in et_lower:
            return caption

    return (
        f"🌟 {event_type} прошёл великолепно! "
        f"{model_name} — настоящий профессионал своего дела. "
        f"Спасибо всем участникам! Nevesty Models #nevesty #models"
    )


def _generate_promo_text(discount: int, validity_days: int) -> str:
    discount = max(0, min(100, discount))
    validity_days = max(1, validity_days)

    urgency = (
        "Только сегодня!" if validity_days == 1
        else f"Акция действует {validity_days} {'день' if validity_days == 1 else 'дня' if validity_days < 5 else 'дней'}!"
    )

    return (
        f"🎉 Специальное предложение от Nevesty Models!\n\n"
        f"Скидка {discount}% на все услуги агентства.\n"
        f"{urgency}\n\n"
        f"Не упустите возможность получить услуги premium-класса по выгодной цене. "
        f"Свяжитесь с нами прямо сейчас и укажите промокод NEVESTY{discount}.\n\n"
        f"📞 Пишите в Telegram — мы ответим в течение 15 минут!"
    )


def _get_brand_voice_guidelines() -> Dict[str, Any]:
    return {
        "tone": "профессиональный, элегантный, доступный",
        "style": "краткий и ёмкий, без клише, с лёгкой интригой",
        "keywords": [
            "профессионализм",
            "элегантность",
            "эксклюзивность",
            "надёжность",
            "красота",
        ],
        "avoid_words": [
            "дёшево",
            "скидка любой",
            "срочно!!!",
            "лучшие в мире",
            "гарантируем 100%",
        ],
        "key_messages": [
            "Nevesty Models — агентство, которому доверяют",
            "Каждая модель — профессионал с историей",
            "Мы создаём впечатления, которые остаются",
        ],
        "channels": {
            "telegram": "информативный, с призывом к действию",
            "instagram": "визуальный сторителлинг, хэштеги, эмодзи умеренно",
            "website": "деловой тон, SEO-оптимизация, конкретные цифры",
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# Department coordinator
# ──────────────────────────────────────────────────────────────────────────────

class CreativeDepartment:
    """Creative Department with template-based methods (no API calls)."""

    def __init__(self):
        self.copywriter = CopywriterAI()
        self.visual = VisualConceptor()
        self.brand_voice = BrandVoiceKeeper()
        self.storytelling = StorytellingAgent()

    # ------------------------------------------------------------------ #
    # execute_task — dispatcher                                            #
    # ------------------------------------------------------------------ #

    def execute_task(self, task: str, context: dict | None) -> dict:
        """Dispatch task to sub-agents based on keywords; always uses at least 2 roles."""
        ctx = context or {}
        task_lower = (task or "").lower()
        result_data: Dict[str, Any] = {}
        roles_used: list[str] = []

        if any(kw in task_lower for kw in ("copy", "текст", "пост", "описан", "caption", "соцсет", "копирайт")):
            result_data["content"] = self.copywriter.run(ctx)
            roles_used.append("copywriter")

        if any(kw in task_lower for kw in ("визуал", "visual", "фото", "концепц", "photo", "concept", "контент")):
            result_data["visual"] = self.visual.run(ctx)
            roles_used.append("visual")

        if any(kw in task_lower for kw in ("бренд", "brand", "голос", "voice", "стиль", "tone", "коммуникац")):
            result_data["brand_voice"] = self.brand_voice.run(ctx)
            roles_used.append("brand_voice")

        if any(kw in task_lower for kw in ("истор", "story", "кейс", "case", "сторителл", "нарратив")) \
                or not roles_used:
            result_data["stories"] = self.storytelling.run(ctx)
            roles_used.append("storytelling")

        # Ensure at least 2 roles are always present
        if len(roles_used) < 2:
            if "copywriter" not in roles_used:
                result_data["content"] = self.copywriter.run(ctx)
                roles_used.append("copywriter")
            if len(roles_used) < 2 and "visual" not in roles_used:
                result_data["visual"] = self.visual.run(ctx)
                roles_used.append("visual")

        all_insights: list[str] = []
        for v in result_data.values():
            if isinstance(v, dict):
                all_insights.extend(v.get("insights", []))

        return {
            "department": "creative",
            "task": task,
            "result": result_data,
            "roles_used": roles_used,
            "insights": all_insights,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------ #
    # generate_model_bio                                                   #
    # ------------------------------------------------------------------ #

    def generate_model_bio(self, model_data: Dict[str, Any]) -> str:
        """Generate a short 2-3 sentence bio from model params."""
        name = model_data.get("name") or "Модель"
        height = model_data.get("height")
        city = model_data.get("city") or "Москва"
        categories = model_data.get("categories") or model_data.get("category") or "подиум"

        height_str = f", рост {height} см" if height else ""
        categories_str = (
            ", ".join(categories) if isinstance(categories, list) else str(categories)
        )

        return (
            f"{name} — профессиональная модель из {city}{height_str}. "
            f"Специализация: {categories_str}. "
            f"Работает с ведущими брендами и агентствами, сочетая природную харизму "
            f"с безупречным профессионализмом."
        )

    # ------------------------------------------------------------------ #
    # generate_social_caption                                              #
    # ------------------------------------------------------------------ #

    def generate_social_caption(self, event_type: str, model_name: str) -> str:
        """Generate a social media caption for a completed event."""
        return _generate_social_caption(event_type, model_name)

    # ------------------------------------------------------------------ #
    # generate_promo_text                                                  #
    # ------------------------------------------------------------------ #

    def generate_promo_text(self, discount: int, validity_days: int) -> str:
        """Generate promo text for a discount offer."""
        return _generate_promo_text(discount, validity_days)

    # ------------------------------------------------------------------ #
    # get_brand_voice_guidelines                                           #
    # ------------------------------------------------------------------ #

    def get_brand_voice_guidelines(self) -> Dict[str, Any]:
        """Return brand voice guidelines dict."""
        return _get_brand_voice_guidelines()
