"""Потолок партии объявляется, и номер телефона не режется пополам.

ДВЕ НАХОДКИ ОДНОГО КЛАССА (тот же, что у сегмента контактов: «молчаливый
потолок»), найдены пробником по всем ручкам API-слоя, которые режут список
целей константой.

1. ПРОВЕРКА НОМЕРОВ. Вставку резал `validate_string(max_len=10000)` —
   ПОСЕРЕДИНЕ номера. Обрубок вроде «+7916123» проходил фильтр `len >= 5` и
   уезжал на проверку как ДРУГОЙ номер: владелец получал ответ про чужой
   телефон и не мог этого заметить. Дальше список резался до 500 штук, и тост
   сообщал только это число — из двух тысяч номеров проверялись первые
   пятьсот, остальные исчезали молча.

2. РАЗОГРЕВ ЧАТА. `acc_ids[:30]` — отметив пятьдесят аккаунтов, владелец
   получал разогрев тридцатью и ни слова об остальных двадцати.

Проверяем по РЕАЛЬНОМУ пути: приложение собирается, обработчик берётся из
роутера и вызывается — а не сверяется по тексту.
"""
from __future__ import annotations

import asyncio
import json
import os
import re


os.environ.setdefault("MINIAPP_SIGNING_KEY", "x" * 40)

from aiohttp import web                                    # noqa: E402

from services import mini_app_api                          # noqa: E402


class _Pool:
    """Пул для ручек, которым хватает «свои живые аккаунты».

    Строка аккаунта отдаётся ПОЛНОЙ: разогрев теперь выбирает участников по
    готовности (свободен / занят арендой / на паузе / негоден), а не срезает
    список по id, и читает для этого те же поля, что и ход.
    """

    def __init__(self, accounts: int = 0):
        self.accounts = accounts

    async def fetch(self, query, *args):
        if "FROM tg_accounts" in query:
            rows = [{"id": i} for i in range(1, self.accounts + 1)]
            if "op_lease_until" not in query:
                return rows
            for r in rows:
                r.update({"label": f"acc{r['id']}", "is_active": True,
                          "has_session": True, "acc_status": "active",
                          "cooldown_until": None, "leased": False,
                          "trust_score": 50})
            return rows
        return []

    async def fetchrow(self, query, *args):
        return None

    async def fetchval(self, query, *args):
        return 0

    async def execute(self, query, *args):
        return "OK"


class _Req:
    def __init__(self, body: dict):
        self._body = body
        # Токен в запросе, а не подменённый _get_uid: ручки, вынесенные из
        # монолита в отдельные модули слоя, забирают _get_uid ПО ЗНАЧЕНИЮ в
        # момент сборки приложения, то есть раньше, чем monkeypatch успеет
        # что-то подменить. Настоящий _get_uid читает токен из запроса и
        # разбирает его через mini_app_api.parse_token — вот его и подменяем
        # (см. _call). Так проверка работает одинаково и для монолита, и для
        # вынесенных модулей.
        # Токен сессии — только в заголовке (D19): ?token= больше не принимается.
        self.query = {}
        self.match_info = {}
        self.headers = {"Authorization": "Bearer t"}
        self.method = "POST"
        self.path = "/api/miniapp/test"
        self.app = web.Application()

    async def json(self):
        return self._body


class _Bus:
    """Перехват постановки операции: что именно ушло в очередь."""

    def __init__(self):
        self.submitted: list[tuple] = []

    async def submit(self, pool, uid, op_type, params, **kw):
        self.submitted.append((op_type, params, kw))
        return 4242


def _handlers(pool) -> dict[str, object]:
    app = web.Application()
    mini_app_api.setup_routes(app, pool)
    out = {}
    for r in app.router.routes():
        out[f"{r.method} {r.resource.canonical}"] = r.handler
    return out


def _call(handler, body: dict, *, uid: int = 7, monkeypatch=None, bus=None):
    # parse_token ищется в globals mini_app_api в момент ВЫЗОВА, поэтому
    # подмена действует и на ручки вынесенных модулей; подмена _get_uid —
    # только на те, что остались в монолите. Нужны обе: первая для вынесенных,
    # вторая для тех, кто читает uid не из токена.
    monkeypatch.setattr(mini_app_api, "parse_token", lambda *a, **k: uid)
    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: uid)
    if bus is not None:
        monkeypatch.setattr(mini_app_api, "_obus", bus)
    loop = asyncio.new_event_loop()
    try:
        resp = loop.run_until_complete(handler(_Req(body)))
    finally:
        loop.close()
    return resp.status, json.loads(resp.body.decode("utf-8"))


# ── Проверка номеров ─────────────────────────────────────────────────────────

def _phone_handler(pool=None):
    return _handlers(pool or _Pool())["POST /api/miniapp/phone_check"]


