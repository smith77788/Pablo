"""Tests for research_department.py standalone heuristic agents."""
from __future__ import annotations
import pytest
from factory.agents.research_department import (
    MarketResearcher,
    CompetitorAnalyst,
    TrendSpotter,
    InsightSynthesizer,
)


class TestMarketResearcher:
    def setup_method(self):
        self.researcher = MarketResearcher()

    def test_analyze_market_segment_returns_dict(self):
        result = self.researcher.analyze_market_segment("fashion")
        assert isinstance(result, dict)
        assert result["segment"] == "fashion"
        assert "market_size_rub" in result
        assert "annual_growth_pct" in result
        assert "competition_level" in result
        assert "opportunity_score" in result

    def test_analyze_market_segment_commercial(self):
        result = self.researcher.analyze_market_segment("commercial")
        assert result["segment"] == "commercial"
        assert result["market_size_rub"] > 0
        assert 0 <= result["opportunity_score"] <= 100

    def test_analyze_market_segment_unknown_defaults_to_commercial(self):
        result = self.researcher.analyze_market_segment("unknown_segment")
        assert isinstance(result, dict)
        assert result["opportunity_score"] >= 0

    def test_estimate_addressable_market_moscow(self):
        result = self.researcher.estimate_addressable_market("Москва", "fashion")
        assert result["city"] == "Москва"
        assert result["tam_rub"] > 0
        assert result["sam_rub"] < result["tam_rub"]
        assert result["som_rub"] < result["sam_rub"]

    def test_estimate_addressable_market_small_city(self):
        result = self.researcher.estimate_addressable_market("Воронеж", "events")
        assert result["tam_rub"] > 0
        assert result["city_multiplier"] == 0.10

    def test_opportunity_score_range(self):
        for segment in ["fashion", "commercial", "events", "promo"]:
            result = self.researcher.analyze_market_segment(segment)
            assert 0 <= result["opportunity_score"] <= 100


class TestCompetitorAnalyst:
    def setup_method(self):
        self.analyst = CompetitorAnalyst()

    def test_identify_competitive_gaps_returns_list(self):
        gaps = self.analyst.identify_competitive_gaps(["fashion", "events"])
        assert isinstance(gaps, list)
        assert len(gaps) > 0

    def test_gaps_sorted_by_available_share(self):
        gaps = self.analyst.identify_competitive_gaps([])
        shares = [g["available_share"] for g in gaps]
        assert shares == sorted(shares, reverse=True)

    def test_gap_has_required_keys(self):
        gaps = self.analyst.identify_competitive_gaps(["fashion"])
        for gap in gaps:
            assert "segment" in gap
            assert "competitor_count" in gap
            assert "available_share" in gap
            assert "opportunity" in gap
            assert gap["opportunity"] in ("high", "medium", "low")

    def test_benchmark_pricing_premium(self):
        result = self.analyst.benchmark_pricing(70_000, "fashion")
        assert result["market_position"] in ("premium", "luxury")
        assert "recommendation" in result
        assert "benchmarks" in result

    def test_benchmark_pricing_budget(self):
        result = self.analyst.benchmark_pricing(5_000, "fashion")
        assert result["market_position"] in ("budget", "mid")
        assert result["recommendation"] == "increase"

    def test_benchmark_pricing_mid_market(self):
        result = self.analyst.benchmark_pricing(25_000, "fashion")
        assert result["recommendation"] == "maintain"


class TestTrendSpotter:
    def setup_method(self):
        self.spotter = TrendSpotter()

    def test_get_actionable_trends_returns_list(self):
        trends = self.spotter.get_actionable_trends()
        assert isinstance(trends, list)
        assert len(trends) > 0

    def test_trends_have_required_keys(self):
        trends = self.spotter.get_actionable_trends()
        for t in trends:
            assert "name" in t
            assert "impact" in t
            assert "action" in t
            assert t["impact"] in ("high", "medium", "low")

    def test_trends_sorted_by_impact(self):
        trends = self.spotter.get_actionable_trends()
        high_before_medium = True
        seen_medium = False
        for t in trends:
            if t["impact"] == "medium":
                seen_medium = True
            if seen_medium and t["impact"] == "high":
                high_before_medium = False
        assert high_before_medium

    def test_filter_by_focus_area(self):
        trends = self.spotter.get_actionable_trends(focus_area="marketing")
        assert isinstance(trends, list)

    def test_score_trend_relevance_known_trend(self):
        result = self.spotter.score_trend_relevance("AI-assisted model selection", {"team_size": 5})
        assert isinstance(result, dict)
        assert "score" in result
        assert 0 <= result["score"] <= 100
        assert "priority" in result

    def test_score_trend_relevance_unknown_trend(self):
        result = self.spotter.score_trend_relevance("Completely Unknown Trend XYZ", {})
        assert result["score"] == 0
        assert "reason" in result


class TestInsightSynthesizer:
    def setup_method(self):
        self.synthesizer = InsightSynthesizer()

    def test_synthesize_insights_returns_dict(self):
        market_data = {"segment": "fashion", "market_size_rub": 1_000_000}
        competitor_gaps = [{"segment": "promo", "available_share": 0.75, "opportunity": "high"}]
        trends = [{"name": "AI tools", "action": "build AI"}]
        perf = {"conversion_rate": 0.25, "avg_budget": 15_000}
        result = self.synthesizer.synthesize_insights(market_data, competitor_gaps, trends, perf)
        assert isinstance(result, dict)
        assert "executive_summary" in result
        assert "top_opportunities" in result
        assert "strategic_alerts" in result

    def test_alerts_for_low_conversion(self):
        result = self.synthesizer.synthesize_insights({}, [], [], {"conversion_rate": 0.1})
        assert any("конверси" in a.lower() for a in result["strategic_alerts"])

    def test_alerts_for_low_budget(self):
        result = self.synthesizer.synthesize_insights({}, [], [], {"avg_budget": 5_000})
        assert any("чек" in a.lower() for a in result["strategic_alerts"])

    def test_no_alerts_for_good_metrics(self):
        result = self.synthesizer.synthesize_insights({}, [], [], {"conversion_rate": 0.5, "avg_budget": 50_000})
        assert len(result["strategic_alerts"]) == 0

    def test_generate_weekly_insight_report_returns_str(self):
        report = self.synthesizer.generate_weekly_insight_report({
            "orders_this_week": 10,
            "conversion_rate": 0.35,
            "top_segment": "fashion",
            "revenue_growth_pct": 8.5,
        })
        assert isinstance(report, str)
        assert "отчёт" in report.lower() or "Отчёт" in report
        assert "10" in report

    def test_report_positive_growth_indicator(self):
        report = self.synthesizer.generate_weekly_insight_report({"revenue_growth_pct": 10.0})
        assert "↑" in report

    def test_report_negative_growth_indicator(self):
        report = self.synthesizer.generate_weekly_insight_report({"revenue_growth_pct": -5.0})
        assert "↓" in report
