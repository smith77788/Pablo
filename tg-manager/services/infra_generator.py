"""Infra Generator — декларативное описание проекта → дерево целей.

Отличие от «создать канал»: здесь пользователь описывает СЕТЬ, а движок сам
раскрывает её в конкретные объекты. Вход — проект (география × уровни × роли ×
пулы шаблонов), выход — плоский список целей, каждая со своим названием,
username, описанием и seed аватара.

Три свойства, ради которых модуль существует:

* **Пулы вместо одного шаблона.** На каждый объект шаблон выбирается из пула,
  поэтому 1500 каналов не выглядят как один канал, размноженный 1500 раз —
  а это первый признак сетки для модерации Telegram.
* **Детерминизм по seed.** Одна и та же пара (plan_seed, цель) всегда даёт тот
  же результат, поэтому предпросмотр показывает РОВНО то, что уйдёт в
  исполнение, а не «похожий пример».
* **Уникальность на этапе плана.** username выдаёт `UsernameAllocator`, который
  видит и уже занятые имена владельца, и всё, что выдано внутри этого же
  проекта. Коллизии разруливаются до запуска, а не отказом Telegram в бою.
"""

from __future__ import annotations

import hashlib
import random

from services.username_engine import (
    UsernameAllocator,
    slugify,
)

# ── Уровни географии ────────────────────────────────────────────────────────
# Проект может жить сразу на нескольких уровнях: федеральный канал-зонтик,
# региональные и городские. Уровень определяет, что подставится в {{SCOPE}}.

LEVEL_COUNTRY = "country"
LEVEL_REGION = "region"
LEVEL_CITY = "city"

LEVELS: dict[str, dict] = {
    LEVEL_COUNTRY: {"label": "🏛 Федеральный", "hint": "один объект на страну"},
    LEVEL_REGION: {"label": "🗺 Региональный", "hint": "один объект на регион/область"},
    LEVEL_CITY: {"label": "🏙 Городской", "hint": "один объект на город"},
}

DEFAULT_LEVELS: tuple[str, ...] = (LEVEL_CITY,)


# ── Библиотека ролей (тематик) ──────────────────────────────────────────────
# Роль = что это за объект в структуре города. Несёт свой тип актива и свои
# пулы шаблонов, поэтому «Чат {{CITY_NAME}}» никогда не станет каналом, а
# «Новости» не получат описание барахолки.

