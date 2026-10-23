"""Список с потолком обязан сказать, сколько записей за ним осталось.

НАХОДКА (23.10.2026). У `tests/test_miniapp_lists_admit_truncation.py` список
маршрутов ведётся РУКАМИ (`OWNER_BUILT_LISTS`). Такой список ловит только то,
что кто-то вспомнил в него дописать, — ровно так мимо проверки пути слияния
прошла `auto_merge_with_confidence`. Здесь то же правило, но измеренное: все
зарегистрированные ручки API-слоя мини-аппа, у которых в теле есть `_list_limit(`,
а в ответе нет настоящего итога. Вместе с достижимостью маршрута (тот же способ,
что в test_routes_the_owner_can_reach) мерка нашла четыре живых случая:

  * `/uch/graph/pairs` — предыдущий экран показывает «Совпадение телефона — 420
    связей», по нажатию приезжает пятьдесят пар и ни слова об остальных;
  * `/audience_analytics/segment` — экран сегмента существует, чтобы ЗАБРАТЬ
    номера для рассылки. Сегмент «Спящие» — тысяча двести человек, сервер
    отдавал первую сотню, а кнопка говорила «📋 Скопировать номера (100)».
    Владелец уносил в буфере восемь процентов сегмента и считал, что унёс
    сегмент. Это худший случай класса: ошибка уезжала в рассылку;
  * `/content_mesh/{id}` — чипы над списком считают ВСЮ очередь («с ошибкой
    48»), а список был последними тридцатью записями по времени: сорок восемь
    ошибок могли не попасть в него ни одной, и узнать, почему репост не дошёл,
    было негде. Чип стал дверью в срез по состоянию;
  * `/ghost/{id}` — журнал действий призрака последними тридцатью, без подписи.

Детектор был проверен на заведомо здоровом примере ПРЕЖДЕ правок: ручка
`compliance_overview` считает `count_recent` и отдаёт `recent_total` — то есть
ключ итога не обязан называться ровно `total`, и первая версия мерки врала на
ней. Отсюда суффиксное совпадение `*total`.

Достижимость — часть правила, а не исключение в списке: у `/mp/catalog` и
`/ranking/history/{id}` двери нет (решение по разделам «витрина провайдеров» и
«рейтинг» за владельцем), и пока её нет, молчаливый потолок никто не видит. Как
только дверь появится, мерка покраснеет сама — руками ничего дописывать не
надо.

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся):
  1. убрать `total` из ответа `uch_graph_pairs` -> красный
     test_every_reachable_capped_list_admits_it, строка про uch_graph_pairs;
  2. убрать `anCap(` из `openAudSegment`/`loadAudSegment` -> красный
     test_the_screens_say_it_to_the_owner: итог доезжает до ответа и умирает
     в нём, экран по-прежнему молчит;
  3. вернуть в `anCap` возврат строки при `t <= shown` -> красный
     test_the_note_is_silent_when_nothing_is_cut: подпись «Показаны 30 из 30»
     под полным списком — это шум, который учит её не читать.
"""
from __future__ import annotations

import ast
import functools
import os
import re
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from miniapp_source import (  # noqa: E402
    api_files, api_source, js_functions, miniapp_source)

# Ключ настоящего итога. Не только `total`: compliance_overview отдаёт
# `recent_total`, uch_timeline — `truncated`, список контактов — `has_more`.
_HONEST = re.compile(r"""["'](?:[a-z_]*total|truncated|has_more)["']\s*:""")
_REG = re.compile(
    r'app\.router\.add_(?:get|post|put|delete|patch)\(\s*"([^"]+)"\s*,\s*([A-Za-z_]\w*)')


def _uncommented(code: str) -> str:
    """Без комментариев: объяснение про потолок не должно сходить за итог."""
    return "\n".join(l for l in code.splitlines()
                     if not l.lstrip().startswith("#"))


def is_capped(code: str) -> bool:
    return "_list_limit(" in _uncommented(code)


def is_honest(code: str) -> bool:
    return bool(_HONEST.search(_uncommented(code)))


