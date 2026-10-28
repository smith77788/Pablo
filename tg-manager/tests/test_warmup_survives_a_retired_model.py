"""Снятая провайдером модель больше не выключает разогрев молча.

ЧТО ЛОМАЕТСЯ. Модель Groq зафиксирована константой
(`GROQ_WARMUP_MODEL`, по умолчанию llama-3.3-70b-versatile). Провайдеры выводят
модели из обращения по собственному расписанию, и в этот день разогрев,
работавший месяцами, просто замолкает: запрос отвергнут, реплики нет, ход тихо
пропущен. Ни в коде, ни в настройках владельца ничего не менялось — поэтому
причину искать негде, а выглядит это как «флот вступил в чат и молчит».

ЧТО ТЕПЕРЬ. Отказ ИМЕННО ПО МОДЕЛИ лечится на месте: спрашиваем у провайдера
его живой список и берём ближайшую подходящую. Узко намеренно — на неверный
ключ, квоту или сетевой сбой замену искать бессмысленно, и повтор только
удвоил бы нагрузку.
"""
from __future__ import annotations

import asyncio

import pytest

from services import chat_warmup as cw


class _Model:
    def __init__(self, mid):
        self.id = mid


class _Listing:
    def __init__(self, ids):
        self.data = [_Model(i) for i in ids]


class _Msg:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})()


class _Resp:
    def __init__(self, content):
        self.choices = [_Msg(content)]


class _Models:
    def __init__(self, ids, boom=False):
        self._ids, self._boom = ids, boom

    async def list(self):
        if self._boom:
            raise RuntimeError("список недоступен")
        return _Listing(self._ids)


class _Completions:
    def __init__(self, dead: str, error: Exception):
        self.dead, self.error, self.asked = dead, error, []

    async def create(self, *, model, messages, **kw):
        self.asked.append(model)
        if model == self.dead:
            raise self.error
        return _Resp("норм тема, го")


class _Client:
    def __init__(self, dead, error, ids, boom=False):
        self.chat = type("C", (), {})()
        self.chat.completions = _Completions(dead, error)
        self.models = _Models(ids, boom)


def _patch(monkeypatch, client):
    import services.ai_providers as ap

    prov = type("P", (), {"name": "groq", "api_key": "k", "base_url": "u"})()
    monkeypatch.setattr(ap, "configured_providers", lambda: [prov])
    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: client)


# ── Распознавание отказа ─────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "The model `llama-3.3-70b-versatile` has been decommissioned",
    "model_not_found: no such model",
    "This model does not exist",
    "model is no longer available",
])
def test_a_retired_model_is_recognised(text):
    assert cw._is_model_error(RuntimeError(text)), text


@pytest.mark.parametrize("text", [
    "Invalid API key provided",
    "Rate limit reached for requests",
    "Connection reset by peer",
    "insufficient_quota: you exceeded your current quota",
])
def test_other_failures_are_not_model_failures(text):
    assert not cw._is_model_error(RuntimeError(text)), (
        f"«{text}» принято за снятую модель — будет лишний запрос списка "
        f"моделей и второй вызов на каждом ходу"
    )


# ── Лечение на месте ─────────────────────────────────────────────────────────


def test_the_turn_survives_a_decommissioned_model(monkeypatch):
    dead = cw.GROQ_WARMUP_MODEL
    client = _Client(dead, RuntimeError(f"The model `{dead}` has been decommissioned"),
                     ["llama-3.1-8b-instant", "llama-3.4-70b-versatile", "whisper-large"])
    _patch(monkeypatch, client)

    text, err = asyncio.run(cw._groq_complete_detailed("s", "u"))
    assert text == "норм тема, го", (
        f"разогрев замолчал из-за снятой модели: {err}"
    )
    assert client.chat.completions.asked[-1] == "llama-3.4-70b-versatile", (
        "взята не крупная llama — диалог станет заметно хуже"
    )


def test_voice_and_guard_models_are_not_picked(monkeypatch):
    dead = cw.GROQ_WARMUP_MODEL
    client = _Client(dead, RuntimeError(f"model {dead} does not exist"),
                     ["whisper-large-v3", "llama-guard-4", "playai-tts", "gemma2-9b-it"])
    _patch(monkeypatch, client)

    asyncio.run(cw._groq_complete_detailed("s", "u"))
    assert client.chat.completions.asked[-1] == "gemma2-9b-it", (
        "взята модель распознавания речи, озвучки или модерации — текста не будет"
    )


def test_a_bad_key_does_not_trigger_a_model_hunt(monkeypatch):
    dead = cw.GROQ_WARMUP_MODEL
    client = _Client(dead, RuntimeError("Invalid API key provided"), ["llama-3.1-8b"])
    _patch(monkeypatch, client)

    text, err = asyncio.run(cw._groq_complete_detailed("s", "u"))
    assert text is None
    assert "Invalid API key" in err, "причина не дошла до владельца дословно"
    assert client.chat.completions.asked == [dead], (
        "на неверный ключ пошёл второй запрос — удвоенная нагрузка без шанса на успех"
    )


def test_when_no_replacement_exists_the_reason_names_the_model(monkeypatch):
    dead = cw.GROQ_WARMUP_MODEL
    client = _Client(dead, RuntimeError(f"model {dead} decommissioned"), [], boom=True)
    _patch(monkeypatch, client)

    text, err = asyncio.run(cw._groq_complete_detailed("s", "u"))
    assert text is None
    assert dead in err and "недоступна" in err, (
        "владельцу не сказали, что подводит именно модель — а это меняется "
        "одной переменной окружения"
    )
