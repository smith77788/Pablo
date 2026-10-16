"""M1/M3/M4 инвайта: честные счётчики и защита промоутера.

  • M1: цель, добавленная промоут-трюком или фолбэк-ссылкой, раньше оставалась и
    в total_fail (как privacy-отказ в основном цикле), и попадала в total_ok —
    двойной счёт: «добавлено+ошибок» превышало аудиторию, а прогресс переваливал
    за 100%. Теперь трюк/фолбэк ПЕРЕВОДЯТ цель из провала в успех (total_fail-=R),
    а done_items повторно не растёт.
  • M3: промоут-трюк = 2 EditAdmin на цель (выдать+снять админку). Сотни подряд
    одним промоутером — прямая дорога к его бану. Трюк за прогон берёт не больше
    _PROMOTE_TRICK_MAX_PER_RUN целей; остаток уходит в фолбэк/следующий прогон.
  • M4: «уже в чате» (UserAlreadyParticipant) раньше считался успехом (ok) и врал
    в «приглашено N». Теперь это отдельная корзина already_in — дедупим, но в ok
    и дневной лимит инвайтов не кладём.
"""
from __future__ import annotations

from services import mass_inviter_engine as mie
from services import op_worker
from tests.test_invite_overflow_channels import chain  # noqa: F401 — фикстура стенда+цепочки
from tests.test_invite_queue_scheduler import (  # noqa: F401 — фикстуры стенда
    _Pool,
    _reset_account_claims,
    _run,
    stand,
)


def _trick_ok_all(*extra_refs):
    async def _trick(sess, acc, group, refs):
        return {"ok": len(refs), "failed": 0, "peer_flood": False, "flood_wait": 0,
                "errors": [], "no_rights": False, "chat_full": False,
                "still_blocked": [], "untried": [], "short_floods": [], "admins_left": []}
    return _trick


async def _promoter_status(sess, acc, group):
    return {"ok": True, "can_promote": True, "channel_id": 100, "participants": 10}


def _all_privacy(group, refs):
    return {"ok": 0, "failed": len(refs), "errors": [], "untried": [],
            "privacy_failed": list(refs)}


# ── M1: нет двойного счёта при спасении трюком ───────────────────────────────

def test_trick_moves_targets_from_fail_to_success_no_double_count(chain, monkeypatch):
    targets = [f"u{i}" for i in range(1, 6)]  # 5 целей, все отбиты приватностью
    chain(_all_privacy)
    monkeypatch.setattr(mie, "channel_admin_status", _promoter_status)
    monkeypatch.setattr(mie, "add_via_promote", _trick_ok_all())

    pool = _Pool()
    res = _run(pool, targets, group="@main", account_ids=[1, 2])

    assert res["failed"] == 0, "цели, спасённые трюком, не должны оставаться в провале"
    assert res["ok"] == len(targets), "все спасённые — в успехах"
    assert pool.done_items <= len(targets), (
        f"прогресс превысил размер аудитории: {pool.done_items} из {len(targets)}")
    # итог «добавлено+ошибок» не должен превышать аудиторию
    assert res["ok"] + res["failed"] <= len(targets)


# ── M3: промоут-трюк ограничен по числу целей за прогон ──────────────────────

def test_promote_trick_capped_per_run_to_protect_promoter(chain, monkeypatch):
    monkeypatch.setattr(op_worker, "_PROMOTE_TRICK_MAX_PER_RUN", 3)
    targets = [f"u{i}" for i in range(1, 11)]  # 10 privacy-blocked
    chain(_all_privacy)
    monkeypatch.setattr(mie, "channel_admin_status", _promoter_status)

    seen: list = []

    async def _trick(sess, acc, group, refs):
        seen.extend(refs)
        return {"ok": len(refs), "failed": 0, "peer_flood": False, "flood_wait": 0,
                "errors": [], "no_rights": False, "chat_full": False,
                "still_blocked": [], "untried": [], "short_floods": [], "admins_left": []}
    monkeypatch.setattr(mie, "add_via_promote", _trick)

    _run(_Pool(), targets, group="@main", account_ids=[1, 2])
    assert len(seen) <= 3, (
        f"трюк обработал {len(seen)} целей за прогон — промоутер перегружен "
        "(каждая цель = 2 EditAdmin)")


# ── M4: «уже в чате» — не новый инвайт ───────────────────────────────────────

def test_already_in_chat_not_counted_as_new_invite(stand):
    targets = [f"u{i}" for i in range(1, 11)]

    def responder(acc_id, refs, dry=False):
        return {"ok": 0, "failed": 0, "errors": [],
                "already_in": len(refs), "added": list(refs), "dead": [], "untried": []}

    stand(responder)
    res = _run(_Pool(), targets)

    assert res["ok"] == 0, "уже состоящие в чате — не новый инвайт, в «добавлено» не идут"
    assert "Уже были в чате" in res["summary"], "итог обязан отдельно показать already_in"


def test_invite_batch_counts_already_in_separately():
    """Контракт движка: already_in — отдельное поле, не внутри ok."""
    import inspect
    src = inspect.getsource(mie.invite_batch)
    assert '"already_in"' in src, "invite_batch должен возвращать already_in"
    # UserAlreadyParticipant больше не инкрементит ok напрямую
    i = src.index("except UserAlreadyParticipantError:")
    seg = src[i:src.index("except ", i + 10)]  # тело ровно этой ветки
    assert "already_in += 1" in seg
    assert "ok += 1" not in seg, "уже-в-чате не должен считаться новым инвайтом (ok)"
