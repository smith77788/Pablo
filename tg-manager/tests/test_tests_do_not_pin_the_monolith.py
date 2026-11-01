"""Потолок: число наборов, прибитых к пути `services/mini_app_api.py`.

API-слой мини-аппа пилится на `services/mini_app_*.py`. Проверка, которая
читает монолит ПО ПУТИ, после выноса ручки краснеет не потому, что
функциональность пропала, а потому, что она переехала — и каждый вынос стоит
отдельного красного прогона в CI. За одну сессию так упало четыре раза:
боты, инвайты с каналами, операции, дашборд.

Хуже другого рода случай: не покраснеть. Храповик
`test_miniapp_path_param_scope_ratchet` — защита от самого дорогого бага
мультиарендного продукта — читал один файл и к моменту этой проверки видел
113 ручек из 172. Треть поверхности была вне защиты, и узналось это не находкой,
а тем, что собственная самопроверка храповика не дотянула до порога.

Поэтому: новые наборы берут источник из `tests.miniapp_source` (`api_source`,
`api_function`, `api_routes`, `api_files`), а число старых обязано только
убывать. Потолок — не список имён: он честно сторожит ровно рост, и его
достаточно, чтобы долг не увеличивался. Уменьшили — опустите число здесь же,
тем же коммитом.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

# Снято 15.10.2026: 123 набора переведены на общий источник за один проход,
# осталось 18. Снято ещё одно при выносе блока аккаунтов (фильтр аккаунтов,
# политика прокси, фильтр спам-блока — на общий источник; интерактивный
# вход — на новый файл). Ещё два при выносе сетей и экосистем
# (кросспостинг, шаблоны сетей, масштаб прогона, пульт
# вирт. администратора — на api_source/api_function). Только вниз.
CEILING = 14

# Имя файла КАК ЛИТЕРАЛ — в любом виде, которым его собирают в путь:
# `"services/mini_app_api.py"`, `ROOT / "services" / "mini_app_api.py"`,
# `os.path.join(ROOT, "services", "mini_app_api.py")`.
#
# Первая версия этой проверки искала только склеенный путь одной строкой и
# насчитала 55 наборов вместо 123 — то есть собственный измеритель видел
# меньше половины долга, которую взялся сторожить. Ровно тот случай из
# CLAUDE.md: проверять измеритель на заведомо известном примере ПРЕЖДЕ, чем
# верить его числу. Заведомый пример здесь — `grep -l '"mini_app_api.py"'`.
#
# Упоминание имени в прозе (docstring, комментарий «переехало из
# mini_app_api.py») под литерал не попадает: кавычек вокруг него нет.
# Второй вид привязки — КАВЫЧНЫЙ МОДУЛЬНЫЙ ПУТЬ (`"services.mini_app_api"`
# рядом с `importlib.import_module` + `inspect.getsource`), а также
# `getsource(mini_app_api)` у уже импортированного модуля. Это та же привязка
# к тексту монолита, только записанная точками, и первая версия измерителя её
# не видела: `test_stuck_threshold_is_one_number` держал мини-апп именно так,
# не попал в счёт, и вынос карточки «требует внимания» в mini_app_dashboard
# покраснел в CI уже после пуша. Обычный `from services import mini_app_api`
# под привязку НЕ попадает: вызов ручки через импорт переезд переживает,
# потому что маршруты по-прежнему подключает setup_routes.
PIN = re.compile(
    r'["\']mini_app_api\.py["\']'
    r'|["\']services\.mini_app_api["\']'
    r'|getsource\(\s*_?mini_app_api\b')


# Третий вид — ЧЕРЕЗ `__file__` ИМПОРТИРОВАННОГО МОДУЛЯ
# (`from services import mini_app_api as M` … `Path(M.__file__).read_text()`).
# Это тоже чтение текста монолита, но в нём нет ни имени файла, ни точек, и
# первые две версии измерителя его не видели: так пряталось СЕМЬ наборов,
# каждый из которых краснел бы при выносе своего участка. Обычный импорт без
# `__file__` под привязку не попадает — вызов ручки переезд переживает.
def _module_file_pins(text: str) -> bool:
    for alias in re.findall(r"from services import mini_app_api(?:\s+as\s+(\w+))?",
                            text):
        if re.search(re.escape(alias or "mini_app_api") + r"\.__file__", text):
            return True
    return False


def _pinned() -> list[str]:
    out = []
    for path in sorted(TESTS.glob("test_*.py")):
        # Этот файл сам носит имя монолита в своём регулярном выражении.
        #
        # test_workflow_actions_go_through_the_engine — законное исключение:
        # его утверждение ОТРИЦАТЕЛЬНОЕ («копии ручки в монолите не осталось»),
        # и по всему слою оно ложно по определению, потому что ручка живёт в
        # вынесенном модуле. Такому утверждению нужен именно один файл.
        if path.name in ("miniapp_source.py", pathlib.Path(__file__).name,
                         "test_workflow_actions_go_through_the_engine.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if PIN.search(text) or _module_file_pins(text):
            out.append(path.name)
    return out


def test_the_detector_still_finds_the_pins():
    """Самопроверка: пустой список — это сломанный измеритель, а не чистота."""
    assert _pinned(), "ни одного набора с путём к монолиту — проверка измеряет не то"


def test_the_number_of_pinned_test_files_only_shrinks():
    pinned = _pinned()
    assert len(pinned) <= CEILING, (
        f"наборов, прибитых к пути монолита, стало {len(pinned)} вместо "
        f"{CEILING}. Новая проверка про API-слой обязана брать источник из "
        f"tests.miniapp_source (api_source / api_function / api_routes), иначе "
        f"она ослепнет при первом же выносе ручки.\n  "
        + "\n  ".join(pinned[-12:]))
    if len(pinned) < CEILING:
        raise AssertionError(
            f"долг уменьшился до {len(pinned)} — опустите CEILING в этом файле "
            f"тем же коммитом, иначе потолок перестанет сторожить рост")


# ── Тот же долг на экранном слое ─────────────────────────────────────────────
#
# Экраны уехали из `mini_app/index.html` в `mini_app/screens/*.js` — сейчас
# девятнадцать модулей. Проверка, которая ищет экранный код в index.html,
# после выноса экрана ведёт себя двояко. Утверждение «такое-то имя есть»
# покраснеет, и его починят. Утверждение ОТРИЦАТЕЛЬНОЕ или подсчёт останутся
# зелёными, глядя не туда, — и это тот же самый дорогой случай, что и у
# храповика про ручки: защита цела на вид и мертва на деле.
#
# Правило поэтому механическое: если набор упоминает имя, которое живёт ТОЛЬКО
# в экранном модуле, он обязан читать и экранный слой — `screens/*.js` напрямую
# или через `tests.miniapp_source`. Читать при этом index.html совершенно
# законно: в нём остаётся отпечаток экрана (тег `<script src="screens/…">` и
# его порядок), и это проверяют отдельно.
_SCREEN_GLUE = ("miniapp_source", "screen_files", "source_of",
                "js_function", "js_functions")

# Путь к экранному модулю собирают и по частям: `os.path.join(ROOT,
# "mini_app", "screens", "cmdcenter.js")`. Первая версия этого правила искала
# только склеенный `screens/` и выдала ровно одну находку — и та оказалась
# ложной: `test_cmdcenter_numbers_lead_somewhere` читает модуль именно
# через join. Та же ошибка, что описана выше про путь к монолиту, повторённая
# на экранах, и снова её поймала проверка измерителя, а не прогон.
_READS_SCREENS = re.compile(r'screens/|["\']screens["\']')


def _screen_only_names() -> dict[str, str]:
    """Имена, объявленные в экранных модулях и отсутствующие в index.html."""
    mini = ROOT / "mini_app"
    index = (mini / "index.html").read_text(encoding="utf-8", errors="replace")
    out: dict[str, str] = {}
    for path in sorted((mini / "screens").glob("*.js")):
        src = path.read_text(encoding="utf-8", errors="replace")
        for name in re.findall(r"\b(?:function|const|let|var)\s+(_?[A-Za-z][\w$]{3,})",
                               src):
            if name not in index:
                out.setdefault(name, path.name)
    return out


def _blind_to_the_screens(texts: dict[str, str] | None = None) -> list[str]:
    """Наборы, судящие экранный код по одному index.html."""
    names = _screen_only_names()
    if texts is None:
        texts = {p.name: p.read_text(encoding="utf-8", errors="replace")
                 for p in sorted(TESTS.glob("test_*.py"))}
    out = []
    for fname, text in sorted(texts.items()):
        if fname == pathlib.Path(__file__).name or "index.html" not in text:
            continue
        if _READS_SCREENS.search(text) or any(g in text for g in _SCREEN_GLUE):
            continue
        hit = sorted(n for n in names if re.search(r"[\"']" + re.escape(n), text))
        if hit:
            out.append(f"{fname}: {', '.join(hit[:4])} (экран {names[hit[0]]})")
    return out


def test_the_screen_rule_sees_the_screen_names():
    """Пустой список имён сделал бы правило ниже зелёным ни на чём."""
    names = _screen_only_names()
    assert len(names) >= 100, (
        f"имён, живущих только в экранных модулях, найдено {len(names)}: "
        "правило ниже сторожит пустоту")


def test_the_screen_rule_is_red_on_a_set_that_judges_by_one_file():
    """Обратный контроль: иначе зелёный ниже не значит ничего."""
    name = next(iter(_screen_only_names()))
    bad = _blind_to_the_screens({"test_fake.py": (
        f'html = _read("mini_app/index.html")\nassert "{name}" not in html\n')})
    assert bad, (
        "правило не видит набора, который ищет экранное имя в одном index.html")
    good = _blind_to_the_screens({"test_fake.py": (
        f'html = _read("mini_app/index.html")\n'
        f'js = _read("mini_app/screens/dashboard.js")\n'
        f'assert "{name}" in js\n')})
    assert not good, "правило ругается на набор, который читает экранный слой"


def test_no_test_judges_screen_code_by_index_html_alone():
    blind = _blind_to_the_screens()
    assert not blind, (
        "набор ищет в `mini_app/index.html` имя, которое живёт только в "
        "`mini_app/screens/*.js`: после выноса экрана такая проверка не "
        "краснеет, а слепнет — берите источник из tests.miniapp_source "
        "или читайте сам экранный модуль:\n  " + "\n  ".join(blind))
