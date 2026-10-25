"""Число воронки обязано открываться списком тех самых людей.

НАХОДКА (23.10.2026). Оба экрана воронок показывали ТОЛЬКО числа:

  * авто-воронка: «идут 120 · дошли до конца 40 · сошли 48», и под шагами
    «⏳ ждут этого шага: 12»;
  * классическая: «📊 дошли: 90 (64%) · отвал дальше: 31».

Главный вопрос к любой цепочке — кто именно ушёл и на каком шаге — ответа не
имел: маршрута, который отдал бы этих людей, не было ни у одной из двух систем
воронок. Владелец видел, что цепочка теряет людей, и не мог ни посмотреть их,
ни написать им. Это тот же класс, что «Спящие · 1 240» в сегментах аудитории:
число — тупик.

Правило, которое здесь держится: срез задаётся ТЕМИ ЖЕ полями, из которых
посчитано число на экране, поэтому итог среза совпадает с ним по построению, а
не по совпадению. Для классической воронки это главное:
`funnel_subscriptions.current_step` — сколько шагов уже доставлено, значит
«отвал дальше» на шаге i — ровно те, у кого current_step = i, и срез `step=i`
обязан дать это же число. Ниже это сверено живьём: экран и список считаются
двумя разными запросами, и они обязаны сойтись.

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся):
  1. заменить в funnel_people срез `current_step=$2` на `current_step>=$2` ->
     красный test_the_drop_off_number_and_its_list_agree: итог становится 2
     против 1 на шаге 1 (это «дошли до шага», а не «отвал»);
  2. убрать `status IN ('stopped','error')` и оставить один `stopped` ->
     красный test_the_lost_chip_counts_both_ways_of_losing: чип складывает два
     состояния, срез обязан складывать те же два;
  3. убрать `mb.added_by=$2` из запроса воронки в funnel_people -> красные
     test_a_strangers_funnel_is_not_found (чужая воронка отдала 200 вместо
     404) и test_both_routes_are_registered.
"""
from __future__ import annotations

import os

import pytest

from tests.miniapp_source import api_function, js_functions

DSN = os.environ.get("INFRAGRAM_TEST_DSN")
live = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 991901
STRANGER = 991902
BOT = 7_700_101


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Двери: число на экране обязано звать маршрут за людьми ──────────────────

def test_every_funnel_number_has_a_door():
    """Все четыре числа — чипы, ожидание шага и отвал — открывают список."""
    F = js_functions()
    steps = F["openFunnelSteps"]
    for q in ("status=active", "status=completed", "status=lost"):
        assert q in steps, f"чип {q} никуда не ведёт"
    assert "status=active&step=" in steps, (
        "«ждут этого шага» осталось числом без двери")
    detail = F["buildFunnelDetail"]
    assert "openFunnelPeople('classic'" in detail and "'step=" in detail, (
        "«отвал дальше» осталось числом без двери")
    assert "openFunnelPeople(" in steps, "экран шагов не зовёт список людей"


def test_the_list_screen_admits_its_own_ceiling():
    """Новый экран сам подчиняется правилу про потолок."""
    body = js_functions()["loadFunnelPeople"]
    assert "anCap(" in body and "d.total" in body, "экран людей молчит о потолке"
    assert "limit=" in body, "потолок не передаётся — догрузить нечем"
    assert "Догрузить до 500" in body, "остальных никак не достать"
    assert "fpplCopyIds(" in body, "номера нельзя забрать"


def test_both_routes_are_registered():
    """Ручка без регистрации недостижима, сколько бы её ни тестировали."""
    from tests.miniapp_source import api_source
    src = api_source()
    assert '"/api/miniapp/funnel/{funnel_id}/people", funnel_people' in src
    assert '"/api/miniapp/auto_funnel/{funnel_id}/people", auto_funnel_people' in src
    # Скоуп держит не обработчик, а запрос: у классической воронки владельца
    # нет своего поля, он берётся через managed_bots.added_by.
    assert "mb.added_by=$2" in api_function("funnel_people"), (
        "классическая воронка читается без проверки владельца")
    assert "_own_funnel(uid, fid)" in api_function("auto_funnel_people"), (
        "авто-воронка читается без проверки владельца")


# ── Живьём: число на экране и список за ним считаются врозь и обязаны сойтись ─

async def _seed_classic(pool):
    """Воронка из трёх шагов и шесть подписчиков с разной глубиной."""
    await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT)
    await pool.execute(
        "INSERT INTO managed_bots(bot_id, added_by, token, username)"
        " VALUES($1,$2,'funnel-probe-token','funnel_probe')", BOT, OWNER)
    fid = await pool.fetchval(
        "INSERT INTO funnels(bot_id, name, trigger_type) "
        "VALUES($1,'Проба','start') RETURNING id", BOT)
    for n in (1, 2, 3):
        await pool.execute(
            "INSERT INTO funnel_steps(funnel_id, step_order, message_text,"
            " delay_minutes) VALUES($1,$2,$3,0)", fid, n, f"шаг {n}")
    # current_step = сколько шагов доставлено: 0,1,1,2,3,3
    for uid_, step, done in ((801, 0, False), (802, 1, False), (803, 1, False),
                             (804, 2, False), (805, 3, True), (806, 3, True)):
        await pool.execute(
            "INSERT INTO funnel_subscriptions(funnel_id, user_id, current_step,"
            " completed) VALUES($1,$2,$3,$4)", fid, uid_, step, done)
    return fid


