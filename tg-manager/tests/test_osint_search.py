"""Поиск по нику (модуль blackbird): разбор ввода, запуск процесса, итог.

Настоящий Blackbird ходит в сеть на 600 сайтов, поэтому здесь вместо него —
поддельный модуль в tmp: тот же интерфейс (`python -m pablo_blackbird
--username X` печатает JSON), а поведение задаётся запросом.
"""
from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

import pytest
from services import osint_search as osint

REPO = Path(__file__).resolve().parents[2]


# ── Разбор ввода ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("johndoe", ("username", "johndoe")),
    ("  @john.doe_1 ", ("username", "john.doe_1")),
    ("https://t.me/johndoe", ("username", "johndoe")),
    ("John@Example.com", ("email", "john@example.com")),
])
def test_parse_query_understands_nick_link_and_email(raw, expected):
    assert osint.parse_query(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "two words", "-rf", "../etc", "a", "ник", "x@y", "a\nb",
    "--username", "x" * 65,
])
def test_parse_query_rejects_garbage_with_a_russian_reason(raw):
    with pytest.raises(ValueError) as exc:
        osint.parse_query(raw)
    assert any("а" <= ch <= "я" for ch in str(exc.value).lower())


# ── Итог для уведомления ─────────────────────────────────────────────────────

def test_summary_escapes_html_and_caps_links():
    data = {"accounts": [
        {"name": f"S<{i}>", "url": f"https://x.com/a?b=1&c={i}", "status": "FOUND"}
        for i in range(5)]}
    text = osint.summary_text("username", "<b>", data, max_links=3)
    assert "<b>" not in text and "&lt;b&gt;" in text
    assert "&amp;c=0" in text
    assert "найдено аккаунтов — 5" in text
    assert "ещё 2" in text


def test_summary_says_nothing_found():
    assert "не найдено" in osint.summary_text("email", "a@b.cd", {"accounts": []})


# ── Запуск процесса ──────────────────────────────────────────────────────────

FAKE = textwrap.dedent('''
    import json, sys, time
    args = sys.argv[1:]
    q = args[1]
    if q == "slow":
        time.sleep(30)
    if q == "boom":
        print("сломалось", file=sys.stderr); sys.exit(2)
    if q == "junk":
        print("not json"); sys.exit(0)
    print(json.dumps({"query": q, "kind": args[0][2:], "found_count": 1,
        "accounts": [{"name": "GitHub", "url": "https://github.com/" + q,
                      "category": "coding", "status": "FOUND"}]}))
''')


@pytest.fixture
def fake_module(tmp_path, monkeypatch):
    pkg = tmp_path / "pablo_blackbird"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text(FAKE)
    monkeypatch.setenv("BLACKBIRD_DIR", str(tmp_path))
    monkeypatch.setenv("BLACKBIRD_PYTHON", sys.executable)
    return tmp_path


def test_unavailable_without_module(tmp_path, monkeypatch):
    monkeypatch.setenv("BLACKBIRD_DIR", str(tmp_path))
    monkeypatch.delenv("BLACKBIRD_PYTHON", raising=False)
    assert not osint.is_available()
    with pytest.raises(osint.SearchUnavailable):
        asyncio.run(osint.run_search("username", "johndoe"))


def test_run_search_returns_found_accounts(fake_module):
    assert osint.is_available()
    data = asyncio.run(osint.run_search("username", "johndoe"))
    found = osint.found_accounts(data)
    assert [a["url"] for a in found] == ["https://github.com/johndoe"]
    assert osint.log_message(found[0]) == "GitHub · coding"


@pytest.mark.parametrize("q", ["boom", "junk"])
def test_run_search_failure_is_loud(fake_module, q):
    with pytest.raises(osint.SearchFailed):
        asyncio.run(osint.run_search("username", q))


def test_stop_kills_the_process(fake_module, monkeypatch):
    monkeypatch.setattr(osint, "_CANCEL_POLL_S", 0.1)
    calls = {"n": 0}

    async def stop():
        calls["n"] += 1
        return calls["n"] >= 2

    async def go():
        t0 = asyncio.get_running_loop().time()
        res = await osint.run_search("username", "slow", should_stop=stop)
        return res, asyncio.get_running_loop().time() - t0

    res, took = asyncio.run(go())
    assert res is None and took < 10


