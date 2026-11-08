"""Мини-апп без комментариев — для отдачи по сети.

index.html весит 2,2 МБ, и пятая часть скрипта в нём — комментарии: подробные
объяснения «почему так» на русском. Это правильно для исходника и бесполезно
телефону, который качает их по мобильной сети при каждом холодном старте
(замер: deploy/scripts/cold_start.mjs). Русский текст жмётся хуже кода, так
что и после gzip это десятки килобайт.

Здесь — вырезание комментариев и отступов при отдаче, без внешних
инструментов (сервер на Python, сборки нет). Код не переписывается: удаляются
только комментарии JS/CSS/HTML и пробелы в начале строк. Лексер различает
строки, шаблонные строки с ${...} любой вложенности и литералы регулярных
выражений, поэтому «//» в URL или в регэкспе остаётся на месте.
tests/test_miniapp_minify.py сверяет результат с исходником по синтаксическому
дереву TypeScript: дерево обязано совпасть один в один.

Любой сбой — отдаём исходник как есть: лишние килобайты лучше сломанного
приложения.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# После этих символов «/» начинает регулярное выражение, а не деление.
_RE_AFTER_PUNCT = set("(,=:[!&|?{};+-*%<>~^")
_RE_AFTER_WORDS = {"return", "typeof", "case", "in", "of", "new", "delete", "void",
                   "throw", "else", "do", "yield", "await", "instanceof"}
_WORD = re.compile(r"[A-Za-z_$][\w$]*$")


def strip_js(src: str) -> str:
    out: list[str] = []
    i, n = 0, len(src)
    # Стек контекстов: "code" или "tpl"; у code — глубина фигурных скобок,
    # чтобы понять, где кончается ${...} внутри шаблонной строки.
    stack: list[list] = [["code", 0]]
    last = ""          # последний значимый фрагмент кода (для выбора / vs регэксп)
    at_line_start = True

    def emit(s: str) -> None:
        out.append(s)

    while i < n:
        ctx = stack[-1]
        if ctx[0] == "tpl":
            c = src[i]
            if c == "\\":
                emit(src[i:i + 2]); i += 2; continue
            if c == "`":
                emit(c); i += 1; stack.pop(); last = "`"; continue
            if c == "$" and i + 1 < n and src[i + 1] == "{":
                emit("${"); i += 2; stack.append(["code", 0]); last = "{"; continue
            emit(c); i += 1; continue

        c = src[i]
        # Отступы в начале строки кода.
        if at_line_start and c in " \t":
            i += 1; continue
        at_line_start = False
        if c == "\n":
            # Пустые строки схлопываем: перевод строки один (он нужен для ASI).
            if not out or out[-1].endswith("\n"):
                pass
            else:
                emit("\n")
            i += 1; at_line_start = True; continue
        if c in " \t\r":
            emit(c); i += 1; continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            if j < 0:
                raise ValueError("незакрытый комментарий")
            body = src[i:j + 2]
            i = j + 2
            # Комментарий с переводом строки ведёт себя как перевод строки (ASI).
            if "\n" in body:
                if out and not out[-1].endswith("\n"):
                    emit("\n")
                at_line_start = True
            else:
                emit(" ")
            continue
        if c in "'\"":
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 1
                elif src[j] == "\n":
                    raise ValueError("перевод строки в строке")
                j += 1
            emit(src[i:j + 1]); i = j + 1; last = c; continue
        if c == "`":
            emit(c); i += 1; stack.append(["tpl", 0]); continue
        if c == "/":
            prev = last
            is_re = (prev == "" or prev[-1] in _RE_AFTER_PUNCT
                     or prev in _RE_AFTER_WORDS)
            if is_re:
                j, in_cls = i + 1, False
                while j < n:
                    ch = src[j]
                    if ch == "\\":
                        j += 2; continue
                    if ch == "\n":
                        raise ValueError("перевод строки в регэкспе")
                    if in_cls:
                        if ch == "]":
                            in_cls = False
                    elif ch == "[":
                        in_cls = True
                    elif ch == "/":
                        break
                    j += 1
                j += 1
                while j < n and (src[j].isalnum() or src[j] == "_"):
                    j += 1   # флаги
                emit(src[i:j]); i = j; last = "re"; continue
            emit(c); i += 1; last = "/"; continue
        if c == "{":
            ctx[1] += 1
        elif c == "}":
            if ctx[1] == 0 and len(stack) > 1:
                # Конец ${...}: назад в шаблонную строку.
                stack.pop(); emit(c); i += 1; continue
            ctx[1] -= 1
        m = re.match(r"[A-Za-z_$][\w$]*|\d[\w.]*", src[i:i + 64])
        if m:
            emit(m.group(0)); i += m.end(); last = m.group(0); continue
        emit(c); i += 1; last = c
    if len(stack) != 1:
        raise ValueError("незакрытая шаблонная строка")
    return "".join(out)


def strip_css(src: str) -> str:
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                raise ValueError("незакрытый комментарий CSS")
            i = j + 2; continue
        if c in "'\"":
            j = src.find(c, i + 1)
            while j > 0 and src[j - 1] == "\\":
                j = src.find(c, j + 1)
            if j < 0:
                raise ValueError("незакрытая строка CSS")
            out.append(src[i:j + 1]); i = j + 1; continue
        out.append(c); i += 1
    return re.sub(r"\n[ \t]*(?:\n[ \t]*)*", "\n", "".join(out))


_SCRIPT = re.compile(r"(<script>)(.*?)(</script>)", re.S)
_STYLE = re.compile(r"(<style>)(.*?)(</style>)", re.S)
_HTML_COMMENT = re.compile(r"<!--(?!\[if).*?-->", re.S)


def minify_html(src: str) -> str:
    """index.html: комментарии в разметке, во встроенных <script> и <style>."""
    parts: list[str] = []
    pos = 0
    blocks = sorted([(m, strip_js) for m in _SCRIPT.finditer(src)]
                    + [(m, strip_css) for m in _STYLE.finditer(src)],
                    key=lambda t: t[0].start())
    for m, fn in blocks:
        if m.start() < pos:
            continue
        parts.append(_HTML_COMMENT.sub("", src[pos:m.start()]))
        parts.append(m.group(1) + fn(m.group(2)) + m.group(3))
        pos = m.end()
    parts.append(_HTML_COMMENT.sub("", src[pos:]))
    return "".join(parts)


def safe(fn, src: str, label: str) -> str:
    """Применить минификатор; при любой ошибке — исходник как есть."""
    try:
        return fn(src)
    except Exception as exc:  # noqa: BLE001 — сломанный мини-апп хуже лишних байт
        log.warning("miniapp_minify: %s отдан без сжатия комментариев: %s", label, exc)
        return src
