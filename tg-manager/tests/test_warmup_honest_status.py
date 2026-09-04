"""Честный статус прогрева: план больше не врёт, что он работает.

Разрывы, которые проверяются здесь.

1. Движок прогрева молча пропускает день в шести случаях (аккаунт занят
   операцией, выключен, забанен, в спам-блоке, сессия протухла, нет сессии).
   План при этом оставался `active`, день не рос — и экран неделями показывал
   «🟢 Активен · День 3/21» на аккаунте, который умер на второй день.

2. Паузу, которую движок ставит САМ при бане/спам-блоке/длинном FloodWait,
   было не отличить от паузы, поставленной человеком. Пользователь видел
   «⏸ Пауза», жал «▶ Возобновить» и снова гнал действия по флагнутому
   аккаунту — то есть добивал его своими руками.

3. О самостоятельной остановке не сообщалось вообще.

4. `account_warmup_log` пишется с первого дня существования прогрева и НИ РАЗУ
   не показывался пользователю: на вопрос «он вообще греется?» ответить было
   нечем.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
from datetime import datetime, timedelta, timezone

from services import warmup_status as WS

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_API_SRC = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
_WARMER_SRC = (_ROOT / "services" / "account_warmer.py").read_text(encoding="utf-8")
_UI = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _func_src(src: str, name: str) -> str:
    """Точный исходник функции по границам AST.

    Именно так, а не окном фиксированной длины: окно молча «отключается»,
    когда код сдвигается, и проверка перестаёт что-либо проверять.
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"функция {name} не найдена")


def _plan(**kw):
    base = {"status": "active", "current_day": 3, "target_days": 21,
            "last_action_at": _NOW - timedelta(hours=5), "started_at": _NOW - timedelta(days=3)}
    base.update(kw)
    return base


# ── 1. Молчаливый простой теперь виден ─────────────────────────────────────

def test_healthy_plan_reads_as_running():
    h = WS.classify(_plan(), _NOW)
    assert h["state"] == "running"
    assert h["attention"] is False


def test_plan_that_stopped_moving_is_called_out():
    """Главный разрыв: план числится активным, а день не двигается неделю."""
    h = WS.classify(_plan(last_action_at=_NOW - timedelta(days=7)), _NOW)
    assert h["state"] == "stalled"
    assert h["attention"] is True
    assert h["hint"]


def test_banned_account_is_not_shown_as_working():
    h = WS.classify(_plan(last_skip_reason="banned"), _NOW)
    assert h["state"] == "stalled"
    assert h["attention"] is True
    assert "забанен" in h["hint"].lower() or "заблокирован" in h["hint"].lower()


def test_transient_skip_does_not_raise_a_false_alarm():
    """Аккаунт занят операцией — это норма и пройдёт само: тревожить незачем,
    но сказать об этом честно нужно."""
    h = WS.classify(_plan(last_skip_reason="busy"), _NOW)
    assert h["state"] == "running"
    assert h["attention"] is False
    assert "занят" in h["label"].lower()


def test_plan_that_never_started_eventually_counts_as_stalled():
    h = WS.classify(_plan(last_action_at=None, started_at=_NOW - timedelta(days=4)), _NOW)
    assert h["state"] == "stalled"


def test_fresh_plan_without_actions_is_not_yet_an_alarm():
    h = WS.classify(_plan(last_action_at=None, started_at=_NOW - timedelta(hours=2)), _NOW)
    assert h["state"] == "running"
    assert h["attention"] is False


def test_local_night_delay_does_not_trigger_a_false_stall():
    """Цикл ходит раз в 20 ч и откладывает план на локальную ночь аккаунта
    (до ~8 ч). 28 часов простоя — штатное поведение, не поломка."""
    h = WS.classify(_plan(last_action_at=_NOW - timedelta(hours=28)), _NOW)
    assert h["state"] == "running"


# ── 2. Пауза движка отличается от паузы человека ───────────────────────────

def test_user_pause_stays_an_ordinary_pause():
    h = WS.classify(_plan(status="paused", pause_reason="user"), _NOW)
    assert h["state"] == "paused"
    assert h["resumable"] is True
    assert h["attention"] is False


def test_ban_pause_is_not_resumable():
    h = WS.classify(_plan(status="paused", pause_reason="banned"), _NOW)
    assert h["state"] == "blocked"
    assert h["resumable"] is False
    assert h["attention"] is True


def test_restriction_pause_is_resumable_but_flagged():
    """Спам-блок снимается сам (реабилитацией) — возобновлять можно, но
    человек должен понимать, чего он ждёт."""
    h = WS.classify(_plan(status="paused", pause_reason="restricted"), _NOW)
    assert h["state"] == "blocked"
    assert h["resumable"] is True
    assert h["attention"] is True


def test_legacy_pause_without_reason_is_treated_as_the_users_own():
    """Планы, поставленные на паузу до появления причины, не должны выглядеть
    как аварийные."""
    h = WS.classify(_plan(status="paused", pause_reason=None), _NOW)
    assert h["state"] == "paused"
    assert h["resumable"] is True


