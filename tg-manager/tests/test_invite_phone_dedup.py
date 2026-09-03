"""Регрессия: дедуп телефонов между прогонами + честный пер-номер отчёт.

Раньше телефоны не дедупились («номер→user неизвестен заранее») и повторный
прогон импортировал те же номера заново. Теперь:
  * invite_by_phones различает, какой номер Telegram сопоставил юзеру (по client_id
    из ImportContacts), и возвращает invited_phones + not_found_phones;
  * исполнитель дедупит телефоны по нормализованному номеру против invite_target_log
    и запоминает ТОЛЬКО успешно приглашённые (не «не в Telegram» — их можно позже);
  * «номеров не в Telegram» выносится в честный итог операции.
"""
from __future__ import annotations

import inspect

from services import mass_inviter_engine, op_worker


def test_invite_by_phones_returns_per_phone_breakdown():
    src = inspect.getsource(mass_inviter_engine.invite_by_phones)
    assert '"invited_phones"' in src, "нужен список успешно приглашённых номеров"
    assert '"not_found_phones"' in src, "нужен список не найденных номеров"
    # маппинг по client_id из результата ImportContacts (а не разница длин)
    assert "client_id" in src and "_resolved_phones" in src


def test_executor_dedups_phones_on_load():
    """Телефоны проходят тот же фильтр свежести, что и user_refs.

    Поведение проверяется в tests/test_invite_queue_scheduler.py
    (test_phones_dedup_between_runs); здесь — что фильтр вообще применён к
    телефонам, а не только к username'ам."""
    src = inspect.getsource(op_worker._exec_mass_invite)
    assert "phones = [p for p in phones if _fresh(p)]" in src, (
        "телефоны должны проходить фильтр дедупа/opt-out"
    )


def test_executor_records_only_successful_phones():
    src = inspect.getsource(op_worker._exec_mass_invite)
    assert 'res.get("invited_phones")' in src, (
        "в дедуп-лог должны попадать только успешно приглашённые номера"
    )
    assert 'res.get("not_found_phones")' in src and "_phones_not_found" in src, (
        "не найденные номера должны считаться и показываться в итоге"
    )


def test_not_found_surfaced_in_summary():
    src = inspect.getsource(op_worker._exec_mass_invite)
    assert "Номеров не в Telegram" in src, (
        "счётчик не найденных номеров должен попадать в сводку операции"
    )
