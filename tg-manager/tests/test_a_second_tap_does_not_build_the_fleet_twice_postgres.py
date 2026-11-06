"""Второе нажатие «Запустить план присутствия» не создаёт активы по второму разу.

ЧТО БЫЛО. Запуск плана создавал строку плана, писал её цели и ставил операцию
со свежим `plan_id` в params. Окно идемпотентности `operation_bus.submit` на
этот случай не работает в принципе: у двух нажатий разные id, значит разные
params, значит две операции. Защита внутри исполнителя есть, но она привязана
к ЦЕЛИ (`UPDATE global_presence_targets SET status='running' WHERE id=$1 AND
status='pending'`) — то есть к строкам ОДНОГО плана. У второго плана свои
свежие цели, и флот создаёт все каналы, группы и ботов заново: лишние активы
на тех же аккаунтах и двойной расход самого баноопасного действия продукта.

Нажатие дублируется обычным путём: кнопка живёт в состоянии `confirming`,
которое чистится ПОСЛЕ всей работы, сборка целей занимает секунды, а Телеграм
сам повторяет callback, если бот не ответил.

ЧТО ЗАКРЕПЛЕНО. Личность нажатия — СОДЕРЖИМОЕ плана (владелец, тип актива,
паттерны, выбранные аккаунты, шаблон и отпечаток целей: страна, город, тип),
а не его свежий id. Проверяется на живой базе: сравнение отпечатка целей
целиком лежит в SQL, и стенд с подделанным пулом о нём не говорит ничего.
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
ROOT = pathlib.Path(__file__).resolve().parent.parent
OWNER = 993618

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=2)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.execute("DELETE FROM global_presence_plans WHERE owner_id=$1", OWNER))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(pool.execute("DELETE FROM global_presence_plans WHERE owner_id=$1", OWNER))


def _targets(cities: list[str], asset_type: str = "channel") -> list[dict]:
    return [
        {
            "country": "Россия",
            "country_code": "RU",
            "city": c.title(),
            "city_slug": c,
            "asset_type": asset_type,
            # Имена генератор даёт разными на каждое нажатие — они намеренно
            # не входят в отпечаток, и тест это держит.
            "planned_name": f"{c}-{os.urandom(2).hex()}",
        }
        for c in cities
    ]


def _submit(pool, targets, **kw):
    from database import db

    opts = dict(
        owner_id=OWNER,
        asset_type="channel",
        name_pattern="Новости {city}",
        username_pattern=None,
        geo_selection={"preset": "ru_top", "count": len(targets)},
        account_selection={"account_ids": [11, 22]},
        template_id=None,
        targets=targets,
    )
    opts.update(kw)
    return _run(db.find_or_create_global_presence_plan(pool, **opts))


def _counts(pool, plan_id: int) -> int:
    return int(_run(pool.fetchval(
        "SELECT count(*) FROM global_presence_targets WHERE plan_id=$1", plan_id)))


def test_the_same_tap_twice_is_one_plan(pool):
    first, reused_1 = _submit(pool, _targets(["moscow", "kazan"]))
    second, reused_2 = _submit(pool, _targets(["moscow", "kazan"]))

    assert reused_1 is False
    assert second == first, (
        "второе нажатие создало второй план: флот создаст те же каналы заново")
    assert reused_2 is True
    assert _counts(pool, first) == 2, (
        "цели записались по второму разу — исполнитель создаст активы дважды "
        "даже внутри одного плана")
    assert int(_run(pool.fetchval(
        "SELECT count(*) FROM global_presence_plans WHERE owner_id=$1",
        OWNER))) == 1


def test_another_city_list_is_another_plan(pool):
    first, _ = _submit(pool, _targets(["moscow", "kazan"]))
    second, reused = _submit(pool, _targets(["moscow", "sochi"]))
    assert second != first, "план по другим городам слили с предыдущим"
    assert reused is False


def test_a_longer_list_with_the_same_cities_is_another_plan(pool):
    """Три канала в одном городе — не то же, что один: отпечаток хранит кратность."""
    first, _ = _submit(pool, _targets(["moscow"]))
    second, reused = _submit(pool, _targets(["moscow", "moscow", "moscow"]))
    assert second != first, "план на три канала слили с планом на один"
    assert reused is False


def test_another_asset_type_is_another_plan(pool):
    """Каналы и боты запускаются двумя планами в ОДНОМ нажатии — не двойники."""
    chans, _ = _submit(pool, _targets(["moscow"]), asset_type="channel")
    bots, reused = _submit(pool, _targets(["moscow"], asset_type="bot"),
                           asset_type="bot")
    assert bots != chans, (
        "план ботов слили с планом каналов: половина нажатия потерялась")
    assert reused is False


def test_a_deliberate_repeat_after_the_window_creates_a_new_plan(pool):
    """Окно обязано кончаться: вторая волна тех же активов — законное дело."""
    first, _ = _submit(pool, _targets(["moscow"]))
    _run(pool.execute(
        "UPDATE global_presence_plans SET created_at = now() - INTERVAL '1 hour' "
        "WHERE id=$1", first))
    second, reused = _submit(pool, _targets(["moscow"]))
    assert second != first, "повтор через час слили с давно созданным планом"
    assert reused is False


def test_a_finished_plan_is_not_reused(pool):
    """Повтор по законченному плану — это именно «сделать ещё раз»."""
    first, _ = _submit(pool, _targets(["moscow"]))
    _run(pool.execute(
        "UPDATE global_presence_plans SET status='done' WHERE id=$1", first))
    second, reused = _submit(pool, _targets(["moscow"]))
    assert second != first, "новый запуск сел на уже законченный план"
    assert reused is False


def test_the_window_is_long_enough_for_a_slow_launch():
    from database import db

    assert db.GP_PLAN_MERGE_WINDOW_SEC >= 120, (
        "план собирается дольше рассылки (цели, аллокатор username, "
        "экосистема): окно короче двух минут не покрывает повтор нажатия")


def test_the_handler_creates_the_plan_through_the_door():
    """Прямая вставка плана в обработчике возвращала бы дыру целиком.

    Мерка — по смыслу: в `_submit_plan` не должно остаться ни вызова
    `create_global_presence_plan`, ни отдельной записи целей (она делается
    ОДНОЙ транзакцией с планом, иначе второе нажатие видит план без целей,
    отпечаток не совпадает и дверь пропускает второй план).
    """
    src = (ROOT / "bot" / "handlers" / "global_presence.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_submit_plan")
    body = ast.get_source_segment(src, fn) or ""
    assert "find_or_create_global_presence_plan" in body
    # (?<!or_) — иначе подстрока находится внутри имени самой двери.
    assert not re.search(r"(?<!or_)create_global_presence_plan\(", body), (
        "план создаётся в обход двери — второе нажатие создаст второй")
    assert "create_global_presence_targets(" not in body, (
        "цели пишутся отдельно от плана: в этой щели живёт та самая гонка")


def test_a_different_description_is_a_different_plan(pool):
    """Описание плана живёт в geo_selection и тоже часть личности.

    Без него два плана, различающиеся ОДНИМ описанием, слились бы в один, и
    второе описание пропало бы молча — потеря намеренного действия владельца
    опаснее лишней строки в базе.
    """
    first, _ = _submit(pool, _targets(["moscow"]),
                       geo_selection={"geo_source": "list", "count": 1,
                                      "description": "Городские новости"})
    second, reused = _submit(pool, _targets(["moscow"]),
                             geo_selection={"geo_source": "list", "count": 1,
                                            "description": "Афиша"})
    assert second != first, "план с другим описанием слили с предыдущим"
    assert reused is False


def test_the_mini_app_plan_waiting_for_launch_is_reused(pool):
    """Мини-апп заводит план в 'pending' и запускает отдельной ручкой.

    Два нажатия «создать» (ответ не вернулся, экран показал ошибку, владелец
    нажал снова) заводили два плана с одними и теми же целями — и запуск
    каждого создавал те же каналы заново.
    """
    first, _ = _submit(pool, _targets(["moscow"]), status="pending")
    second, reused = _submit(pool, _targets(["moscow"]), status="pending")
    assert second == first, "второй «создать» завёл второй план"
    assert reused is True
    assert _run(pool.fetchval(
        "SELECT status FROM global_presence_plans WHERE id=$1", first)) == "pending", (
        "мини-апповский план перестал быть 'pending': его ручка запуска "
        "принимает только pending/failed, и запустить план стало бы нельзя")
    assert _counts(pool, first) == 1, "цели записались по второму разу"


def test_every_plan_creator_goes_through_the_door():
    """Мерка по смыслу: ищем ЛЮБОЕ создание плана в обход двери.

    Их было четыре пути, и у трёх дыра была одна и та же. Прямая вставка или
    вызов `create_global_presence_plan` где угодно, кроме самой двери, вернёт
    дыру целиком, а снаружи это выглядит как «флот создал всё дважды».
    """
    bad = []
    # По всему коду, а не по списку файлов: рукописный список не знает о пути,
    # который заведут завтра, — а дыра возвращается именно так.
    scanned = 0
    for sub in ("services", "bot", "database"):
        for path in sorted((ROOT / sub).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel == "database/db.py":
                continue  # сама дверь
            scanned += 1
            src = path.read_text(encoding="utf-8")
            if re.search(r"(?<!or_)create_global_presence_plan\(", src):
                bad.append(f"{rel}: зовёт create_global_presence_plan напрямую")
            if re.search(r"INSERT\s+INTO\s+global_presence_plans", src, re.I):
                bad.append(f"{rel}: вставляет план своим SQL")
    assert scanned > 100, f"обойдено всего {scanned} файлов — обход сломан"
    assert not bad, "; ".join(bad)


def test_a_repeat_tap_does_not_create_a_second_ecosystem():
    """Повтор садится на тот же план — значит и структуру заводить не надо.

    Экосистема создаётся ПОСЛЕ плана и в той же кнопке. Повтор нажатия
    получает тот же plan_id, но прежний код всё равно заводил новую
    экосистему и писал «план запущен» второй раз: в списке владельца две
    одинаковые структуры, а активы разошлись бы по ним. Признак того, что
    первая попытка всё сделала, — `ecosystem_id` на плане.
    """
    src = (ROOT / "bot" / "handlers" / "global_presence.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "create_ecosystem"]
    assert calls, "создание экосистемы не найдено — мерка смотрит не туда"
    guarded = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = ast.get_source_segment(src, node.test) or ""
        body = "\n".join(filter(None, (ast.get_source_segment(src, b)
                                       for b in node.body)))
        if "create_ecosystem" in body and "_eco_id" in test:
            guarded.append(test)
    assert guarded, (
        "создание экосистемы не прикрыто проверкой «у плана она уже есть»: "
        "повтор нажатия заведёт вторую структуру на тот же план")
    assert re.search(r"SELECT\s+ecosystem_id\s+FROM\s+global_presence_plans",
                     src), "признак читается не с плана — проверка условна"
