"""Публикация в канал по ЧИСЛОВОМУ id не валит операцию на ровном месте.

Реальный прод-баг (скрин владельца, октябрь 2026): операция «Массовая
публикация в канал» (op_type=bulk_post_to_channel) падала с
`'int' object has no attribute 'replace'`, хотя флот активен и без ограничений,
попытки 2/2 исчерпаны. Причём падала ПОСЛЕ цикла публикации — то есть посты
уже уходили в канал, а операция всё равно помечалась «ошибка».

Причина. Мини-апп по контракту шлёт `channel_ref` ЧИСЛОМ (int channel_id —
см. submit в services/mini_app_api.py). В конце исполнителя итоговый текст
собирался через `html.escape(channel_ref)`, а `html.escape` внутри делает
`s.replace("&", "&amp;")` — на int это AttributeError. Строка стоит после
цикла и вне per-account try/except, поэтому исключение всплывало на уровень
операции и рушило весь прогон.

Фикс: `html.escape(str(channel_ref))`. Числовой ref — штатный вход, а не
ошибка вызова.
"""
from __future__ import annotations

import asyncio

import pytest


class _Pool:
    async def fetch(self, query, *args):
        return []

    async def fetchrow(self, query, *args):
        if "SELECT status FROM operation_queue" in query:
            return {"status": "running"}
        return None

    async def fetchval(self, query, *args):
        return 0

    async def execute(self, query, *args):
        return "UPDATE 1"


ACCOUNTS = [{"id": 1, "phone": "+70000000001", "first_name": "Акк",
             "session_str": "s1"}]


def _harness(monkeypatch, posted):
    from services import account_manager, op_worker, resource_selector

    async def _fake_post(session_str, channel_ref, text, **kw):
        # Ровно то, что делает настоящий post_to_channel при успехе.
        posted.append((str(channel_ref), text))
        return {"msg_id": 123}

    async def _select_all_active(pool, owner_id, **kw):
        want = kw.get("include_ids")
        return [dict(a) for a in ACCOUNTS
                if not want or int(a["id"]) in {int(x) for x in want}]

    async def _claim(acc_ids):
        return list(acc_ids)

    async def _release(acc_ids):
        return None

    async def _not_quarantined(pool, acc_id):
        return False

    async def _not_cancelled(pool, op_id):
        return False

    async def _no_completed(pool, op_id):
        return set()

    async def _no_flood_sleep(seconds, where=""):
        return 0.0

    async def _instant(*a, **kw):
        return None

    monkeypatch.setattr(account_manager, "post_to_channel", _fake_post)
    monkeypatch.setattr(resource_selector, "select_all_active", _select_all_active)
    monkeypatch.setattr(op_worker.resource_selector, "select_all_active", _select_all_active)
    monkeypatch.setattr(op_worker, "try_claim_accounts", _claim)
    monkeypatch.setattr(op_worker, "release_accounts", _release)
    monkeypatch.setattr(op_worker, "completed_targets", _no_completed)
    monkeypatch.setattr(op_worker, "_is_cancelled", _not_cancelled)
    monkeypatch.setattr(op_worker, "bounded_flood_sleep", _no_flood_sleep)
    monkeypatch.setattr(op_worker, "_flood_cooldown_left", lambda acc_id: 0.0)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _not_quarantined)
    monkeypatch.setattr(asyncio, "sleep", _instant)
    return op_worker


@pytest.mark.asyncio
async def test_numeric_channel_ref_does_not_crash_the_op(monkeypatch):
    """Главный регресс: int channel_ref, успешная публикация → статус 'done'."""
    posted: list[tuple[str, str]] = []
    op_worker = _harness(monkeypatch, posted)

    # Ровно то, что шлёт мини-апп: channel_ref — ЧИСЛО.
    res = await op_worker._exec_bulk_post_to_channel(
        _Pool(), object(), 262, 555,
        {"account_ids": [1], "channel_ref": 3945320144,
         "text_to_post": "Привет, канал", "bulk_access_hash": 0},
    )

    assert posted == [("3945320144", "Привет, канал")], (
        "пост не ушёл в канал — сломан сам путь публикации"
    )
    assert res["status"] == "done", (
        f"операция провалилась на числовом channel_ref (status={res.get('status')}, "
        f"reason={res.get('reason')!r}) — повтор прод-бага "
        f"'int' object has no attribute 'replace'"
    )
    assert res["ok"] == 1 and res["failed"] == 0
    # В итоговом тексте числовой ref отрисован, а не уронил сборку сводки.
    assert "3945320144" in res["summary"]
