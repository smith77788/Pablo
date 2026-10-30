"""Текст ошибки, который доходит до владельца, написан по-русски.

Владелец не читает по-английски. Фронтовый `errRu` считает сообщение русским,
если в нём есть хоть одна кириллическая буква, — поэтому восемь мест в API,
которые строили текст как f"Ошибка: {str(exc)}", проводили английское тело
исключения (Telethon, asyncpg, aiohttp) прямо на экран под русским префиксом.
Закрыто в единственной двери наружу — `_err`.

Проверка должна печатать знаменатель и доказывать, что видит, с обеих сторон:
русские тексты (их больше тысячи) она не трогает, а реальные сообщения
библиотек ловит. Иначе «английского нет» означает лишь «мерка молчит».
"""

from __future__ import annotations

import glob
import re

from services.mini_app_api import _err, _looks_english, _owner_ru

_RU = re.compile(r"[А-Яа-яЁё]")
# Литерал в коде может содержать f-подстановку. На экран попадает её ЗНАЧЕНИЕ
# (имя канала, список полей), а не выражение `', '.join(sorted(allowed))`, —
# поэтому выражение здесь заменяется нейтральным словом: проверяем слова
# автора, а не внутренности f-строки.
_SUBST = re.compile(r"\{[^{}]*\}")


def _err_literals():
    """Все русские литералы, которые уходят в `_err`, с местом в коде."""
    out = []
    for path in sorted(glob.glob("services/mini_app_*.py")):
        src = open(path, encoding="utf-8").read()
        for m in re.finditer(
            r"(?<![A-Za-z0-9_])_err\(\s*(?:f?\"([^\"\\]{0,300})\"|f?'([^'\\]{0,300})')",
            src,
        ):
            text = m.group(1) or m.group(2) or ""
            if not _RU.search(text):
                continue
            out.append((f"{path}:{src[: m.start()].count(chr(10)) + 1}", text))
    return out


# Настоящие сообщения, которые владелец видел бы на экране: Telethon, asyncpg,
# aiohttp, стандартная библиотека, баг рендера в самом мини-аппе.
_REAL_ENGLISH = [
    "Chat admin privileges are required to do that",
    "The user has blocked peer invitations",
    "A wait of 300 seconds is required (caused by ResolveUsernameRequest)",
    "cannot perform operation: another operation is in progress",
    "connection was closed in the middle of operation",
    "Cannot connect to host 1.2.3.4:1080 ssl:default",
    "Peer id invalid",
    "Cannot read properties of undefined (reading 'id')",
    "No such file or directory",
    "server rejected the request",
]


def test_russian_error_texts_are_not_touched():
    lits = _err_literals()
    changed = [
        f"{where}: {text!r} -> {_owner_ru(_SUBST.sub('значение', text))!r}"
        for where, text in lits
        if _owner_ru(_SUBST.sub("значение", text)) != _SUBST.sub("значение", text)
    ]
    assert len(lits) > 1000, (
        f"русских текстов ошибок на проверке всего {len(lits)} — сломан разбор, "
        "а не код: мерка перестала их находить"
    )
    assert not changed, (
        f"русский текст ошибки испорчен очисткой (проверено {len(lits)}): "
        f"{changed[:5]}"
    )


# Русские тексты с латиницей внутри: имя поля, название канала, адрес прокси,
# значение статуса. Одно служебное слово тут не фраза — иначе владелец
# перестал бы узнавать, О ЧЁМ ошибка.
_RUSSIAN_WITH_LATIN = [
    "Некорректное значение: from",
    'Канал "The Daily" не найден',
    "Прокси socks5://host.example:1080 не отвечает",
    "Недопустимое поле. Доступны: alias, title",
    "Операцию нельзя запустить сейчас в статусе «paused»",
    "Бот @my_shop_bot уже добавлен",
]


def test_latin_inside_a_russian_message_is_not_a_phrase():
    spoiled = [s for s in _RUSSIAN_WITH_LATIN if _owner_ru(s) != s]
    assert not spoiled, (
        f"русский текст испорчен из-за латинского слова внутри "
        f"(образцов {len(_RUSSIAN_WITH_LATIN)}): {spoiled}"
    )


