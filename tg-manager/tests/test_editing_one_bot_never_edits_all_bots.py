"""Правка профиля ОДНОГО бота не уходит на всех ботов.

Жалоба владельца: «выбираю для редактирования один бот, указываю новое
описание, нажимаю запуск — а изменения применяются сразу ко всем ботам».

Причина двойная, и обе половины здесь под регрессом:

1. В `mini_app/index.html` стояли ДВА окна с одним `id="mo-bot-profile"`:
   массовое («Применить к выбранным» → submitBotProfileEdit) и окно одного
   бота («Сохранить» → submitBotProfile). `getElementById` отдаёт первое по
   разметке — массовое. Открыв профиль одного бота, владелец видел массовое
   окно, и запуск уходил в массовую правку. Поля ввода (`bpName`, `bpDesc` …)
   тоже совпадали, так что даже кнопка одиночного окна читала бы чужие поля.
2. Массовая правка вне режима выбора слала `bot_ids: []`, а сервер считал
   пустой список «все боты». Теперь «все» — только явным `all: true`.

Тот же класс (повторяющийся id статической разметки) нашёлся ещё в трёх
местах: форма «Новая папка» брала поле ошибки и кнопку у «Поиска контакта»,
ошибки «Поста во все каналы» уходили в скрытое окно профиля ботов, вторая
плитка «Фабрика ботов» не получала числа. Храповик ниже держит класс закрытым.
"""
from __future__ import annotations

import asyncio
import collections
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "mini_app", "index.html")


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


# ── храповик: id статической разметки уникальны ───────────────────────────────

def _markup_ids(html: str) -> list[str]:
    """id из РАЗМЕТКИ, без JS: внутри <script> id живут в шаблонах-ветках
    (рисуется одна из двух), там повтор законен."""
    markup = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S | re.I)
    return re.findall(r'\bid="([A-Za-z][\w-]*)"', markup)


def test_markup_ids_are_unique():
    c = collections.Counter(_markup_ids(_read(INDEX)))
    dup = sorted(k for k, v in c.items() if v > 1)
    assert not dup, (
        "Повторяющийся id в разметке мини-аппа: getElementById отдаёт ПЕРВЫЙ, "
        "и второй элемент молча не работает (так правка одного бота уходила на "
        f"всех): {dup}")


def test_the_rule_sees_a_duplicate():
    bad = '<div id="x"></div><div id="x"></div><script>a=`<p id="y"></p>`;b=`<p id="y"></p>`</script>'
    ids = _markup_ids(bad)
    assert ids.count("x") == 2 and "y" not in ids


# ── окно одного бота — своё ───────────────────────────────────────────────────

def test_single_bot_modal_has_its_own_ids():
    html = _read(INDEX)
    i = html.find('id="mo-bot-profile-one"')
    assert i > 0, "окно профиля одного бота не найдено под своим id"
    j = html.find('class="mbg"', i + 1)      # до начала следующего окна
    block = html[i:j if j > 0 else None]
    assert 'onclick="submitBotProfile()"' in block
    assert "submitBotProfileEdit" not in block
    js = _read(os.path.join(ROOT, "mini_app", "screens", "bot_detail.js"))
    fn = js[js.index("function openBotProfile("):js.index("async function openBotSubs(")]
    assert "showModal('mo-bot-profile-one')" in fn
    assert "'bpDesc'" not in fn and "'bpoDesc'" in fn


# ── сервер: пустой выбор — не «все боты» ──────────────────────────────────────

class _Pool:
    async def fetchval(self, *a, **k):
        return 3

    async def fetch(self, *a, **k):
        return []

    async def fetchrow(self, *a, **k):
        return None

    async def execute(self, *a, **k):
        return "OK"


def _post(monkeypatch, body):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from services import mini_app_api, mini_app_bots, operation_bus

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: 777)
    submitted: list[dict] = []

    async def _submit(pool, uid, op_type, params, **k):
        submitted.append(params)
        return 1

    monkeypatch.setattr(operation_bus, "submit", _submit)

    async def _go():
        app = web.Application()
        mini_app_bots.setup_routes(app, _Pool())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.post("/api/miniapp/bots/bulk_profile", json=body)
            return r.status, await r.json(), submitted
        finally:
            await client.close()

    return asyncio.run(_go())


def test_empty_selection_is_refused_not_all(monkeypatch):
    status, data, sub = _post(monkeypatch, {"description": "новое", "bot_ids": []})
    assert status == 400 and sub == [], "пустой выбор ушёл в правку всех ботов"


def test_missing_selection_is_refused_not_all(monkeypatch):
    status, data, sub = _post(monkeypatch, {"description": "новое"})
    assert status == 400 and sub == []


def test_selected_bot_only(monkeypatch):
    status, data, sub = _post(monkeypatch, {"description": "новое", "bot_ids": [111]})
    assert status == 200 and sub and sub[0].get("bot_ids") == [111]


def test_all_bots_only_when_asked_explicitly(monkeypatch):
    status, data, sub = _post(monkeypatch, {"description": "новое", "all": True})
    assert status == 200 and sub and "bot_ids" not in sub[0]
