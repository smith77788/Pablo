"""Регресс-тесты Claude-пути (services/ai_claude) и предпочтения его в
общей точке генерации services/spintax_ai.complete.

SDK не вызывается по сети — anthropic.AsyncAnthropic подменяется фейком, который
фиксирует параметры запроса и возвращает готовое сообщение.
"""

import anthropic
import pytest

import services.ai_claude as ai_claude


# ── Фейковый anthropic-клиент ────────────────────────────────────────────────
class _Block:
    def __init__(self, type_, text=""):
        self.type = type_
        self.text = text


class _Msg:
    def __init__(self, content):
        self.content = content


class _Stream:
    def __init__(self, msg):
        self._msg = msg

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_final_message(self):
        return self._msg


class _Messages:
    def __init__(self, sink):
        self._sink = sink

    def stream(self, **kwargs):
        self._sink["stream_kwargs"] = kwargs
        # thinking-блок (пустой текст) + два текстовых — проверяем склейку/фильтр
        msg = _Msg([_Block("thinking", ""), _Block("text", "Привет"), _Block("text", " мир")])
        return _Stream(msg)


def _fake_client_factory(sink):
    class _Client:
        def __init__(self, **kwargs):
            sink["init_kwargs"] = kwargs
            self.messages = _Messages(sink)

    return _Client


# ── enabled() ────────────────────────────────────────────────────────────────
def test_enabled_reflects_key(monkeypatch):
    monkeypatch.setattr(ai_claude, "_key", lambda name: "" )
    assert ai_claude.enabled() is False
    monkeypatch.setattr(ai_claude, "_key", lambda name: "sk-ant-xxx")
    assert ai_claude.enabled() is True


# ── _extract_text пропускает не-text блоки ───────────────────────────────────
def test_extract_text_skips_thinking():
    msg = _Msg([_Block("thinking", "секрет"), _Block("text", "A"), _Block("text", "B")])
    assert ai_claude._extract_text(msg) == "AB"
    assert ai_claude._extract_text(_Msg([])) == ""


# ── complete() шлёт ровно спецификацию и склеивает текст ─────────────────────
async def test_complete_sends_exact_spec(monkeypatch):
    sink = {}
    monkeypatch.setattr(ai_claude, "_key", lambda name: "sk-ant-test")
    monkeypatch.setattr(ai_claude, "MODEL", "claude-opus-4-8")
    monkeypatch.setattr(ai_claude, "EFFORT", "xhigh")
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _fake_client_factory(sink))

    text = await ai_claude.complete("СИСТЕМА", "ЮЗЕР")

    assert text == "Привет мир"  # thinking-блок пропущен, текст склеен
    kw = sink["stream_kwargs"]
    assert kw["model"] == "claude-opus-4-8"
    assert kw["max_tokens"] == 64000
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "xhigh"}
    assert kw["system"] == "СИСТЕМА"
    assert kw["messages"] == [{"role": "user", "content": "ЮЗЕР"}]
    assert sink["init_kwargs"]["api_key"] == "sk-ant-test"


async def test_complete_raises_without_key(monkeypatch):
    monkeypatch.setattr(ai_claude, "_key", lambda name: "")
    with pytest.raises(RuntimeError):
        await ai_claude.complete("s", "u")


def test_max_tokens_env_override(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MAX_TOKENS", "32000")
    assert ai_claude._max_tokens() == 32000
    monkeypatch.setenv("ANTHROPIC_MAX_TOKENS", "не-число")
    assert ai_claude._max_tokens() == 64000  # битое значение → дефолт


# ── spintax_ai предпочитает Claude, но делает failover ───────────────────────
async def test_spintax_prefers_claude(monkeypatch):
    import services.spintax_ai as spin

    async def fake_claude(system, user):
        return "ОТ CLAUDE"

    monkeypatch.setattr(ai_claude, "enabled", lambda: True)
    monkeypatch.setattr(ai_claude, "complete", fake_claude)
    # если Claude ответил — openai-провайдеры не трогаем
    monkeypatch.setattr(spin, "configured_providers", lambda: (_ for _ in ()).throw(
        AssertionError("не должно вызываться, Claude ответил")))

    assert await spin.complete("s", "u") == "ОТ CLAUDE"


async def test_spintax_failover_when_claude_empty(monkeypatch):
    import services.spintax_ai as spin
    from services.spintax_service import SpintaxServiceError

    async def empty_claude(system, user):
        return "   "  # пусто → failover

    monkeypatch.setattr(ai_claude, "enabled", lambda: True)
    monkeypatch.setattr(ai_claude, "complete", empty_claude)
    monkeypatch.setattr(spin, "configured_providers", lambda: [])  # нет openai-провайдеров

    with pytest.raises(SpintaxServiceError):
        await spin.complete("s", "u")


async def test_spintax_no_ai_configured(monkeypatch):
    import services.spintax_ai as spin
    from services.spintax_service import SpintaxServiceError

    monkeypatch.setattr(ai_claude, "enabled", lambda: False)
    monkeypatch.setattr(spin, "configured_providers", lambda: [])

    with pytest.raises(SpintaxServiceError, match="ANTHROPIC_API_KEY"):
        await spin.complete("s", "u")
