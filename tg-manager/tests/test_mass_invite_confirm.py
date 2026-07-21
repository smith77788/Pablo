"""Массовые операции: подтверждение перед массовым инвайтом (самая баноопасная).

Жёсткий гейт: массовая операция по реальным аккаунтам должна проходить через
осознанное подтверждение (канарейка → проверка → масштаб). У публикатора было
подтверждение/канарейка, у инвайтера — нет, хотя инвайт — самая баноопасная операция.

Фикс: submitMassInvite() перед запуском вызывает askConfirm с тёплым предупреждением;
если лимит на аккаунт не задан — усиленное предупреждение о риске бана.
"""
from __future__ import annotations

import re
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_submit_mass_invite_confirms_before_launch():
    m = re.search(r"async function submitMassInvite\(\)\s*\{(.*?)\n\}", HTML, re.DOTALL)
    assert m, "submitMassInvite не найден"
    body = m.group(1)
    # подтверждение стоит ДО отправки на бэкенд
    i_confirm = body.find("askConfirm(")
    i_api = body.find("/api/miniapp/mass_invite")
    assert i_confirm != -1, "нет подтверждения перед инвайтом"
    assert i_api != -1 and i_confirm < i_api, "подтверждение должно быть ДО запуска"
    # ранний выход, если пользователь отказался
    assert re.search(r"if\s*\(!\(await askConfirm\(.*?\)\)\)\s*return", body, re.DOTALL)


def test_confirm_warns_when_no_per_account_limit():
    m = re.search(r"async function submitMassInvite\(\)\s*\{(.*?)\n\}", HTML, re.DOTALL)
    body = m.group(1)
    # усиленное предупреждение при отсутствии лимита на аккаунт
    assert "Лимит на аккаунт не задан" in body
