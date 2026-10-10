"""Presence Planner: генератор текстов инфраструктуры (названия/username/описания).

Не «одно название на всё», а ГЕНЕРАТОР: паттерн может быть ПУЛОМ шаблонов (по
одному на строку) — для каждого города детерминированно выбирается один вариант,
плюс токены уникальности ({randN}, {N} цифр). Детерминизм по (city, index) →
предпросмотр СОВПАДАЕТ с тем, что реально создастся (класс #4: честный предпросмотр).

Токены в шаблоне:
    {{CITY}} {{CITY_NAME}} {{CITY_GEN}} {{CITY_LOC}} {{CITY_SLUG}} {{CITY_ABBR}}
    {{SCOPE}} {{SCOPE_GEN}} {{SCOPE_LOC}} {{SCOPE_SLUG}}
    {{REGION}} {{COUNTRY}} {{COUNTRY_CODE}} {{COUNTRY_SLUG}} {{LANGUAGE}} {{INDEX}}
    {randN} — N случайных символов (без похожих: без i,l,o,1,0); {rand} = 4
    {N}     — N случайных цифр (например {2} → «47», {3} → «182»)
Пул шаблонов: несколько строк (или через `|`) — на город берётся один по сид-хешу.
"""

from __future__ import annotations

import hashlib
import random
import re

from services.username_engine import slugify

# Символы без визуально похожих (i,l,o,1,0) — как в username_engine.
_RAND_ALNUM = "abcdefghjkmnpqrstuvwxyz23456789"
_DIGITS = "0123456789"

# Сокращения крупных городов (msk/spb/ekb…) — доступны как {{CITY_ABBR}}.
# Отсутствует в словаре → fallback на city_slug.
_CITY_ABBR: dict[str, str] = {
    "moscow": "msk",
    "saint_petersburg": "spb",
    "yekaterinburg": "ekb",
    "novosibirsk": "nsk",
    "nizhny_novgorod": "nn",
    "kazan": "kzn",
    "chelyabinsk": "chel",
    "samara": "smr",
    "omsk": "omsk",
    "rostov_on_don": "rnd",
    "ufa": "ufa",
    "krasnoyarsk": "krsk",
    "voronezh": "vrn",
    "perm": "perm",
    "volgograd": "vlg",
    "krasnodar": "krd",
    "saratov": "srt",
    "tyumen": "tmn",
    "vladivostok": "vdk",
    "sochi": "sochi",
}

_RE_RAND = re.compile(r"\{rand(\d*)\}")
_RE_DIGITS = re.compile(r"\{(\d+)\}")


def _seed_int(*parts: object) -> int:
    """Детерминированный int-сид из частей (стабилен между preview и запуском)."""
    raw = "|".join(str(p) for p in parts)
    return int(hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12], 16)


def _seeded_rng(*parts: object) -> random.Random:
    return random.Random(_seed_int(*parts))


def _split_top_level(line: str, sep: str = "|") -> list[str]:
    """Разбить строку по `sep`, ИГНОРИРУЯ разделители внутри фигурных скобок.

    `|` служит сразу двум механизмам вариативности: разделяет шаблоны в пуле и
    альтернативы в spintax-группе `{Новости|Вести}`. Наивный `split("|")` рвал
    группу на мусор (`'{Новости'` + `'Вести} …'`), и заметно это было только по
    кривым названиям уже созданных каналов.
    """
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in line:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def split_pool(text: str) -> list[str]:
    """Пул шаблонов из текста: по строкам и `|`, без пустых. Один шаблон → [шаблон].

    Разделитель `|` внутри `{...}` не считается границей шаблона — это
    spintax-альтернатива, а не второй шаблон пула.
    """
    out: list[str] = []
    for line in (text or "").replace("\r", "").split("\n"):
        for part in _split_top_level(line):
            p = part.strip()
            if p:
                out.append(p)
    return out


def expand_random_tokens(text: str, rng: random.Random) -> str:
    """Заменить {randN}/{rand} и {N} на случайные строки из данного rng."""
    def _rand(m: re.Match) -> str:
        n = int(m.group(1)) if m.group(1) else 4
        n = max(1, min(n, 12))
        return "".join(rng.choice(_RAND_ALNUM) for _ in range(n))

    def _digits(m: re.Match) -> str:
        n = max(1, min(int(m.group(1)), 12))
        return "".join(rng.choice(_DIGITS) for _ in range(n))

    text = _RE_RAND.sub(_rand, text)
    text = _RE_DIGITS.sub(_digits, text)
    return text


