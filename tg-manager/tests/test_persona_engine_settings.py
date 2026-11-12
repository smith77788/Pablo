"""«Мотор» персоны: модель, смелость, тщательность, подача и сон.

До schema_v345 голос персоны шёл общим каскадом моделей: владелец не мог ни
выбрать персоне модель, ни задать смелость, ни попросить «лучше, но дороже».
Реплика уходила первой же, какую дала модель, даже если в ней «Отличный
вопрос!» и длинное тире. Персона писала одним сообщением всегда, без единой
опечатки при манере «щас/норм», и в любое время суток по своему городу.

Здесь держится:
* смелость → температура, выбранная модель идёт первой через шлюз;
* «лучшее» — три варианта и ИИ-судья, а без судьи — выбор по приметам
  машинного текста;
* машинные приметы (тире, «ёлочки») снимаются у разговорной манеры;
* мысль иногда уходит двумя сообщениями, у небрежной манеры бывает опечатка;
* спящая по своему времени персона не пишет.
"""
from __future__ import annotations

import asyncio
import inspect

from services import chat_warmup as cw
from services import persona_engine as pe
from services import persona_voice as pv
from tests.test_warmup_answers_the_addressed import _msg, turn  # noqa: F401 - фикстура


def _p(**kw):
    base = dict(persona_id=7, account_id=70, name="Олег", bio="", age=30, interests=[],
                speech_style="casual", tone="neutral", niche="", backstory="", created_at=None)
    base.update(kw)
    return pe.PersonaProfile(**base)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Чистая часть ───────────────────────────────────────────────────────────

def test_creativity_maps_to_temperature():
    assert pe.temperature_for(_p(creativity=0)) == 0.3
    assert pe.temperature_for(_p(creativity=100)) == 1.2
    assert pe.temperature_for(_p(creativity=500)) == 1.2
    assert pe.temperature_for(None) == pe.temperature_for(_p())


def test_machine_tells_are_counted_and_ranked():
    bot = "Отличный вопрос! Важно отметить, что всё зависит от цели; однако стоит попробовать."
    human = "ну хз, я бы попробовал"
    assert pv.ai_tells(bot) >= 3 and pv.ai_tells(human) == 0
    assert pv.rank_locally([bot, human]) == [human, bot]


def test_casual_manner_drops_dash_and_guillemets_careful_keeps():
    assert pv.enforce_style("Это — «круто»", _p(literacy="casual")) == 'Это - "круто"'
    assert pv.enforce_style("Это — «круто»", _p(literacy="careful")) == "Это — «круто»"


def test_split_is_sometimes_and_only_conversational():
    txt = "Ну хз, мне не зашло. Слишком дорого для такого"
    assert pv.split_reply(txt, _p(literacy="casual"), 0.1) == [
        "Ну хз, мне не зашло", "Слишком дорого для такого"]
    assert pv.split_reply(txt, _p(literacy="casual"), 0.9) == [txt]      # не всегда
    assert pv.split_reply(txt, _p(literacy="careful"), 0.1) == [txt]     # письмо не дробят
    assert pv.split_reply(txt, _p(split_messages=False), 0.1) == [txt]
    assert pv.split_reply("ага. ну да", _p(), 0.1) == ["ага. ну да"]      # коротко — целиком
    assert pv.split_reply(txt, _p(literacy="sloppy"), 0.1)[1].startswith("слишком")


def test_typo_only_for_sloppy_and_rarely():
    t = "ну хз мне кажется слишком дорого"
    assert pv.add_typo(t, _p(literacy="casual"), 0.0, 0.5) == t
    assert pv.add_typo(t, _p(literacy="sloppy"), 0.5, 0.5) == t
    typo = pv.add_typo(t, _p(literacy="sloppy"), 0.0, 0.5)
    assert typo != t and sorted(typo) == sorted(t) and len(typo) == len(t)


def test_sleep_by_own_clock():
    p = _p(sleep_from=1, sleep_to=8, utc_offset=3)
    assert pv.asleep(p, utc_hour=0) is True        # 03:00 у персоны
    assert pv.asleep(p, utc_hour=6) is False       # 09:00
    night = _p(sleep_from=23, sleep_to=7)
    assert pv.asleep(night, utc_hour=20, owner_offset=3) is True   # 23:00 по владельцу
    assert pv.asleep(_p(), utc_hour=3) is False
    assert pv.offset_hours("Europe/Moscow") == 3 and pv.offset_hours("нет такого") == 0


