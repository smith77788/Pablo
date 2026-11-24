"""Экран раздела показывает, что с флотом сейчас, и ведёт к проблеме кнопкой.

Разделы «📱 Аккаунты» и «📡 Каналы и боты» были только описанием. Что три
аккаунта вылетели из сессии, а два попали под ограничения, владелец узнавал,
лишь открыв список аккаунтов и пролистав его. Теперь на экране раздела стоит
сводка («Сейчас: 12 аккаунтов · ⚠️ 2 с ограничениями · 🔑 3 без сессии»), а
первые кнопки ведут туда, где это чинится: «🔑 Войти заново: 3» — в
переавторизатор, «⚠️ С ограничениями: 2» — в здоровье. Пустой флот — кнопка
«➕ Добавить первый аккаунт» / «➕ Подключить бота» первой.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace


class _Pool:
    def __init__(self, row, fetched=None):
        self.row = row
        self.fetched = list(fetched or [])

    async def fetch(self, sql, *a, **k):
        if isinstance(self.row, BaseException):
            raise self.row
        return self.fetched.pop(0) if self.fetched else []

    async def fetchrow(self, *a, **k):
        if isinstance(self.row, BaseException):
            raise self.row
        return self.row


def _open(monkeypatch, key, row, fetched=None):
    from bot.handlers import botmother_menu as bm

    shown: list[tuple[str, object]] = []

    async def fake_edit(cb, text, markup=None):
        shown.append((text, markup))

    async def fake_answer(cb, *a, **k):
        return None

    async def fake_plan(pool, uid):
        return "free"

    monkeypatch.setattr(bm, "_edit", fake_edit)
    monkeypatch.setattr(bm, "safe_answer", fake_answer)
    monkeypatch.setattr(bm, "_get_user_plan", fake_plan)
    cb = SimpleNamespace(from_user=SimpleNamespace(id=99189001))
    asyncio.run(bm.cb_section(cb, bm.BmCb(action="section", sub=key), _Pool(row, fetched)))
    assert len(shown) == 1
    text, markup = shown[0]
    btns = [(b.text, b.callback_data or "") for r in markup.inline_keyboard for b in r]
    return text, btns


def test_accounts_section_names_problems_and_leads_to_the_fix(monkeypatch):
    text, btns = _open(monkeypatch, "accounts",
                       {"total": 12, "paused": 1, "limited": 2, "expired": 3})
    assert "Сейчас: 12 аккаунтов · ⏳ 1 на паузе · ⚠️ 2 с ограничениями · 🔑 3 без сессии" in text
    assert btns[0][0] == "🔑 Войти заново: 3" and btns[0][1].startswith("acc:reauth_list")
    assert btns[1][0] == "⚠️ С ограничениями: 2" and btns[1][1].startswith("hlth:menu")
    # Обычные действия раздела остались.
    assert any(t == "📱 TG-аккаунты" for t, _ in btns)


def test_healthy_fleet_gets_only_the_summary(monkeypatch):
    text, btns = _open(monkeypatch, "accounts",
                       {"total": 1, "paused": 0, "limited": 0, "expired": 0})
    assert "Сейчас: 1 аккаунт</i>" in text
    assert btns[0][0] in ("🌐 Открыть раздел в приложении", "📱 TG-аккаунты")


def test_empty_fleet_offers_to_add_first(monkeypatch):
    text, btns = _open(monkeypatch, "accounts",
                       {"total": 0, "paused": 0, "limited": 0, "expired": 0})
    assert "Аккаунтов пока нет" in text
    assert btns[0][0] == "➕ Добавить первый аккаунт" and btns[0][1].startswith("acc:add")


def test_channels_section_counts_bots_and_channels(monkeypatch):
    text, btns = _open(monkeypatch, "channels", {"bots": 0, "chans": 2})
    assert "Сейчас: 0 ботов · 2 канала" in text
    assert btns[0][0] == "➕ Подключить бота"


def test_database_hiccup_still_shows_the_section(monkeypatch):
    text, btns = _open(monkeypatch, "accounts", RuntimeError("db down"))
    assert "Сейчас:" not in text
    assert any(t == "📱 TG-аккаунты" for t, _ in btns)


def test_messages_section_shows_running_sends_as_buttons(monkeypatch):
    bcs = [{"id": 7, "bot_id": 555, "sent_count": 300, "failed_count": 40,
            "total_users": 1200, "status": "running"}]
    dms = [{"id": 9, "name": "Осенняя акция для старых клиентов", "sent_count": 5,
            "fail_count": 0, "total_targets": 50, "status": "paused"}]
    text, btns = _open(monkeypatch, "messages", None, [bcs, dms])
    assert "Сейчас идёт: 📢 1 рассылка через бота · ⏸ 1 на паузе" in text
    assert btns[0] == ("📢 Рассылка идёт: 340 из 1200", "bc:detail:555:7:")
    assert btns[1][0] == "⏸ «Осенняя акция для стары…» на паузе"
    assert btns[1][1].startswith("dm:detail:9:")


def test_quiet_messages_section_says_so(monkeypatch):
    text, btns = _open(monkeypatch, "messages", None, [[], []])
    assert "Сейчас ничего не отправляется." in text
    assert btns[0][0] in ("🌐 Открыть раздел в приложении", "📢 Рассылка через бота")
