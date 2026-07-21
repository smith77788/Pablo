"""Anti-detection: аккаунт-рассылки текста уважают риск-пульс (is_account_quarantined).

Жёсткий гейт: массовая операция по реальным аккаунтам обязана уважать
`is_account_quarantined` ПЕРЕД действием. Публикатор/инвайт/join/leave это делали, а
4 аккаунт-рассылки текста — нет (постили/слали ЛС даже с флагнутых аккаунтов = быстрый
бан). Фикс:
  • multi-account (bulk_dm_adhoc, bulk_post_to_channel): фильтруем аккаунты в карантине
    (fail-open — если все в карантине, работаем всеми, чтобы не обнулить операцию);
  • single-account (group_announce, bulk_post_chans): если аккаунт в карантине — отказ
    с понятной причиной (защита от бана, снимется автоматически).
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from services import op_worker


# ── single-account: функциональный тест раннего отказа ──────────────────────────

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


async def _fake_row(*a, **k):
    return {"id": 5, "session_str": "x", "first_name": "A", "phone": "+1"}


@pytest.mark.parametrize("fn,params", [
    (op_worker._exec_group_announce, {"acc_id": 5, "text": "hi"}),
    (op_worker._exec_bulk_post_chans, {"acc_id": 5, "channel_ids": [1], "text": "hi"}),
])
def test_single_account_refuses_when_quarantined(monkeypatch, fn, params):
    monkeypatch.setattr(op_worker, "_safe_fetchrow", _fake_row)

    async def _quar(pool, aid):
        return True
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _quar)

    res = _run(fn(None, None, 1, 1, params))
    assert res["status"] == "failed"
    assert "риск-пульс" in res["reason"].lower(), res


def test_single_account_proceeds_when_not_quarantined(monkeypatch):
    # не в карантине → НЕ отказываем на этапе риск-пульса (доходим до работы с диалогами).
    monkeypatch.setattr(op_worker, "_safe_fetchrow", _fake_row)

    async def _not_quar(pool, aid):
        return False
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _not_quar)

    calls = {"dialogs": 0}

    async def _dialogs(*a, **k):
        calls["dialogs"] += 1
        return []  # нет групп → done, но риск-пульс НЕ помешал
    from services import account_manager
    monkeypatch.setattr(account_manager, "get_dialogs", _dialogs)

    res = _run(op_worker._exec_group_announce(None, None, 1, 1, {"acc_id": 5, "text": "hi"}))
    assert calls["dialogs"] == 1, "риск-пульс не должен блокировать здоровый аккаунт"
    assert "риск-пульс" not in str(res.get("reason", "")).lower()


# ── multi-account: проверка наличия фильтра (fail-open) ─────────────────────────

def test_multi_account_filters_quarantined():
    for fn in (op_worker._exec_bulk_dm_adhoc, op_worker._exec_bulk_post_to_channel):
        src = inspect.getsource(fn)
        assert "is_account_quarantined" in src, f"{fn.__name__} должен уважать карантин"
        # fail-open: фильтр применяется только если что-то осталось (_kept)
        assert "_kept" in src and "if _kept and len(_kept)" in src, \
            f"{fn.__name__} должен быть fail-open (пустой результат не обнуляет)"
