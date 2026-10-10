"""schema_v157: чистка сырых плейсхолдеров у уже созданных ботов.

Гарантируем, что значения в SQL-миграции совпадают с default_subs пресета
support_bot (иначе миграция и код разъедутся), и что после подстановки в
реальном тексте пресета не остаётся сырых токенов.
"""
from __future__ import annotations

import pathlib
import re

from services.preset_templates import default_subs, get_preset_by_key, render_template

_SQL = (pathlib.Path(__file__).resolve().parents[1] / "schema_v157.sql").read_text(encoding="utf-8")
_PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}")


def _apply_sql_replaces(text: str) -> str:
    """Повторяет цепочку REPLACE из миграции на Python (значения парсим из SQL)."""
    pairs = re.findall(r"'(\{\{[A-Z_]+\}\})',\s*'([^']*)'", _SQL)
    # dedup по токену (в файле два одинаковых блока для auto_replies и funnel_steps)
    seen = {}
    for tok, val in pairs:
        seen.setdefault(tok, val)
    for tok, val in seen.items():
        text = text.replace(tok, val)
    return text


def test_sql_values_match_default_subs():
    subs = default_subs(get_preset_by_key("bot__support_bot"))
    pairs = dict(re.findall(r"'(\{\{[A-Z_]+\}\})',\s*'([^']*)'", _SQL))
    assert pairs.get("{{COMPANY}}") == subs["COMPANY"]
    assert pairs.get("{{HOURS}}") == subs["HOURS"]
    assert pairs.get("{{OPERATOR_LINE}}") == subs["OPERATOR_LINE"]


def test_migration_leaves_no_raw_tokens_on_preset_text():
    tpl = get_preset_by_key("bot__support_bot")["template"]
    for field in ("welcome_message",):
        cleaned = _apply_sql_replaces(tpl[field])
        assert not _PLACEHOLDER.search(cleaned), f"остался сырой токен в {field}: {cleaned}"
    # результат совпадает с тем, что дал бы код при применении по умолчанию
    subs = default_subs(get_preset_by_key("bot__support_bot"))
    code_rendered = render_template(tpl, subs)["welcome_message"]
    assert _apply_sql_replaces(tpl["welcome_message"]) == code_rendered


def test_migration_is_idempotent_guarded():
    # обе таблицы фильтруются по наличию '{{...}}' → повторный прогон no-op
    assert _SQL.count("LIKE '%{{%}}%';") == 2  # по одному WHERE-гарду на UPDATE
    assert "auto_replies" in _SQL and "funnel_steps" in _SQL
