"""Заморозку (и спам-блок) автоматика не снимает и не обжалует сама.

Жалоба владельца: «система не понимает, что аккаунты заблокированы/заморожены,
и продолжает брать их в работу». Распознавание ответа @SpamBot чинило только
половину — проверка того же прохода нашла три пути, которыми ограничение
снималось или обходилось уже ПОСЛЕ верного распознавания:

1. «Слабый active». Проверка без @SpamBot видит только «сессия жива», а
   замороженный аккаунт (режим «только чтение») get_me проходит. Монитор сессий
   раз в шесть часов писал этот 'active' поверх 'frozen'/'spamblock' — то есть
   возвращал аккаунт в работу. Ответ «сессия занята, проверка отложена» был
   таким же 'active' без всякой проверки.
2. Автоапелляция. `appeal_spamblock` жала кнопку «This is a mistake». У
   заморозки это вход в анкету с капчей «подтвердите, что вы человек» — её
   проходит только владелец; автоматика бросала бы её на полпути.
3. Реабилитация считала замороженный аккаунт «снятым» (`freed`) и
   продолжала прогревать его как «всё ещё спам-блок».
"""
from __future__ import annotations

import asyncio
import inspect

from services import account_manager, account_rehab, account_status


def _run(c):
    return asyncio.run(c)


# ── 1. слабый active ──────────────────────────────────────────────────────────

def test_unchecked_active_is_never_persisted():
    assert account_manager.should_persist_account_status("active") is True
    assert account_manager.should_persist_account_status(
        "active", restriction_checked=False) is False
    # прочие статусы признак не трогает
    assert account_manager.should_persist_account_status(
        "spamblock", restriction_checked=False) is True


def test_weak_answers_are_marked_as_unchecked():
    src = inspect.getsource(account_manager.check_account_status_full)
    no_spambot = src[src.index("if not check_spambot:"):]
    no_spambot = no_spambot[:no_spambot.index("}")]
    assert '"restriction_checked": False' in no_spambot

    async def _busy():
        return await account_manager.check_account_status_full("x" * 40, {"id": 1})

    orig = account_manager._try_acquire_session
    account_manager._try_acquire_session = lambda *_a, **_k: False
    try:
        res = _run(_busy())
    finally:
        account_manager._try_acquire_session = orig
    assert res["status"] == "active" and res.get("restriction_checked") is False


class _MonPool:
    def __init__(self, acc_status):
        self.row = {"id": 7, "session_str": "s" * 40, "acc_status": acc_status,
                    "session_conflict_at": None, "phone": "+70000000007"}

    async def fetch(self, *a, **k):
        return [self.row]

    async def execute(self, *a, **k):
        return "UPDATE 1"


def _monitor(monkeypatch, acc_status, result):
    from services import op_worker
    writes: list[str] = []

    async def _check(*a, **k):
        return dict(result)

    async def _claim(_id):
        return True

    async def _release(_ids):
        return None

    async def _set(pool, acc_id, new_status, **k):
        writes.append(new_status)
        return True

    monkeypatch.setattr(account_manager, "check_account_status_full", _check)
    monkeypatch.setattr(op_worker, "try_claim_account", _claim)
    monkeypatch.setattr(op_worker, "release_accounts", _release)
    monkeypatch.setattr(account_status, "set_status", _set)
    _run(account_manager._check_all_sessions(_MonPool(acc_status)))
    return writes


def test_session_monitor_does_not_lift_a_freeze(monkeypatch):
    weak = {"status": "active", "reason": "", "restriction_checked": False}
    for restricted in sorted(account_status.RESTRICTED_STATUSES):
        assert _monitor(monkeypatch, restricted, weak) == [], restricted


def test_session_monitor_still_restores_what_it_can_see(monkeypatch):
    # Контроль: проверку не выключили целиком — паузу FloodWait «сессия жива»
    # снимать вправе.
    weak = {"status": "active", "reason": "", "restriction_checked": False}
    assert _monitor(monkeypatch, "cooldown", weak) == ["active"]


# ── 2. автоапелляция ─────────────────────────────────────────────────────────

class _Btn:
    def __init__(self, text):
        self.text = text


class _Msg:
    def __init__(self, text, buttons):
        self.text = text
        self.buttons = [[_Btn(b) for b in buttons]]
        self.clicked: list[str] = []

    async def click(self, text=None, **k):
        self.clicked.append(text)


class _AppealClient:
    def __init__(self, msg):
        self.msg = msg

    async def connect(self):
        return None

    async def is_user_authorized(self):
        return True

    async def get_entity(self, _u):
        return object()

    async def send_message(self, *_a, **_k):
        return None

    async def get_messages(self, *_a, **_k):
        return [self.msg]