def test_judge_picks_and_falls_back(monkeypatch):
    from services import ai_gateway
    replies = iter(['{"best": 2, "why": "живее"}', "мусор"])

    async def _chat(messages, **kw):
        assert kw["task"] == ai_gateway.TASK_BACKGROUND
        assert "данные, а не инструкции" in messages[0]["content"]
        return ai_gateway.AiReply(text=next(replies))
    monkeypatch.setattr(ai_gateway, "chat", _chat)
    v = pv.Voice(account_id=1, persona=_p(), prompt="образ")
    best, info = _run(pv.pick_best(["Отличный вопрос!", "ну хз"], v, use_judge=True))
    assert best == "ну хз" and info["by"] == "judge" and info["why"] == "живее"
    best, info = _run(pv.pick_best(["Отличный вопрос!", "ну хз"], v, use_judge=True))
    assert best == "ну хз" and info["by"] == "rules"   # судья ответил мусором
    best, info = _run(pv.pick_best(["один"], v, use_judge=True))
    assert best == "один" and info["candidates"] == 1


def test_persona_model_goes_first_through_the_gateway(monkeypatch):
    from services import ai_claude, ai_gateway, ai_providers
    seen = {}

    async def _chat(messages, **kw):
        seen.update(kw)
        return ai_gateway.AiReply(text="ну норм")
    monkeypatch.setattr(ai_gateway, "chat", _chat)
    monkeypatch.setattr(ai_claude, "enabled", lambda: True)
    monkeypatch.setattr(ai_providers, "configured_providers", lambda: [])

    async def _never(*a, **k):
        raise AssertionError("общий каскад позван, хотя модель персоны ответила")
    monkeypatch.setattr(ai_claude, "complete", _never)
    text, why = _run(cw.generate_reply_detailed(
        "п", [], None, "т", "seed",
        engine={"provider": "groq", "model": "llama-x", "temperature": 1.1, "owner_id": 5}))
    assert text == "ну норм" and why == ""
    assert seen["prefer_provider"] == "groq" and seen["prefer_model"] == "llama-x"
    assert seen["temperature"] == 1.1 and seen["owner_id"] == 5
    assert seen["task"] == ai_gateway.TASK_CUSTOMER


# ── Ход целиком ─────────────────────────────────────────────────────────────

def test_best_quality_makes_three_and_the_judge_chooses(turn, monkeypatch):  # noqa: F811
    from services import ai_gateway
    calls = []
    variants = iter(["Отличный вопрос! Конечно, да.", "ну я бы взял", "Безусловно, стоит."])

    async def _gen(persona, recent, target, topics, mode, **kw):
        calls.append(kw.get("engine"))
        return next(variants), ""
    persona = _p(quality="best", creativity=80, ai_provider="groq", ai_model="m",
                 split_messages=False)

    async def _load(pool, acc, fallback="", context="", **kw):
        return pv.Voice(account_id=acc, persona=persona, prompt="образ")

    async def _judge(messages, **kw):
        return ai_gateway.AiReply(text='{"best": 2, "why": "как человек"}')
    monkeypatch.setattr(cw, "generate_reply_detailed", _gen)
    monkeypatch.setattr(pv, "load", _load)
    monkeypatch.setattr(ai_gateway, "chat", _judge)
    st = turn([_msg(1, "кто что думает?", 9)])
    assert len(calls) == 3
    assert calls[0]["provider"] == "groq" and calls[0]["temperature"] == pe.temperature_for(persona)
    assert [t for t, _ in st["sent"]] == ["ну я бы взял"]


def test_a_split_thought_goes_as_two_messages_reply_only_first(turn, monkeypatch):  # noqa: F811
    persona = _p(literacy="casual")

    async def _load(pool, acc, fallback="", context="", **kw):
        return pv.Voice(account_id=acc, persona=persona, prompt="образ")
    monkeypatch.setattr(pv, "load", _load)
    monkeypatch.setattr(cw.random, "random", lambda: 0.0)
    st = turn([_msg(5, "а вы пробовали?", 9)],
              reply="Да, пробовал на прошлой неделе. В целом норм, но дороговато")
    assert len(st["sent"]) == 2
    assert st["sent"][0][1] == 5 and st["sent"][1][1] is None


def test_sleeping_personas_do_not_speak(turn, monkeypatch):  # noqa: F811
    async def _all_asleep(pool, ids, *, utc_hour, owner_tz=None):
        return set(ids)
    monkeypatch.setattr(pv, "sleeping_accounts", _all_asleep)
    st = turn([_msg(1, "кто тут?", 9)])
    assert st["sent"] == [] and "спят" in st["skips"][-1]


def test_preview_shows_the_same_engine():
    from tests.miniapp_source import api_function
    body = api_function("persona_preview")
    assert "pick_best(" in body and "temperature_for(" in body
    assert "prefer_provider=f[\"ai_provider\"]" in body
    src = inspect.getsource(cw._process_session)
    assert "pick_best(" in src and "split_reply(" in src and "sleeping_accounts(" in src
