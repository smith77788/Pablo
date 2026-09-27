"""В боковом меню можно найти раздел, а не искать его глазами.

Замер в Chromium на экране 360×780: каталог — 82 раздела в 11 категориях,
5070 точек прокрутки, больше шести экранов. Дойти до «Прогрева» или
«Нотариуса» можно было только пролистав их взглядом.

Поиск фильтрует каталог по названию И по пояснению, поэтому раздел находится
по задаче: «бан» выводит «Дэшборд здоровья» (в пояснении — «риски бана»).
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_html, miniapp_source

SRC = miniapp_source()
HTML = miniapp_html()


def _fn_body(name: str) -> str:
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\s*\(", SRC, re.M)
    assert m, f"функция {name} не найдена"
    p = SRC.index("(", m.end() - 1)
    depth = 0
    for j in range(p, len(SRC)):
        if SRC[j] == "(":
            depth += 1
        elif SRC[j] == ")":
            depth -= 1
            if depth == 0:
                p = j
                break
    i = SRC.index("{", p)
    depth = 0
    for j in range(i, len(SRC)):
        if SRC[j] == "{":
            depth += 1
        elif SRC[j] == "}":
            depth -= 1
            if depth == 0:
                return SRC[i:j + 1]
    raise AssertionError(f"не закрылось тело {name}")


def test_search_box_lives_in_markup_not_in_the_rebuilt_list():
    """Иначе перестроение каталога пересоздавало бы input и фокус слетал бы
    после первой буквы."""
    assert 'id="navFind"' in HTML, "поле поиска по разделам исчезло"
    i = HTML.index('id="navFind"')
    box = HTML[i - 300:i + 300]
    assert "navFilter(this.value)" in box
    assert "aria-label=" in box, "поле без имени — скринридер прочитает пустоту"
    # поле стоит ВНЕ контейнера, который перерисовывает buildDrawer()
    assert HTML.index('id="navFind"') < HTML.index('id="navBody"')


def test_filter_matches_description_too():
    """Поиск по одному названию заставлял бы помнить, как раздел называется."""
    body = _fn_body("navFilter")
    assert "navNorm(el.textContent)" in body, (
        "фильтр смотрит не на весь текст пункта — по пояснению искать не выйдет")
    assert "const q = navNorm(" in body, "запрос не приводится к тому же виду, что и текст"
    # Обе стороны обязаны нормализоваться ОДНОЙ функцией: разъедься они, поиск
    # молча перестал бы находить по регистру или по «ё», оставаясь зелёным.
    norm = _fn_body("navNorm")
    assert "toLowerCase()" in norm, "поиск чувствителен к регистру"
    assert "replace(/ё/g" in norm, "«ё» и «е» должны совпадать"


def test_descriptions_exist_for_the_catalogue():
    """Фильтр по пояснению работает, только пока пояснения есть."""
    m = re.search(r"const MODULE_DESCS = \{(.*?)\n\};", SRC, re.DOTALL)
    assert m, "словарь пояснений исчез"
    assert m.group(1).count("':'") + m.group(1).count("':\"") > 60, "пояснений стало подозрительно мало"
    assert "ni-desc" in _fn_body("buildDrawer"), "пояснение перестало попадать в пункт меню"


def test_empty_categories_are_hidden():
    """Заголовок категории без видимых пунктов — пустая строка на экране."""
    body = _fn_body("navFilter")
    assert "body.querySelectorAll('.nav-cat').forEach(" in body, "заголовки категорий не обходятся"
    assert "cat.style.display = any" in body, "видимость заголовка ни от чего не зависит"
    i = body.index("querySelectorAll('.nav-cat')")
    seg = body[i:i + 420]
    assert "nav-item" in seg and "display !== 'none'" in seg, (
        "заголовок прячется не по видимости своих пунктов")


def test_nothing_found_says_so():
    """Текст лежит в разметке, показывает его фильтр."""
    assert 'id="navFindEmpty"' in HTML, "строка пустого результата исчезла из разметки"
    i = HTML.index('id="navFindEmpty"')
    assert "Ничего не найдено" in HTML[i:i + 300], "пустой результат выглядел бы как пропавший каталог"
    body = _fn_body("navFilter")
    assert "navFindEmpty" in body and "shown ?" in body, "строка не показывается и не прячется"


def test_filter_resets_when_the_menu_reopens():
    """Иначе меню откроется с одним пунктом от прошлого поиска."""
    body = _fn_body("openDrawer")
    assert "navFind" in body and "navFilter('')" in body, "фильтр переживает закрытие меню"
