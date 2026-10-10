"""Экосистема: состав — люди и объекты, а не «account #17» на чтение.

Экран детали показывал участников типом и номером строки в базе, роль и тип
события — по-английски, счётчик «Участники (50)» упирался в LIMIT запроса, а
управлять составом было нечем: авто-наполнение есть, убрать лишнее — нет.

Плюс два готовых куска движка висели без интерфейса: пересечения аудитории
внутри экосистемы и советы по экосистемам (эндпоинты были, экранов не было).
"""
from __future__ import annotations

import inspect
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
API = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
OVERLAPS_JS = os.path.join(ROOT, "mini_app", "screens", "eco_overlaps.js")


def _no_comments(js: str) -> str:
    """JS без строк-комментариев: иначе старый текст в пояснении ломает проверки."""
    return "\n".join(ln for ln in js.splitlines() if not ln.strip().startswith("//"))


def _fn(js: str, name: str) -> str:
    m = re.search(r"(?:async )?function %s\s*\([^)]*\)\s*\{" % re.escape(name), js)
    assert m, f"нет функции {name}"
    i = m.end() - 1
    depth = 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[i:j + 1]
    raise AssertionError(f"не закрылось тело {name}")


# ── Состав экосистемы ────────────────────────────────────────────────────────

def test_server_resolves_member_names():
    assert "async def _eco_member_names" in API
    body = API[API.index("async def _eco_member_names"):]
    body = body[:body.index("async def ecosystem_detail")]
    for table in ("tg_accounts", "managed_bots", "managed_channels"):
        assert table in API, table
    assert 'm["name"] = found.get' in body
    # Выборка имён — по владельцу: чужой объект имени не получает.
    detail = API[API.index("async def ecosystem_detail"):]
    detail = detail[:detail.index("async def ecosystem_member_drop")]
    assert "_eco_member_names(uid, members)" in detail


def test_member_count_is_real_not_page_size():
    detail = API[API.index("async def ecosystem_detail"):]
    detail = detail[:detail.index("async def ecosystem_member_drop")]
    assert "members_total" in detail
    assert "SELECT COUNT(*) FROM ecosystem_members WHERE ecosystem_id=$1" in detail


def test_member_can_be_removed():
    assert "async def ecosystem_member_drop" in API
    assert '"/api/miniapp/ecosystem/{eco_id}/member/drop"' in API
    drop = API[API.index("async def ecosystem_member_drop"):]
    drop = drop[:drop.index("async def ecosystem_auto_discover")]
    # Владение проверяется до удаления, тип объекта — из белого списка.
    assert "FROM ecosystems WHERE id=$1 AND owner_id=$2" in drop
    assert '("account", "bot", "channel", "group")' in drop
    assert "ecosystem_brain.remove_member" in drop
    assert "max_val=2 ** 63 - 1" in drop      # id бота — BIGINT
    front = _no_comments(_fn(HTML, "ecoDropMember"))
    assert "member/drop" in front and "askConfirm" in front


def test_member_row_is_russian_and_leads_to_the_object():
    body = _no_comments(_fn(HTML, "openEcoDetail"))
    assert "ecoObjRu(t)" in body            # «Аккаунт», не «account»
    assert "ecoRoleRu(m.role)" in body      # «участник», не «member»
    assert "ecoOpenMember(" in body         # строка ведёт к самому объекту
    assert "m.name ? m.name" in body        # имя вместо номера строки
    assert "ecoDropMember(" in body
    jump = _no_comments(_fn(HTML, "ecoOpenMember"))
    for fn in ("openAccount", "openBot", "openChannel"):
        assert fn in jump, fn
        assert re.search(r"(?:async )?function %s\s*\(" % fn, HTML), f"нет {fn}"


def test_event_type_is_translated():
    body = _no_comments(_fn(HTML, "openEcoDetail"))
    assert "ecoEvRu(ev.event_type)" in body
    assert "ev.event_type}" not in body     # сырой тип больше не выводится


