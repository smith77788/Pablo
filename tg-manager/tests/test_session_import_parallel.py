"""Импорт сессий не должен обрывать первый шаг продукта тайм-аутом.

Дефект: сессии проверялись СТРОГО ПО ОЧЕРЕДИ, до 15 с на каждую, порцией до 20.
Тайм-аут мини-аппа — 30 с, значит уже три медленные строки обрывали запрос:
человек видел «нет соединения», хотя импорт на сервере продолжался, и жал
повтор. Вдобавок wait_for покрывал только connect, а последующий get_me висел
без ограничения вовсе — одна такая сессия подвешивала весь импорт.
"""
from __future__ import annotations

import asyncio
import pathlib
import time

import pytest

from services import session_importer as SI

_ROOT = pathlib.Path(__file__).resolve().parent.parent


class _Pool:
    """Пул, где ничего не дублируется и всё пишется успешно."""

    def __init__(self):
        self.inserted = []

    async def fetchval(self, q, *a):
        return 1          # прокси принадлежит владельцу

    async def fetchrow(self, q, *a):
        return None       # дубликатов нет

    async def execute(self, q, *a):
        self.inserted.append(a)
        return "INSERT 0 1"


def _run(monkeypatch, lines, delay=0.0, valid=True):
    calls = {"n": 0, "peak": 0, "cur": 0}

    async def _fake_validate(sess, proxy_url=None, timeout_s=None):
        calls["n"] += 1
        calls["cur"] += 1
        calls["peak"] = max(calls["peak"], calls["cur"])
        if delay:
            await asyncio.sleep(delay)
        calls["cur"] -= 1
        if not valid:
            return {"valid": False, "error": "AUTH_KEY_UNREGISTERED"}
        return {"valid": True, "phone": "+79990000000", "user_id": 1,
                "first_name": "И", "username": "u"}

    monkeypatch.setattr(SI, "validate_session", _fake_validate)
    monkeypatch.setattr(SI, "detect_format", lambda d: "string")
    monkeypatch.setattr(SI, "extract_session_string", lambda d, f: d)
    pool = _Pool()
    t0 = time.monotonic()
    res = asyncio.run(SI.import_sessions(pool, 1, "\n".join(lines)))
    return res, pool, calls, time.monotonic() - t0


def test_validation_runs_in_parallel(monkeypatch):
    """Десять «медленных» сессий обязаны проверяться разом, а не по очереди."""
    lines = [f"sess{i}" for i in range(10)]
    res, _pool, calls, elapsed = _run(monkeypatch, lines, delay=0.2)
    assert res["imported"] == 10
    assert calls["peak"] > 1, "проверки шли последовательно"
    assert elapsed < 10 * 0.2 * 0.8, f"суммарное время как у очереди: {elapsed:.2f}s"


def test_concurrency_is_bounded(monkeypatch):
    """Без ограничения массовый импорт открыл бы сразу все соединения."""
    lines = [f"sess{i}" for i in range(20)]
    _res, _pool, calls, _e = _run(monkeypatch, lines, delay=0.05)
    assert calls["peak"] <= SI.VALIDATE_CONCURRENCY


def test_line_numbers_stay_in_order(monkeypatch):
    """Параллельная проверка не должна перепутать нумерацию строк в ошибках."""
    monkeypatch.setattr(SI, "detect_format",
                        lambda d: "unknown" if d == "плохая" else "string")
    monkeypatch.setattr(SI, "extract_session_string", lambda d, f: d)

    async def _ok(sess, proxy_url=None, timeout_s=None):
        return {"valid": True, "phone": "", "user_id": 1, "first_name": "", "username": ""}

    monkeypatch.setattr(SI, "validate_session", _ok)
    res = asyncio.run(SI.import_sessions(_Pool(), 1, "хорошая\nплохая\nхорошая2"))
    assert res["imported"] == 2 and res["failed"] == 1
    assert any("Строка 2" in e for e in res["errors"]), res["errors"]


def test_parse_errors_do_not_trigger_network_calls(monkeypatch):
    """Мусорные строки не должны занимать сетевой слот."""
    monkeypatch.setattr(SI, "detect_format", lambda d: "unknown")
    called = {"n": 0}

    async def _v(sess, proxy_url=None, timeout_s=None):
        called["n"] += 1
        return {"valid": True}

    monkeypatch.setattr(SI, "validate_session", _v)
    res = asyncio.run(SI.import_sessions(_Pool(), 1, "мусор1\nмусор2"))
    assert called["n"] == 0 and res["failed"] == 2


def test_one_exploding_validation_does_not_abort_the_import(monkeypatch):
    """Попытка сломать: проверка одной сессии падает исключением."""
    monkeypatch.setattr(SI, "detect_format", lambda d: "string")
    monkeypatch.setattr(SI, "extract_session_string", lambda d, f: d)

    async def _v(sess, proxy_url=None, timeout_s=None):
        if sess == "bad":
            raise RuntimeError("сеть отвалилась")
        return {"valid": True, "phone": "", "user_id": 1, "first_name": "", "username": ""}

    monkeypatch.setattr(SI, "validate_session", _v)
    res = asyncio.run(SI.import_sessions(_Pool(), 1, "ok1\nbad\nok2"))
    assert res["imported"] == 2 and res["failed"] == 1


def test_invalid_sessions_are_reported_not_imported(monkeypatch):
    res, pool, _c, _e = _run(monkeypatch, ["a", "b"], valid=False)
    assert res["imported"] == 0 and res["failed"] == 2 and pool.inserted == []


def test_batch_truncation_still_reported(monkeypatch):
    """Порция ограничена — сообщение об остатке обязано остаться первым."""
    lines = [f"s{i}" for i in range(30)]
    res, _p, _c, _e = _run(monkeypatch, lines)
    assert res["errors"] and "не обработано за раз" in res["errors"][0]


# ── Бюджет времени ────────────────────────────────────────────────────────────

def _func_source(relpath: str, name: str) -> str:
    """Точные границы функции через AST.

    Окно фиксированной длины здесь недопустимо: ниже стоит ОТРИЦАТЕЛЬНАЯ
    проверка, и стоит коду сдвинуться — окно промахнётся, «искомого нет»
    станет правдой, и защита выключится молча.
    """
    import ast

    src = (_ROOT / relpath).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            seg = ast.get_source_segment(src, node)
            assert seg is not None
            return seg
    raise AssertionError(f"{name} не найдена в {relpath}")


def test_timeout_covers_the_whole_conversation():
    """Раньше ограничивался только connect, а get_me висел без предела."""
    body = _func_source("services/session_importer.py", "validate_session")
    assert "async def _talk" in body and "wait_for(_talk()" in body
    assert "wait_for(client.connect()" not in body


def test_batch_fits_the_frontend_timeout():
    """Порция должна укладываться в 30 с тайм-аута мини-аппа."""
    ui = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert "const FETCH_TIMEOUT_MS = 30000;" in ui
    src = (_ROOT / "services" / "session_importer.py").read_text(encoding="utf-8")
    max_per = int(src.split("MAX_PER_IMPORT = ")[1].split()[0])
    waves = -(-max_per // SI.VALIDATE_CONCURRENCY)      # ceil
    assert waves * SI.VALIDATE_TIMEOUT_S < 30, (
        f"худший случай {waves * SI.VALIDATE_TIMEOUT_S}s не влезает в тайм-аут фронта"
    )
