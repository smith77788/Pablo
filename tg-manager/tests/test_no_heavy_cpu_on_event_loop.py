"""Храповик: тяжёлый разбор и сборка файлов не идут на цикле событий.

ЗАЧЕМ. Бот, HTTP-API мини-аппа, исполнитель операций и ~50 фоновых циклов —
ОДИН процесс и один цикл событий. Синхронный CPU-код внутри `async def` не
отдаёт управление, пока не закончит, и на это время замирает всё: апдейты
бота, ответы мини-аппа, отправки операций, хартбиты аренды исполнителя — у
всех клиентов сразу.

Замер 03.11.2026 (Python 3.12, openpyxl): список для инвайта в .xlsx весом
3.3 МБ (200 тысяч строк) разбирается 7.5 с, а предел загрузки — 25 МБ, то есть
до минуты полного простоя продукта от одного файла. Так работали четыре места:

* загрузка списка инвайта файлом в мини-аппе (`invite_parse_file`);
* то же в боте (`mass_inviter._refs_from_document`);
* файл для ИИ-ассистента (PDF/DOCX/XLSX, `ai_assistant._read_attached_document`);
* Excel-выгрузка аудитории бота (`audience.cb_export_xlsx`).

Теперь эта работа идёт в `asyncio.to_thread`. Проверка — статическая: внутри
`async def` (но не во вложенной синхронной функции — её как раз и отдают в
поток) нельзя напрямую звать функции из списка ниже. Нужно новое тяжёлое
место — оберни в `await asyncio.to_thread(fn, ...)`, а не добавляй исключение.
"""
from __future__ import annotations

import ast
import asyncio
import io
import os
import sys
import threading
from types import SimpleNamespace


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Имена, вызов которых — заведомо тяжёлый синхронный CPU: разбор или сборка
# файла целиком. Ловятся и как `fn(...)`, и как `mod.fn(...)`.
HEAVY = {
    "Workbook",              # openpyxl: сборка книги
    "load_workbook",         # openpyxl: разбор книги
    "PdfReader",             # pypdf
    "parse_invite_file",     # services.invite_list_parser
    "extract_text_from_file",
    "_extract_document_text",  # bot.handlers.ai_assistant
    "_build_audience_xlsx",    # bot.handlers.audience
}

# `extract_text` слишком общее имя (так же зовётся метод страницы pypdf),
# поэтому ловится только импортированное из services.invite_list_parser.
_INVITE_PARSER_NAMES = {"extract_text"}

_SKIP_DIRS = {"tests", ".venv", "venv", "node_modules", "__pycache__", ".git"}


def _py_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for f in filenames:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


def _invite_parser_aliases(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and (n.module or "").endswith("invite_list_parser"):
            for a in n.names:
                if a.name in _INVITE_PARSER_NAMES:
                    out.add(a.asname or a.name)
    return out


def _direct_calls(fn: ast.AsyncFunctionDef):
    """Вызовы в теле корутины, кроме вложенных синхронных функций/lambda и
    вложенных корутин (те проверяются отдельно, как свои)."""
    stack = list(fn.body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.Lambda, ast.AsyncFunctionDef)):
            continue
        if isinstance(n, ast.Call):
            yield n
        stack.extend(ast.iter_child_nodes(n))


