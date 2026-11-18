"""Авто-смена чата, когда сам ЧАТ получил ограничение инвайта.

Просьба владельца (18.11.2026): «это чат получил ограничение, в этот чат больше
нельзя добавлять пользователей — из-за этого ошибка, система этого не понимает и
не меняет чат для приглашения как задано в настройках (брать другой канал/чат,
оформить так же, сделать такой же пост, выдать админку первым нескольким
аккаунтам флота и продолжить инвайтинг)».

Механизм перелива в резерв (оформить резервный канал тем же сетапом, сделать его
админа промоутером, продолжить) в op_worker уже есть — он запускался при
ЗАПОЛНЕНИИ чата (_switch_channel). Не хватало триггера по ОГРАНИЧЕНИЮ: когда
несколько аккаунтов подряд не могут добавить никого при нуле успехов — это про
чат, а не про аккаунты. Решение вынесено в чистую функцию, её и проверяем; сам
пивот — существующий _switch_channel.
"""
from __future__ import annotations

import os

from services import mass_inviter_engine as inv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


# ── Чистое решение «чат ограничен» ───────────────────────────────────────────

def test_two_blocked_accounts_zero_success_means_restricted():
    # Два разных аккаунта не смогли добавить никого, успехов в чате нет → чат.
    assert inv.chat_looks_restricted(chan_ok=0, distinct_blocked_accounts=2) is True


def test_one_blocked_account_is_not_enough():
    # Один аккаунт мог не получить прав по своей причине — это ещё не про чат.
    assert inv.chat_looks_restricted(chan_ok=0, distinct_blocked_accounts=1) is False


def test_any_success_means_chat_is_fine():
    # Есть хоть один успех в чате — значит добавлять в него МОЖНО, дело в аккаунтах.
    assert inv.chat_looks_restricted(chan_ok=5, distinct_blocked_accounts=9) is False


def test_threshold_cannot_be_lowered_below_two():
    # Защита от ложного срабатывания: порог нельзя опустить до одного аккаунта.
    assert inv.chat_looks_restricted(0, 1, min_accounts=1) is False
    assert inv.chat_looks_restricted(0, 1, min_accounts=0) is False
    assert inv.chat_looks_restricted(0, 2, min_accounts=1) is True


def test_higher_threshold_is_respected():
    assert inv.chat_looks_restricted(0, 2, min_accounts=3) is False
    assert inv.chat_looks_restricted(0, 3, min_accounts=3) is True


# ── Проводка в исполнителе: ограничение чата ведёт к пивоту ───────────────────

def test_op_worker_switches_channel_on_chat_restriction():
    src = _src("services/op_worker.py")
    # Счётчик РАЗНЫХ заблокированных аккаунтов по текущему каналу + сброс в пивоте.
    assert "_chan_no_rights_accs" in src
    assert "_chan_no_rights_accs.clear()" in src, "счётчик должен сбрасываться при смене канала"
    # Решение — через чистую функцию движка, а не свой список статусов.
    assert "inv.chat_looks_restricted(" in src
    # Триггерит ТОТ ЖЕ пивот в резерв, что и заполнение (_switch_channel).
    body = src[src.index("_chan_no_rights_accs.add(acc_id)"):]
    body = body[:body.index("ok_n = int(res.get(\"ok\")")]
    assert "_switch_channel(" in body, "ограничение чата должно звать перелив в резерв"
    assert "_ovf.ST_BURNED" in body
    # Без резерва — честная остановка с причиной, видимой владельцу.
    assert "_group_reason =" in body and "чат ограничен" in body


def test_threshold_is_single_source_from_engine():
    src = _src("services/op_worker.py")
    assert "_CHAT_RESTRICTED_MIN_ACCS = inv.CHAT_RESTRICTED_MIN_ACCOUNTS" in src, (
        "порог должен браться из движка, а не дублироваться числом")