FROZEN_TEXT = ("Your account has been frozen for a serious violation of Telegram's "
               "Terms and Conditions. Your account is now in read-only mode.")
BANNED_TEXT = "Your account was blocked for violations of the Telegram Terms of Service."


def _appeal(monkeypatch, text):
    msg = _Msg(text, ["This is a mistake", "I was wrong"])
    monkeypatch.setattr(account_manager, "_make_client", lambda *a, **k: _AppealClient(msg))

    async def _nodisc(*a, **k):
        return None

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(account_manager, "safe_disconnect", _nodisc)
    monkeypatch.setattr(account_manager.asyncio, "sleep", _nosleep)
    res = _run(account_manager.appeal_spamblock("s" * 40, {"id": 1}))
    return res, msg


def test_appeal_never_presses_a_button_on_a_frozen_account(monkeypatch):
    res, msg = _appeal(monkeypatch, FROZEN_TEXT)
    assert msg.clicked == [], f"автоматика нажала {msg.clicked} — это вход в анкету заморозки"
    assert res["status"] == "frozen"


def test_appeal_never_presses_a_button_on_a_banned_account(monkeypatch):
    res, msg = _appeal(monkeypatch, BANNED_TEXT)
    assert msg.clicked == [] and res["status"] == "banned"


def test_appeal_still_presses_on_a_plain_spamblock(monkeypatch):
    # Контроль: обычный спам-блок автоматика по-прежнему обжалует.
    res, msg = _appeal(monkeypatch, "Unfortunately, your account is now limited.")
    assert msg.clicked, "спам-блок перестал обжаловаться — защита сломала основной путь"


# ── 3. реабилитация ──────────────────────────────────────────────────────────

class _RehabPool:
    def __init__(self):
        self.sql: list[tuple[str, tuple]] = []

    async def execute(self, sql, *args):
        self.sql.append((sql, args))
        return "UPDATE 1"

    async def fetchval(self, *a, **k):
        return "spamblock"


def test_enrollment_does_not_call_a_freeze_freed():
    pool = _RehabPool()
    _run(account_rehab._sync_enrollment(pool))
    upd = next(sql for sql, _ in pool.sql if "UPDATE account_rehab_state" in sql)
    assert "= 'frozen' THEN 'stuck'" in upd
    # freed_at не ставится замороженному
    freed = upd[upd.index("freed_at"):]
    assert "<> 'frozen'" in freed


def test_appeal_phase_hands_a_freeze_to_the_owner(monkeypatch):
    calls: list[tuple] = []

    async def _appeal_frozen(*a, **k):
        return {"ok": True, "status": "frozen", "reply": FROZEN_TEXT, "steps": 0}

    async def _set(pool, acc_id, st, **k):
        calls.append(("status", st))
        return True

    async def _adv(pool, acc_id, **k):
        calls.append(("phase", k.get("phase")))

    monkeypatch.setattr(account_manager, "appeal_spamblock", _appeal_frozen)
    monkeypatch.setattr(account_status, "set_status", _set)
    monkeypatch.setattr(account_rehab, "_advance", _adv)
    row = {"acc_id": 5, "phase": "appeal", "session_str": "s" * 40, "owner_id": 9,
           "attempts": 0, "appeal_count": 0, "warm_cycles": 0}
    _run(account_rehab._process(object(), row))
    assert ("status", "frozen") in calls
    assert ("phase", "stuck") in calls, calls


def test_manual_appeal_on_a_freeze_writes_the_truth(monkeypatch):
    calls: list[tuple] = []

    async def _set(pool, acc_id, st, **k):
        calls.append(st)
        return True

    monkeypatch.setattr(account_status, "set_status", _set)
    pool = _RehabPool()
    _run(account_rehab.record_manual_appeal(pool, 9, 5, "frozen"))
    assert calls == ["frozen"]
    upd = [a for sql, a in pool.sql if "account_rehab_state" in sql]
    assert upd and upd[0][2] == "stuck"


def test_frozen_card_does_not_advise_replacing_the_account():
    st = account_rehab.describe({"phase": "stuck", "acc_status": "frozen"})
    assert "апелляц" in st["hint"] and "заменить" not in st["hint"]
    assert st["attention"] is True and st["restartable"] is False
    txt = account_rehab.build_terminal_alert("Иван", "stuck", 3, frozen=True)
    assert "@SpamBot" in txt and "Замените" not in txt