ROLE_LIBRARY: dict[str, dict] = {
    "news": {
        "label": "📰 Новости",
        "asset_type": "channel",
        "name_patterns": [
            "Новости {{CITY_NAME}}",
            "{{CITY_NAME}} Новости",
            "Новости {{CITY_NAME}} 24",
            "Новости {{CITY_NAME}} Онлайн",
            "{{CITY_NAME}} Онлайн",
            "Подслушано {{CITY_NAME}}",
            "{{CITY_NAME}} INFO",
            "{{CITY_NAME}} • Новости",
            "{{CITY_NAME}} Live",
            "Что происходит в {{CITY_LOC}}",
        ],
        "username_templates": [
            "{city}_news",
            "news_{city}",
            "{city}_news_{2}",
            "{abbr}_news_{2}",
            "{city}news{2}",
            "{city}_24_{2}",
            "{city}_online_{2}",
            "{alt}_news_{2}",
        ],
        "about_patterns": [
            "Актуальные новости {{CITY_GEN}}: события, происшествия, анонсы.",
            "Всё важное о {{CITY_LOC}} в одном канале. Публикуем каждый день.",
            "Новости и объявления {{CITY_GEN}}. Коротко и по делу.",
            "Городские новости {{CITY_GEN}} — оперативно и без воды.",
            "{{CITY_NAME}}: новости, погода, транспорт, события дня.",
        ],
    },
    "chat": {
        "label": "💬 Чат",
        "asset_type": "group",
        "name_patterns": [
            "Чат {{CITY_NAME}}",
            "{{CITY_NAME}} Чат",
            "Жители {{CITY_GEN}}",
            "{{CITY_NAME}} Общение",
            "Болталка {{CITY_NAME}}",
            "Подслушано {{CITY_NAME}} — чат",
            "{{CITY_NAME}} • Чат",
        ],
        "username_templates": [
            "{city}_chat",
            "chat_{city}",
            "{city}_chat_{2}",
            "{abbr}_chat_{2}",
            "{city}chat{2}",
            "{city}_talk_{2}",
            "{alt}_chat_{2}",
        ],
        "about_patterns": [
            "Чат жителей {{CITY_GEN}}. Общаемся, помогаем, обсуждаем город.",
            "Обсуждаем события {{CITY_GEN}}. Без рекламы и оскорблений.",
            "Официальное сообщество жителей {{CITY_GEN}}.",
            "Главный чат {{CITY_GEN}}: вопросы, советы, знакомства по интересам.",
        ],
    },
    "jobs": {
        "label": "💼 Работа",
        "asset_type": "channel",
        "name_patterns": [
            "Работа {{CITY_NAME}}",
            "Вакансии {{CITY_NAME}}",
            "Работа в {{CITY_LOC}}",
            "{{CITY_NAME}} Работа и подработка",
            "Вакансии и подработка — {{CITY_NAME}}",
        ],
        "username_templates": [
            "{city}_job",
            "{city}_jobs_{2}",
            "job_{city}_{2}",
            "{abbr}_jobs_{2}",
            "{city}work{2}",
            "{city}_vacancy_{2}",
        ],
        "about_patterns": [
            "Вакансии и подработка в {{CITY_LOC}}. Новые предложения каждый день.",
            "Работа в {{CITY_LOC}}: полная занятость, подработка, удалёнка.",
            "Ищете работу в {{CITY_LOC}}? Свежие вакансии от прямых работодателей.",
        ],
    },
    "afisha": {
        "label": "🎭 Афиша",
        "asset_type": "channel",
        "name_patterns": [
            "Афиша {{CITY_NAME}}",
            "Куда сходить в {{CITY_LOC}}",
            "События {{CITY_NAME}}",
            "{{CITY_NAME}} Афиша и события",
            "{{CITY_NAME}} • Афиша",
        ],
        "username_templates": [
            "{city}_afisha",
            "afisha_{city}_{2}",
            "{city}_events_{2}",
            "{abbr}_afisha_{2}",
            "{city}event{2}",
        ],
        "about_patterns": [
            "Афиша {{CITY_GEN}}: концерты, выставки, спорт, кино.",
            "Куда сходить в {{CITY_LOC}} — подборки событий на каждую неделю.",
            "События и мероприятия {{CITY_GEN}}. Планируйте выходные заранее.",
        ],
    },
    "market": {
        "label": "🛒 Барахолка",
        "asset_type": "group",
        "name_patterns": [
            "Барахолка {{CITY_NAME}}",
            "Объявления {{CITY_NAME}}",
            "Купи-продай {{CITY_NAME}}",
            "{{CITY_NAME}} Барахолка",
            "Доска объявлений {{CITY_NAME}}",
        ],
        "username_templates": [
            "{city}_market",
            "{city}_baraholka_{2}",
            "market_{city}_{2}",
            "{abbr}_market_{2}",
            "{city}sale{2}",
        ],
        "about_patterns": [
            "Барахолка {{CITY_GEN}}: продать, купить, отдать даром.",
            "Объявления {{CITY_GEN}}. Пишите цену и район — так быстрее продаётся.",
            "Купля-продажа в {{CITY_LOC}}. Частные объявления без посредников.",
        ],
    },
    "realty": {
        "label": "🏠 Недвижимость",
        "asset_type": "channel",
        "name_patterns": [
            "Недвижимость {{CITY_NAME}}",
            "Аренда {{CITY_NAME}}",
            "Квартиры {{CITY_NAME}}",
            "{{CITY_NAME}} Недвижимость",
            "Снять квартиру — {{CITY_NAME}}",
        ],
        "username_templates": [
            "{city}_realty",
            "{city}_flat_{2}",
            "realty_{city}_{2}",
            "{abbr}_realty_{2}",
            "{city}rent{2}",
        ],
        "about_patterns": [
            "Недвижимость {{CITY_GEN}}: аренда и продажа квартир, комнат, домов.",
            "Снять или купить жильё в {{CITY_LOC}}. Объявления от собственников.",
            "Аренда и продажа недвижимости в {{CITY_LOC}} — обновляем ежедневно.",
        ],
    },
    "auto": {
        "label": "🚗 Авто",
        "asset_type": "channel",
        "name_patterns": [
            "Авто {{CITY_NAME}}",
            "Автобарахолка {{CITY_NAME}}",
            "{{CITY_NAME}} Авто",
            "Автоновости {{CITY_NAME}}",
            "Купить авто — {{CITY_NAME}}",
        ],
        "username_templates": [
            "{city}_auto",
            "auto_{city}_{2}",
            "{city}_car_{2}",
            "{abbr}_auto_{2}",
            "{city}auto{2}",
        ],
        "about_patterns": [
            "Авто {{CITY_GEN}}: продажа, покупка, обмен, запчасти.",
            "Автомобильный канал {{CITY_GEN}} — объявления и полезное для водителей.",
            "Всё про авто в {{CITY_LOC}}: ДТП, пробки, сервисы, объявления.",
        ],
    },
    "services": {
        "label": "🔧 Услуги",
        "asset_type": "channel",
        "name_patterns": [
            "Услуги {{CITY_NAME}}",
            "Мастера {{CITY_NAME}}",
            "{{CITY_NAME}} Услуги",
            "Ремонт и услуги — {{CITY_NAME}}",
        ],
        "username_templates": [
            "{city}_serv",
            "{city}_service_{2}",
            "serv_{city}_{2}",
            "{abbr}_serv_{2}",
            "{city}master{2}",
        ],
        "about_patterns": [
            "Услуги и мастера {{CITY_GEN}}: ремонт, клининг, доставка, репетиторы.",
            "Найдите мастера в {{CITY_LOC}}. Проверенные исполнители и отзывы.",
            "Частные услуги в {{CITY_LOC}} — от бытового ремонта до фотосъёмки.",
        ],
    },
    "dating": {
        "label": "❤️ Знакомства",
        "asset_type": "group",
        "name_patterns": [
            "Знакомства {{CITY_NAME}}",
            "{{CITY_NAME}} Знакомства",
            "Давай познакомимся — {{CITY_NAME}}",
            "{{CITY_NAME}} • Знакомства",
        ],
        "username_templates": [
            "{city}_date",
            "{city}_dating_{2}",
            "date_{city}_{2}",
            "{abbr}_dating_{2}",
            "{city}love{2}",
        ],
        "about_patterns": [
            "Знакомства в {{CITY_LOC}}. Пишите о себе — так находят быстрее.",
            "Сообщество знакомств {{CITY_GEN}}: общение, встречи, интересы.",
            "Знакомства {{CITY_GEN}} — для общения, дружбы и отношений.",
        ],
    },
    "bot": {
        "label": "🤖 Бот",
        "asset_type": "bot",
        "name_patterns": [
            "{{CITY_NAME}} Бот",
            "Помощник {{CITY_NAME}}",
            "{{CITY_NAME}} Assistant",
            "Бот {{CITY_NAME}}",
        ],
        "username_templates": [
            "{city}_bot",
            "{city}_{2}_bot",
            "{abbr}_{role}_bot",
            "{city}helper{2}bot",
        ],
        "about_patterns": [
            "Бот-помощник {{CITY_GEN}}: справка, объявления, обратная связь.",
            "Сервисный бот {{CITY_GEN}}. Задайте вопрос — ответим.",
        ],
    },
}

