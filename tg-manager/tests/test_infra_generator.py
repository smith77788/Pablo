"""Генератор инфраструктуры: пулы шаблонов, конструктор username, склонение.

Что здесь защищается (а не просто «функция вызывается»):

* **Уникальность username внутри проекта.** Это узкое место массовой генерации:
  один шаблон = одно имя на город, и при коллизии цель падает уже в бою.
  Аллокатор обязан выдавать разные имена даже когда шаблон один и город один.
* **Детерминизм по seed.** Предпросмотр показывает пользователю конкретные
  имена; если исполнение сгенерирует другие — превью врёт. Один seed → тот же
  результат, включая порядок выдачи username.
* **Грамматика русских названий.** «Работа в Москва» на 1500 объектах — это
  подпись сетки. Падежные плейсхолдеры обязаны склонять, а на нерусских
  названиях — молча возвращать исходную форму, а не коверкать.
"""
from __future__ import annotations

import pytest

from services import infra_generator as ig
from services import ru_morph
from services.presence_planner import render_pattern
from services.username_engine import (
    UsernameAllocator,
    abbreviate_city,
    canonical_city_slug,
    city_alias_options,
    expand_username_template,
    is_valid_username,
    normalize_username,
    slugify,
    validate_username_template,
)


# ── Транслитерация: украинские/белорусские буквы ────────────────────────────

def test_slugify_handles_ukrainian_letters():
    # Регресс: «Київ» слагифицировался в «ki_v» — буквы і/ї/є не было в таблице,
    # NFD их выбрасывал, и на их месте появлялось подчёркивание.
    assert slugify("Київ") == "kiyiv"
    assert slugify("Львів") == "lviv"
    assert slugify("Запоріжжя") == "zaporizhzhya"
    assert "_" not in slugify("Тернопіль")


def test_slugify_no_stray_underscores_from_apostrophes():
    assert slugify("Кам'янець") == "kamyanets"


# ── Сокращения и альтернативные написания ───────────────────────────────────

def test_abbreviation_resolves_through_transliteration():
    # «Москва» приходит как «moskva», «Moscow» — как «moscow»: это один город.
    assert abbreviate_city("Москва") == "msk"
    assert abbreviate_city("Moscow") == "msk"
    assert abbreviate_city("Санкт-Петербург") == "spb"
    assert abbreviate_city("Saint Petersburg") == "spb"


def test_canonical_slug_maps_aliases():
    assert canonical_city_slug("moskva") == "moscow"
    assert canonical_city_slug("msk") == "moscow"
    assert canonical_city_slug("piter") == "saint_petersburg"


def test_unknown_city_keeps_own_slug():
    assert abbreviate_city("Урюпинск") == slugify("Урюпинск")
    assert canonical_city_slug("uryupinsk") == "uryupinsk"


def test_alias_options_include_abbreviation():
    opts = city_alias_options("Москва")
    assert "moscow" in opts and "msk" in opts and "moskva" in opts


# ── Конструктор username ────────────────────────────────────────────────────

def _rng(seed=1):
    import random

    return random.Random(seed)


def test_template_tokens_expand():
    ctx = {"city_slug": "samara", "country_code": "ru", "role": "news", "index": 7}
    assert expand_username_template("{city}_chat", ctx, _rng()) == "samara_chat"
    assert expand_username_template("{city}_{role}", ctx, _rng()) == "samara_news"
    assert expand_username_template("{cc}_{city}", ctx, _rng()) == "ru_samara"
    assert expand_username_template("{city}_{index}", ctx, _rng()) == "samara_7"


def test_digit_and_random_tokens_have_requested_width():
    ctx = {"city_slug": "samara"}
    for _ in range(20):
        two = expand_username_template("{city}_{2}", ctx, _rng(_))
        assert two.startswith("samara_") and len(two) == len("samara_") + 2
        four = expand_username_template("{city}_{rand4}", ctx, _rng(_))
        assert len(four) == len("samara_") + 4


