"""Шаг №1 аудита: «одна дверь к аккаунту» — заморозка утечки выбора.

ПРОБЛЕМА (находка аудита). Выбор аккаунта ДЛЯ ДЕЙСТВИЯ должен идти через
флуд-осознанный слой (resource_selector → flood_engine): он учитывает cooldown,
доверие и серии флудов и не даёт толкнуть в действие аккаунт, которому сейчас
нельзя. Но по коду разбросаны собственные SELECT-ы из tg_accounts, которые
выбирают аккаунты сырым SQL мимо этого слоя. Такой путь может выбрать аккаунт в
кулдауне/с высоким риском и отправить его в операцию — прямая дорога к бану, и
она не видна, пока аккаунт не улетит. Это тот же архитектурный шов, что и авария
с транспортом: ум есть в центре, но десятки путей ходят мимо него.

ПОЧЕМУ ЗАМОРОЗКА, А НЕ ПЕРЕПИСЫВАНИЕ. Уникальных паттернов выбора — 26 (мест
больше: один паттерн встречается во многих исполнителях), переписать всё разом
рискованно. Поэтому первый безопасный шаг: заморозить множество известных
утечек. Новая утечка (в новом файле или новый паттерн выбора) роняет тест —
распространяться дальше нельзя. По мере миграции запись удаляется из BASELINE, и
множество сжимается («ratchet down»). Уже мигрировано: 7 паттернов (op_worker боевые, channel_ops-инвайт, session_pool,
geo_router, phone_checker, ad_intelligence, mini_app_api global_search).

ОГРАНИЧЕНИЕ ДЕТЕКТОРА. Ловятся СТАТИЧЕСКИЕ SQL-строки (в т.ч. неявная склейка
литералов — Python сворачивает её в одну константу). Запрос, собранный из
переменных/ف-строк/`.format`, детектор не увидит — такие редки, но это честная
граница, а не гарантия полноты. Часть замороженных мест уже фильтрует cooldown
вручную (phone_checker) или это диагностика/показ (health_dashboard,
infra_analytics, scan) — они в реестре как известные, но мигрировать их в первую
очередь не обязательно.

ЧТО СЧИТАЕТСЯ УТЕЧКОЙ. Выбор ИЗ МНОГИХ аккаунтов (не загрузка одного по id — там
аккаунт уже выбран выше) с ГОЛОЙ колонкой session_str в проекции (то есть строим
клиента), вне флуд-осознанного/канонического ядра (LEGIT ниже). Загрузка одного
аккаунта по `id=$N` легитимна и сюда не входит.

КАК МИГРИРОВАТЬ (и убрать запись из BASELINE). Перевести выбор на
services.resource_selector (select_account/select_accounts/select_all_active) или
flood_engine.get_best_account — они возвращают аккаунты с учётом флуда и здоровья.
Затем удалить соответствующий ключ из BASELINE. Тест проверит, что мёртвых
записей не осталось.
"""
from __future__ import annotations

import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SKIP = {"tests", ".git", "__pycache__", "node_modules", "mini_app"}

# Флуд-осознанное / каноническое ядро: здесь выбор аккаунтов ЗАКОНЕН.
#   resource_selector/flood_engine — сам умный слой;
#   database/db.py — канонический telethon_accounts_query;
#   account_manager/monitor/health — ядро жизненного цикла аккаунта.
_LEGIT = {
    "services/resource_selector.py", "services/flood_engine.py",
    "database/db.py", "services/account_manager.py",
    "services/account_monitor.py", "services/account_health.py",
}

# Замороженное множество известных утечек (файл::сигнатура проекции, ≤60 симв).
# СЖИМАЕТСЯ по мере миграции — не растёт. Новый ключ = новая утечка = падение.
# Роль каждого оставшегося места:
#   TODO  — боевой выбор-для-действия, кандидат на миграцию в resource_selector;
#   LEAVE — намеренно оставлено: показ/дашборд/предполёт/не выбор-для-действия;
#           там нужен ПОЛНЫЙ флот (включая cooling), миграция сменила бы смысл.
# Мигрировано за шаг №1: 10 паттернов (op_worker боевые ×4-исполн., bot_factory,
# channel_ops-инвайт, session_pool, geo_router, phone_checker, ad_intelligence,
# mini_app_api/global_search bot, audience_parser).
BASELINE = {
    'bot/handlers/channel_factory.py::id, session_str, first_name, phone, device_model, system_ver',  # TODO
    'bot/handlers/channel_ops.py::a.id, a.session_str, a.first_name, a.phone, a.device_model, ',  # TODO
    'bot/handlers/channel_ops.py::a.id, a.session_str, a.first_name, a.username, a.device_mode',  # TODO
    'bot/handlers/channel_ops.py::a.id, a.session_str, a.phone, a.first_name, a.username, a.is',  # TODO (_get_accounts: уже фильтрует cooldown)
    'bot/handlers/channel_ops.py::a.id, a.session_str, a.tg_user_id, a.first_name, a.username,',  # TODO
    'bot/handlers/health_dashboard.py::id, session_str, phone, first_name, username, trust_score, d',  # LEAVE: дашборд здоровья
    'bot/handlers/infra_analytics.py::id, acc_status, trust_score, session_str, proxy_id',  # LEAVE: аналитика
    'bot/handlers/infra_analytics.py::id, phone, first_name, session_str, device_model, system_ver',  # LEAVE: аналитика
    'bot/handlers/promo_platform.py::id, session_str, first_name, username, phone, proxy_id',  # LEAVE: поиск аккаунта-владельца ботов (не выживаемость)
    'services/invite_preflight.py::id, phone, session_str',  # LEAVE: предполётная проверка (показывает и cooling)
    'services/mini_app_api.py::a.id, a.owner_id, a.session_str, a.device_model, a.system_ve',  # LEAVE: rights_check/grant_admin (предполёт, свой proxy-фильтр)
    'services/mini_app_api.py::a.id, a.session_str, a.first_name, a.phone, a.device_model, ',  # LEAVE: diag
    'services/op_worker.py::a.id, a.owner_id, a.session_str, a.device_model, a.system_ve',  # TODO: _maybe_requeue
    'services/op_worker.py::a.id, a.session_str, a.first_name, a.phone, a.username, a.de',  # LEAVE: health/scan (диагностика)
    'services/op_worker.py::a.id, a.session_str, a.first_name, a.phone, a.username, p.pr',  # LEAVE: health-check (диагностика)
    'services/op_worker.py::id, session_str, first_name, phone, device_model, system_ver',  # TODO: bulk-исполнители (10 мест)
    'services/strike_engine.py::id, phone, session_str, trust_score, is_active, acc_status, ',  # TODO: mass_report
}


