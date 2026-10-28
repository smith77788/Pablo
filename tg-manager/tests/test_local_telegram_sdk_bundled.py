"""Telegram SDK раздаётся СВОЕЙ копией с того же origin, что и приложение.

ЧТО БЫЛО. Мини-апп грузил SDK внешним тегом с telegram.org. У многих операторов
(особенно РФ) telegram.org/js/* режется провайдером, хотя сам Telegram работает
через MTProto. Тогда внешний скрипт не открывался, `window.Telegram` не
существовал, и приложение висело пустым — «не грузится» (см. скрин с экраном
«Не загрузился Telegram SDK»).

ФИКС. Официальный telegram-web-app.js положен рядом с index.html и грузится
относительным путём — раз страница уже пришла с нашего сервера, эта копия тоже
доступна (тот же origin), независимо от доступности telegram.org.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MINIAPP = os.path.join(ROOT, "mini_app")
SDK = os.path.join(MINIAPP, "telegram-web-app.js")
INDEX = os.path.join(MINIAPP, "index.html")


def _index() -> str:
    with open(INDEX, encoding="utf-8") as f:
        return f.read()


def test_local_sdk_file_present_and_real():
    assert os.path.isfile(SDK), "нет локальной копии mini_app/telegram-web-app.js"
    body = open(SDK, encoding="utf-8").read()
    # это настоящий SDK: заводит window.Telegram.WebApp и обработчики событий
    assert len(body) > 20000, "файл SDK подозрительно мал — вероятно, заглушка"
    assert "Telegram.WebApp" in body or "WebApp" in body
    assert "eventHandlers" in body or "postEvent" in body or "initParams" in body


def test_index_loads_local_sdk_first():
    html = _index()
    # Первым грузится ЛОКАЛЬНАЯ копия (относительный путь, тот же origin).
    assert re.search(r'<script src="telegram-web-app\.js">', html), \
        "index.html не грузит локальную копию SDK относительным путём"
    # Локальный тег идёт РАНЬШЕ внешнего telegram.org (если тот вообще есть).
    i_local = html.find('src="telegram-web-app.js"')
    i_cdn = html.find("telegram.org/js/telegram-web-app.js")
    assert i_local != -1
    # внешний остаётся только как фолбэк в ранней сетке безопасности — ПОСЛЕ локального
    assert i_cdn == -1 or i_local < i_cdn, \
        "внешний telegram.org грузится раньше локальной копии — при блокировке провайдером снова повиснет"


def test_cdn_fallback_is_guarded_not_primary():
    """telegram.org допустим ТОЛЬКО как запасной путь при отсутствии SDK, а не как
    основной блокирующий <script src> в <head>."""
    html = _index()
    # нет прямого блокирующего внешнего тега (это и была причина зависания)
    assert '<script src="https://telegram.org/js/telegram-web-app.js">' not in html, \
        "остался прямой внешний тег SDK — при блокировке telegram.org приложение повиснет"