def test_digit_token_allows_leading_zero():
    # {2} — это «две цифры», а не «число до 99»: 09 обязано быть возможным.
    ctx = {"city_slug": "samara"}
    seen = {expand_username_template("{city}_{2}", ctx, _rng(s))[-2:] for s in range(200)}
    assert any(v.startswith("0") for v in seen)


def test_abbr_token_uses_short_form():
    ctx = {"city_slug": "moscow", "role": "news"}
    assert expand_username_template("{abbr}_{role}", ctx, _rng()) == "msk_news"


def test_expansion_always_valid_or_empty():
    ctx = {"city_slug": "moscow", "role": "news"}
    for tpl in ("{city}_{role}", "{2}{city}", "{city}", "{rand4}_{city}"):
        out = expand_username_template(tpl, ctx, _rng())
        assert out == "" or is_valid_username(out), (tpl, out)


def test_normalize_username_rules():
    assert normalize_username("__Abc__DEF__") == "abc_def"
    assert normalize_username("@Moscow_News") == "moscow_news"
    assert normalize_username("123") == ""  # только цифры — непригодно
    assert normalize_username("12moscow") == "moscow"  # username начинается с буквы


def test_validate_template_reports_real_problems():
    assert validate_username_template("{city}_news_{2}") == []
    assert any("неизвестный" in p for p in validate_username_template("{city}_{foo}"))
    # Шаблон без городского токена дал бы всем городам ОДНО имя — это молчаливая
    # поломка проекта, а не стилистика, поэтому она обязана всплыть.
    assert any("городского" in p for p in validate_username_template("news_{2}"))


# ── Аллокатор: уникальность ─────────────────────────────────────────────────

def test_allocator_never_repeats_within_project():
    alloc = UsernameAllocator(seed=1)
    ctx = {"city_slug": "moscow", "role": "news"}
    issued = [alloc.allocate(["{city}_news"], ctx) for _ in range(30)]
    assert all(issued), "аллокатор не должен исчерпываться на 30 именах одного города"
    assert len(set(issued)) == len(issued), "выданы дубли"


def test_allocator_respects_already_taken():
    alloc = UsernameAllocator(taken={"moscow_news", "@Moscow_Chat"}, seed=1)
    ctx = {"city_slug": "moscow", "role": "news"}
    issued = {alloc.allocate(["{city}_news", "{city}_chat"], ctx) for _ in range(10)}
    assert "moscow_news" not in issued
    assert "moscow_chat" not in issued  # сравнение регистронезависимое, без '@'


def test_allocator_is_deterministic_for_same_seed():
    ctx = {"city_slug": "kazan", "role": "chat"}
    a = [UsernameAllocator(seed=42).allocate(["{city}_chat_{2}"], ctx) for _ in range(1)]
    b = [UsernameAllocator(seed=42).allocate(["{city}_chat_{2}"], ctx) for _ in range(1)]
    assert a == b


def test_allocator_falls_back_when_user_template_exhausted():
    # Шаблон без источника вариативности даёт ровно одно имя. Второй запрос
    # обязан вернуть НЕ None (движок переходит к дефолтному пулу), иначе
    # проект встанет на втором объекте того же города.
    alloc = UsernameAllocator(seed=3)
    ctx = {"city_slug": "omsk", "role": "news"}
    first = alloc.allocate(["{city}_news"], ctx)
    second = alloc.allocate(["{city}_news"], ctx)
    assert first == "omsk_news"
    assert second and second != first


# ── Склонение ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "nom,gen,loc",
    [
        ("Москва", "Москвы", "Москве"),
        ("Самара", "Самары", "Самаре"),
        ("Калуга", "Калуги", "Калуге"),          # после г пишется -и, не -ы
        ("Саратов", "Саратова", "Саратове"),
        ("Тверь", "Твери", "Твери"),
        ("Казань", "Казани", "Казани"),
        ("Нижний Новгород", "Нижнего Новгорода", "Нижнем Новгороде"),
        ("Великий Новгород", "Великого Новгорода", "Великом Новгороде"),
        ("Ростов-на-Дону", "Ростова-на-Дону", "Ростове-на-Дону"),
        ("Санкт-Петербург", "Санкт-Петербурга", "Санкт-Петербурге"),
    ],
)
def test_declension_common_toponyms(nom, gen, loc):
    assert ru_morph.genitive(nom) == gen
    assert ru_morph.prepositional(nom) == loc