def _bare_session(proj: str) -> bool:
    """session_str как САМОСТОЯТЕЛЬНАЯ колонка (строим клиента), а не признак
    `(session_str IS NOT NULL) AS has_session` в дашборде."""
    return any(re.fullmatch(r"(a\.)?session_str( as \w+)?", c.strip())
               for c in proj.split(","))


def _leaks_in_source(rel: str, source: str) -> set[str]:
    keys: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return keys
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Constant) and isinstance(n.value, str)):
            continue
        low = " ".join(n.value.split()).lower()
        m = re.search(r"\bselect\b(.*?)\bfrom\s+tg_accounts\b", low)
        if not m or not _bare_session(m.group(1)):
            continue
        # загрузка одного по id — аккаунт уже выбран выше, флуд-выбор не нужен.
        # Цифра/параметр после '=' ОБЯЗАТЕЛЬНЫ (\d, не \d?): иначе join-условие
        # `LEFT JOIN user_proxies p ON p.id = a.proxy_id` ложно читается как
        # «загрузка по id», и выборка без ANY, но с джойном, пропускается —
        # ратчет становится дырявым (так были потеряны phone_checker и др.).
        if re.search(r"\bid\s*=\s*\$?\d", low) and "any(" not in low:
            continue
        keys.add(f"{rel}::{m.group(1).strip()[:60]}")
    return keys


def _current_leaks() -> set[str]:
    found: set[str] = set()
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if d not in _SKIP]
        for f in fn:
            if not f.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dp, f), ROOT).replace(os.sep, "/")
            if rel in _LEGIT:
                continue
            src = open(os.path.join(dp, f), encoding="utf-8", errors="replace").read()
            found |= _leaks_in_source(rel, src)
    return found


def test_no_new_account_selection_leaks():
    """Новый сырой выбор аккаунтов мимо флуд-слоя — запрещён.

    Появился ключ вне BASELINE — значит, добавили ещё одну «дверь» в обход
    resource_selector. Либо ведите выбор через него, либо (если это осознанно
    и безопасно) добавьте ключ в BASELINE с объяснением в ревью.
    """
    new = _current_leaks() - BASELINE
    assert not new, (
        "новый выбор аккаунтов сырым SQL мимо флуд-слоя:\n  "
        + "\n  ".join(sorted(new))
        + "\n\nВедите выбор через services.resource_selector "
          "(select_account/select_accounts/select_all_active) — он учитывает "
          "cooldown/доверие/флуды. Иначе аккаунт в кулдауне уйдёт в действие → бан."
    )


def test_baseline_has_no_dead_entries():
    """BASELINE не должен содержать уже мигрированных мест.

    Ключ есть в реестре, но в коде его больше нет — значит место мигрировали, а
    из BASELINE не убрали. Реестр обязан сжиматься: удалите мёртвый ключ.
    """
    dead = BASELINE - _current_leaks()
    assert not dead, (
        "в BASELINE есть записи, которых больше нет в коде (мигрированы?):\n  "
        + "\n  ".join(sorted(dead))
        + "\n\nУберите их из BASELINE — реестр сжимается по мере миграции."
    )


def test_detector_catches_a_planted_leak():
    """Детектор, который ничего не находит, — зелёный и бесполезный."""
    planted = (
        "async def f(pool, owner):\n"
        "    return await pool.fetch(\n"
        "        \"SELECT id, session_str, device_model FROM tg_accounts \"\n"
        "        \"WHERE owner_id=$1 AND is_active=TRUE ORDER BY id\", owner)\n"
    )
    got = _leaks_in_source("services/some_new_engine.py", planted)
    assert got, "детектор не увидел явную новую утечку выбора"

    # А единичную загрузку по id — НЕ считает утечкой (аккаунт уже выбран).
    single = (
        "async def g(pool, aid, owner):\n"
        "    return await pool.fetchrow(\n"
        "        \"SELECT id, session_str FROM tg_accounts WHERE id=$1 AND owner_id=$2\",\n"
        "        aid, owner)\n"
    )
    assert not _leaks_in_source("services/x.py", single), (
        "загрузка одного аккаунта по id ошибочно помечена утечкой")