@functools.lru_cache(maxsize=1)
def _handlers() -> dict[str, tuple[str, str, tuple[str, ...]]]:
    """Имя ручки -> (файл, тело, пути). Только то, что реально зарегистрировано.

    Привязка к регистрации убирает из выдачи и сам помощник `_list_limit`, и
    объемлющую `setup_routes`: первая версия мерки считала их находками.
    """
    paths: dict[str, list[str]] = {}
    for path, name in _REG.findall(api_source()):
        paths.setdefault(name, []).append(path)
    out: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    for p in api_files():
        text = p.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in paths and node.name not in out):
                out[node.name] = (p.name,
                                  ast.get_source_segment(text, node) or "",
                                  tuple(paths[node.name]))
    return out


def _reachable(route: str) -> bool:
    """Та же мерка достижимости, что в test_routes_the_owner_can_reach."""
    head = route.split("{")[0].rstrip("/")
    return bool(head) and head in miniapp_source()


def test_the_detector_sees_both_sides():
    """Пустой реестр или пустой мини-апп сделали бы тест зелёным."""
    h = _handlers()
    assert len(h) > 500, f"ручек в реестре подозрительно мало: {len(h)}"
    capped = [n for n, (_, b, _) in h.items() if is_capped(b)]
    assert len(capped) > 20, f"ручек с потолком найдено мало: {len(capped)}"
    assert len(miniapp_source()) > 500_000, "мини-апп прочитался не целиком"
    reach = [n for n, (_, _, ps) in h.items() if any(_reachable(p) for p in ps)]
    assert len(reach) > 300, f"достижимых ручек мало: {len(reach)}"


def test_the_detector_spares_a_healthy_handler():
    """Проба: мерка обязана отличать здоровую ручку от молчаливой."""
    sick = ('rows = await pool.fetch(SQL, uid, _list_limit(request, 30, 200))\n'
            'return _json_resp({"items": [dict(r) for r in rows]})')
    healthy = ('rows = await pool.fetch(SQL, uid, _list_limit(request, 30, 200))\n'
               'n = await _safe_count(pool, "SELECT COUNT(*) FROM t WHERE o=$1", uid)\n'
               'return _json_resp({"items": [dict(r) for r in rows], "total": n})')
    whole = 'rows = await pool.fetch(SQL, uid)\nreturn _json_resp({"items": rows})'
    assert is_capped(sick) and not is_honest(sick), "мерка не видит молчаливый потолок"
    assert is_capped(healthy) and is_honest(healthy), "мерка ругает честную ручку"
    assert not is_capped(whole), "мерка считает потолком список без потолка"
    excuse = ('# total считается на экране\n'
              'rows = await pool.fetch(SQL, _list_limit(request, 30, 200))')
    assert not is_honest(excuse), "объяснение в комментарии сошло за итог"


def test_the_key_of_the_total_need_not_be_named_total():
    """Заведомо здоровый пример из продукта: `recent_total`, не `total`.

    Первая версия мерки искала ровно `total` и объявила эту ручку сломанной —
    при настоящем `count_recent` в ней. Если проба перестанет проходить,
    чинить надо мерку, а не ручку.
    """
    _, body, _ = _handlers()["compliance_overview"]
    assert "count_recent" in body, "журнал соответствия перестал считать итог"
    assert is_capped(body) and is_honest(body), (
        "мерка снова не видит итог под именем recent_total")


def test_every_reachable_capped_list_admits_it():
    """Потолок на экране, до которого владелец доходит, обязан быть назван."""
    silent, doorless = [], []
    for name, (where, body, paths) in sorted(_handlers().items()):
        if not is_capped(body) or is_honest(body):
            continue
        (doorless if not any(_reachable(p) for p in paths) else silent).append(
            f"{where}:{name}  {paths[0]}")
    assert not silent, (
        "Список обрывается молча, а владелец видит его как весь список:\n  "
        + "\n  ".join(silent)
        + "\n(без двери, пока терпимо: " + ", ".join(doorless) + ")")