def test_finished_plans_are_quiet():
    for st in ("completed", "cancelled"):
        h = WS.classify(_plan(status=st), _NOW)
        assert h["attention"] is False


# ── Возобновление больше не добивает аккаунт ───────────────────────────────

def test_resume_refused_for_banned_account():
    ok, why = WS.can_resume("banned", "banned", True)
    assert ok is False and why


def test_resume_refused_while_account_is_restricted():
    ok, why = WS.can_resume("restricted", "spamblock", True)
    assert ok is False
    assert "спам" in why.lower()


def test_resume_refused_for_switched_off_account():
    ok, why = WS.can_resume("user", "active", False)
    assert ok is False
    assert "включ" in why.lower()


def test_resume_allowed_for_a_healthy_account():
    ok, why = WS.can_resume("user", "active", True)
    assert ok is True and why == ""


def test_resume_allowed_after_flood_wait_passed():
    assert WS.can_resume("flood", "active", True)[0] is True


def test_alert_names_the_account_and_the_day():
    txt = WS.build_pause_alert("Иван", "banned", 4, 21, "UserBannedInChannel")
    assert "Иван" in txt and "4" in txt and "21" in txt


# ── 3. Движок действительно записывает причины ─────────────────────────────

class _RecPool:
    def __init__(self, rows=None):
        self.calls = []
        self._rows = rows or []

    async def execute(self, q, *a):
        self.calls.append((" ".join(q.split()), a))

    async def fetch(self, q, *a):
        self.calls.append((" ".join(q.split()), a))
        return self._rows


def test_skip_reason_is_persisted():
    from services import account_warmer as AW

    pool = _RecPool()
    asyncio.run(AW._note_skip(pool, 7, "busy"))
    q, args = pool.calls[0]
    assert "last_skip_reason" in q and args == (7, "busy")


def test_engine_pause_records_why():
    from services import account_warmer as AW

    pool = _RecPool()
    asyncio.run(AW._pause_plan(pool, 7, "banned", "UserDeactivatedBan"))
    q, args = pool.calls[0]
    assert "pause_reason" in q and "status='paused'" in q
    assert args[1] == "banned"
    # Метку «уже сообщили» сбрасываем — иначе о новой остановке промолчали бы.
    assert "pause_notified_at=NULL" in q


def test_failed_skip_write_does_not_break_the_warmup_day():
    """Запись причины — вспомогательная. Если база моргнула, день прогрева
    должен продолжаться, а не падать."""
    from services import account_warmer as AW

    class _Boom:
        async def execute(self, q, *a):
            raise RuntimeError("нет связи с базой")

    asyncio.run(AW._note_skip(_Boom(), 1, "busy"))
    asyncio.run(AW._pause_plan(_Boom(), 1, "banned", "x"))


def test_every_silent_skip_path_records_a_reason():
    """Каждый ранний выход из дневного прогрева обязан оставлять след.

    Считаем по исходнику самой функции (границы — AST, не окно фиксированной
    длины): сколько возвратов «пропускаем день», столько и отметок причины.
    """
    src = _func_src(_WARMER_SRC, "_run_daily_warmup_impl")
    skips = src.count("return _skip_result")
    assert skips >= 4, "пути пропуска исчезли — проверку надо пересмотреть"
    assert src.count("_note_skip(") >= skips + 2  # +2 ранних возврата до _skip_result


def test_engine_pauses_carry_reasons_not_bare_status_writes():
    src = _func_src(_WARMER_SRC, "_run_daily_warmup_impl")
    assert "SET status='paused'" not in src, (
        "пауза без причины вернулась — она снова неотличима от паузы человека")
    for reason in ("banned", "restricted", "flood"):
        assert f'"{reason}"' in src


# ── Уведомление о самостоятельной остановке ────────────────────────────────

def _notify_run(rows):
    from services import account_warmer as AW

    sent = []

    class _Pool(_RecPool):
        async def fetch(self, q, *a):
            return rows

    pool = _Pool()

    import database.db as _db

    orig = _db.notify_if_enabled

    async def _fake(pool_, bot, uid, pref, text, **kw):
        sent.append((uid, pref, text, kw.get("dedup_key")))

    _db.notify_if_enabled = _fake
    try:
        n = asyncio.run(AW.notify_paused_plans(pool, object()))
    finally:
        _db.notify_if_enabled = orig
    return n, sent, pool


def test_owner_is_told_when_the_engine_stops_a_plan():
    rows = [{"id": 5, "owner_id": 42, "pause_reason": "banned",
             "pause_detail": "UserDeactivatedBan", "current_day": 4,
             "target_days": 21, "phone": "+7999", "first_name": "Иван"}]
    n, sent, pool = _notify_run(rows)
    assert n == 1
    uid, pref, text, dedup = sent[0]
    assert uid == 42 and "Иван" in text
    assert dedup and "5" in dedup


