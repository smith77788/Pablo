"""Гейт: фронт не читает поля ЭЛЕМЕНТОВ списка, которых бэкенд не кладёт.

`test_api_contract_keys` стережёт верхний уровень ответа (`d.foo`). Уровнем
ниже защиты не было — и там нашлось две поломки подряд:

* «Пул прокси» читал `p.account_count` и `p.latency_ms`: ключ `proxies` в ответе
  есть, а этих полей у строк нет. Счётчик аккаунтов не показывался никогда,
  «Средняя задержка» всегда была «—», график задержек всегда рисовал «нет
  данных» — при живой колонке `latency_avg_ms`.
* «Разведка рекламы» читала `c.channel_username` / `c.placements_count` /
  `c.avg_er` и `a.advertiser_name` / `a.total_placements` / `a.avg_spend`, а
  бэкенд клал `username` / `subscribers` / `er_rate` и `username` / `placements`
  / `last_seen`. Из пяти полей совпадало ОДНО. Экран вечно показывал
  «@undefined · 0 размещений · ER 0.0%» — и выглядел как «данных пока нет».

Оба раза поломка молчала: пустое значение неотличимо от отсутствия данных.

**Точность важнее полноты.** Проверяем только строго сопоставимые пары: ответ
собран как литеральный список литеральных словарей (`"key": [ {...} for r in
rows ]`), а фронт делает `d.key` и сразу `.map(v => ...)`. Всё динамическое
(`dict(row)`, `**stats`, сборка в сервисе) пропускаем — иначе гейт начнёт врать,
а «детектор, дающий десятки находок в зрелом коде, почти всегда сломан»
(CLAUDE.md).
"""
from __future__ import annotations

import ast
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_API_SRC = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")


def _frontend_js() -> str:
    html = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    js = "\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                              html, re.DOTALL))
    for f in sorted((_ROOT / "mini_app" / "screens").glob("*.js")):
        js += "\n" + f.read_text(encoding="utf-8")
    return js


_JS = _frontend_js()

# Поля, которые фронт может читать законно, не получив их от этого эндпоинта.
_ALLOWED: set[tuple[str, str]] = set()


def _literal_item_keys(node: ast.AST) -> set[str] | None:
    """Ключи элемента, если значение — список литеральных словарей.

    Понимает и `[{...} for r in rows]`, и `[{...}, {...}]`. Возвращает None,
    когда форма динамическая — такие пары гейт не разбирает.
    """
    if isinstance(node, ast.ListComp):
        elt = node.elt
    elif isinstance(node, ast.List) and node.elts:
        elt = node.elts[0]
    else:
        return None
    if not isinstance(elt, ast.Dict):
        return None
    keys = set()
    for k in elt.keys:
        if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
            return None  # **spread или вычисляемый ключ — форма динамическая
        keys.add(k.value)
    return keys or None


def _response_item_shapes() -> dict[str, set[str]]:
    """`ключ ответа` → множество полей элемента, для литеральных форм.

    Ключи-омонимы отбрасываются. `accounts`, `bots`, `users` и т.п. возвращают
    ДЕСЯТКИ эндпоинтов, и у части из них форма динамическая (`dict(row)`).
    Сопоставлять `.map()` фронта с литеральной формой ОДНОГО из них — прямой
    путь к ложным обвинениям: первая же версия гейта дала 40 «находок», все
    из-за этой коллизии. Поэтому ключ участвует, только если КАЖДЫЙ его
    производитель литеральный: тогда множество полей полное и сравнимое.
    """
    literal: dict[str, set[str]] = {}
    dynamic: set[str] = set()
    tree = ast.parse(_API_SRC)
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "_json_resp"):
            continue
        if not n.args or not isinstance(n.args[0], ast.Dict):
            continue
        for k, v in zip(n.args[0].keys, n.args[0].values):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                continue
            keys = _literal_item_keys(v)
            if keys:
                literal.setdefault(k.value, set()).update(keys)
            else:
                dynamic.add(k.value)
    return {k: v for k, v in literal.items() if k not in dynamic}


