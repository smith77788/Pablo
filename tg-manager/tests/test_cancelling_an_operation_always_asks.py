"""Окно операций: отмена всегда спрашивает, повтор предлагается одинаково.

Найдено при проходе по окну операций (03.11.2026):

1. «Отмена» отменяла операцию с одного касания из двух мест: мелкая ссылка в
   строке списка операций — рядом с «▶ Возобновить», промах пальцем — и
   «отменить» у запланированной публикации. Подтверждение в `cancelOp` было
   флагом, и эти два входа его не передавали, а отмена необратима: «Вернуть её
   в очередь будет нельзя». Теперь подтверждение — поведение по умолчанию.
2. «↺ Повторить» в карточке операции стоял только у проваленной. У отменённой,
   недоведённой и завершённой с ошибками строки списка кнопку показывали
   (`opCanRetry`), а карточка той же операции — нет. Теперь карточка зовёт то же
   правило, а сервер отдаёт ей тот же счётчик упавших целей (`err_cnt`).

Отмена проверяется ПОВЕДЕНИЕМ: настоящая функция исполняется в Node с
заглушками, и без согласия запрос на отмену не должен уйти.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from tests.miniapp_source import api_function, js_function, miniapp_source

NODE = shutil.which("node")


def _run_cancel(call: str, answer: bool) -> list[str]:
    src = js_function("cancelOp")
    prog = f"""
const sent = [];
async function askConfirm(m) {{ return {json.dumps(answer)}; }}
async function api(u, o) {{ sent.push((o && o.method || 'GET') + ' ' + u); return {{}}; }}
function toast() {{}} function _opProgressMarkCancelled() {{}} function loadOps() {{}}
function errRu(e) {{ return String(e); }}
{src}
(async () => {{ await {call}; console.log(JSON.stringify(sent)); }})();
"""
    out = subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not NODE, reason="нужен node")
def test_cancel_without_flag_asks_and_respects_no():
    assert _run_cancel("cancelOp(7)", answer=False) == [], (
        "отмена без флага ушла без подтверждения")
    assert _run_cancel("cancelOp(7)", answer=True) == ["POST /api/miniapp/operation/7/cancel"]


@pytest.mark.skipif(not NODE, reason="нужен node")
def test_cancel_with_explicit_true_still_asks():
    assert _run_cancel("cancelOp(7, true)", answer=False) == []


def test_no_entry_point_turns_the_question_off():
    # Отключить вопрос можно только явным false — и пока такого входа нет.
    calls = re.findall(r"cancelOp\(([^)]*)\)", miniapp_source())
    off = [c for c in calls if c.split(",")[-1].strip() == "false"]
    assert not off, f"вход, отменяющий операцию без вопроса: {off}"


def test_detail_offers_retry_by_the_same_rule_as_the_list():
    body = js_function("openOpDetail")
    assert "opCanRetry(o)" in body, "карточка решает про повтор своим правилом"
    assert "o.status==='failed') {\n      html += `<div style=\"padding:0 16px\"><button class=\"btn btn-p\" onclick=\"retryOp" not in body


def test_detail_endpoint_returns_the_failed_targets_count():
    # opCanRetry для «готово с ошибками» смотрит err_cnt — без него карточка
    # такую операцию повторить не предложит.
    assert "AS err_cnt" in api_function("operation_status")
