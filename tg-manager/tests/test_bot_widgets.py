"""HTML-виджеты Telegram (HTMLBubbles, Bot API 10.4): чистая часть.

Что держит файл:
* каждый шаблон помещается в предел Telegram с запасом (zlib 9 ≤ 3800 из
  4096 байт) и его скрипт разбирается JavaScript-ом без ошибок;
* проверка своего кода называет то, что Telegram не примет, и то, что в
  виджете не сработает (сеть, внешние скрипты, поля ввода);
* форма → данные: неверное отбивается по-русски, данные ≤ 1024 байт;
* правка отправленного голосования сохраняет голоса и не даёт поменять
  варианты опроса (голоса привязаны к номеру варианта);
* вызовы идут под именами Bot API 10.4 (sendWidget, allow_queries, is_shared,
  public_data, editSharedWidgetData…) на ОСНОВНОЙ Bot API, а если он метода
  не знает (404, сервер ещё старый) — через бета-шлюз, и какое-то время сразу
  туда; основной снова берёт всё, как только ответит; off выключает бету;
* отказ Telegram доходит до человека по-русски.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess

import pytest

from services import bot_widgets as bw


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.mark.parametrize("tid", list(bw.TEMPLATES))
def test_every_template_fits_telegram_with_room_to_spare(tid):
    rep = bw.check(bw.TEMPLATES[tid]["html"])
    assert rep["ok"], rep
    assert rep["packed"] <= bw.PACKED_WARN, f"{tid}: {rep['packed']} байт после сжатия"
    assert not rep["warnings"], f"{tid}: шаблон сам нарушает профиль виджета: {rep['warnings']}"


@pytest.mark.skipif(not shutil.which("node"), reason="нужен node")
@pytest.mark.parametrize("tid", list(bw.TEMPLATES))
def test_every_template_script_parses(tid, tmp_path):
    html = bw.TEMPLATES[tid]["html"]
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(scripts) == 1
    f = tmp_path / f"{tid}.js"
    f.write_text(scripts[0], encoding="utf-8")
    res = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_templates_use_only_what_the_widget_profile_has():
    for tid, t in bw.TEMPLATES.items():
        html = t["html"]
        assert "window.TelegramWidget" in html, tid
        assert not re.search(r"<(input|select|textarea)\b", html), tid
        assert "fetch(" not in html and "XMLHttpRequest" not in html, tid
        # Русский интерфейс — без английских подписей для человека.
        assert "Vote" not in html and "Loading" not in html, tid
    for tid in ("poll", "rating", "rsvp"):
        assert "T.request('vote:'+i)" in bw.TEMPLATES[tid]["html"], tid
        assert bw.is_vote_template(tid)


def test_own_code_check_names_what_telegram_refuses_and_what_will_not_work():
    big = "<div>" + "x" * (bw.HTML_MAX + 10) + "</div>"
    assert not bw.check(big)["ok"]
    assert any("32 КБ" in e for e in bw.check(big)["errors"])
    noisy = "".join(chr(0x4E00 + (i * 7919) % 20000) for i in range(6000))
    rep = bw.check(noisy)
    assert not rep["ok"] and any("После сжатия" in e for e in rep["errors"])
    assert not bw.check("   ")["ok"]
    assert not bw.check("<div>\x00</div>")["ok"]
    rep = bw.check('<script src="https://cdn.x/y.js"></script><img src="https://x/y.png">'
                   "<input><script>fetch('/a')</script>")
    assert rep["ok"]
    joined = " ".join(rep["warnings"])
    for word in ("Внешние скрипты", "Картинки", "Сети в виджете нет", "Поля ввода"):
        assert word in joined


def test_form_values_become_widget_data_or_a_russian_refusal():
    now = 1_800_000_000
    d = bw.build_data("countdown", {"t": "Старт", "at": now + 3600}, now=now)
    assert d == {"t": "Старт", "at": now + 3600}
    for tpl, vals, word in [
        ("countdown", {"t": "Старт", "at": now - 1}, "уже прошла"),
        ("countdown", {"t": "", "at": now + 10}, "Заголовок"),
        ("poll", {"q": "Как?", "o": ["Да"]}, "от 2 до 6"),
        ("poll", {"q": "Как?", "o": ["Да", "да"]}, "повторяются"),
        ("quiz", {"q": "2+2?", "o": ["3", "4"], "a": 3}, "от 1 до 2"),
        ("card", {"t": "Акция", "u": "http://x.ru"}, "https://"),
        ("card", {"t": "Акция", "b": "Купить"}, "нет ссылки"),
        ("goal", {"t": "Сбор", "g": "много"}, "нужно число"),
        ("nope", {}, "шаблона нет"),
    ]:
        with pytest.raises(bw.WidgetError) as e:
            bw.build_data(tpl, vals, now=now)
        assert word in str(e.value), (tpl, str(e.value))
    poll = bw.build_data("poll", {"q": "Куда?", "o": "Море\nГоры\n\n"})
    assert poll == {"q": "Куда?", "o": ["Море", "Горы"], "v": [0, 0], "r": 0}
    quiz = bw.build_data("quiz", {"q": "2+2?", "o": ["3", "4"], "a": "2"})
    assert quiz["a"] == 1, "в форме счёт с единицы, в виджете — с нуля"
    assert bw.build_data("rating", {"t": "Оцените"})["v"] == [0] * 5
    assert bw.build_data("goal", {"t": "Сбор", "g": "1 000,5"})["g"] == 1000.5


def test_editing_a_sent_vote_keeps_the_votes_and_the_options():
    old = {"q": "Куда?", "o": ["Море", "Горы"], "v": [3, 5], "r": 8}
    new = bw.build_data("poll", {"q": "Куда летом?", "o": ["Море", "Горы"]})
    merged = bw.merge_edit("poll", old, new)
    assert merged["q"] == "Куда летом?" and merged["v"] == [3, 5] and merged["r"] == 9
    with pytest.raises(bw.WidgetError):
        bw.merge_edit("poll", old, bw.build_data("poll", {"q": "?", "o": ["Горы", "Море"]}))
    card = bw.merge_edit("card", {"t": "Старое"}, {"t": "Новое"})
    assert card == {"t": "Новое"}


def test_vote_data_trims_names_to_fit_1024_bytes():
    data = {"t": "Встреча", "w": "суббота", "p": "Парк"}
    names = ["Очень-длинное-имя-участника-" + str(i) for i in range(30)]
    out = bw.vote_public(data, [30, 1, 0], 7, names)
    text = bw.dump_data(out)
    assert bw.data_bytes(text) <= bw.DATA_MAX
    assert out["v"] == [30, 1, 0] and out["r"] == 7
    assert 0 < len(out["n"]) <= bw.RSVP_NAMES_MAX


@pytest.fixture(autouse=True)
def _fresh_route(monkeypatch):
    monkeypatch.setattr(bw, "_main_missing_until", 0.0)
    monkeypatch.delenv("TG_WIDGETS_API_BASE", raising=False)


_MISSING = {"ok": False, "error_code": 404, "description": "Not Found"}
_OK = {"ok": True, "result": {"message_id": 1, "chat": {"id": 5}}}


class _FakeApi:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, session, token, method, *, api_base=None, **params):
        self.calls.append((method, api_base, {k: v for k, v in params.items() if v is not None}))
        return self.answers.pop(0)


def test_send_uses_bot_api_10_4_names(monkeypatch):
    fake = _FakeApi([{"ok": True, "result": {"message_id": 7, "chat": {"id": -100},
                                              "widget": {"id": "w1"}}}])
    monkeypatch.setattr(bw.bot_api, "_call", fake)
    res = _run(bw.send(None, "t", "@grp", bw.TEMPLATES["poll"]["html"],
                       allow_queries=True, public_data='{"q":"?"}', caption="Опрос"))
    assert res == {"ok": True, "message_id": 7, "chat_id": -100, "widget_id": "w1"}
    method, base, params = fake.calls[0]
    assert method == "sendWidget" and base is None
    assert params["chat_id"] == "@grp" and params["allow_queries"] is True
    assert params["is_shared"] is True and params["public_data"] == '{"q":"?"}'
    assert params["caption"] == "Опрос" and "html" in params
    assert "width" not in params and "height" not in params, "0 — авто: поле не шлём"


def test_unknown_method_is_retried_through_the_beta_gateway(monkeypatch):
    fake = _FakeApi([{"ok": False, "error_code": 404, "description": "Not Found"},
                     {"ok": True, "result": {"message_id": 1, "chat": {"id": 5}}}])
    monkeypatch.setattr(bw.bot_api, "_call", fake)
    res = _run(bw.send(None, "t", 5, "<b>hi</b>"))
    assert res["ok"]
    assert [c[1] for c in fake.calls] == [None, bw.BETA_BASE]


def test_main_api_goes_first_and_beta_is_untouched_when_main_knows_widgets(monkeypatch):
    fake = _FakeApi([_OK, _OK])
    monkeypatch.setattr(bw.bot_api, "_call", fake)
    assert _run(bw.send(None, "t", 5, "<b>hi</b>"))["ok"]
    assert _run(bw.send_shared(None, "t", 6, "w1"))["ok"]
    assert [c[1] for c in fake.calls] == [None, None]
    assert bw.route() == "main"


def test_old_main_api_is_not_knocked_twice_on_every_call(monkeypatch):
    fake = _FakeApi([dict(_MISSING), _OK, _OK])
    monkeypatch.setattr(bw.bot_api, "_call", fake)
    assert _run(bw.send(None, "t", 5, "<b>hi</b>"))["ok"]
    assert bw.route() == "beta"
    assert _run(bw.send(None, "t", 5, "<b>hi</b>"))["ok"]
    assert [c[1] for c in fake.calls] == [None, bw.BETA_BASE, bw.BETA_BASE]


def test_main_api_is_rechecked_and_wins_back_after_the_pause(monkeypatch):
    fake = _FakeApi([dict(_MISSING), _OK, _OK])
    monkeypatch.setattr(bw.bot_api, "_call", fake)
    assert _run(bw.send(None, "t", 5, "<b>hi</b>"))["ok"]
    monkeypatch.setattr(bw, "_main_missing_until", 1.0)  # пауза истекла
    assert _run(bw.send(None, "t", 5, "<b>hi</b>"))["ok"]
    assert [c[1] for c in fake.calls] == [None, bw.BETA_BASE, None]
    assert bw.route() == "main" and bw._main_missing_until == 0.0


def test_beta_gone_while_main_is_marked_old_falls_back_to_main(monkeypatch):
    fake = _FakeApi([dict(_MISSING), _OK])
    monkeypatch.setattr(bw.bot_api, "_call", fake)
    monkeypatch.setattr(bw, "_main_missing_until", 10 ** 12)
    assert _run(bw.send(None, "t", 5, "<b>hi</b>"))["ok"]
    assert [c[1] for c in fake.calls] == [bw.BETA_BASE, None]
    assert bw.route() == "main"


def test_both_gateways_without_widgets_cost_two_calls_and_explain(monkeypatch):
    fake = _FakeApi([dict(_MISSING), dict(_MISSING)])
    monkeypatch.setattr(bw.bot_api, "_call", fake)
    res = _run(bw.send(None, "t", 5, "<b>hi</b>"))
    assert not res["ok"] and "Bot API 10.4" in res["error"]
    assert len(fake.calls) == 2


@pytest.mark.parametrize("value", ["off", "OFF", "0", "none"])
def test_beta_fallback_can_be_switched_off(monkeypatch, value):
    monkeypatch.setenv("TG_WIDGETS_API_BASE", value)
    fake = _FakeApi([dict(_MISSING)])
    monkeypatch.setattr(bw.bot_api, "_call", fake)
    assert not _run(bw.send(None, "t", 5, "<b>hi</b>"))["ok"]
    assert [c[1] for c in fake.calls] == [None]
    assert bw.route() == "main"


def test_custom_beta_gateway_is_respected(monkeypatch):
    monkeypatch.setenv("TG_WIDGETS_API_BASE", "https://bot-api.example/beta")
    fake = _FakeApi([dict(_MISSING), _OK])
    monkeypatch.setattr(bw.bot_api, "_call", fake)
    assert _run(bw.send(None, "t", 5, "<b>hi</b>"))["ok"]
    assert fake.calls[1][1] == "https://bot-api.example/beta"


def test_shared_widget_edit_reaches_every_copy(monkeypatch):
    fake = _FakeApi([{"ok": False, "error_code": 400,
                      "description": "Bad Request: message is not modified"}])
    monkeypatch.setattr(bw.bot_api, "_call", fake)
    res = _run(bw.edit_data(None, "t", chat_id=1, message_id=2, widget_id="w9",
                            public_data='{"v":[1]}'))
    assert res == {"ok": True}, "«не изменилось» — не ошибка"
    method, _, params = fake.calls[0]
    assert method == "editSharedWidgetData"
    assert params == {"widget_id": "w9", "public_data": '{"v":[1]}', "cache_time": bw.SHARED_CACHE_S}


def test_message_widget_edit_goes_by_chat_and_message(monkeypatch):
    fake = _FakeApi([{"ok": True, "result": True}])
    monkeypatch.setattr(bw.bot_api, "_call", fake)
    assert _run(bw.edit_data(None, "t", chat_id=1, message_id=2, public_data="{}"))["ok"]
    assert fake.calls[0][0] == "editMessageWidgetData"
    assert fake.calls[0][2] == {"chat_id": 1, "message_id": 2, "public_data": "{}"}


@pytest.mark.parametrize("desc,code,word", [
    ("Bad Request: INPUT_MEDIA_WIDGET_NOT_ALLOWED", 400, "только в личку"),
    ("Bad Request: INPUT_MEDIA_WIDGET_TOO_LARGE", 400, "4 КБ"),
    ("Bad Request: CHAT_SEND_PHOTOS_FORBIDDEN", 400, "медиа"),
    ("Bad Request: chat not found", 400, "Чат не найден"),
    ("Forbidden: bot can't initiate conversation with a user", 403, "«Старт»"),
    ("Forbidden: bot was blocked by the user", 403, "заблокирован"),
    ("Not Found", 404, "Bot API 10.4"),
    ("Too Many Requests: retry after 12", 429, "подождать"),
    ("Bad Request: something new", 400, "код ошибки 400"),
])
def test_telegram_refusals_reach_the_owner_in_russian(desc, code, word):
    text = bw.error_ru({"ok": False, "error_code": code, "description": desc})
    assert word in text
    assert not re.search(r"[A-Za-z]{4,}", text.replace("Bot API", "").replace("Telegram", "")), text


def test_the_widget_list_shows_templates_without_their_code():
    items = bw.templates_public()
    assert {t["id"] for t in items} == set(bw.TEMPLATES)
    for t in items:
        assert "html" not in t and t["packed"] > 0 and t["fields"]
    json.dumps(items, ensure_ascii=False)
