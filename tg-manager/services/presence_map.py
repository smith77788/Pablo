"""Карта присутствия: где у нас есть флот/активы по гео и где пробелы.

Присутствие = композиция мультигео (аккаунты по странам прокси) + активы
(каналы/боты) + планы присутствия (global_presence_targets по странам). Здесь —
чистая свёртка планов в покрытие/пробелы; распределение аккаунтов берётся из
`geo_router.summarize_distribution`. Без сети — тестируется без БД.
"""
from __future__ import annotations

_DONE = ("done",)
_FAIL = ("failed", "skipped")

# Код страны без гео-прокси: аккаунт есть, страна неизвестна.
UNKNOWN = "UNKNOWN"
_UNKNOWN_RU = "Без гео-прокси"


def country_ru(code: str, fallback: str = "") -> dict:
    """Русское имя страны и флаг по коду ISO-3166 alpha-2.

    Карта присутствия показывала сырые двухбуквенные коды («DE», «NL»), а имена
    стран в планах приходили из генератора по-английски («Germany»). Владелец
    читает по-русски, поэтому подпись собирается здесь: имя из
    `geo_data.COUNTRY_NATIVE_RU`, флаг — из региональных индикаторов самого кода.
    """
    cc = (code or "").strip().upper()
    if not cc or cc in (UNKNOWN, "??"):
        return {"code": cc or UNKNOWN, "name": _UNKNOWN_RU, "flag": "🌐",
                "title": "🌐 " + _UNKNOWN_RU}
    name = ""
    try:
        from services.geo_data import COUNTRY_NATIVE_RU
        name = COUNTRY_NATIVE_RU.get(cc.lower(), "")
    except Exception:          # данных нет — подпись всё равно должна быть
        name = ""
    name = name or (fallback or "").strip() or cc
    flag = ""
    if len(cc) == 2 and cc.isalpha():
        flag = "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in cc)
    return {"code": cc, "name": name, "flag": flag,
            "title": (flag + " " + name).strip()}


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
        ru = country_ru(cc, name)
        c = countries.setdefault(cc, {"code": cc, "name": name,
                                      "name_ru": ru["name"], "flag": ru["flag"],
                                      "title": ru["title"],
                                      "planned": 0, "done": 0, "pending": 0,
                                      "plans": []})
        c["planned"] += 1
        # План, из которого растёт пробел: без него строка «развёрнуто 2/7» была
        # тупиком — открыть сам план было нечем.
        pid = r.get("plan_id")
        if pid is not None and not any(pl["id"] == pid for pl in c["plans"]):
            c["plans"].append({"id": pid,
                               "name": r.get("plan_name") or r.get("name_pattern") or ""})
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
