"""Sales Department — heuristic agents (no external API calls)."""
from __future__ import annotations
import datetime
import random
from datetime import timezone, timedelta
from typing import Any, Dict, List

class LeadQualifier:
    """Scores and qualifies incoming booking leads."""

    SCORING_WEIGHTS = {
        'has_budget': 25,
        'has_date': 20,
        'has_event_type': 15,
        'has_contact': 15,
        'is_repeat_client': 25,
    }

    def score_lead(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        """Score a lead 0-100 and return tier."""
        score = 0
        reasons = []

        if order_data.get('budget') and float(str(order_data['budget']).replace(' ', '') or 0) > 0:
            score += self.SCORING_WEIGHTS['has_budget']
            reasons.append('budget specified')

        if order_data.get('event_date'):
            score += self.SCORING_WEIGHTS['has_date']
            reasons.append('date specified')

        if order_data.get('event_type') and order_data['event_type'] != 'other':
            score += self.SCORING_WEIGHTS['has_event_type']
            reasons.append('event type known')

        if order_data.get('client_phone') or order_data.get('client_email'):
            score += self.SCORING_WEIGHTS['has_contact']
            reasons.append('contact info provided')

        if order_data.get('is_repeat', False):
            score += self.SCORING_WEIGHTS['is_repeat_client']
            reasons.append('repeat client')

        if score >= 75:
            tier = 'hot'
        elif score >= 50:
            tier = 'warm'
        elif score >= 25:
            tier = 'cold'
        else:
            tier = 'unqualified'

        return {
            'score': score,
            'tier': tier,
            'reasons': reasons,
            'recommended_action': self._recommend_action(tier)
        }

    def _recommend_action(self, tier: str) -> str:
        actions = {
            'hot': 'Contact within 1 hour, offer model selection call',
            'warm': 'Send catalog with top 3 recommended models',
            'cold': 'Send welcome package, follow up in 48h',
            'unqualified': 'Request more information, do not assign manager yet'
        }
        return actions.get(tier, 'Standard follow-up')

    def batch_qualify(self, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Qualify multiple orders, sorted by score descending."""
        results = [{'order_id': o.get('id'), **self.score_lead(o)} for o in orders]
        return sorted(results, key=lambda x: x['score'], reverse=True)


class ProposalWriter:
    """Generates customized booking proposals."""

    EVENT_TEMPLATES = {
        'корпоратив': {
            'hook': 'Сделайте ваш корпоратив незабываемым с профессиональными моделями',
            'benefits': ['Элегантное обслуживание гостей', 'Фотозоны с моделями', 'Ведение мероприятия'],
            'upsell': 'Добавьте фотографа для создания контента'
        },
        'фотосессия': {
            'hook': 'Создайте профессиональный контент с топ-моделями агентства',
            'benefits': ['Опыт в коммерческой съёмке', 'Умение работать с камерой', 'Разнообразие стилей'],
            'upsell': 'Закажите пакет из 3 моделей со скидкой 15%'
        },
        'показ': {
            'hook': 'Профессиональные подиумные модели для вашего показа',
            'benefits': ['Подиумная подготовка', 'Работа с дизайнерами', 'Командная синхронность'],
            'upsell': 'Предложите кастинг для выбора идеального состава'
        },
    }

    def generate_proposal(self, order_data: dict[str, Any], model_options: list[dict]) -> dict[str, Any]:
        """Generate a personalized proposal."""
        event_type = (order_data.get('event_type') or '').lower()

        template = None
        for key in self.EVENT_TEMPLATES:
            if key in event_type:
                template = self.EVENT_TEMPLATES[key]
                break

        if not template:
            template = {
                'hook': 'Профессиональные модели для вашего мероприятия',
                'benefits': ['Опыт работы', 'Пунктуальность', 'Профессионализм'],
                'upsell': 'Свяжитесь с нами для индивидуального предложения'
            }

        client_name = order_data.get('client_name', 'Уважаемый клиент')

        proposal = {
            'greeting': f"Здравствуйте, {client_name}!",
            'hook': template['hook'],
            'benefits': template['benefits'],
            'model_count': len(model_options),
            'top_model': model_options[0] if model_options else None,
            'upsell': template['upsell'],
            'cta': 'Забронируйте прямо сейчас — даты заполняются быстро',
            'validity_hours': 48
        }
        return proposal

    def format_proposal_text(self, proposal: dict[str, Any]) -> str:
        """Format proposal as readable text."""
        lines = [
            proposal['greeting'],
            '',
            proposal['hook'],
            '',
            'Что вы получите:',
        ]
        for b in proposal.get('benefits', []):
            lines.append(f'  ✓ {b}')
        lines.extend([
            '',
            f"Доступно {proposal['model_count']} моделей на ваши даты.",
            '',
            f"💡 {proposal['upsell']}",
            '',
            f"🎯 {proposal['cta']}",
            '',
            f"Предложение действительно {proposal['validity_hours']} часа."
        ])
        return '\n'.join(lines)


class FollowUpSpecialist:
    """Manages follow-up sequences for unconverted leads."""

    FOLLOW_UP_SEQUENCES = {
        'hot': [
            {'delay_hours': 2, 'message': 'quick_check', 'channel': 'telegram'},
            {'delay_hours': 24, 'message': 'value_add', 'channel': 'telegram'},
            {'delay_hours': 72, 'message': 'last_chance', 'channel': 'telegram'},
        ],
        'warm': [
            {'delay_hours': 24, 'message': 'catalog_share', 'channel': 'telegram'},
            {'delay_hours': 96, 'message': 'testimonial', 'channel': 'telegram'},
            {'delay_hours': 168, 'message': 'special_offer', 'channel': 'telegram'},
        ],
        'cold': [
            {'delay_hours': 72, 'message': 'educational', 'channel': 'telegram'},
            {'delay_hours': 336, 'message': 'reconnect', 'channel': 'telegram'},
        ]
    }

    MESSAGES = {
        'quick_check': 'Добрый день! Успели ознакомиться с нашими предложениями? Готовы ответить на любые вопросы.',
        'value_add': 'Поделились интересной статьёй о трендах в event-индустрии. Также у нас появились новые модели!',
        'last_chance': 'Даты на следующий месяц заполняются. Хотите зафиксировать дату для вашего мероприятия?',
        'catalog_share': 'Подготовили для вас подборку моделей, подходящих под ваш формат мероприятия.',
        'testimonial': 'Наши клиенты отмечают профессионализм и пунктуальность наших моделей.',
        'special_offer': 'Специально для вас — скидка 10% на первое бронирование в этом месяце.',
        'educational': 'Рассказываем, как выбрать правильный тип модели для вашего мероприятия.',
        'reconnect': 'Мы готовы помочь сделать ваше следующее мероприятие незабываемым.'
    }

    def get_follow_up_schedule(self, lead_tier: str, order_created_at: str) -> list[dict[str, Any]]:
        """Return scheduled follow-up messages with absolute timestamps."""
        sequence = self.FOLLOW_UP_SEQUENCES.get(lead_tier, self.FOLLOW_UP_SEQUENCES['cold'])
        try:
            base_time = datetime.datetime.fromisoformat(order_created_at.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            base_time = datetime.datetime.now()

        schedule = []
        for step in sequence:
            send_at = base_time + datetime.timedelta(hours=step['delay_hours'])
            schedule.append({
                'send_at': send_at.isoformat(),
                'channel': step['channel'],
                'message': self.MESSAGES.get(step['message'], step['message']),
                'message_key': step['message']
            })
        return schedule

    def get_optimal_send_time(self, base_hour: int = 10) -> dict[str, int]:
        """Return optimal send time (10-12am or 5-7pm)."""
        if 10 <= base_hour <= 12:
            return {'hour': base_hour, 'reason': 'morning_peak'}
        elif 17 <= base_hour <= 19:
            return {'hour': base_hour, 'reason': 'evening_peak'}
        elif base_hour < 10:
            return {'hour': 10, 'reason': 'morning_peak'}
        else:
            return {'hour': 17, 'reason': 'evening_peak'}


class PricingNegotiator:
    """Handles dynamic pricing and negotiation strategies."""

    BASE_RATES = {
        'fashion': {'hourly': 3000, 'half_day': 10000, 'full_day': 18000},
        'commercial': {'hourly': 2500, 'half_day': 8000, 'full_day': 14000},
        'events': {'hourly': 2000, 'half_day': 7000, 'full_day': 12000},
        'default': {'hourly': 2500, 'half_day': 8500, 'full_day': 15000}
    }

    DISCOUNT_RULES = [
        {'condition': 'repeat_client', 'discount_pct': 10, 'label': 'Скидка постоянного клиента'},
        {'condition': 'multiple_models', 'discount_pct': 5, 'label': 'Скидка за несколько моделей'},
        {'condition': 'advance_booking', 'discount_pct': 7, 'label': 'Скидка за раннее бронирование'},
        {'condition': 'off_peak', 'discount_pct': 8, 'label': 'Скидка не в сезон'},
    ]

    def calculate_quote(self, duration_hours: float, model_count: int, category: str = 'default',
                        is_repeat: bool = False, advance_days: int = 0) -> dict[str, Any]:
        """Calculate a pricing quote with applicable discounts."""
        rates = self.BASE_RATES.get(category, self.BASE_RATES['default'])

        if duration_hours <= 2:
            base = rates['hourly'] * duration_hours
        elif duration_hours <= 5:
            base = rates['half_day']
        else:
            base = rates['full_day']

        base *= model_count

        applicable_discounts = []
        total_discount_pct = 0

        if is_repeat:
            applicable_discounts.append(self.DISCOUNT_RULES[0])
            total_discount_pct += self.DISCOUNT_RULES[0]['discount_pct']
        if model_count >= 3:
            applicable_discounts.append(self.DISCOUNT_RULES[1])
            total_discount_pct += self.DISCOUNT_RULES[1]['discount_pct']
        if advance_days >= 14:
            applicable_discounts.append(self.DISCOUNT_RULES[2])
            total_discount_pct += self.DISCOUNT_RULES[2]['discount_pct']

        # Cap discount at 25%
        total_discount_pct = min(25, total_discount_pct)
        discount_amount = base * total_discount_pct / 100
        final_price = base - discount_amount

        return {
            'base_price': round(base),
            'discount_pct': total_discount_pct,
            'discount_amount': round(discount_amount),
            'final_price': round(final_price),
            'applicable_discounts': [d['label'] for d in applicable_discounts],
            'currency': 'RUB',
            'category': category,
            'duration_hours': duration_hours,
            'model_count': model_count
        }

    def suggest_counter_offer(self, client_budget: float, calculated_price: float) -> dict[str, Any]:
        """Suggest a counter-offer when client budget is below calculated price."""
        gap_pct = (calculated_price - client_budget) / calculated_price * 100

        if gap_pct <= 0:
            return {'action': 'accept', 'message': 'Budget meets or exceeds our price', 'adjusted_price': calculated_price}
        elif gap_pct <= 10:
            return {
                'action': 'accept_with_note',
                'message': 'Minor gap — offer to waive booking fee',
                'adjusted_price': calculated_price * 0.95
            }
        elif gap_pct <= 25:
            return {
                'action': 'negotiate',
                'message': 'Suggest reducing duration or model count',
                'adjusted_price': client_budget * 1.05
            }
        else:
            return {
                'action': 'decline_politely',
                'message': 'Budget significantly below minimum — suggest alternative options',
                'adjusted_price': None
            }


class SalesDepartment:
    """Unified Sales Department facade — wraps specialist classes."""

    BASE_PRICES = {
        'корпоратив':  (15000, 35000),
        'фотосессия':  (10000, 25000),
        'свадьба':     (20000, 45000),
        'показ':       (12000, 30000),
        'реклама':     (18000, 40000),
    }
    _DEFAULT = (10000, 20000)

    def __init__(self) -> None:
        self._qualifier = LeadQualifier()
        self._writer = ProposalWriter()
        self._followup = FollowUpSpecialist()
        self._negotiator = PricingNegotiator()

    # ── qualify_lead ──────────────────────────────────────────────────────────
    def qualify_lead(self, order_data: dict[str, Any]) -> dict[str, Any]:
        """Return score (0-100), tier (premium/standard/economy), and notes."""
        budget = 0
        try:
            budget = float(str(order_data.get('budget') or 0).replace(' ', ''))
        except (ValueError, TypeError):
            pass

        score = 0
        notes_parts: list[str] = []

        # Budget scoring
        if budget >= 100_000:
            score += 75
            notes_parts.append('высокий бюджет')
        elif budget >= 30_000:
            score += 45
            notes_parts.append('средний бюджет')
        elif budget > 0:
            score += 20
            notes_parts.append('минимальный бюджет')

        # Event type bonus
        et = str(order_data.get('event_type') or '').lower()
        if 'корпоратив' in et:
            score += 15
            notes_parts.append('корпоративный клиент')
        elif et and et != 'other':
            score += 5

        # Urgency bonus (event date within 14 days)
        date_str = order_data.get('date') or order_data.get('event_date') or ''
        if date_str:
            try:
                event_dt = datetime.date.fromisoformat(str(date_str))
                days_left = (event_dt - datetime.date.today()).days
                if 0 < days_left <= 14:
                    score += 10
                    notes_parts.append('срочный заказ')
            except (ValueError, TypeError):
                pass

        score = min(100, score)

        if score >= 80:
            tier = 'premium'
        elif score >= 50:
            tier = 'standard'
        else:
            tier = 'economy'

        return {
            'score': score,
            'tier': tier,
            'notes': '; '.join(notes_parts) if notes_parts else 'стандартный клиент',
        }

    # ── generate_proposal ─────────────────────────────────────────────────────
    def generate_proposal(self, order_data: dict[str, Any]) -> str:
        """Return a text proposal for the client."""
        client = order_data.get('client_name') or 'Уважаемый клиент'
        et = order_data.get('event_type') or 'мероприятие'
        proposal = self._writer.generate_proposal(order_data, [])
        text = self._writer.format_proposal_text(proposal)
        # Always prepend header with client name and event type
        header = f"Здравствуйте, {client}!\nТип мероприятия: {et}\n\n"
        return header + text

    # ── get_followup_schedule ─────────────────────────────────────────────────
    def get_followup_schedule(self, order_id: int, status: str) -> list[dict[str, Any]]:
        """Return follow-up messages based on order status."""
        tier_map = {
            'new': 'hot',
            'processing': 'warm',
            'reviewing': 'warm',
            'confirmed': 'warm',
            'completed': 'cold',
            'cancelled': 'cold',
        }
        tier = tier_map.get(status, 'cold')
        base_time = datetime.datetime.now().isoformat()
        return self._followup.get_follow_up_schedule(tier, base_time)

    # ── suggest_pricing ───────────────────────────────────────────────────────
    def suggest_pricing(self, params: dict[str, Any]) -> dict[str, Any]:
        """Return {min_price, max_price, recommended} for given params."""
        et = str(params.get('event_type') or '').lower()
        count = max(1, int(params.get('model_count') or 1))

        base_min, base_max = self._DEFAULT
        for key, (lo, hi) in self.BASE_PRICES.items():
            if key in et:
                base_min, base_max = lo, hi
                break

        min_price = base_min * count
        max_price = base_max * count
        recommended = int(min_price + (max_price - min_price) * 0.4)

        return {
            'min_price': min_price,
            'max_price': max_price,
            'recommended': recommended,
            'currency': 'RUB',
        }