def test_autodiscover_toast_is_russian():
    body = _no_comments(_fn(HTML, "ecoAutoDiscover"))
    assert "ECO_OBJ_RU[k]" in body, "в тосте оставались ключи account/channel/bot"


def test_empty_composition_is_not_a_dead_end():
    body = _no_comments(_fn(HTML, "openEcoDetail"))
    assert "Пока никого" in body
    assert "ecoAutoDiscover()" in body
    assert "Событий пока нет" in body


# ── Пересечения аудитории ────────────────────────────────────────────────────

def test_overlaps_screen_exists_and_is_wired():
    assert os.path.exists(OVERLAPS_JS), "нет экрана пересечений"
    js = open(OVERLAPS_JS, encoding="utf-8").read()
    assert 'screens/eco_overlaps.js' in HTML, "экран не подключён в index.html"
    assert "function openEcoOverlaps" in js
    assert "/api/miniapp/ecosystem/" in js and "/overlaps" in js
    # Экран доступен из детали экосистемы.
    assert "openEcoOverlaps(" in _no_comments(_fn(HTML, "openEcoDetail"))


def test_overlaps_pairs_carry_channel_names():
    ov = API[API.index("async def ecosystem_overlaps"):]
    ov = ov[:ov.index("app.router.add_get") if "app.router.add_get" in ov else len(ov)]
    assert 'pr["name_a"]' in ov and 'pr["name_b"]' in ov
    assert "FROM managed_channels" in ov and "owner_id=$1" in ov
    js = open(OVERLAPS_JS, encoding="utf-8").read()
    assert "p.name_a" in js and "p.name_b" in js
    assert "openChannel(" in js


def test_overlaps_empty_states_are_distinct_and_actionable():
    """«Каналов мало» и «подписчиков не собрали» — разные проблемы."""
    js = open(OVERLAPS_JS, encoding="utf-8").read()
    body = _no_comments(_fn(js, "openEcoOverlaps"))
    assert "chs < 2" in body
    assert "Сравнивать пока нечего" in body
    assert "Подписчики ещё не собраны" in body
    assert "openParser()" in body


# ── Советы по экосистемам ────────────────────────────────────────────────────

def test_recommendations_carry_a_target():
    from services.ecosystem_brain import get_ecosystem_recommendations
    src = inspect.getsource(get_ecosystem_recommendations)
    assert "-> list[dict]" in src, "совет остался просто строкой"
    for key in ("eco_create", "accounts", "op_create", "ops_failed",
                "accounts_cooldown", "channels"):
        assert f'"{key}"' in src, key
    go = re.search(r"const ECO_REC_GO = \{(.*?)\n\};", HTML, re.S)
    assert go, "в мини-аппе нет карты ECO_REC_GO"
    keys = set(re.findall(r"(\w+):\s*\{fn:", go.group(1)))
    assert {"eco_create", "accounts", "accounts_cooldown", "op_create",
            "ops_failed", "channels"} <= keys, keys


def test_recommendations_are_shown_on_the_list_screen():
    assert 'id="ecoRecs"' in HTML
    assert "ecoLoadRecs()" in _no_comments(_fn(HTML, "openEcosystems"))
    loader = _no_comments(_fn(HTML, "ecoLoadRecs"))
    assert "/api/miniapp/ecosystem_recommendations" in loader
    assert "ECO_REC_GO[r.go]" in loader


def test_recommendation_targets_exist_in_mini_app():
    go = re.search(r"const ECO_REC_GO = \{(.*?)\n\};", HTML, re.S).group(1)
    for key, call in re.findall(r"(\w+):\s*\{fn:\s*(\"[^\"]+\"|'[^']+')", go):
        name = call.strip("\"'").split("(")[0]
        assert re.search(r"(?:async )?function %s\s*\(" % re.escape(name), HTML), \
            f"совет {key} ведёт в {name}() — такой функции нет"
