"""Центр синхронизации: видно, с какого аккаунта контакты не идут и почему.

Экран показывал «Всего контактов: N», дату последнего сбора и плоский список
«Аккаунт #12 — 340 контактов». Список строился по contact_sources, то есть
аккаунт, с которого синхронизация ПАДАЕТ, в нём просто отсутствовал: сессия
протухла, контакты не идут, и узнать об этом было неоткуда — экран выглядел
исправным.

Причина при этом пишется с самого начала: sync_account кладёт в
contact_sync_log понятный русский текст (classify_session_error), а не сырой
Telethon. Теперь он доходит до экрана.

Отдельно держим класс видимого экрана. Автообновление во время сбора было
написано на `classList.contains('active')`, а push ставит `show` — условие не
срабатывало никогда, и все двадцать минут сбора экран показывал цифры, снятые
до его начала.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
API = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
SYNC = open(os.path.join(ROOT, "services", "contacts_hub", "sync_service.py"),
            encoding="utf-8").read()
SNAPSHOT = open(os.path.join(ROOT, "tests", "miniapp_routes_snapshot.txt"),
                encoding="utf-8").read()


def _fn(name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", HTML)
    assert m, f"функция {name} не найдена"
    i = HTML.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(HTML)):
        if HTML[j] == "{":
            depth += 1
        elif HTML[j] == "}":
            depth -= 1
            if depth == 0:
                return HTML[i:j + 1]
    raise AssertionError(name)


def _handler(name: str) -> str:
    i = API.index(f"async def {name}(request")
    j = API.index("\n    async def ", i + 10)
    return API[i:j]


def test_route_exists():
    assert "GET /api/miniapp/uch/sync_center" in SNAPSHOT


def test_the_error_is_actually_written_somewhere():
    """Измеритель проверяет себя: причина отказа действительно пишется."""
    assert "log_sync(pool, owner_id, account_id, 'auto', 0, 0, 0, 0, duration_ms, friendly" in SYNC, (
        "sync_account больше не пишет причину — экран нечего показывать")


def test_failing_account_is_listed_at_all():
    """Список идёт от аккаунтов, а не от собранных контактов."""
    h = _handler("uch_sync_center")
    assert "FROM tg_accounts a" in h, (
        "список снова строится по contact_sources — падающий аккаунт исчезнет")
    assert "contact_sync_log" in h, "последняя попытка сбора не читается"
    assert "error_message" in h, "причина отказа не доходит до экрана"


def test_everything_is_scoped_to_the_owner():
    h = _handler("uch_sync_center")
    assert "WHERE a.owner_id = $1" in h, "чужие аккаунты попадут в список"
    assert "WHERE owner_id = $1 AND account_id = a.id" in h, (
        "лог сбора берётся без скоупа по владельцу")
    assert "WHERE uc.owner_id = $1" in h


def test_running_sync_is_reported():
    h = _handler("uch_sync_center")
    assert "op_type='contacts_sync'" in h and "status IN ('pending','running')" in h, (
        "идущий сбор не виден — кнопка предложит запустить второй")


def test_screen_names_the_reason():
    f = _fn("loadSyncCenter")
    assert "a.error" in f, "причина отказа не выводится"
    assert "Ни разу не собирался" in f, (
        "аккаунт без единой попытки неотличим от собравшего ноль контактов")
    assert "Нет сохранённой сессии" in f, (
        "аккаунт без сессии выглядит как сломанный сбор")


def test_screen_shows_progress_while_syncing():
    f = _fn("loadSyncCenter")
    assert "d.running" in f or "const r = d.running" in f, "прогресс сбора не показан"
    assert "disabled" in f, "кнопку можно нажать второй раз поверх идущего сбора"


def test_autorefresh_uses_the_real_screen_class():
    """push ставит show; с 'active' автообновление не срабатывало никогда."""
    f = _fn("syncContacts")
    assert "classList.contains('show')" in f, (
        "автообновление снова висит на несуществующем классе")
    assert "classList.contains('active')" not in f


def test_no_raw_internal_ids_on_screen():
    f = _fn("loadSyncCenter")
    assert "Аккаунт #" not in f, "в подпись вернулся внутренний номер аккаунта"


def test_error_and_empty_states_have_a_way_out():
    f = _fn("loadSyncCenter")
    assert "errHtml(errRu(e), 'loadSyncCenter()')" in f, "ошибка без кнопки повтора"
    assert "empty('🔄'" in f and "goTab(" in f, (
        "пустой экран не говорит, что делать дальше")
