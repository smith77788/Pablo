"""Массовый инвайт: два разрыва с живого прогона (операция #90, флот 51,
«не ответили (сессия/сеть): 43» без единой зацепки; «Выдана админка
инвайтерам: 48», а реально работали только 8 — часть флота получила права
и тут же пропала из круга).

1. РИСК-ПУЛЬС ПРОВЕРЯЛСЯ ОДИН РАЗ, НА СТАРТЕ. Прогон массового инвайта может
   идти часами (паузы на флуде до 900с, круги, ребаланс каждые 20с) —
   аккаунт, ушедший в карантин ПОСЛЕ старта (например, поймал critical-
   ограничение в ДРУГОЙ операции — это тот же реальный Telegram-аккаунт),
   продолжал бы приглашать до самого конца прогона. Теперь риск-пульс
   перепроверяется живьём той же периодикой, что и честный дележ флота между
   параллельными операциями (_rebalance_claim).

2. МЕСТО АДМИНА ТЕКЛО ПРИ ОТДАЧЕ АККАУНТА. Ротация мест админа (эта же
   сессия, отдельный фикс) освобождает место ТОЛЬКО там, где аккаунт попадает
   в retired.add(acc_id). Но аккаунт, отданный другой параллельной операции
   владельца (_rebalance_claim) или свежекарантинный, просто ИСЧЕЗАЕТ из
   accounts — он никогда не попадёт ни в один retired.add() ЭТОГО прогона, и
   без явного освобождения держал бы место до конца прогона впустую.

3. «НЕ ОТВЕТИЛИ» БЫЛО ЕДИНОЙ КОРЗИНОЙ БЕЗ РАЗБОРА. errors[] от каждого сбоя
   подключения уже лежали в _noconnect_errs — просто никогда не считались по
   типам, только искались на 2 конкретные подстроки для одной подсказки.
   Оператор не мог понять, 43 «мёртвых» аккаунта — это мёртвые сессии (нужно
   переавторизовать) или мёртвые прокси (нужно назначить другой) — и раз за
   раз тратил суточный лимит на нерабочие аккаунты вслепую.
"""
from __future__ import annotations

import re
from pathlib import Path

from services.invite_recovery import (
    classify_noconnect_error, NOCONNECT_DEAD_SESSION, NOCONNECT_AUTH_CONFLICT,
    NOCONNECT_PROXY, NOCONNECT_TIMEOUT, NOCONNECT_BUSY, NOCONNECT_OTHER,
    NOCONNECT_LABELS, NOCONNECT_ADVICE,
)

ROOT = Path(__file__).resolve().parents[1]
WORKER = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


def _exec_body() -> str:
    m = re.search(r"async def _exec_mass_invite\(.*?(?=\nasync def )", WORKER, re.DOTALL)
    assert m
    return m.group(0)


# ── классификатор причин «не ответили» — чистая логика, без сети/БД ────────

def test_classifies_dead_session_reasons():
    for text in [
        "connect: [AUTH_KEY_UNREGISTERED] ...",
        "SESSION_REVOKED",
        "acc=1: Session expired",
        "Authorization key not found",
    ]:
        assert classify_noconnect_error(text) == NOCONNECT_DEAD_SESSION, text


def test_classifies_auth_conflict_as_distinct_from_dead_session():
    assert classify_noconnect_error("AUTH_KEY_DUPLICATED") == NOCONNECT_AUTH_CONFLICT
    assert classify_noconnect_error("connect: two different IP addresses") == NOCONNECT_AUTH_CONFLICT


def test_classifies_proxy_reasons():
    for text in ["connect: Proxy error", "SOCKS5 handshake failed",
                 "ConnectionRefusedError", "Connection reset by peer"]:
        assert classify_noconnect_error(text) == NOCONNECT_PROXY, text


def test_classifies_timeout_reason():
    assert classify_noconnect_error("connect: TimeoutError") == NOCONNECT_TIMEOUT


def test_classifies_busy_reason():
    assert classify_noconnect_error(
        "acc=5: сессия занята другим подключением (ожидание 15с исчерпано)"
    ) == NOCONNECT_BUSY


