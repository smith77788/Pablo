"""Подтверждение удаления называет то, что исчезнет.

Было: 31 необратимое действие спрашивало «Удалить шаблон?» — без единого
признака, КАКОЙ шаблон. В списке из двадцати строк промах пальцем неотличим от
попадания: человек подтверждал вслепую и узнавал об ошибке по пропавшей строке,
а вернуть её нечем.

Стало: `confirmDelete(what, el, note)` подставляет в вопрос имя объекта, а
`rowName(el)` берёт это имя ИЗ DOM — из строки списка или из шапки
экрана-карточки.

Почему имя читается из DOM, а не прокидывается через onclick: названия
приходят от пользователя и из Telegram. Апостроф в имени («O'Brien») разорвал
бы атрибут `onclick="deleteContact('O'Brien')"` — кнопка перестала бы работать
вовсе. Поэтому в разметку уходит только `this`.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_source

SRC = miniapp_source()

# Функции удаления, которые СПРАВЕДЛИВО обходятся без rowName. Список закрытый:
# всё остальное обязано звать confirmDelete, иначе новая кнопка удаления снова
# приедет безымянной.
ALLOWED_WITHOUT_NAME = {
    # аватар у бота один, это не строка списка — называть нечего
    "deleteBotAvatar",
    # имя команды уже стоит в тексте вопроса: «Удалить команду /start?»
    "deleteCmd",
    # вебхук у бота один, кнопка на его же экране; текст объясняет последствие
    "deleteWebhook",
}


def _fn_body(name: str) -> str:
    """Тело функции по балансу скобок.

    Границы берём по коду, а не отрезком фиксированной длины: сдвинется
    соседняя функция — и проверка начнёт смотреть не туда, оставаясь зелёной.
    Скобки сигнатуры пропускаем отдельно, иначе первая `{` найдётся в значении
    параметра по умолчанию (`opts={}`).
    """
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


def _destructive_functions() -> list[str]:
    return sorted(set(re.findall(r"^(?:async )?function ((?:delete|remove|del)[A-Z]\w*)\s*\(", SRC, re.M)))


def _markup_calls(name: str) -> list[str]:
    r"""Аргументы вызова из разметки, по балансу скобок.

    Отрезком `\(([^)]*)\)` пользоваться нельзя: аргумент бывает выражением со
    своими скобками (`${Number(p.acc_count||0)}`), и отрезок обрывается на
    первой закрывающей — проверка видит обрубок и ругается на здоровый код.
    """
    out = []
    for m in re.finditer(r"""on(?:click|keydown)=["'][^"']*\b""" + re.escape(name) + r"\(", SRC):
        i = m.end() - 1
        depth = 0
        for j in range(i, len(SRC)):
            if SRC[j] == "(":
                depth += 1
            elif SRC[j] == ")":
                depth -= 1
                if depth == 0:
                    out.append(SRC[i + 1:j])
                    break
    return out


def test_helpers_exist():
    assert "function rowName(el)" in SRC, "хелпер имени строки исчез"
    assert "function confirmDelete(what, el, note)" in SRC, "хелпер подтверждения исчез"


def test_every_destructive_confirm_names_its_target():
    """Ратчет: новая функция удаления не проедет без имени объекта."""
    anonymous = []
    for name in _destructive_functions():
        if name in ALLOWED_WITHOUT_NAME:
            continue
        body = _fn_body(name)
        if "askConfirm(" not in body and "confirmDelete(" not in body:
            continue  # подтверждения нет вовсе — это уже другая проверка
        if "confirmDelete(" in body or "rowName(" in body:
            continue
        anonymous.append(name)
    assert not anonymous, (
        "удаление спрашивает подтверждение, но не называет объект: "
        + ", ".join(anonymous)
        + ". Используйте confirmDelete(what, _el, note) и передайте `this` из onclick."
    )


def test_confirming_functions_accept_the_element():
    """Имя берётся из элемента, значит элемент обязан доезжать до функции."""
    for name in _destructive_functions():
        body = _fn_body(name)
        if "confirmDelete(" not in body and "rowName(" not in body:
            continue
        sig = re.search(r"^(?:async )?function " + re.escape(name) + r"\s*\(([^)]*)\)", SRC, re.M)
        params = [p.strip() for p in sig.group(1).split(",") if p.strip()]
        assert len(params) >= 2, f"{name}: нет параметра под элемент строки"


def test_call_sites_pass_the_element():
    """`this` доезжает из разметки: без него вопрос молча вернётся к безымянному."""
    missing = []
    for name in _destructive_functions():
        body = _fn_body(name)
        if "confirmDelete(" not in body and "rowName(" not in body:
            continue
        calls = _markup_calls(name)
        assert calls, f"{name}: не нашлось ни одного вызова из разметки"
        for args in calls:
            if not re.search(r"\bthis\s*$", args.strip()):
                missing.append(f"{name}({args})")
    assert not missing, "вызов не передаёт элемент строки: " + ", ".join(missing)


def test_name_is_read_from_dom_not_interpolated():
    """Апостроф в имени не должен разрывать атрибут onclick.

    Поэтому ни один вызов confirmDelete не собирает имя сам: второй аргумент —
    всегда переменная с элементом, а не подстановка данных.
    """
    for m in re.finditer(r"confirmDelete\(([^;]*?)\)\)", SRC):
        args = m.group(1)
        assert "${" not in args, f"имя подставляется в текст вместо чтения из DOM: {args[:80]}"
    # У rowName один параметр — элемент. Появится второй (имя строкой) — это и
    # будет та самая подстановка, только через другую дверь.
    sig = re.search(r"^function rowName\s*\(([^)]*)\)", SRC, re.M)
    assert sig, "rowName не найден"
    params = [x.strip() for x in sig.group(1).split(",") if x.strip()]
    assert params == ["el"], f"rowName принимает не только элемент: {params}"
    body = _fn_body("rowName")
    # Каждая ветка возврата имени идёт от textContent узла, а не от данных.
    returns = [r.strip() for r in re.findall(r"\breturn ([^;]+);", body)]
    named = [r for r in returns if r not in ("''",)]
    assert named, "rowName ничего не возвращает"
    for r in named:
        assert re.search(r"\bt\b|\bht\b", r), f"rowName возвращает не вычитанное из DOM: {r}"
    assert body.count("textContent") >= 2, "rowName перестал читать текст узлов"


def test_consequence_notes_survive():
    """Предупреждение о последствии — часть вопроса, а не украшение.

    Эти четыре удаления уносят с собой данные, которые человек не ждёт
    потерять; текст об этом проверяем поимённо.
    """
    expected = {
        "deleteTpl": "Его текст не восстановить.",
        "deleteComp": "Собранная по нему история пропадёт.",
        "deleteKw": "История позиций по нему больше не собирается.",
        "deleteFolder": "Сама папка в Telegram останется.",
    }
    for name, note in expected.items():
        assert note in _fn_body(name), f"{name}: пропало предупреждение о последствии"


def test_running_campaign_warning_kept_its_name():
    """У рассылки текст собирается вручную (у идущей кампании он особый) —
    имя должно попасть в обе ветки, а не только в одну."""
    body = _fn_body("deleteDm")
    assert body.count("_nm") >= 3, "имя кампании подставляется не во все ветки вопроса"
    assert "ИДУЩУЮ кампанию" in body, "пропало предупреждение об остановке рассылки"
