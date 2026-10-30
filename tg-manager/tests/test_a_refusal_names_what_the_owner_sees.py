"""Отказ не вправе называть владельцу имя поля из нашего же JSON.

Владелец читает только по-русски (CLAUDE.md, первая строка), и «name
обязателен», «topic и core_message обязательны», «plan_type должен быть
gentle/standard/aggressive» — это не причина отказа, а строчка из кода. На
экране у этих полей другие подписи: «Название», «Тема», «Главное сообщение»,
«Режим прогрева». То есть сообщение называло сущность, которой владелец нигде
не видел, и сделать по нему было нечего.

Класс уже закрывался однажды в одном месте — в сохранении профиля гео-сети, где
отказ Telegram подписывался сырыми ключами (`name`, `short_description`), и
тогда появился словарь подписей. Здесь он посчитан по всему слою мини-аппа: 50
вхождений в 21 файле.

КАК ПРАВИЛО ОТЛИЧАЕТ «ИМЯ ПОЛЯ» ОТ ОБЫЧНОГО СЛОВА. Не по латинице: `.session`,
`tdata`, `@username`, `ISO`, `@BotFather`, `Account ID` — это слова, которые
владелец видит в Telegram и в приложении, и переводить их незачем. Находка —
это слово, которое ТОТ ЖЕ обработчик читает как поле запроса
(`data.get("topic")`, `body["steps"]`). Тогда слово в отказе и ключ в JSON —
одно и то же имя, и значит имя пришло из кода, а не из языка.

ИСКЛЮЧЕНИЯ. Несколько ключей совпадают со словами, которые владелец и правда
читает на экране и в Telegram: `username`, `phone`, `password`, `code`,
`token`, `keyword`, `start_param`, `title`, `about`. Для них имя поля и слово
языка — одно и то же, и запрещать его значило бы запрещать нормальную речь.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Ключи, которые владелец и так читает: это слова, а не имена из кода.
_ALSO_WORDS = frozenset({
    "username", "phone", "password", "code", "token", "keyword",
    "start_param", "title", "about",
})

_WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9]{2,}")


def _request_fields(fn: ast.AST) -> set[str]:
    """Имена, которые этот обработчик читает как поля запроса."""
    out: set[str] = set()
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in ("get", "getone", "getall") and n.args
                and isinstance(n.args[0], ast.Constant)
                and isinstance(n.args[0].value, str)):
            out.add(n.args[0].value)
        if (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
                and isinstance(n.slice.value, str)):
            out.add(n.slice.value)
    return out


def _findings(files=None) -> list[str]:
    bad: list[str] = []
    paths = files if files is not None else sorted(
        (ROOT / "services").glob("mini_app*.py"))
    for path in paths:
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            # `setup_routes` объемлет все обработчики файла, и его множество
            # полей — объединение чужих: судить по нему значит мешать поля
            # одной ручки с отказами другой.
            if (not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                    or fn.name == "setup_routes"):
                continue
            fields = _request_fields(fn)
            if not fields:
                continue
            for n in ast.walk(fn):
                if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                        and n.func.id == "_err" and n.args):
                    continue
                arg = n.args[0]
                if not (isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)):
                    continue
                leaked = sorted(w for w in _WORD.findall(arg.value)
                                if w in fields and w not in _ALSO_WORDS)
                if leaked:
                    bad.append(f"{path.name}:{n.lineno} {fn.name}: "
                               f"{arg.value[:70]!r} — {', '.join(leaked)}")
    return bad


# ── Проба: правило красное на том, что уже было ────────────────────────────

def test_the_rule_is_red_on_a_field_name(tmp_path):
    """Подложная ручка, отказывающая именем своего поля, обязана быть находкой."""
    src = '''
async def narrative_create(request):
    data = await request.json()
    topic = str(data.get("topic", "")).strip()
    if not topic:
        return _err("topic обязателен", 400)
'''
    f = tmp_path / "mini_app_probe.py"
    f.write_text(src, encoding="utf-8")
    bad = _findings([f])
    assert len(bad) == 1 and "topic" in bad[0], bad


def test_a_word_the_owner_reads_is_not_a_finding(tmp_path):
    """`@username` и `токен` — речь, а не имя из кода."""
    src = '''
async def bot_add(request):
    body = await request.json()
    token = str(body.get("token", "")).strip()
    uname = str(body.get("username", "")).strip()
    if not token:
        return _err("Укажите токен бота", 400)
    if not uname:
        return _err("Укажите @username бота", 400)
'''
    f = tmp_path / "mini_app_probe2.py"
    f.write_text(src, encoding="utf-8")
    assert _findings([f]) == []


def test_a_word_that_is_not_a_field_is_not_a_finding(tmp_path):
    """`.session` и `tdata` в отказе — это про Telegram, а не про наш JSON."""
    src = '''
async def session_import(request):
    body = await request.json()
    kind = str(body.get("kind", "")).strip()
    if kind != "session":
        return _err("Ожидается ZIP-архив папки tdata", 400)
'''
    f = tmp_path / "mini_app_probe3.py"
    f.write_text(src, encoding="utf-8")
    assert _findings([f]) == []


def test_the_rule_looks_at_the_whole_layer():
    """Пустой список находок при пустом охвате ничего не значит."""
    files = sorted((ROOT / "services").glob("mini_app*.py"))
    assert len(files) >= 25, f"файлов слоя найдено {len(files)}"
    judged = 0
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and fn.name != "setup_routes" and _request_fields(fn)):
                judged += 1
    assert judged >= 200, (
        f"обработчиков с полями запроса осуждено {judged} — правило ослепло")


# ── Собственно правило ─────────────────────────────────────────────────────

def test_no_refusal_names_a_request_field():
    bad = _findings()
    assert not bad, (
        "отказ называет владельцу имя поля из нашего JSON: на экране у этого "
        "поля другая подпись, и сделать по такому сообщению нечего. Назовите "
        "то, что видит владелец:\n  " + "\n  ".join(bad))


# ── Числа из тела запроса идут через int_field ──────────────────────────────
#
# `int(data.get("delay_minutes", 30))` на пустом поле кидает ValueError НАРУЖУ
# обработчика: ответом уходит не русская причина, а 500 «Внутренняя ошибка
# сервиса». `int_field` говорит, какого поля не хватает, по-русски.

_USER_DATA = re.compile(r"\b(body|payload|data|match_info|request\.query"
                        r"|request\.rel_url\.query)\b")


def _unguarded_conversions(files=None) -> list[str]:
    bad: list[str] = []
    for path in (files if files is not None
                 else sorted((ROOT / "services").glob("mini_app*.py"))):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        writes = {m.group(1) for m in re.finditer(
            r"add_(?:post|put|delete|patch)\([^,]+,\s*([A-Za-z_]\w*)", src)}
        for fn in ast.walk(tree):
            if not (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and fn.name in writes):
                continue
            guarded: set[int] = set()
            for t in ast.walk(fn):
                if not isinstance(t, ast.Try):
                    continue
                names: set[str] = set()
                for h in t.handlers:
                    if h.type is None:
                        names.add("BaseException")
                        continue
                    for x in ([h.type] if not isinstance(h.type, ast.Tuple)
                              else h.type.elts):
                        if isinstance(x, ast.Name):
                            names.add(x.id)
                if names & {"ValueError", "TypeError", "Exception", "BaseException"}:
                    for stmt in t.body:
                        for n in ast.walk(stmt):
                            guarded.add(id(n))
            for n in ast.walk(fn):
                if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                        and n.func.id in ("int", "float") and n.args):
                    continue
                expr = ast.unparse(n.args[0])
                if not _USER_DATA.search(expr) or id(n) in guarded:
                    continue
                # `or 0` и `.isdigit()` — защита на месте.
                if re.search(r"\bor\s+['\"]?\d", expr):
                    continue
                bad.append(f"{path.name}:{n.lineno} {fn.name}: {ast.unparse(n)[:60]}")
    return bad


def test_the_conversion_rule_is_red_on_a_bare_int(tmp_path):
    """Проба: голое int() по телу запроса в изменяющей ручке — находка."""
    src = '''
app.router.add_post("/api/miniapp/probe", probe_create)


async def probe_create(request):
    data = await request.json()
    delay = int(data.get("delay_minutes", 30))
    return _json_resp({"ok": True, "delay": delay})
'''
    f = tmp_path / "mini_app_probe4.py"
    f.write_text(src, encoding="utf-8")
    bad = _unguarded_conversions([f])
    assert any("probe_create" in b for b in bad), bad


def test_int_field_closes_the_conversion(tmp_path):
    """А через int_field то же место находкой быть перестаёт."""
    src = '''
app.router.add_post("/api/miniapp/probe", probe_create)


async def probe_create(request):
    data = await request.json()
    try:
        delay = int_field(data.get("delay_minutes"), "Задержка", default=30)
    except ValueError as exc:
        return _err(str(exc), 400)
    return _json_resp({"ok": True, "delay": delay})
'''
    f = tmp_path / "mini_app_probe5.py"
    f.write_text(src, encoding="utf-8")
    assert _unguarded_conversions([f]) == []


def test_numbers_from_the_body_go_through_int_field():
    bad = [b for b in _unguarded_conversions() if "isdigit" not in b]
    # `crosspost_run` проверяет значение `.isdigit()` до преобразования —
    # исключение оттуда не вылетит; правило этой защиты не видит, поэтому
    # место названо здесь, а не молча пропущено.
    bad = [b for b in bad if "crosspost_run" not in b]
    assert not bad, (
        "число из тела запроса преобразуется голым int(): на пустом или "
        "негодном значении ValueError уйдёт наружу, и владелец увидит 500 "
        "вместо русской причины. Нужен int_field(value, «Подпись поля»):\n  "
        + "\n  ".join(bad))
