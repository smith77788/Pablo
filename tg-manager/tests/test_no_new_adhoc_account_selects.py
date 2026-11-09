"""Аккаунт для работы с Telegram выбирается общей выборкой, а не своей в каждом файле.

ЧТО БЫЛО. Почти сотня мест собирала словарь аккаунта своим SELECT: в 22 из
них не было id, в 29 — устройства. Клиент такого аккаунта шёл не через свой
прокси, с чужим устройством и другой парой api_id — для Telegram это другой
человек в той же сессии, то есть прямой путь к ограничению.

ЧТО ТЕПЕРЬ. Недостающее добирает сама фабрика клиента
(account_manager._complete_account_identity), поэтому старые места безопасны.
Но новых своих выборок быть не должно: для них есть
`db.telethon_accounts_query()` и `db.get_account_for_telethon()`.

Число своих выборок в файле может только уменьшаться. Перевели место на общую
выборку — уменьшите число здесь.
"""
from __future__ import annotations

import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SELECT = re.compile(
    r"SELECT\b((?:(?!SELECT\b).){0,600}?)\bFROM\s+tg_accounts\b", re.S | re.I)

# Файл → сколько своих выборок сессии в нём сейчас.
BASELINE: dict[str, int] = {
    "bot/handlers/channel_factory.py": 7,
    "bot/handlers/channel_ops.py": 15,
    "bot/handlers/group_factory.py": 5,
    "bot/handlers/health_dashboard.py": 1,
    "bot/handlers/infra_analytics.py": 2,
    "bot/handlers/promo_platform.py": 1,
    "bot/handlers/seo.py": 8,
    "bot/utils/ai_tools.py": 4,
    "services/account_health.py": 2,
    "services/account_manager.py": 1,
    "services/account_warmer.py": 1,
    "services/chat_warmup.py": 1,
    "services/content_mesh.py": 2,
    "services/drift_detector.py": 2,
    "services/fleet_doctor.py": 1,
    "services/flood_engine.py": 2,
    "services/ghost_engine.py": 1,
    "services/gift_inventory.py": 1,
    "services/gift_operation.py": 1,
    "services/google_sheets.py": 1,
    "services/infra_advisor.py": 1,
    "services/intelligence_engine.py": 2,
    "services/invite_preflight.py": 4,
    "services/keyword_watcher.py": 1,
    "services/mini_app_accounts.py": 5,
    "services/mini_app_api.py": 4,
    "services/mini_app_channels.py": 1,
    "services/mini_app_dashboard.py": 2,
    "services/mini_app_fleet.py": 1,
    "services/mini_app_invite.py": 1,
    "services/mini_app_uch.py": 1,
    "services/op_worker.py": 9,
    "services/showcase_layer.py": 1,
    "services/tg_cloud_fleet.py": 1,
    "services/trust_engine.py": 1,
}


def _carries_a_session(cols: str) -> bool:
    """Выборка отдаёт сессию (её колонку или все колонки разом)."""
    if re.search(r"(^|[\s,])(a\.)?\*", cols):
        return True
    return bool(re.search(r"\bsession_str\b(?!\s+IS\b)", cols))


def _count(path: str) -> int:
    src = open(path, encoding="utf-8", errors="ignore").read()
    return sum(1 for m in _SELECT.finditer(src) if _carries_a_session(m.group(1)))


def _scan() -> dict[str, int]:
    out = {}
    for sub in ("services", "bot"):
        for path in glob.glob(os.path.join(ROOT, sub, "**", "*.py"), recursive=True):
            n = _count(path)
            if n:
                out[os.path.relpath(path, ROOT).replace(os.sep, "/")] = n
    return out


def test_no_new_adhoc_selects_of_an_account_session():
    grown = {f: (BASELINE.get(f, 0), n) for f, n in _scan().items()
             if n > BASELINE.get(f, 0)}
    assert not grown, (
        "новая своя выборка сессии аккаунта (было, стало): " + str(grown)
        + ". Берите аккаунт через db.telethon_accounts_query() или "
          "db.get_account_for_telethon(): они отдают id, устройство и прокси, "
          "без которых клиент выходит в Telegram другим человеком.")


def test_the_baseline_is_tightened_when_a_place_is_fixed():
    now = _scan()
    loose = {f: (n, now.get(f, 0)) for f, n in BASELINE.items() if now.get(f, 0) < n}
    assert not loose, "выборок стало меньше — уменьшите число в BASELINE: " + str(loose)


def test_the_detector_sees_a_session_select_and_skips_a_count():
    assert _carries_a_session(" id, session_str ")
    assert _carries_a_session(" a.* ")
    assert not _carries_a_session(" COUNT(*) FILTER (WHERE session_str IS NOT NULL) ")
    assert not _carries_a_session(" id, phone ")
