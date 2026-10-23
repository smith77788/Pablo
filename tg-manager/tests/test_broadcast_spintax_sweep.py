"""Anti-detection: spintax НА ЦЕЛЬ во всех аккаунт-рассылках текста.

Паттерн-свип (сиблинги фикса mass_publish): исполнители, которые слали ОДИН и тот
же пользовательский текст многим целям через account_manager (Telethon session_str),
— это палевная сигнатура координации, из-за которой банят аккаунты. Каждая цель
должна получать свой вариант (expand_spintax; без spintax — текст как есть, no-op).

Покрываем 4 исполнителя:
  • _exec_bulk_dm_adhoc      — ЛС многим usernames (самое палевное: PeerFlood/spam)
  • _exec_group_announce     — объявление во все группы одного аккаунта
  • _exec_bulk_post_to_channel — разные аккаунты в ОДИН канал (явная координация)
  • _exec_bulk_post_chans    — один аккаунт в много каналов

Ботовые рассылки (broadcaster/self_promo/aiogram) сюда НЕ входят — там текст шлёт
бот своим подписчикам (легитимный newsletter, не анти-детект аккаунтов).
"""
from __future__ import annotations

import inspect

from services import op_worker


def _src(fn):
    return inspect.getsource(fn)


def test_bulk_dm_adhoc_spintax_per_recipient():
    src = _src(op_worker._exec_bulk_dm_adhoc)
    assert "expand_spintax" in src, "ЛС-рассылка должна применять spintax"
    assert "_msg = _expand_spintax(text)" in src
    # шлётся именно вариант, а не сырой text
    i = src.index("for i, username")
    after = src[i:]
    assert "send_dm(\n                acc[\"session_str\"], username, _msg" in after or \
           ", username, _msg," in after, "send_dm должен слать _msg, а не text"


def test_group_announce_spintax_per_group():
    src = _src(op_worker._exec_group_announce)
    assert "_ann = _expand_spintax(text)" in src
    i = src.index("for idx, grp")
    after = src[i:]
    assert "grp[\"id\"], _ann" in after, "post_to_channel должен слать _ann, а не text"


def test_bulk_post_to_channel_spintax_per_account():
    src = _src(op_worker._exec_bulk_post_to_channel)
    assert "_body = _expand_spintax(text_to_post)" in src
    i = src.index("for idx, acc")
    after = src[i:]
    # текст-аргумент post_to_channel — это _body, а не сырой text_to_post
    assert "_body," in after
    assert "text_to_post,\n                access_hash" not in after, \
        "сырой text_to_post не должен уходить в канал"


def test_bulk_post_chans_spintax_per_channel():
    src = _src(op_worker._exec_bulk_post_chans)
    assert "_body = _expand_spintax(text)" in src
    i = src.index("for idx, ch")
    after = src[i:]
    assert "ch_id, _body," in after, "post_to_channel должен слать _body, а не text"


def test_niche_growth_post_applies_spintax_and_quarantine():
    """Growth Agent постит promo_text в НЕСКОЛЬКО ниш-групп — тот же анти-детект класс:
    свой spintax-вариант на группу + уважение риск-пульса аккаунтов."""
    src = _src(op_worker._exec_niche_growth_post)
    assert "expand_spintax" in src, "Growth Agent должен спинтить promo_text"
    assert "_promo = _expand_spintax(promo_text)" in src
    assert "_filter_quarantined_accounts" in src, "Growth Agent должен уважать риск-пульс"