def test_a_real_english_exception_never_reaches_the_owner():
    leaked = [s for s in _REAL_ENGLISH if _looks_english(_owner_ru(s))]
    assert not leaked, (
        f"английский текст дошёл до владельца (образцов {len(_REAL_ENGLISH)}): "
        f"{leaked}"
    )
    mute = [s for s in _REAL_ENGLISH if not _RU.search(_owner_ru(s))]
    assert not mute, f"после очистки текст без кириллицы: {mute}"


def test_the_russian_head_of_the_message_is_kept():
    """Владельцу важно ЧТО не получилось; это знает место вызова, не библиотека."""
    got = _owner_ru("Не удалось добавить прокси: Cannot connect to host 1.2.3.4")
    assert got.startswith("Не удалось добавить прокси"), got
    assert "Cannot" not in got, got
    bare = _owner_ru("cannot perform operation: another operation is in progress")
    assert _RU.search(bare), bare
    assert "operation" not in bare, bare


def test_the_cleaning_is_wired_into_the_single_door():
    """`_owner_ru` в самом `_err`, до сборки тела, а не только объявлена рядом."""
    src = open("services/mini_app_api.py", encoding="utf-8").read()
    lines = src.splitlines()
    start = next(
        i for i, ln in enumerate(lines)
        if ln.startswith("def _err(msg: str, status: int = 400)")
    )
    # Тело — до первой следующей строки нулевого отступа. Границу по длине
    # ставить нельзя: дописанный комментарий молча выносит проверку за кадр.
    end = next(
        (i for i in range(start + 1, len(lines))
         if lines[i].strip() and not lines[i][0].isspace()),
        len(lines),
    )
    body = "\n".join(lines[start:end])
    assert "_owner_ru(" in body, "в `_err` нет вызова `_owner_ru` — очистка мимо двери"
    assert body.index("_owner_ru(") < body.index('body = {"error"'), (
        "очистка после сборки тела — в ответ уйдёт неочищенный текст"
    )


def test_the_paywall_marker_survives_the_cleaning():
    """Пейволл ищется по слову «подписк»; очистка не должна его срезать."""
    r = _err("Требуется подписка Pro for this feature", 403)
    assert r.status == 403
    assert b"paywall" in r.body, r.body


def test_the_login_screen_does_not_show_a_raw_exception():
    src = open("mini_app/screens/account_login.js", encoding="utf-8").read()
    raw = re.findall(r"_accErr\(\s*String\([^)]*\.message[^)]*\)", src)
    assert not raw, f"вход показывает сырой текст исключения: {len(raw)} мест"
    assert len(re.findall(r"_accErr\(\s*errRu\(", src)) >= 10, (
        "места перехвата ошибок на экране входа перестали ходить через errRu"
    )


def _json_error_doors():
    """Ответы, которые несут текст ошибки МИМО `_err` — у них своя дверь."""
    out = []
    for path in sorted(glob.glob("services/mini_app_*.py")):
        src = open(path, encoding="utf-8").read()
        for m in re.finditer(r"_json_resp\(\s*\{", src):
            i = m.end()
            depth, j = 1, i
            while j < len(src) and depth:
                if src[j] == "{":
                    depth += 1
                elif src[j] == "}":
                    depth -= 1
                j += 1
            arg = src[i : j - 1]
            if not re.search(r"""["'](?:error|message)["']\s*:""", arg):
                continue
            out.append((f"{path}:{src[: m.start()].count(chr(10)) + 1}", arg))
    return out


def test_a_door_past_err_still_speaks_russian():
    """`_err` — не единственный путь наружу; у обходных путей то же правило.

    Правило здесь по факту, а не по вызову: текст либо литерал (его писал
    автор, и он по-русски), либо выражение — тогда через `_owner_ru`.
    """
    doors = _json_error_doors()
    bad = []
    for where, arg in doors:
        for m in re.finditer(r"""["'](?:error|message)["']\s*:\s*(.{0,120})""", arg, re.S):
            # Литерал может быть собран в скобках и перенесён на строки —
            # снимаем обёртку, иначе проверка видит «(» и считает место
            # необъяснённым.
            val = re.sub(r"^[(\s]+", "", m.group(1))
            if re.match(r"""(?:f\s*)?["']|\{""", val):
                continue
            if "_owner_ru(" in val or "_err(" in val:
                continue
            bad.append(f"{where}: {val[:60]!r}")
    assert len(doors) >= 3, (
        f"обходных дверей на проверке всего {len(doors)} — сломан разбор, "
        "а не код"
    )
    assert not bad, f"текст наружу мимо `_err` и без очистки (дверей {len(doors)}): {bad}"
