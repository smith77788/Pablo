"""Хранилище диалогов в онбординге: шаг «Быстрого старта» в каждом пути цели,
окно подключения в мини-аппе и кнопка в /start.

Хранилище — одна из главных фишек продукта, а в онбординге его не было вовсе:
подключить можно было только с экрана Хранилища или командой /vault. Шаг
рекомендуемый: это не путь к первому результату, и активацию он не держит.
Показывается всем одинаково: подключать бота к личным чатам теперь можно и без
Premium (решение владельца 21.11.2026), поэтому текст Premium не требует.
"""
from __future__ import annotations

import asyncio
import os
import re

import pytest

from services import onboarding

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _ids(r):
    return [s["id"] for s in r["steps"]]


def _step(r, sid):
    return next(s for s in r["steps"] if s["id"] == sid)


@pytest.mark.parametrize("goal,after", [
    (None, "accounts"), ("outreach", "accounts"), ("crm", "accounts"), ("sell_bot", "bot"),
])
def test_vault_step_is_in_every_goal_path_right_after_the_first_step(goal, after):
    r = onboarding.build_checklist(accounts=0, accounts_alive=0, plan_paid=False,
                                   goal=goal, vault_state="never")
    ids = _ids(r)
    assert "vault" in ids
    assert ids.index("vault") == ids.index(after) + 1
    st = _step(r, "vault")
    assert st["done"] is False
    assert st["optional"] is True and st["recommended"] is True
    assert st["action"] == {"kind": "vault_connect"}
    assert st["cta"] == "Подключить"
    # Польза названа словами; Premium не требуем — он больше не нужен.
    assert "удалил" in st["hint"] and "Premium" not in st["hint"]


def test_vault_step_does_not_hold_activation_or_focus():
    """Рекомендуемый шаг не становится «следующим» и не мешает результату."""
    r = onboarding.build_checklist(accounts=1, accounts_alive=1, plan_paid=True,
                                   user_ops=1, vault_state="never",
                                   first_result={"id": 5, "op_type": "x", "label": "Прогрев", "ok": 1})
    assert r["activated"] is True
    r = onboarding.build_checklist(accounts=1, accounts_alive=1, plan_paid=True,
                                   vault_state="never")
    assert r["next_step"] == "first_op"


def test_connected_vault_is_done_and_counted():
    off = onboarding.build_checklist(accounts=0, plan_paid=False, vault_state="never")
    on = onboarding.build_checklist(accounts=0, plan_paid=False, vault_state="on")
    assert _step(on, "vault")["done"] is True
    assert "подключено" in _step(on, "vault")["title"]
    assert on["done"] == off["done"] + 1 and on["total"] == off["total"]


def test_disabled_vault_asks_to_reconnect():
    r = onboarding.build_checklist(accounts=0, plan_paid=False, vault_state="disabled")
    st = _step(r, "vault")
    assert st["done"] is False
    assert "переподключить" in st["title"].lower()
    assert st["cta"] == "Переподключить"
    assert st["action"] == {"kind": "vault_connect"}


def test_unknown_vault_state_hides_the_step_instead_of_lying():
    for state in (None, "garbage"):
        r = onboarding.build_checklist(accounts=0, plan_paid=False, vault_state=state)
        assert "vault" not in _ids(r)


class _Pool:
    def __init__(self, row=None, exc=None):
        self.row, self.exc, self.sql = row, exc, None

    async def fetchrow(self, sql, *args):
        self.sql = sql
        if self.exc:
            raise self.exc
        return self.row


@pytest.mark.parametrize("row,expected", [
    ({"on_": None, "n": 0}, "never"),
    ({"on_": True, "n": 2}, "on"),
    ({"on_": False, "n": 1}, "disabled"),
])
def test_load_vault_state_reads_business_connections(row, expected):
    pool = _Pool(row=row)
    assert asyncio.run(onboarding.load_vault_state(pool, 7)) == expected
    assert "business_connections" in pool.sql and "owner_id=$1" in pool.sql


def test_load_vault_state_fails_soft():
    pool = _Pool(exc=RuntimeError("relation does not exist"))
    assert asyncio.run(onboarding.load_vault_state(pool, 7)) is None


def test_load_state_passes_vault_state_to_the_checklist():
    src = _read("services/onboarding.py")
    body = src[src.index("async def load_state("):src.index("async def record_activation(")]
    assert "load_vault_state(pool, uid)" in body
    assert '"vault_state": vault_st' in body


def test_vault_step_is_titled_in_the_funnel_report():
    assert "vault" in onboarding._STEP_TITLES


# ── Мини-апп ────────────────────────────────────────────────────────────────

def test_miniapp_step_opens_the_connect_sheet():
    html = _read("mini_app/index.html")
    assert re.search(r"vault_connect:\s*'openVaultConnect'", html), \
        "шаг vault_connect должен открывать окно подключения"
    assert '<script src="screens/vault_connect.js"></script>' in html
    # Ссылка #vault_connect из бота и подсказки ведут туда же.
    assert "if (k==='vault_connect') return openVaultConnect();" in html
    # Рекомендуемый шаг подписан «рекомендуем», а не «необязательно».
    assert "s.recommended" in html


def test_connect_sheet_copies_bot_and_rechecks_status():
    js = _read("mini_app/screens/vault_connect.js")
    for fn in ("async function openVaultConnect(", "function closeVaultConnect(",
               "async function vcCheck(", "function vcCopyBot("):
        assert fn in js, fn
    assert "/api/miniapp/vault/status" in js
    assert "copyToClipboard('@' + _VC_BOT" in js
    # Вернулся из настроек Telegram — статус проверяется сам.
    assert "visibilitychange" in js
    # Показывается всем одинаково: Premium не проверяем и не требуем.
    assert "is_premium" not in js and "Premium" not in js


# ── Бот ──────────────────────────────────────────────────────────────────────

def test_start_offers_vault_button_while_not_connected():
    src = _read("bot/handlers/start.py")
    assert 'VaultCb(a="how")' in src
    assert 'st["id"] == "vault"' in src
    assert "рекомендуем" in src


def test_vault_how_callback_shows_the_vault_screen():
    src = _read("bot/handlers/business_vault.py")
    assert 'if a == "how":' in src
    assert "await send_vault_screen(cq.message, pool, uid)" in src
    # /vault и кнопка — один экран, а не две копии текста.
    assert "await send_vault_screen(message, pool, uid)" in src
    assert "CopyTextButton(text=handle)" in src


# ── Напоминания в личку ─────────────────────────────────────────────────────

def test_vault_nudge_is_started_and_bounded():
    """Ровно два напоминания, только новичкам, через тумблер «Советы»;
    поведение на живой базе — test_vault_nudge_postgres.py."""
    from services import vault_nudge as vn
    assert len(vn.TEXTS) == vn.MAX_NUDGES == 2
    assert vn.FIRST_AFTER_DAYS < vn.SECOND_AFTER_DAYS
    assert vn.NEW_USER_WINDOW_DAYS <= 30
    src = _read("services/vault_nudge.py")
    assert '"advice"' in src and "notify_if_enabled" in src
    # Claim счётчика стоит ДО отправки.
    body = src[src.index("async def check_once("):]
    assert body.index("_claim(") < body.index("notify_if_enabled(")
    assert 'vault_nudge.run, pool, bot' in _read("main.py")
    assert "#vault_connect" in src
