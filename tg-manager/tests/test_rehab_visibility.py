"""Восстановление аккаунтов перестало быть невидимым.

Разрыв. `services/account_rehab` — полноценный работающий модуль: он сутками
вытягивает аккаунты из спам-блока (аппеляция → тихий пассивный прогрев →
перепроверка с растущим бэк-оффом) и переводит безнадёжные в фазу `stuck`
с пометкой «нужен ручной разбор». В продукте от него не было НИЧЕГО:

  • ни экрана — при том что подпись раздела «Управление аккаунтами» прямо
    обещала «восстановление»;
  • ни уведомления — вернувшийся в строй аккаунт человек успевал списать и
    заменить, а «переданный на ручной разбор» вставал в очередь, о которой
    никто не знал;
  • ни ручного управления — бэк-офф перепроверок доходит до 72 часов, и
    ускорить его было нельзя даже зная, что блок уже снят;
  • `rehab_overview` не вызывался ни из одного места кода.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
from datetime import datetime, timedelta, timezone

from services import account_rehab as RH

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_API_SRC = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
_MAIN = (_ROOT / "main.py").read_text(encoding="utf-8")
_UI = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _func_src(src: str, name: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"функция {name} не найдена")


# ── Описание фазы понятно человеку ─────────────────────────────────────────

def test_every_phase_is_explained_not_just_named():
    for phase in ("appeal", "warming", "recheck", "freed", "stuck", "gone", "off"):
        d = RH.describe({"phase": phase})
        assert d["label"] and d["label"] != phase
        assert d["hint"], f"фаза {phase} без объяснения"


def test_waiting_time_until_the_next_step_is_shown():
    d = RH.describe({"phase": "recheck",
                     "next_action_at": _NOW + timedelta(hours=6)}, _NOW)
    assert d["next_in_seconds"] == 6 * 3600
    assert d["active"] is True


def test_overdue_step_does_not_show_negative_time():
    d = RH.describe({"phase": "warming",
                     "next_action_at": _NOW - timedelta(hours=2)}, _NOW)
    assert d["next_in_seconds"] == 0


def test_finished_phases_have_no_countdown():
    for phase in ("freed", "stuck", "gone", "off"):
        d = RH.describe({"phase": phase, "next_action_at": _NOW + timedelta(days=1)}, _NOW)
        assert d["next_in_seconds"] is None
        assert d["active"] is False


def test_only_stuck_asks_for_a_human():
    assert RH.describe({"phase": "stuck"})["attention"] is True
    for phase in ("appeal", "warming", "recheck", "freed", "gone", "off"):
        assert RH.describe({"phase": phase})["attention"] is False


def test_restart_offered_only_where_automation_gave_up():
    assert RH.describe({"phase": "stuck"})["restartable"] is True
    assert RH.describe({"phase": "off"})["restartable"] is True
    assert RH.describe({"phase": "warming"})["restartable"] is False


def test_describe_survives_a_row_from_an_older_schema():
    """Строка без next_action_at и без note не должна ронять экран."""
    d = RH.describe({"phase": "appeal"})
    assert d["next_in_seconds"] is None and d["note"] == ""


def test_naive_timestamp_is_treated_as_utc():
    d = RH.describe({"phase": "recheck",
                     "next_action_at": datetime(2026, 6, 1, 14, 0)}, _NOW)
    assert d["next_in_seconds"] == 2 * 3600


# ── Итог восстановления доходит до владельца ───────────────────────────────

def test_recovery_alert_tells_the_account_is_back():
    txt = RH.build_terminal_alert("Иван", "freed", 3)
    assert "Иван" in txt and "восстановлен" in txt.lower()


def test_giving_up_alert_says_what_to_do_next():
    txt = RH.build_terminal_alert("Иван", "stuck", 8)
    assert "8" in txt
    assert "замен" in txt.lower()


class _Pool:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    async def fetch(self, q, *a):
        self.calls.append((" ".join(q.split()), a))
        return self.rows

    async def execute(self, q, *a):
        self.calls.append((" ".join(q.split()), a))

    async def fetchrow(self, q, *a):
        self.calls.append((" ".join(q.split()), a))
        return self.rows[0] if self.rows else None


def _notify(rows):
    sent = []
    pool = _Pool(rows)
    import database.db as _db

    orig = _db.notify_if_enabled

    async def _fake(p, bot, uid, pref, text, **kw):
        sent.append((uid, text, kw.get("dedup_key")))

    _db.notify_if_enabled = _fake
    try:
        n = asyncio.run(RH.notify_terminal(pool, object()))
    finally:
        _db.notify_if_enabled = orig
    return n, sent, pool


def test_owner_hears_about_both_outcomes():
    rows = [
        {"acc_id": 1, "owner_id": 7, "phase": "freed", "attempts": 2,
         "phone": "+700", "first_name": "Аня"},
        {"acc_id": 2, "owner_id": 7, "phase": "stuck", "attempts": 8,
         "phone": "+701", "first_name": None},
    ]
    n, sent, _p = _notify(rows)
    assert n == 2
    assert "Аня" in sent[0][1]
    assert "+701" in sent[1][1]


def test_each_outcome_is_announced_once():
    rows = [{"acc_id": 1, "owner_id": 7, "phase": "freed", "attempts": 1,
             "phone": "+700", "first_name": None}]
    _n, _sent, pool = _notify(rows)
    assert any("notified_phase=$2" in q for q, _a in pool.calls)
    q, _a = pool.calls[0]
    assert "COALESCE(r.notified_phase,'') <> r.phase" in q


def test_a_broken_notification_does_not_stop_the_cycle():
    class _Boom(_Pool):
        async def fetch(self, q, *a):
            raise RuntimeError("нет связи")

    assert asyncio.run(RH.notify_terminal(_Boom(), object())) == 0


# ── Ручное управление ──────────────────────────────────────────────────────

def test_user_can_skip_the_backoff():
    pool = _Pool([{"acc_id": 5}])
    assert asyncio.run(RH.force_now(pool, 7, 5)) is True
    q, args = pool.calls[0]
    assert "next_action_at=now()" in q
    # Ускорять можно только то, что реально идёт.
    assert "phase IN ('appeal','warming','recheck')" in q
    assert args == (5, 7)


def test_skipping_the_backoff_is_owner_scoped():
    pool = _Pool([{"acc_id": 5}])
    asyncio.run(RH.force_now(pool, 7, 5))
    q, _args = pool.calls[0]
    assert "owner_id=$2" in q


def test_turning_rehab_off_and_back_on():
    pool = _Pool([{"acc_id": 5}])
    assert asyncio.run(RH.set_enabled(pool, 7, 5, False)) is True
    q, args = pool.calls[0]
    assert args[2] == "off"

    pool2 = _Pool([{"acc_id": 5}])
    asyncio.run(RH.set_enabled(pool2, 7, 5, True))
    q2, args2 = pool2.calls[0]
    assert args2[2] == "appeal"
    # Второй шанс — значит с чистого листа, иначе он мгновенно снова «исчерпан».
    assert "attempts = CASE WHEN $3='appeal' THEN 0" in q2
    # Забаненный аккаунт ('gone') поднимать нельзя: цикл его не возьмёт, а
    # экран показывал бы «идёт восстановление» на пустом месте.
    assert "phase IN ('stuck','off')" in q2


def test_a_switched_off_account_returns_to_rehab_after_the_block_clears():
    """Иначе выключение, сделанное однажды, молча отменяло бы восстановление
    и при следующей блокировке через месяцы."""
    src = _func_src(
        (_ROOT / "services" / "account_rehab.py").read_text(encoding="utf-8"),
        "_sync_enrollment")
    assert "'appeal','warming','recheck','off'" in src


def test_off_accounts_are_not_picked_up_while_still_blocked():
    src = _func_src(
        (_ROOT / "services" / "account_rehab.py").read_text(encoding="utf-8"),
        "_due_rows")
    assert "phase IN ('appeal','warming','recheck')" in src


def test_manual_actions_fail_soft():
    class _Boom(_Pool):
        async def fetchrow(self, q, *a):
            raise RuntimeError("база недоступна")

    assert asyncio.run(RH.force_now(_Boom(), 1, 1)) is False
    assert asyncio.run(RH.set_enabled(_Boom(), 1, 1, True)) is False


# ── Проводка ───────────────────────────────────────────────────────────────

def test_overview_is_no_longer_an_orphan():
    src = _func_src(_API_SRC, "rehab_overview_ep")
    assert "rehab_overview" in src and "list_for_owner" in src
    assert 'app.router.add_get("/api/miniapp/rehab"' in _API_SRC
    assert 'app.router.add_post("/api/miniapp/rehab/{acc_id}/{action}"' in _API_SRC


def test_actions_endpoint_rejects_unknown_verbs():
    src = _func_src(_API_SRC, "rehab_action")
    for verb in ("now", "off", "restart"):
        assert f'"{verb}"' in src
    assert "Неизвестное действие" in src


def test_the_promised_recovery_screen_now_exists():
    assert 'id="s-rehab"' in _UI
    assert "openRehab()" in _UI
    assert "rehabAct" in _UI


def test_destructive_rehab_actions_ask_first():
    assert "askConfirm" in _UI.split("async function rehabAct")[1][:600]


def test_account_card_shows_the_recovery_state():
    """Карточка ограниченного аккаунта была тупиком: статус «ограничен» и ни
    слова о том, что система его уже вытаскивает."""
    src = _func_src(_API_SRC, "account_detail")
    assert "account_rehab_state" in src
    assert '"rehab": rehab' in src
    assert "d.rehab" in _UI and "rehabHtml" in _UI


def test_loop_can_announce_outcomes():
    assert "notify_terminal" in (_ROOT / "services" / "account_rehab.py").read_text(encoding="utf-8")
    assert "run_rehab_loop, pool, bot" in _MAIN


def test_schema_and_self_heal_add_the_notice_column():
    sql = (_ROOT / "schema_v197_rehab_notice.sql").read_text(encoding="utf-8")
    assert "notified_phase" in sql and "IF NOT EXISTS" in sql
    assert "ADD COLUMN IF NOT EXISTS notified_phase" in _MAIN