def render_pattern(pattern: str, geo: dict, rng: random.Random | None = None) -> str:
    """Подставить {{PLACEHOLDER}} из geo и раскрыть токены уникальности. Не падает.

    `scope` — имя гео-узла на его уровне (страна/регион/город); служит фолбэком для
    {{CITY}}/{{CITY_NAME}}, чтобы шаблон «Новости {{CITY_NAME}}» работал и на
    федеральном узле без города (иначе название схлопнулось бы в «Новости»).
    `rng` задаёт детерминизм случайных токенов (preview == запуск); None → свежий rng
    (годится для эхо-предпросмотра ввода).
    """
    scope = (geo.get("scope") or "").strip()
    scope_slug = geo.get("scope_slug") or slugify(scope)
    city = geo.get("city") or scope
    city_slug = geo.get("city_slug") or slugify(geo.get("city", "")) or scope_slug
    country_slug = geo.get("country_slug") or slugify(geo.get("country", ""))
    # {{CITY_NAME}} = нативное название (Москва, Київ, Wien), fallback → английское
    city_native = geo.get("city_native") or geo.get("city") or scope or ""
    city_abbr = _CITY_ABBR.get(city_slug, city_slug)
    # Падежи нужны, чтобы шаблон читался по-русски: «Работа в {{CITY_LOC}}» →
    # «Работа в Москве», «Новости города {{CITY_GEN}}» → «…города Самары».
    # Для нерусских названий склонятели возвращают исходную форму.
    from services import ru_morph

    replacements = {
        "{{CITY}}": city,
        "{{CITY_NAME}}": city_native,
        "{{CITY_GEN}}": ru_morph.genitive(city_native),
        "{{CITY_LOC}}": ru_morph.prepositional(city_native),
        "{{SCOPE}}": scope or city_native,
        "{{SCOPE_GEN}}": ru_morph.genitive(scope or city_native),
        "{{SCOPE_LOC}}": ru_morph.prepositional(scope or city_native),
        "{{SCOPE_SLUG}}": scope_slug or city_slug,
        "{{COUNTRY}}": geo.get("country") or "",
        "{{REGION}}": geo.get("region") or "",
        "{{LANGUAGE}}": geo.get("language") or "",
        "{{COUNTRY_CODE}}": (geo.get("country_code") or "").upper(),
        "{{CITY_SLUG}}": city_slug,
        "{{CITY_ABBR}}": city_abbr,
        "{{COUNTRY_SLUG}}": country_slug,
        "{{INDEX}}": str(geo.get("index", 1)),
    }
    for key, val in replacements.items():
        pattern = pattern.replace(key, val)
    # «о Адлерском» → «об Адлерском»: выбрать форму предлога можно только
    # ПОСЛЕ подстановки — в шаблоне на этом месте стоит плейсхолдер.
    pattern = ru_morph.fix_prepositions(pattern)
    return expand_random_tokens(pattern, rng or random.Random())


def plan_target(
    geo: dict,
    index: int,
    name_pattern: str,
    username_pattern: str | None = None,
) -> tuple[str | None, str | None]:
    """Детерминированно спланировать (name, username) для одного гео.

    name_pattern/username_pattern могут быть ПУЛОМ (несколько шаблонов) — на город
    выбирается один по стабильному сид-хешу (city_slug+index). Случайные токены
    тоже сидятся → одинаковый вход даёт одинаковый выход (preview == запуск).
    """
    geo_i = {**geo, "index": index}
    city_slug = (
        geo.get("city_slug")
        or slugify(geo.get("city", ""))
        or geo.get("scope_slug")
        or slugify(geo.get("scope", ""))
        or "x"
    )

    name_pool = split_pool(name_pattern)
    name: str | None = None
    if name_pool:
        tpl = name_pool[_seed_int(city_slug, index, "namepick") % len(name_pool)]
        name = render_pattern(tpl, geo_i, _seeded_rng(city_slug, index, "name")) or None

    username: str | None = None
    if username_pattern:
        u_pool = split_pool(username_pattern)
        if u_pool:
            tpl = u_pool[_seed_int(city_slug, index, "userpick") % len(u_pool)]
            raw = render_pattern(tpl, geo_i, _seeded_rng(city_slug, index, "user"))
            username = slugify(raw)[:32] or None

    return name, username


def build_targets(
    geo_list: list[dict],
    asset_type: str,
    name_pattern: str,
    username_pattern: str | None,
    account_ids: list[int],
) -> list[dict]:
    """Build list of target dicts for insertion into global_presence_targets.

    name_pattern/username_pattern могут быть пулом шаблонов (см. plan_target) —
    вариант выбирается детерминированно на город, чтобы предпросмотр совпадал.
    """
    n_accs = len(account_ids)
    targets: list[dict] = []

    for i, geo in enumerate(geo_list):
        planned_name, planned_username = plan_target(
            geo, i + 1, name_pattern, username_pattern
        )
        selected_account_id = account_ids[i % n_accs] if n_accs else None

        targets.append(
            {
                "country": geo.get("country") or None,
                "country_code": geo.get("country_code") or None,
                "region": geo.get("region") or None,
                "city": geo.get("city") or None,
                "city_slug": geo.get("city_slug")
                or slugify(geo.get("city", ""))
                or None,
                "language": geo.get("language") or None,
                "timezone": geo.get("timezone") or None,
                "asset_type": asset_type,
                "planned_name": planned_name,
                "planned_username": planned_username,
                "selected_account_id": selected_account_id,
            }
        )

    return targets


def derive_pattern_from_reference(reference: str, sample: str, placeholder: str = "{{CITY_NAME}}") -> str:
    """Унификация из референса: по образцу ('Новости Москва') и городу-образцу
    ('Москва') строит паттерн ('Новости {{CITY_NAME}}'), заменяя вхождение города
    на плейсхолдер. Регистронезависимо. Если город не найден в референсе — возвращает
    референс как есть (пользователь сам подставит плейсхолдер). Чистая функция."""
    import re as _re
    ref = (reference or "").strip()
    smp = (sample or "").strip()
    if not ref:
        return ""
    if not smp:
        return ref
    return _re.sub(_re.escape(smp), placeholder, ref, flags=_re.IGNORECASE)


def estimate_duration_minutes(n_targets: int, safe_mode: bool = True) -> int:
    """Estimate execution duration in minutes for safe pacing."""
    avg_delay = 67.5 if safe_mode else 30  # midpoint of 45-90s range
    cooldown_per_5 = 450 / 5  # 300-600s / 5 targets = 90s per target
    per_item = avg_delay + cooldown_per_5
    return int(n_targets * per_item / 60)