def test_unknown_text_falls_back_to_other_not_silently_dropped():
    # Пустая строка — единственный законный случай "нет диагностики", реальный
    # текст ошибки обязан попасть в корзину, а не потеряться молча.
    assert classify_noconnect_error("some unrecognised RPC blah") == NOCONNECT_OTHER
    assert classify_noconnect_error("") == NOCONNECT_OTHER


def test_every_category_has_a_label():
    for key in (NOCONNECT_DEAD_SESSION, NOCONNECT_AUTH_CONFLICT, NOCONNECT_PROXY,
                NOCONNECT_TIMEOUT, NOCONNECT_BUSY, NOCONNECT_OTHER):
        assert key in NOCONNECT_LABELS and NOCONNECT_LABELS[key]


def test_actionable_categories_have_distinct_advice():
    # Мёртвая сессия и мёртвый прокси требуют РАЗНЫХ действий оператора — это и
    # есть весь смысл разбивки, не должны схлопываться в один совет.
    assert NOCONNECT_ADVICE[NOCONNECT_DEAD_SESSION] != NOCONNECT_ADVICE[NOCONNECT_PROXY]
    assert "переавторизуйте" in NOCONNECT_ADVICE[NOCONNECT_DEAD_SESSION].lower()
    assert "прокси" in NOCONNECT_ADVICE[NOCONNECT_PROXY].lower()


# ── wiring внутри _exec_mass_invite ─────────────────────────────────────────

def test_report_breaks_down_noconnect_by_reason():
    body = _exec_body()
    assert "classify_noconnect_error" in body
    assert "_noconnect_breakdown" in body
    # разбивка обязана попадать в строку отчёта рядом с «не ответили»
    i = body.index('"⚪ не ответили (сессия/сеть)')
    seg = body[i:i + 300]
    assert "_noconnect_breakdown" in seg


def test_sess_hint_covers_more_than_two_hardcoded_substrings():
    body = _exec_body()
    i = body.index("Подсказка по КРУПНЕЙШЕЙ корзине сбоя подключения")
    seg = body[i:body.index("_method_hdr", i)]
    assert "NOCONNECT_AUTH_CONFLICT" in seg
    assert "NOCONNECT_ADVICE" in seg, "подсказка обязана уметь больше двух захардкоженных подстрок"


def test_quarantine_is_rechecked_live_not_only_at_start():
    body = _exec_body()
    assert "_REQUARANTINE_EVERY_S" in body
    assert "_filter_quarantined_accounts(pool, op_id, accounts)" in body
    # перепроверка обязана жить РЯДОМ с ребалансом (тот же цикл, та же кадансовость)
    i = body.index("_REBALANCE_EVERY_S =")
    j = body.index("_REQUARANTINE_EVERY_S =")
    assert j - i < 1000, "живой риск-пульс должен быть объявлен рядом с ребалансом"


def test_include_risky_operator_is_not_silently_overridden_by_recheck():
    """Владелец осознанно включил рисковых (include_risky) — периодическая
    перепроверка не должна тайком выкидывать их же через несколько минут."""
    body = _exec_body()
    i = body.index("_last_quarantine_check >= _REQUARANTINE_EVERY_S")
    seg = body[max(0, i - 120):i]
    assert "not _include_risky" in seg


def test_rebalance_and_requarantine_release_admin_seat_for_departed_accounts():
    """Аккаунт, отданный другой операции ИЛИ ушедший в карантин посреди прогона,
    исчезает из accounts и никогда не попадёт в retired.add() этого прогона —
    без явного освобождения держал бы место админа впустую до конца прогона."""
    body = _exec_body()
    i = body.index("_before_ids = {int(x[\"id\"]) for x in accounts}")
    seg = body[i:i + 1700]
    assert "_left_ids = _before_ids - _kept_ids" in seg
    assert "await _release_admin_seat(_lid)" in seg
    # порядок: сначала считаем ушедших, потом чистим retired/budget по оставшимся
    assert seg.index("_release_admin_seat(_lid)") < seg.index("retired = {a for a in retired if a in _kept_ids}")
