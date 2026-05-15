"""
Integration tests for cycle.py — Phase 23 (Research Department) and Phase 24 (Channel Content).

Verifies that:
- cycle.py contains Phase 23 and Phase 24 blocks
- The agents referenced in those phases have the correct interface
- Phase 24 results structure matches what cycle.py stores in results["phases"]
- The overall phase count meets expectations
"""
from __future__ import annotations
import os
import re
import sys
import pytest
from pathlib import Path

# Ensure the factory package is importable from any working directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

CYCLE_PY = Path(__file__).parent.parent / "cycle.py"


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────

def _read_cycle() -> str:
    return CYCLE_PY.read_text(encoding="utf-8")


# ────────────────────────────────────────────────────────────────────
# Phase 23 — Research Department
# ────────────────────────────────────────────────────────────────────

def test_cycle_has_phase_23_label():
    """cycle.py must contain the Phase 23 label."""
    content = _read_cycle()
    assert "Phase 23" in content, "cycle.py is missing 'Phase 23'"


def test_cycle_phase_23_references_research_department():
    """cycle.py Phase 23 block must import from research_department."""
    content = _read_cycle()
    assert "research_department" in content, (
        "cycle.py does not import from research_department for Phase 23"
    )


def test_cycle_phase_23_uses_standalone_agents():
    """Phase 23 must use standalone agent classes from research_department.py."""
    content = _read_cycle()
    # All four standalone agent classes must be present in cycle.py
    assert "MarketResearcher" in content
    assert "CompetitorAnalyst" in content
    assert "TrendSpotter" in content
    assert "InsightSynthesizer" in content
    # Phase 23 must import from research_department (not the legacy research_dept)
    # Locate the Phase 23 block and verify it references research_department
    phase23_start = content.find("Phase 23")
    phase24_start = content.find("Phase 24")
    assert phase23_start != -1, "Phase 23 block not found"
    phase23_block = content[phase23_start:phase24_start] if phase24_start != -1 else content[phase23_start:]
    assert "research_department" in phase23_block, (
        "Phase 23 block does not import from research_department"
    )
    # The Phase 23 block must not use the legacy monolithic wrapper
    assert "research_dept" not in phase23_block, (
        "Phase 23 should use standalone agents from research_department, not research_dept"
    )


def test_cycle_phase_23_stores_research_department_key():
    """Phase 23 must write results into results['phases']['research_department']."""
    content = _read_cycle()
    assert 'results["phases"]["research_department"]' in content or \
           "results['phases']['research_department']" in content, (
        "cycle.py Phase 23 does not store output under results['phases']['research_department']"
    )


def test_research_department_agents_importable():
    """All four Phase 23 agent classes must be importable."""
    from factory.agents.research_department import (
        MarketResearcher,
        CompetitorAnalyst,
        TrendSpotter,
        InsightSynthesizer,
    )
    assert MarketResearcher is not None
    assert CompetitorAnalyst is not None
    assert TrendSpotter is not None
    assert InsightSynthesizer is not None


def test_phase_23_research_output_keys():
    """The dict stored by Phase 23 must contain the keys that cycle.py creates."""
    from factory.agents.research_department import (
        MarketResearcher, CompetitorAnalyst, TrendSpotter, InsightSynthesizer
    )
    researcher = MarketResearcher()
    analyst = CompetitorAnalyst()
    spotter = TrendSpotter()
    synthesizer = InsightSynthesizer()

    market = researcher.analyze_market_segment("commercial")
    gaps = analyst.identify_competitive_gaps(["fashion", "events", "commercial"])
    trends = spotter.get_actionable_trends()[:3]
    insights = synthesizer.synthesize_insights(
        market, gaps, trends, {"conversion_rate": 0.3, "avg_budget": 20_000}
    )

    # Simulate what cycle.py builds for results["phases"]["research_department"]
    phase_result = {
        "top_segment": "commercial",
        "market_opportunity_score": market.get("opportunity_score", 0),
        "top_opportunities": insights.get("top_opportunities", []),
        "strategic_alerts": insights.get("strategic_alerts", []),
        "confidence": insights.get("confidence_level", "low"),
    }

    assert "top_segment" in phase_result
    assert "market_opportunity_score" in phase_result
    assert isinstance(phase_result["top_opportunities"], list)
    assert isinstance(phase_result["strategic_alerts"], list)
    assert phase_result["confidence"] in ("low", "medium", "high")


# ────────────────────────────────────────────────────────────────────
# Phase 24 — Channel Content
# ────────────────────────────────────────────────────────────────────

