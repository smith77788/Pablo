"""Tests for Sales Department agents."""
import pytest
from factory.agents.sales_department import (
    LeadQualifier, ProposalWriter, FollowUpSpecialist, PricingNegotiator
)

class TestLeadQualifier:
    def setup_method(self):
        self.q = LeadQualifier()

    def test_empty_order_is_unqualified(self):
        result = self.q.score_lead({})
        assert result['tier'] == 'unqualified'
        assert result['score'] == 0

    def test_full_order_is_hot(self):
        result = self.q.score_lead({
            'budget': 50000, 'event_date': '2026-06-01', 'event_type': 'корпоратив',
            'client_phone': '+7999', 'is_repeat': True
        })
        assert result['tier'] == 'hot'
        assert result['score'] == 100

    def test_budget_only_is_cold(self):
        result = self.q.score_lead({'budget': 10000})
        assert result['score'] == 25
        assert result['tier'] == 'cold'

    def test_repeat_client_boosts_score(self):
        r1 = self.q.score_lead({'budget': 10000})
        r2 = self.q.score_lead({'budget': 10000, 'is_repeat': True})
        assert r2['score'] > r1['score']

    def test_hot_tier_threshold(self):
        result = self.q.score_lead({
            'budget': 10000, 'event_date': '2026-07-01', 'event_type': 'fashion',
            'client_phone': '+7'
        })
        assert result['score'] >= 75
        assert result['tier'] == 'hot'

    def test_returns_reasons_list(self):
        result = self.q.score_lead({'budget': 5000})
        assert isinstance(result['reasons'], list)
        assert len(result['reasons']) >= 1

    def test_returns_recommended_action(self):
        result = self.q.score_lead({})
        assert isinstance(result['recommended_action'], str)
        assert len(result['recommended_action']) > 0

    def test_batch_qualify_sorted_desc(self):
        orders = [
            {'id': 1},
            {'id': 2, 'budget': 10000, 'event_date': '2026-06-01', 'event_type': 'корпоратив', 'client_phone': '+7', 'is_repeat': True},
            {'id': 3, 'budget': 5000}
        ]
        results = self.q.batch_qualify(orders)
        assert len(results) == 3
        assert results[0]['score'] >= results[1]['score'] >= results[2]['score']

    def test_batch_qualify_includes_order_id(self):
        results = self.q.batch_qualify([{'id': 42}])
        assert results[0]['order_id'] == 42

    def test_warm_tier(self):
        result = self.q.score_lead({'budget': 10000, 'event_date': '2026-06-01', 'event_type': 'фото'})
        assert result['score'] == 60
        assert result['tier'] == 'warm'

    def test_event_type_other_not_scored(self):
        r1 = self.q.score_lead({'event_type': 'other'})
        r2 = self.q.score_lead({'event_type': 'корпоратив'})
        assert r2['score'] > r1['score']

    def test_email_counts_as_contact(self):
        r1 = self.q.score_lead({})
        r2 = self.q.score_lead({'client_email': 'test@test.com'})
        assert r2['score'] > r1['score']


class TestProposalWriter:
    def setup_method(self):
        self.pw = ProposalWriter()

    def test_generate_proposal_returns_dict(self):
        result = self.pw.generate_proposal({'event_type': 'корпоратив', 'client_name': 'Иван'}, [])
        assert isinstance(result, dict)

    def test_greeting_includes_client_name(self):
        result = self.pw.generate_proposal({'client_name': 'Мария'}, [])
        assert 'Мария' in result['greeting']

    def test_model_count_in_proposal(self):
        models = [{'id': 1}, {'id': 2}]
        result = self.pw.generate_proposal({}, models)
        assert result['model_count'] == 2

    def test_top_model_is_first(self):
        models = [{'id': 10}, {'id': 20}]
        result = self.pw.generate_proposal({}, models)
        assert result['top_model']['id'] == 10

    def test_top_model_none_when_empty(self):
        result = self.pw.generate_proposal({}, [])
        assert result['top_model'] is None

    def test_benefits_is_list(self):
        result = self.pw.generate_proposal({'event_type': 'фотосессия'}, [])
        assert isinstance(result['benefits'], list)
        assert len(result['benefits']) >= 1

    def test_validity_hours_positive(self):
        result = self.pw.generate_proposal({}, [])
        assert result['validity_hours'] > 0

    def test_format_proposal_text_is_string(self):
        proposal = self.pw.generate_proposal({'event_type': 'корпоратив', 'client_name': 'Тест'}, [])
        text = self.pw.format_proposal_text(proposal)
        assert isinstance(text, str)
        assert len(text) > 50

    def test_format_includes_cta(self):
        proposal = self.pw.generate_proposal({}, [])
        text = self.pw.format_proposal_text(proposal)
        assert proposal['cta'] in text

    def test_unknown_event_uses_default_template(self):
        result = self.pw.generate_proposal({'event_type': 'неизвестный тип'}, [])
        assert 'hook' in result
        assert len(result['hook']) > 0

    def test_корпоратив_template_applied(self):
        result = self.pw.generate_proposal({'event_type': 'корпоратив'}, [])
        assert 'корпоратив' in result['hook'].lower() or len(result['benefits']) > 0

    def test_показ_template_applied(self):
        result = self.pw.generate_proposal({'event_type': 'показ'}, [])
        assert result['benefits']


