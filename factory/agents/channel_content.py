"""
Channel Content Agent — generates Telegram channel post templates for Nevesty Models agency.

Classes:
  ChannelContentGenerator — heuristic template-based generator (no API key required)
  TelegramChannelAgent    — LLM-powered generator (falls back to templates when no API key)
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
import random

from factory.agents.base import FactoryAgent


POST_FORMATS = ["case_study", "model_spotlight", "tips", "promotion", "behind_scenes", "stats"]

SEASONAL_THEMES: Dict[int, str] = {
    1: "Новогодние корпоративы",
    2: "Романтические события (14 февраля)",
    3: "Весенние показы",
    4: "Выставки и форумы",
    5: "Майские праздники",
    6: "Свадебный сезон",
    7: "Летние промо",
    8: "Бэк-ту-скул кампании",
    9: "Осенние Fashion weeks",
    10: "Корпоративы октябрь",
    11: "Pre-Christmas съёмки",
    12: "Новогодние корпоративы",
}

EVENT_TYPE_HASHTAGS: Dict[str, List[str]] = {
    "корпоратив": ["#корпоратив", "#корпоративноемероприятие", "#модели", "#event"],
    "фотосессия": ["#фотосессия", "#фото", "#модели", "#photoshoot"],
    "показ": ["#показмод", "#fashion", "#runway", "#подиум"],
    "промо": ["#промоутеры", "#promo", "#реклама", "#промо"],
    "свадьба": ["#свадьба", "#wedding", "#свадебноеагентство"],
    "default": ["#модели", "#агентствомоделей", "#nevestymodels"],
}


class ChannelContentGenerator:
    """Generates ready-to-post Telegram channel content."""

    AGENCY_NAME = "Nevesty Models"
    AGENCY_HANDLE = "@nevesty_models"

    def generate_model_spotlight_post(self, model_data: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a 'Model of the Week' spotlight post."""
        name = model_data.get("name", "Наша модель")
        age = model_data.get("age", "")
        height = model_data.get("height", "")
        category = model_data.get("category", "fashion")
        city = model_data.get("city", "Москва")
        order_count = model_data.get("order_count", 0)

        category_emoji = {"fashion": "👗", "commercial": "📺", "events": "🎉"}.get(category, "✨")
        height_str = f"{height} см" if height else ""
        age_str = f"{age} лет" if age else ""
        params = ", ".join(filter(None, [age_str, height_str, city]))

        text = (
            f"✨ <b>Модель недели — {name}</b>\n\n"
            f"{category_emoji} Специализация: {category}\n"
            f"📍 {params}\n"
            f"📋 Выполнено заказов: {order_count}\n\n"
            f"💼 Доступна для:\n"
            f"• Корпоративных мероприятий\n"
            f"• Фотосессий и рекламных съёмок\n"
            f"• Показов и презентаций\n\n"
            f"📲 Забронировать: {self.AGENCY_HANDLE}\n\n"
            f"#моделинедели #модели #агентствомоделей #{city.lower().replace(' ', '')} #nevestymodels"
        )
        return {
            "format": "model_spotlight",
            "text": text,
            "char_count": len(text),
            "model_name": name,
            "recommended_time": "вт/чт 18:00-20:00",
        }

    def generate_case_study_post(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a case study post from a completed order."""
        event_type = order_data.get("event_type", "мероприятие")
        city = order_data.get("city", "Москва")
        model_count = order_data.get("model_count", 1)
        duration_h = order_data.get("duration_hours", 4)
        month = datetime.now(timezone.utc).strftime("%B")

        hashtags = EVENT_TYPE_HASHTAGS.get(event_type.lower(), EVENT_TYPE_HASHTAGS["default"])
        hashtag_str = " ".join(hashtags[:4])

        text = (
            f"📸 <b>Кейс: {event_type} в {city}</b>\n\n"
            f"В {month} наши модели блестяще отработали очередное мероприятие.\n\n"
            f"🔢 Детали проекта:\n"
            f"• Тип: {event_type}\n"
            f"• Город: {city}\n"
            f"• Моделей: {model_count}\n"
            f"• Длительность: {duration_h} часа\n\n"
            f"✅ Клиент остался доволен профессионализмом команды!\n\n"
            f"Нужны модели для вашего события?\n"
            f"📲 Пишите: {self.AGENCY_HANDLE}\n\n"
            f"{hashtag_str} #nevestymodels"
        )
        return {
            "format": "case_study",
            "text": text,
            "char_count": len(text),
            "event_type": event_type,
            "recommended_time": "пн/ср/пт 12:00-14:00",
        }

    def generate_tips_post(self, topic: Optional[str] = None) -> Dict[str, Any]:
        """Generate a tips/educational post."""
        topics = {
            "choosing_model": {
                "title": "Как выбрать модель для вашего мероприятия",
                "tips": [
                    "Определите тип события: корпоратив, промо или показ",
                    "Учитывайте параметры: рост, возраст, опыт",
                    "Проверьте портфолио и отзывы",
                    "Заранее согласуйте дресс-код и сценарий",
                    "Бронируйте за 2–4 недели до события",
                ],
            },
            "event_prep": {
                "title": "5 шагов для идеального мероприятия с моделями",
                "tips": [
                    "Чёткое техзадание для модели за 3 дня",
                    "Согласованный дресс-код и образ",
                    "Инструктаж на месте за 30 минут до старта",
                    "Продуманный маршрут/сценарий",
                    "Контакт менеджера на протяжении всего события",
                ],
            },
        }
        selected = topics.get(topic or "", topics["choosing_model"])
        tips_text = "\n".join(f"{i+1}. {t}" for i, t in enumerate(selected["tips"]))

        text = (
            f"💡 <b>{selected['title']}</b>\n\n"
            f"{tips_text}\n\n"
            f"✨ {self.AGENCY_NAME} — профессиональный подход к каждому проекту.\n\n"
            f"📲 Консультация: {self.AGENCY_HANDLE}\n\n"
            f"#советы #мероприятие #модели #агентствомоделей #nevestymodels"
        )
        return {
            "format": "tips",
            "text": text,
            "char_count": len(text),
            "topic": topic or "choosing_model",
            "recommended_time": "ср 10:00-12:00",
        }

    def generate_promotion_post(self, discount_pct: int = 15, valid_days: int = 7) -> Dict[str, Any]:
        """Generate a promotional post with discount."""
        seasonal_theme = SEASONAL_THEMES.get(datetime.now(timezone.utc).month, "мероприятий")

        text = (
            f"🎉 <b>Специальное предложение!</b>\n\n"
            f"Готовитесь к {seasonal_theme.lower()}?\n\n"
            f"🔥 Скидка <b>{discount_pct}%</b> на бронирование моделей!\n\n"
            f"⏰ Акция действует ещё {valid_days} {'дней' if valid_days > 4 else 'дня'}\n\n"
            f"📋 Что включено:\n"
            f"• Подбор модели под ваш бриф\n"
            f"• Согласование образа\n"
            f"• Менеджерское сопровождение\n\n"
            f"📲 Для получения скидки напишите: {self.AGENCY_HANDLE}\n"
            f"Укажите промокод: <code>CHANNEL{discount_pct}</code>\n\n"
            f"#скидка #акция #модели #nevestymodels"
        )
        return {
            "format": "promotion",
            "text": text,
            "char_count": len(text),
            "discount_pct": discount_pct,
            "promo_code": f"CHANNEL{discount_pct}",
            "valid_days": valid_days,
            "recommended_time": "пт 15:00-17:00",
        }

    def generate_stats_post(self, stats: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a monthly stats/achievement post."""
        orders = stats.get("total_orders", 0)
        models = stats.get("active_models", 0)
        cities = stats.get("cities_served", 1)
        rating = stats.get("avg_rating", 5.0)
        month_name = datetime.now(timezone.utc).strftime("%B")

        text = (
            f"📊 <b>Итоги {month_name} — {self.AGENCY_NAME}</b>\n\n"
            f"🗓 Этот месяц был насыщенным!\n\n"
            f"📈 Наши результаты:\n"
            f"✅ Выполнено заказов: {orders}\n"
            f"💃 Активных моделей: {models}\n"
            f"🌆 Городов: {cities}\n"
            f"⭐ Средний рейтинг: {rating:.1f}/5\n\n"
            f"Спасибо нашим клиентам за доверие! 🙏\n\n"
            f"Готовы работать ещё лучше в следующем месяце!\n"
            f"📲 {self.AGENCY_HANDLE}\n\n"
            f"#итоги #статистика #модели #nevestymodels"
        )
        return {
            "format": "stats",
            "text": text,
            "char_count": len(text),
            "month": month_name,
            "recommended_time": "1-е число месяца 10:00",
        }

    def get_content_calendar(self, weeks: int = 4) -> List[Dict[str, Any]]:
        """Return a content calendar with scheduled post formats."""
        calendar = []
        formats_cycle = ["model_spotlight", "tips", "case_study", "promotion", "stats", "model_spotlight"]
        for week in range(weeks):
            for day_offset, post_format in [(1, formats_cycle[week % len(formats_cycle)]),
                                             (4, "case_study" if week % 2 == 0 else "tips")]:
                calendar.append({
                    "week": week + 1,
                    "day": f"неделя {week + 1}, день {day_offset}",
                    "format": post_format,
                    "recommended_time": "12:00-14:00" if day_offset == 1 else "18:00-20:00",
                })
        return calendar[:weeks * 2]


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 9.1 — LLM-powered Telegram channel post generator
# ─────────────────────────────────────────────────────────────────────────────

class TelegramChannelAgent(FactoryAgent):
    """LLM-powered Telegram channel post generator for Nevesty Models agency.

    Uses the Anthropic SDK (or local claude CLI) when available; falls back to
    high-quality heuristic templates from ChannelContentGenerator otherwise.
    """

    name = "TelegramChannelAgent"
    department = "content"
    role = "telegram_channel"

    system_prompt = (
        "Ты — опытный SMM-специалист модельного агентства Nevesty Models. "
        "Создаёшь яркие, вовлекающие посты для Telegram-канала агентства. "
        "Стиль: профессиональный, живой, с нотками эксклюзивности. "
        "Всегда пишешь на русском языке. Используешь эмодзи умеренно (не более 4 на пост). "
        "Посты должны привлекать внимание с первых слов и заканчиваться чётким призывом к действию."
    )

    _fallback = ChannelContentGenerator()

    # ── Model spotlight ───────────────────────────────────────────────────────

    def generate_model_spotlight(
        self,
        model_name: str,
        model_params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Generate a Telegram post spotlighting a specific model.

        Returns a dict with keys: format, text, char_count, model_name,
        recommended_time, source ('llm' or 'template').
        """
        height = model_params.get("height", "170")
        city = model_params.get("city", "Москва")
        category = model_params.get("category", "fashion")
        order_count = model_params.get("order_count", 0)

        prompt = (
            f"Создай пост для Telegram-канала модельного агентства Nevesty о модели {model_name}.\n\n"
            f"Параметры: рост {height} см, город {city}, специализация {category}, "
            f"выполнено заказов: {order_count}.\n\n"
            "Пост должен:\n"
            "- Привлекать внимание с первых слов\n"
            "- Содержать 2-3 интересных факта или преимущества модели\n"
            "- Иметь призыв к действию (бронирование через @nevesty_models)\n"
            "- Использовать 2-3 релевантных хэштега\n"
            "- Быть не длиннее 300 слов\n"
            "- Использовать HTML-разметку Telegram (<b>, <i>) где уместно\n\n"
            "Верни ТОЛЬКО текст поста, без пояснений."
        )

        text = self.think(prompt, max_tokens=600)

        if not text:
            result = self._fallback.generate_model_spotlight_post({
                "name": model_name,
                "height": height,
                "city": city,
                "category": category,
                "order_count": order_count,
            })
            result["source"] = "template"
            return result

        return {
            "format": "model_spotlight",
            "text": text,
            "char_count": len(text),
            "model_name": model_name,
            "recommended_time": "вт/чт 18:00-20:00",
            "source": "llm",
        }

    # ── Promo post ────────────────────────────────────────────────────────────

    def generate_promo_post(
        self,
        promo_type: str = "seasonal",
        discount_pct: int = 15,
        valid_days: int = 7,
    ) -> Dict[str, Any]:
        """Generate a promotional post for the Telegram channel.

        promo_type: 'seasonal' | 'flash' | 'referral' | 'new_arrival' | str
        Returns a dict with keys: format, text, char_count, promo_type,
        promo_code, recommended_time, source.
        """
        seasonal_theme = SEASONAL_THEMES.get(datetime.now(timezone.utc).month, "мероприятий")
        promo_code = f"CHAN{discount_pct}"

        type_hints: Dict[str, str] = {
            "seasonal": f"сезонная скидка {discount_pct}% к теме «{seasonal_theme}»",
            "flash": f"флэш-акция {discount_pct}% — только {valid_days} дней",
            "referral": "реферальная программа: приведи друга — получи бонус",
            "new_arrival": "новые модели в каталоге — пригласи первым",
        }
        hint = type_hints.get(promo_type, f"промо-акция ({promo_type}), скидка {discount_pct}%")

        prompt = (
            f"Создай рекламный пост для Telegram-канала модельного агентства Nevesty.\n\n"
            f"Тип промо: {hint}\n"
            f"Промокод: {promo_code}\n"
            f"Срок акции: {valid_days} дней\n\n"
            "Пост должен:\n"
            "- Быть убедительным и создавать срочность\n"
            "- Чётко называть выгоду для клиента\n"
            "- Содержать призыв написать @nevesty_models и указать промокод\n"
            "- Быть не длиннее 200 слов\n"
            "- Использовать HTML-разметку Telegram (<b>, <i>, <code>) где уместно\n\n"
            "Верни ТОЛЬКО текст поста, без пояснений."
        )

        text = self.think(prompt, max_tokens=400)

        if not text:
            result = self._fallback.generate_promotion_post(discount_pct, valid_days)
            result["source"] = "template"
            result["promo_type"] = promo_type
            return result

        return {
            "format": "promotion",
            "text": text,
            "char_count": len(text),
            "promo_type": promo_type,
            "promo_code": promo_code,
            "valid_days": valid_days,
            "recommended_time": "пт 15:00-17:00",
            "source": "llm",
        }

    # ── Event announcement post ───────────────────────────────────────────────

    def generate_event_post(
        self,
        event_type: str,
        date: str = "",
        city: str = "Москва",
        model_count: int = 1,
    ) -> Dict[str, Any]:
        """Generate an event announcement post.

        event_type: e.g. 'корпоратив', 'показ мод', 'фотосессия', 'промо-акция'
        Returns a dict with keys: format, text, char_count, event_type,
        recommended_time, source.
        """
        date_str = date or "ближайшие выходные"
        hashtags = EVENT_TYPE_HASHTAGS.get(event_type.lower(), EVENT_TYPE_HASHTAGS["default"])

        prompt = (
            f"Создай анонс мероприятия для Telegram-канала агентства Nevesty.\n\n"
            f"Тип мероприятия: {event_type}\n"
            f"Дата: {date_str}\n"
            f"Город: {city}\n"
            f"Число моделей: {model_count}\n"
            f"Хэштеги: {' '.join(hashtags[:3])}\n\n"
            "Анонс должен:\n"
            "- Создавать ажиотаж и интерес\n"
            "- Содержать конкретные детали (тип события, дата, место)\n"
            "- Содержать призыв записаться или написать @nevesty_models\n"
            "- Быть не длиннее 200 слов\n"
            "- Использовать HTML-разметку Telegram (<b>, <i>) где уместно\n\n"
            "Верни ТОЛЬКО текст поста, без пояснений."
        )

        text = self.think(prompt, max_tokens=400)

        if not text:
            result = self._fallback.generate_case_study_post({
                "event_type": event_type,
                "city": city,
                "model_count": model_count,
                "duration_hours": 4,
            })
            result["source"] = "template"
            return result

        return {
            "format": "event_announcement",
            "text": text,
            "char_count": len(text),
            "event_type": event_type,
            "date": date_str,
            "recommended_time": "пн/ср 10:00-12:00",
            "source": "llm",
        }

    # ── FAQ post ──────────────────────────────────────────────────────────────

    def generate_faq_post(self, question: str, answer: str | None = None) -> str:
        """Generate a FAQ post for Telegram channel"""
        if not answer:
            # Template-based fallback
            return (
                f"❓ <b>Часто спрашивают</b>\n\n"
                f"<b>{question}</b>\n\n"
                f"Обратитесь к нашему менеджеру для получения подробной консультации 👇"
            )
        return f"❓ <b>{question}</b>\n\n{answer}\n\n✉️ Есть вопросы? Напишите нам!"

    # ── Tips post ─────────────────────────────────────────────────────────────

    def generate_tip_post(self, topic: str) -> str:
        """Generate a tips post (how to prepare for a shoot, etc.)"""
        tips: Dict[str, List[str]] = {
            "photo": [
                "Позаботьтесь о чистоте кожи за 2-3 дня до съёмки",
                "Подготовьте несколько образов для смены",
                "Убедитесь что одежда не помята",
            ],
            "casting": [
                "Приходите с чистыми, уложенными волосами",
                "Минимум макияжа — пусть видна натуральная красота",
                "Захватите портфолио или ссылку на него",
            ],
        }
        topic_tips = tips.get(topic, ["Консультируйтесь с профессионалами"])
        tips_text = "\n".join(f"✅ {t}" for t in topic_tips)
        return (
            f"💡 <b>Советы от агентства</b>\n\n"
            f"Тема: {topic}\n\n"
            f"{tips_text}\n\n"
            f"🌟 Наши модели всегда готовы к профессиональной работе!"
        )

    # ── Case / success story post ─────────────────────────────────────────────

    def generate_case_post(self, model_name: str, event_type: str, result: str) -> str:
        """Generate a success story / case study post"""
        return (
            f"🏆 <b>Кейс агентства</b>\n\n"
            f"Модель: {model_name}\n"
            f"Мероприятие: {event_type}\n\n"
            f"{result}\n\n"
            f"📌 Хотите также? Напишите нам!"
        )

    # ── Weekly content plan (7-day) ───────────────────────────────────────────

    def generate_weekly_content_plan(self, db_path: str | None = None) -> list:
        """Generate a 7-day content plan for the Telegram channel"""
        plan = [
            {"day": 1, "type": "spotlight", "content": self.generate_model_spotlight("Наша модель", {})},
            {"day": 2, "type": "tip", "content": self.generate_tip_post("photo")},
            {"day": 3, "type": "promo", "content": self.generate_promo_post("seasonal", 15, 7)},
            {"day": 4, "type": "faq", "content": self.generate_faq_post("Как заказать модель для фотосъёмки?")},
            {"day": 5, "type": "event", "content": self.generate_event_post("fashion_show", "15.06", "Москва", 5)},
            {"day": 6, "type": "tip", "content": self.generate_tip_post("casting")},
            {"day": 7, "type": "spotlight", "content": self.generate_model_spotlight("Топ-модель недели", {})},
        ]
        return plan

    # ── Convenience: generate a full weekly content batch ────────────────────

    def generate_weekly_batch(
        self,
        model_data: Optional[Dict[str, Any]] = None,
        event_type: str = "корпоратив",
        promo_type: str = "seasonal",
    ) -> Dict[str, Any]:
        """Generate model_spotlight + event + promo posts in one call.

        Returns dict with keys: posts (list), total_chars, calendar (2-week schedule).
        """
        md = model_data or {"name": "Мария", "height": "172", "city": "Москва", "category": "fashion"}
        name = md.get("name", "Мария")

        spotlight = self.generate_model_spotlight(name, md)
        event = self.generate_event_post(event_type)
        promo = self.generate_promo_post(promo_type)

        posts = [spotlight, event, promo]
        calendar = self._fallback.get_content_calendar(weeks=2)

        return {
            "posts": posts,
            "total_chars": sum(p["char_count"] for p in posts),
            "calendar": calendar,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def run(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """FactoryAgent entry point — called by cycle.py."""
        ctx = context or {}
        batch = self.generate_weekly_batch(
            model_data=ctx.get("model_data"),
            event_type=ctx.get("event_type", "корпоратив"),
            promo_type=ctx.get("promo_type", "seasonal"),
        )
        sources = [p.get("source", "template") for p in batch["posts"]]
        llm_count = sources.count("llm")
        return {
            "insights": [
                f"Generated {len(batch['posts'])} channel posts "
                f"({llm_count} via LLM, {len(sources) - llm_count} via template)"
            ],
            "recommendations": [p["text"] for p in batch["posts"]],
            "posts": batch["posts"],
            "calendar": batch["calendar"],
            "total_chars": batch["total_chars"],
            "generated_at": batch["generated_at"],
        }
