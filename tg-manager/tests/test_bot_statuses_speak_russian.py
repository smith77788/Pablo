"""Экраны бота печатают статусы и режимы по-русски, а не словом из базы.

Было: «Статус: active · Режим: standard» в сессии прогрева, «Статус: ✅ done»
в рассылке, «План уже завершён (cancelled)», «🌿 Standard» в выборе режима,
«[active]» на кнопке аккаунта, «mass_invite [running]» в активах аккаунта.
Ещё: название цепочки в HTML-тексте шло без экранирования — имя с «<» или
«&» роняло ответ Telegram (can't parse entities), и экран не открывался.
"""
import re
from pathlib import Path

from bot.utils.op_names import acc_status_short, run_status

ROOT = Path(__file__).resolve().parents[1] / "bot/handlers"


def _src(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_run_status_words():
    assert run_status("active") == "идёт"
    assert run_status("completed") == "завершена"
    assert run_status("paused") == "на паузе"
    assert run_status("failed") == "завершилась с ошибкой"
    assert run_status("executing") == "выполняется"
    assert run_status("cancelled", masculine=True) == "отменён"
    assert run_status("done", masculine=True) == "завершён"
    for code in ("weird_new", "", None):
        assert not re.search(r"[A-Za-z]", run_status(code))
        assert not re.search(r"[A-Za-z]", run_status(code, masculine=True))


def test_account_short_status():
    assert acc_status_short("frozen") == "🧊 заморожен"
    assert acc_status_short(None) == "✅"
    assert not re.search(r"[A-Za-z]", acc_status_short("unknown_state"))


def test_warmup_modes_are_russian():
    from bot.handlers import account_warmup as aw
    for cfg in aw._SESSION_PLAN_CONFIG.values():
        assert not re.search(r"[A-Za-z]", cfg["label"]), cfg
    for label in aw._PLAN_LABELS.values():
        assert not re.search(r"[A-Za-z]", label), label
    assert aw._plan_name("standard") == "🌿 Обычный"
    src = _src("account_warmup.py")
    for gone in ("Gentle", "Standard", "Intensive", "Aggressive",
                 "Статус: <b>{s['status']}</b>", "Режим: <b>{s['plan_type']}</b>",
                 "[{acc['acc_status']}]"):
        assert gone not in src, gone


def test_raw_statuses_gone_from_screens():
    assert "{bc['status']}{progress_bar}" not in _src("broadcast.py")
    assert "({plan['status']})" not in _src("global_presence.py")
    assert "• {row['status']} —" not in _src("intent_engine.py")
    assert "[{status}]" not in _src("accounts.py")
    assert "· model:" not in _src("admin.py")


def test_funnel_names_are_escaped():
    src = _src("funnels.py")
    assert not re.search(r"\{funnel\['name'\][^}]*\}", src), "имя цепочки без escape"


def test_leftover_english_labels_gone():
    from bot.handlers.account_shield_hub import _ACTION_LABEL
    assert _ACTION_LABEL["ok"] == "✅ Всё в порядке"
    assert "Saved Messages" not in _src("account_warmup.py")
    acc = _src("accounts.py")
    assert "FloodWait / cooldown" not in acc
    assert 'status_title = "FloodWait' not in acc


def test_failed_search_offers_a_new_search():
    src = _src("global_search.py")
    assert "def _again_kb" in src and 'GSearchCb(action="open")' in src
    # Пустой запрос, таймаут, ошибка, отказ и пустой результат — все ведут к
    # новому поиску, а не только в главное меню.
    assert src.count("reply_markup=_again_kb()") >= 5
    assert "Попробуйте /search снова" not in src