class TestFollowUpSpecialist:
    def setup_method(self):
        self.fu = FollowUpSpecialist()

    def test_hot_has_3_steps(self):
        schedule = self.fu.get_follow_up_schedule('hot', '2026-05-01T10:00:00')
        assert len(schedule) == 3

    def test_warm_has_3_steps(self):
        schedule = self.fu.get_follow_up_schedule('warm', '2026-05-01T10:00:00')
        assert len(schedule) == 3

    def test_cold_has_2_steps(self):
        schedule = self.fu.get_follow_up_schedule('cold', '2026-05-01T10:00:00')
        assert len(schedule) == 2

    def test_unknown_tier_defaults_to_cold(self):
        schedule = self.fu.get_follow_up_schedule('unknown', '2026-05-01T10:00:00')
        assert len(schedule) == 2

    def test_schedule_has_send_at(self):
        schedule = self.fu.get_follow_up_schedule('hot', '2026-05-01T10:00:00')
        for step in schedule:
            assert 'send_at' in step
            assert isinstance(step['send_at'], str)

    def test_schedule_chronological_order(self):
        schedule = self.fu.get_follow_up_schedule('hot', '2026-05-01T10:00:00')
        times = [step['send_at'] for step in schedule]
        assert times == sorted(times)

    def test_schedule_has_message(self):
        schedule = self.fu.get_follow_up_schedule('warm', '2026-05-01T10:00:00')
        for step in schedule:
            assert 'message' in step
            assert len(step['message']) > 10

    def test_optimal_send_time_morning(self):
        result = self.fu.get_optimal_send_time(11)
        assert result['hour'] == 11
        assert result['reason'] == 'morning_peak'

    def test_optimal_send_time_evening(self):
        result = self.fu.get_optimal_send_time(18)
        assert result['hour'] == 18
        assert result['reason'] == 'evening_peak'

    def test_optimal_send_time_early_morning_adjusted(self):
        result = self.fu.get_optimal_send_time(6)
        assert result['hour'] == 10

    def test_optimal_send_time_late_evening_adjusted(self):
        result = self.fu.get_optimal_send_time(22)
        assert result['hour'] == 17

    def test_invalid_date_does_not_crash(self):
        schedule = self.fu.get_follow_up_schedule('hot', 'invalid-date')
        assert len(schedule) == 3


class TestPricingNegotiator:
    def setup_method(self):
        self.pn = PricingNegotiator()

    def test_calculate_quote_returns_dict(self):
        result = self.pn.calculate_quote(4, 1, 'events')
        assert isinstance(result, dict)

    def test_quote_has_required_fields(self):
        result = self.pn.calculate_quote(4, 1)
        for field in ('base_price', 'final_price', 'discount_pct', 'currency'):
            assert field in result

    def test_currency_is_rub(self):
        result = self.pn.calculate_quote(4, 1)
        assert result['currency'] == 'RUB'

    def test_hourly_rate_for_short_jobs(self):
        r1 = self.pn.calculate_quote(1, 1, 'events')
        r2 = self.pn.calculate_quote(2, 1, 'events')
        assert r2['base_price'] > r1['base_price']

    def test_half_day_rate(self):
        result = self.pn.calculate_quote(3, 1, 'events')
        assert result['base_price'] == 7000

    def test_full_day_rate(self):
        result = self.pn.calculate_quote(8, 1, 'events')
        assert result['base_price'] == 12000

    def test_multiple_models_multiplies_price(self):
        r1 = self.pn.calculate_quote(4, 1, 'events')
        r2 = self.pn.calculate_quote(4, 2, 'events')
        assert r2['base_price'] == r1['base_price'] * 2

    def test_repeat_client_discount(self):
        r1 = self.pn.calculate_quote(4, 1, 'events', is_repeat=False)
        r2 = self.pn.calculate_quote(4, 1, 'events', is_repeat=True)
        assert r2['final_price'] < r1['final_price']

    def test_advance_booking_discount(self):
        r1 = self.pn.calculate_quote(4, 1, advance_days=0)
        r2 = self.pn.calculate_quote(4, 1, advance_days=30)
        assert r2['final_price'] < r1['final_price']

    def test_multiple_models_discount_applies_at_3(self):
        r2 = self.pn.calculate_quote(4, 2, is_repeat=False)
        r3 = self.pn.calculate_quote(4, 3, is_repeat=False)
        assert r3['discount_pct'] > r2['discount_pct']

    def test_discount_capped_at_25_pct(self):
        result = self.pn.calculate_quote(4, 5, is_repeat=True, advance_days=30)
        assert result['discount_pct'] <= 25

    def test_final_price_less_than_base(self):
        result = self.pn.calculate_quote(4, 1, is_repeat=True)
        assert result['final_price'] <= result['base_price']

    def test_counter_offer_accept_when_budget_ok(self):
        result = self.pn.suggest_counter_offer(15000, 10000)
        assert result['action'] == 'accept'

    def test_counter_offer_negotiate_when_close(self):
        result = self.pn.suggest_counter_offer(9000, 10000)  # 10% gap
        assert result['action'] in ('accept_with_note', 'negotiate')

    def test_counter_offer_decline_when_too_low(self):
        result = self.pn.suggest_counter_offer(5000, 15000)  # 66% gap
        assert result['action'] == 'decline_politely'

    def test_counter_offer_has_message(self):
        result = self.pn.suggest_counter_offer(10000, 12000)
        assert isinstance(result['message'], str)

    def test_unknown_category_uses_default(self):
        result = self.pn.calculate_quote(4, 1, 'unknown_category')
        assert result['final_price'] > 0

    def test_applicable_discounts_list(self):
        result = self.pn.calculate_quote(4, 3, is_repeat=True)
        assert isinstance(result['applicable_discounts'], list)
