"""Никто не получает приглашение в канал дважды — ни из одной двери.

Дыры, которые были закрыты:
  • один человек дважды в самом списке аудитории (спаршен из двух каналов,
    '@Ivan' и '@ivan') получал два приглашения за один прогон;
  • дедуп читался один раз на старте, а прогон идёт часами: параллельная
    операция в тот же канал за это время звала тех же людей;
  • инвайт из бота (карточка канала, «контакты в канал») журнал не читал и не
    пополнял — массовый инвайт и бот звали одних и тех же людей повторно;
  • при переливе по резерву приглашённый в канал резерва записывался только
    под ключом кампании — отдельная кампания прямо в этот канал звала его снова.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from services import invite_dedup as idd
from services import invite_overflow as ovf
from services import mass_inviter_engine as mie
from services import op_worker
from tests.test_invite_queue_scheduler import (  # noqa: F401 — фикстуры стенда
    _Pool,
    _reset_account_claims,
    _run,
    stand,
)

ROOT = Path(__file__).resolve().parents[1]


def _ok(acc_id, refs, dry=False):
    return {"ok": len(refs), "failed": 0, "errors": [], "untried": []}


def _sent(s) -> list:
    return [r for _, refs in s.calls for r in refs]


def test_duplicates_inside_the_list_are_invited_once(stand):
    s = stand(_ok)
    res = _run(_Pool(), ["@Ivan", "@ivan", "u2", "u2", "@IVAN"])
    assert sorted(_sent(s)) == ["@Ivan", "u2"]
    assert "Повторы в списке убраны: 3" in res["summary"]


class _JournalPool(_Pool):
    """Журнал приглашений, который пополняет «другая операция» по ходу прогона."""

    def __init__(self, invited_meanwhile):
        super().__init__()
        self.invited_meanwhile = set(invited_meanwhile)

    async def fetch(self, q, *a):
        if "FROM invite_target_log" in q and "ANY($3" in q:
            return [{"target": t} for t in a[2] if t in self.invited_meanwhile]
        return []


def test_targets_invited_by_another_op_midrun_are_not_sent(stand):
    s = stand(_ok)
    res = _run(_JournalPool({"u3", "u7"}), [f"u{i}" for i in range(1, 11)])
    assert "u3" not in _sent(s) and "u7" not in _sent(s)
    assert len(_sent(s)) == 8
    assert "пригласила другая операция: 2" in res["summary"]


def test_reinvite_switch_still_allows_repeats_on_purpose(stand):
    """Оператор сам выключил дедуп — сверка по ходу прогона тоже не мешает."""
    s = stand(_ok)
    _run(_JournalPool({"u3"}), ["u1", "u2", "u3"], skip_invited=False)
    assert "u3" in _sent(s)


def test_overflow_channel_invitees_are_recorded_under_the_channel_too(stand, monkeypatch):
    recorded: dict = {}

    async def _rec(pool, owner_id, key, op_id, targets):
        recorded.setdefault(key, set()).update(str(t) for t in targets)
        return True

    monkeypatch.setattr(op_worker, "_record_invited_targets", _rec)

    async def _invite(sess, acc, group, refs, pace_mult=1.0, bulk=None):
        if group == "@main":
            return {"ok": 0, "failed": 0, "untried": list(refs),
                    "errors": ["group error: в чате достигнут лимит участников Telegram"]}
        return {"ok": len(refs), "failed": 0, "errors": [], "untried": []}

    async def _anop(*a, **k):
        return None

    async def _empty(*a, **k):
        return []

    async def _no(*a, **k):
        return False

    async def _load(pool, owner_id, ids):
        return [{"channel_id": 501, "access_hash": 1, "username": "", "title": "R",
                 "acc_id": 1}]

    async def _prep(acc, ch, setup):
        return {"ok": True, "ref": "https://t.me/+res", "done": [], "warnings": []}

    async def _status(*a, **k):
        return {"ok": True, "can_promote": False}

    stand(_ok)
    monkeypatch.setattr(mie, "invite_batch", _invite)
    monkeypatch.setattr(ovf, "chain_rows", _empty)
    monkeypatch.setattr(ovf, "busy_channel_ids", lambda *a, **k: _set())
    monkeypatch.setattr(ovf, "taken_elsewhere", _no)
    monkeypatch.setattr(ovf, "load_reserve", _load)
    monkeypatch.setattr(ovf, "mark", _anop)
    monkeypatch.setattr(ovf, "prepare_channel", _prep)
    monkeypatch.setattr(mie, "channel_admin_status", _status)

    _run(_Pool(), ["u1", "u2"], group="@main", reserve_channels=[501])
    assert recorded.get("@main") == {"u1", "u2"}, "под ключом кампании"
    assert recorded.get("501") == {"u1", "u2"}, "и под id канала резерва"


async def _set_coro():
    return set()


def _set():
    return _set_coro()


# ── общий модуль для всех дверей ──────────────────────────────────────────────

def test_dedup_keys_cover_every_form_of_one_channel():
    keys = idd.dedup_keys(group_ref="https://t.me/MyChan", channel_id=-1001234567890,
                          username="MyChan")
    assert "1234567890" in keys
    assert mie.parse_group_ref("@MyChan") in keys
    assert len(keys) == len(set(keys))


class _LogPool:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, *a):
        return "OK"

    async def fetch(self, q, *a):
        if "invite_target_log" in q:
            return [{"target": t} for k, t in self.rows if k in a[1]]
        return []


@pytest.mark.asyncio
async def test_filter_new_drops_invited_optout_and_repeats(monkeypatch):
    from services import contact_opt_out as coo

    async def _oo(pool, owner_id):
        return {"@blocked"}
    monkeypatch.setattr(coo, "load_opted_out", _oo)
    pool = _LogPool([("1234567890", "@Old"), ("other", "@x")])
    fresh, dup, opt = await idd.filter_new(
        pool, 1, ["1234567890"], ["@old", "@new", "@NEW", "@blocked", "@x"])
    assert fresh == ["@new", "@x"], "@x приглашали в ДРУГОЙ канал — сюда звать можно"
    assert dup == 1 and opt == 1


@pytest.mark.asyncio
async def test_remember_writes_under_every_key(monkeypatch):
    seen = []

    async def _rec(pool, owner_id, key, op_id, targets):
        seen.append((key, sorted(targets)))
        return True
    monkeypatch.setattr(op_worker, "_record_invited_targets", _rec)
    assert await idd.remember(None, 1, ["a", "b"], ["@u"])
    assert seen == [("a", ["@u"]), ("b", ["@u"])]


def test_bot_doors_go_through_the_same_journal():
    """Каждый вызов invite_users_to_channel в боте окружён сверкой и записью."""
    src = (ROOT / "bot" / "handlers" / "channel_ops.py").read_text(encoding="utf-8")
    calls = src.count("_am.invite_users_to_channel(")
    assert calls >= 2
    assert src.count("_idd.filter_new(") >= calls, "дверь бота зовёт без сверки с журналом"
    assert src.count("_idd.settle(") >= calls, "дверь бота не пишет приглашённых"
    am = (ROOT / "services" / "account_manager.py").read_text(encoding="utf-8")
    assert '"invited_list": invited_list' in am, "бот не узнал бы, кого именно пригласили"
