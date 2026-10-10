"""«Нотариус»: проводка — цепочка UI → маршрут → наблюдатель → БД жива.

Чистое ядро можно сделать безупречным и не подключить: экран будет, кнопки
будут, а протокол не появится никогда. Здесь проверяется именно стык.

Отдельно стерегутся два инварианта наблюдателя, нарушение которых не видно на
юнит-тестах ядра:
* наблюдение захватывает аккаунт (живая сессия → AUTH_KEY_DUPLICATED);
* ошибка чтения превращается в `unknown`, а не в «поста нет».
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
UI = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
OBS = (ROOT / "services" / "notary_observer.py").read_text(encoding="utf-8")


def _fn(src: str, name: str) -> str:
    for n in ast.walk(ast.parse(src)):
        if (isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                and n.name == name):
            return ast.get_source_segment(src, n) or ""
    raise AssertionError(f"{name} не найдена")


# ── Маршруты и экран ───────────────────────────────────────────────────────

def test_all_routes_are_registered():
    for route, method in (
        ('add_get("/api/miniapp/notary"', "список"),
        ('add_post("/api/miniapp/notary"', "создание"),
        ('add_get("/api/miniapp/notary/{watch_id}"', "протокол"),
        ('add_post("/api/miniapp/notary/{watch_id}/check"', "проверить сейчас"),
        ('add_delete("/api/miniapp/notary/{watch_id}"', "снятие"),
    ):
        assert route in API, f"нет маршрута: {method}"


def test_screens_exist_and_are_reachable():
    for sid in ("s-notary", "s-notarynew", "s-notarycert"):
        assert f'id="{sid}"' in UI, sid
    assert 'onclick="openNotary()"' in UI, "нет входа в раздел"


def test_every_screen_function_exists():
    for fn in ("openNotary", "openNotaryNew", "submitNotary", "openNotaryCert",
               "notaryCheckNow", "notaryCancel", "copyNotaryCert"):
        assert re.search(rf"(async\s+)?function\s+{fn}\s*\(", UI), fn


def test_frontend_reads_only_keys_the_api_returns():
    """Ключи ответа, на которых держится экран."""
    for key in ('"watches"', '"verdict_label"', '"verdict_hint"', '"facts"',
                '"journal"', '"certificate"', '"burst"', '"signature_valid"'):
        assert key in API, key


def test_certificate_text_is_not_stuffed_into_an_attribute():
    """Протокол — длинный текст с переводами строк и апострофами."""
    assert "NOTARY_CERT_TEXT" in UI
    assert "copyNotaryCert()" in UI


# ── Схема доезжает до прода ────────────────────────────────────────────────

def test_schema_file_exists():
    assert (ROOT / "schema_v210_notary.sql").exists()


def test_tables_are_mirrored_into_inline_migrations():
    """schema_v*.sql применяется журналом, но mini_app лечит схему сам — без
    зеркала новая таблица не появится в уже живой базе."""
    assert "CREATE TABLE IF NOT EXISTS notary_watches" in API
    assert "CREATE TABLE IF NOT EXISTS notary_observations" in API


# ── Фоновый цикл заведён ───────────────────────────────────────────────────

def test_observer_loop_is_started():
    assert "notary_observer" in MAIN and "notary_observer.run" in MAIN


# ── Инварианты наблюдателя ─────────────────────────────────────────────────

def test_observation_claims_the_account():
    body = _fn(OBS, "observe")
    assert "try_claim_account" in body, "наблюдение поднимает сессию без захвата"
    assert "release_accounts" in body, "захват не освобождается"


def test_observer_uses_the_single_door_for_account_choice():
    assert "select_account_rotated" in _fn(OBS, "observe")


def test_busy_account_is_not_read_as_a_missing_post():
    body = _fn(OBS, "observe")
    i = body.find("нет свободного наблюдателя")
    assert i != -1, "нет ветки «наблюдатель занят»"
    assert "UNKNOWN" in body[max(0, i - 200):i], "занятость должна давать unknown"


def test_read_failures_become_unknown():
    body = _fn(OBS, "read_post")
    assert re.search(r"except Exception[\s\S]{0,200}notary\.UNKNOWN", body), (
        "ошибка чтения обязана давать unknown, иначе своя сетевая проблема "
        "превратится в обвинение канала"
    )


def test_reader_always_disconnects():
    body = _fn(OBS, "read_post")
    assert "finally:" in body and "disconnect" in body


def test_journal_is_append_only():
    """Протокол, который можно переписать задним числом, ничего не стоит."""
    assert "UPDATE notary_observations" not in OBS
    assert "DELETE FROM notary_observations" not in OBS


def test_cancel_keeps_the_journal():
    body = _fn(API, "notary_cancel")
    assert "status='cancelled'" in body
    assert "DELETE FROM notary_watches" not in body


# ── Подпись ────────────────────────────────────────────────────────────────

def test_signature_roundtrip():
    from services import notary_observer as no
    payload = "1|@ch|55|kept"
    assert no.verify(payload, no.sign(payload))
    assert not no.verify(payload + "x", no.sign(payload))
    assert not no.verify(payload, "")


def test_detail_shows_signature_only_when_it_verifies():
    """«Подписано, но не сходится» опаснее, чем без подписи."""
    body = _fn(API, "notary_detail")
    assert "signature_valid" in body and "_no.verify(" in body
    assert "sig if verified else None" in body


# ── Создание наблюдения проверяет вход ─────────────────────────────────────

def test_create_validates_its_input():
    body = _fn(API, "notary_create")
    assert "Укажите канал" in body
    assert "0.5 <= hours" in body, "срок должен быть ограничен с обеих сторон"


def test_post_id_is_optional_by_design():
    """Без id поста доказывается обратное: оплачено, но не опубликовано."""
    body = _fn(API, "notary_create")
    assert "msg_id = int(msg_id) if msg_id else None" in body


# ── Такт наблюдателя ───────────────────────────────────────────────────────

class _FakePool:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    async def fetch(self, q, *a):
        return self.rows if "notary_watches" in q else []

    async def execute(self, q, *a):
        self.executed.append(q)

    async def fetchrow(self, q, *a):
        return None


def test_tick_survives_a_failing_observation():
    """Один упавший канал не должен рушить весь такт панели."""
    from services import notary_observer as no

    async def _boom(pool, watch):
        raise RuntimeError("сеть")

    orig = no.observe
    no.observe = _boom
    try:
        n = asyncio.run(no.tick(_FakePool([{"id": 1, "owner_id": 7}]), None))
    finally:
        no.observe = orig
    assert n == 0


def test_tick_picks_only_due_active_watches():
    from services import notary_observer as no
    src = _fn(OBS, "tick")
    assert "status='active'" in src and "next_check_at <= now()" in src
    assert no.BATCH > 0