def test_the_screens_say_it_to_the_owner():
    """Итог в ответе, который никто не рисует, — та же ложь в два шага."""
    F = js_functions()
    pairs = [
        ("openUchGraphType", "d.total"),
        ("loadAudSegment", "_AUDSEG_TOTAL"),
        ("loadMeshDetail", "d.recent_total"),
        ("openGhostDetail", "d.log_total"),
    ]
    broken = []
    for fn, field in pairs:
        body = F.get(fn)
        if not body:
            broken.append(f"{fn}: экрана больше нет")
            continue
        code = "\n".join(l for l in body.splitlines()
                         if not l.lstrip().startswith("//"))
        if "anCap(" not in code:
            broken.append(f"{fn}: подписи о потолке нет")
        elif field not in code:
            broken.append(f"{fn}: итог {field} с сервера не читается")
    assert not broken, (
        "Сервер считает итог, а экран о потолке молчит:\n  " + "\n  ".join(broken))


def test_the_copy_button_says_how_many_it_copies():
    """Кнопка «Скопировать номера» уносила сотню из тысячи и молчала."""
    F = js_functions()
    render = F["loadAudSegment"]
    assert "_AUDSEG_TOTAL > _AUDSEG_ROWS.length ? ' из ' + num(_AUDSEG_TOTAL)" in render, (
        "кнопка копирования снова называет только размер страницы")
    copy = F["audSegCopyIds"]
    assert "_AUDSEG_TOTAL" in copy and "не загружены" in copy, (
        "сообщение после копирования не говорит, что сегмент взят не весь")
    assert "limit=" in render, (
        "экран не просит потолок — догрузить остальных будет нечем")


def test_the_queue_chips_lead_into_the_queue():
    """«С ошибкой 48» обязано вести к этим сорока восьми."""
    F = js_functions()
    chip = F["_meshChip"]
    assert "loadMeshDetail(" in chip, "чип очереди снова ничего не открывает"
    code = _handlers()["content_mesh_detail"][1]
    assert 'request.query.get("status")' in code, "срез очереди по состоянию исчез"
    assert '("pending", "sent", "error")' in code, (
        "состояние из адреса больше не сверяется со списком — в запрос уйдёт что угодно")


