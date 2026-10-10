"""Авто-воронки: шаги заводятся в мини-аппе, и видно, где стоят люди.

Экран шагов воронки показывал бота, сегмент, статус и список шагов — и на
пустом списке писал «Добавьте шаги через Telegram-бота». Это было правдой:
шаги заводились только из бота, в мини-аппе их нельзя было ни добавить, ни
поправить, ни удалить. Движение людей по воронке тоже нигде не показывалось,
хотя auto_funnel_runs хранит и статус, и номер следующего шага: понять, на
каком шаге люди застревают, было нельзя.

Отдельно тест держит удаление шага: номера обязаны перенумеровываться подряд,
а активные прохождения — сдвигаться. Дырка в нумерации оставила бы людей на
шаге, которого больше нет, и цепочка встала бы молча.
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

def test_step_routes_exist():
    for r in ("POST /api/miniapp/auto_funnel/{funnel_id}/step",
              "PUT /api/miniapp/auto_funnel/{funnel_id}/step/{step_id}",
              "DELETE /api/miniapp/auto_funnel/{funnel_id}/step/{step_id}"):
        assert r in SNAPSHOT, f"нет маршрута: {r}"


def test_every_step_route_is_owner_scoped():
    for name in ("auto_funnel_step_add", "auto_funnel_step_update",
                 "auto_funnel_step_delete"):
        assert "_own_funnel(uid, fid)" in _handler(name), \
            f"{name}: шаг правится без проверки владельца воронки"


def test_step_belongs_to_its_funnel():
    upd = _handler("auto_funnel_step_update")
    assert "WHERE id=$1 AND funnel_id=$2" in upd, \
        "можно поправить шаг чужой воронки по номеру"
    dele = _handler("auto_funnel_step_delete")
    assert "WHERE id=$1 AND funnel_id=$2" in dele


def test_step_number_is_taken_under_a_lock():
    h = _handler("auto_funnel_step_add")
    assert "FOR UPDATE" in h, "два одновременных добавления возьмут один номер"
    assert "conn.transaction()" in h
    # FOR UPDATE с агрегатом Postgres не разрешает — блокируется строка воронки
    assert "FROM auto_funnels WHERE id=$1 FOR UPDATE" in h


def test_delete_renumbers_steps_and_moves_people():
    h = _handler("auto_funnel_step_delete")
    assert "SET step_num = step_num - 1" in h, \
        "после удаления остаётся дырка в нумерации"
    assert "auto_funnel_runs" in h and "next_step_num" in h, \
        "люди остаются ждать шага, которого больше нет"


def test_step_fields_are_validated():
    m = re.search(r"def _funnel_step_fields\(body: dict\).*?return \{\"message_text\"",
                  API, re.S)
    assert m, "нет разбора полей шага"
    src = m.group(0)
    assert "Текст сообщения обязателен" in src
    assert "max_val=24 * 365" in src, "задержка без верхней границы"
    assert "tg://" in src, "ссылка кнопки не проверяется"
    assert "btn_text and not btn_url" in src, "кнопка без ссылки проходит"


def test_detail_reports_where_people_are():
    h = _handler("auto_funnel_detail")
    assert "auto_funnel_runs" in h, "движение людей по воронке не отдаётся"
    assert "waiting_on_step" in h and "by_status" in h


# ── экран ────────────────────────────────────────────────────────────────────

def test_screen_no_longer_sends_the_owner_to_the_bot():
    assert "Добавьте шаги через Telegram-бота" not in HTML
    body = _fn("openFunnelSteps")
    assert "afStepAdd(" in body, "шаг нельзя добавить с экрана"
    assert "afStepEdit(" in body, "шаг нельзя открыть и поправить"


def test_screen_shows_the_run_stats():
    body = _fn("openFunnelSteps")
    assert "waiting_on_step" in body, "не видно, на каком шаге стоят люди"
    assert "дошли до конца" in body


def test_step_form_requires_text_and_pairs_the_button():
    form = _fn("_afStepForm")
    assert "Текст сообщения обязателен" in form
    assert "button_url" in form and "button_text" in form


def test_delete_asks_before_removing_a_step():
    body = _fn("afStepEdit")
    assert "askConfirm(" in body, "шаг удаляется без подтверждения"
    assert "method:'DELETE'" in body


def test_segment_labels_are_full_russian_words():
    assert "AF_SEG_RU" in HTML
    assert "Новые за 7 дней" in HTML, "сегмент сокращён до «Новые 7д»"


def test_errors_and_empty_states_lead_somewhere():
    for name in ("openAutoFunnels", "openFunnelSteps"):
        assert re.search(r"errHtml\(errRu\(e\)\s*,", _fn(name)), \
            f"{name}: ошибка без «Повторить»"
    assert "openAutofunnelModal()" in _fn("openAutoFunnels"), \
        "«Воронок нет» без кнопки создания"
