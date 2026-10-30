"""Причина отказа по ветке кампании не уезжает владельцу фразой Python.

Единая кампания («каналы + чаты + боты + ЛС одним запуском») не падает из-за
одной ветки: остальные уходят, а причины копятся в `errors`, и экран честно
говорит «Запущено операций: 2. Не ушло: …». Но в эту строку попадало ДВА вида
текста, которых владелец прочесть не может:

  * ключ вида ресурса — `dm: …`, `channels: …`. Для него это не «личные
    сообщения», а слово, которого нет ни на одном экране;
  * текст встроенного исключения на непредвиденном сбое — `str(exc)[:140]`,
    то есть `invalid literal for int()…` без имени поля и без того, что делать.

Тот же класс, что разобран в `test_errors_the_owner_reads_are_not_python.py`, и
лечится так же: причина пишется ТАМ, ГДЕ ОНА ИЗВЕСТНА (ветка не поставилась), а
не угадывается потом по тексту — фильтр «нет кириллицы» на русском вводе
проходит на сломанном коде, потому что Python вставляет значение в сообщение.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from services import campaign_hub as H

UID = 55012
# Две латинские «слова» подряд — отпечаток фразы Python, а не нашего текста.
_LATIN_PHRASE = re.compile(r"[A-Za-z]{3,}\s+[A-Za-z]{3,}")


class _Pool:
    async def fetchval(self, q, *a):
        return 1

    async def execute(self, q, *a):
        return "OK"

    async def fetchrow(self, q, *a):
        return None

    async def fetch(self, q, *a):
        return []


_SPEC = {
    "content": {"text": "Привет"},
    "targets": {"channels": {"enabled": True}},
}


def _launch(monkeypatch, boom):
    from services import operation_bus as obus

    async def _submit(*a, **k):
        raise boom

    monkeypatch.setattr(obus, "submit", _submit)
    return asyncio.run(H.launch_campaign(_Pool(), UID, dict(_SPEC)))


@pytest.mark.parametrize("boom", [
    ValueError("invalid literal for int() with base 10: 'abc'"),
    KeyError("channel_ids"),
    RuntimeError("connection already closed"),
])
def test_an_unexpected_failure_is_not_shown_as_python(monkeypatch, boom):
    res = _launch(monkeypatch, boom)
    assert res["errors"], "ветка упала, а причины в ответе нет"
    for msg in res["errors"]:
        assert not _LATIN_PHRASE.search(msg), (
            f"владельцу уехала фраза Python: {msg!r}")


def test_the_branch_is_named_the_way_the_screen_names_it(monkeypatch):
    res = _launch(monkeypatch, RuntimeError("connection already closed"))
    assert res["errors"][0].startswith("каналы:"), (
        f"ветка названа ключом, а не по-русски: {res['errors'][0]!r}")


@pytest.mark.parametrize("kind", list(H.ALL_KINDS))
def test_every_kind_has_a_russian_name(kind):
    label = H.KIND_LABEL.get(kind)
    assert label and re.search(r"[а-яё]", label), (
        f"у вида {kind} нет русского названия: причина отказа уедет ключом")


def test_a_plan_refusal_keeps_its_own_russian_text(monkeypatch):
    """Наше собственное сообщение (CampaignSpecError) не должно подменяться."""
    res = _launch(monkeypatch, H.CampaignSpecError("Список получателей ДМ пуст"))
    assert "Список получателей ДМ пуст" in res["errors"][0]
