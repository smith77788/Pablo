"""Реестр «не писать» обязан действовать во ВСЕХ исходящих путях.

Разрыв, который это закрывает: `contact_opt_out` применялся только в
масс-инвайте. Человек, явно попросивший больше не писать, всё равно получал
DM-кампанию и разовую рассылку — юридический риск и прямая причина
спам-репортов, от которых горят аккаунты.

Проверяем поведение чистых фильтров (а не наличие строк в исходнике) и
подключение их к обоим путям отправки.
"""
from __future__ import annotations

import ast
import pathlib

from services.dm_engine import (
    filter_opted_out,
    filter_opted_out_refs,
    opt_out_keys,
)

_ROOT = pathlib.Path(__file__).resolve().parent.parent


# ── Ключи получателя ──────────────────────────────────────────────────────────

def test_opt_out_keys_cover_id_and_username():
    keys = opt_out_keys(user_id=12345, username="Ivan")
    assert "12345" in keys
    assert "@ivan" in keys, "username должен приводиться к нижнему регистру с «собакой»"


def test_opt_out_keys_username_with_at_sign_not_doubled():
    assert "@ivan" in opt_out_keys(username="@Ivan")
    assert "@@ivan" not in opt_out_keys(username="@Ivan")


def test_opt_out_keys_ignore_garbage_id():
    assert opt_out_keys(user_id=0) == []
    assert opt_out_keys(user_id=None, username=None) == []
    assert opt_out_keys(user_id="не-число") == []


# ── Фильтр получателей кампании ───────────────────────────────────────────────

def _targets():
    return [
        {"user_id": 1, "username": "alice"},
        {"user_id": 2, "username": "Bob"},
        {"user_id": 3, "username": None},
        {"user_id": 0, "username": "carol"},
    ]


def test_campaign_filter_removes_by_username_case_insensitive():
    remaining, removed = filter_opted_out(_targets(), {"@bob"})
    assert removed == 1
    assert all(t["user_id"] != 2 for t in remaining), "@Bob должен отсеяться по '@bob'"


def test_campaign_filter_removes_by_numeric_id():
    remaining, removed = filter_opted_out(_targets(), {"3"})
    assert removed == 1
    assert all(t["user_id"] != 3 for t in remaining)


def test_campaign_filter_matches_either_key():
    """Получатель отсеивается, даже если оператор занёс его по ДРУГОМУ ключу."""
    remaining, removed = filter_opted_out([{"user_id": 7, "username": "dave"}], {"7"})
    assert removed == 1 and remaining == []
    remaining, removed = filter_opted_out([{"user_id": 7, "username": "dave"}], {"@dave"})
    assert removed == 1 and remaining == []


def test_campaign_filter_empty_registry_is_noop():
    remaining, removed = filter_opted_out(_targets(), set())
    assert removed == 0 and len(remaining) == 4


def test_campaign_filter_survives_missing_keys():
    """Попытка сломать: элементы без ожидаемых ключей не должны ронять фильтр."""
    remaining, removed = filter_opted_out([{}, {"username": "zed"}], {"@zed"})
    assert removed == 1
    assert remaining == [{}]


def test_campaign_filter_does_not_mutate_input():
    src = _targets()
    filter_opted_out(src, {"@bob"})
    assert len(src) == 4, "фильтр обязан возвращать новый список, не править исходный"


# ── Фильтр плоского списка (разовая рассылка) ─────────────────────────────────

def test_refs_filter_matches_bare_username_without_at():
    """Главная ловушка: в разовой рассылке получатели приходят без «собаки».

    contact_opt_out.filter_targets сравнивает строки как есть и на 'ivan' не
    сработал бы против записи '@ivan' — поэтому у рассылки свой нормализующий
    фильтр.
    """
    remaining, removed = filter_opted_out_refs(["ivan", "petr"], {"@ivan"})
    assert removed == 1 and remaining == ["petr"]


def test_refs_filter_matches_at_and_case():
    remaining, removed = filter_opted_out_refs(["@Ivan"], {"@ivan"})
    assert removed == 1 and remaining == []


def test_refs_filter_matches_numeric_ref():
    remaining, removed = filter_opted_out_refs(["555", "@bob"], {"555"})
    assert removed == 1 and remaining == ["@bob"]


def test_refs_filter_empty_registry_is_noop():
    remaining, removed = filter_opted_out_refs(["a", "b"], set())
    assert removed == 0 and remaining == ["a", "b"]


# ── Подключение к обоим путям отправки ────────────────────────────────────────

def _func_source(relpath: str, funcname: str) -> str:
    src = (_ROOT / relpath).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == funcname:
            seg = ast.get_source_segment(src, node)
            assert seg is not None
            return seg
    raise AssertionError(f"{funcname} не найдена в {relpath}")


def test_campaign_runner_applies_opt_out():
    src = _func_source("services/dm_engine.py", "run_campaign")
    assert "filter_opted_out" in src and "load_opted_out" in src, (
        "run_campaign обязана фильтровать аудиторию по реестру «не писать»"
    )


def test_adhoc_blast_applies_opt_out():
    src = _func_source("services/op_worker.py", "_exec_bulk_dm_adhoc")
    assert "filter_opted_out_refs" in src and "load_opted_out" in src, (
        "разовая рассылка обязана фильтровать получателей по реестру «не писать»"
    )
