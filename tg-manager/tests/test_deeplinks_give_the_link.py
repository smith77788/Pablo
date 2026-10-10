"""Deep Link: экран наконец отдаёт саму ссылку.

Ссылка существует ради того, чтобы её кому-то отдать. Экран показывал
название, «?start=promo», счётчик кликов и кнопку удаления — всё, кроме
ссылки. Собрать её было не из чего: запрос не возвращал имени бота, а без
имени t.me/<бот>?start=… не построить.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
API = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()


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
    m = re.search(r"\n    async def " + re.escape(name) + r"\(request", API)
    assert m, f"хендлер {name} не найден"
    return API[m.start():API.find("\n    async def ", m.end())]


def test_backend_returns_the_bot_name():
    h = _handler("bot_deeplinks")
    assert '"bot_username"' in h, "без имени бота ссылку не собрать"
    assert "FROM managed_bots WHERE bot_id=$1 AND added_by=$2" in h, "чужой бот"


def test_link_is_built_and_copyable():
    u = _fn("dlUrl")
    assert "https://t.me/" in u and "?start=" in u, u
    assert "copyToClipboard" in _fn("dlCopy")
    f = _fn("loadDeeplinks")
    assert "dlCopy(" in f, "строка ссылки не копируется по тапу"
    assert "t.me/" in f, "ссылка не показана в строке"


def test_link_can_be_forwarded():
    s = _fn("dlShare")
    assert "t.me/share/url" in s, s
    assert "openTelegramLink" in s


def test_missing_bot_name_is_said_plainly():
    """Без имени бота ссылку не собрать — и об этом говорят, а не молчат."""
    f = _fn("loadDeeplinks")
    assert "У бота не записано имя" in f, f[:400]
    assert "ссылку не собрать" in _fn("dlCopy")


def test_delete_button_does_not_fire_on_row_tap():
    """Строка копирует, кнопки внутри неё — свои действия."""
    f = _fn("loadDeeplinks")
    assert "event.stopPropagation()" in f, f


def test_empty_state_explains_what_a_link_is_for():
    f = _fn("loadDeeplinks")
    i = f.index("Ссылок пока нет")
    chunk = f[i:f.index("));", i) + 3]
    assert "откуда" in chunk, chunk


def test_start_param_never_goes_into_onclick():
    """start_param и название приходят из базы — в onclick это код."""
    f = _fn("loadDeeplinks")
    assert "dlCopy('" not in f and "dlShare('" not in f, f
    assert re.search(r"dlCopy\(\$\{i\}\)", f), f
