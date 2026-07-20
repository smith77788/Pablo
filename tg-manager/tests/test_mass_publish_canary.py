"""Ядро, шаг 4: канарейка + подтверждение для массовой публикации.

Раньше «Опубликовать во все каналы» било СРАЗУ по всем каналам, одним тапом, без
подтверждения и без возможности протестировать — ровно тот необратимый mass-риск,
о котором правило прод-безопасности (канарейка перед массой). Backend уже умел
channel_ids (подмножество), но UI его не использовал.

Фикс: поле «🐤 Тест на N каналов» → публикация в первые N (channel_ids); полная
публикация во ВСЕ каналы требует подтверждения (необратимо).
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from services import mini_app_api


def _api_src() -> str:
    return inspect.getsource(mini_app_api)


def _index() -> str:
    return (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_backend_supports_channel_subset_scoped():
    src = _api_src()
    m = re.search(r"async def mass_publish\(.*?\n(.*?)\n    app\.router", src, re.DOTALL)
    assert m, "mass_publish не найден"
    body = m.group(1)
    # подмножество каналов поддержано, считается по длине, идёт в params
    assert "channel_ids" in body
    assert "total = len(channel_ids)" in body
    assert 'params["channel_ids"] = channel_ids' in body


def test_form_has_canary_field():
    html = _index()
    assert 'id="mpCanary"' in html, "нет поля канарейки"
    assert "тест на N каналов" in html


def test_send_uses_canary_and_confirms_full_blast():
    html = _index()
    m = re.search(r"async function sendMassPublish\(\)\s*\{(.*?)\n\}", html, re.DOTALL)
    assert m, "sendMassPublish не найдена"
    body = m.group(1)
    # канарейка: берём первые N каналов и шлём channel_ids
    assert "mpCanary" in body
    assert "slice(0, canary)" in body
    assert "payload.channel_ids = channel_ids" in body
    # полная публикация во все — только после подтверждения (необратимо)
    assert "askConfirm(" in body
    # подтверждение именно на ветке «во все» (canary==0)
    assert "canary > 0" in body


def test_open_resets_canary():
    html = _index()
    m = re.search(r"function openMassPub\(\)\s*\{(.*?)\n\}", html, re.DOTALL)
    assert m and "mpCanary" in m.group(1) and "'0'" in m.group(1), (
        "openMassPub должен сбрасывать канарейку в 0"
    )
