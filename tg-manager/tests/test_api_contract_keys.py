"""Гейт: фронт не читает ключи ответа, которых бэкенд не отдаёт.

Класс багов, пойманный сквозным аудитом: мини-апп рисует KPI/списки из `d.foo`,
а хендлер такого ключа не возвращает → плитка вечно показывает 0 / виджет вечно
пуст, причём МОЛЧА (нет ни ошибки, ни пустого состояния). Так были найдены:
  * вестигиальный A/B-виджет рассылок (`ab_variant`/`ab_wins_*` — колонок нет вообще);
  * Разведка рекламы: `total_advertisers`/`total_placements` не отдавались → две
    KPI-плитки из трёх всегда показывали 0.

Проверяем ТОЛЬКО строго сопоставимые пары (совпал HTTP-метод и путь, хендлер
возвращает литеральный dict), чтобы гейт был детерминированным и без ложных
срабатываний. Динамические ответы (dict(row)/**kwargs) пропускаются.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Осознанные исключения: ключ читается в заведомо недостижимой защитной ветке.
ALLOWED: set[tuple[str, str, str]] = {
    # cf_pool_deploy всегда возвращает started=True; ветка с r.assigned — fallback
    ("POST", "/api/miniapp/cf/pool/deploy", "assigned"),
}

_IGNORED_ATTRS = {
    "then", "catch", "map", "length", "forEach", "filter", "json",
    "ok", "message", "status",
}


def _frontend_js() -> str:
    js = "\n".join(
        re.findall(r"<script>(.*?)</script>",
                   (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8"),
                   re.DOTALL)
    )
    for f in sorted((ROOT / "mini_app" / "screens").glob("*.js")):
        js += "\n" + f.read_text(encoding="utf-8")
    return js


def _routes(api_src: str) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for m in re.finditer(
        r'app\.router\.add_(get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']\s*,\s*(\w+)',
        api_src,
    ):
        path = re.sub(r"\{[^}]+\}", "{}", m.group(2)).rstrip("/")
        out[(m.group(1).upper(), path)] = m.group(3)
    return out


def _handler_keys(api_src: str, fn: str):
    """(отдаваемые ключи, динамический_ли_ответ) или None если хендлер не найден."""
    m = re.search(
        r"    async def " + fn + r"\s*\(request.*?(?=\n    async def |\n    # ──|\n    app\.router)",
        api_src, re.DOTALL,
    )
    if not m:
        return None
    body = m.group(0)
    keys: set[str] = set()
    # Разбор по балансу скобок: ответы содержат вложенные dict/list, поэтому
    # нежадный `\{(.*?)\}` обрывался на первом '}' и терял часть ключей.
    for mm in re.finditer(r"_json_resp\(\s*\{", body):
        start = body.index("{", mm.start())
        depth, i = 0, start
        while i < len(body):
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        blk = body[start : i + 1]
        # только ключи ВЕРХНЕГО уровня: считаем глубину внутри блока
        depth = 0
        for km in re.finditer(r"""[{}]|["']([a-z_][a-z0-9_]*)["']\s*:""", blk):
            tok = km.group(0)
            if tok == "{":
                depth += 1
            elif tok == "}":
                depth -= 1
            elif depth == 1 and km.group(1):
                keys.add(km.group(1))
    dynamic = bool(re.search(r"dict\(r\)|dict\(row\)|\*\*|for r in rows", body))
    return keys, dynamic


def test_frontend_reads_only_keys_backend_returns():
    js = _frontend_js()
    api_src = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    routes = _routes(api_src)

    problems: list[str] = []
    checked = 0
    call_re = re.compile(
        r"""(?:const|let|var)\s+(\w+)\s*=\s*await\s+api\(\s*[`'"]([^`'"]+)[`'"]\s*"""
        r"""(,\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})?\s*\)"""
    )
    for m in call_re.finditer(js):
        var, route, opts = m.group(1), m.group(2).split("?")[0], m.group(3) or ""
        method = "POST" if re.search(r"""method\s*:\s*['"]POST""", opts) else (
            "DELETE" if "DELETE" in opts else "GET")
        path = re.sub(r"\$\{[^}]+\}", "{}", route).rstrip("/")

        rest = js[m.end():]
        # окно = до следующего вызова api(), конца функции или 1200 символов
        bounds = [x for x in (rest.find("await api("), rest.find("\n}"), 1200) if x > 0]
        seg = rest[: min(bounds)] if bounds else rest[:1200]
        read = set(re.findall(r"\b" + re.escape(var) + r"\.([a-z_][a-z0-9_]*)\b", seg))
        read -= _IGNORED_ATTRS
        if not read:
            continue

        fn = routes.get((method, path))
        if not fn:
            continue
        hk = _handler_keys(api_src, fn)
        if not hk:
            continue
        returned, dynamic = hk
        if dynamic or len(returned) < 2:
            continue  # ответ формируется динамически — статически не судим

        checked += 1
        for key in sorted(read - returned):
            if (method, path, key) in ALLOWED:
                continue
            problems.append(
                f"[{method}] {path} (fn={fn}): фронт читает '{key}', "
                f"а хендлер отдаёт {sorted(returned)}"
            )

    assert checked >= 50, f"гейт деградировал: строго проверено всего {checked} пар"
    assert not problems, (
        "фронт читает ключи, которых нет в ответе (молчаливо нулевые плитки/пустые "
        "виджеты):\n  " + "\n  ".join(problems)
    )
