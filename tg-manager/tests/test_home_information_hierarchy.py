"""The home screen keeps one clear hierarchy instead of a wall of cards."""
from __future__ import annotations

from pathlib import Path

from tests.miniapp_source import miniapp_html

ROOT = Path(__file__).resolve().parents[1]
HTML = miniapp_html()
SUMMARY = (ROOT / "mini_app" / "screens" / "summary.js").read_text(
    encoding="utf-8"
)


def _home() -> str:
    start = HTML.index('<div class="screen show" id="s-home">')
    end = HTML.index('<!-- ══════════════════════ BOTS LIST', start)
    return HTML[start:end]


def test_summary_is_one_coherent_block():
    start = SUMMARY.index("function renderSummary(")
    end = SUMMARY.index("async function loadSummary(", start)
    body = SUMMARY[start:end]

    assert body.count("UI.block(") == 1
    assert "UI.group('Требует внимания'" in body
    assert "UI.sections(" not in body, "section tiles duplicate the quick navigation"


def test_primary_navigation_precedes_service_controls():
    home = _home()
    assert home.index('id="summaryWrap"') < home.index('class="qa-grid"')
    assert home.index('class="qa-grid"') < home.index('id="kpiScroll"')
    assert home.index('id="kpiScroll"') < home.index('id="homeHealthFold"')


def test_secondary_panels_are_collapsed_into_two_disclosures():
    home = _home()
    health = home[home.index('id="homeHealthFold"'):]
    for element_id in (
        "organismPulse",
        "copilotWrap",
    ):
        assert f'id="{element_id}"' in health
    assert 'id="fleetCleanBtn"' not in home
    assert 'onclick="openHealthHub()"' in health

    create = home[home.index('class="home-fold home-create-fold"'):]
    for element_id in ("quickBcText", "quickChLink", "quickBotToken"):
        assert f'id="{element_id}"' in create


def test_quick_actions_fit_in_two_rows_on_normal_phones():
    assert "#s-home .qa-grid{grid-template-columns:repeat(4" in HTML
    mobile_rule = (
        "@media(max-width:340px){#s-home .qa-grid{"
        "grid-template-columns:repeat(3"
    )
    assert mobile_rule in HTML