def _called_name(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def find_violations() -> list[str]:
    bad: list[str] = []
    for path in _py_files():
        try:
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        aliases = _invite_parser_aliases(tree)
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for call in _direct_calls(fn):
                name = _called_name(call)
                if name in HEAVY or (isinstance(call.func, ast.Name) and name in aliases):
                    rel = os.path.relpath(path, ROOT)
                    bad.append(f"{rel}:{call.lineno} {fn.name}: {name}(...)")
    return sorted(bad)


def test_no_heavy_cpu_directly_in_coroutines():
    bad = find_violations()
    assert not bad, (
        "Тяжёлый синхронный разбор/сборка файла прямо в корутине замораживает "
        "весь процесс (бот, мини-апп, операции). Оберни в "
        "`await asyncio.to_thread(fn, ...)`:\n  " + "\n  ".join(bad)
    )


def test_detector_sees_a_planted_violation(tmp_path, monkeypatch):
    """Детектор не слепой: та же ошибка в новом файле находится, а вызов
    через to_thread и во вложенной синхронной функции — нет."""
    src = (
        "import asyncio\n"
        "from services.invite_list_parser import extract_text\n"
        "from openpyxl import load_workbook\n"
        "async def bad(data):\n"
        "    return extract_text('a.xlsx', data)\n"
        "async def bad2(data):\n"
        "    return load_workbook(data)\n"
        "async def good(data):\n"
        "    def _sync():\n"
        "        return load_workbook(data)\n"
        "    await asyncio.to_thread(extract_text, 'a.xlsx', data)\n"
        "    return await asyncio.to_thread(_sync)\n"
    )
    (tmp_path / "mod.py").write_text(src, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", str(tmp_path))
    bad = find_violations()
    assert [b.split(" ", 1)[1] for b in bad] == [
        "bad: extract_text(...)",
        "bad2: load_workbook(...)",
    ]


# ── Поведение: работа действительно уходит в другой поток ────────────────────


async def test_bot_invite_file_is_parsed_off_the_event_loop(monkeypatch):
    from bot.handlers import mass_inviter
    from services import invite_list_parser as ilp

    seen = {}

    def _fake_parse(filename, data, mode="refs"):
        seen["thread"] = threading.current_thread()
        return ["@a", "@b"]

    monkeypatch.setattr(ilp, "parse_invite_file", _fake_parse)

    async def get_file(file_id):
        return SimpleNamespace(file_path="x")

    async def download_file(path):
        return io.BytesIO(b"@a\n@b\n")

    async def answer(*a, **k):
        seen["answered"] = a

    msg = SimpleNamespace(
        document=SimpleNamespace(file_size=10, file_id="f", file_name="l.txt"),
        bot=SimpleNamespace(get_file=get_file, download_file=download_file),
        answer=answer,
    )
    out = await mass_inviter._refs_from_document(msg, "refs")
    assert out == ["@a", "@b"]
    assert seen["thread"] is not threading.main_thread()


async def test_ai_document_is_extracted_off_the_event_loop(monkeypatch):
    from bot.handlers import ai_assistant

    seen = {}

    def _fake_extract(file_name, mime, raw):
        seen["thread"] = threading.current_thread()
        return "текст"

    monkeypatch.setattr(ai_assistant, "_extract_document_text", _fake_extract)

    async def get_file(file_id):
        return SimpleNamespace(file_path="x")

    async def download_file(path):
        return io.BytesIO(b"%PDF-")

    msg = SimpleNamespace(
        document=SimpleNamespace(file_size=5, file_id="f", file_name="a.pdf",
                                 mime_type="application/pdf"),
        bot=SimpleNamespace(get_file=get_file, download_file=download_file),
    )
    name, text = await ai_assistant._read_attached_document(msg)
    assert (name, text) == ("a.pdf", "текст")
    assert seen["thread"] is not threading.main_thread()


async def test_audience_xlsx_is_built_off_the_event_loop(monkeypatch):
    from datetime import datetime

    from openpyxl import load_workbook

    from bot.handlers import audience

    real_build = audience._build_audience_xlsx
    seen = {}

    def _spy(rows, stats, label):
        seen["thread"] = threading.current_thread()
        return real_build(rows, stats, label)

    monkeypatch.setattr(audience, "_build_audience_xlsx", _spy)

    async def get_bot(pool, bot_id, owner_id):
        return {"bot_id": 1, "username": "b", "first_name": "B"}

    rows = [{
        "user_id": 7, "username": "u", "first_name": "Ю", "last_name": None,
        "language_code": "ru", "first_seen": datetime(2026, 1, 1),
        "last_seen": None, "is_active": True,
    }]

    async def get_audience_full(pool, bot_id):
        return rows

    async def get_audience_stats(pool, bot_id):
        return {"total": 1}

    monkeypatch.setattr(audience.db, "get_bot", get_bot)
    monkeypatch.setattr(audience.db, "get_audience_full", get_audience_full)
    monkeypatch.setattr(audience.db, "get_audience_stats", get_audience_stats)

    sent = {}

    async def answer_document(doc, **k):
        sent["doc"] = doc

    async def cb_answer(*a, **k):
        return None

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        answer=cb_answer,
        message=SimpleNamespace(answer_document=answer_document),
    )
    await audience.cb_export_xlsx(cb, SimpleNamespace(bot_id=1), pool=None)

    assert seen["thread"] is not threading.main_thread()
    wb = load_workbook(io.BytesIO(sent["doc"].data))
    assert wb["Аудитория"]["A2"].value == 7
    assert wb["Статистика"]["B3"].value == 1


def test_to_thread_keeps_loop_responsive():
    """Смысл приёма: пока тяжёлое идёт в потоке, цикл продолжает обслуживать
    другие корутины (GIL отпускается каждые несколько мс)."""
    import time

    def _cpu(sec):
        end = time.perf_counter() + sec
        x = 0
        while time.perf_counter() < end:
            x += 1
        return x

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        t = asyncio.create_task(ticker())
        await asyncio.to_thread(_cpu, 0.5)
        t.cancel()
        return ticks

    assert asyncio.run(main()) >= 10
