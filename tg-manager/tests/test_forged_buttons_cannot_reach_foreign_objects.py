"""Подделанная кнопка бота не открывает чужие объекты.

callback_data собирает клиент Telegram, значит id в ней — ввод, а не факт.
Эти кнопки брали bot_id / funnel_id / mesh_id из нажатия и не сверяли
владельца — ни на кнопке, ни перед записью:

* память ботов: «профиль пользователя» показывал переписку и факты
  подписчиков ЧУЖОГО бота;
* воронки и автоворонки: свой шаг (со ссылкой) дописывался в чужую воронку,
  и бот жертвы рассылал его подписчикам;
* сеть контента: свой канал дописывался в чужую сеть — посты жертвы
  копировались туда её же аккаунтом;
* теги CRM и диплинки заводились в чужом боте.

Пул ниже ничего не считает «своим»: любой отказ проверки владения — None.
Падает без фикса.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bot.callbacks import (AutoFunnelCb, ContentMeshCb, CrmCb, DeepLinkCb,
                           FunnelCb, MemCb)

ME, VICTIM_BOT, VICTIM_OBJ, VICTIM_USER = 5002, 9901, 88, 123456


class _Pool:
    def __init__(self):
        self.writes: list[str] = []
        self.reads: list[str] = []

    async def fetchval(self, sql, *a):
        self.reads.append(sql)
        return None

    async def fetchrow(self, sql, *a):
        if "INSERT" in sql:
            self.writes.append(sql)
            return {"id": 1}
        self.reads.append(sql)
        return None

    async def fetch(self, sql, *a):
        self.reads.append(sql)
        return []

    async def execute(self, sql, *a):
        self.writes.append(sql)
        return "OK"


class _State:
    def __init__(self, data=None):
        self.data, self.state = dict(data or {}), None

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
        self.alerts: list = []
        self.shown: list = []
        self.message = SimpleNamespace(edit_text=self._edit, answer=self._edit)

    async def answer(self, text=None, show_alert=False, **kw):
        self.alerts.append(text)

    async def _edit(self, *a, **kw):
        self.shown.append(a)


class _Msg:
    def __init__(self, text):
        self.text = text
        self.from_user = SimpleNamespace(id=ME)
        self.sent: list = []

    async def answer(self, *a, **kw):
        self.sent.append(a)

    edit_text = answer


def _run(coro):
    return asyncio.run(coro)


def test_memory_of_foreign_bot_is_not_shown(monkeypatch):
    from bot.handlers import semantic_memory_hub as h
    leaked = []

    async def _hist(*a, **kw):
        leaked.append("history")
        return [{"role": "user", "text": "мой номер +7900…", "created_at": None}]

    async def _facts(*a, **kw):
        leaked.append("facts")
        return [{"fact_key": "phone", "fact_value": "+7900"}]

    monkeypatch.setattr(h.semantic_memory, "get_user_history", _hist)
    monkeypatch.setattr(h.semantic_memory, "get_user_facts", _facts)
    cb = _Cb()
    _run(h.cb_mem_view_user(
        cb, MemCb(action="view_user", bot_id=VICTIM_BOT, user_id=VICTIM_USER), _Pool()))
    assert leaked == [] and not any("+7900" in str(x) for x in cb.shown)

    st = _State({"bot_id": VICTIM_BOT})
    _run(h.msg_mem_search_id(_Msg(str(VICTIM_USER)), st, _Pool()))
    assert leaked == []

    pool = _Pool()
    _run(h.msg_mem_set_days(_Msg("30"), _State({"bot_id": VICTIM_BOT}), pool))
    assert pool.writes == []


def test_step_is_not_added_to_foreign_funnel():
    from bot.handlers import funnels as h
    st = _State()
    _run(h.cb_fn_add_step(_Cb(), FunnelCb(action="add_step", bot_id=VICTIM_BOT,
                                          funnel_id=VICTIM_OBJ, step=1), st, _Pool()))
    assert st.state is None

    pool = _Pool()
    _run(h.msg_fn_step_delay(_Msg("0"), _State({
        "funnel_id": VICTIM_OBJ, "bot_id": VICTIM_BOT, "current_step": 1,
        "step_text": "Скидка: https://evil.example"}), pool))
    assert not [w for w in pool.writes if "funnel_steps" in w]


def test_step_is_not_added_to_foreign_auto_funnel():
    from bot.handlers import auto_funnel_hub as h
    st = _State()
    _run(h.cb_af_add_step(_Cb(), AutoFunnelCb(action="add_step", funnel_id=VICTIM_OBJ),
                          st, _Pool()))
    assert st.state is None

    pool = _Pool()
    _run(h.msg_af_step_button(_Msg("Забрать | https://evil.example"), _State({
        "funnel_id": VICTIM_OBJ, "step_delay": 0, "step_text": "x"}), pool))
    assert not [w for w in pool.writes if "auto_funnel_steps" in w]


def test_target_is_not_added_to_foreign_mesh():
    from bot.handlers import content_mesh_hub as h
    st = _State()
    _run(h.cb_mesh_add_target(_Cb(), ContentMeshCb(action="add_target", mesh_id=VICTIM_OBJ),
                              st, _Pool()))
    assert st.state is None

    pool = _Pool()
    _run(h.msg_mesh_target_channel(_Msg("@thief_channel"), _State({"mesh_id": VICTIM_OBJ}),
                                   pool))
    assert not [w for w in pool.writes if "mesh_targets" in w]


def test_tag_and_deeplink_are_not_created_in_foreign_bot():
    from bot.handlers import crm, deeplinks
    st = _State()
    _run(crm.cb_add_tag_global(_Cb(), CrmCb(action="add_tag_global", bot_id=VICTIM_BOT),
                               st, _Pool()))
    assert st.state is None
    pool = _Pool()
    _run(crm.msg_global_tag_name(_Msg("vip"), _State({"bot_id": VICTIM_BOT}), pool))
    assert pool.writes == []

    st = _State()
    _run(deeplinks.cb_dl_create(_Cb(), DeepLinkCb(action="create", bot_id=VICTIM_BOT),
                                st, _Pool()))
    assert st.state is None
    pool = _Pool()
    _run(deeplinks.msg_dl_param(_Msg("promo1"), _State(
        {"bot_id": VICTIM_BOT, "link_name": "x"}), pool))
    assert pool.writes == []
