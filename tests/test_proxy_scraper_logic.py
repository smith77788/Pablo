from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_proxy_scraper_has_bounded_validation_cycle() -> None:
    source = (PROJECT_ROOT / "tg-manager/services/proxy_scraper.py").read_text(
        encoding="utf-8"
    )

    assert "_MAX_VALIDATE_CANDIDATES = 800" in source
    assert "random.shuffle(candidates)" in source
    assert "candidates = candidates[:_MAX_VALIDATE_CANDIDATES]" in source
    assert '"validated": len(candidates)' in source
    assert 'f"0/0@{now_str}"' in source
    assert "await asyncio.sleep(3)" in source
    assert "await asyncio.sleep(30)" not in source


def test_proxy_pool_ui_reports_validated_count() -> None:
    source = (PROJECT_ROOT / "tg-manager/bot/handlers/proxy_manager.py").read_text(
        encoding="utf-8"
    )

    assert 'validated = result.get("validated", fetched)' in source
    assert "🔎 Проверено: {validated}" in source
