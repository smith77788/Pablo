"""Username generation engine: slugify, transliteration, variant generation."""
from __future__ import annotations

import re
import unicodedata

_TRANSLIT: dict[str, str] = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo', 'ж': 'zh',
    'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o',
    'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'kh', 'ц': 'ts',
    'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu',
    'я': 'ya',
    'А': 'a', 'Б': 'b', 'В': 'v', 'Г': 'g', 'Д': 'd', 'Е': 'e', 'Ё': 'yo', 'Ж': 'zh',
    'З': 'z', 'И': 'i', 'Й': 'y', 'К': 'k', 'Л': 'l', 'М': 'm', 'Н': 'n', 'О': 'o',
    'П': 'p', 'Р': 'r', 'С': 's', 'Т': 't', 'У': 'u', 'Ф': 'f', 'Х': 'kh', 'Ц': 'ts',
    'Ч': 'ch', 'Ш': 'sh', 'Щ': 'sch', 'Ъ': '', 'Ы': 'y', 'Ь': '', 'Э': 'e', 'Ю': 'yu',
    'Я': 'ya',
    # German umlauts
    'ä': 'ae', 'ö': 'oe', 'ü': 'ue', 'ß': 'ss',
    'Ä': 'ae', 'Ö': 'oe', 'Ü': 'ue',
    # French
    'à': 'a', 'â': 'a', 'é': 'e', 'è': 'e', 'ê': 'e', 'ë': 'e',
    'î': 'i', 'ï': 'i', 'ô': 'o', 'ù': 'u', 'û': 'u', 'ç': 'c',
    # Spanish/Portuguese
    'á': 'a', 'í': 'i', 'ó': 'o', 'ú': 'u', 'ñ': 'n', 'ã': 'a', 'õ': 'o',
}


def transliterate(text: str) -> str:
    result = []
    for ch in text:
        if ch in _TRANSLIT:
            result.append(_TRANSLIT[ch])
        else:
            try:
                normalized = unicodedata.normalize('NFD', ch)
                ascii_ch = normalized.encode('ascii', 'ignore').decode('ascii')
                result.append(ascii_ch if ascii_ch else '_')
            except Exception:
                result.append('')
    return ''.join(result)


def slugify(text: str) -> str:
    """Convert text to Telegram-safe slug: lowercase a-z0-9 and underscores, max 32 chars."""
    text = transliterate(text)
    text = text.lower()
    text = re.sub(r'[^a-z0-9]+', '_', text)
    text = re.sub(r'_+', '_', text)
    text = text.strip('_')
    return text[:32]


def _valid_username(username: str) -> bool:
    """Check Telegram username rules: 5-32 chars, a-z0-9_, no leading/trailing _, no __."""
    if not (5 <= len(username) <= 32):
        return False
    if not re.match(r'^[a-z][a-z0-9_]*[a-z0-9]$', username):
        return False
    if '__' in username:
        return False
    return True


def generate_username_variants(base: str, geo: dict | None = None) -> list[str]:
    """
    Generate candidate username variants from a base string.
    Returns a list of valid Telegram usernames, base first then fallbacks.
    """
    base_slug = slugify(base)[:27]
    if len(base_slug) < 3:
        base_slug = (base_slug + "channel")[:27]

    candidates: list[str] = [base_slug]

    # Numeric suffixes
    for i in range(1, 6):
        candidates.append(f"{base_slug}_{i}")

    # Word suffixes
    for suffix in ("hub", "news", "info", "group", "chat", "official", "tg"):
        c = f"{base_slug}_{suffix}"
        if len(c) <= 32:
            candidates.append(c)

    # Geo-aware fallback
    if geo:
        cc = slugify(geo.get("country_code", ""))[:3]
        city = slugify(geo.get("city", ""))[:12]
        if cc and city:
            candidates.append(f"{cc}_{city}")
            candidates.append(f"{city}_{cc}")

    seen: set[str] = set()
    valid: list[str] = []
    for c in candidates:
        if c not in seen and _valid_username(c):
            seen.add(c)
            valid.append(c)

    return valid