@pytest.mark.parametrize("plural,loc", [("Химки", "Химках"), ("Люберцы", "Люберцах"),
                                        ("Мытищи", "Мытищах"), ("Чебоксары", "Чебоксарах")])
def test_plural_prepositional(plural, loc):
    assert ru_morph.prepositional(plural) == loc


def test_plural_genitive_left_alone():
    # Родительный множественного требует беглой гласной (Химки → Химок);
    # без словаря правило не выводится, а «Химк» хуже несклоняемой формы.
    assert ru_morph.genitive("Химки") == "Химки"
    assert ru_morph.genitive("Люберцы") == "Люберцы"


@pytest.mark.parametrize("name", ["Berlin", "Київ", "Львів", "Магілёў", "Сочи", "Иваново"])
def test_non_russian_or_indeclinable_unchanged(name):
    assert ru_morph.genitive(name) == name
    assert ru_morph.prepositional(name) == name


def test_case_placeholders_in_render_pattern():
    geo = {"city": "Samara", "city_native": "Самара", "city_slug": "samara"}
    assert render_pattern("Новости города {{CITY_GEN}}", geo) == "Новости города Самары"
    assert render_pattern("Работа в {{CITY_LOC}}", geo) == "Работа в Самаре"


# ── Уровни географии ────────────────────────────────────────────────────────

_GEO = [
    {"city": "Moscow", "city_native": "Москва", "city_slug": "moscow",
     "country": "Russia", "country_code": "ru", "region": "Москва"},
    {"city": "Sochi", "city_native": "Сочи", "city_slug": "sochi",
     "country": "Russia", "country_code": "ru", "region": "Краснодарский край"},
    {"city": "Krasnodar", "city_native": "Краснодар", "city_slug": "krasnodar",
     "country": "Russia", "country_code": "ru", "region": "Краснодарский край"},
]


def test_levels_deduplicate_countries_and_regions():
    nodes = ig.expand_geo_levels(_GEO, ["country", "region", "city"])
    levels = [n["level"] for n in nodes]
    assert levels.count("country") == 1          # одна страна на три города
    assert levels.count("region") == 2           # Москва + Краснодарский край
    assert levels.count("city") == 3


def test_country_level_uses_russian_country_name():
    # «Новости Russia» в русскоязычном продукте читается как машинный перевод.
    node = ig.expand_geo_levels(_GEO, ["country"])[0]
    assert node["scope"] == "Россия"
    assert node["scope_slug"] == "russia"  # слаг остаётся латинским для username


def test_city_level_default_when_levels_missing():
    assert [n["level"] for n in ig.expand_geo_levels(_GEO, [])] == ["city"] * 3
    assert [n["level"] for n in ig.expand_geo_levels(_GEO, ["bogus"])] == ["city"] * 3


def test_region_level_skips_cities_without_region():
    geo = [{"city": "X", "city_slug": "x", "country": "Y", "country_code": "yy"}]
    assert ig.expand_geo_levels(geo, ["region"]) == []


# ── Сборка проекта ──────────────────────────────────────────────────────────

def test_project_expands_roles_per_city():
    targets = ig.build_project_targets(
        _GEO, roles=["news", "chat"], levels=["city"], account_ids=[1, 2], plan_seed=7
    )
    assert len(targets) == 6
    assert {t["asset_type"] for t in targets} == {"channel", "group"}
    # Роль определяет тип актива: чат обязан быть группой, а не каналом.
    assert all(t["asset_type"] == "group" for t in targets if t["role"] == "chat")