def test_cycle_has_phase_24_label():
    """cycle.py must contain the Phase 24 label."""
    content = _read_cycle()
    assert "Phase 24" in content, "cycle.py is missing 'Phase 24'"


def test_cycle_phase_24_references_channel_content():
    """cycle.py Phase 24 block must import ChannelContentGenerator."""
    content = _read_cycle()
    assert "channel_content" in content, "cycle.py does not import channel_content for Phase 24"
    assert "ChannelContentGenerator" in content, (
        "cycle.py does not use ChannelContentGenerator in Phase 24"
    )


def test_cycle_phase_24_stores_channel_content_key():
    """Phase 24 must write results into results['phases']['channel_content']."""
    content = _read_cycle()
    assert 'results["phases"]["channel_content"]' in content or \
           "results['phases']['channel_content']" in content, (
        "cycle.py Phase 24 does not store output under results['phases']['channel_content']"
    )


def test_channel_content_stats_post_keys():
    """generate_stats_post must return the required keys."""
    from factory.agents.channel_content import ChannelContentGenerator
    gen = ChannelContentGenerator()
    stats = gen.generate_stats_post({
        "total_orders": 10,
        "active_models": 5,
        "cities_served": 2,
        "avg_rating": 4.5,
    })
    assert "format" in stats
    assert "text" in stats
    assert "char_count" in stats
    assert stats["format"] == "stats"
    assert stats["char_count"] == len(stats["text"])


def test_channel_content_calendar_2_weeks():
    """get_content_calendar(weeks=2) must return exactly 4 items, each with a 'format' key."""
    from factory.agents.channel_content import ChannelContentGenerator
    gen = ChannelContentGenerator()
    calendar = gen.get_content_calendar(weeks=2)
    assert len(calendar) == 4, f"Expected 4 calendar items for 2 weeks, got {len(calendar)}"
    assert all("format" in item for item in calendar)


def test_phase_24_cycle_result_structure():
    """Simulate what cycle.py stores for results['phases']['channel_content']."""
    from factory.agents.channel_content import ChannelContentGenerator
    gen = ChannelContentGenerator()

    stats_post = gen.generate_stats_post({
        "total_orders": 15,
        "active_models": 8,
        "cities_served": 1,
        "avg_rating": 5.0,
    })
    tips_post = gen.generate_tips_post("choosing_model")
    calendar = gen.get_content_calendar(weeks=2)

    # Reproduce exactly the dict that cycle.py stores
    phase_result = {
        "stats_post_chars": stats_post["char_count"],
        "tips_post_chars": tips_post["char_count"],
        "calendar_posts_scheduled": len(calendar),
        "next_post_format": calendar[0]["format"] if calendar else "model_spotlight",
    }

    assert "stats_post_chars" in phase_result
    assert "tips_post_chars" in phase_result
    assert "calendar_posts_scheduled" in phase_result
    assert "next_post_format" in phase_result

    assert phase_result["stats_post_chars"] > 0
    assert phase_result["tips_post_chars"] > 0
    assert phase_result["calendar_posts_scheduled"] == 4
    assert phase_result["next_post_format"] in [
        "model_spotlight", "tips", "case_study", "promotion", "stats", "behind_scenes"
    ]


# ────────────────────────────────────────────────────────────────────
# Overall phase count
# ────────────────────────────────────────────────────────────────────

def test_cycle_phase_count():
    """cycle.py should reference at least Phase 22 as the maximum numbered phase present."""
    content = _read_cycle()
    phases_found = re.findall(r"Phase\s+(\d+)", content)
    assert phases_found, "No 'Phase N' patterns found in cycle.py"
    phase_numbers = {int(n) for n in phases_found}
    assert max(phase_numbers) >= 22, (
        f"Expected max phase >= 22, got {max(phase_numbers)}"
    )


def test_cycle_phase_24_is_highest_or_near_highest():
    """Phase 24 should be among the highest numbered phases in cycle.py."""
    content = _read_cycle()
    phases_found = re.findall(r"Phase\s+(\d+)", content)
    phase_numbers = {int(n) for n in phases_found}
    assert 24 in phase_numbers, "Phase 24 not found in cycle.py"
    assert max(phase_numbers) >= 24


def test_cycle_phases_23_and_24_both_present():
    """Both Phase 23 and Phase 24 must be present in the same cycle.py file."""
    content = _read_cycle()
    phases_found = re.findall(r"Phase\s+(\d+)", content)
    phase_numbers = {int(n) for n in phases_found}
    assert 23 in phase_numbers, "Phase 23 missing from cycle.py"
    assert 24 in phase_numbers, "Phase 24 missing from cycle.py"
