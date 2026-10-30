"""Вставка на восемьсот прокси добавляла пятьсот, а остальные стирала.

Ручка `POST /api/miniapp/proxies/import` звала разборщик без аргументов, а у
того потолок в значении по умолчанию — `_collect_valid_proxies(raw,
limit=500)`. Разборщик доходил до пятисотой строки и выходил из цикла; строки
за ней не считались даже пропущенными. Ответ говорил «added: 500, skipped: N»,
экран показывал «✅ Добавлено: 500» — и ОЧИЩАЛ поле ввода. Триста прокси,
которые владелец только что вставил, исчезали вместе с текстом: ни в ответе,
ни на экране, ни в поле от них не оставалось ничего.

Та же порода, что у сто первой короткой ссылки: потолок стоит не в тексте
обработчика, а в чужой подписи. Но цена другая. У ссылок данные лежали в базе
и просто не показывались — здесь владелец терял то, чего в базе ещё нет, и
единственная копия была в поле, которое экран стирал.

Проверка `test_cap_500` в `tests/test_import_proxies_parse.py` сама же и
утверждала `skipped == 0` на вставке из шестисот: потолок был описан как
нормальное поведение, и сто строк «пропадали по проекту».
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest
from aiohttp import web

from services import mini_app_api as M
from services import mini_app_proxies as P
from tests.miniapp_source import js_function, miniapp_source

UID = 707404
PATH = "/api/miniapp/proxies/import"


class _Pool:
    """Пул прокси владельца: INSERT ... RETURNING id на каждую строку."""

    def __init__(self) -> None:
        self.inserted = 0

    async def fetch(self, q, *a):
        if "INSERT INTO user_proxies" in q:
            self.inserted += len(a[1])
            return [{"id": i} for i in range(len(a[1]))]
        return []

    async def fetchval(self, q, *a):
        return 0

    async def fetchrow(self, q, *a):
        return None

    async def execute(self, q, *a):
        return "OK"


class _Req:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.headers = {"Host": "infragram.test"}
        self.rel_url = type("U", (), {"query": {}})()
        self.query: dict[str, str] = {}
        self.match_info: dict[str, str] = {}

    async def json(self):
        return self._payload


def _call(pool, payload: dict) -> dict:
    app = web.Application()
    P.setup_routes(app, pool)
    handler = None
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        if route.method == "POST" and (info.get("path") or info.get("formatter")) == PATH:
            handler = route.handler
    assert handler, f"роут POST {PATH} не зарегистрирован"
    resp = asyncio.run(handler(_Req(payload)))
    return json.loads(resp.body.decode("utf-8"))


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(M, "_get_uid", lambda r: UID)
    monkeypatch.setattr(P, "_get_uid", lambda r: UID, raising=False)
    yield


# ── Разборщик ──────────────────────────────────────────────────────────────

def test_the_parser_counts_what_it_left_behind():
    raw = "\n".join(f"socks5://9.9.{i // 256}.{i % 256}:1080" for i in range(800))
    purls, skipped, overflow = M._collect_valid_proxies(raw)
    assert len(purls) == M.PROXY_IMPORT_BATCH
    assert overflow == 800 - M.PROXY_IMPORT_BATCH, (
        f"за потолком осталось {overflow}, а вставлено было 800")


def test_empty_lines_past_the_ceiling_are_not_counted():
    """Пустые строки в хвосте — не потерянные прокси, а перевод строки."""
    raw = ("\n".join(f"socks5://8.8.{i // 256}.{i % 256}:1080"
                     for i in range(M.PROXY_IMPORT_BATCH))
           + "\n\n\n")
    _purls, _skipped, overflow = M._collect_valid_proxies(raw)
    assert overflow == 0, overflow


# ── Ответ ручки ────────────────────────────────────────────────────────────

def test_the_answer_says_how_much_did_not_fit():
    pool = _Pool()
    raw = "\n".join(f"socks5://7.7.{i // 256}.{i % 256}:1080" for i in range(800))
    d = _call(pool, {"proxies": raw})

    assert d["added"] == M.PROXY_IMPORT_BATCH, d["added"]
    assert d["overflow"] == 300, (
        "ответ молчит про строки за потолком: экран снова скажет «Добавлено: "
        "500» и сотрёт остальные триста")
    assert d["batch"] == M.PROXY_IMPORT_BATCH, (
        "ответ не называет размер пачки — экрану нечем отмерить свою")


def test_a_paste_that_fits_reports_no_overflow():
    pool = _Pool()
    raw = "\n".join(f"socks5://6.6.6.{i}:1080" for i in range(9))
    d = _call(pool, {"proxies": raw})
    assert d["added"] == 9 and d["overflow"] == 0 and d["cut_chars"] == 0


def test_a_paste_cut_by_length_says_so():
    """`validate_string(max_len=50000)` режет вставку ПОСЕРЕДИНЕ строки.

    Отрезанный хвост не доходит даже до разборщика, поэтому `overflow` его не
    видит: число отрезанных символов — отдельная величина.
    """
    pool = _Pool()
    raw = "\n".join(f"socks5://5.5.{i // 256}.{i % 256}:1080" for i in range(3000))
    assert len(raw) > 50_000, len(raw)
    d = _call(pool, {"proxies": raw})
    assert d["cut_chars"] > 0, (
        "вставку обрезали по длине, и ответ об этом не говорит")


# ── Экран ──────────────────────────────────────────────────────────────────

def test_the_screen_sends_the_whole_paste_in_batches():
    js = js_function("submitProxyImport")
    assert "PROXY_IMPORT_CHUNK" in js, (
        "экран отправляет всю вставку одним запросом: за потолком пачки "
        "строки снова потеряются")
    assert "chunks" in js and "for (" in js, js[:300]


def test_the_screens_batch_is_smaller_than_the_servers():
    """Иначе остаток пачки умрёт на сервере, и экран об этом не узнает."""
    src = js_function("submitProxyImport")
    m = re.search(r"PROXY_IMPORT_CHUNK\s*=\s*(\d+)", miniapp_source())
    assert m, "PROXY_IMPORT_CHUNK не объявлен"
    chunk = int(m.group(1))
    assert 0 < chunk < M.PROXY_IMPORT_BATCH, (
        f"пачка экрана {chunk} не меньше серверной {M.PROXY_IMPORT_BATCH}")
    assert "r.overflow" in src, (
        "экран не смотрит на overflow: если сервер снизит пачку, потеря "
        "вернётся молча")


def test_the_field_keeps_what_was_not_sent():
    js = js_function("submitProxyImport")
    assert "left.join" in js, (
        "неотправленные строки не возвращаются в поле — повторить будет нечем")
    assert "el.value=''" not in js.replace(" ", ""), (
        "поле очищается безусловно: единственная копия вставки стирается")
    assert "осталось в поле" in js, (
        "экран не говорит, что часть вставки осталась необработанной")


def test_the_toast_names_the_whole_sum():
    js = js_function("submitProxyImport")
    assert "added +=" in js, "итог не суммируется по пачкам"
    assert "num(added)" in js, "в тосте не сумма по пачкам"


# ── Тот же класс: получатели разовой рассылки ──────────────────────────────
#
# `submitDmAdhoc` предупреждал честно: «⚠️ За раз уйдёт только первая 1000 из
# 1500» — а после отправки очищал поле. Пятьсот оставшихся адресатов исчезали
# вместе с текстом, и «следующего раза», который обещало предупреждение,
# взяться было неоткуда. Сервер присылает их число в `truncated`, и фронт его
# не читал.
#
# Весь класс — ровно два места: вставка прокси и этот список. Остальные сорок
# функций, которые зовут api() и чистят поле, держат ОДНО значение (один прокси,
# один канал-цель, одна жалоба) — там очистка правильная.

def test_the_dm_screen_keeps_the_recipients_it_did_not_send():
    js = js_function("submitDmAdhoc")
    assert "r.truncated" in js, (
        "экран не читает `truncated`: он предупредил про потолок и всё равно "
        "сотрёт неотправленных")
    assert "uniq.slice(" in js, (
        "неотправленные получатели не отделяются от отправленных")
    assert "Осталось в поле" in js, (
        "экран не говорит, что часть получателей осталась необработанной")
    # очистка поля осталась, но только на полной отправке
    body = js[js.index("r.truncated"):]
    assert "else {" in body and "daUsers').value = ''" in body, (
        "поле очищается безусловно: единственная копия списка стирается")


def test_the_server_still_reports_what_it_cut():
    """Фронт читает `truncated` — значит ручка обязана его считать."""
    from tests.miniapp_source import api_function

    src = api_function("dm_adhoc_send")
    assert '"truncated": truncated' in src, src[-400:]
    assert "_uniq[:_LIMIT]" in src, (
        "отправляемый срез берётся не от дедуплицированного списка — число "
        "`truncated` тогда ничего не значит")