def test_project_usernames_unique_across_whole_plan():
    big = [
        {"city": f"City{i}", "city_native": f"City{i}", "city_slug": f"city{i}",
         "country": "Russia", "country_code": "ru", "region": "R"}
        for i in range(60)
    ]
    targets = ig.build_project_targets(
        big, roles=["news", "chat", "jobs"], levels=["city"], account_ids=[1], plan_seed=5
    )
    unames = [t["planned_username"] for t in targets]
    assert all(unames), "каждая цель обязана получить username"
    assert len(set(unames)) == len(unames)
    assert ig.find_duplicates(targets) == {"usernames": [], "names": []}


def test_project_is_deterministic_for_same_seed():
    kw = dict(roles=["news", "chat"], levels=["city"], account_ids=[1, 2], plan_seed=2024)
    a = ig.build_project_targets(_GEO, **kw)
    b = ig.build_project_targets(_GEO, **kw)
    assert [(t["planned_name"], t["planned_username"], t["planned_about"]) for t in a] == \
           [(t["planned_name"], t["planned_username"], t["planned_about"]) for t in b]
    assert [t["avatar_seed"] for t in a] == [t["avatar_seed"] for t in b]


def test_different_seeds_give_different_plans():
    a = ig.build_project_targets(_GEO, roles=["news"], plan_seed=1)
    b = ig.build_project_targets(_GEO, roles=["news"], plan_seed=2)
    assert [t["planned_name"] for t in a] != [t["planned_name"] for t in b]


def test_names_vary_across_cities():
    # Пул существует ради разнообразия: если все города получили одно и то же
    # название-шаблон, сетка палится с первого взгляда.
    big = [
        {"city": f"City{i}", "city_native": f"Город{i}", "city_slug": f"city{i}",
         "country": "Russia", "country_code": "ru"}
        for i in range(40)
    ]
    targets = ig.build_project_targets(big, roles=["news"], plan_seed=11)
    shapes = {t["planned_name"].replace(t["city"] or "", "").replace("Город", "") for t in targets}
    assert len(shapes) >= 4, f"слишком однообразные названия: {shapes}"


def test_explicit_pools_override_library():
    targets = ig.build_project_targets(
        _GEO,
        roles=["news"],
        name_pool=["Мой канал {{CITY_NAME}}"],
        username_pool=["{city}_custom_{2}"],
        about_pool=["Описание {{CITY_GEN}}"],
        plan_seed=3,
    )
    assert all(t["planned_name"].startswith("Мой канал ") for t in targets)
    assert all("_custom_" in t["planned_username"] for t in targets)
    assert all(t["planned_about"].startswith("Описание ") for t in targets)


def test_accounts_distributed_round_robin():
    targets = ig.build_project_targets(_GEO, roles=["news"], account_ids=[10, 20], plan_seed=1)
    assert [t["selected_account_id"] for t in targets] == [10, 20, 10]


def test_no_accounts_leaves_target_unassigned():
    targets = ig.build_project_targets(_GEO, roles=["news"], account_ids=[], plan_seed=1)
    assert all(t["selected_account_id"] is None for t in targets)


def test_unknown_role_falls_back_to_news():
    targets = ig.build_project_targets(_GEO, roles=["nonexistent"], plan_seed=1)
    assert {t["role"] for t in targets} == {"news"}


def test_summary_counts_match_targets():
    targets = ig.build_project_targets(
        _GEO, roles=["news", "chat"], levels=["country", "city"], plan_seed=4
    )
    s = ig.summarize_targets(targets)
    assert s["total"] == len(targets)
    assert sum(s["by_asset"].values()) == len(targets)
    assert s["cities"] == 3
    assert s["missing_username"] == 0


def test_structure_presets_reference_known_roles():
    for key, preset in ig.STRUCTURE_PRESETS.items():
        for role in preset["roles"]:
            assert role in ig.ROLE_LIBRARY, f"пресет {key} ссылается на роль {role}"


