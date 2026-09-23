"""Регрессия: в интерфейс утекали сырые английские значения из БД.

CLAUDE.md проекта: «Владелец НЕ понимает английский — не оставляй английских
фраз ни в ответах, ни в UI». Тем не менее интерфейс раз за разом показывал
значения колонок как есть: severity «critical», статус «running», тип объекта
«channel», исход операции «flood_wait», статус аккаунта «session_expired»,
а вместо названия операции — её идентификатор «mass_invite».

Переводы сделаны с честным запасным вариантом: незнакомое значение
показывается как есть. Это не хуже прежнего поведения, а выдумывать перевод
на лету — значит врать о том, что произошло.
"""
from __future__ import annotations

import re

import pytest

from tests.miniapp_source import miniapp_source

UI = miniapp_source()

TRANSLATORS = ["sevRu", "objRu", "outRu", "opRu", "accRu"]


@pytest.mark.parametrize("fn", TRANSLATORS)
def test_translator_exists(fn):
    assert re.search(r"function " + fn + r"\s*\(", UI), f"нет переводчика {fn}"


@pytest.mark.parametrize("fn", TRANSLATORS)
def test_translator_falls_back_to_the_raw_value(fn):
    """Незнакомое значение показываем как есть, а не прячем и не выдумываем."""
    m = re.search(r"function " + fn + r"\s*\([^)]*\)\s*\{(.*?)\n\}", UI, re.DOTALL)
    assert m, f"не нашли тело {fn}"
    body = m.group(1)
    assert "|| (v || '—')" in body, (
        f"{fn} не оставляет запасного варианта: незнакомое значение пропадёт "
        f"или превратится в undefined")


@pytest.mark.parametrize("raw", [
    "${a.severity}", "${r.severity}", "${ev.severity}",
    "${r.outcome}", "${esc(r.op_type)}",
    "${n.type} · ID:", "${t.entity_type}</div>",
])
def test_raw_value_is_no_longer_rendered(raw):
    assert raw not in UI, (
        f"сырое значение {raw} показывается владельцу как есть — "
        f"по-английски и без объяснения")


def test_statuses_go_through_a_translator():
    """Голые ${x.status} в видимом тексте — это running/done/failed на экране."""
    bare = re.findall(r"row-val\">\$\{[a-z]{1,3}\.status\}", UI)
    assert not bare, f"статус показывается как есть в {len(bare)} месте(ах)"


def test_account_status_has_its_own_translator():
    """У аккаунта свой набор значений — stb() знает только статусы операций."""
    m = re.search(r"function accRu\s*\([^)]*\)\s*\{(.*?)\n\}", UI, re.DOTALL)
    assert m and "session_expired" in m.group(1) and "spamblock" in m.group(1), (
        "accRu не покрывает статусы, из-за которых аккаунт не работает")


def test_operation_names_map_covers_the_mass_actions():
    m = re.search(r"const OP_RU = \{(.*?)\n\};", UI, re.DOTALL)
    assert m, "нет карты названий операций"
    body = m.group(1)
    for op in ("mass_invite", "mass_publish", "run_broadcast", "phone_check",
               "boost_views", "group_announce"):
        assert op + ":" in body, f"{op} остался техническим идентификатором на экране"
