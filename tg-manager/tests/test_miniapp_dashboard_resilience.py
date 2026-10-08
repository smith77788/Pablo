"""Главный экран деградирует частично, а не остаётся в вечных skeleton-блоках."""
from __future__ import annotations

import asyncio
from pathlib import Path

from services import mini_app_api


def test_dashboard_query_timeout_returns_fallback_value():
    async def never_finishes():
        await asyncio.Event().wait()

    result = asyncio.run(
        mini_app_api._dashboard_query("blocked_query", never_finishes(), timeout=0.01)
    )

    assert result is None


def test_home_uses_global_timeout_and_replaces_both_initial_skeletons():
    html = (Path(__file__).resolve().parents[1] / "mini_app" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "const FETCH_TIMEOUT_MS" in html
    assert "new AbortController()" in html
    assert "await fetchT(path" in html
    load_home = html[html.index("async function loadHome()"):]
    load_home = load_home[: load_home.index("async function loadOnboarding()")]
    assert "errHtml(errRu(e), 'loadHome()')" in load_home
    assert "document.getElementById('actList').innerHTML = errorHtml" in load_home