def test_role_library_templates_are_usable():
    # Каждая роль обязана уметь произвести валидный username из своих шаблонов,
    # иначе «выбрал тематику → все цели без username» проявится только в бою.
    for role, spec in ig.ROLE_LIBRARY.items():
        alloc = UsernameAllocator(seed=1)
        ctx = {"city_slug": "samara", "role": role}
        got = alloc.allocate(spec["username_templates"], ctx)
        assert got and is_valid_username(got), (role, got)
        assert spec["name_patterns"] and spec["about_patterns"]
        assert spec["asset_type"] in ("channel", "group", "bot")


def test_parse_pattern_pool_dedupes_and_trims():
    assert ig.parse_pattern_pool(" A \n\n A \nB\n") == ["A", "B"]


def test_no_usernames_when_explicitly_disabled():
    # «Без username» — осознанный выбор (объекты будут приватными). Пустой пул
    # означает другое: «возьми из библиотеки». Путать их нельзя, иначе кнопка
    # «Без username» молча выдаёт имена.
    targets = ig.build_project_targets(
        _GEO, roles=["news"], plan_seed=1, assign_usernames=False
    )
    assert all(t["planned_username"] is None for t in targets)
    # А при пустом пуле — наоборот, имена обязаны появиться из библиотеки.
    with_names = ig.build_project_targets(_GEO, roles=["news"], plan_seed=1)
    assert all(t["planned_username"] for t in with_names)


# ── Стык двух механизмов вариативности ──────────────────────────────────────
# Пул шаблонов и spintax-группа оба используют «|», а токены уникальности
# ({2}, {rand4}) живут в том же render_pattern, что и гео-плейсхолдеры.
# Здесь защищается их мирное сосуществование.

def test_uniqueness_tokens_stay_deterministic():
    """Регресс: {2}/{rand4} в пользовательском паттерне раскрывались свежим
    Random, и предпросмотр переставал совпадать с исполнением.

    Детерминизм — основа обещания «вы видите то, что создастся», поэтому он
    обязан держаться и на токенах уникальности, а не только на выборе шаблона.
    """
    geo = [{"city": "Samara", "city_native": "Самара", "city_slug": "samara",
            "country": "Russia", "country_code": "ru"}]
    kw = dict(roles=["news"], name_pool=["Новости {{CITY_NAME}} {2}"], plan_seed=777)
    a = ig.build_project_targets(geo, **kw)
    b = ig.build_project_targets(geo, **kw)
    assert a[0]["planned_name"] == b[0]["planned_name"]
    # И токен действительно раскрыт, а не оставлен в тексте.
    assert "{2}" not in a[0]["planned_name"]


def test_spintax_group_not_split_as_pool():
    """Регресс: split_pool рвал `{Новости|Вести}` на `'{Новости'` + `'Вести} …'`.

    Заметить это можно было только по кривым названиям уже созданных каналов:
    оба механизма используют «|», и наивный split ломал spintax молча.
    """
    from services.presence_planner import split_pool

    assert split_pool("{Новости|Вести} {{CITY_NAME}}") == ["{Новости|Вести} {{CITY_NAME}}"]
    # При этом пул по-прежнему разделяется — и строками, и «|» верхнего уровня.
    assert split_pool("Новости {{CITY}}\nВести {{CITY}}") == [
        "Новости {{CITY}}", "Вести {{CITY}}"
    ]
    assert split_pool("{Новости|Вести} {{CITY}}|Афиша {{CITY}}") == [
        "{Новости|Вести} {{CITY}}", "Афиша {{CITY}}"
    ]


def test_spintax_expands_through_generator():
    geo = [{"city": "Samara", "city_native": "Самара", "city_slug": "samara"}]
    out = {
        ig.build_project_targets(
            geo, roles=["news"], name_pool=["{Новости|Вести|Сводка} {{CITY_NAME}}"],
            plan_seed=s,
        )[0]["planned_name"]
        for s in range(30)
    }
    # Группа раскрыта (скобок не осталось) и даёт больше одного варианта.
    assert all("{" not in n for n in out), out
    assert len(out) > 1, out