def _callback_body(src: str, arrow_end: int) -> str:
    """Тело стрелочной функции внутри `.map(` — по БАЛАНСУ скобок, а не окном
    фиксированной длины. Окно в 1200 символов вылезало за конец колбэка и
    приписывало списку поля из соседнего кода (так `reports[].report_id`
    попал в находки, хотя читается в ответе совсем другого эндпоинта)."""
    depth = 1          # мы уже внутри скобки, открытой у `.map(`
    i = arrow_end
    n = len(src)
    while i < n and depth > 0:
        c = src[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        i += 1
    return src[arrow_end:i]


def _fields_read_on(resp_key: str) -> set[tuple[str, str]]:
    """(поле, фрагмент) — что фронт читает у элементов `d.<resp_key>`.

    Ловим обе распространённые записи: `d.key.map(v => ...)` и
    `const xs = d.key||[]; ... xs.map(v => ...)`.
    """
    found: set[tuple[str, str]] = set()
    for m in re.finditer(
            rf"(?:d|r|res|resp)\.{re.escape(resp_key)}\s*(?:\|\|\s*\[\])?\s*"
            rf"\.map\(\s*(\w+)\s*=>", _JS):
        var = m.group(1)
        body = _callback_body(_JS, m.end())
        for f in re.finditer(rf"\b{re.escape(var)}\.(\w+)\b", body):
            found.add((f.group(1), body[:160]))
    # Форма «через переменную»: const advs = d.key||[]; advs.map(a=>...)
    for m in re.finditer(
            rf"(?:const|let|var)\s+(\w+)\s*=\s*(?:d|r|res|resp)\.{re.escape(resp_key)}\b",
            _JS):
        holder = m.group(1)
        tail = _JS[m.end():m.end() + 2500]
        mm = re.search(rf"\b{re.escape(holder)}\.map\(\s*(\w+)\s*=>", tail)
        if not mm:
            continue
        var = mm.group(1)
        body = _callback_body(tail, mm.end())
        for f in re.finditer(rf"\b{re.escape(var)}\.(\w+)\b", body):
            found.add((f.group(1), body[:160]))
    return found


def _mismatches() -> list[str]:
    bad: list[str] = []
    for resp_key, item_keys in _response_item_shapes().items():
        for field, frag in _fields_read_on(resp_key):
            if field in item_keys or (resp_key, field) in _ALLOWED:
                continue
            bad.append(
                f"{resp_key}[].{field} — бэкенд кладёт {sorted(item_keys)}; "
                f"фрагмент: {' '.join(frag.split())[:110]}"
            )
    return bad


# ── Сам гейт ───────────────────────────────────────────────────────────────

def test_frontend_reads_only_item_fields_backend_puts_there():
    bad = _mismatches()
    assert not bad, (
        "фронт читает поля элементов списка, которых в ответе нет — строка "
        "молча отрисуется пустой:\n  " + "\n  ".join(sorted(bad))
    )


def test_the_gate_actually_compares_something():
    """Если разбор перестанет находить пары, гейт станет зелёным навсегда.

    Порог низкий осознанно: однозначных (без омонимов) списочных ключей в файле
    всего 11 — это цена точности, а не слабость разбора."""
    shapes = _response_item_shapes()
    assert len(shapes) >= 8, f"разобрано лишь {len(shapes)} списочных ответов"
    compared = sum(1 for k in shapes if _fields_read_on(k))
    assert compared >= 5, f"сопоставлено лишь {compared} пар — разбор JS сломался"


# ── Проверка самого измерителя ─────────────────────────────────────────────

def test_gate_catches_a_synthetic_mismatch():
    """Зелёный гейт, который не умеет краснеть, не защищает ничего."""
    api = '''
def h(request):
    return _json_resp({"widgets": [{"name": r["n"], "count": r["c"]} for r in rows]})
'''
    tree = ast.parse(api)
    keys = None
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "_json_resp"):
            keys = _literal_item_keys(n.args[0].values[0])
    assert keys == {"name", "count"}
    js = "txt('x', d.widgets.map(w=>`${w.name}: ${w.total}`).join(''));"
    m = re.search(r"d\.widgets\s*\.map\(\s*(\w+)\s*=>", js)
    body = _callback_body(js, m.end())
    read = {f.group(1) for f in re.finditer(r"\bw\.(\w+)\b", body)}
    assert read == {"name", "total"}
    assert read - keys == {"total"}, "несовпадение поля должно быть видно"


def test_callback_body_stops_at_the_end_of_the_map():
    """Именно на этом ломалось окно фиксированной длины."""
    js = "xs.map(v=>`${v.a}`).join('') ; later.forEach(v=>v.b)"
    m = re.search(r"xs\.map\(\s*(\w+)\s*=>", js)
    body = _callback_body(js, m.end())
    assert "v.a" in body and "v.b" not in body


# ── Прежняя поломка закрыта именно так, как ожидается ──────────────────────

def _ad_intel_render() -> str:
    """Тело loadAdIntel — по балансу скобок от его открывающей, а не окном."""
    i = _JS.find("/api/miniapp/ad_intel")
    assert i != -1, "экран «Разведка рекламы» не найден"
    j = _JS.rfind("async function", 0, i)
    k = _JS.find("{", _JS.find("(", j))
    return _callback_body(_JS, k + 1)


def _ad_intel_shapes() -> dict[str, set[str]]:
    """Форма ответа именно `ad_intel_overview`.

    Через общий разбор её не достать: `top_channels` — омоним (тот же ключ
    отдаёт другой эндпоинт, и динамически), поэтому из сопоставимых он
    исключён. Это цена точности общего гейта, а не повод оставить экран без
    проверки — берём хендлер поимённо.
    """
    tree = ast.parse(_API_SRC)
    for fn in ast.walk(tree):
        if not (isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef))
                and fn.name == "ad_intel_overview"):
            continue
        out: dict[str, set[str]] = {}
        for n in ast.walk(fn):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "_json_resp"):
                continue
            for k, v in zip(n.args[0].keys, n.args[0].values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys = _literal_item_keys(v)
                    if keys:
                        out[k.value] = keys
        return out
    raise AssertionError("ad_intel_overview не найден")


def test_ad_intel_rows_use_the_real_contract():
    """Из пяти полей совпадало одно; проверяем поимённо, чтобы возврат к
    выдуманным именам не прошёл незамеченным."""
    shapes = _ad_intel_shapes()
    assert {"username", "subscribers", "er_rate", "quality_score"} <= shapes["top_channels"]
    assert {"username", "placements", "last_seen"} <= shapes["top_advertisers"]
    render = _ad_intel_render()
    for dead in ("channel_username", "placements_count", "avg_er",
                 "advertiser_name", "total_placements", "avg_spend"):
        assert dead not in render, (
            f"экран «Разведка рекламы» снова читает несуществующее поле {dead}"
        )
