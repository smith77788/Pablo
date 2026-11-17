"""Гео-сеть умеет перестановку ключей и несколько ресурсов на город.

Было: гео-путь (`presence_planner.build_targets`) давал РОВНО один ресурс на
город и не знал SEO-перестановки — её имела только Фабрика. Совместить гео и
перестановку было нельзя (жалоба владельца).

Стало: `build_targets(..., per_city=N, seo_permute=True)` создаёт N ресурсов на
каждый город, а при перестановке крутит ключи локализованного имени/username в N
разных вариантов. Эти тесты проверяют ПОВЕДЕНИЕ (чистая функция, без БД):
количество, различность, валидность, детерминизм и обратную совместимость.
"""
from __future__ import annotations

from services.presence_planner import build_targets

GEO = [
    {"city": "Москва", "city_slug": "moskva", "country": "Россия", "country_code": "RU"},
    {"city": "Киев", "city_slug": "kiev", "country": "Украина", "country_code": "UA"},
]


def test_backward_compatible_one_per_city():
    # Дефолт (per_city=1, без перестановки) — прежнее поведение: 1 цель на город.
    t = build_targets(GEO, "channel", "Новости {{CITY_NAME}}", "novosti_{{CITY_SLUG}}", [1])
    assert len(t) == len(GEO)
    assert t[0]["planned_name"] == "Новости Москва"
    assert t[0]["city"] == "Москва"


def test_per_city_multiplies_targets():
    # N ресурсов на город → всего городов×N целей.
    t = build_targets(GEO, "channel", "Новости {{CITY_NAME}} {{INDEX}}",
                      "novosti_{{CITY_SLUG}}_{{INDEX}}", [1, 2], per_city=3)
    assert len(t) == len(GEO) * 3
    # Каждый город представлен ровно 3 раза.
    from collections import Counter
    by_city = Counter(x["city"] for x in t)
    assert by_city["Москва"] == 3 and by_city["Киев"] == 3


def test_seo_permutation_varies_names_per_city():
    # Перестановка: на город 3 РАЗНЫХ имени из одних ключей (локализованное гео-имя
    # крутится как SEO-массив). Проверяем по Москве.
    t = build_targets(GEO, "channel", "Новости {{CITY_NAME}}", "novosti_{{CITY_SLUG}}",
                      [1], per_city=3, seo_permute=True)
    msk = [x["planned_name"] for x in t if x["city"] == "Москва"]
    assert len(msk) == 3
    assert len(set(msk)) == 3, f"имена должны различаться, получили {msk}"
    # Это перестановка ТЕХ ЖЕ ключей: множество слов первого варианта сохраняется
    # в основе (с точностью до «ростовых» букв). Базовый вариант — исходный порядок.
    assert "Москва" in " ".join(msk)


def test_seo_usernames_unique_and_valid():
    t = build_targets(GEO, "channel", "Новости {{CITY_NAME}}", "novosti_{{CITY_SLUG}}",
                      [1], per_city=4, seo_permute=True)
    unames = [x["planned_username"] for x in t if x["planned_username"]]
    # Уникальны по всему плану (между городами тоже).
    assert len(unames) == len(set(u.lower() for u in unames)), "username должны быть уникальны"
    # Непустые и в нижнем регистре, без пробелов.
    for u in unames:
        assert u == u.lower() and " " not in u and len(u) >= 4


def test_deterministic_preview_equals_launch():
    # Одинаковый вход → одинаковый выход (предпросмотр обязан совпасть с запуском).
    a = build_targets(GEO, "channel", "Новости {{CITY_NAME}}", "novosti_{{CITY_SLUG}}",
                      [1, 2], per_city=3, seo_permute=True)
    b = build_targets(GEO, "channel", "Новости {{CITY_NAME}}", "novosti_{{CITY_SLUG}}",
                      [1, 2], per_city=3, seo_permute=True)
    assert [x["planned_name"] for x in a] == [x["planned_name"] for x in b]
    assert [x["planned_username"] for x in a] == [x["planned_username"] for x in b]


