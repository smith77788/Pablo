"""Username generation engine: slugify, transliteration, variant generation."""

from __future__ import annotations

import re
import unicodedata

_TRANSLIT: dict[str, str] = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "yo",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
    "А": "a",
    "Б": "b",
    "В": "v",
    "Г": "g",
    "Д": "d",
    "Е": "e",
    "Ё": "yo",
    "Ж": "zh",
    "З": "z",
    "И": "i",
    "Й": "y",
    "К": "k",
    "Л": "l",
    "М": "m",
    "Н": "n",
    "О": "o",
    "П": "p",
    "Р": "r",
    "С": "s",
    "Т": "t",
    "У": "u",
    "Ф": "f",
    "Х": "kh",
    "Ц": "ts",
    "Ч": "ch",
    "Ш": "sh",
    "Щ": "sch",
    "Ъ": "",
    "Ы": "y",
    "Ь": "",
    "Э": "e",
    "Ю": "yu",
    "Я": "ya",
    # German umlauts
    "ä": "ae",
    "ö": "oe",
    "ü": "ue",
    "ß": "ss",
    "Ä": "ae",
    "Ö": "oe",
    "Ü": "ue",
    # French
    "à": "a",
    "â": "a",
    "é": "e",
    "è": "e",
    "ê": "e",
    "ë": "e",
    "î": "i",
    "ï": "i",
    "ô": "o",
    "ù": "u",
    "û": "u",
    "ç": "c",
    # Spanish/Portuguese
    "á": "a",
    "í": "i",
    "ó": "o",
    "ú": "u",
    "ñ": "n",
    "ã": "a",
    "õ": "o",
}


def transliterate(text: str) -> str:
    result = []
    for ch in text:
        if ch in _TRANSLIT:
            result.append(_TRANSLIT[ch])
        else:
            try:
                normalized = unicodedata.normalize("NFD", ch)
                ascii_ch = normalized.encode("ascii", "ignore").decode("ascii")
                result.append(ascii_ch if ascii_ch else "_")
            except Exception:
                result.append("")
    return "".join(result)


def slugify(text: str) -> str:
    """Convert text to Telegram-safe slug: lowercase a-z0-9 and underscores, max 32 chars."""
    text = transliterate(text)
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text)
    text = text.strip("_")
    return text[:32]


def _valid_username(username: str) -> bool:
    """Check Telegram username rules: 5-32 chars, a-z0-9_, no leading/trailing _, no __."""
    if not (5 <= len(username) <= 32):
        return False
    if not re.match(r"^[a-z][a-z0-9_]*[a-z0-9]$", username):
        return False
    if "__" in username:
        return False
    return True


def generate_username_variants(base: str, geo: dict | None = None) -> list[str]:
    """
    Generate candidate username variants from a base string.
    Returns a list of valid Telegram usernames, base first then fallbacks.
    Now generates up to 40+ variants for better collision coverage.
    """
    import random
    import string

    base_slug = slugify(base)[:27]
    if len(base_slug) < 3:
        base_slug = (base_slug + "channel")[:27]

    candidates: list[str] = [base_slug]

    # Numeric suffixes (1-20)
    for i in range(1, 21):
        candidates.append(f"{base_slug}_{i}")

    # Word suffixes
    for suffix in ("hub", "news", "info", "group", "chat", "official", "tg", "media", "daily", "update", "channel"):
        c = f"{base_slug}_{suffix}"
        if len(c) <= 32:
            candidates.append(c)

    # Year suffixes
    for year in (2024, 2025, 2026):
        c = f"{base_slug}_{year}"
        if len(c) <= 32:
            candidates.append(c)

    # Geo-aware fallback
    if geo:
        cc = slugify(geo.get("country_code", ""))[:3]
        city = slugify(geo.get("city", ""))[:12]
        if cc and city:
            candidates.append(f"{cc}_{city}")
            candidates.append(f"{city}_{cc}")
            candidates.append(f"{cc}_{city}_news")
            candidates.append(f"{city}_{cc}_channel")

    # Random 4-char suffixes for additional variety
    for _ in range(10):
        suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
        c = f"{base_slug}_{suffix}"
        if len(c) <= 32:
            candidates.append(c)

    seen: set[str] = set()
    valid: list[str] = []
    for c in candidates:
        if c not in seen and _valid_username(c):
            seen.add(c)
            valid.append(c)

    return valid


_SUFFIX_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"  # no i,l,o,1,0 to avoid confusion


def _short_suffix(n: int, length: int = 2) -> str:
    """Convert integer n to a short alphanumeric suffix (base-28 style)."""
    base = len(_SUFFIX_CHARS)
    result = []
    for _ in range(length):
        result.append(_SUFFIX_CHARS[n % base])
        n //= base
    return "".join(reversed(result))


def unique_channel_username(base: str, slot_idx: int) -> str:
    """Generate a unique username for a bulk-create slot.

    Uses a mix of slot-derived char + random char so parallel operations
    don't collide: base + slotchar + randomchar (e.g. myproject3k, myproject7m).
    """
    import random

    slug = slugify(base)
    if not slug:
        slug = "channel"
    slug = slug[:28]
    # First char from slot (deterministic), second char random
    c1 = _SUFFIX_CHARS[slot_idx % len(_SUFFIX_CHARS)]
    c2 = random.choice(_SUFFIX_CHARS)
    candidate = f"{slug}{c1}{c2}"
    if _valid_username(candidate):
        return candidate
    # Fallback: 3 random chars
    r = "".join(random.choices(_SUFFIX_CHARS, k=3))
    candidate = f"{slug[:27]}{r}"
    return candidate if _valid_username(candidate) else f"ch{slot_idx:03d}xx"


def unique_bot_username(base: str, slot_idx: int) -> str:
    """Generate a unique bot username (@...bot) for a bulk-create slot.

    Example: base="mysales", slot 0 → mysales3kbot, slot 1 → mysales7mbot
    """
    import random

    slug = slugify(base)
    if not slug:
        slug = "mybot"
    if slug.endswith("bot"):
        slug = slug[:-3]
    slug = slug[:24]
    c1 = _SUFFIX_CHARS[slot_idx % len(_SUFFIX_CHARS)]
    c2 = random.choice(_SUFFIX_CHARS)
    candidate = f"{slug}{c1}{c2}bot"
    if _valid_username(candidate):
        return candidate
    r = "".join(random.choices(_SUFFIX_CHARS, k=3))
    candidate = f"{slug[:23]}{r}bot"
    return candidate if _valid_username(candidate) else f"b{slot_idx:03d}xxbot"