def test_the_note_is_silent_when_nothing_is_cut():
    """Подпись под полным списком — шум, который учит её не читать.

    Это не греп, а прогон самой `anCap` в node: на 30 из 30 она обязана
    промолчать, на 30 из 420 — назвать оба числа по-русски.
    """
    src = js_functions()["anCap"]
    helpers = (
        "function num(n){return String(n)}\n"
        "function plural(n,a,b,c){const m=n%100,k=n%10;"
        "return m>4&&m<21?c:k===1?a:k>1&&k<5?b:c}\n")
    script = helpers + src + """
const whole = anCap(30, 30, 'пара', 'пары', 'пар');
const cut = anCap(50, 420, 'пара', 'пары', 'пар');
const none = anCap(0, 0, 'пара', 'пары', 'пар');
console.log(JSON.stringify({whole, cut, none}));
"""
    out = subprocess.run([ "node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    import json
    got = json.loads(out.stdout)
    assert got["whole"] == "", f"подпись болтает под полным списком: {got['whole']!r}"
    assert got["none"] == "", "подпись болтает под пустым списком"
    assert "50" in got["cut"] and "420" in got["cut"] and "пар" in got["cut"], (
        f"подпись не называет оба числа: {got['cut']!r}")


# ── Живые проверки: итог обязан считаться тем же условием, что и список ──────

DSN = os.environ.get("INFRAGRAM_TEST_DSN")
live = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 991801
STRANGER = 991802


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


@live
def test_the_pair_count_matches_the_capped_list():
    """Итог пар и сам список обязаны считать одно и то же.

    Если итог посчитать без соединений по владельцу, он станет больше списка
    на пары, у которых одна сторона принадлежит другому владельцу: экран
    скажет «Показаны 5 из 7» там, где их действительно пять.
    """
    import asyncpg

    from services.contacts_hub.relationship_engine import (
        count_pairs_by_type, get_pairs_by_type)

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            for who in (OWNER, STRANGER):
                await pool.execute(
                    "DELETE FROM contact_relationships WHERE owner_id=$1", who)
                await pool.execute(
                    "DELETE FROM unified_contacts WHERE owner_id=$1", who)
            ids = []
            for i in range(6):
                ids.append(await pool.fetchval(
                    "INSERT INTO unified_contacts(owner_id, first_name) "
                    "VALUES($1,$2) RETURNING id", OWNER, f"Свой {i}"))
            alien = await pool.fetchval(
                "INSERT INTO unified_contacts(owner_id, first_name) "
                "VALUES($1,'Чужой') RETURNING id", STRANGER)
            for a, b in zip(ids[:-1], ids[1:]):
                await pool.execute(
                    "INSERT INTO contact_relationships(owner_id, contact_a_id,"
                    " contact_b_id, relationship_type, strength)"
                    " VALUES($1,$2,$3,'phone_match',0.9)", OWNER, a, b)
            # Пара, записанная на владельца, но ведущая на чужой контакт:
            # в список не попадёт, значит и в итоге её быть не должно.
            await pool.execute(
                "INSERT INTO contact_relationships(owner_id, contact_a_id,"
                " contact_b_id, relationship_type, strength)"
                " VALUES($1,$2,$3,'phone_match',0.9)", OWNER, ids[0], alien)
            whole = await get_pairs_by_type(pool, OWNER, "phone_match", 100)
            cut = await get_pairs_by_type(pool, OWNER, "phone_match", 2)
            total = await count_pairs_by_type(pool, OWNER, "phone_match")
            other = await count_pairs_by_type(pool, OWNER, "same_company")
            alien_total = await count_pairs_by_type(pool, STRANGER, "phone_match")
            return len(whole), len(cut), total, other, alien_total
        finally:
            await pool.close()

    whole, cut, total, other, alien_total = _run(go())
    assert whole == 5, f"список своих пар собрался неверно: {whole}"
    assert total == whole, (
        f"итог {total} не равен полному списку {whole} — экран будет обещать "
        "пары, которых в списке никогда не будет")
    assert cut == 2 and total > cut, "потолок перестал резать — проверка пуста"
    assert other == 0, "итог считает пары другого типа"
    assert alien_total == 0, "итог чужого владельца не нулевой"


@live
def test_the_segment_count_matches_the_capped_list():
    """Число людей сегмента и выборка людей — по одному условию."""
    import asyncpg

    from services import audience_analytics as aa

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            bot = 7_700_001
            await pool.execute("DELETE FROM user_activity WHERE bot_id=$1", bot)
            await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", bot)
            await pool.execute(
                "INSERT INTO managed_bots(bot_id, added_by, token, username)"
                " VALUES($1,$2,'x','probe_bot')", bot, OWNER)
            for i in range(7):
                await pool.execute(
                    "INSERT INTO user_activity(bot_id, user_id, message_count,"
                    " last_seen, first_seen) VALUES($1,$2,1,"
                    " now() - interval '400 days', now() - interval '400 days')",
                    bot, 880_000 + i)
            rows = await aa.segment_members(pool, OWNER, "dormant", 3)
            whole = await aa.segment_members(pool, OWNER, "dormant", 500)
            total = await aa.segment_member_count(pool, OWNER, "dormant")
            alien = await aa.segment_member_count(pool, STRANGER, "dormant")
            nonsense = await aa.segment_member_count(pool, OWNER, "нет такого")
            await pool.execute("DELETE FROM user_activity WHERE bot_id=$1", bot)
            await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", bot)
            return len(rows), len(whole), total, alien, nonsense
        finally:
            await pool.close()

    page, whole, total, alien, nonsense = _run(go())
    assert whole == 7, f"сегмент собрался неверно: {whole}"
    assert total == whole, (
        f"итог {total} расходится с выборкой {whole} — кнопка «Скопировать "
        "номера» снова будет обещать людей, которых не отдаёт")
    assert page == 3 and total > page, "потолок перестал резать — проверка пуста"
    assert alien == 0, "итог сегмента чужого владельца не нулевой"
    assert nonsense == 0, "неизвестный ключ сегмента дал ненулевой итог"
