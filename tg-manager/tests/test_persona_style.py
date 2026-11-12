"""Манера персоны замеряется по образцам и исполняется над ответом модели.

Словами («пиши коротко и небрежно») модель управляется плохо: её «коротко» —
два предложения. Цифры с образцов живого человека — длина, строчная буква,
точка в конце, смайлики, словечки — идут в промпт правилами и затем
исполняются над ответом. Образцы можно взять из СВОИХ переписок владельца.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from services import persona_engine as pe
from services import persona_style as ps
from services import persona_voice as pv

_LOW = ["ну хз, мне норм", "короче глянь сам", "ну такое себе", "кто пробовал вообще?",
        "короче не знаю", "ну да, бывает"]


def test_fingerprint_measures_what_a_human_does():
    fp = ps.fingerprint(_LOW)
    assert fp["n"] == 6 and fp["lower_start"] == 1.0 and fp["end_period"] == 0.0
    assert fp["emoji"] == 0.0 and fp["len_max"] <= 25
    assert "короче" in fp["pet_words"]
    assert "короче" in fp["openers"]


def test_two_samples_are_not_a_manner():
    assert ps.fingerprint(["ну", "да"]) is None
    assert ps.rules(None) == "" and ps.summary(None) == ""


def test_rules_speak_in_numbers_and_in_russian():
    r = ps.rules(ps.fingerprint(_LOW))
    assert "не длиннее" in r and "Со строчной буквы начинаешь почти всегда" in r
    assert "Точку в конце ставишь почти никогда" in r and "короче" in r


def test_conform_cuts_a_lecture_to_the_measured_length_and_manner():
    fp = ps.fingerprint(_LOW)
    long = ("Да, отличный вопрос. Я думаю, что это зависит от многих факторов. "
            "Во-первых, стоит учитывать рынок. Во-вторых, личный опыт.")
    out = ps.conform(long, fp)
    assert len(out) <= int(fp["len_max"] * 1.5)
    assert out and out[0].islower() and not out.endswith(".")


def test_conform_keeps_a_name_at_the_start_and_never_empties():
    fp = ps.fingerprint(_LOW)
    assert ps.conform("Казань норм", fp) == "Казань норм"
    assert ps.conform("ОК", fp) == "ОК"
    assert ps.conform("😀", ps.fingerprint(_LOW)) == "😀"   # пустым не делаем


def test_pick_samples_drops_links_numbers_and_repeats():
    got = ps.pick_samples(["ок", "смотри https://x.ru", "мой номер 89991234567",
                           "ну давай", "Ну давай", "@someone глянь", "   ", "го завтра"])
    assert got == ["ну давай", "го завтра"]
    many = [f"сообщение номер {'а' * i}" for i in range(80)]
    assert len(ps.pick_samples(many, 10)) == 10


def test_prompt_carries_the_measure_and_voice_enforces_it():
    p = pe.PersonaProfile(persona_id=1, account_id=1, name="Олег", bio="",
                          age=30, interests=[], speech_style="casual", tone="neutral",
                          niche="", backstory="", created_at=None, sample_messages=_LOW)
    prompt = pe.build_persona_system_prompt(p)
    assert "Замер твоей манеры" in prompt and prompt.count("«") <= 2 * pe.SAMPLES_MAX + 2
    out = pv.enforce_style("Да, я тоже так считаю. Это действительно интересная тема.", p)
    assert out[0].islower() and not out.endswith(".")


# ── Живая база: образцы из своих переписок ───────────────────────────────────

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, OTHER = 990861, 990862
live = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")


@live
def test_samples_come_from_the_owners_own_outgoing_messages_only():
    async def go():
        import asyncpg
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from services import mini_app_api as M
        from services.token_vault import encrypt_token

        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        orig = M._get_uid
        M._get_uid = lambda r: int(r.headers.get("X-Uid", OWNER))

        async def clean():
            await pool.execute("DELETE FROM vault_messages WHERE owner_id = ANY($1::bigint[])",
                               [OWNER, OTHER])
        try:
            await clean()
            rows = [(OWNER, "out", "ну го завтра"), (OWNER, "out", "короче норм"),
                    (OWNER, "out", "ну хз даже"), (OWNER, "out", "давай короче так"),
                    (OWNER, "in", "ЧУЖАЯ ВХОДЯЩАЯ"), (OTHER, "out", "ЧУЖОЙ ВЛАДЕЛЕЦ")]
            for i, (o, d, t) in enumerate(rows):
                await pool.execute(
                    "INSERT INTO vault_messages (owner_id, chat_id, msg_id, direction, text_enc) "
                    "VALUES ($1, 777, $2, $3, $4)", o, 1000 + i, d, encrypt_token(t))
            app = web.Application()
            M.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                r = await client.get("/api/miniapp/persona/style_from_my_chats")
                assert r.status == 200, await r.text()
                d = await r.json()
            finally:
                await client.close()
            assert sorted(d["samples"]) == sorted(["ну го завтра", "короче норм",
                                                   "ну хз даже", "давай короче так"])
            assert d["summary"].startswith("По 4 образцам")
        finally:
            M._get_uid = orig
            try:
                await clean()
            finally:
                await pool.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(go())
    finally:
        loop.close()
