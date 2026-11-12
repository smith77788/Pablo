"""Ленивая загрузка кода мини-аппа: на старте — только то, что нужно старту.

Зачем. Первое открытие мини-аппа тянуло ~510 КБ (brotli) — из них ~420 КБ
скрипт: 1,3 МБ кода в index.html и 44 файла экранов. Почти весь он —
объявления функций разделов, которые на старте не вызываются ни разу:
главный экран открывается, а код «Разведки рекламы», «Менеджера продаж» и ещё
восьмидесяти разделов уже скачан, разобран и лежит в памяти.

Как. Сервер при отдаче (как и вырезание комментариев, services/miniapp_minify.py)
разбирает каждый скрипт на верхнеуровневые объявления функций и остальное.
Функция уходит в ленивый кусок (`lazy/<хэш>.js`), если на неё нигде нет
ничего, кроме прямого вызова `имя(...)`, `typeof имя` или вызова из строки
обработчика (`onclick="имя(...)"`). На её место ставится заглушка с тем же
именем на `window`: первый вызов подгружает кусок и вызывает настоящую
функцию с теми же `this` и аргументами, возвращая её результат. Код разделов
при этом не переписывается и остаётся в исходниках там же, где был.

Почему это не ломает поведение:
* Заглушка СИНХРОННАЯ. Если кусок ещё не скачан, она берёт его синхронным
  запросом (XHR, тот же origin) и выполняет как обычный классический скрипт —
  поэтому `const x = f()` получает то же значение, что и раньше, а не Promise.
  Синхронный запрос — запасной путь: после старта все куски докачиваются в
  простое (requestIdleCallback), и дальше заглушек уже нет.
* В кусок уходят только функции, на которые нет ссылок-значений: их не
  передают колбэком, не сравнивают, не оборачивают и не переприсваивают.
  Любое другое употребление имени (`x = f`, `f.cache`, `new f`, `{f}`,
  `f = ...`) оставляет функцию на месте. Дубли имён — тоже на месте.
* Объявления функций в классическом скрипте и так поднимаются наверх
  (hoisting): вызвать функцию можно было до её строки. Заглушка ставится
  раньше всех скриптов, так что этот порядок сохранён.
* Строгий режим куска — как у исходного скрипта.

Любой сбой разбора — страница отдаётся целиком, как раньше. Выключатель —
переменная окружения MINIAPP_LAZY=0 и адрес `?full=1` (им же страница
перезагружает себя, если кусок не загрузился).

Проверки: tests/test_miniapp_lazy.py (набор имён сверяется с деревом
TypeScript, каждый кусок разбирается) и deploy/scripts/lazy_walk.mjs (обход
всех разделов в Chromium с ленивой страницей).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import zlib
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# ── Лексер ────────────────────────────────────────────────────────────────

_ID = re.compile(r"[A-Za-z_$\u0080-￿][\w$\u0080-￿]*")
_NUM = re.compile(r"\d[\w.]*|\.\d[\w.]*")
_PUNCT = ("...", ">>>=", "===", "!==", "**=", "<<=", ">>=", ">>>", "&&=", "||=", "??=",
          "=>", "==", "!=", "<=", ">=", "&&", "||", "??", "?.", "++", "--", "+=", "-=",
          "*=", "/=", "%=", "&=", "|=", "^=", "**", "<<", ">>")
_RE_AFTER_PUNCT = set("(,=:[!&|?{};+-*%<>~^")
_RE_AFTER_WORDS = {"return", "typeof", "case", "in", "of", "new", "delete", "void",
                   "throw", "else", "do", "yield", "await", "instanceof"}


@dataclass
class Tok:
    kind: str        # id | num | str | tpl | re | p (пунктуация)
    val: str
    start: int
    end: int
    nl: bool         # перед токеном был перевод строки
    depth: int       # глубина скобок ДО токена


def tokenize(src: str) -> list[Tok]:
    """Токены JS. Комментарии и пробелы выброшены; строки и шаблоны целиком
    (кусок шаблона между `${...}` — отдельный токен tpl). Глубина считает
    (), [], {} и ${}. Ошибка — ValueError."""
    toks: list[Tok] = []
    i, n = 0, len(src)
    depth = 0
    stack: list[str] = []      # что закрывает очередная «}»: "{" или "${"
    nl = True
    last = ""

    def push(kind, val, s, e):
        nonlocal nl, last
        toks.append(Tok(kind, val, s, e, nl, depth))
        nl = False
        last = val if kind in ("id", "p") else kind

    def read_tpl(j):
        """С позиции после ` или после } от ${ — до ` или ${. Возвращает (конец, открылся_ли_${)."""
        while j < n:
            c = src[j]
            if c == "\\":
                j += 2
                continue
            if c == "`":
                return j + 1, False
            if c == "$" and j + 1 < n and src[j + 1] == "{":
                return j + 2, True
            j += 1
        raise ValueError("незакрытая шаблонная строка")

    while i < n:
        c = src[i]
        if c == "\n":
            nl = True
            i += 1
            continue
        if c in " \t\r﻿   ":
            i += 1
            continue
        if c == "/" and src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                raise ValueError("незакрытый комментарий")
            if "\n" in src[i:j]:
                nl = True
            i = j + 2
            continue
        if c in "'\"":
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 1
                elif src[j] == "\n":
                    raise ValueError("перевод строки в строке")
                j += 1
            push("str", src[i:j + 1], i, j + 1)
            i = j + 1
            continue
        if c == "`":
            j, opened = read_tpl(i + 1)
            push("tpl", src[i:j], i, j)
            if opened:
                stack.append("${")
                depth += 1
            i = j
            continue
        if c == "}" and stack and stack[-1] == "${":
            stack.pop()
            depth -= 1
            j, opened = read_tpl(i + 1)
            push("tpl", src[i:j], i, j)
            if opened:
                stack.append("${")
                depth += 1
            i = j
            continue
        m = _ID.match(src, i)
        if m:
            push("id", m.group(0), i, m.end())
            i = m.end()
            continue
        m = _NUM.match(src, i)
        if m:
            push("num", m.group(0), i, m.end())
            i = m.end()
            continue
        if c == "/":
            prev = last
            if prev == "" or prev in _RE_AFTER_WORDS or (len(prev) == 1 and prev in _RE_AFTER_PUNCT) \
                    or (prev not in (")", "]", "}") and toks and toks[-1].kind == "p"
                        and prev not in ("++", "--")):
                j, in_cls = i + 1, False
                while j < n:
                    ch = src[j]
                    if ch == "\\":
                        j += 2
                        continue
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
                    j += 1
                push("re", src[i:j], i, j)
                i = j
                continue
        for p in _PUNCT:
            if src.startswith(p, i):
                break
        else:
            p = c
        if p in ("(", "[", "{"):
            push("p", p, i, i + 1)
            depth += 1
            if p == "{":
                stack.append("{")
        elif p in (")", "]", "}"):
            depth -= 1
            if p == "}":
                if not stack:
                    raise ValueError("лишняя }")
                stack.pop()
            if depth < 0:
                raise ValueError("лишняя закрывающая скобка")
            push("p", p, i, i + 1)
        else:
            push("p", p, i, i + len(p))
        i += len(p)
    if depth != 0 or stack:
        raise ValueError("незакрытая скобка")
    return toks


# ── Верхнеуровневые объявления функций ────────────────────────────────────

@dataclass
class FuncDecl:
    name: str
    start: int       # позиция в исходнике (с async)
    end: int         # после закрывающей }
    name_tok: int    # индекс токена имени
    length: int = 0  # Function.length: параметры до первого со значением по умолчанию


# Токен перед `function`, после которого это ВЫРАЖЕНИЕ, а не объявление.
_EXPR_BEFORE = {"=", "(", ",", ":", "?", "[", "!", "&&", "||", "??", "+", "-", "*", "/",
                "%", "<", ">", "|", "&", "^", "~", "=>", "return", "typeof", "void", "new",
                "delete", "in", "of", "instanceof", "yield", "await", "case", "throw",
                "default", "else", "do", "export", "...", "==", "===", "!=", "!==", "<=",
                ">=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "**", "<<", ">>",
                ">>>", "&&=", "||=", "??=", "?.", "extends"}


def _match(toks: list[Tok], k: int, open_: str, close: str) -> int:
    """Индекс парной закрывающей скобки для toks[k] == open_."""
    d = toks[k].depth
    for j in range(k + 1, len(toks)):
        t = toks[j]
        if t.kind == "p" and t.val == close and t.depth == d:
            return j
    raise ValueError("нет парной скобки")


def _fn_length(toks: list[Tok], open_k: int, close_k: int) -> int:
    """Function.length по токенам списка параметров."""
    d = toks[open_k].depth + 1
    n, cur_simple, any_tok = 0, True, False
    for t in toks[open_k + 1:close_k]:
        if t.depth == d and t.kind == "p" and t.val == ",":
            if not cur_simple:
                return n
            n += 1
            any_tok = False
            continue
        if t.depth == d and t.kind == "p" and t.val in ("=", "..."):
            cur_simple = False
        any_tok = True
    if any_tok and cur_simple:
        n += 1
    return n


def top_level_functions(toks: list[Tok]) -> list[FuncDecl]:
    out: list[FuncDecl] = []
    k, n = 0, len(toks)
    while k < n:
        t = toks[k]
        if not (t.depth == 0 and t.kind == "id" and t.val == "function"):
            k += 1
            continue
        first = k
        if k > 0 and toks[k - 1].kind == "id" and toks[k - 1].val == "async" and not t.nl:
            first = k - 1
        prev = toks[first - 1] if first > 0 else None
        is_decl = prev is None or (prev.kind == "p" and prev.val in (";", "}")) or (
            toks[first].nl and not (prev.kind == "p" and prev.val in _EXPR_BEFORE)
            and not (prev.kind == "id" and prev.val in _EXPR_BEFORE))
        j = k + 1
        if j < n and toks[j].kind == "p" and toks[j].val == "*":
            j += 1
        if not is_decl or j >= n or toks[j].kind != "id":
            k += 1
            continue
        name_k = j
        if j + 1 >= n or toks[j + 1].val != "(":
            k += 1
            continue
        close_par = _match(toks, j + 1, "(", ")")
        if close_par + 1 >= n or toks[close_par + 1].val != "{":
            raise ValueError(f"функция {toks[j].val}: нет тела")
        close_body = _match(toks, close_par + 1, "{", "}")
        out.append(FuncDecl(toks[name_k].val, toks[first].start, toks[close_body].end, name_k,
                            _fn_length(toks, j + 1, close_par)))
        k = close_body + 1
    return out


def top_level_names(toks: list[Tok]) -> set[str]:
    """Имена var/let/const/class верхнего уровня (простые, без деструктуризации
    — деструктуризация даёт имена внутри скобок, их ловит проверка ссылок)."""
    names: set[str] = set()
    for k, t in enumerate(toks):
        if t.depth == 0 and t.kind == "id" and t.val in ("var", "let", "const", "class") \
                and k + 1 < len(toks) and toks[k + 1].kind == "id":
            names.add(toks[k + 1].val)
    return names


# ── Разбор страницы ───────────────────────────────────────────────────────

@dataclass
class Script:
    label: str           # "index#3" или "screens/x.js"
    src: str             # исходник (уже без комментариев — так режет сервер)
    strict: bool
    toks: list[Tok] = field(default_factory=list)
    funcs: list[FuncDecl] = field(default_factory=list)


_USE_STRICT = re.compile(r"""^\s*(?:'use strict'|"use strict")""")


def _strict(src: str) -> bool:
    return bool(_USE_STRICT.match(src))


_GLOBAL_OBJ = {"window", "globalThis", "self"}


def _forbid_dynamic_globals(s: "Script") -> None:
    """`window[k] = ...`, Object.assign(window, …), defineProperty(window, …):
    имя не видно лексеру, а подменить можно любую функцию. Такой код — отказ от
    нарезки целиком (страница уходит полной), а не угадывание."""
    toks = s.toks
    for k, t in enumerate(toks):
        if t.kind != "id" or t.val not in _GLOBAL_OBJ:
            continue
        nxt = toks[k + 1] if k + 1 < len(toks) else None
        if nxt is not None and nxt.val == "[":
            close = _match(toks, k + 1, "[", "]")
            after = toks[close + 1] if close + 1 < len(toks) else None
            if after is not None and after.kind == "p" and after.val.endswith("=") \
                    and after.val not in ("==", "===", "!=", "!==", "<=", ">=", "=>"):
                raise ValueError(f"{s.label}: присваивание {t.val}[…] — нарезка небезопасна")
        prev = toks[k - 1] if k else None
        pp = toks[k - 2] if k >= 2 else None
        if prev is not None and prev.val == "(" and pp is not None and pp.val in ("assign", "defineProperty", "defineProperties"):
            raise ValueError(f"{s.label}: {pp.val}({t.val}, …) — нарезка небезопасна")


def _safe_refs(scripts: list[Script], extra_text: list[str]) -> tuple[set[str], dict[str, str]]:
    """Имена функций, которые можно унести в кусок, и причины отказа для остальных."""
    counts: dict[str, int] = {}
    for s in scripts:
        for f in s.funcs:
            counts[f.name] = counts.get(f.name, 0) + 1
    cand = {nm for nm, c in counts.items() if c == 1}
    why: dict[str, str] = {nm: "объявлена дважды" for nm, c in counts.items() if c > 1}
    lexical: set[str] = set()
    for s in scripts:
        lexical |= top_level_names(s.toks)
    for nm in cand & lexical:
        why[nm] = "имя занято переменной"
    cand -= lexical
    decl_toks = {(id(s), f.name_tok) for s in scripts for f in s.funcs}

    def reject(nm, reason):
        if nm in cand:
            cand.discard(nm)
            why[nm] = reason

    for s in scripts:
        toks = s.toks
        n = len(toks)
        for k, t in enumerate(toks):
            if t.kind in ("str", "tpl"):
                extra_text.append(t.val)
                continue
            if t.kind != "id" or t.val not in cand:
                continue
            if (id(s), k) in decl_toks:
                continue
            prev = toks[k - 1] if k else None
            nxt = toks[k + 1] if k + 1 < n else None
            if prev is not None and prev.kind == "p" and prev.val in (".", "?."):
                # obj.имя — чужое свойство, не наша функция. Но window.имя —
                # она самая: `window.goTab = обёртка` (screens/desktop.js)
                # подменяет функцию, и кусок затёр бы обёртку.
                if not (k >= 2 and toks[k - 2].kind == "id" and toks[k - 2].val in _GLOBAL_OBJ):
                    continue
                prev = toks[k - 3] if k >= 3 else None
            if prev is not None and prev.kind == "id" and prev.val == "typeof":
                continue
            if prev is not None and prev.kind == "id" and prev.val in ("new", "function"):
                reject(t.val, f"{prev.val} {t.val} ({s.label})")
                continue
            if nxt is not None and nxt.kind == "p" and nxt.val in ("(", "?."):
                if nxt.val == "?." and not (k + 2 < n and toks[k + 2].val == "("):
                    reject(t.val, f"{t.val}?.свойство ({s.label})")
                continue
            # Ключ объекта `{ имя: ... }` — не ссылка, но отличить от `a ? имя : b`
            # лексером нельзя: остаёмся на месте.
            reject(t.val, f"ссылка-значение ({s.label})")

    # Строки и разметка: «имя(» — вызов из обработчика; голое имя в строке —
    # это `window[имя]()` (navGo и плитки), заглушка лежит на window. Но
    # `имя.` или `имя =` в строке — уже не вызов.
    if cand:
        blob = "\n".join(extra_text)
        pat = re.compile(r"(?<![\w$.])(" + "|".join(sorted(map(re.escape, cand), key=len,
                                                            reverse=True)) + r")(?![\w$])\s*(.?)")
        for m in pat.finditer(blob):
            nm, after = m.group(1), m.group(2)
            if after in ("=", "."):
                nxt2 = blob[m.end(): m.end() + 1]
                if after == "=" and nxt2 in ("=", ">"):
                    continue
                reject(nm, f"в строке: {nm}{after}")
    return cand, why


def _chunk_key(name: str) -> int:
    return zlib.crc32(name.encode()) & 0xFFFF


@dataclass
class Chunk:
    label: str
    strict: bool
    names: list[str]
    body: str
    hash: str = ""
    lengths: dict = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"lazy/{self.hash}.js"


@dataclass
class Split:
    html: str                       # страница со заглушками и остатком
    chunks: list[Chunk]
    lazy_names: int
    kept: dict[str, str]            # имя → почему осталось
    boot_kept: int
    markup: list = field(default_factory=list)   # куски разметки
    deferred: int = 0                            # сколько контейнеров отложено


_TARGET = 128 * 1024    # ~128 КБ исходника на кусок: ~25 КБ brotli
_MAX = 256 * 1024


def _cut(script: Script, lazy: set[str]) -> tuple[str, list[tuple[str, str]]]:
    """Остаток скрипта и [(имя, текст функции)] ушедших в куски, по порядку."""
    rest: list[str] = []
    gone: list[tuple[str, str]] = []
    pos = 0
    for f in script.funcs:
        if f.name not in lazy:
            continue
        rest.append(script.src[pos:f.start])
        gone.append((f.name, script.src[f.start:f.end]))
        pos = f.end
        # `function a(){}` всегда завершён `}`; на месте оставляем перевод
        # строки, чтобы соседние строки не склеились без ASI.
        rest.append("\n")
    rest.append(script.src[pos:])
    return "".join(rest), gone


def _group(label: str, strict: bool, gone: list[tuple[str, str]]) -> list[Chunk]:
    """Куски по содержимому: граница — после функции, чьё имя «выпало» по хэшу,
    когда набралось _TARGET. Новая функция меняет только свой кусок, а не
    сдвигает все следующие (кэш браузера переживает деплой)."""
    out: list[Chunk] = []
    names: list[str] = []
    parts: list[str] = []
    size = 0
    for nm, body in gone:
        names.append(nm)
        parts.append(body)
        size += len(body)
        if size >= _MAX or (size >= _TARGET and _chunk_key(nm) % 4 == 0):
            out.append(Chunk(label, strict, names, "\n".join(parts)))
            names, parts, size = [], [], 0
    if names:
        out.append(Chunk(label, strict, names, "\n".join(parts)))
    return out


_LOADER_SRC = r"""(function () {
  var W = window, D = document;
  var M = __LZM__;   // кусок кода → [адрес, строгий, имя[/длина]…]
  var H = __LZH__;   // кусок разметки → [адрес, id контейнера…]
  var I = __LZI__;   // id контейнера → [id внутри…]
  var T = __LZT__;   // слова селекторов, встречающиеся только в отложенной разметке
  var S = {}, HS = {}, Q = [], F = 0;
  var HTML = {}, SHELL = {}, IDS = {}, TOK = {}, left = 0;

  function trace(n) { (W.__lzTrace = W.__lzTrace || []).push(n); }
  function inject(t, url) {
    var s = D.createElement('script');
    s.text = t + '\n//# sourceURL=' + url;
    (D.head || D.documentElement).appendChild(s);
    s.remove();
  }
  // Кусок не пришёл: страница перезагружает себя полной (?full=1) — один раз
  // за сессию, чтобы не уйти в петлю, если сервер полную не отдаёт.
  function fail(e) {
    try {
      if (sessionStorage.getItem('__lz_fail')) throw e;
      sessionStorage.setItem('__lz_fail', '1');
    } catch (_) { throw e; }
    var u = location.href.replace(/([?&])full=1(&|$)/, '$1');
    location.replace(u + (u.indexOf('?') < 0 ? '?' : '&') + 'full=1');
    throw e;
  }
  function getSync(url) {
    var x = new XMLHttpRequest();
    x.open('GET', url, false);
    try { x.send(null); } catch (e) { fail(e); }
    if (x.status !== 200) fail(new Error('lazy ' + x.status + ' ' + url));
    return x.responseText;
  }

  // ── Код: синхронные заглушки ──
  function load(c, n) {
    if (S[c] === 2) return;
    trace(n);
    var t = getSync(M[c][0]);
    S[c] = 2;
    inject(t, M[c][0]);
  }
  function stub(c, n, st, l) {
    var f = st
      ? function () { 'use strict'; if (W[n] === f) { load(c, n); if (W[n] === f) fail(new Error('lazy ' + n)); } return W[n].apply(this, arguments); }
      : function () { if (W[n] === f) { load(c, n); if (W[n] === f) fail(new Error('lazy ' + n)); } return W[n].apply(this, arguments); };
    try { Object.defineProperty(f, 'name', { value: n }); Object.defineProperty(f, 'length', { value: l }); } catch (_) {}
    W[n] = f;
  }
  for (var c in M) {
    for (var i = 2; i < M[c].length; i++) { var p = M[c][i].split('/'); stub(c, p[0], M[c][1], +(p[1] || 0)); }
    Q.push(c);
  }

  // ── Разметка: экраны и окна приходят при первом обращении ──
  W.__lzHtml = function (o) { for (var k in o) HTML[k] = o[k]; };
  for (var h in H) for (var j = 1; j < H[h].length; j++) { SHELL[H[h][j]] = h; IDS[H[h][j]] = H[h][j]; left++; }
  for (var sid in I) for (var k = 0; k < I[sid].length; k++) if (!(I[sid][k] in IDS)) IDS[I[sid][k]] = sid;
  for (var t = 0; t < T.length; t++) TOK[T[t]] = 1;
  var DP = Document.prototype, EP = Element.prototype, orig = [];
  var gid = DP.getElementById, qs = EP.querySelector;
  function loadHtml(h, why) {
    if (HS[h] === 2) return;
    trace('@' + why);
    inject(getSync(H[h][0]), H[h][0]);
    HS[h] = 2;
  }
  function mat(id, why) {
    var h = SHELL[id];
    if (h === undefined) return;
    if (!(id in HTML)) loadHtml(h, why || id);
    delete SHELL[id];
    var el = gid.call(D, id);
    var html = HTML[id];
    delete HTML[id];
    if (el && el.hasAttribute('data-lz')) {
      el.innerHTML = html;
      el.removeAttribute('data-lz');
      try { el.dispatchEvent(new CustomEvent('lz:fill', { bubbles: true })); } catch (_) {}
    }
    if (!--left) unpatch();
  }
  function matAll(why) { for (var id in SHELL) mat(id, why); }
  // Запрос «по всей странице», который сам доходит до каждого контейнера
  // при его наполнении (событие lz:fill), наполнять всё не должен.
  var quiet = 0;
  W.__lzQuiet = function (f) { quiet++; try { return f(); } finally { quiet--; } };
  function touch(root, sel) {
    if (!left || quiet) return;
    if (root && root !== D && root.nodeType === 1) {
      if (root.hasAttribute('data-lz')) { mat(root.id); return; }
      if (!qs.call(root, '[data-lz]')) return;
    }
    if (sel == null) { matAll('*'); return; }
    var w = String(sel).match(/[A-Za-z_][\w-]*/g) || [];
    for (var i = 0; i < w.length; i++) {
      if (TOK[w[i]]) { matAll(String(sel)); return; }
      if (IDS[w[i]] !== undefined) mat(IDS[w[i]]);
    }
  }
  function wrap(proto, name, how) {
    var f = proto[name];
    if (!f) return;
    orig.push([proto, name, f]);
    proto[name] = function (a) { how(this, a); return f.apply(this, arguments); };
  }
  function unpatch() {
    for (var i = 0; i < orig.length; i++) orig[i][0][orig[i][1]] = orig[i][2];
    orig = [];
    if (mo) mo.disconnect();
  }
  var mo = null;
  if (left) {
    wrap(DP, 'getElementById', function (_r, id) { var s = IDS[id]; if (s !== undefined) mat(s); });
    ['querySelector', 'querySelectorAll', 'getElementsByClassName', 'getElementsByTagName'].forEach(function (n) {
      wrap(DP, n, touch); wrap(EP, n, touch);
    });
    wrap(DP, 'getElementsByName', function () { matAll('name'); });
    // Сетка безопасности: контейнер показали в обход getElementById
    // (класс show на элементе из querySelectorAll('.screen')) — наполняем
    // до отрисовки.
    try {
      mo = new MutationObserver(function (rs) {
        for (var i = 0; i < rs.length; i++) { var el = rs[i].target; if (el.hasAttribute && el.hasAttribute('data-lz')) mat(el.id, 'class'); }
      });
      mo.observe(D.body || D.documentElement, { subtree: true, attributes: true, attributeFilter: ['class', 'style'] });
    } catch (_) {}
  }

  // ── Докачка в простое: код, потом разметка ──
  var HQ = Object.keys(H);
  function idle(f) { (W.requestIdleCallback || function (g) { setTimeout(g, 60); })(f, { timeout: 2000 }); }
  function add(url, ok, bad) {
    var s = D.createElement('script');
    s.src = url; s.async = true; s.onload = ok; s.onerror = bad;
    D.head.appendChild(s);
  }
  function next() {
    if (Q.length) {
      var c = Q.shift();
      if (S[c]) { next(); return; }
      S[c] = 1;
      add(M[c][0], function () { S[c] = 2; idle(next); },
        function () { S[c] = 0; if (++F < 3) Q.push(c); idle(next); });
      return;
    }
    if (HQ.length) {
      var h = HQ.shift();
      if (HS[h]) { idle(next); return; }
      HS[h] = 1;
      add(H[h][0], function () {
        HS[h] = 2;
        var ids = H[h].slice(1);
        (function step() {
          var t0 = Date.now();
          while (ids.length && Date.now() - t0 < 8) mat(ids.shift(), 'idle');
          if (ids.length) idle(step); else idle(next);
        })();
      }, function () { HS[h] = 0; if (++F < 3) HQ.push(h); idle(next); });
    }
  }
  W.__lazyLoadAll = function () { for (var c in M) load(c, '*'); matAll('*'); };
  W.__lazyState = function () {
    var r = { total: 0, loaded: 0, markup_left: left };
    for (var c in M) { r.total++; if (S[c] === 2) r.loaded++; }
    r.total++; if (!left) r.loaded++;
    return r;
  };
  W.addEventListener('load', function () { if (!W.__lzNoIdle) setTimeout(function () { idle(next); }, __LZD__); });
})();"""

# Через сколько после load начинать докачку: главный экран в это время тянет
# свои данные из API — не отнимаем у него канал.
IDLE_DELAY_MS = 1500


def _loader_text() -> str:
    from services import miniapp_minify as mm
    return mm.strip_js(_LOADER_SRC)


def _loader(chunks: list[Chunk], markup: list["Chunk"], ids: dict, toks: list[str]) -> str:
    j = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))  # noqa: E731
    m = {str(i): [c.url, 1 if c.strict else 0] + [
        nm + ("/" + str(c.lengths[nm]) if c.lengths.get(nm) else "") for nm in c.names]
        for i, c in enumerate(chunks)}
    h = {"h" + str(i): [c.url] + c.names for i, c in enumerate(markup)}
    return (_loader_text().replace("__LZM__", j(m)).replace("__LZH__", j(h))
            .replace("__LZI__", j(ids)).replace("__LZT__", j(toks))
            .replace("__LZD__", str(IDLE_DELAY_MS)))


# ── Отложенная разметка ───────────────────────────────────────────────────

_CONTAINER = re.compile(r'<div class="(screen|mbg)(?: [^"]*)?" id="([^"]+)"')
_DIV = re.compile(r"<div\b|</div>")
_TAG = re.compile(r"<([a-zA-Z][\w-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>")
_ATTR = re.compile(r"([^\s=/>\"']+)(?:\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+))?")
_ID_ATTR = re.compile(r"""\bid=["']([^"']+)["']""")

# Вкладки и главный экран — нужны сразу.
EAGER_CONTAINERS = {"s-home", "s-bots", "s-accounts", "s-broadcasts", "s-more"}


def _mask_scripts(html: str) -> str:
    """Скрипты и стили — пробелами той же длины: «<div» в строке JS не тег."""
    return re.sub(r"<(script|style)\b.*?</\1>", lambda m: " " * len(m.group(0)), html, flags=re.S)


def _words(markup: str) -> tuple[set[str], set[str]]:
    """(слова, по которым селектор может найти что-то в разметке, id)."""
    words: set[str] = set()
    ids: set[str] = set()
    for m in _TAG.finditer(markup):
        words.add(m.group(1).lower())
        for a in _ATTR.finditer(m.group(2)):
            name = a.group(1).lower()
            words.add(name)
            val = (a.group(2) or "").strip("\"'")
            if name == "id":
                ids.add(val)
            elif name == "class":
                words.update(val.split())
            elif name == "name" or name.startswith("data-") or name in ("type", "role", "for"):
                words.update(re.findall(r"[A-Za-z_][\w-]*", val))
    return words, ids


def defer_markup(html: str, keep: set[str]) -> tuple[str, dict[str, str], dict[str, list[str]], set[str]]:
    """Вынуть содержимое экранов и окон, кроме keep и показанных.

    Возвращает (страница с пустыми контейнерами data-lz, {id: содержимое},
    {id: [id внутри]}, слова). Слова — всё, по чему селектор может найти
    элемент в вынутой разметке (теги, атрибуты, классы): запрос с таким
    словом сначала наполняет все контейнеры, иначе он не нашёл бы то, что
    нашёл бы на полной странице.
    """
    masked = _mask_scripts(html)
    taken: list[tuple[int, int, str]] = []     # (начало содержимого, конец, id)
    pos = 0
    for m in _CONTAINER.finditer(masked):
        if m.start() < pos:
            continue                            # вложен в уже вынутый
        open_end = masked.index(">", m.end()) + 1
        d, end = 0, None
        for t in _DIV.finditer(masked, m.start()):
            d += 1 if t.group(0) == "<div" else -1
            if d == 0:
                end = t.start()
                break
        if end is None:
            raise ValueError(f"незакрытый контейнер {m.group(2)}")
        pos = end
        cid = m.group(2)
        tag = masked[m.start():open_end]
        if cid in keep or " show" in tag.split('"')[1] or html[open_end:end] != masked[open_end:end]:
            continue                            # нужен сразу или со скриптом внутри
        if end - open_end < 200:
            continue                            # пустой каркас — нечего выносить
        taken.append((open_end, end, cid))
    out: list[str] = []
    inner: dict[str, str] = {}
    inner_ids: dict[str, list[str]] = {}
    words: set[str] = set()
    pos = 0
    for start, end, cid in taken:
        # Каркас остаётся: id, класс и место в документе; содержимое — потом.
        out.append(html[pos:start - 1] + ' data-lz="1">')
        part = html[start:end]
        inner[cid] = part
        w, ids = _words(part)
        words |= w
        inner_ids[cid] = sorted(ids)
        pos = end
    out.append(html[pos:])
    return "".join(out), inner, inner_ids, words


_SCRIPT_TAG = re.compile(r"<script>(.*?)</script>", re.S)
_SRC_TAG = re.compile(r'<script src="(screens/[A-Za-z0-9_\-]+\.js)"></script>')


def split_page(html: str, read_file, minify_js, boot: set[str] | None = None,
               min_inline: int = 20000, markup: bool = True) -> Split:
    """Разрезать уже очищенную страницу.

    read_file(rel) → текст файла экрана (или None); minify_js(text, label) →
    очищенный текст. boot — функции, вызываемые на старте: остаются на месте
    (их вызов через заглушку стоил бы синхронный запрос в самый нагруженный
    момент). Встроенные скрипты короче min_inline не трогаем — это ранние
    сетки безопасности и тема в <head>.
    """
    boot = boot or set()
    scripts: list[Script] = []
    slots: list[tuple[int, int, Script, bool]] = []   # (start, end, script, is_src)
    for k, m in enumerate(_SCRIPT_TAG.finditer(html)):
        body = m.group(1)
        if len(body) < min_inline:
            continue
        s = Script(f"index#{k}", body, _strict(body))
        scripts.append(s)
        slots.append((m.start(), m.end(), s, False))
    for m in _SRC_TAG.finditer(html):
        rel = m.group(1)
        raw = read_file(rel)
        if raw is None:
            continue
        body = minify_js(raw, rel)
        s = Script(rel, body, _strict(body))
        scripts.append(s)
        slots.append((m.start(), m.end(), s, True))
    if not scripts:
        raise ValueError("нечего резать")
    for s in scripts:
        s.toks = tokenize(s.src)
        s.funcs = top_level_functions(s.toks)
        _forbid_dynamic_globals(s)

    # Тексты вне разрезаемых скриптов: разметка с обработчиками и мелкие
    # встроенные скрипты в <head>.
    extra = [_SCRIPT_TAG.sub(lambda m_: m_.group(0) if len(m_.group(1)) < min_inline else "",
                             _SRC_TAG.sub("", html))]
    lazy, why = _safe_refs(scripts, extra)
    for nm in boot & lazy:
        why[nm] = "нужна на старте"
    lazy -= boot

    # Два потока по строгому режиму: в одном куске режим один на всех, а
    # мелкие экраны склеиваются с соседями — 40 запросов по 3 КБ жмутся хуже
    # и стоят дороже, чем десяток по 30 КБ.
    lengths = {f.name: f.length for s in scripts for f in s.funcs}
    chunks: list[Chunk] = []
    rests: dict[int, str] = {}
    streams: dict[bool, list[tuple[str, str]]] = {True: [], False: []}
    for s in scripts:
        rest, gone = _cut(s, lazy)
        rests[id(s)] = rest
        streams[s.strict] += gone
    for strict in (True, False):
        chunks += _group("strict" if strict else "sloppy", strict, streams[strict])
    for c in chunks:
        if c.strict:
            c.body = '"use strict";\n' + c.body
        c.hash = hashlib.sha256(c.body.encode()).hexdigest()[:16]
        c.lengths = {nm: lengths[nm] for nm in c.names}

    # Разметка экранов и окон: на месте остаются пустые каркасы.
    keep = EAGER_CONTAINERS | {b[1:] for b in boot if b.startswith("#")}
    if markup:
        html, inner, inner_ids, words = defer_markup(html, keep)
        slots = _reslot(html, slots)
    else:
        inner, inner_ids, words = {}, {}, set()
    mchunks = _group_markup(inner)

    out: list[str] = []
    pos = 0
    loader_done = False
    for start, end, s, _is_src in sorted(slots, key=lambda t: t[0]):
        out.append(html[pos:start])
        if not loader_done:
            out.append("<script>" + _loader(chunks, mchunks, inner_ids, sorted(words)) + "</script>")
            loader_done = True
        # Экран с остатком — встроенным скриптом на том же месте: отдельный
        # <script> сохраняет и порядок, и изоляцию ошибок, и строгий режим.
        out.append("<script>" + rests[id(s)].replace("</script", "<\\/script") + "</script>")
        pos = end
    out.append(html[pos:])
    page = "".join(out)
    sp = Split(page, chunks, len(lazy), why, len(boot))
    sp.markup = mchunks
    sp.deferred = len(inner)
    return sp


def _reslot(html: str, slots):
    """Позиции скриптов после выноса разметки (она стоит раньше них)."""
    tags = [m for m in _SCRIPT_TAG.finditer(html)] + [m for m in _SRC_TAG.finditer(html)]
    by_body = {}
    for m in tags:
        by_body.setdefault(m.group(0), []).append((m.start(), m.end()))
    out = []
    for start, end, s, is_src in slots:
        key = (f'<script src="{s.label}"></script>' if is_src else "<script>" + s.src + "</script>")
        out.append(by_body[key].pop(0) + (s, is_src))
    return out


_MARKUP_TARGET = 48 * 1024


def _group_markup(inner: dict[str, str]) -> list[Chunk]:
    out: list[Chunk] = []
    cur: dict[str, str] = {}
    size = 0

    def flush():
        body = "__lzHtml(" + json.dumps(cur, ensure_ascii=False, separators=(",", ":")) \
            .replace("</", "<\\/") + ");"
        c = Chunk("markup", False, list(cur), body)
        c.hash = hashlib.sha256(body.encode()).hexdigest()[:16]
        out.append(c)

    for cid, part in inner.items():
        cur[cid] = part
        size += len(part)
        if size >= _MARKUP_TARGET and _chunk_key(cid) % 3 == 0:
            flush()
            cur, size = {}, 0
    if cur:
        flush()
    return out


# ── Список функций старта ─────────────────────────────────────────────────

BOOT_FILE = "lazy_boot.txt"


def read_boot(static_dir: str) -> set[str]:
    try:
        with open(os.path.join(static_dir, BOOT_FILE), encoding="utf-8") as f:
            # «# текст» — комментарий; «#id» — контейнер разметки, нужный на старте.
            return {ln.strip() for ln in f
                    if ln.strip() and not (ln.startswith("# ") or ln.strip() == "#")}
    except OSError:
        return set()


class LazyCache:
    """Нарезка для сервера: одна на версию страницы, в фоне, с кусками в памяти.

    Нарезка стоит ~5 с CPU, поэтому идёт в потоке и ОДИН раз на версию; пока
    она не готова, отдаётся полная страница — как раньше. Куски прошлых версий
    держатся ещё два деплоя: открытая до деплоя страница догружает свои куски,
    а не получает 404 посреди работы.
    """

    KEEP_PAGES = 3

    def __init__(self) -> None:
        self.pages: dict[str, Split | None] = {}     # версия → нарезка (None — сбой)
        self.chunks: dict[str, str] = {}             # хэш → текст
        self.owner: dict[str, str] = {}              # хэш → версия
        self.gz: dict[str, bytes] = {}
        self._pending: set[str] = set()
        self._tasks: set = set()

    def page(self, version: str, html: str, static_dir: str, minify_js) -> Split | None:
        if version in self.pages:
            return self.pages[version]
        if version in self._pending:
            return None
        try:
            import asyncio
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        self._pending.add(version)

        def rf(rel: str) -> str | None:
            root = os.path.realpath(static_dir)
            full = os.path.realpath(os.path.join(root, rel))
            if not full.startswith(root + os.sep):
                return None
            try:
                with open(full, encoding="utf-8") as fh:
                    return fh.read()
            except OSError:
                return None

        def work() -> Split:
            return split_page(html, rf, minify_js, boot=read_boot(static_dir))

        async def run() -> None:
            import asyncio
            try:
                sp = await asyncio.to_thread(work)
                self._store(version, sp)
                log.info("miniapp_lazy: %d функций в %d кусках, страница %d КБ",
                         sp.lazy_names, len(sp.chunks), len(sp.html) // 1024)
            except Exception:
                log.warning("miniapp_lazy: нарезка не удалась — отдаём полную страницу",
                            exc_info=True)
                self.pages[version] = None
            finally:
                self._pending.discard(version)

        task = loop.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return None

    def _store(self, version: str, sp: Split) -> None:
        self.pages[version] = sp
        for c in sp.chunks + sp.markup:
            self.chunks[c.hash] = c.body
            self.owner[c.hash] = version
        while len(self.pages) > self.KEEP_PAGES:
            old = next(iter(self.pages))
            del self.pages[old]
            for h in [h for h, v in self.owner.items() if v == old]:
                if not any(c.hash == h for p in self.pages.values() if p for c in p.chunks + p.markup):
                    self.chunks.pop(h, None)
                    self.gz.pop(h, None)
                    self.owner.pop(h, None)

    def chunk_gz(self, h: str) -> bytes | None:
        body = self.chunks.get(h)
        if body is None:
            return None
        if h not in self.gz:
            import gzip
            self.gz[h] = gzip.compress(body.encode("utf-8"), 6)
        return self.gz[h]


def enabled() -> bool:
    return os.getenv("MINIAPP_LAZY", "1").strip().lower() not in ("0", "false", "no", "off")


# ── Сборка в каталог (замеры и обход в Chromium) ──────────────────────────

def build_dir(static_dir: str, out_dir: str, lazy: bool = True, boot: set[str] | None = None) -> Split | None:
    """Записать в out_dir страницу так, как её отдаёт сервер: очищенную и (если
    lazy) разрезанную, с куском в lazy/<хэш>.js. Остальные файлы — копией
    (экраны — очищенными). Для deploy/scripts/lazy_check.mjs и cold_start.mjs."""
    import shutil

    from services import miniapp_minify as mm
    os.makedirs(out_dir, exist_ok=True)
    for name in os.listdir(static_dir):
        src = os.path.join(static_dir, name)
        if os.path.isfile(src) and name != "index.html":
            shutil.copy(src, os.path.join(out_dir, name))
    os.makedirs(os.path.join(out_dir, "screens"), exist_ok=True)
    for name in os.listdir(os.path.join(static_dir, "screens")):
        with open(os.path.join(static_dir, "screens", name), encoding="utf-8") as f:
            body = mm.strip_js(f.read())
        with open(os.path.join(out_dir, "screens", name), "w", encoding="utf-8") as f:
            f.write(body)
    with open(os.path.join(static_dir, "index.html"), encoding="utf-8") as f:
        html = mm.minify_html(f.read())
    sp = None
    if lazy:
        def rf(rel):
            try:
                with open(os.path.join(static_dir, rel), encoding="utf-8") as fh:
                    return fh.read()
            except OSError:
                return None
        sp = split_page(html, rf, lambda t, _l: mm.strip_js(t),
                        boot=read_boot(static_dir) if boot is None else boot)
        html = sp.html
        os.makedirs(os.path.join(out_dir, "lazy"), exist_ok=True)
        for c in sp.chunks + sp.markup:
            with open(os.path.join(out_dir, c.url), "w", encoding="utf-8") as f:
                f.write(c.body)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    return sp


if __name__ == "__main__":  # python -m services.miniapp_lazy <out> [--full] [--no-boot]
    import sys
    _root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mini_app")
    _sp = build_dir(_root, sys.argv[1], lazy="--full" not in sys.argv,
                    boot=set() if "--no-boot" in sys.argv else None)
    if _sp:
        print(json.dumps({"chunks": len(_sp.chunks), "lazy": _sp.lazy_names,
                          "markup_chunks": len(_sp.markup), "deferred": _sp.deferred,
                          "kept": len(_sp.kept)}, ensure_ascii=False))
