"""Инвайт доходит до цели в максимальном режиме и не теряет аккаунты зря.

Два дефекта одного прогона (380 целей, 203 добавлено, 6 флудов, 2ч07м):

1. **Остановка «флот перегрет» на ровном месте.** Счётчик считал не «флуды
   подряд по времени», а «батчи с флудом, между которыми не было НИ ОДНОГО
   успешного батча», и сбрасывался только при ok>0. К концу прогона в очереди
   остаются цели с закрытой приватностью — батч честно отдаёт ok=0 и серию не
   рвёт. Пять флудов, разбросанных по двум часам (1,6% от объёма), сложились в
   «шторм», которого не было. Особенно дорого это в режиме «один проход»: там
   стоп-кран — единственная защита вместо суточного лимита, то есть ошибка
   измерителя прямо режет заявленный максимум.

   Настоящий шторм — кучность: Telegram смотрит на флот как на группу и бьёт по
   нему в короткий промежуток. Поэтому считаем флуды в скользящем окне времени.

2. **Потеря аккаунтов из-за невыданной админки.** `promote_to_admin` возвращал
   False на все случаи разом, а инвайт давал ровно ОДНУ попытку на аккаунт.
   Любой случайный сбой — таймаут, флуд у промоутера, а чаще всего
   not_participant (Telegram не успел зарегистрировать вступление) — выводил
   рабочий аккаунт из круга до конца операции. Отсюда «выдана админка 35 раз, а
   трое всё равно без прав и выброшены».
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
OP_WORKER = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
ACC_MGR = (ROOT / "services" / "account_manager.py").read_text(encoding="utf-8")


def _func_src(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f"функция {name} не найдена")


# ── стоп-кран по флудам ──────────────────────────────────────────────────────

def test_storm_is_measured_in_a_time_window():
    """Шторм — кучность флудов, а не их сумма за весь прогон."""
    src = _func_src(OP_WORKER, "_exec_mass_invite")
    assert "_flood_window_sec" in src, (
        "стоп-кран не знает про окно времени — значит снова считает «флуды без "
        "успеха между ними» и остановит длинный productive прогон"
    )
    assert "_flood_times" in src, "отметки времени флудов не ведутся"


def test_window_is_configurable_and_sane():
    """Окно настраивается, но не может выродиться в ноль."""
    src = _func_src(OP_WORKER, "_exec_mass_invite")
    assert "INVITE_FLOOD_WINDOW_SEC" in src
    # ast.unparse нормализует кавычки — проверка не должна от них зависеть
    m = re.search(r"max\(60,\s*int\(_os_env\.getenv\(['\"]INVITE_FLOOD_WINDOW_SEC", src)
    assert m, "окно должно иметь нижнюю границу — иначе 0 секунд отключит защиту"


def test_stale_floods_leave_the_window():
    """Старые отметки обязаны выпадать, иначе окно превратится в общий счётчик."""
    src = _func_src(OP_WORKER, "_exec_mass_invite")
    assert re.search(r"_flood_times\[:\]\s*=\s*\[t for t in _flood_times", src), (
        "окно не очищается от старых флудов"
    )


def test_storm_still_stops_a_dead_fleet():
    """Защита не должна исчезнуть: если успехов нет вовсе, а флуды идут —
    продолжать значит жечь аккаунты впустую."""
    src = _func_src(OP_WORKER, "_exec_mass_invite")
    assert "total_ok == 0" in src and "flood_storm = True" in src, (
        "потерян случай «флот не даёт ни одного успеха» — стоп-кран обязан "
        "срабатывать и по нему"
    )


def test_summary_explains_the_real_reason():
    """Пользователь должен видеть «столько флудов за столько минут», а не
    «флудов подряд» — иначе объяснение не сходится с его отчётом."""
    src = _func_src(OP_WORKER, "_exec_mass_invite")
    assert "флудов подряд" not in src, "итог по-прежнему обещает «подряд»"
    assert "Флот перегрет" in src and "_flood_window_sec" in src


# ── выдача прав админа ───────────────────────────────────────────────────────

def test_promote_reports_why_it_failed():
    """Без причины отказа вызывающий не отличит «подожди» от «никогда»."""
    src = _func_src(ACC_MGR, "promote_to_admin_ex")
    for reason in ("not_participant", "no_add_admins", "flood"):
        assert f'"{reason}"' in src, f"причина {reason} не возвращается"
    assert "return (True, '')" in src or 'return True, ""' in src or "(True, '')" in src


def test_old_boolean_contract_is_preserved():
    """Десять существующих вызывающих не должны знать о смене контракта."""
    from services import account_manager

    sig = inspect.signature(account_manager.promote_to_admin)
    assert sig.return_annotation in (bool, "bool"), (
        "обёртка обязана остаться булевой — иначе ломаются все прежние вызывающие"
    )
    assert "promote_to_admin_ex" in _func_src(ACC_MGR, "promote_to_admin")


def test_invite_retries_transient_grant_failures():
    """Временный отказ не должен стоить аккаунта."""
    src = _func_src(OP_WORKER, "_exec_mass_invite")
    # Счётчик попыток живёт в services/invite_recovery (работа параллельного
    # агента) — здесь проверяем, что инвайт им пользуется, а не заводит свой.
    assert "_no_rights_on_demand" in src, (
        "снова одна попытка на аккаунт — случайный сбой выдачи выкинет рабочий "
        "аккаунт из круга до конца прогона"
    )
    assert "_irec_promote_retry_allowed" in src
    assert "not_participant" in src, (
        "самая частая временная причина не распознаётся, значит и не повторяется"
    )


def test_permanent_refusal_is_not_retried():
    """Если у промоутера нет права add_admins — повторять бессмысленно."""
    src = _func_src(OP_WORKER, "_exec_mass_invite")
    m = re.search(r"_reason in \(([^)]*)\)", src)
    assert m, "нет разбора причины отказа"
    retryable = m.group(1)
    assert "no_add_admins" not in retryable, (
        "окончательный отказ попал в повторяемые — впустую потратим попытки"
    )
    assert "not_participant" in retryable


def test_join_failure_is_no_longer_swallowed():
    """Молча проглоченный сбой вступления — прямая причина not_participant."""
    src = _func_src(OP_WORKER, "_exec_mass_invite")
    assert "вступление не удалось" in src, (
        "сбой join_channel снова гасится молча — потом не понять, почему "
        "промоутер отвечает «не участник»"
    )


def test_retry_waits_before_second_attempt():
    """Telegram регистрирует членство не мгновенно — второй попытке нужна пауза."""
    src = _func_src(OP_WORKER, "_exec_mass_invite")
    assert re.search(r"if _tries:\s*\n?\s*await asyncio\.sleep", src), (
        "повтор идёт мгновенно — снова получим not_participant"
    )


# ── «Мать-Дочка» видна в отчёте ──────────────────────────────────────────────

def test_daughter_mode_is_visible_in_the_summary():
    """Механизм не должен работать втихую.

    Итог операции не упоминал «Мать-Дочку» ВООБЩЕ — ни строки о расходной
    группе, ни счётчика ротаций. При этом ссылка в шапке отчёта при включённом
    режиме принадлежит дочерней группе, а подписана как обычная цель: отчёт
    выглядел так, будто людей заводили прямо в боевой канал.

    Практическое следствие: по такому итогу владелец не может проверить, был ли
    механизм включён и сработал ли он. Именно из-за этого разбор аварии пошёл по
    ложному следу — «Мать-Дочка выключена» было выведено из скриншота другого
    экрана, а не из отчёта, потому что отчёт молчал.
    """
    src = _func_src(OP_WORKER, "_exec_mass_invite")
    assert "Мать-Дочка: инвайт шёл в расходную группу" in src, (
        "отчёт снова молчит о режиме — проверить его работу будет нечем"
    )
    assert "_mother_ref" in src and "_daughter_rotations" in src, (
        "в отчёте нет ни боевого канала, ни числа сожжённых дочерних"
    )


def test_summary_warns_about_the_join_burst():
    """Честная граница механизма: от бана за инвайты он защищает, а всплеск
    вступлений в боевой канал создаёт сам — люди идут туда по закреплённой
    ссылке. Умолчать об этом — обещать защиту, которой нет."""
    src = _func_src(OP_WORKER, "_exec_mass_invite")
    assert "всплеск вступлений" in src, (
        "отчёт обещает защиту, не называя её границу"
    )
