"""Ротация мест админа в массовом инвайте (жалоба владельца, операция #90):

«если флот большой — не все аккаунты получат админ права... нужно... чтобы
аккаунты которые отработали свой батч — теряли права администратора и вместо
них получали другие аккаунты».

До фикса `_exec_mass_invite` умела выдавать права ДО упора (bulk-промоут) и
«на лету» одному аккаунту без прав — но НИКОГДА их не забирала. У чата
ограниченное число мест админа (Telegram: CHAT_ADMINS_TOO_MUCH); на большом
флоте лимит исчерпывался, и все аккаунты сверх лимита были обречены падать
на `no_rights` весь прогон — вместо того чтобы дождаться места, освобождаемого
отработавшим свой батч соседом.

Три связанных разрыва проверяются здесь:

1. `account_manager.promote_to_admin_ex` обязан различать «упёрлись в лимит
   чата» (admins_too_much — стоит ждать ротацию) от прочих отказов (окончательно
   или обычный сетевой сбой).
2. `_release_admin_seat` (op_worker) обязан вызываться на КАЖДОМ выводе
   аккаунта из круга (`retired.add`) — иначе место остаётся занятым отработавшим
   аккаунтом до самого конца прогона, и ротация не происходит НИКОГДА.
3. Аккаунт, ожидающий admins_too_much, обязан получать ЩЕДРЫЙ бюджет попыток
   (MAX_ADMIN_SEAT_WAIT_ATTEMPTS=30), а не обычный (MAX_PROMOTE_ATTEMPTS=3) —
   ротация места другим аккаунтом может занять много раундов, это не сетевая
   заминка. Здесь был реальный баг в первой версии фикса: ВНЕШНИЙ гейт (до
   звонка в Telegram, когда причина ещё не известна) ограничивал попытки
   обычным бюджетом в 3 — и обрывал ожидание задолго до истечения щедрого
   бюджета, так что 30 попыток по факту никогда не давались.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ── account_manager: различение причины отказа ────────────────────────────

def test_promote_to_admin_ex_detects_admins_too_much():
    src = _read("services/account_manager.py")
    i = src.index("async def promote_to_admin_ex")
    seg = src[i:src.index("\nasync def demote_from_admin", i)]
    assert '"AdminsTooMuch" in _name' in seg
    assert 'return False, "admins_too_much"' in seg


def test_demote_from_admin_exists_and_is_best_effort():
    src = _read("services/account_manager.py")
    i = src.index("async def demote_from_admin")
    seg = src[i:src.index("\n\n\nasync def promote_to_admin(", i)]
    assert "EditAdminRequest" in seg
    assert "invite_users=False" in seg
    assert "return False" in seg, "сбой демоута не должен ронять вызывающего — best-effort"


# ── op_worker: место освобождается при КАЖДОМ выводе из круга ─────────────

def _exec_mass_invite_source() -> str:
    src = _read("services/op_worker.py")
    i = src.index("async def _exec_mass_invite(")
    # следующая функция верхнего уровня после неё
    j = src.index("\nasync def ", i + 20)
    return src[i:j]


def test_every_retirement_site_releases_the_admin_seat():
    seg = _exec_mass_invite_source()
    sites = [m.start() for m in re.finditer(r"retired\.add\(acc_id\)", seg)]
    assert len(sites) >= 5, "ожидали несколько мест вывода аккаунта из круга"
    for pos in sites:
        tail = seg[pos:pos + 400]
        # следующая содержательная строка после retired.add(acc_id) обязана
        # освобождать место — иначе отработавший аккаунт держит админку до
        # конца прогона, и ротации не происходит
        after = tail.split("retired.add(acc_id)", 1)[1]
        next_lines = [ln.strip() for ln in after.splitlines() if ln.strip()][:3]
        assert any(ln.startswith("await _release_admin_seat(acc_id)") for ln in next_lines), (
            f"после retired.add(acc_id) на позиции {pos} нет освобождения места админа: {next_lines}"
        )


def test_admin_seats_tracked_on_both_promote_paths():
    seg = _exec_mass_invite_source()
    # bulk-промоут (до старта прогона) и промоут «на лету» (внутри круга)
    # обязаны ОБА регистрировать захваченное место — иначе _release_admin_seat
    # не найдёт acc_id в _admin_seats и не отправит демоут.
    assert seg.count("_admin_seats.add(") >= 2


def test_upfront_promote_stops_early_once_cap_is_hit():
    """При большом флоте promote-цикл ДО старта не должен долбить join+promote
    на заведомо обречённые аккаунты после первого admins_too_much — это
    дорого (сотни лишних сетевых вызовов) и бессмысленно: лимит чата один на
    всех, а не персональный."""
    seg = _exec_mass_invite_source()
    i = seg.index("_admin_cap_hit = False")
    loop = seg[i:i + 1100]
    assert "if _admin_cap_hit:" in loop and "break" in loop


def test_release_admin_seat_is_best_effort_and_scoped_to_holders():
    seg = _exec_mass_invite_source()
    i = seg.index("async def _release_admin_seat")
    body = seg[i:seg.index("\n\n    # ── Очередь-планировщик", i)]
    assert "if acc_id not in _admin_seats" in body, (
        "нельзя пытаться демоутнуть аккаунт, который место не держал"
    )
    assert "demote_from_admin" in body
    assert "_admin_seats.discard(acc_id)" in body


# ── бюджет ожидания ротации: главный найденный баг ─────────────────────────

def test_outer_retry_gate_uses_the_wide_seat_wait_budget():
    """Регресс на реальный баг первой версии фикса: внешний гейт (СРАЗУ после
    `if res.get("no_rights"):`, до звонка в Telegram — причина отказа ещё не
    известна) обязан пропускать до щедрого бюджета ожидания места
    (MAX_ADMIN_SEAT_WAIT_ATTEMPTS=30), а не обычного (MAX_PROMOTE_ATTEMPTS=3).

    С обычным бюджетом здесь аккаунт, реально ждущий освобождения места
    другим (admins_too_much), обрывался бы на 3-й попытке — не дождавшись
    ротации, которая может занять много раундов — хотя код ниже вычисляет
    для этой причины именно 30-попыточный _retry_ok, который так и не
    успевал сработать.
    """
    seg = _exec_mass_invite_source()
    i = seg.index('if res.get("no_rights"):')
    gate_block = seg[i:seg.index("_no_rights_on_demand[acc_id] = _no_rights_on_demand.get(acc_id, 0) + 1", i)]
    assert "_irec_promote_retry_allowed(" in gate_block
    assert "_MAX_ADMIN_SEAT_WAIT_ATTEMPTS" in gate_block, (
        "внешний гейт должен допускать попытки до щедрого бюджета ожидания "
        "места — иначе аккаунты, ждущие ротацию, обрываются на обычном "
        "бюджете в 3 попытки задолго до того, как место реально освободится"
    )


def test_on_demand_promote_success_paces_the_promoter():
    """Промоут «на лету» успел уйти в continue БЕЗ единой паузы — в отличие от
    bulk-выдачи выше (там после каждого promote_to_admin_ex стоит sleep
    random.uniform(1.5, 3.0)). Один и тот же ПРОМОУТЕР делает EditAdminRequest
    подряд по разным целям каждый раз, когда кому-то не хватило прав — без
    паузы это машинный темп на одном аккаунте, не только на инвайтере."""
    seg = _exec_mass_invite_source()
    i = seg.index('log.info("mass_invite op=%d acc=%s: права выданы на лету')
    j = seg.index("continue  # НЕ ретайрим", i)
    block = seg[i:j]
    assert "asyncio.sleep(random.uniform(1.5, 3.0))" in block


def test_inner_retry_decision_branches_by_reason():
    seg = _exec_mass_invite_source()
    i = seg.index("_retry_ok = (")
    block = seg[i:i + 500]
    assert '"admins_too_much"' in block
    assert "_MAX_ADMIN_SEAT_WAIT_ATTEMPTS" in block
    # обычные (не «ждём место») причины НЕ должны получать щедрый бюджет
    assert '"not_participant"' in block and '"flood"' in block and '"error"' in block


# ── invite_recovery: сами бюджеты, изолированно от сети/БД ────────────────

def test_seat_wait_budget_is_far_more_patient_than_default():
    from services.invite_recovery import (
        promote_retry_allowed, MAX_PROMOTE_ATTEMPTS, MAX_ADMIN_SEAT_WAIT_ATTEMPTS,
    )

    assert MAX_ADMIN_SEAT_WAIT_ATTEMPTS > MAX_PROMOTE_ATTEMPTS

    # На бюджете по умолчанию (обычный сетевой сбой) ожидание обрывается быстро.
    assert promote_retry_allowed(MAX_PROMOTE_ATTEMPTS - 1) is True
    assert promote_retry_allowed(MAX_PROMOTE_ATTEMPTS) is False

    # На щедром бюджете (ждём ротацию места) аккаунт держится в круге куда дольше.
    assert promote_retry_allowed(MAX_PROMOTE_ATTEMPTS, MAX_ADMIN_SEAT_WAIT_ATTEMPTS) is True
    assert promote_retry_allowed(MAX_ADMIN_SEAT_WAIT_ATTEMPTS - 1, MAX_ADMIN_SEAT_WAIT_ATTEMPTS) is True
    assert promote_retry_allowed(MAX_ADMIN_SEAT_WAIT_ATTEMPTS, MAX_ADMIN_SEAT_WAIT_ATTEMPTS) is False