def test_a_number_is_never_cut_in_half(monkeypatch):
    """Самое дорогое: обрубленный номер — это проверка ЧУЖОГО телефона.

    Форма вставки здесь не случайная. Старый потолок стоял на 10000 символов,
    а партия — на 500 номерах, поэтому обрубок доезжал до операции только
    когда в первые 10000 символов влезало МЕНЬШЕ пятисот номеров, то есть на
    форматированной вставке («+7 (916) …» с разделителем «, » — 21 символ на
    номер). Четыреста восемьдесят таких номеров — 10079 символов: старый код
    резал четыреста семьдесят седьмой пополам и отправлял его на проверку.
    """
    bus = _Bus()
    nums = ["+7 (916) %07d-55" % i for i in range(480)]
    body = {"phones": ", ".join(nums)}
    assert 10000 < len(body["phones"]) < 20000, len(body["phones"])
    digits = len(re.sub(r"\D", "", nums[0]))
    status, data = _call(_phone_handler(), body, monkeypatch=monkeypatch, bus=bus)
    assert status == 200, data
    sent = bus.submitted[0][1]["phones"]
    assert len(sent) == 480, f"номера потерялись: {len(sent)} из 480"
    for ph in sent:
        assert len(re.sub(r"\D", "", ph)) == digits, (
            f"номер {ph} короче остальных — его обрезали посередине, "
            "и на проверку уехал ЧУЖОЙ телефон")


def test_the_dropped_tail_is_counted(monkeypatch):
    bus = _Bus()
    status, data = _call(_phone_handler(),
                         {"phones": "\n".join("+7916%07d" % i for i in range(700))},
                         monkeypatch=monkeypatch, bus=bus)
    assert status == 200
    assert data["count"] == 500, data
    assert data["total"] == 700, "размер вставки не посчитан"
    assert data["truncated"] == 200, (
        "двести номеров не проверены, и ручка об этом молчит")


def test_duplicates_and_junk_are_named(monkeypatch):
    bus = _Bus()
    body = {"phones": "+79161234567, +7 (916) 123-45-67, abc, +79990001122"}
    status, data = _call(_phone_handler(), body, monkeypatch=monkeypatch, bus=bus)
    assert status == 200, data
    assert data["count"] == 2, data            # дубль по цифрам отброшен
    assert data["duplicates"] == 1, data
    assert data["invalid"] == 1, data          # «abc» номером не является


def test_a_wall_without_separators_is_refused_with_a_reason(monkeypatch):
    status, data = _call(_phone_handler(), {"phones": "7" * 25000},
                         monkeypatch=monkeypatch, bus=_Bus())
    assert status == 400
    assert "разделител" in data.get("error", "").lower(), data


def test_an_empty_list_still_says_so(monkeypatch):
    status, data = _call(_phone_handler(), {"phones": "   "},
                         monkeypatch=monkeypatch, bus=_Bus())
    assert status == 400 and "номера" in data.get("error", "").lower()


# ── Разогрев чата ────────────────────────────────────────────────────────────

def test_chat_warmup_names_the_accounts_left_out(monkeypatch):
    pool = _Pool(accounts=50)
    handler = _handlers(pool)["POST /api/miniapp/chatwarmup/session"]

    async def _create(*a, **kw):
        return {"id": 1}

    from services import chat_warmup
    monkeypatch.setattr(chat_warmup, "create_session", _create)
    status, data = _call(handler, {"chat_ref": "@somechat"}, monkeypatch=monkeypatch)
    assert status == 200, data
    assert data["accounts"] == 30, data
    assert data["requested"] == 50, "сколько аккаунтов выбрали — не сказано"
    assert data["omitted"] == 20, "двадцать аккаунтов не вошли, и об этом молчок"
    # И ПОЧЕМУ не вошли: «не поместился в потолок» и «забанен» — разные новости
    # и разные действия владельца, одно число их не различает.
    assert sum(d["count"] for d in data.get("omitted_detail") or []) == 20, data
    assert any("потолок" in d["reason"]
               for d in data.get("omitted_detail") or []), data


def test_chat_warmup_is_silent_when_nothing_was_dropped(monkeypatch):
    pool = _Pool(accounts=5)
    handler = _handlers(pool)["POST /api/miniapp/chatwarmup/session"]

    async def _create(*a, **kw):
        return {"id": 2}

    from services import chat_warmup
    monkeypatch.setattr(chat_warmup, "create_session", _create)
    status, data = _call(handler, {"chat_ref": "@somechat"}, monkeypatch=monkeypatch)
    assert (data["accounts"], data["omitted"]) == (5, 0)


# ── Экран ────────────────────────────────────────────────────────────────────

def test_both_screens_print_the_remainder():
    import pathlib

    ui = (pathlib.Path(__file__).resolve().parents[1]
          / "mini_app" / "index.html").read_text(encoding="utf-8")
    i = ui.index("Запущена проверка")
    assert "r.truncated" in ui[i - 600:i + 300], "экран молчит про непроверенные номера"
    j = ui.index("Разогрев запущен")
    assert "r.omitted" in ui[j:j + 300], "экран молчит про аккаунты вне потолка"
