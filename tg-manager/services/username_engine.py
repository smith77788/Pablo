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
    # Украинский / белорусский: без них «Київ» слагифицировался в «ki_v»
    # (буква не в таблице → NFD → отбрасывается → подчёркивание).
    "і": "i",
    "І": "i",
    "ї": "yi",
    "Ї": "yi",
    "є": "ye",
    "Є": "ye",
    "ґ": "g",
    "Ґ": "g",
    "ў": "u",
    "Ў": "u",
    "'": "",
    "’": "",
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


def is_valid_username(username: str) -> bool:
    """Публичный алиас `_valid_username` — правила Telegram для username канала/группы.

    5–32 символа, только a-z0-9_, начинается с буквы, заканчивается буквой/цифрой,
    без двойного подчёркивания.
    """
    return _valid_username(username)


def normalize_username(candidate: str) -> str:
    """Привести кандидата к валидной форме Telegram-username, насколько возможно.

    Схлопывает `__`, срезает подчёркивания по краям, режет до 32 символов,
    гарантирует букву в начале. Возвращает '' если привести нельзя (пусто/короче 5).
    Не изобретает суффиксы — этим занимается генератор вариантов.
    """
    s = (candidate or "").strip().lstrip("@").lower()
    s = re.sub(r"[^a-z0-9_]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        return ""
    if not s[0].isalpha():
        # username обязан начинаться с буквы: срезаем ведущие цифры,
        # а если ничего не осталось — кандидат непригоден.
        s = s.lstrip("0123456789_")
        s = re.sub(r"_+", "_", s).strip("_")
        if not s or not s[0].isalpha():
            return ""
    s = s[:32].rstrip("_")
    return s if _valid_username(s) else ""


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


# ═══════════════════════════════════════════════════════════════════════════
#  Конструктор username: шаблоны с токенами + сокращения + аллокатор
#
#  Проблема, которую решает этот блок: при генерации инфраструктуры на сотни
#  городов уникальность username — узкое место. Один паттерн даёт ровно одно
#  имя на город, и если оно занято, вся цель падает. Конструктор превращает
#  «одно имя» в пространство имён: пул шаблонов × токены-суффиксы, из которого
#  аллокатор выбирает первый свободный вариант БЕЗ ручной правки.
# ═══════════════════════════════════════════════════════════════════════════

# Сокращения городов (slug → короткая форма). Живой сленг: msk/spb/ekb люди
# ищут и пишут чаще полного слага, а короткий username остаётся читаемым даже
# после добавления суффикса.
CITY_ABBREVIATIONS: dict[str, str] = {
    # Россия
    "moscow": "msk",
    "saint_petersburg": "spb",
    "yekaterinburg": "ekb",
    "ekaterinburg": "ekb",
    "novosibirsk": "nsk",
    "nizhny_novgorod": "nn",
    "kazan": "kzn",
    "chelyabinsk": "chel",
    "krasnoyarsk": "krsk",
    "rostov_on_don": "rnd",
    "krasnodar": "krd",
    "vladivostok": "vldk",
    "volgograd": "vlg",
    "voronezh": "vrn",
    "samara": "smr",
    "perm": "prm",
    "tyumen": "tmn",
    "kaliningrad": "klgd",
    "irkutsk": "irk",
    "khabarovsk": "khv",
    "barnaul": "brn",
    "yaroslavl": "yar",
    "tomsk": "tsk",
    "kemerovo": "kem",
    "novokuznetsk": "nvkz",
    "ryazan": "rzn",
    "astrakhan": "astr",
    "naberezhnye_chelny": "chelny",
    "penza": "pnz",
    "lipetsk": "lpk",
    "kirov": "krv",
    "cheboksary": "chb",
    "kaluga": "klg",
    "sevastopol": "sev",
    "simferopol": "simf",
    "stavropol": "stav",
    "surgut": "srg",
    "bryansk": "brnsk",
    "magnitogorsk": "mgn",
    "belgorod": "blg",
    "ulyanovsk": "uln",
    "makhachkala": "mkhl",
    # Украина
    "kyiv": "kyiv",
    "kharkiv": "kh",
    "odesa": "od",
    "dnipro": "dp",
    "lviv": "lv",
    "zaporizhzhia": "zp",
    # Беларусь / Казахстан
    "minsk": "mnsk",
    "almaty": "ala",
    "astana": "ast",
    "nur_sultan": "ast",
    # Мир
    "new_york": "nyc",
    "los_angeles": "la",
    "san_francisco": "sf",
    "london": "ldn",
    "berlin": "ber",
    "paris": "prs",
    "amsterdam": "ams",
    "barcelona": "bcn",
    "madrid": "mad",
    "rome": "rom",
    "dubai": "dxb",
    "singapore": "sg",
    "hong_kong": "hk",
    "tokyo": "tyo",
    "istanbul": "ist",
    "warsaw": "waw",
    "prague": "prg",
    "vienna": "vie",
    "munich": "muc",
    "frankfurt": "fra",
    "hamburg": "hh",
    "zurich": "zrh",
    "milan": "mil",
    "lisbon": "lis",
    "bangkok": "bkk",
    "sydney": "syd",
    "toronto": "tor",
    "chicago": "chi",
    "miami": "mia",
    "tel_aviv": "tlv",
}

# Альтернативные написания (slug → варианты). Расширяют пространство имён,
# когда каноническое написание уже занято, и попадают в поисковые запросы
# людей, которые пишут «moskva» или «kiev».
CITY_ALIASES: dict[str, tuple[str, ...]] = {
    # Кириллица приходит транслитом (slugify("Москва") == "moskva"), поэтому
    # родные написания перечислены здесь же — иначе {abbr} для «Москва» не
    # нашёл бы msk и тихо отдал бы длинный слаг.
    "moscow": ("moskva", "msk"),
    "saint_petersburg": ("spb", "piter", "peterburg", "sankt_peterburg"),
    "yekaterinburg": ("ekaterinburg", "ekb"),
    "nizhny_novgorod": ("nnov", "nizhniy", "nizhniy_novgorod"),
    "rostov_on_don": ("rostov", "rnd", "rostov_na_donu"),
    "novosibirsk": ("nsk",),
    "krasnoyarsk": ("krsk",),
    "kyiv": ("kiev", "kiyiv"),
    "odesa": ("odessa",),
    "kharkiv": ("kharkov",),
    "dnipro": ("dnepr",),
    "lviv": ("lvov",),
    "zaporizhzhia": ("zaporozhye", "zaporizhzhya"),
    "almaty": ("alma_ata",),
    "new_york": ("nyc", "newyork"),
    "los_angeles": ("la", "losangeles"),
    "san_francisco": ("sf", "sanfran"),
}

# alias → канонический слаг. Строится один раз: без него «Москва» (→ moskva)
# и «Moscow» вели бы себя по-разному, хотя это один город.
_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canon, _alts in CITY_ALIASES.items():
    for _alt in _alts:
        _ALIAS_TO_CANONICAL.setdefault(_alt, _canon)


def canonical_city_slug(city_or_slug: str) -> str:
    """Канонический слаг города: 'moskva'/'msk' → 'moscow'. Неизвестный — как есть."""
    slug = slugify(city_or_slug)
    return _ALIAS_TO_CANONICAL.get(slug, slug)

# Разделители, допустимые в username Telegram. Точка/дефис в username
# ЗАПРЕЩЕНЫ (в отличие от названия) — поддерживаем только '_' и склейку.
USERNAME_SEPARATORS: tuple[str, ...] = ("_", "")

_RAND_CHARS = "abcdefghijkmnopqrstuvwxyz23456789"  # без l/0/1 — визуально спорные

# Токены конструктора. Namespace намеренно ОТДЕЛЬНЫЙ от {{PLACEHOLDER}}:
# {{CITY_SLUG}} — гео-подстановка (детерминированная), {city} — конструктор
# (может быть случайным). Их можно смешивать в одном шаблоне.
_UNAME_TOKEN_RE = re.compile(r"\{([a-z_]+\d*|\d+)\}")

_STATIC_TOKENS = frozenset(
    {
        "city",
        "city_slug",
        "slug",
        "abbr",
        "city_abbr",
        "alt",
        "city_alt",
        "cc",
        "country",
        "country_code",
        "region",
        "index",
        "i",
        "year",
        "yy",
        "sep",
        "role",
    }
)


def abbreviate_city(city_or_slug: str) -> str:
    """Короткая форма города (msk, spb, ekb…). Нет в словаре → слаг как есть."""
    raw = slugify(city_or_slug)
    if not raw:
        return ""
    canon = _ALIAS_TO_CANONICAL.get(raw, raw)
    return CITY_ABBREVIATIONS.get(canon) or CITY_ABBREVIATIONS.get(raw) or raw


def city_alias_options(city_or_slug: str) -> list[str]:
    """Все написания города: исходное + каноническое + альтернативы + сокращение."""
    raw = slugify(city_or_slug)
    if not raw:
        return []
    canon = _ALIAS_TO_CANONICAL.get(raw, raw)
    out = [raw]
    if canon not in out:
        out.append(canon)
    for alt in CITY_ALIASES.get(canon, ()):
        if alt not in out:
            out.append(alt)
    abbr = CITY_ABBREVIATIONS.get(canon)
    if abbr and abbr not in out:
        out.append(abbr)
    return out


def username_template_tokens(template: str) -> list[str]:
    """Список токенов конструктора в шаблоне (без дублей, в порядке появления)."""
    seen: set[str] = set()
    out: list[str] = []
    for tok in _UNAME_TOKEN_RE.findall(template or ""):
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def validate_username_template(template: str) -> list[str]:
    """Проблемы шаблона для показа пользователю. Пустой список = шаблон годен.

    Проверяем то, что делает шаблон НЕПРИГОДНЫМ или молча бесполезным, а не
    стилистику: неизвестные токены (сгенерируют мусор), отсутствие города
    (все города получат одно имя), запрещённые символы, заведомая длина.
    """
    problems: list[str] = []
    tpl = (template or "").strip()
    if not tpl:
        return ["Шаблон пустой"]

    for tok in username_template_tokens(tpl):
        if tok.isdigit():
            if not (1 <= int(tok) <= 6):
                problems.append(f"{{{tok}}} — длина цифрового суффикса вне 1–6")
            continue
        if tok.startswith("rand"):
            tail = tok[4:]
            if not tail.isdigit() or not (1 <= int(tail) <= 8):
                problems.append(f"{{{tok}}} — ожидается {{rand1}}…{{rand8}}")
            continue
        if tok not in _STATIC_TOKENS:
            problems.append(f"{{{tok}}} — неизвестный токен")

    # Символы вне шаблонных скобок должны быть допустимы в username.
    literal = _UNAME_TOKEN_RE.sub("", tpl)
    literal = re.sub(r"\{\{[A-Z_]+\}\}", "", literal)  # гео-плейсхолдеры ок
    bad = sorted({c for c in literal if not re.match(r"[a-z0-9_]", c)})
    if bad:
        problems.append("Недопустимые символы: " + " ".join(bad))

    has_geo = bool(re.search(r"\{\{(CITY|CITY_SLUG|COUNTRY|REGION)[A-Z_]*\}\}", tpl)) or any(
        t in ("city", "city_slug", "slug", "abbr", "city_abbr", "alt", "city_alt", "index", "i")
        for t in username_template_tokens(tpl)
    )
    if not has_geo:
        problems.append("Нет городского токена — все города получат одинаковый username")

    return problems


def _expand_uname_token(token: str, ctx: dict, rng) -> str:
    """Развернуть один токен конструктора. Неизвестный токен → пустая строка."""
    if token.isdigit():
        width = max(1, min(6, int(token)))
        return "".join(rng.choice("0123456789") for _ in range(width))
    if token.startswith("rand") and token[4:].isdigit():
        width = max(1, min(8, int(token[4:])))
        return "".join(rng.choice(_RAND_CHARS) for _ in range(width))

    city_slug = slugify(str(ctx.get("city_slug") or ctx.get("city") or ""))
    if token in ("city", "city_slug", "slug"):
        return city_slug
    if token in ("abbr", "city_abbr"):
        return abbreviate_city(city_slug)
    if token in ("alt", "city_alt"):
        options = city_alias_options(city_slug)
        return rng.choice(options) if options else city_slug
    if token in ("cc", "country_code"):
        return slugify(str(ctx.get("country_code") or ""))
    if token == "country":
        return slugify(str(ctx.get("country") or ""))
    if token == "region":
        return slugify(str(ctx.get("region") or ""))
    if token in ("index", "i"):
        return str(ctx.get("index") or 1)
    if token == "role":
        return slugify(str(ctx.get("role") or ""))
    if token == "year":
        from datetime import datetime, timezone

        return str(datetime.now(timezone.utc).year)
    if token == "yy":
        from datetime import datetime, timezone

        return str(datetime.now(timezone.utc).year)[-2:]
    if token == "sep":
        return rng.choice(USERNAME_SEPARATORS)
    return ""


def expand_username_template(template: str, ctx: dict, rng=None) -> str:
    """Развернуть шаблон-конструктор в кандидат username.

    Поддерживает токены: {city} {abbr} {alt} {cc} {region} {index} {role}
    {year} {yy} {sep}, цифровые суффиксы {2}/{3}/{4} (случайные цифры, ведущий
    ноль допустим) и {rand3}/{rand4} (случайный alnum). Гео-плейсхолдеры
    {{CITY_SLUG}} должны быть развёрнуты ДО вызова (render_pattern).

    Результат нормализован под правила Telegram; '' если шаблон непригоден.
    rng — источник случайности: передайте seeded random.Random, чтобы превью
    совпадало с исполнением.
    """
    import random as _random

    r = rng if rng is not None else _random
    rendered = _UNAME_TOKEN_RE.sub(lambda m: _expand_uname_token(m.group(1), ctx, r), template or "")
    return normalize_username(rendered)


# Пул шаблонов по умолчанию — из него аллокатор берёт варианты, когда
# пользовательский шаблон исчерпан. Порядок = приоритет читаемости.
DEFAULT_USERNAME_TEMPLATES: tuple[str, ...] = (
    "{city}_{role}",
    "{city}{role}",
    "{city}_{role}_{2}",
    "{city}{role}{2}",
    "{abbr}_{role}_{2}",
    "{city}_{role}_{rand3}",
    "{city}_{3}",
    "{alt}_{role}_{2}",
    "{city}_{role}_{rand4}",
    "{abbr}{role}{3}",
)


class UsernameAllocator:
    """Выдаёт уникальные username для целей плана, без ручной правки.

    Держит множество уже занятых имён (внутри проекта + известные из БД) и на
    каждый запрос перебирает шаблоны пула, пока не найдёт свободный валидный
    вариант. Выданное имя сразу резервируется — два города одного плана не
    получат одинаковый username даже при совпадении шаблона.

    Детерминизм: при одном seed последовательность выдач воспроизводима, то
    есть превью показывает ровно те имена, которые уйдут в исполнение.
    """

    def __init__(self, taken=None, *, seed: int | None = None, attempts_per_template: int = 6):
        import random as _random

        self._taken: set[str] = {
            n for n in ((t or "").strip().lstrip("@").lower() for t in (taken or ())) if n
        }
        self._rng = _random.Random(seed)
        self._attempts = max(1, attempts_per_template)

    @property
    def taken(self) -> set[str]:
        return set(self._taken)

    def reserve(self, username: str) -> None:
        """Пометить имя занятым (например, после отказа Telegram)."""
        name = (username or "").strip().lstrip("@").lower()
        if name:
            self._taken.add(name)

    def release(self, username: str) -> None:
        self._taken.discard((username or "").strip().lstrip("@").lower())

    def candidates(self, templates, ctx: dict, *, limit: int = 12) -> list[str]:
        """Свободные кандидаты (без резервирования) — для превью и ретраев."""
        out: list[str] = []
        local_seen: set[str] = set()
        for tpl in self._template_order(templates, ctx):
            for _ in range(self._attempts):
                cand = expand_username_template(tpl, ctx, self._rng)
                if not cand or cand in self._taken or cand in local_seen:
                    continue
                local_seen.add(cand)
                out.append(cand)
                if len(out) >= limit:
                    return out
        return out

    def allocate(self, templates, ctx: dict) -> str | None:
        """Выдать и зарезервировать свободный username. None — если не вышло.

        None означает «пространство имён исчерпано для этого контекста» —
        честный сигнал вызывающему, а не молча выданный дубль.
        """
        for tpl in self._template_order(templates, ctx):
            for _ in range(self._attempts):
                cand = expand_username_template(tpl, ctx, self._rng)
                if cand and cand not in self._taken:
                    self._taken.add(cand)
                    return cand
        # Последний рубеж: базовые варианты от движка вариантов.
        base = slugify(str(ctx.get("city_slug") or ctx.get("city") or "channel"))
        for cand in generate_username_variants(base, ctx):
            if cand not in self._taken:
                self._taken.add(cand)
                return cand
        return None

    def _template_order(self, templates, ctx: dict) -> list[str]:
        """Пользовательские шаблоны первыми, затем пул по умолчанию.

        Пользователь выбрал форму осознанно — она приоритетна; дефолтный пул
        подключается только когда её пространство исчерпано.
        """
        if isinstance(templates, str):
            user_tpls = [templates] if templates.strip() else []
        else:
            user_tpls = [t for t in (templates or ()) if t and t.strip()]
        ordered = list(user_tpls)
        for tpl in DEFAULT_USERNAME_TEMPLATES:
            if tpl not in ordered:
                ordered.append(tpl)
        return ordered
