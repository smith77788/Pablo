"""Карта присутствия: где у нас есть флот/активы по гео и где пробелы.

Присутствие = композиция мультигео (аккаунты по странам прокси) + активы
(каналы/боты) + планы присутствия (global_presence_targets по странам). Здесь —
чистая свёртка планов в покрытие/пробелы; распределение аккаунтов берётся из
`geo_router.summarize_distribution`. Без сети — тестируется без БД.
"""
from __future__ import annotations

_DONE = ("done",)
_FAIL = ("failed", "skipped")


def summarize_targets(rows: list[dict]) -> dict:
    """Свёртка целей присутствия по странам.

    rows: [{country, country_code, status}]. Возвращает
    {countries: {cc: {name, planned, done, pending}}, gaps: [cc...]}.
    «Пробел» — страна с запланированными, но не полностью развёрнутыми целями.
    """
    countries: dict[str, dict] = {}
    for r in rows or []:
        cc = (r.get("country_code") or r.get("country") or "??").strip().upper() or "??"
        name = (r.get("country") or cc)
        c = countries.setdefault(cc, {"code": cc, "name": name,
                                      "planned": 0, "done": 0, "pending": 0})
        c["planned"] += 1
        status = (r.get("status") or "").lower()
        if status in _DONE:
            c["done"] += 1
        elif status not in _FAIL:
            c["pending"] += 1
    gaps = sorted(cc for cc, c in countries.items() if c["pending"] > 0)
    return {"countries": countries, "gaps": gaps}


def coverage_score(distinct_geo: int, gaps: int) -> dict:
    """Простой индикатор широты присутствия для мозга/UI.

    distinct_geo — сколько стран с флотом; gaps — недоразвёрнутые планы.
    """
    if distinct_geo <= 1:
        level = "narrow"
    elif distinct_geo <= 3:
        level = "moderate"
    else:
        level = "wide"
    return {"level": level, "distinct_geo": int(distinct_geo), "gaps": int(gaps)}
