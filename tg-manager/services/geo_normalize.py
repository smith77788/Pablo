"""Нормализация страны к ISO2 + флаг-эмодзи.

Первопричина бага: детект прокси (ip-api) писал `geo_country` полным именем
(«Ukraine»), а все матч-сайты сравнивают его с ISO2-кодом
(`UPPER(geo_country)=UPPER('UA')`) → гео-подбор прокси/аккаунтов не работал.
Единый нормализатор приводит всё к ISO2. Маппинг имён берётся из готового
датасета services/geo_data.py (105 стран, единый источник правды) плюс
supplement под альтернативные написания ip-api.
"""
from __future__ import annotations

from typing import Optional

from services import geo_data as _gd


def _build_name_map() -> dict[str, str]:
    """country (lower) → ISO2 (upper) из всех списков-датасетов geo_data."""
    m: dict[str, str] = {}
    for v in vars(_gd).values():
        if isinstance(v, list):
            for it in v:
                if isinstance(it, dict):
                    name = (it.get("country") or "").strip().lower()
                    code = (it.get("country_code") or "").strip().upper()
                    if name and len(code) == 2:
                        m.setdefault(name, code)
    return m


_NAME_TO_ISO2 = _build_name_map()

# Альтернативные написания ip-api / бытовые, отличные от geo_data.
_ALIASES: dict[str, str] = {
    "usa": "US", "u.s.a.": "US", "united states of america": "US",
    "russian federation": "RU",
    "korea": "KR", "republic of korea": "KR", "south korea": "KR",
    "north korea": "KP",
    "czechia": "CZ", "czech republic": "CZ",
    "uk": "GB", "great britain": "GB",
    "viet nam": "VN",
    "iran, islamic republic of": "IR",
    "republic of moldova": "MD",
    "uae": "AE", "united arab emirates": "AE",
    "hong kong": "HK", "macao": "MO", "macau": "MO",
    "laos": "LA", "brunei": "BN",
    "ivory coast": "CI", "cote d'ivoire": "CI", "côte d'ivoire": "CI",
    "democratic republic of the congo": "CD", "the congo": "CG",
    "the netherlands": "NL",
    "türkiye": "TR", "turkiye": "TR",
    "syrian arab republic": "SY",
    "united republic of tanzania": "TZ",
}


def to_iso2(value: Optional[str]) -> Optional[str]:
    """Привести страну (полное имя или код) к ISO2 (upper). None, если не распознано."""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    if len(s) == 2 and s.isalpha():
        return s.upper()
    key = s.lower()
    return _ALIASES.get(key) or _NAME_TO_ISO2.get(key)


def flag_emoji(iso2: Optional[str]) -> str:
    """ISO2 → эмодзи-флаг (региональные индикаторы). Пусто, если не ISO2."""
    if not iso2 or len(iso2) != 2 or not iso2.isalpha():
        return ""
    cc = iso2.upper()
    return chr(0x1F1E6 + ord(cc[0]) - 65) + chr(0x1F1E6 + ord(cc[1]) - 65)


def name_map() -> dict[str, str]:
    """Полный маппинг имя→ISO2 (для генерации бэкфилл-миграции/тестов)."""
    out = dict(_NAME_TO_ISO2)
    out.update(_ALIASES)
    return out
