"""Self-promo фоновый постинг: карантин-гейт + spintax-на-цель (класс #7).

_post_to_channels_bg постил с КАЖДОГО привязанного аккаунта БЕЗ проверки
is_account_quarantined и слал идентичный текст во все каналы (когортная
сигнатура). Тест фиксирует: аккаунт под карантином пропускается (не постим),
пропуск честно считается, и текст раскрывается спинтаксом на каждую цель.
"""
from __future__ import annotations

import pytest


class FakePool:
    def __init__(self):
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return "UPDATE 1"


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, uid, text, **kw):
        self.messages.append((uid, text))


@pytest.mark.asyncio
async def test_quarantined_account_skipped_and_spintax_applied(monkeypatch):
    import bot.handlers.self_promo as sp
    import services.infra_memory as infra
    import services.spintax_service as spx

    posted = []

    async def fake_post(**kw):
        posted.append(kw)
        return {"msg_id": 1}

    async def fake_get_acc(pool, acc_id, user_id):
        return {"session_str": f"sess{acc_id}", "id": acc_id}

    async def fake_quar(pool, account_id, **kw):
        return account_id == 2  # аккаунт 2 под карантином

    expand_calls = []

    def fake_expand(template, **kw):
        expand_calls.append(template)
        return f"variant::{template}"

    monkeypatch.setattr(sp.account_manager, "post_to_channel", fake_post)
    monkeypatch.setattr(sp.db, "get_account_for_telethon", fake_get_acc)
    monkeypatch.setattr(infra, "is_account_quarantined", fake_quar)
    monkeypatch.setattr(spx, "expand_template", fake_expand)
    # ускоряем: без реальной паузы 1.5с × N
    async def _nosleep(*a, **k):
        return None
    monkeypatch.setattr(sp.asyncio, "sleep", _nosleep)

    channels = [
        {"acc_id": 1, "channel_id": 100, "access_hash": 0},
        {"acc_id": 2, "channel_id": 200, "access_hash": 0},  # карантин → пропуск
        {"acc_id": 3, "channel_id": 300, "access_hash": 0},
    ]
    bot = FakeBot()
    pool = FakePool()
    await sp._post_to_channels_bg(bot, pool, user_id=42, run_id=7,
                                  template_id=9, channels=channels, content="{Привет|Хай}")

    # аккаунт 2 (карантин) НЕ постил
    posted_channels = {p["channel_id"] for p in posted}
    assert posted_channels == {100, 300}, "карантинный аккаунт не должен постить"
    # spintax раскрыт на каждую отправленную цель
    assert len(expand_calls) == 2
    assert all(p["text"].startswith("variant::") for p in posted)
    # итог честный: sent=2, skipped=1 упомянут пользователю
    assert bot.messages, "должно быть финальное сообщение"
    final = bot.messages[-1][1]
    assert "Опубликовано: <b>2</b>" in final
    assert "Пропущено (карантин): <b>1</b>" in final


def test_global_presence_channel_has_quarantine_gate():
    """op_worker._exec_global_presence_channel обязан звать единый риск-гейт."""
    import inspect
    from services import op_worker
    src = inspect.getsource(op_worker._exec_global_presence_channel)
    assert "is_account_quarantined" in src, "нет единого карантин-гейта в gp-постинге"