def test_the_alert_is_stamped_so_it_is_not_repeated_hourly():
    rows = [{"id": 5, "owner_id": 42, "pause_reason": "restricted",
             "pause_detail": None, "current_day": 2, "target_days": 14,
             "phone": "+7999", "first_name": None}]
    _n, _sent, pool = _notify_run(rows)
    assert any("pause_notified_at=NOW()" in q for q, _a in pool.calls)


def test_nothing_to_report_is_not_an_error():
    n, sent, _p = _notify_run([])
    assert n == 0 and sent == []


# ── 4. Проводка: экран и эндпоинты ─────────────────────────────────────────

def test_overview_returns_honest_state_per_plan():
    src = _func_src(_API_SRC, "warmup_overview")
    assert "warmup_status" in src and "classify" in src
    assert '"attention"' in src


def test_resume_endpoint_refuses_to_revive_a_dead_account():
    src = _func_src(_API_SRC, "warmup_resume_plan")
    assert "can_resume" in src
    assert "409" in src
    # Проверка обязана быть ДО записи, иначе план уже переведён в active.
    assert src.index("can_resume") < src.index("status='active'")


def test_user_pause_marks_itself_as_the_users_own():
    src = _func_src(_API_SRC, "warmup_pause_plan")
    assert "pause_reason='user'" in src


def test_resume_clears_the_old_complaint():
    src = _func_src(_API_SRC, "warmup_resume_plan")
    assert "last_skip_reason=NULL" in src


def test_classify_survives_a_row_without_the_new_columns():
    """Если inline-миграция не прошла, экран откатывается на старый набор полей.
    Разбор такой строки не должен падать — иначе лаг миграции обрушил бы весь
    экран прогрева, как когда-то cf_relay_url обрушил загрузку аккаунта."""
    legacy = {"status": "active", "current_day": 2, "target_days": 14,
              "started_at": _NOW - timedelta(hours=3)}
    h = WS.classify(legacy, _NOW)
    assert h["state"] == "running"


def test_overview_falls_back_when_the_columns_are_missing():
    src = _func_src(_API_SRC, "warmup_overview")
    assert src.count("_PLAN_SQL.format") == 2, (
        "запасной запрос без колонок причин пропал — лаг миграции снова "
        "обрушит весь экран прогрева")


def test_action_journal_endpoint_exists_and_is_owner_scoped():
    src = _func_src(_API_SRC, "warmup_plan_log")
    assert "account_warmup_log" in src
    assert "owner_id=$2" in src   # чужой план не откроется
    assert 'app.router.add_get("/api/miniapp/warmup/{plan_id}/log"' in _API_SRC


def test_creating_a_plan_checks_the_account_on_the_server():
    """Пригодность аккаунта проверялась только в браузере: прямой запрос (или
    устаревший список на экране) заводил план на забаненном аккаунте."""
    src = _func_src(_API_SRC, "warmup_create_plan")
    assert "can_resume" in src and "409" in src
    assert "acc_status" in src


def test_bulk_warmup_does_not_revive_plans_the_engine_stopped():
    """«Прогреть все подходящие» шло в обход защиты: ON CONFLICT возвращал в
    active план, который движок остановил из-за спам-блока."""
    src = _func_src(_API_SRC, "_warmup_bulk_core")
    assert "'spamblock'" in src
    # И снимает старую жалобу, чтобы перезапущенный план не показывал её.
    assert "pause_reason=NULL" in src


def test_warmup_op_does_not_report_success_for_a_stopped_plan():
    """Операция прогрева рапортовала «Прогрев активен» ЛЮБОМУ существующему
    плану — включая отменённый и остановленный движком."""
    src = _func_src(
        (_ROOT / "services" / "op_worker.py").read_text(encoding="utf-8"),
        "_exec_account_warmup")
    assert 'existing["status"]' in src
    assert "status='active'" in src  # неактивный план поднимается, а не выдумывается


def test_restarting_a_plan_clears_the_previous_complaint():
    src = _func_src(_API_SRC, "warmup_create_plan")
    assert "last_skip_reason=NULL" in src


def test_ui_shows_the_real_state_and_opens_the_journal():
    assert "openWarmupLog" in _UI
    assert 'id="s-warmuplog"' in _UI
    assert 'id="warmupWarn"' in _UI
    assert "p.health" in _UI


def test_ui_hides_resume_when_it_would_only_harm():
    assert "h.resumable" in _UI


def test_schema_adds_the_plan_memory():
    sql = (_ROOT / "schema_v196_warmup_health.sql").read_text(encoding="utf-8")
    for col in ("pause_reason", "pause_detail", "paused_at",
                "pause_notified_at", "last_skip_reason", "last_skip_at"):
        assert col in sql
    # Колонки должны появляться и на уже развёрнутой базе.
    assert "IF NOT EXISTS" in sql
    for col in ("pause_reason", "last_skip_reason"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in _API_SRC