def _client(pool, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from services import mini_app_api, mini_app_funnels
    monkeypatch.setattr(mini_app_api, "_get_uid",
                        lambda r: int(r.headers.get("X-Uid") or 0) or None)
    app = web.Application()
    mini_app_funnels.setup_routes(app, pool)
    return TestClient(TestServer(app))


@live
def test_the_drop_off_number_and_its_list_agree(monkeypatch):
    """«Отвал дальше» на шаге i и срез step=i — одно и то же число.

    Считаются они двумя разными запросами в двух разных ручках, поэтому
    совпадение здесь — проверка, а не тавтология.
    """
    import asyncpg

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            fid = await _seed_classic(pool)
            client = _client(pool, monkeypatch)
            await client.start_server()
            try:
                h = {"X-Uid": str(OWNER)}
                scr = await (await client.get(
                    f"/api/miniapp/funnel/{fid}/steps", headers=h)).json()
                out = {}
                for i in (1, 2, 3):
                    r = await client.get(
                        f"/api/miniapp/funnel/{fid}/people?step={i}", headers=h)
                    out[i] = await r.json()
                return scr["step_stats"], out
            finally:
                await client.close()
        finally:
            await pool.close()

    stats, slices = _run(go())
    # 6 подписчиков: доставлено 0,1,1,2,3,3 -> дошли до 1: 5, до 2: 3, до 3: 2
    assert [s["delivered"] for s in stats] == [5, 3, 2], f"экран считает иначе: {stats}"
    assert stats[0]["lost_after"] == 2, "отвал после первого шага посчитан неверно"
    assert stats[1]["lost_after"] == 1, "отвал после второго шага посчитан неверно"
    for i, expected in ((1, 2), (2, 1)):
        got = slices[i]["total"]
        assert got == expected, (
            f"шаг {i}: экран обещает отвал {expected}, список отдаёт {got} — "
            "число и люди за ним считаются по-разному")
        assert len(slices[i]["items"]) == expected, "список короче своего итога"
    # Последний шаг — не отвал: дальше идти некуда.
    assert stats[2]["lost_after"] == 0 and slices[3]["total"] == 2, (
        "на последнем шаге отвала быть не может, а люди на нём есть")


@live
def test_a_strangers_funnel_is_not_found(monkeypatch):
    """Чужая воронка — 404, а не список её подписчиков."""
    import asyncpg

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            fid = await _seed_classic(pool)
            client = _client(pool, monkeypatch)
            await client.start_server()
            try:
                mine = await client.get(f"/api/miniapp/funnel/{fid}/people",
                                        headers={"X-Uid": str(OWNER)})
                alien = await client.get(f"/api/miniapp/funnel/{fid}/people",
                                         headers={"X-Uid": str(STRANGER)})
                junk = await client.get(f"/api/miniapp/funnel/xyz/people",
                                        headers={"X-Uid": str(OWNER)})
                return mine.status, alien.status, junk.status
            finally:
                await client.close()
        finally:
            await pool.close()

    mine, alien, junk = _run(go())
    assert mine == 200, f"свою воронку не отдали: {mine}"
    assert alien == 404, f"чужая воронка ответила {alien}, а не 404"
    assert junk in (400, 404), f"мусор в адресе дал {junk}, а не отказ"


@live
def test_the_lost_chip_counts_both_ways_of_losing(monkeypatch):
    """Чип «сошли» складывает stopped и error — срез обязан складывать те же."""
    import asyncpg

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            await pool.execute("DELETE FROM auto_funnels WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT)
            await pool.execute(
                "INSERT INTO managed_bots(bot_id, added_by, token, username)"
                " VALUES($1,$2,'funnel-probe-token','funnel_probe')", BOT, OWNER)
            fid = await pool.fetchval(
                "INSERT INTO auto_funnels(owner_id, bot_id, name, target_segment)"
                " VALUES($1,$2,'Проба','all') RETURNING id", OWNER, BOT)
            rows = (("active", 1), ("active", 1), ("active", 2),
                    ("completed", 3), ("stopped", 2), ("error", 1), ("error", 2))
            for i, (status, step) in enumerate(rows):
                await pool.execute(
                    "INSERT INTO auto_funnel_runs(funnel_id, bot_id, user_id,"
                    " next_step_num, next_send_at, status)"
                    " VALUES($1,$2,$3,$4,now(),$5)",
                    fid, BOT, 900_000 + i, step, status)
            client = _client(pool, monkeypatch)
            await client.start_server()
            try:
                h = {"X-Uid": str(OWNER)}
                scr = await (await client.get(
                    f"/api/miniapp/auto_funnel/{fid}", headers=h)).json()
                lost = await (await client.get(
                    f"/api/miniapp/auto_funnel/{fid}/people?status=lost",
                    headers=h)).json()
                step1 = await (await client.get(
                    f"/api/miniapp/auto_funnel/{fid}/people?status=active&step=1",
                    headers=h)).json()
                return scr["runs"], lost, step1
            finally:
                await client.close()
        finally:
            await pool.close()

    runs, lost, step1 = _run(go())
    st = runs["by_status"]
    chip = int(st.get("stopped", 0)) + int(st.get("error", 0))
    assert chip == 3, f"чип «сошли» посчитан неверно: {st}"
    assert lost["total"] == chip, (
        f"чип обещает {chip} сошедших, срез отдаёт {lost['total']} — "
        "одно из двух считает не то")
    waiting = int(runs["waiting_on_step"].get("1", 0))
    assert waiting == 2, f"«ждут шага 1» посчитано неверно: {runs['waiting_on_step']}"
    assert step1["total"] == waiting, (
        f"«ждут шага 1» обещает {waiting}, срез отдаёт {step1['total']}")
    assert len(step1["items"]) == waiting, "список короче своего итога"
