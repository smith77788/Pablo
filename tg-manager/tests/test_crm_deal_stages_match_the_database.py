"""Стадии сделок в коде — те же, что разрешает база.

Баг: бот (bot/handlers/crm.py) создавал и двигал сделки по стадиям
new/contacted/qualified из schema_v90, авто-ответчик и сценарии создавали
сделки со stage='new', а crm_deals_stage_check (services/mini_app_api.py)
разрешает только lead/contact/proposal/negotiation/won/lost. Каждая такая
вставка падала на проверке: из бота сделку нельзя было ни создать, ни
передвинуть, а заявки из авто-ответа и сценариев молча терялись.

Тест берёт разрешённый набор из самой проверки и требует, чтобы:
* стадии бота и db.CRM_DEAL_STAGES совпадали с ним;
* ни одна вставка/обновление crm_deals в коде не писала стадию-литерал вне него;
* старые имена переводились в новые (CSV, кнопки, отправленные до обновления).

Против настоящей базы то же проверяет tests/test_crm_deal_stages_postgres.py.
"""
from __future__ import annotations

import pathlib
import re

from database import db
from tests.miniapp_source import api_source

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _allowed() -> set[str]:
    # Проверку ставит слой API; ищем во всём слое, а не в одном файле, чтобы
    # тест не ослеп, когда список DDL переедет из монолита.
    src = api_source()
    m = re.search(r"crm_deals_stage_check \"\s*\"CHECK \(stage IN \(([^)]*)\)\)", src)
    assert m, "проверка crm_deals_stage_check не найдена в слое API"
    return set(re.findall(r"'([a-z]+)'", m.group(1)))


def test_code_stages_are_the_database_stages():
    from bot.handlers import crm
    allowed = _allowed()
    assert set(db.CRM_DEAL_STAGES) == allowed
    assert set(crm._PIPELINE_STAGES) == allowed
    assert set(crm._STAGE_LABELS) == allowed


def test_no_literal_stage_outside_the_allowed_set():
    allowed = _allowed()
    bad = []
    for p in list((ROOT / "services").rglob("*.py")) + list((ROOT / "bot").rglob("*.py")) \
            + [ROOT / "database" / "db.py"]:
        src = p.read_text(encoding="utf-8")
        for m in re.finditer(r"INSERT INTO\s+crm_deals\s*\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)",
                             src, re.I):
            cols = [c.strip().lower() for c in m.group(1).split(",")]
            vals = [v.strip() for v in m.group(2).split(",")]
            if "stage" in cols and len(vals) == len(cols):
                v = vals[cols.index("stage")]
                if v.startswith("'") and v.strip("'") not in allowed:
                    bad.append(f"{p.relative_to(ROOT)}: stage={v}")
        for m in re.finditer(r"UPDATE\s+crm_deals\s+SET\s+stage\s*=\s*'([a-z]+)'", src, re.I):
            if m.group(1) not in allowed:
                bad.append(f"{p.relative_to(ROOT)}: SET stage='{m.group(1)}'")
    assert not bad, "стадии, которые база отвергнет: " + "; ".join(bad)


def test_old_names_are_translated():
    assert db.normalize_crm_stage("new") == "lead"
    assert db.normalize_crm_stage("contacted") == "contact"
    assert db.normalize_crm_stage("qualified") == "proposal"
    assert db.normalize_crm_stage("Переговоры") == "negotiation"
    assert db.normalize_crm_stage("won") == "won"
    assert db.normalize_crm_stage("мусор") == "lead"
    assert db.normalize_crm_stage("мусор", None) is None
