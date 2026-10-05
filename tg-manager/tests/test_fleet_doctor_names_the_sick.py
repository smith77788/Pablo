"""Диагностика флота: вердикт ведёт к лечению, а воронка — к больным.

Экран говорил «Готовы к инвайту: 0» и «Ни у одного аккаунта нет сессии» —
и на этом заканчивался. Какие именно аккаунты отсеялись, узнать было нечем:
ни одна строка не нажималась. Вердикт предлагал «нажмите „🚀 Поднять флот"»,
а кнопки на этом экране не было — её искали во вкладке «Аккаунты».

Отдельно: последние ошибки операций не вели в саму операцию и не говорили,
когда случились, хотя время в запросе уже бралось и выбрасывалось.
"""
from __future__ import annotations

import os
import re

from services import fleet_doctor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
API = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()


def _fn(name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", HTML)
    assert m, f"функция {name} не найдена"
    i = HTML.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(HTML)):
        if HTML[j] == "{":
            depth += 1
        elif HTML[j] == "}":
            depth -= 1
            if depth == 0:
                return HTML[i:j + 1]
    raise AssertionError(name)


def _rows():
    """Флот со всеми болезнями разом — по одному аккаунту на шаг воронки."""
    return [
        {"id": 1, "is_active": False, "has_session": True, "acc_status": "active",
         "cd_active": False, "trust_score": 1.0},
        {"id": 2, "is_active": True, "has_session": False, "acc_status": "active",
         "cd_active": False, "trust_score": 1.0},
        {"id": 3, "is_active": True, "has_session": True, "acc_status": "banned",
         "cd_active": False, "trust_score": 1.0},
        {"id": 4, "is_active": True, "has_session": True, "acc_status": "active",
         "cd_active": True, "trust_score": 1.0},
        {"id": 5, "is_active": True, "has_session": True, "acc_status": "active",
         "cd_active": False, "trust_score": 0.10},
        {"id": 6, "is_active": True, "has_session": True, "acc_status": "active",
         "cd_active": False, "trust_score": 0.40},
        {"id": 7, "is_active": True, "has_session": True, "acc_status": "active",
         "cd_active": False, "trust_score": 0.90},
    ]


def test_blockers_match_the_funnel_counts():
    """Паритет: кого воронка не досчиталась, того список и называет.

    Разойдись эти два места — экран показал бы «отсеялось 30» и пустой список.
    """
    rows = _rows()
    f = fleet_doctor.compute_funnel(rows)
    pairs = [("active", "total", "active"), ("with_session", "active", "with_session"),
             ("alive", "with_session", "alive"), ("not_cooldown", "alive", "not_cooldown"),
             ("operable_join", "not_cooldown", "operable_join"),
             ("operable_invite", "not_cooldown", "operable_invite")]
    for step, before, after in pairs:
        lost = f[before] - f[after]
        got = len(fleet_doctor.step_blockers(rows, step))
        assert got == lost, f"шаг {step}: воронка потеряла {lost}, список назвал {got}"


def test_each_blocker_says_what_is_wrong_in_russian():
    rows = _rows()
    for step in fleet_doctor.FUNNEL_STEPS:
        for b in fleet_doctor.step_blockers(rows, step):
            r = b.get("reason", "")
            assert r, f"{step}: аккаунт без причины"
            assert re.search(r"[а-яё]", r), f"{step}: причина не по-русски: {r!r}"
    # Коды статусов в подписи не показываются.
    alive = fleet_doctor.step_blockers(rows, "alive")
    assert alive and alive[0]["reason"] == "забанен", alive


def test_unknown_step_is_rejected():
    assert fleet_doctor.step_blockers(_rows(), "ничего") == []
    assert fleet_doctor.step_blockers(_rows(), "") == []


def test_route_exists_and_is_owner_scoped():
    assert '"/api/miniapp/fleet/diagnose/blocked", fleet_diagnose_blocked' in API
    m = re.search(r"\n    async def fleet_diagnose_blocked\(request", API)
    assert m
    nxt = API.find("\n    async def ", m.end())
    h = API[m.start():nxt]
    assert "WHERE owner_id=$1" in h, "чужие аккаунты"
    assert "fleet_doctor.FUNNEL_STEPS" in h, "шаг из запроса не проверяется"


def test_verdict_offers_the_fix_on_this_screen():
    """Вердикт звал нажать кнопку, которой на экране не было."""
    assert "const FD_FIX" in HTML
    body = HTML[HTML.index("const FD_FIX"):HTML.index("const FD_STEP_TITLE")]
    for key in ("no_accounts", "all_inactive", "no_session", "all_dead",
                "all_cooldown", "low_trust", "transport_fail", "auth_dup"):
        assert key in body, f"вердикту {key} нечего предложить"
    # Каждый вердикт движка имеет свою кнопку.
    src = open(os.path.join(ROOT, "services", "fleet_doctor.py"), encoding="utf-8").read()
    keys = set(re.findall(r'"key": "([a-z_]+)"', src)) - {"ok", "error"}
    missing = [k for k in keys if k not in body]
    assert not missing, f"вердикты без кнопки: {missing}"
    assert 'onclick="${fix.fn}"' in _fn("fleetDoctorLoad")


def test_funnel_rows_open_the_accounts_that_dropped():
    f = _fn("fleetDoctorLoad")
    assert "fdBlocked('" in f, "шаг воронки никуда не ведёт"
    assert "отсеялось" in f, "сколько потеряно на шаге — не показано"
    assert "openAccount(" in _fn("fdBlocked"), "из списка больных нельзя открыть аккаунт"


def test_recent_errors_open_the_operation_and_say_when():
    f = _fn("fleetDoctorLoad")
    assert "openOpDetail(" in f, "ошибка операции никуда не ведёт"
    assert "ago(e.finished_at)" in f, "непонятно, вчерашняя ошибка или сейчасная"
    src = open(os.path.join(ROOT, "services", "fleet_doctor.py"), encoding="utf-8").read()
    assert '"id": r["id"]' in src, "движок не отдаёт id операции"
    assert '"finished_at"' in src


def test_busy_accounts_card_does_not_pretend_to_lead_somewhere():
    """Среза «занят операцией» в списке аккаунтов нет — похожий подставлять нельзя."""
    f = _fn("fleetDoctorLoad")
    i = f.index("Занято акк.")
    # Границы — по разметке самой плитки, а не окном фиксированной длины:
    # сдвинься код на строку, и проверка молча перестала бы что-либо ловить.
    start = f.rindex('<div class="kpi-card', 0, i)
    chunk = f[start:i]
    assert "kpi-val" in chunk, chunk        # положительный якорь: это та плитка
    assert "onclick" not in chunk, chunk


def test_verdicts_are_russian():
    """«Trust всех аккаунтов», «is_active=FALSE» — владелец это не читает."""
    src = open(os.path.join(ROOT, "services", "fleet_doctor.py"), encoding="utf-8").read()
    for m in re.finditer(r'"text": ("(?:[^"\\]|\\.)*"(?:\s*\n\s*"(?:[^"\\]|\\.)*")*)', src):
        text = m.group(1)
        bad = re.findall(r"[A-Za-z_]{4,}", text)
        # Имена собственные и код ошибки Telegram переводить нечем и незачем.
        allow = {"AUTH_KEY_DUPLICATED", "IPv", "IPv6", "CF", "Telegram", "Infragram"}
        bad = [w for w in bad if w not in allow]
        assert not bad, f"английское слово в вердикте: {bad} — {text[:80]}"
