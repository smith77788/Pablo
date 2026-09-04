"""Импорт перестал сажать всю партию за один выход в сеть.

Разрыв. Поле прокси при импорте одно на всю партию: выбранный прокси
записывался КАЖДОМУ аккаунту, а если прокси не выбран — все получали NULL и
уходили в сеть с одного реального IP сервера. Двадцать аккаунтов, добавленных
одним действием, оказывались за одним выходом.

Это противоречило собственному стандарту продукта: `proxy_selector`
.audit_proxy_isolation считает нормой один аккаунт на один IP и показывает
общий IP как риск бана. То есть продукт создавал нарушение на первом же шаге,
а потом честно докладывал о нём в отдельном аудите, который пользователь мог
не открыть никогда.

Общий IP — самый сильный признак связи аккаунтов: отпечаток устройства и
api_id импорт уже рандомизирует по аккаунту, а выход в сеть был общий.
"""
from __future__ import annotations

import ast
import pathlib

from services.proxy_balancer import (
    isolation_note, isolation_summary, plan_distribution,
)

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_IMPORTER = (_ROOT / "services" / "session_importer.py").read_text(encoding="utf-8")
_API = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
_UI = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def _func_src(src: str, name: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"функция {name} не найдена")


# ── Раскладка ──────────────────────────────────────────────────────────────

def test_batch_is_spread_one_account_per_proxy():
    """Главное: пять аккаунтов на пять свободных прокси — по одному."""
    plan = plan_distribution([(1, 0), (2, 0), (3, 0), (4, 0), (5, 0)], 5)
    assert sorted(plan) == [1, 2, 3, 4, 5]


def test_already_loaded_proxies_are_taken_last():
    """Свободный прокси должен разбираться раньше того, на котором уже
    сидят аккаунты, иначе раскладка усугубляет существующую скученность."""
    plan = plan_distribution([(1, 3), (2, 0)], 2)
    assert plan[0] == 2


def test_load_evens_out_when_there_are_fewer_proxies():
    plan = plan_distribution([(1, 0), (2, 0)], 5)
    assert plan.count(1) + plan.count(2) == 5
    assert abs(plan.count(1) - plan.count(2)) <= 1


def test_no_proxies_means_honest_none_not_a_made_up_binding():
    assert plan_distribution([], 3) == [None, None, None]


def test_distribution_is_reproducible():
    a = plan_distribution([(7, 0), (2, 0), (5, 0)], 3)
    b = plan_distribution([(2, 0), (5, 0), (7, 0)], 3)
    assert a == b


def test_zero_accounts_needs_no_plan():
    assert plan_distribution([(1, 0)], 0) == []


def test_broken_rows_do_not_break_the_plan():
    plan = plan_distribution([(1, 0), (None, 5), "мусор"], 2)
    assert plan == [1, 1]


# ── Честный итог ───────────────────────────────────────────────────────────

def test_summary_counts_accounts_that_share_an_exit():
    s = isolation_summary([1, 1, 2, None])
    assert s["assigned"] == 3 and s["without_proxy"] == 1
    assert s["proxies_used"] == 2 and s["max_per_proxy"] == 2


def test_summary_includes_accounts_that_were_already_there():
    """Прокси с четырьмя аккаунтами и одним новым — это пять за одним IP,
    а не один."""
    s = isolation_summary([1], {1: 4})
    assert s["max_per_proxy"] == 5


def test_perfect_isolation_is_silent():
    assert isolation_note(isolation_summary([1, 2, 3])) is None


def test_crowded_proxy_is_called_out_with_the_reason():
    note = isolation_note(isolation_summary([1, 1, 1]))
    assert note and "3" in note
    assert "IP" in note


def test_accounts_without_a_proxy_are_called_out():
    note = isolation_note(isolation_summary([None, None]))
    assert note and "без прокси" in note


def test_a_single_naked_account_is_worded_in_singular():
    note = isolation_note(isolation_summary([None]))
    assert note and "1 аккаунт без прокси" in note


