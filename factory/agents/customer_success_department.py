"""
Customer Success Department — Onboarding, retention, feedback, upsell.

Includes heuristic CustomerSuccessDepartment (no-LLM) and
agent-style specialist classes: OnboardingSpecialist, RetentionAnalyst,
FeedbackCollector, UpsellAdvisor, plus a CustomerSuccessDepartment.execute_task
entry-point that orchestrates them all.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Dict, List, Any


class CustomerSuccessDepartment:
    """Customer Success Department with heuristic-based methods (no API calls)."""

    # ------------------------------------------------------------------ #
    # generate_onboarding_message                                          #
    # ------------------------------------------------------------------ #

    def generate_onboarding_message(self, client_name: str, order_number: str) -> str:
        """Generate a welcome message after the client's first order."""
        client_name = client_name or "Клиент"
        order_number = order_number or "—"

        return (
            f"Здравствуйте, {client_name}! 👋\n\n"
            f"Добро пожаловать в Nevesty Models!\n\n"
            f"Ваша заявка #{order_number} успешно получена и уже обрабатывается. "
            f"В течение 1 рабочего часа наш менеджер свяжется с вами для уточнения деталей.\n\n"
            f"Что вас ждёт дальше:\n"
            f"1. Персональный подбор моделей под ваш запрос\n"
            f"2. Согласование деталей мероприятия\n"
            f"3. Подписание договора и фиксация условий\n"
            f"4. Проведение мероприятия с нашей поддержкой\n\n"
            f"Если у вас есть вопросы — пишите в любое время. "
            f"Мы рады помочь!\n\n"
            f"С уважением,\nКоманда Nevesty Models 🌟"
        )

    # ------------------------------------------------------------------ #
    # analyze_retention_risk                                               #
    # ------------------------------------------------------------------ #

    def analyze_retention_risk(self, client_history: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Analyze retention risk from order history.

        Returns {risk_level, days_since_last_order, recommendation}.
        """
        if not client_history:
            return {
                "risk_level": "unknown",
                "days_since_last_order": -1,
                "recommendation": "Нет данных по истории клиента. Уточните информацию.",
            }

        # Find the most recent order date
        last_date: datetime | None = None
        for order in client_history:
            raw = order.get("date") or order.get("created_at") or order.get("event_date")
            if not raw:
                continue
            try:
                if isinstance(raw, str):
                    dt = datetime.fromisoformat(raw)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = raw
                if last_date is None or dt > last_date:
                    last_date = dt
            except (ValueError, TypeError):
                continue

        if last_date is None:
            return {
                "risk_level": "unknown",
                "days_since_last_order": -1,
                "recommendation": "Не удалось определить дату последнего заказа.",
            }

        days_since = (datetime.now(timezone.utc) - last_date).days

        if days_since <= 30:
            risk_level = "low"
            recommendation = "Клиент активен. Предложите follow-up или апселл."
        elif days_since <= 90:
            risk_level = "medium"
            recommendation = "Клиент не заказывал 1–3 месяца. Отправьте персональное предложение."
        elif days_since <= 180:
            risk_level = "high"
            recommendation = "Клиент неактивен 3–6 месяцев. Запустите реактивационную кампанию."
        else:
            risk_level = "critical"
            recommendation = (
                "Клиент неактивен более 6 месяцев. "
                "Срочно свяжитесь с персональным предложением или скидкой."
            )

        return {
            "risk_level": risk_level,
            "days_since_last_order": days_since,
            "recommendation": recommendation,
        }

    # ------------------------------------------------------------------ #
    # generate_review_request                                              #
    # ------------------------------------------------------------------ #

    def generate_review_request(self, order_data: Dict[str, Any]) -> str:
        """Generate a polite review request message."""
        client = order_data.get("client_name") or order_data.get("name") or "Клиент"
        event_type = order_data.get("event_type") or "мероприятие"
        order_id = order_data.get("id") or order_data.get("order_id") or ""
        order_ref = f" (заявка #{order_id})" if order_id else ""

        return (
            f"Здравствуйте, {client}! 😊\n\n"
            f"Надеемся, что {event_type}{order_ref} прошло именно так, как вы планировали.\n\n"
            f"Нам очень важно ваше мнение! Не могли бы вы уделить 2 минуты и оставить "
            f"отзыв о нашей работе? Это поможет нам становиться лучше и поможет другим "
            f"клиентам сделать правильный выбор.\n\n"
            f"Если что-то пошло не так — расскажите нам напрямую, мы обязательно разберёмся.\n\n"
            f"Спасибо за доверие!\nNevesty Models 🌟"
        )

    # ------------------------------------------------------------------ #
    # suggest_upsell                                                       #
    # ------------------------------------------------------------------ #

    def suggest_upsell(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        """Return {suggestions, reason} based on event type and budget."""
        event_type = (order_data.get("event_type") or "").lower()
        budget = order_data.get("budget") or 0
        model_count = order_data.get("model_count") or 1

        suggestions: list[str] = []
        reason = "Стандартные рекомендации по расширению заказа."

        # Event-type based suggestions
        if "корпоратив" in event_type or "corporate" in event_type:
            suggestions.append("Добавьте хостес-модель для регистрации гостей")
            suggestions.append("Закажите профессионального фотографа в пакете")
            if model_count < 3:
                suggestions.append("Увеличьте команду до 3 моделей для максимального охвата")
            reason = "Корпоративные мероприятия выигрывают от расширенной команды и медиасопровождения."

        elif "свадьба" in event_type or "wedding" in event_type:
            suggestions.append("Добавьте модель-ассистента для работы с гостями")
            suggestions.append("Закажите видеосъёмку церемонии")
            reason = "Свадьба — уникальное событие. Дополнительные услуги сделают его незабываемым."

        elif "фотосессия" in event_type or "photo" in event_type:
            suggestions.append("Расширьте съёмку до full-day (8 часов вместо 4)")
            suggestions.append("Добавьте вторую модель для парных кадров")
            suggestions.append("Закажите профессиональный макияж и стилиста")
            reason = "Разнообразие образов и моделей значительно повышает ценность фотосессии."

        else:
            suggestions.append("Рассмотрите пакет «Всё включено» со стилистом")
            suggestions.append("Добавьте видеосъёмку для контента в соцсети")

        # Budget-based premium suggestion
        if budget and budget > 50_000:
            suggestions.append("Подключите персонального менеджера на весь день мероприятия")

        return {
            "suggestions": suggestions,
            "reason": reason,
        }

    # ------------------------------------------------------------------ #
    # execute_task — agent-style orchestrator entry point                  #
    # ------------------------------------------------------------------ #

    def execute_task(self, task: str, context: Dict[str, Any] | None) -> Dict[str, Any]:
        """Orchestrate all CS specialist roles for a given task.

        Returns a combined result with insights from every specialist,
        a list of roles_used, and a UTC timestamp.
        """
        ctx: Dict[str, Any] = context or {}
        task_lower = (task or "").lower()

        # Run all specialists and collect their insights
        onboarding = OnboardingSpecialist()
        retention = RetentionAnalyst()
        feedback = FeedbackCollector()
        upsell = UpsellAdvisor()

        onboarding_result = onboarding.run(ctx)
        retention_result = retention.run(ctx)
        feedback_result = feedback.run(ctx)
        upsell_result = upsell.run(ctx)

        all_insights: List[str] = []
        for r in (onboarding_result, retention_result, feedback_result, upsell_result):
            insights = r.get("insights", [])
            if isinstance(insights, list):
                all_insights.extend(insights)
            elif isinstance(insights, str):
                all_insights.append(insights)

        return {
            "task": task,
            "roles_used": [
                onboarding.role,
                retention.role,
                feedback.role,
                upsell.role,
            ],
            "insights": all_insights,
            "details": {
                "onboarding": onboarding_result,
                "retention": retention_result,
                "feedback": feedback_result,
                "upsell": upsell_result,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ══════════════════════════════════════════════════════════════════════════════
# Agent-style specialist classes
# Each wraps the heuristic logic and exposes a uniform .run(context) -> dict
# interface consumed by CustomerSuccessDepartment.execute_task above.
# ══════════════════════════════════════════════════════════════════════════════

class _BaseCSAgent:
    """Shared base for all Customer Success agent specialists."""

    department: str = "customer_success"
    role: str = "cs_agent"

    def run(self, context: Dict[str, Any] | None) -> Dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_ctx(context: Dict[str, Any] | None) -> Dict[str, Any]:
        return context or {}

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()


class OnboardingSpecialist(_BaseCSAgent):
    """Generates onboarding welcome messages for new clients."""

    role: str = "onboarding_specialist"

    def run(self, context: Dict[str, Any] | None) -> Dict[str, Any]:
        ctx = self._safe_ctx(context)
        kpis = ctx.get("nevesty_kpis", {}) or {}
        clients_total = kpis.get("clients_total", 0)

        insights: List[str] = [
            "Відправити вітальне повідомлення нового клієнту протягом 1 години після заявки.",
            "Призначити персонального менеджера для супроводу клієнта.",
        ]
        if clients_total and clients_total > 100:
            insights.append(
                f"Велика база ({clients_total} клієнтів): розгляньте автоматизацію онбордингу."
            )

        return {
            "role": self.role,
            "insights": insights,
            "timestamp": self._now_iso(),
        }


class RetentionAnalyst(_BaseCSAgent):
    """Analyses client retention risk and recommends re-engagement actions."""

    role: str = "retention_analyst"

    def run(self, context: Dict[str, Any] | None) -> Dict[str, Any]:
        ctx = self._safe_ctx(context)
        kpis = ctx.get("nevesty_kpis", {}) or {}
        repeat_rate = kpis.get("repeat_client_rate", None)

        insights: List[str] = [
            "Проаналізувати клієнтів, що не замовляли понад 90 днів.",
            "Запустити реактиваційну кампанію з персональними пропозиціями.",
        ]
        if repeat_rate is not None and repeat_rate < 20:
            insights.append(
                f"Повторний рейтинг клієнтів {repeat_rate}% — нижче норми. "
                "Переглянути програму лояльності."
            )

        return {
            "role": self.role,
            "insights": insights,
            "timestamp": self._now_iso(),
        }


class FeedbackCollector(_BaseCSAgent):
    """Collects and analyses client feedback after each event."""

    role: str = "feedback_collector"

    def run(self, context: Dict[str, Any] | None) -> Dict[str, Any]:
        ctx = self._safe_ctx(context)
        kpis = ctx.get("nevesty_kpis", {}) or {}
        orders_month = kpis.get("orders_this_month", 0)

        insights: List[str] = [
            "Надіслати запит на відгук через 24 години після заходу.",
            "Збирати NPS-оцінки для відстеження задоволеності клієнтів.",
        ]
        if orders_month and orders_month > 20:
            insights.append(
                f"Цього місяця {orders_month} замовлень: розгляньте автоматизований збір відгуків."
            )

        return {
            "role": self.role,
            "insights": insights,
            "timestamp": self._now_iso(),
        }


class UpsellAdvisor(_BaseCSAgent):
    """Recommends upsell and cross-sell opportunities based on client data."""

    role: str = "upsell_advisor"

    def run(self, context: Dict[str, Any] | None) -> Dict[str, Any]:
        ctx = self._safe_ctx(context)
        kpis = ctx.get("nevesty_kpis", {}) or {}
        avg_check = kpis.get("avg_check", 0)

        insights: List[str] = [
            "Пропонуйте пакет «Все включено» клієнтам з бюджетом понад 30 000 грн.",
            "Рекомендуйте додаткового фотографа або стиліста при корпоративних подіях.",
        ]
        if avg_check and avg_check > 50_000:
            insights.append(
                f"Середній чек {avg_check} грн — висока цінність. "
                "Запропонуйте VIP-пакет з особистим менеджером."
            )

        return {
            "role": self.role,
            "insights": insights,
            "timestamp": self._now_iso(),
        }
