"""A/B эксперименты бота: экран не только показывает, но и управляет.

Эксперимент в боте можно было запустить, поставить на паузу и объявить
победителя (bot/handlers/experiments.py), а в мини-аппе тот же эксперимент был
витриной: три числа, список вариантов «Показов: N · CR: x%» и одна кнопка
«Удалить». Статус показывался подписью и не менялся, winner_variant_id
читался, но назначить победителя было нечем, а текст варианта вообще нигде не
был виден — владелец не мог понять, какой из них какой.

Отдельно тест держит две вещи, на которых легко ошибиться:
 • победителем нельзя назначить вариант из ЧУЖОГО эксперимента;
 • нельзя запустить второй активный эксперимент того же типа на том же боте —
   бот берёт один (get_active_experiment ... LIMIT 1), второй молча не
   исполнялся бы.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
API = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
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


# ── маршруты ─────────────────────────────────────────────────────────────────

def test_lifecycle_routes_exist():
    for route in ("POST /api/miniapp/experiment/{exp_id}/status",
                  "POST /api/miniapp/experiment/{exp_id}/winner"):
        assert route in SNAPSHOT, f"нет маршрута: {route}"


def test_status_handler_validates_and_scopes():
    h = _handler("experiment_set_status")
    assert "_own_experiment" in h, "статус меняется без проверки владельца"
    assert "_EXP_STATUSES" in h, "статус принимается какой угодно"
    assert "409" in h, "нет отказа при втором активном эксперименте того же типа"
    assert "set_experiment_status" in h, "статус пишется мимо общей функции"


def test_only_one_active_experiment_per_bot_and_type():
    h = _handler("experiment_set_status")
    m = re.search(r"SELECT id, name FROM experiments(.*?)\"\"\"", h, re.S)
    assert m, "нет запроса на конфликтующий активный эксперимент"
    sql = m.group(1)
    assert "bot_id=$1" in sql and "experiment_type=$2" in sql
    assert "status='active'" in sql and "id<>$3" in sql


def test_winner_handler_checks_the_variant_belongs_to_the_experiment():
    h = _handler("experiment_set_winner")
    assert "_own_experiment" in h, "победитель ставится без проверки владельца"
    assert "FROM experiment_variants WHERE id=$1 AND experiment_id=$2" in h, \
        "победителем можно назначить вариант чужого эксперимента"
    assert "status='completed'" in h and "winner_variant_id=$2" in h


def test_detail_returns_a_significance_verdict():
    h = _handler("experiment_detail")
    assert "ab_engine" in h and "pick_winner" in h, \
        "экран показывает конверсию без оценки достоверности"
    assert '"verdict"' in h and '"min_sample"' in h
    assert "e.bot_id" in h, "из эксперимента не открыть его бота"


# ── экран ────────────────────────────────────────────────────────────────────

def test_detail_screen_can_run_pause_and_finish():
    body = _fn("openExpDetail")
    for call in ("expSetStatus(", "expPickWinner("):
        assert call in body, f"на экране нет управления: {call}"
    assert "'active'" in body and "'paused'" in body
    assert "function expSetStatus(" in HTML or "async function expSetStatus(" in HTML


def test_variant_row_shows_its_text_and_opens_actions():
    body = _fn("openExpDetail")
    assert "v.content" in body, "текст варианта нигде не виден — варианты неразличимы"
    assert "openExpVariant(" in body, "строка варианта ничего не открывает"
    v = _fn("openExpVariant")
    assert "expSetWinner(" in v


def test_verdict_block_is_honest_about_small_samples():
    v = _fn("_expVerdict")
    assert "vd.confident" in v, "отрыв объявляется победой без проверки значимости"
    assert "minS" in v, "не сказано, сколько показов нужно для вывода"
    assert "vd.leader" in v


def test_detail_screen_offers_a_retry_on_error():
    body = _fn("openExpDetail")
    assert re.search(r"errHtml\(errRu\(e\)\s*,", body), "ошибка без «Повторить»"
    lst = _fn("openExperiments")
    assert re.search(r"errHtml\(errRu\(e\)\s*,", lst), \
        "список экспериментов: ошибка без «Повторить»"


def test_empty_list_offers_to_create():
    lst = _fn("openExperiments")
    assert "openExpModal()" in lst, "«Нет экспериментов» — тупик без действия"


def test_actions_confirm_before_changing_state():
    for name in ("expSetStatus", "expSetWinner"):
        assert "askConfirm(" in _fn(name), f"{name}: состояние меняется без подтверждения"


def test_post_bodies_are_json_strings():
    """opts.body уходит в fetch как есть — объект превратился бы в [object Object]."""
    for name in ("expSetStatus", "expSetWinner"):
        body = _fn(name)
        m = re.search(r"body:\s*([^,}]+)", body)
        assert m and "JSON.stringify" in m.group(1), f"{name}: тело запроса не JSON"
