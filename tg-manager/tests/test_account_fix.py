"""Строка аккаунта чинится из списка, а не только из карточки.

Разрыв: список честно называл диагноз («🔑 Релог», «🛑 На паузе», «🔌 прокси
не отвечает»), но лечения рядом не было — надо было открыть карточку,
развернуть «Действия с аккаунтом» и найти нужную кнопку среди тринадцати. На
флоте в 32 аккаунта это перебор карточек вручную.

Вторая половина контракта — сдержанность: кнопка на каждой строке обесценивает
кнопки на строках, где действительно сломано.
"""
from __future__ import annotations

import pathlib
import re

from services import account_fix as F

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_UI = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
_API = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")


def _row(**over):
    r = {
        "id": 7,
        "acc_status": "ok",
        "is_active": True,
        "has_proxy": True,
        "proxy_alive": True,
        "trust_score": 90,
    }
    r.update(over)
    return r


# ── Сломанное состояние получает лечение ───────────────────────────────────

def test_expired_session_offers_relog():
    a = F.suggest(_row(acc_status="session_expired"))
    assert a["fn"] == "reauthAccount" and a["args"] == [7]


def test_banned_goes_to_rehab():
    assert F.suggest(_row(acc_status="banned"))["fn"] == "openRehab"


def test_spamblock_offers_the_appeal():
    assert F.suggest(_row(acc_status="spamblock"))["fn"] == "appealSpamblock"


def test_quarantined_account_goes_to_rehab():
    """«Активен», но операции его пропускают — без кнопки это выглядело как
    «аккаунт не работает без причины»."""
    a = F.suggest(_row(health_status="quarantine"))
    assert a["fn"] == "openRehab" and a["tone"] == "danger"


def test_dead_proxy_sends_to_the_proxy_picker_not_to_the_account():
    a = F.suggest(_row(proxy_alive=False))
    assert a["fn"] == "openAccProxy" and a["args"] == [7, None]
    assert "прокси" in a["reason"].lower()


def test_at_risk_offers_warmup():
    assert F.suggest(_row(health_status="at_risk"))["fn"] == "openWarmup"


# ── Порядок: сначала то, что блокирует всё остальное ───────────────────────

def test_expired_session_wins_over_a_dead_proxy():
    """Пока Telegram не признаёт ключ, менять IP бессмысленно."""
    a = F.suggest(_row(acc_status="session_expired", proxy_alive=False))
    assert a["id"] == "relog"


def test_ban_wins_over_risk():
    a = F.suggest(_row(acc_status="banned", health_status="at_risk"))
    assert a["id"] == "rehab"


# ── Сдержанность ───────────────────────────────────────────────────────────

def test_healthy_account_gets_no_button():
    assert F.suggest(_row()) is None


def test_account_without_proxy_is_not_treated_as_broken():
    """Работа с адреса хоста — штатный режим; кнопка висела бы на десятках
    строк и обесценила бы остальные."""
    assert F.suggest(_row(has_proxy=False, proxy_alive=None)) is None


def test_unchecked_proxy_is_not_a_dead_proxy():
    """proxy_alive=None — прокси просто не проверяли."""
    assert F.suggest(_row(proxy_alive=None)) is None


def test_cooldown_is_left_to_expire_on_its_own():
    """Сбрасывать кулдаун — обходить предохранитель флуда, а не чинить."""
    assert F.suggest(_row(cooldown_until="2030-01-01T00:00:00Z")) is None


def test_manually_disabled_account_is_not_a_problem_to_fix():
    assert F.suggest(_row(is_active=False)) is None


def test_low_trust_alone_is_not_enough():
    """Просевший trust лечится прогревом на уровне флота; строка молчит, пока
    иммунная система не назвала аккаунт рисковым."""
    assert F.suggest(_row(trust_score=10)) is None


# ── Битые данные не ломают список ──────────────────────────────────────────

def test_garbage_rows_do_not_crash():
    assert F.suggest({}) is None
    assert F.suggest({"id": "не число"}) is None
    assert F.suggest(None) is None


def test_annotate_keeps_the_list_and_marks_only_the_broken():
    rows = [_row(id=1), _row(id=2, acc_status="banned"), {"id": "x"}]
    out = F.annotate(rows)
    assert out is rows and len(out) == 3
    assert "fix" not in out[0] and out[1]["fix"]["id"] == "rehab"


def test_annotate_survives_an_empty_list():
    assert F.annotate([]) == []
    assert F.annotate(None) is None


# ── Ни одной мёртвой кнопки ────────────────────────────────────────────────

def test_every_offered_function_exists_in_the_mini_app():
    states = [
        {"acc_status": "session_expired"}, {"acc_status": "banned"},
        {"acc_status": "spamblock"}, {"health_status": "quarantine"},
        {"proxy_alive": False}, {"health_status": "at_risk"},
    ]
    fns = {F.suggest(_row(**s))["fn"] for s in states}
    missing = [f for f in sorted(fns)
               if not re.search(rf"(async\s+)?function\s+{re.escape(f)}\s*\(", _UI)]
    assert not missing, f"нет таких функций в мини-аппе: {missing}"


# ── Проводка ───────────────────────────────────────────────────────────────

def test_accounts_endpoint_annotates_rows():
    assert "account_fix" in _API and "_afix.annotate(rows)" in _API


def test_annotation_happens_after_the_health_merge():
    """Карантин и риск приходят только из пульса — посчитай раньше, и обе
    самые важные кнопки исчезнут."""
    assert _API.index("get_account_health") < _API.index("_afix.annotate(rows)")


def test_ui_renders_the_button_and_skips_missing_functions():
    assert "_fixBtn" in _UI
    assert "typeof window[f.fn] !== 'function'" in _UI
    assert "event.stopPropagation()" in _UI


def test_button_is_hidden_in_selection_mode():
    """В режиме выбора тап по строке ставит галочку — кнопка мешала бы."""
    assert "ACC_SEL_MODE ? '' : _fixBtn(a.fix)" in _UI
