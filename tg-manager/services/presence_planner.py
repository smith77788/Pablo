"""Presence Planner: pattern rendering and target list generation."""
from __future__ import annotations

from services.username_engine import slugify


def render_pattern(pattern: str, geo: dict) -> str:
    """Replace {{PLACEHOLDER}} tokens in pattern with geo values. Never crashes."""
    city_slug = geo.get("city_slug") or slugify(geo.get("city", ""))
    country_slug = geo.get("country_slug") or slugify(geo.get("country", ""))
    replacements = {
        "{{CITY}}": geo.get("city") or "",
        "{{COUNTRY}}": geo.get("country") or "",
        "{{REGION}}": geo.get("region") or "",
        "{{LANGUAGE}}": geo.get("language") or "",
        "{{COUNTRY_CODE}}": (geo.get("country_code") or "").upper(),
        "{{CITY_SLUG}}": city_slug,
        "{{COUNTRY_SLUG}}": country_slug,
        "{{INDEX}}": str(geo.get("index", 1)),
    }
    for key, val in replacements.items():
        pattern = pattern.replace(key, val)
    return pattern


def build_targets(
    geo_list: list[dict],
    asset_type: str,
    name_pattern: str,
    username_pattern: str | None,
    account_ids: list[int],
) -> list[dict]:
    """Build list of target dicts for insertion into global_presence_targets."""
    n_accs = len(account_ids)
    targets: list[dict] = []

    for i, geo in enumerate(geo_list):
        geo_indexed = {**geo, "index": i + 1}
        planned_name = render_pattern(name_pattern, geo_indexed)

        planned_username: str | None = None
        if username_pattern:
            raw = render_pattern(username_pattern, geo_indexed)
            planned_username = slugify(raw)[:32] or None

        selected_account_id = account_ids[i % n_accs] if n_accs else None

        targets.append({
            "country": geo.get("country") or None,
            "country_code": geo.get("country_code") or None,
            "region": geo.get("region") or None,
            "city": geo.get("city") or None,
            "city_slug": geo.get("city_slug") or slugify(geo.get("city", "")) or None,
            "language": geo.get("language") or None,
            "timezone": geo.get("timezone") or None,
            "asset_type": asset_type,
            "planned_name": planned_name or None,
            "planned_username": planned_username,
            "selected_account_id": selected_account_id,
        })

    return targets


def estimate_duration_minutes(n_targets: int, safe_mode: bool = True) -> int:
    """Estimate execution duration in minutes for safe pacing."""
    avg_delay = 67.5 if safe_mode else 30  # midpoint of 45-90s range
    cooldown_per_5 = 450 / 5  # 300-600s / 5 targets = 90s per target
    per_item = avg_delay + cooldown_per_5
    return int(n_targets * per_item / 60)
