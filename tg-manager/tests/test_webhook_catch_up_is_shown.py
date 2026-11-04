"""Включение получателя после выключения сбоями говорит, что пропущенное догоняется.

Сервер при таком включении возвращает в очередь недоставленные события за
последние дни и отдаёт их число в `endpoint.requeued` (services/webhooks.py).
Без подписи владелец видел только «🟢 Включён» и не знал, придут ли заказы и
оплаты за время простоя. Проверяется поведение: настоящие функции мини-аппа
исполняются в Node с заглушками.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests.miniapp_source import js_function

NODE = shutil.which("node")


def _toasts(fn: str, answer: dict, on: bool) -> list[str]:
    prog = f"""
const shown = [];
function toast(m) {{ shown.push(m); }}
async function api() {{ return {json.dumps(answer)}; }}
function openWebhook() {{}} function openCrm() {{}} function back() {{}}
function errRu(e) {{ return String(e); }}
{js_function("plural")}
{js_function("igEnabledToast")}
{js_function(fn)}
(async () => {{ await {fn}(5, {json.dumps(on)}); console.log(JSON.stringify(shown)); }})();
"""
    out = subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not NODE, reason="нужен node")
@pytest.mark.parametrize("fn", ["igToggleWebhook", "igToggleCrm"])
def test_requeued_events_are_announced(fn):
    (msg,) = _toasts(fn, {"ok": True, "endpoint": {"id": 5, "requeued": 3}}, True)
    assert "догоняем 3 события" in msg, msg


@pytest.mark.skipif(not NODE, reason="нужен node")
@pytest.mark.parametrize("fn", ["igToggleWebhook", "igToggleCrm"])
def test_plain_enable_and_disable_say_nothing_extra(fn):
    for answer, on in (({"ok": True, "endpoint": {"id": 5, "requeued": 0}}, True),
                       ({"ok": True, "endpoint": {"id": 5}}, True),
                       ({"ok": True, "endpoint": {"id": 5, "requeued": 7}}, False)):
        (msg,) = _toasts(fn, answer, on)
        assert "догоняем" not in msg, msg


@pytest.mark.skipif(not NODE, reason="нужен node")
def test_plural_form_for_many():
    (msg,) = _toasts("igToggleWebhook", {"endpoint": {"requeued": 25}}, True)
    assert "догоняем 25 событий" in msg, msg