def test_a_db_blip_is_not_a_stop(fake_module, monkeypatch):
    monkeypatch.setattr(osint, "_CANCEL_POLL_S", 0.05)

    async def broken():
        raise ConnectionError("blip")

    data = asyncio.run(osint.run_search("username", "johndoe", should_stop=broken))
    assert data["found_count"] == 1


def test_timeout_kills_the_process(fake_module, monkeypatch):
    monkeypatch.setattr(osint, "_CANCEL_POLL_S", 0.1)
    with pytest.raises(osint.SearchFailed, match="не уложился"):
        asyncio.run(osint.run_search("username", "slow", timeout_s=0.5))


# ── Проводка ─────────────────────────────────────────────────────────────────

def test_operation_is_registered_and_dispatched():
    from services import op_worker, operation_bus
    assert "osint_search" in operation_bus.OP_REGISTRY
    assert op_worker.handler_for("osint_search") is not None


def test_module_ships_with_the_repo_and_image():
    assert (REPO / "blackbird" / "pablo_blackbird" / "__main__.py").is_file()
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "/app/blackbird/.venv" in dockerfile
    assert osint.DEFAULT_DIR == "/app/blackbird"


def test_bot_audience_section_has_the_button():
    from bot.handlers import botmother_menu
    texts = [t for t, _ in botmother_menu._section_buttons("audience")]
    assert any("Поиск по нику" in t for t in texts)


# ── Исполнитель операции ─────────────────────────────────────────────────────

def _run_exec(monkeypatch, data, params=None):
    from services import op_worker

    calls = {"exec": [], "many": []}

    async def fake_exec(pool, q, *a, **k):
        calls["exec"].append((q, a))
        return "OK"

    async def fake_many(pool, q, rows, **k):
        calls["many"].append(list(rows))
        return "OK"

    async def never(pool, op_id):
        return False

    async def fake_search(kind, query, **k):
        if isinstance(data, Exception):
            raise data
        return data

    monkeypatch.setattr(op_worker, "_safe_execute", fake_exec)
    monkeypatch.setattr(op_worker, "_journal_writemany", fake_many)
    monkeypatch.setattr(op_worker, "_is_cancelled", never)
    monkeypatch.setattr(osint, "run_search", fake_search)
    res = asyncio.run(op_worker.handler_for("osint_search")(
        None, None, 7, 1, params or {"kind": "username", "query": "johndoe"}))
    return res, calls


def test_executor_writes_found_profiles_to_the_journal(monkeypatch):
    data = {"accounts": [
        {"name": "GitHub", "url": "https://github.com/johndoe", "category": "coding", "status": "FOUND"},
        {"name": "X", "url": "https://x.com/johndoe", "category": "social", "status": "FOUND"},
    ]}
    res, calls = _run_exec(monkeypatch, data)
    assert res["status"] == "done" and res["found"] == 2
    assert "github.com/johndoe" in res["summary"]
    rows = calls["many"][0]
    assert [r[2] for r in rows] == ["https://github.com/johndoe", "https://x.com/johndoe"]
    # повтор не плодит дублей: журнал операции чистится до записи
    assert any("DELETE FROM operation_log" in q for q, _ in calls["exec"])


def test_executor_reports_nothing_found_as_done(monkeypatch):
    res, calls = _run_exec(monkeypatch, {"accounts": []})
    assert res["status"] == "done" and res["found"] == 0 and not calls["many"]


def test_executor_fails_honestly(monkeypatch):
    res, _ = _run_exec(monkeypatch, osint.SearchUnavailable("нет модуля"))
    assert res["status"] == "failed" and "нет модуля" in res["summary"]
    res, _ = _run_exec(monkeypatch, None, {"query": "two words"})
    assert res["status"] == "failed"


def test_executor_stop_is_cancelled(monkeypatch):
    res, _ = _run_exec(monkeypatch, None)
    assert res["status"] == "cancelled"
