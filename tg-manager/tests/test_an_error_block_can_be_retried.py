"""Ошибка выбрасывала экран вместо того, чтобы перезапросить данные.

`errHtml(msg, retryFn)` без второго аргумента рисует «← Назад»: выход есть, но
он уводит с экрана, а человек хотел именно этот экран — просто ответ не
пришёл. Таких вызовов было 27, и у каждого аргументы экрана лежали рядом, в
той же функции.

Строку вызова нельзя собирать склейкой: аргумент бывает числом (id бота),
строкой (uuid контакта, название воронки) и булевым флагом (догрузка
страницы). Кавычки выбирает `retryCall` — один раз и для всех.
"""
from __future__ import annotations

import re

from tests.miniapp_source import js_function, miniapp_source


def _no_comments(src: str) -> str:
    """Строчные комментарии вон: в них `errHtml()` поминается словом.

    Дважды уже было, что собственный комментарий к правке становился находкой
    проверки — проверка читает код, а не рассказ о нём.
    """
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


def _err_calls(src: str) -> list[str]:
    """Аргументы каждого errHtml(...) — по балансу скобок, а не регуляркой."""
    src = _no_comments(src)
    out: list[str] = []
    for m in re.finditer(r"errHtml\(", src):
        depth, j = 0, m.end() - 1
        while j < len(src):
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
                if depth == 0:
                    out.append(src[m.end():j])
                    break
            j += 1
    return out


def test_no_error_block_is_left_without_a_way_to_retry():
    bare = [c for c in _err_calls(miniapp_source())
            if c.strip() and "," not in c and "retryCall" not in c]
    assert not bare, (
        "ошибка без повтора оставляет только «Назад» — экран выбрасывается "
        f"целиком, а данные можно было перезапросить: {bare[:5]}")


def test_the_retry_quotes_its_arguments_itself():
    body = js_function("retryCall")
    assert "escJs(a)" in body, (
        "название воронки с кавычкой порвало бы атрибут onclick")
    assert "typeof a === 'number'" in body and "'boolean'" in body, (
        "число и флаг в кавычках пришли бы в экран строкой — "
        "`openOpDetail('7')` ищет операцию не по тому ключу")
    assert "'null'" in body, "пропущенный аргумент дал бы `openX()` без него"


def test_a_screen_with_arguments_retries_with_them():
    """Повтор без аргумента перезапрашивает ЧУЖОЙ экран (или никакой)."""
    assert "retryCall('openOpDetail', opId)" in js_function("openOpDetail")
    assert "retryCall('openContactDetail', id)" in js_function("openContactDetail")
    assert "retryCall('openBot', botId)" in js_function("openBot")

def _empty_error_calls(src: str) -> list[str]:
    """Аргументы каждого empty('⚠️', …) — тоже по балансу скобок.

    Значок предупреждения у empty() означает именно сбой: ветка выхода в самом
    empty() на него и смотрит (`isErr = ico === '⚠️'`).
    """
    src = _no_comments(src)
    out: list[str] = []
    for m in re.finditer(r"empty\('⚠️'", src):
        depth, j, q = 0, src.index("(", m.start()), None
        while j < len(src):
            c = src[j]
            if q:
                if c == "\\":
                    j += 2
                    continue
                if c == q:
                    q = None
            elif c in "'\"`":
                q = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    out.append(src[m.end():j])
                    break
            j += 1
    return out


def test_an_error_dressed_as_an_empty_state_also_retries():
    """`empty('⚠️','Ошибка',errRu(e))` — тот же потерянный экран.

    Исключение одно и названо вслух: «Сравнение неполное» в поиске дублей —
    предел сравнения, а не сбой загрузки, повторять там нечего.
    """
    bare = [c for c in _empty_error_calls(miniapp_source())
            if "fn:" not in c and "action" not in c]
    assert len(bare) == 1 and "Сравнение неполное" in bare[0], (
        "ошибка со значком, но без повтора — выход только «Назад», то есть "
        f"экран выбрасывается целиком: {[c[:80] for c in bare]}")
