"""Geography supplied for a custom network must survive target planning."""

import asyncio

from services.geo_data import parse_custom_geo_list
from services.presence_planner import build_targets


def test_known_ukrainian_city_keeps_location_for_planned_channel():
    geo = parse_custom_geo_list("Kyiv, Ukraine, ua")[0]
    assert geo["city_native"] == "Київ"
    assert geo["city_slug"] == "kyiv"
    assert geo["country"] == "Ukraine"
    assert geo["country_code"] == "ua"
    assert geo["region"] == "Kyiv"
    assert geo["language"] == "uk"
    assert geo["timezone"] == "Europe/Kyiv"

    target = build_targets([geo], "channel", "Новини {{CITY_NAME}}", "news_{{CITY_SLUG}}", [42])[0]
    assert target["planned_name"] == "Новини Київ"
    assert target["country_code"] == "ua"
    assert target["region"] == "Kyiv"
    assert target["language"] == "uk"


def test_unknown_city_keeps_explicit_geo_columns():
    geo = parse_custom_geo_list("Irpin, Ukraine, UA, Kyiv Oblast, uk, Europe/Kyiv")[0]
    assert geo["city"] == "Irpin"
    assert geo["country"] == "Ukraine"
    assert geo["country_code"] == "ua"
    assert geo["region"] == "Kyiv Oblast"
    assert geo["language"] == "uk"
    assert geo["timezone"] == "Europe/Kyiv"


def test_country_mismatch_does_not_inherit_another_city_location():
    geo = parse_custom_geo_list("Brest, France, fr")[0]
    assert geo["country"] == "France"
    assert geo["country_code"] == "fr"
    assert not geo.get("region")
    assert not geo.get("timezone")


def test_native_city_name_uses_known_canonical_slug():
    geo = parse_custom_geo_list("Київ, Ukraine, ua")[0]
    assert geo["city_slug"] == "kyiv"
    assert geo["region"] == "Kyiv"


def test_bot_csv_upload_preserves_optional_columns():
    from bot.handlers.global_presence import _parse_geo_csv_bytes

    raw = ("місто;країна;код;область;мова;часовий пояс\n"
           "Irpin;Ukraine;UA;Kyiv Oblast;uk;Europe/Kyiv\n").encode()
    geo = asyncio.run(_parse_geo_csv_bytes(raw))[0]
    assert geo["country_code"] == "ua"
    assert geo["region"] == "Kyiv Oblast"
    assert geo["language"] == "uk"
    assert geo["timezone"] == "Europe/Kyiv"


def test_custom_500_city_list_reaches_target_planner_without_truncation():
    raw = "\n".join(
        f"City {i}, Ukraine, UA, Region {i}, uk, Europe/Kyiv"
        for i in range(1, 501)
    )
    geos = parse_custom_geo_list(raw)
    targets = build_targets(geos, "channel", "Новини {{CITY_NAME}}", "news_{{CITY_SLUG}}", [1, 2])
    assert len(targets) == 500
    assert len({target["planned_name"] for target in targets}) == 500
    assert targets[0]["region"] == "Region 1"
    assert targets[-1]["region"] == "Region 500"
    assert targets[-1]["selected_account_id"] == 2
