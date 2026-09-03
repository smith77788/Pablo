"""Регресс: глобальный реестр «не приглашать» (contact_opt_out).

Первопричина: invite_target_log дедупит инвайты ТОЛЬКО в пределах одной группы
и не различает исход — человек, явно попросивший больше не приглашать, снова
получает инвайт в следующую группу. Ни CRM (unified_contacts), ни инвайт-движок
не знали о таком отказе. Реестр — ручной (авто-opt-out по
UserPrivacyRestrictedError был бы ложным срабатыванием: это настройка
приватности, а не отказ), но подключён по всем точкам дедупа.
"""
from __future__ import annotations

import os

from services.contact_opt_out import normalize_target, filter_targets, _compare_key

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ── normalize_target: должен зеркалить mass_inviter_engine.parse_user_refs/parse_phones ──

def test_username_normalized_lowercase_with_at():
    assert normalize_target("@Ivan") == "@ivan"
    assert normalize_target("Ivan") == "@ivan"
    assert normalize_target("ivan") == "@ivan"


def test_short_numeric_is_telegram_id():
    assert normalize_target("123456789") == "123456789"  # <11 цифр


def test_boundary_10_digits_is_id_not_phone():
    """Ровно 10 цифр без '+' — Telegram user_id (граница проекта: <11 цифр =
    id, 11+ = телефон, см. mass_inviter_engine._PHONE_MIN_DIGITS)."""
    assert normalize_target("1234567890") == "1234567890"


def test_phone_variants_normalize_to_e164():
    assert normalize_target("+79991234567") == "+79991234567"
    assert normalize_target("79991234567") == "+79991234567"  # 11 цифр без '+'
    assert normalize_target("+7 999 123-45-67") == "+79991234567"


def test_invalid_inputs_return_none():
    for bad in ("", "  ", "@", "ab", None):
        assert normalize_target(bad) is None


def test_normalization_compare_key_matches_parse_user_refs_and_parse_phones():
    """mass_inviter_engine.parse_user_refs НЕ приводит username к нижнему
    регистру ('@Ivan' остаётся как есть), а normalize_target — приводит
    ('@ivan') для хранения. Поэтому сравнивать их нужно через _compare_key
    (регистронезависимо для username), а не побайтово — иначе opt-out тихо не
    сработал бы для любого username, чей регистр в аудитории отличается от
    того, как его ввёл оператор."""
    from services.mass_inviter_engine import parse_user_refs, parse_phones

    assert _compare_key(parse_user_refs("Ivan")[0]) == _compare_key(normalize_target("Ivan"))
    assert parse_user_refs("123456789")[0] == normalize_target("123456789")
    assert parse_phones("+7 999 123-45-67")[0] == normalize_target("+7 999 123-45-67")


def test_filter_targets_case_insensitive_for_usernames():
    """Реальный сценарий: аудитория содержит '@Ivan' (исходный регистр
    источника), оператор ввёл 'ivan' при добавлении в реестр."""
    stored = normalize_target("ivan")
    remaining, n = filter_targets(["@Ivan", "@Petr"], {stored})
    assert remaining == ["@Petr"] and n == 1


# ── filter_targets: чистая функция ────────────────────────────────────────

def test_filter_targets_removes_opted_out():
    remaining, n = filter_targets(["@a", "@b", "@c"], {"@b"})
    assert remaining == ["@a", "@c"] and n == 1


def test_filter_targets_empty_optout_is_noop():
    remaining, n = filter_targets(["@a", "@b"], set())
    assert remaining == ["@a", "@b"] and n == 0


def test_filter_targets_all_removed():
    remaining, n = filter_targets(["@a", "@b"], {"@a", "@b"})
    assert remaining == [] and n == 2


# ── Проводка в op_worker._exec_mass_invite ────────────────────────────────

def test_wired_into_mass_invite_exec():
    src = _read("services/op_worker.py")
    start = src.index("async def _exec_mass_invite")
    # следующая exec-функция начинается с новой строки — ищем ПОСЛЕ начала
    # своей же сигнатуры, иначе "async def _exec_" совпадёт с самим собой на
    # позиции 0 и обрежет срез до пустой строки.
    next_def = src.find("\nasync def _exec_", start + 1)
    seg = src[start:next_def if next_def != -1 else len(src)]
    assert "contact_opt_out" in seg
    assert "load_opted_out(pool, owner_id)" in seg
    assert "filter_targets(" in seg
    # отдельный счётчик от per-группового дедупа — разная причина пропуска
    assert "_opted_out" in seg
    # попадает в финальную сводку оператору, а не только в ранний return
    assert "реестра «не приглашать»" in seg


# ── Точка входа в интерфейсе (бот: меню Инвайтера → реестр) ────────────────

def test_bot_menu_has_optout_entry():
    src = _read("bot/handlers/mass_inviter.py")
    assert 'InviterCb(action="optout_menu")' in src
    for action in ("optout_menu", "optout_add", "optout_remove"):
        assert f'InviterCb.filter(F.action == "{action}")' in src, f"нет хендлера {action}"
    assert "optout_target = State()" in src


def test_bot_optout_handlers_use_service_not_raw_sql():
    """Бот-хендлеры работают через contact_opt_out.py, не дублируют SQL."""
    src = _read("bot/handlers/mass_inviter.py")
    seg = src[src.index("Реестр «не приглашать»"):]
    assert "contact_opt_out as coo" in seg
    assert "coo.add(" in seg and "coo.remove(" in seg and "coo.list_for_owner(" in seg


def test_module_docstring_explains_no_auto_optout():
    """Зафиксировать осознанное решение — чтобы следующая сессия не
    «улучшила» это добавлением авто-opt-out по privacy-ошибкам."""
    src = _read("services/contact_opt_out.py")
    assert "UserPrivacyRestrictedError" in src
    assert "НЕ автоматизируется" in src
    assert "ложные" in src and "срабатывания" in src
