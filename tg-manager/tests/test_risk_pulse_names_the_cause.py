"""Риск-пульс ставил паузу, не называя причину, и советовал не то.

ЧТО ВИДЕЛ ВЛАДЕЛЕЦ. В списке аккаунтов — «🛑 На паузе» и больше ничего; в
карточке аккаунта — «Аккаунт на паузе — массовые операции его пропускают.
Причина: иммунная система зафиксировала риск». Этот текст появлялся ровно
тогда, когда причиной были статус аккаунта, отсутствие сессии или счётчик
подряд падающих действий: экран пересказывал пульс своими двумя-тремя полями
(ограничения, FloodWait, траст) и, не найдя их, писал «зафиксировала риск».
То есть продукт останавливал аккаунт и не говорил, почему.

ЧЕМ СОВЕТ БЫЛ ВРЕДЕН. Дальше шло «Дайте аккаунту отлежаться (3+ дней без
действий) — пульс снимет паузу автоматически». Для ограничений и флудов это
верно. Для мёртвого статуса, отозванной сессии и хронических отказов — нет:
три дня тишины не вернут аккаунт, и он стоит ровно столько, сколько владелец
ждёт. Теперь пульс сам говорит, пройдёт ли это само (`needs_owner`).

ОТДЕЛЬНАЯ НАХОДКА ТОГО ЖЕ КЛАССА. Аккаунт без строки сессии пульс считал
ЗДОРОВЫМ: он не спрашивал про session_str вообще. При этом ни одна дверь
выбора такой аккаунт не берёт, а счётчик «готовых» в мини-аппе и экран флота
его уже не считают. Та же ложь, что была со спам-блоком, только причина проще.
"""
from __future__ import annotations

import asyncio

from services import account_status as acc
from services.infra_memory import get_account_health


class _Pool:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, q, *a):
        return [dict(r) for r in self.rows]


def _row(**kw):
    row = {"id": 1, "phone": "+70000000001", "acc_status": "active",
           "trust_score": 1.0, "has_session": True, "cd_active": False,
           "restrictions": 0, "severe": 0, "floods": 0}
    row.update(kw)
    return row


def _one(**kw):
    out = asyncio.run(get_account_health(_Pool([_row(**kw)]), 42))
    assert out["accounts"], "пульс не вернул аккаунт"
    return out["accounts"][0]


# ── Причина названа всегда, когда есть пауза или риск ───────────────────────

def test_every_paused_account_names_its_cause():
    cases = [
        {"severe": 2},
        {"acc_status": "spamblock"},
        {"acc_status": "deleted"},
        {"has_session": False},
        {"restrictions": 1},
        {"floods": 4},
        {"trust_score": 0.1},
        {"acc_status": "warming"},
    ]
    for kw in cases:
        a = _one(**kw)
        assert a["status"] != "healthy", kw
        assert a["reason"], f"пауза без причины: {kw}"
        assert a["reason_codes"], kw


def test_a_healthy_account_has_no_cause_to_name():
    """Самопроверка пробника: на здоровом аккаунте причин быть не должно."""
    a = _one()
    assert a["status"] == "healthy"
    assert a["reason"] == "" and a["reason_codes"] == []


def test_the_cause_is_written_in_russian():
    for kw in ({"acc_status": "frozen"}, {"has_session": False},
               {"severe": 1}, {"floods": 2}, {"trust_score": 0.2}):
        reason = _one(**kw)["reason"]
        assert reason
        # «FloodWait» и «Telegram» — термины, которые владелец и так читает по
        # всему продукту; остальных английских слов в причине быть не должно.
        plain = reason.replace("FloodWait", "").replace("Telegram", "")
        assert not any(c.isascii() and c.isalpha() for c in plain), reason


def test_a_dead_status_is_named_by_its_russian_label():
    a = _one(acc_status="session_expired")
    assert acc.ru_label("session_expired") in a["reason"], a["reason"]


# ── Аккаунт без сессии больше не «здоров» ───────────────────────────────────

def test_an_account_without_a_session_is_not_healthy():
    a = _one(has_session=False)
    assert a["status"] == "quarantine", (
        "аккаунт без строки сессии показан здоровым, хотя его не берёт ни "
        "одна дверь выбора")
    assert "no_session" in a["reason_codes"]
    assert a["has_session"] is False


def test_the_summary_counts_the_sessionless_account_as_paused():
    out = asyncio.run(get_account_health(
        _Pool([_row(id=1), _row(id=2, has_session=False)]), 42))
    assert out["summary"] == {"healthy": 1, "at_risk": 0,
                              "quarantine": 1, "total": 2}


# ── Совет зависит от того, пройдёт ли это само ──────────────────────────────

def test_time_heals_restrictions_and_floods():
    for kw in ({"severe": 1}, {"restrictions": 2}, {"floods": 5},
               {"trust_score": 0.2}, {"acc_status": "warming"}):
        assert _one(**kw)["needs_owner"] is False, kw


def test_time_does_not_heal_a_dead_session():
    for kw in ({"has_session": False}, {"acc_status": "banned"},
               {"acc_status": "session_expired"}, {"acc_status": "frozen"}):
        assert _one(**kw)["needs_owner"] is True, kw


# ── Экран печатает причину сервера, а не пересказывает её ───────────────────

def test_the_account_screen_prints_the_cause_the_pulse_computed():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1]
            / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert "_h.reason ||" in html, (
        "карточка аккаунта снова пересказывает причину своими полями")
    assert "_h.needs_owner" in html, (
        "совет не различает «пройдёт само» и «нужно действие владельца»")
    assert "a.health_reason" in html, "в списке аккаунтов причина не печатается"


def test_the_api_passes_the_cause_to_the_list():
    from pathlib import Path

    api = (Path(__file__).resolve().parents[1]
           / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    assert 'r["health_reason"]' in api
    assert 'r["health_needs_owner"]' in api


# ── Совет приходит от пульса, а не выдумывается экраном ─────────────────────

def test_every_paused_account_gets_an_actionable_advice():
    cases = [{"severe": 2}, {"acc_status": "spamblock"}, {"acc_status": "banned"},
             {"has_session": False}, {"floods": 4}, {"trust_score": 0.1}]
    for kw in cases:
        advice = _one(**kw)["advice"]
        assert advice, f"пауза без совета: {kw}"
        plain = advice.replace("FloodWait", "").replace("Telegram", "")
        assert not any(c.isascii() and c.isalpha() for c in plain), advice


def test_a_lost_account_is_not_told_to_wait():
    advice = _one(acc_status="banned")["advice"]
    assert "отлежаться" not in advice, advice
    assert "безвозвратно" in advice, advice


def test_a_restricted_account_is_not_told_to_delete_itself():
    advice = _one(acc_status="spamblock")["advice"]
    assert "Удалять аккаунт не нужно" in advice, advice


def test_an_account_without_a_session_is_told_to_reconnect():
    assert "Переподключите сессию" in _one(has_session=False)["advice"]


def test_restrictions_are_told_they_pass_on_their_own():
    assert "отлежаться" in _one(severe=3)["advice"]


def test_a_healthy_account_gets_no_advice():
    assert _one()["advice"] == ""


def test_the_screen_prints_the_advice_the_pulse_computed():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1]
            / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert "_h.advice" in html, "экран снова сам решает, что посоветовать"
