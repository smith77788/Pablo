"""Сторож прокси: мёртвый прокси не должен выглядеть как сломанные аккаунты.

Разрыв: фоновой проверки прокси не было ВООБЩЕ — user_proxies.is_alive
обновлялся только по ручному нажатию «проверить». Прокси, умерший молча
(кончился трафик, провайдер снял адрес, сменился пароль), оставался в базе
живым, а отбор аккаунтов смотрит на is_active — ручной флаг, а не на реальную
живость. Аккаунты на нём продолжали выбираться в каждую операцию, каждое
подключение падало по сети, и снаружи это выглядело как «аккаунты сломались»:
человек шёл чинить самое дорогое, что у него есть, вместо замены прокси.
"""
from __future__ import annotations

import asyncio
import pathlib

from services.proxy_watchdog import (
    FAIL_STREAK_TO_ALERT,
    build_alert,
    check_once,
    decide_transition,
)

_ROOT = pathlib.Path(__file__).resolve().parent.parent


# ── Решение по результату проверки ────────────────────────────────────────────

def test_alive_resets_the_streak():
    d = decide_transition(True, False, streak=5, notified=False, assigned=3)
    assert d["is_alive"] is True and d["streak"] == 0 and d["notify"] is False


def test_recovery_clears_the_notice_flag():
    """Ожил — о следующей смерти нужно предупредить снова."""
    d = decide_transition(True, False, streak=9, notified=True, assigned=3)
    assert d["clear_notice"] is True


def test_single_failure_does_not_alert():
    """Публичные прокси регулярно моргают: тревога с первой неудачи — шум."""
    d = decide_transition(False, True, streak=0, notified=False, assigned=5)
    assert d["is_alive"] is False and d["streak"] == 1 and d["notify"] is False


def test_alerts_after_a_sustained_streak():
    d = decide_transition(False, False, streak=FAIL_STREAK_TO_ALERT - 1,
                          notified=False, assigned=5)
    assert d["notify"] is True and d["streak"] == FAIL_STREAK_TO_ALERT


def test_does_not_alert_twice():
    d = decide_transition(False, False, streak=10, notified=True, assigned=5)
    assert d["notify"] is False


def test_silent_when_no_accounts_depend_on_it():
    """Смерть запасного прокси никому не мешает — дёргать человека незачем."""
    d = decide_transition(False, False, streak=10, notified=False, assigned=0)
    assert d["notify"] is False and d["is_alive"] is False


def test_streak_survives_none():
    d = decide_transition(False, None, streak=None, notified=False, assigned=1)
    assert d["streak"] == 1


# ── Текст уведомления ─────────────────────────────────────────────────────────

def test_alert_names_the_cost_in_accounts():
    """Иначе человек пойдёт чинить аккаунты вместо замены прокси."""
    msg = build_alert("Прокси-1", 7, 3)
    assert "7" in msg and "Прокси-1" in msg
    assert "ЦЕЛЫ" in msg or "целы" in msg.lower()


# ── Обход ─────────────────────────────────────────────────────────────────────

class _Pool:
    def __init__(self, rows):
        self._rows = rows
        self.updates = []

    async def fetch(self, q, *a):
        return self._rows

    async def execute(self, q, *a):
        self.updates.append(a)
        return "UPDATE 1"


def _row(**kw):
    base = {"id": 1, "owner_id": 10, "proxy_url": "socks5://u:p@1.2.3.4:1080",
            "label": None, "is_alive": True, "streak": FAIL_STREAK_TO_ALERT - 1,
            "dead_notified_at": None, "assigned": 4}
    base.update(kw)
    return base


def _run(pool, monkeypatch, alive):
    import services.proxy_watchdog as W

    async def _fake(url, timeout_s=10.0):
        return {"alive": alive, "latency_ms": 12 if alive else None}

    monkeypatch.setattr(W, "check_proxy_alive", _fake)
    sent = []

    async def _fake_notify(pool_, bot_, owner_id, proxy_id, text):
        sent.append((owner_id, text))
        return True

    monkeypatch.setattr(W, "_notify", _fake_notify)
    res = asyncio.run(W.check_once(pool, object()))
    return res, sent


