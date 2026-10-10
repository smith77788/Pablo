"""UX-консистентность: приложение использует фирменные toast()/askConfirm()/
askPrompt()/showInfo() (стилизованные, через Telegram WebApp API). Нативные
alert()/confirm()/prompt() ломают визуальный язык — допустимы ТОЛЬКО как
fallback внутри самих хелперов (window.confirm/window.alert).
"""
from __future__ import annotations

import os
import re

_HTML = os.path.join(os.path.dirname(__file__), "..", "mini_app", "index.html")


def _body_without_helpers() -> str:
    html = open(_HTML, encoding="utf-8").read()
    body = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
    # вырезаем тела хелперов askConfirm/showInfo/askPrompt (там легитимный fallback)
    for fn in ("askConfirm", "showInfo", "askPrompt"):
        body = re.sub(rf"function {fn}\(.*?\n\}}", "", body, count=1, flags=re.DOTALL)
    return body


def test_no_native_dialogs_outside_helpers():
    body = _body_without_helpers()
    offenders = []
    for name in ("alert", "confirm", "prompt"):
        # вызов диалога не как свойство объекта (исключаем tg.showAlert, obj.confirm)
        for m in re.finditer(rf"(?<![\w.])({name})\s*\(", body):
            # исключаем askConfirm/askPrompt (уже вырезаны, но на всякий случай)
            start = max(0, m.start() - 4)
            if body[start:m.start()].endswith(("ask",)):
                continue
            offenders.append(name)
    assert not offenders, (
        f"Нативные диалоги вне хелперов ломают UX-консистентность: {set(offenders)}. "
        "Используйте toast()/askConfirm()/askPrompt()/showInfo()."
    )


def test_prompt_and_info_helpers_exist():
    html = open(_HTML, encoding="utf-8").read()
    assert "function askPrompt(" in html, "askPrompt (стилизованный ввод) должен существовать"
    assert "function showInfo(" in html, "showInfo (стилизованный alert) должен существовать"
