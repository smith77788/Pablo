"""Модуль «Инфраструктура»: пулами можно пользоваться, журнал читается.

Что было (скриншоты владельца, 10.11.2026):

* Пул нельзя было создать из мини-аппа. Пустой экран отправлял «назначить
  на карточке аккаунта», а на карточке такой настройки не было: назначать
  пул умел только бот. Проверяем, что дверь есть на трёх путях — экран
  пулов, панель выбора аккаунтов, карточка аккаунта, — и что сервер её
  держит со скоупом владельца.
* Журнал показывал латиницу («warmup», «day 8», «all 3 actions failed»):
  владелец английского не читает. Подписи теперь пишет СЕРВЕР, и каждое
  действие, которое продукт кладёт в operation_audit своим словом, обязано
  иметь русское название.
* Строка «Без пула» должна открывать аккаунты БЕЗ пула, а не весь флот.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

from services import infra_hub as ih
from tests.miniapp_source import api_source, js_function, miniapp_html, miniapp_source

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ── Названия пулов ──────────────────────────────────────────────────────────

def test_pool_name_is_trimmed_and_checked():
    assert ih.normalize_pool_name("  Инвайт   Москва ") == "Инвайт Москва"
    for bad in ("", "   ", "—", "x" * (ih.POOL_NAME_MAX + 1)):
        try:
            ih.normalize_pool_name(bad)
        except ih.PoolNameError as e:
            assert re.search(r"[А-Яа-я]", str(e)), "причина отказа не по-русски"
        else:
            raise AssertionError(f"принято неверное название {bad!r}")


def test_russian_system_pool_name_maps_to_the_same_pool():
    """Иначе рядом с «primary» появился бы второй пул «Основной»."""
    assert ih.normalize_pool_name("основной") == "primary"
    assert ih.pool_label("primary") == "Основной"
    assert ih.pool_label(None) == "Без пула"


def test_bot_and_miniapp_rebalance_by_one_rule():
    from bot.handlers import infra_analytics as bot_ia

    cases = [
        {"trust_score": 0.9, "warnings": [], "on_cooldown": False},
        {"trust_score": 0.9, "warnings": ["x"], "on_cooldown": False},
        {"trust_score": 0.1, "warnings": [], "on_cooldown": False},
        {"trust_score": 0.5, "warnings": [], "on_cooldown": True},
        {"trust_score": 0.5, "warnings": [], "on_cooldown": False},
        {"trust_score": None, "warnings": None, "on_cooldown": False},
    ]
    want = ["primary", "monitoring", "monitoring", "cooldown", None, None]
    assert [ih.classify_account(c) for c in cases] == want
    assert [bot_ia._classify_account(c) for c in cases] == want


# ── Запись пула: скоуп владельца и «пусто — значит никого» ───────────────────

class _Pool:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "UPDATE 3"


def test_assign_is_scoped_to_owner_and_empty_selection_touches_nobody():
    p = _Pool()
    assert asyncio.run(ih.assign_pool(p, 7, "x", account_ids=[])) == 0
    assert not p.calls, "пустой выбор ушёл в базу — это был бы весь флот"
    assert asyncio.run(ih.assign_pool(p, 7, "x", account_ids=[1, 2])) == 3
    sql, args = p.calls[-1]
    assert "owner_id=$1" in sql and args[0] == 7
    assert "ANY($3::bigint[])" in sql and args[2] == [1, 2]
    asyncio.run(ih.assign_pool(p, 7, "x", only_unpooled=True))
    assert "pool IS NULL OR pool = ''" in p.calls[-1][0]


def test_rename_and_dissolve_are_scoped_to_owner():
    p = _Pool()
    asyncio.run(ih.rename_pool(p, 7, "a", "b"))
    asyncio.run(ih.dissolve_pool(p, 7, "a"))
    for sql, args in p.calls:
        assert "owner_id=$1" in sql and args[0] == 7


# ── Двери в мини-аппе ───────────────────────────────────────────────────────

def test_pool_can_be_managed_from_the_app():
    src = api_source()
    for route in ("/api/miniapp/infra/pools/assign", "/api/miniapp/infra/pools/rename",
                  "/api/miniapp/infra/pools/dissolve", "/api/miniapp/infra/pools/rebalance",
                  "/api/miniapp/infra/audit"):
        assert f'"{route}"' in src, f"нет ручки {route}"
    ui = miniapp_source()
    for call in ("/api/miniapp/infra/pools/assign", "/api/miniapp/infra/pools/rename",
                 "/api/miniapp/infra/pools/dissolve", "/api/miniapp/infra/pools/rebalance",
                 "/api/miniapp/infra/audit"):
        assert call in ui, f"ручку {call} не зовёт ни один экран"


def test_selected_accounts_can_be_put_into_a_pool():
    html = miniapp_html()
    bar = html[html.index('id="accSelBar"'):]
    bar = bar[:bar.index("</div>\n  </div>")]
    assert "openSelectedResourceActions('account', this)" in bar
    assert "guarded(runAccPool)" in js_function("openSelectedResourceActions")
    assert "op: 'set_pool'" in js_function("runAccPool")
    src = api_source()
    assert 'if op == "set_pool":' in src, "массовое действие не умеет пул"


def test_account_card_shows_and_changes_the_pool():
    assert "accPoolRowHtml(a)" in js_function("buildAccDetail")
    assert "accSetPool(" in js_function("accPoolRowHtml")
    detail = re.search(r"async def account_detail\(.*?\n    async def ", api_source(), re.S).group(0)
    assert detail.count("pool\n                   FROM tg_accounts") == 2, \
        "карточка не получает пул аккаунта"


def test_unpooled_row_opens_only_unpooled_accounts():
    """Пустое значение среза = «без фильтра», то есть весь флот."""
    assert "healthGoAccounts('all','','__none__')" in js_function("openInfra")
    where = re.search(r"def _accounts_where\(.*?\n    def |def _accounts_where\(.*?\n    async def ",
                      api_source(), re.S).group(0)
    assert "acc_pool == UNPOOLED" in where
    assert "(pool IS NULL OR pool = '')" in where


# ── Журнал: по-русски ───────────────────────────────────────────────────────

def _own_audit_actions() -> set[str]:
    """Слова, которые продукт кладёт в operation_audit.action сам, а не типом
    операции: собственные действия воркера, бюджет, прогрев, ручной журнал."""
    out: set[str] = set()
    from services import account_budget as ab
    out |= set(ab._COUNTED_ACTIONS) | set(ab._OWN_LIMIT_ACTIONS)
    worker = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
    for m in re.finditer(r'_audit\(\s*pool,\s*[^,]+,\s*([^,\n]+),', worker):
        out |= set(re.findall(r'"([a-z_]+)"', m.group(1)))
    warmer = (ROOT / "services" / "account_warmer.py").read_text(encoding="utf-8")
    out |= set(re.findall(r'_write_op_audit\([^)]*?action="([a-z_]+)"', warmer, re.S))
    for path in list((ROOT / "services").glob("*.py")) + list((ROOT / "bot").rglob("*.py")) \
            + [ROOT / "database" / "db.py"]:
        text = path.read_text(encoding="utf-8")
        out |= set(re.findall(r'record_manual_action\(\s*[^,]+,\s*[^,]+,\s*"([a-z_]+)"', text))
    return out


def test_every_own_audit_action_has_a_russian_name():
    own = _own_audit_actions()
    assert {"warmup", "join", "publish", "account_delete"} <= own, \
        "сборщик не находит заведомые действия — проверка потеряла смысл"
    missing = sorted(a for a in own if a not in ih.ACTION_RU)
    assert not missing, f"без русского названия: {missing}"


def test_operation_types_are_named_from_the_registry():
    assert ih.action_ru("mass_invite") == "Массовый инвайт участников в группы"
    assert not re.search(r"[a-z]{3}", ih.action_ru("warmup"))


def test_target_and_errors_are_translated():
    assert ih.target_ru("day 8") == "день 8"
    assert ih.error_ru("all 3 actions failed") == "Не удалось ни одно из 3 действий"
    assert ih.error_ru("2/5 failed") == "Не удалось 2 из 5 действий"
    assert "подождать 120" in ih.error_ru("A wait of 120 seconds is required")
    assert "PeerFlood" in ih.error_ru("PEER_FLOOD")
    assert ih.error_ru("Канал не найден") == "Канал не найден"
    # Неизвестный английский текст не показывается как причина.
    unknown = ih.error_ru("Something odd happened")
    assert "odd" not in unknown and re.search(r"[А-Яа-я]", unknown)


def test_audit_filters_build_owner_scoped_sql():
    where, args = ih._audit_where(5, result="fail", period="24h", action="join",
                                  account_id=9, pool_name="p", q="50%")
    assert where.startswith("oa.owner_id=$1") and args[0] == 5
    assert "NOT (oa.result = ANY(" in where
    assert args[1] == 1 and "join" in args and 9 in args and "p" in args
    assert r"%50\%%" in args, "% в поиске не экранирован"
    w_all, a_all = ih._audit_where(5, period="all")
    assert "occurred_at" not in w_all and a_all == [5]


def test_audit_row_carries_russian_labels():
    from datetime import datetime, timezone
    r = {"id": 1, "action": "warmup", "target": "day 8", "result": "error",
         "error_msg": "all 4 actions failed", "flood_wait_s": None, "duration_ms": 10,
         "operation_id": None, "account_id": 3, "occurred_at": datetime.now(timezone.utc),
         "first_name": "Елена", "username": None, "phone": None, "pool": "primary"}
    row = ih.audit_row(r)
    assert row["action_ru"] == "Прогрев аккаунта"
    assert row["target"] == "день 8"
    assert row["tone"] == "fail" and row["result_ru"] == "Ошибка"
    assert row["error_ru"] == "Не удалось ни одно из 4 действий"
    assert row["account_label"] == "Елена"


def test_bot_audit_does_not_print_raw_action():
    src = (ROOT / "bot" / "handlers" / "infra_analytics.py").read_text(encoding="utf-8")
    assert "<code>{r['action']}</code>" not in src
    assert "_action_ru(r['action'])" in src