def test_dead_proxy_is_reported_once_with_account_count(monkeypatch):
    pool = _Pool([_row()])
    res, sent = _run(pool, monkeypatch, alive=False)
    assert res["dead"] == 1 and res["alerted"] == 1
    assert sent and "4" in sent[0][1]


def test_credentials_never_appear_in_the_alert(monkeypatch):
    """Полный адрес прокси содержит логин и пароль."""
    pool = _Pool([_row(label=None, proxy_url="socks5://secretuser:secretpass@1.2.3.4:1080")])
    _res, sent = _run(pool, monkeypatch, alive=False)
    assert sent
    assert "secretpass" not in sent[0][1] and "secretuser" not in sent[0][1]


def test_alive_proxy_is_not_reported(monkeypatch):
    pool = _Pool([_row()])
    res, sent = _run(pool, monkeypatch, alive=True)
    assert res["dead"] == 0 and res["alerted"] == 0 and sent == []


def test_state_is_persisted_even_without_alert(monkeypatch):
    pool = _Pool([_row(assigned=0)])
    _res, sent = _run(pool, monkeypatch, alive=False)
    assert pool.updates, "живость обязана записываться и без уведомления"
    assert sent == []


def test_no_proxies_is_a_noop():
    pool = _Pool([])
    res = asyncio.run(check_once(pool, object()))
    assert res == {"checked": 0, "dead": 0, "alerted": 0}


def test_one_failing_check_does_not_abort_the_sweep(monkeypatch):
    """Попытка сломать: проверка одного прокси падает исключением."""
    import services.proxy_watchdog as W

    calls = {"n": 0}

    async def _flaky(url, timeout_s=10.0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("сеть отвалилась")
        return {"alive": True, "latency_ms": 5}

    monkeypatch.setattr(W, "check_proxy_alive", _flaky)
    pool = _Pool([_row(id=1), _row(id=2)])
    res = asyncio.run(W.check_once(pool, object()))
    assert res["checked"] == 1, "второй прокси обязан быть проверен"


# ── Подключение ───────────────────────────────────────────────────────────────

def test_watchdog_is_started_in_main():
    src = (_ROOT / "main.py").read_text(encoding="utf-8")
    assert 'proxy_watchdog.run' in src and '"proxy_watchdog"' in src


def test_schema_adds_the_notice_column():
    sql = (_ROOT / "schema_v195_proxy_dead_notice.sql").read_text(encoding="utf-8")
    assert "dead_notified_at" in sql and "ADD COLUMN IF NOT EXISTS" in sql


def test_sweep_is_bounded_in_concurrency():
    """Обход не должен открывать сотни соединений разом."""
    import services.proxy_watchdog as W
    assert 1 <= W.CONCURRENCY <= 32
    src = (_ROOT / "services" / "proxy_watchdog.py").read_text(encoding="utf-8")
    assert "Semaphore" in src


# ── Глушение уведомлений: один мёртвый прокси не должен прятать остальные ────


def test_each_dead_proxy_gets_its_own_notification_slot(monkeypatch):
    """Падает обычно весь пул сразу — и владелец должен узнать про каждый.

    notify_if_enabled глушит повторы по тройке (владелец, тип, ключ) раз в
    минуту и молча отбрасывает остальное. Без ключа все мёртвые прокси
    владельца делили ОДИН слот (и делили его ещё и с сообщениями движка
    операций): доходило сообщение про один прокси, а про остальные владелец не
    узнавал уже никогда — dead_notified_at им проставлен, и сторож считает их
    отработанными.
    """
    import services.proxy_watchdog as W

    from database import db as _db

    keys = []

    async def _spy(pool_, bot_, uid, pref, text, reply_markup=None, *, dedup_key=None):
        keys.append(dedup_key)

    monkeypatch.setattr(_db, "notify_if_enabled", _spy)

    asyncio.run(W._notify(None, None, 777, 11, "мёртв"))
    asyncio.run(W._notify(None, None, 777, 12, "мёртв"))

    assert all(k for k in keys), "уведомление о мёртвом прокси идёт без ключа"
    assert len(set(keys)) == 2, (
        "два разных прокси делят один слот — про второй владелец не узнает"
    )
    assert all("11" in k or "12" in k for k in keys), (
        "ключ обязан различать прокси, а не только род события"
    )
