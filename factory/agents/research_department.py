"""
Research Department — Market research, competitor analysis, trend spotting, insight synthesis.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any


class MarketResearcher:
    """Researches market conditions and sizing for modeling agency."""

    MARKET_SEGMENTS: dict[str, dict[str, Any]] = {
        "fashion": {"size_rub": 2_500_000_000, "growth_pct": 8.5, "competition": "high"},
        "commercial": {"size_rub": 1_800_000_000, "growth_pct": 12.0, "competition": "medium"},
        "events": {"size_rub": 3_200_000_000, "growth_pct": 6.0, "competition": "high"},
        "promo": {"size_rub": 900_000_000, "growth_pct": 15.0, "competition": "low"},
    }

    def analyze_market_segment(self, segment: str) -> dict[str, Any]:
        """Return market size, growth rate and competition level for a segment."""
        seg = segment.lower().strip()
        data = self.MARKET_SEGMENTS.get(seg, self.MARKET_SEGMENTS["commercial"])
        return {
            "segment": seg,
            "market_size_rub": data["size_rub"],
            "annual_growth_pct": data["growth_pct"],
            "competition_level": data["competition"],
            "opportunity_score": self._opportunity_score(data),
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
        }

    def _opportunity_score(self, data: dict[str, Any]) -> int:
        competition_scores = {"low": 30, "medium": 20, "high": 10}
        base = competition_scores.get(data["competition"], 15)
        growth_bonus = min(int(data["growth_pct"] * 2), 40)
        size_bonus = min(int(data["size_rub"] / 100_000_000), 30)
        return min(base + growth_bonus + size_bonus, 100)

    def estimate_addressable_market(self, city: str, segment: str) -> dict[str, Any]:
        """Estimate total addressable market (TAM) for a city and segment."""
        city_multipliers: dict[str, float] = {
            "москва": 1.0,
            "санкт-петербург": 0.55,
            "екатеринбург": 0.20,
            "новосибирск": 0.18,
            "казань": 0.15,
        }
        multiplier = city_multipliers.get(city.lower().strip(), 0.10)
        market_data = self.MARKET_SEGMENTS.get(segment.lower(), self.MARKET_SEGMENTS["commercial"])
        tam = market_data["size_rub"] * multiplier
        sam = tam * 0.15
        som = sam * 0.05
        return {
            "city": city,
            "segment": segment,
            "tam_rub": int(tam),
            "sam_rub": int(sam),
            "som_rub": int(som),
            "city_multiplier": multiplier,
        }


class CompetitorAnalyst:
    """Analyzes competitor landscape for modeling agency market."""

    COMPETITOR_PROFILES: list[dict[str, Any]] = [
        {"name": "TopModels Agency", "strength": "fashion", "pricing": "premium", "market_share": 0.12},
        {"name": "City Events Models", "strength": "events", "pricing": "mid", "market_share": 0.08},
        {"name": "PromoGirls", "strength": "promo", "pricing": "budget", "market_share": 0.05},
        {"name": "Elite Models Moscow", "strength": "fashion", "pricing": "luxury", "market_share": 0.18},
    ]

    def identify_competitive_gaps(self, our_strengths: list[str]) -> list[dict[str, Any]]:
        """Find segments where competition is weak and we can grow."""
        all_strengths = {c["strength"] for c in self.COMPETITOR_PROFILES}
        gaps = []
        for segment in ["fashion", "commercial", "events", "promo"]:
            competitors_in_segment = [c for c in self.COMPETITOR_PROFILES if c["strength"] == segment]
            total_share = sum(c["market_share"] for c in competitors_in_segment)
            we_play = segment in [s.lower() for s in our_strengths]
            gaps.append({
                "segment": segment,
                "competitor_count": len(competitors_in_segment),
                "competitor_share": round(total_share, 3),
                "available_share": round(1.0 - total_share, 3),
                "we_compete": we_play,
                "opportunity": "high" if total_share < 0.25 else "medium" if total_share < 0.5 else "low",
            })
        return sorted(gaps, key=lambda x: x["available_share"], reverse=True)

    def benchmark_pricing(self, our_price: float, event_type: str) -> dict[str, Any]:
        """Compare our pricing against market benchmarks."""
        benchmarks: dict[str, dict[str, float]] = {
            "fashion": {"budget": 10_000, "mid": 25_000, "premium": 60_000, "luxury": 150_000},
            "events": {"budget": 8_000, "mid": 20_000, "premium": 50_000, "luxury": 100_000},
            "promo": {"budget": 5_000, "mid": 12_000, "premium": 30_000, "luxury": 60_000},
            "commercial": {"budget": 12_000, "mid": 30_000, "premium": 70_000, "luxury": 120_000},
        }
        tiers = benchmarks.get(event_type.lower(), benchmarks["commercial"])
        position = "budget"
        for tier in ["luxury", "premium", "mid", "budget"]:
            if our_price >= tiers[tier]:
                position = tier
                break
        mid_price = tiers["mid"]
        pct_diff = round((our_price - mid_price) / mid_price * 100, 1)
        return {
            "our_price": our_price,
            "market_position": position,
            "vs_mid_market_pct": pct_diff,
            "recommendation": "reduce" if pct_diff > 30 else "increase" if pct_diff < -20 else "maintain",
            "benchmarks": tiers,
        }


class TrendSpotter:
    """Identifies and evaluates industry trends."""

    CURRENT_TRENDS: list[dict[str, Any]] = [
        {
            "name": "AI-assisted model selection",
            "impact": "high",
            "timeframe": "now",
            "relevance": "direct",
            "action": "build recommendation engine",
        },
        {
            "name": "Short-form video content (Reels/TikTok)",
            "impact": "high",
            "timeframe": "now",
            "relevance": "marketing",
            "action": "create video portfolio section",
        },
        {
            "name": "Sustainable/ethical fashion events",
            "impact": "medium",
            "timeframe": "1-2 years",
            "relevance": "client_acquisition",
            "action": "highlight eco-friendly credentials",
        },
        {
            "name": "Remote casting and virtual fittings",
            "impact": "medium",
            "timeframe": "now",
            "relevance": "operations",
            "action": "add video call booking option",
        },
        {
            "name": "Micro-influencer model crossover",
            "impact": "high",
            "timeframe": "now",
            "relevance": "talent_sourcing",
            "action": "recruit models with social following",
        },
        {
            "name": "Personalization at scale",
            "impact": "high",
            "timeframe": "now",
            "relevance": "direct",
            "action": "personalized recommendations by client history",
        },
    ]

    def get_actionable_trends(self, focus_area: str | None = None) -> list[dict[str, Any]]:
        """Return trends filtered by relevance area, sorted by impact."""
        trends = self.CURRENT_TRENDS
        if focus_area:
            area = focus_area.lower()
            trends = [t for t in trends if area in t["relevance"] or area == "all"]
        priority_order = {"high": 0, "medium": 1, "low": 2}
        return sorted(trends, key=lambda t: priority_order.get(t["impact"], 3))

    def score_trend_relevance(self, trend_name: str, business_context: dict[str, Any]) -> dict[str, Any]:
        """Score how relevant a specific trend is to our current business."""
        trend = next((t for t in self.CURRENT_TRENDS if trend_name.lower() in t["name"].lower()), None)
        if not trend:
            return {"trend": trend_name, "score": 0, "reason": "Тренд не найден в базе данных"}
        base_score = {"high": 80, "medium": 50, "low": 20}.get(trend["impact"], 30)
        timeframe_bonus = 20 if trend["timeframe"] == "now" else 10
        size = business_context.get("team_size", 5)
        complexity_penalty = 0 if size > 10 else 10
        score = min(base_score + timeframe_bonus - complexity_penalty, 100)
        return {
            "trend": trend["name"],
            "score": score,
            "impact": trend["impact"],
            "suggested_action": trend["action"],
            "priority": "немедленно" if score >= 70 else "в ближайший квартал" if score >= 40 else "отложить",
        }


class InsightSynthesizer:
    """Synthesizes research findings into strategic insights."""

    def synthesize_insights(
        self,
        market_data: dict[str, Any],
        competitor_gaps: list[dict[str, Any]],
        trends: list[dict[str, Any]],
        performance_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Combine all research inputs into actionable strategic insights."""
        top_gap = max(competitor_gaps, key=lambda g: g["available_share"]) if competitor_gaps else {}
        top_trend = trends[0] if trends else {}
        conversion = performance_data.get("conversion_rate", 0)
        avg_budget = performance_data.get("avg_budget", 0)
        alerts = []
        if conversion < 0.3:
            alerts.append("Конверсия ниже 30% — необходим анализ воронки продаж")
        if avg_budget < 20_000:
            alerts.append("Средний чек ниже целевого — рассмотреть повышение ценности услуг")
        opportunities = []
        if top_gap:
            opportunities.append(
                f"Сегмент '{top_gap.get('segment')}' — {int(top_gap.get('available_share', 0) * 100)}% рынка доступно"
            )
        if top_trend:
            opportunities.append(f"Тренд: {top_trend.get('name')} — {top_trend.get('action')}")
        return {
            "executive_summary": f"Рынок показывает рост. Лучшая возможность: {top_gap.get('segment', 'commercial')}.",
            "top_opportunities": opportunities[:3],
            "strategic_alerts": alerts,
            "recommended_focus": top_gap.get("segment", "commercial"),
            "confidence_level": "medium" if len(opportunities) >= 2 else "low",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def generate_weekly_insight_report(self, data: dict[str, Any]) -> str:
        """Generate a formatted weekly insight report text."""
        orders_this_week = data.get("orders_this_week", 0)
        conversion = data.get("conversion_rate", 0)
        top_segment = data.get("top_segment", "неизвестно")
        growth = data.get("revenue_growth_pct", 0.0)
        trend_arrow = "↑" if growth > 0 else "↓" if growth < 0 else "→"
        return (
            f"📊 Еженедельный аналитический отчёт\n"
            f"{'=' * 40}\n"
            f"Период: {datetime.now(timezone.utc).strftime('%d.%m.%Y')}\n\n"
            f"🔑 Ключевые метрики:\n"
            f"  • Заявок за неделю: {orders_this_week}\n"
            f"  • Конверсия: {conversion:.1%}\n"
            f"  • Топ-сегмент: {top_segment}\n"
            f"  • Рост выручки: {trend_arrow} {abs(growth):.1f}%\n\n"
            f"💡 Инсайт: {'Позитивная динамика — усилить маркетинг.' if growth > 5 else 'Стабильно — оптимизировать воронку.'}\n"
        )