def test_accounts_spread_across_variants_not_single_account():
    # Несколько ресурсов одного города не должны все висеть на одном аккаунте.
    t = build_targets([GEO[0]], "channel", "Новости {{CITY_NAME}}", "novosti_{{CITY_SLUG}}",
                      [10, 11, 12], per_city=3, seo_permute=True)
    accs = [x["selected_account_id"] for x in t]
    assert len(set(accs)) > 1, f"аккаунты должны распределяться, получили {accs}"


def test_bot_usernames_do_not_double_suffix():
    # Боты: username не должен получить двойной «_bot» (исполнитель дописывает сам).
    t = build_targets(GEO, "bot", "Помощник {{CITY_NAME}}", "{{CITY_SLUG}}_helper",
                      [1], per_city=2, seo_permute=True)
    for x in t:
        u = x["planned_username"] or ""
        assert not u.endswith("bot_bot")
        assert u.count("_bot") <= 1


# ── Слайс 3a: описание (спинтакс + плейсхолдеры) и аватар на КАЖДУЮ цель ──────

def test_description_rendered_per_target_with_spintax_and_placeholders():
    # Описание было: мини-апп-путь не клал planned_about вовсе (исполнитель ставил
    # дженерик). Теперь шаблон рендерится per-target: спинтакс {A|B} + {{CITY_NAME}}.
    t = build_targets(GEO, "channel", "Новости {{CITY_NAME}}", "novosti_{{CITY_SLUG}}",
                      [1], per_city=1, description="{Новости|Вести} {{CITY_NAME}} — погода")
    msk = next(x for x in t if x["city"] == "Москва")
    ab = msk["planned_about"]
    assert ab, "описание обязано попасть в planned_about"
    assert "{" not in ab and "}" not in ab, f"спинтакс/плейсхолдеры должны раскрыться: {ab}"
    assert "Москва" in ab  # плейсхолдер подставлен
    assert ab.split()[0] in ("Новости", "Вести")  # спинтакс раскрыт в один из вариантов


def test_bot_uses_short_description_not_channel_description():
    t = build_targets(GEO, "bot", "Помощник {{CITY_NAME}}", "{{CITY_SLUG}}_help",
                      [1], per_city=1, description="это для каналов",
                      short_description="{Помощник|Бот} {{CITY_NAME}}")
    about = t[0]["planned_about"] or ""
    assert "каналов" not in about, "бот обязан брать короткое описание, не описание канала"
    assert about.split()[0] in ("Помощник", "Бот")


def test_every_target_gets_an_avatar_seed():
    # Раньше мини-апп гео-ресурсы шли БЕЗ аватара (seed не задавался). Теперь у
    # каждого ресурса свой сид → исполнитель ставит сгенерированный аватар.
    t = build_targets(GEO, "channel", "Новости {{CITY_NAME}}", "novosti_{{CITY_SLUG}}",
                      [1], per_city=3, seo_permute=True)
    seeds = [x["avatar_seed"] for x in t]
    assert all(s is not None for s in seeds), "у каждой цели обязан быть avatar_seed"
    # Ресурсы одного города не должны делить один аватар.
    msk_seeds = [x["avatar_seed"] for x in t if x["city"] == "Москва"]
    assert len(set(msk_seeds)) == len(msk_seeds), "аватары ресурсов города должны различаться"


def test_avatar_style_propagates_when_given():
    t = build_targets(GEO, "channel", "Новости {{CITY_NAME}}", None, [1],
                      per_city=1, avatar_style="flat")
    assert all(x["avatar_style"] == "flat" for x in t)


def test_description_deterministic():
    kw = dict(per_city=2, description="{Новости|Вести|Инфо} {{CITY_NAME}}")
    a = build_targets(GEO, "channel", "Н {{CITY_NAME}}", "n_{{CITY_SLUG}}", [1], **kw)
    b = build_targets(GEO, "channel", "Н {{CITY_NAME}}", "n_{{CITY_SLUG}}", [1], **kw)
    assert [x["planned_about"] for x in a] == [x["planned_about"] for x in b]
