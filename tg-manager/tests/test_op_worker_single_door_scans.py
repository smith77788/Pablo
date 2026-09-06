"""Четыре исполнителя выбирали аккаунт мимо единой двери.

`_exec_ad_intel_scan`, `_exec_phone_check`, `_exec_gift_scan` и `_exec_report_peer`
брали аккаунт сырым `SELECT ... FROM tg_accounts WHERE is_active AND session_str
IS NOT NULL ORDER BY last_used ASC LIMIT N`. Такой выбор не спрашивает ни о чём
из того, что знает `services/resource_selector.select_all_active`:

* `acc_status` — забаненный, деактивированный, с истёкшей сессией и в спамблоке
  проходили как «активные» (`is_active` их не отсеивает);
* `cooldown_until` — аккаунт, отдыхающий после флуда, забирали в новое действие;
* живость прокси — аккаунт без сети до Telegram давал гарантированную ошибку.

Дороже всего это в `_exec_phone_check`: ImportContacts — одна из самых
баноопасных операций Telegram, а `ORDER BY last_used ASC` подбирал как раз
наименее использованный, то есть часто самый холодный аккаунт.

Ратчет `test_account_selection_single_door` эти места не ловил: он требует
`session_str` голой колонкой в проекции, а здесь выбирали `id` или `SELECT *`.

Отдельно стережётся сдержанность миграции: порог доверия НЕ повышен. Внутри
`select_all_active` стоит `COALESCE(a.trust_score, 0) >= min_trust`, поэтому
любой положительный порог выкинул бы аккаунты с ещё не измеренным доверием
(NULL) и на свежем флоте операция превратилась бы в «нет аккаунтов».
"""
from __future__ import annotations

import ast
import pathlib
import re

_SRC = (pathlib.Path(__file__).resolve().parent.parent
        / "services" / "op_worker.py").read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)

_MIGRATED = ("_exec_ad_intel_scan", "_exec_phone_check",
             "_exec_gift_scan", "_exec_report_peer")


def _body(name: str) -> str:
    """Тело функции по границам AST — не окно фиксированной длины: сдвинулся
    код, и проверка на окне выключилась бы молча."""
    for n in ast.walk(_TREE):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name:
            return ast.get_source_segment(_SRC, n) or ""
    raise AssertionError(f"{name} не найден — исполнитель переименован?")


def _sql(body: str) -> str:
    return " ".join(body.split()).lower()


# ── Сырой выбор ушёл ───────────────────────────────────────────────────────

def test_no_executor_picks_accounts_with_raw_sql_anymore():
    for name in _MIGRATED:
        s = _sql(_body(name))
        assert "from tg_accounts" not in s, (
            f"{name} снова выбирает аккаунты сырым SQL мимо resource_selector"
        )


def test_the_least_recently_used_ordering_is_gone():
    """`ORDER BY last_used ASC` — это и был обход: он ранжировал аккаунты, не
    спрашивая ни о статусе, ни о кулдауне."""
    for name in _MIGRATED:
        assert "last_used asc" not in _sql(_body(name)), name


# ── Каждый исполнитель ходит через свою правильную дверь ───────────────────

def test_single_account_executors_use_the_rotating_door():
    """Один аккаунт на операцию: нужна и защита, и размазывание нагрузки —
    иначе весь поток уходит в один аккаунт (это и лечила прежняя сортировка)."""
    for name in ("_exec_ad_intel_scan", "_exec_phone_check"):
        assert "select_account_rotated" in _body(name), name


def test_multi_account_executors_use_select_all_active():
    for name in ("_exec_gift_scan", "_exec_report_peer"):
        assert "select_all_active" in _body(name), name


def test_report_peer_applies_its_limit_after_the_filters():
    """Срез до acc_count раньше фильтров выбирал бы лимит из непригодных."""
    b = _body("_exec_report_peer")
    m = re.search(r"select_all_active\((.|\n)*?\)\)\[:acc_count\]", b)
    assert m, "лимит acc_count должен применяться к УЖЕ отфильтрованному списку"


# ── Сдержанность: миграция не добавила нового способа отказать ─────────────

def test_migration_did_not_raise_the_trust_floor():
    """COALESCE(trust_score, 0) внутри select_all_active означает, что любой
    положительный порог выкидывает аккаунты с NULL-доверием."""
    for name in _MIGRATED:
        b = _body(name)
        m = re.search(r'action_type\s*=\s*"([a-z_]+)"', b)
        assert m, f"{name}: action_type должен быть указан явно"
        assert m.group(1) == "default", (
            f"{name} поднял порог доверия до «{m.group(1)}» — на свежем флоте "
            f"(trust_score NULL) операция станет «нет аккаунтов»"
        )


def test_the_trust_floor_really_would_drop_null_trust_accounts():
    """Проверка самого рассуждения: если бы COALESCE отдавал 1.0, а не 0,
    оговорка выше была бы лишней — тест поймает смену семантики."""
    rs = (pathlib.Path(__file__).resolve().parent.parent
          / "services" / "resource_selector.py").read_text(encoding="utf-8")
    assert "COALESCE(a.trust_score, 0) >=" in rs


# ── Исполнители остались на месте и рабочими ───────────────────────────────

def test_executors_still_report_an_honest_empty_fleet():
    """Пустой результат выбора обязан давать понятный отказ, а не падение."""
    for name in _MIGRATED:
        b = _body(name)
        assert re.search(r"if not (acc|acc_row|accounts|account_id)\b", b), name


def test_migrated_executors_are_still_registered():
    from services import op_worker
    for name in _MIGRATED:
        assert hasattr(op_worker, name), f"{name} исчез из модуля"
