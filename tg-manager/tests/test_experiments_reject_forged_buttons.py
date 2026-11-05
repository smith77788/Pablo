"""A/B тесты: подделанная кнопка не открывает чужой эксперимент.

`exp_id` и `bot_id` приходят из callback_data, а её собирает клиент. «Добавить
вариант» и «Новый эксперимент» владение не проверяли: кнопкой
`exp:add_variant:0:<чужой id>:0` в чужой запущенный тест дописывался свой
вариант, и бот жертвы слал этот текст её подписчикам (ссылки — тоже).
Проверка стоит и на кнопке, и перед записью: состояние FSM живёт дольше
кнопки. Падает без фикса.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bot.callbacks import ExperimentCb
from bot.handlers import experiments as ex

ME, VICTIM_EXP, VICTIM_BOT = 5001, 77, 9900


class _Pool:
    def __init__(self):
        self.writes = []

    async def fetchval(self, sql, *a):
        return None  # ни эксперимент, ни бот не принадлежат ME

    async def fetchrow(self, sql, *a):
        if "INSERT" in sql:
            self.writes.append(sql)
            return {"id": 1}
        return None

    async def fetch(self, sql, *a):
        return []

    async def execute(self, sql, *a):
        self.writes.append(sql)
        return "OK"


class _State:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.state = None

    async def set_state(self, s):
        self.state = s

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.data, self.state = {}, None


class _Cb:
    def __init__(self):
        self.from_user = SimpleNamespace(id=ME)
        self.alerts = []
        self.message = SimpleNamespace(edit_text=self._edit)
        self.edited = []

    async def answer(self, text=None, show_alert=False, **kw):
        self.alerts.append(text)

    async def _edit(self, *a, **kw):
        self.edited.append(a)


class _Msg:
    def __init__(self, text):
        self.text = text
        self.from_user = SimpleNamespace(id=ME)
        self.sent = []

    async def answer(self, *a, **kw):
        self.sent.append(a)


def test_forged_add_variant_button_is_refused():
    cb, st, pool = _Cb(), _State(), _Pool()
    asyncio.run(ex.cb_add_variant(
        cb, ExperimentCb(action="add_variant", bot_id=VICTIM_BOT, exp_id=VICTIM_EXP),
        st, pool))
    assert st.state is None and "exp_id" not in st.data
    assert cb.alerts and "не найден" in (cb.alerts[0] or "")


def test_variant_is_not_written_into_foreign_experiment():
    st = _State({"exp_id": VICTIM_EXP, "bot_id": VICTIM_BOT, "variant_name": "x"})
    pool = _Pool()
    asyncio.run(ex.msg_variant_content(_Msg("Скидка! https://evil.example"), st, pool))
    assert pool.writes == []


def test_experiment_is_not_created_on_foreign_bot(monkeypatch):
    async def _plan(*a, **kw):
        return True

    monkeypatch.setattr(ex, "require_plan", _plan)
    cb, st, pool = _Cb(), _State(), _Pool()
    asyncio.run(ex.cb_exp_create(
        cb, ExperimentCb(action="create", bot_id=VICTIM_BOT), st, pool))
    assert st.state is None

    st2 = _State({"bot_id": VICTIM_BOT})
    asyncio.run(ex.msg_exp_name(_Msg("тест"), st2, pool))
    assert pool.writes == []
