"""
Creative Department — Copywriting, brand voice, storytelling.
"""
from __future__ import annotations


class CreativeDepartment:
    """Creative Department with template-based methods (no API calls)."""

    # ------------------------------------------------------------------ #
    # generate_model_bio                                                   #
    # ------------------------------------------------------------------ #

    def generate_model_bio(self, model_data: dict) -> str:
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

    # ------------------------------------------------------------------ #
    # generate_promo_text                                                  #
    # ------------------------------------------------------------------ #

    def generate_promo_text(self, discount: int, validity_days: int) -> str:
        """Generate promo text for a discount offer."""
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

    # ------------------------------------------------------------------ #
    # get_brand_voice_guidelines                                           #
    # ------------------------------------------------------------------ #

    def get_brand_voice_guidelines(self) -> dict:
        """Return brand voice guidelines dict."""
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