# Готовые наборы ролей — «выбрал шаблон, получил структуру города».
STRUCTURE_PRESETS: dict[str, dict] = {
    "news_only": {"label": "📰 Только новости", "roles": ["news"]},
    "news_chat": {"label": "📰+💬 Новости и чат", "roles": ["news", "chat"]},
    "city_core": {
        "label": "🏙 Ядро города",
        "roles": ["news", "chat", "jobs", "afisha", "market"],
    },
    "classifieds": {
        "label": "🛒 Классифайды",
        "roles": ["market", "realty", "auto", "services"],
    },
    "full_city": {
        "label": "🌆 Полный город",
        "roles": ["news", "chat", "jobs", "afisha", "market", "realty", "auto", "services"],
    },
    "with_bot": {"label": "🤖 Новости+чат+бот", "roles": ["news", "chat", "bot"]},
}


def role_asset_type(role: str) -> str:
    """Тип актива для роли. Неизвестная роль → канал (безопасный дефолт)."""
    return (ROLE_LIBRARY.get(role) or {}).get("asset_type", "channel")


def known_roles() -> list[str]:
    return list(ROLE_LIBRARY.keys())


# ── Пулы шаблонов ───────────────────────────────────────────────────────────


def parse_pattern_pool(text: str) -> list[str]:
    """Многострочный ввод → пул шаблонов (по одному на строку, без пустых/дублей)."""
    out: list[str] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _seeded_rng(plan_seed: int, *parts: object) -> random.Random:
    """RNG, детерминированный по (plan_seed, ключ цели).

    Хэш, а не индекс: цель, сдвинувшаяся в списке (город добавили в середину),
    сохраняет свои имя/аватар — иначе повторный предпросмотр перетасовал бы
    уже показанный пользователю план.
    """
    key = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(f"{plan_seed}|{key}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def pick_from_pool(pool, rng: random.Random, fallback: str = "") -> str:
    """Выбрать шаблон из пула. Пустой пул → fallback."""
    items = [p for p in (pool or ()) if p and str(p).strip()]
    if not items:
        return fallback
    return rng.choice(items)


def render_text(pattern: str, geo: dict, rng: random.Random) -> str:
    """Развернуть шаблон: гео-плейсхолдеры + spintax `{A|B}`.

    Spintax раскрывается ПОСЛЕ гео-подстановки, чтобы `{Новости|Вести} {{CITY}}`
    работал как ожидается. Сбой spintax не должен ронять генерацию — на любой
    ошибке возвращаем текст с уже подставленной географией.
    """
    from services.presence_planner import render_pattern

    rendered = render_pattern(pattern or "", geo)
    if "{" not in rendered or "|" not in rendered:
        return rendered
    try:
        from services import spintax_service

        return spintax_service.expand_template(rendered, seed=rng.randrange(1 << 30))
    except Exception:
        return rendered


# ── Раскрытие географии по уровням ──────────────────────────────────────────


def expand_geo_levels(geo_list: list[dict], levels) -> list[dict]:
    """Раскрыть список городов в узлы запрошенных уровней.

    Городской уровень — сами города; региональный — уникальные (страна, регион);
    федеральный — уникальные страны. Каждый узел получает `level` и `scope` —
    имя, которое подставится в {{SCOPE}}, поэтому ОДИН шаблон
    «Новости {{SCOPE}}» корректно работает на всех трёх уровнях.
    """
    wanted = [lv for lv in (levels or DEFAULT_LEVELS) if lv in LEVELS] or list(DEFAULT_LEVELS)
    out: list[dict] = []

    if LEVEL_COUNTRY in wanted:
        from services.geo_data import country_display_name

        seen: set[str] = set()
        for geo in geo_list:
            country = (geo.get("country") or "").strip()
            key = (geo.get("country_code") or country).strip().lower()
            if not country or key in seen:
                continue
            seen.add(key)
            # scope — русское имя страны (для названия), scope_slug — от
            # английского (для username: слаг «rossiya» хуже «russia»).
            out.append(
                {
                    **{k: v for k, v in geo.items() if k not in ("city", "city_native", "city_slug", "region")},
                    "level": LEVEL_COUNTRY,
                    "scope": country_display_name(geo) or country,
                    "scope_slug": slugify(country),
                    "city": "",
                    "city_native": "",
                    "city_slug": "",
                    "region": "",
                }
            )

    if LEVEL_REGION in wanted:
        seen_r: set[tuple[str, str]] = set()
        for geo in geo_list:
            region = (geo.get("region") or "").strip()
            if not region:
                continue
            key = ((geo.get("country_code") or geo.get("country") or "").lower(), region.lower())
            if key in seen_r:
                continue
            seen_r.add(key)
            out.append(
                {
                    **{k: v for k, v in geo.items() if k not in ("city", "city_native", "city_slug")},
                    "level": LEVEL_REGION,
                    "scope": region,
                    "scope_slug": slugify(region),
                    "city": "",
                    "city_native": "",
                    "city_slug": "",
                }
            )

    if LEVEL_CITY in wanted:
        for geo in geo_list:
            city = (geo.get("city") or "").strip()
            if not city:
                continue
            out.append(
                {
                    **geo,
                    "level": LEVEL_CITY,
                    "scope": geo.get("city_native") or city,
                    "scope_slug": geo.get("city_slug") or slugify(city),
                }
            )

    return out


# ── Сборка целей проекта ────────────────────────────────────────────────────


def build_project_targets(
    geo_list: list[dict],
    *,
    roles=None,
    levels=None,
    name_pool=None,
    username_pool=None,
    about_pool=None,
    account_ids=None,
    plan_seed: int = 0,
    taken_usernames=None,
    avatar_style: str | None = None,
    assign_usernames: bool = True,
) -> list[dict]:
    """Раскрыть проект в плоский список целей.

    Пулы, переданные явно, ПЕРЕКРЫВАЮТ библиотечные для всех ролей — так
    работает «свой шаблон на весь проект». Не передали — каждая роль берёт
    свои (новости получают новостные тексты, барахолка — свои).

    `assign_usernames=False` — объекты создаются приватными, без username.
    Это отдельный флаг, а не «пустой пул»: пустой пул означает «возьми из
    библиотеки», и путать его с осознанным отказом нельзя.

    Возвращает цели в формате `global_presence_targets` + поля генератора
    (`role`, `level`, `planned_about`, `avatar_seed`).
    """
    roles = [r for r in (roles or ["news"]) if r in ROLE_LIBRARY] or ["news"]
    nodes = expand_geo_levels(geo_list, levels)
    acc_ids = list(account_ids or [])
    n_accs = len(acc_ids)

    user_name_pool = parse_pattern_pool("\n".join(name_pool)) if name_pool else []
    user_uname_pool = parse_pattern_pool("\n".join(username_pool)) if username_pool else []
    user_about_pool = parse_pattern_pool("\n".join(about_pool)) if about_pool else []

    allocator = UsernameAllocator(taken=taken_usernames, seed=plan_seed or None)

    targets: list[dict] = []
    slot = 0
    for node in nodes:
        node_key = f"{node.get('level')}:{node.get('scope_slug') or node.get('scope')}"
        for role in roles:
            spec = ROLE_LIBRARY[role]
            rng = _seeded_rng(plan_seed, node_key, role)

            geo_ctx = {**node, "index": slot + 1, "role": role}

            names = user_name_pool or spec["name_patterns"]
            name = render_text(pick_from_pool(names, rng), geo_ctx, rng).strip()
            if not name:
                name = (node.get("scope") or "Channel").strip()
            name = name[:128]

            abouts = user_about_pool or spec["about_patterns"]
            about = render_text(pick_from_pool(abouts, rng), geo_ctx, rng).strip()[:255]

            username = None
            uname_ctx = {
                "city": node.get("scope_slug") or node.get("city_slug") or "",
                "city_slug": node.get("scope_slug") or node.get("city_slug") or "",
                "country": node.get("country") or "",
                "country_code": node.get("country_code") or "",
                "region": node.get("region") or "",
                "index": slot + 1,
                "role": role,
            }
            if assign_usernames:
                uname_templates = user_uname_pool or spec["username_templates"]
                username = allocator.allocate(uname_templates, uname_ctx)

            targets.append(
                {
                    "country": node.get("country") or None,
                    "country_code": node.get("country_code") or None,
                    "region": node.get("region") or None,
                    "city": node.get("city") or None,
                    "city_slug": node.get("city_slug") or None,
                    "language": node.get("language") or None,
                    "timezone": node.get("timezone") or None,
                    "asset_type": spec["asset_type"],
                    "planned_name": name,
                    "planned_username": username,
                    "planned_about": about or None,
                    "role": role,
                    "level": node.get("level"),
                    "scope": node.get("scope"),
                    "avatar_seed": _avatar_seed(plan_seed, node_key, role),
                    "avatar_style": avatar_style or None,
                    "selected_account_id": acc_ids[slot % n_accs] if n_accs else None,
                }
            )
            slot += 1

    return targets


def _avatar_seed(plan_seed: int, node_key: str, role: str) -> int:
    """Стабильный seed аватара: один и тот же объект всегда получает свою картинку."""
    digest = hashlib.sha256(f"av|{plan_seed}|{node_key}|{role}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def summarize_targets(targets: list[dict]) -> dict:
    """Сводка плана для предпросмотра: сколько чего будет создано."""
    by_asset: dict[str, int] = {}
    by_role: dict[str, int] = {}
    by_level: dict[str, int] = {}
    countries: set[str] = set()
    cities: set[str] = set()
    missing_username = 0

    for t in targets:
        by_asset[t.get("asset_type", "channel")] = by_asset.get(t.get("asset_type", "channel"), 0) + 1
        if t.get("role"):
            by_role[t["role"]] = by_role.get(t["role"], 0) + 1
        if t.get("level"):
            by_level[t["level"]] = by_level.get(t["level"], 0) + 1
        if t.get("country_code") or t.get("country"):
            countries.add((t.get("country_code") or t.get("country") or "").lower())
        if t.get("city"):
            cities.add(t["city"].lower())
        if not t.get("planned_username"):
            missing_username += 1

    return {
        "total": len(targets),
        "by_asset": by_asset,
        "by_role": by_role,
        "by_level": by_level,
        "countries": len(countries),
        "cities": len(cities),
        "missing_username": missing_username,
    }


def find_duplicates(targets: list[dict]) -> dict[str, list[str]]:
    """Дубли внутри плана: username и названия, встречающиеся больше одного раза.

    Аллокатор гарантирует уникальность username, поэтому непустой результат —
    сигнал, что цели собраны в обход него (например, склеены два плана).
    """
    uname_seen: dict[str, int] = {}
    name_seen: dict[str, int] = {}
    for t in targets:
        u = (t.get("planned_username") or "").lower()
        if u:
            uname_seen[u] = uname_seen.get(u, 0) + 1
        n = (t.get("planned_name") or "").strip().lower()
        if n:
            name_seen[n] = name_seen.get(n, 0) + 1
    return {
        "usernames": sorted(u for u, c in uname_seen.items() if c > 1),
        "names": sorted(n for n, c in name_seen.items() if c > 1),
    }