def test_empty_summary_is_not_an_alarm():
    assert isolation_note({}) is None
    assert isolation_note(isolation_summary([])) is None


# ── Проводка ───────────────────────────────────────────────────────────────

def test_import_distributes_instead_of_reusing_one_proxy():
    src = _func_src(_IMPORTER, "import_sessions")
    assert "plan_distribution" in src
    # Живость прокси учитываем: раскладывать по мёртвому — то же самое, что
    # оставить аккаунт без сети.
    assert "is_alive" in src
    # Раскладка считается по прошедшим проверку, а не по числу строк.
    assert "_ok_count" in src


def test_rejected_lines_do_not_consume_proxy_slots():
    """Дубли и битые строки не должны «занимать» прокси: иначе половина пула
    уходит в никуда, а живые аккаунты садятся на общий выход."""
    src = _func_src(_IMPORTER, "import_sessions")
    assert "_plan_pos += 1" in src
    body = src[src.index("_plan_pos += 1"):]
    # Сдвиг делается только после успешной записи аккаунта.
    assert "imported += 1" in src[:src.index("_plan_pos += 1")]


def test_import_reports_isolation_to_the_user():
    src = _func_src(_IMPORTER, "import_sessions")
    assert '"isolation"' in src and '"isolation_note"' in src
    assert "isolation_note" in _UI
    assert "r.isolation" in _UI


def test_single_account_logins_also_get_a_proxy():
    """Файл сессии, tdata, вход по номеру и QR идут через один путь: там тоже
    нельзя молча оставлять аккаунт на общем IP сервера."""
    src = _func_src(_API, "_persist_login")
    assert "plan_distribution" in src
    assert "is_alive" in src


def test_explicit_choice_is_still_honoured():
    """Явный выбор пользователя не подменяем — только считаем честно."""
    src = _func_src(_IMPORTER, "import_sessions")
    assert "[proxy_id] * _ok_count" in src


def test_ui_no_longer_calls_the_default_no_proxy():
    assert "Авто — разложить по моим прокси" in _UI
    assert "Без прокси (прямое подключение)" not in _UI


# ── Переселение с мёртвых прокси ───────────────────────────────────────────
# Сторож прокси просит «переназначьте аккаунты на рабочий», но единственное
# автоматическое переназначение (failover) берёт ТОЛЬКО прокси, заранее
# помеченные резервными. У пользователя, который просто держит несколько живых
# прокси, оно не делало ничего, и аккаунты стояли намертво.

def test_stranded_accounts_move_to_the_least_loaded_live_proxy():
    from services.proxy_balancer import plan_evacuation

    res = plan_evacuation([10, 11], [(1, 5), (2, 0)])
    assert res["moves"][0] == (10, 2)      # сначала свободный
    assert not res["stranded"]


def test_nothing_to_move_is_not_an_error():
    from services.proxy_balancer import plan_evacuation

    assert plan_evacuation([], [(1, 0)]) == {"moves": [], "stranded": []}


def test_without_live_proxies_accounts_stay_and_are_reported():
    """Молча «переселить в никуда» нельзя: пользователь должен узнать, что
    живых прокси не хватает."""
    from services.proxy_balancer import plan_evacuation

    res = plan_evacuation([10, 11], [])
    assert res["moves"] == [] and res["stranded"] == [10, 11]


def test_evacuation_endpoint_targets_only_confirmed_dead_proxies():
    src = _func_src(_API, "proxy_evacuate")
    assert "is_alive IS FALSE" in src
    assert "PROXY_DEAD_STREAK" in src   # одна неудача — не смерть
    assert "owner_id=$1" in src


def test_evacuation_tells_the_truth_about_isolation():
    src = _func_src(_API, "proxy_evacuate")
    assert "isolation_note" in src
    assert "evacuateDeadProxies" in _UI
    assert 'app.router.add_post("/api/miniapp/proxy/evacuate"' in _API
