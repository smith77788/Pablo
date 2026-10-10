"""Регресс: связка карточки CRM-контакта с историей инвайтов и opt-out.

Первопричина: invite_target_log и contact_opt_out хранят инвайты/отказы как
строки-target'ы (@username / id / +телефон), полностью изолированно от
unified_contacts. Карточка контакта не могла показать «приглашался N раз» и
opt-out нельзя было поставить БЕЗ ручного ввода username/телефона — хотя вся
эта информация уже есть в самом контакте.

Решение: резолвер, а не новая таблица — переиспользует ту же нормализацию
(contact_opt_out.normalize_target), что уже пишет mass_inviter_engine, поэтому
сравнение target-строк совпадает без новых полей/индексов.
"""
from __future__ import annotations

import os

from services.contact_invite_link import contact_target_candidates

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ── contact_target_candidates: чистая функция ───────────────────────────────

def test_candidates_include_id_username_phones():
    c = {"telegram_user_id": 123456789, "username": "Ivan", "phones": ["+7 999 123-45-67"]}
    cands = contact_target_candidates(c)
    assert cands == ["123456789", "@ivan", "+79991234567"]


def test_candidates_dedup_and_skip_empty():
    c = {"telegram_user_id": None, "username": "", "phones": ["", None, "bad phone"]}
    assert contact_target_candidates(c) == []


def test_candidates_use_same_normalization_as_invite_pipeline():
    """Гарантия совместимости (регистронезависимо для username): резолвер
    канонизирует '@Ivan' -> '@ivan', а parse_user_refs — нет ('@Ivan' как
    есть). invite_history_for_contact сравнивает через LOWER() именно из-за
    этого расхождения — здесь фиксируем сам факт расхождения форм, чтобы
    следующая правка SQL-сравнения на точное совпадение сломала этот тест."""
    from services.mass_inviter_engine import parse_user_refs, parse_phones

    c = {"username": "Ivan", "phones": ["79991234567"]}
    cands = contact_target_candidates(c)
    assert cands[0].lower() == parse_user_refs("Ivan")[0].lower()
    assert cands[0] != parse_user_refs("Ivan")[0]  # регистр реально расходится
    assert cands[1] in parse_phones("79991234567")  # телефон/id регистра не имеют


def test_candidates_order_stable_id_first():
    """opt_out_contact хранит candidates[0] — порядок должен быть предсказуем
    (id — самый однозначный идентификатор, если он есть)."""
    c = {"telegram_user_id": 42, "username": "ivan", "phones": ["+79991234567"]}
    assert contact_target_candidates(c)[0] == "42"


# ── Проводка: модуль подключён к репозиторию CRM и к opt-out ────────────────

def test_module_reuses_repository_and_opt_out():
    src = _read("services/contact_invite_link.py")
    assert "from services.contacts_hub.repository import get_contact" in src
    assert "from services import contact_opt_out as coo" in src
    assert "from services.contact_opt_out import normalize_target" in src
    # read-only по дизайну — не пишет построчно в contact_history на инвайт
    assert "READ-ONLY" in src or "READ_ONLY" in src


def test_history_query_case_insensitive_against_invite_target_log():
    """invite_target_log хранит username в исходном регистре из user_refs —
    точное сравнение с канонизированными (lowercase) candidates тихо не нашло
    бы историю. Регресс на сам факт LOWER() в SQL."""
    src = _read("services/contact_invite_link.py")
    seg = src[src.index("async def invite_history_for_contact"):]
    seg = seg[:seg.index("async def is_contact_opted_out")]
    assert "LOWER(target) = ANY(" in seg, "сравнение стало точным — регрессия по регистру"


def test_none_phone_entries_do_not_become_phantom_targets():
    """JSONB-массив телефонов может содержать None/'' (частичные данные из
    sync) — str(None) иначе даёт валидный на вид username '@none'."""
    c = {"phones": [None, "", "  "]}
    assert contact_target_candidates(c) == []


def test_opt_out_contact_registers_all_forms_not_just_first():
    """Реальный баг: opt_out_contact регистрировал только candidates[0] (id),
    поэтому аудитория, ссылающаяся на контакт по username/телефону (другая
    форма ТОГО ЖЕ человека), не блокировалась — ложное чувство защиты.
    Пойман на реальном PG (см. коммит): здесь фиксируем контракт функции."""
    src = _read("services/contact_invite_link.py")
    seg = src[src.index("async def opt_out_contact"):]
    seg = seg[:seg.index("async def allow_contact_invite")]
    assert "for target in candidates:" in seg, "регистрирует не все формы контакта"
    assert "candidates[0]" not in seg, "регрессия к «только первая форма»"


def test_mini_app_endpoints_registered_and_wired():
    src = _read("services/mini_app_api.py")
    for path, fn in (
        ("/api/miniapp/uch/contacts/{contact_id}/invite_history", "uch_contact_invite_history"),
        ("/api/miniapp/uch/contacts/{contact_id}/opt_out", "uch_contact_opt_out"),
        ("/api/miniapp/uch/contacts/{contact_id}/allow_invite", "uch_contact_allow_invite"),
    ):
        assert f'"{path}", {fn})' in src, f"не зарегистрирован маршрут {path}"
        assert f"async def {fn}(" in src, f"нет реализации {fn}"
    # реально вызывают сервисный слой, а не заглушки
    seg = src[src.index("async def uch_contact_invite_history"):]
    seg = seg[:seg.index("async def uch_contact_versions")]
    assert "invite_history_for_contact" in seg
    assert "opt_out_contact" in seg
    assert "allow_contact_invite" in seg


def test_mini_app_ui_wired_into_contact_detail():
    """Ровно одна строка в openContactDetail подключает новую секцию — не
    трогает основной рендер карточки (минимальный отпечаток в index.html)."""
    html = _read("mini_app/index.html")
    assert '<script src="screens/contact_invite_link.js"></script>' in html
    i_db = html.index('<script src="screens/dashboard.js">')
    i_cil = html.index('<script src="screens/contact_invite_link.js">')
    assert i_db < i_cil, "contact_invite_link.js должен грузиться после dashboard.js"
    assert "renderContactInviteLink(id)" in html


def test_mini_app_screen_uses_documented_endpoints():
    js = _read("mini_app/screens/contact_invite_link.js")
    for fn in ("renderContactInviteLink", "_cilEnsureBox", "_cilRender", "_cilOptOut", "_cilAllow"):
        assert f"function {fn}" in js, f"нет {fn}"
    assert "/invite_history')" in js
    assert "/opt_out'" in js
    assert "/allow_invite'" in js
    # box переиспользуется при повторном вызове — не задваивает секцию
    assert "getElementById('cilBox')" in js


def test_all_public_functions_present():
    from services import contact_invite_link as cil

    for fn in ("contact_target_candidates", "invite_history_for_contact",
               "is_contact_opted_out", "opt_out_contact", "allow_contact_invite"):
        assert hasattr(cil, fn), f"нет {fn}"
