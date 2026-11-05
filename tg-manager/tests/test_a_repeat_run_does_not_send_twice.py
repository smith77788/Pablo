"""Повтор прогона не отправляет человеку второе такое же сообщение.

Повтор — ШТАТНОЕ событие, а не редкость: рабочая ветка едет в прод на каждом
пуше, рестарт возвращает операцию в очередь, `_reset_stale_running` обнуляет
`done_items`, и исполнитель идёт по списку целей С НАЧАЛА. На этой оси у каждой
отправляющей операции стоит своя дверь — пер-целевой журнал, отметка в params
или собственная таблица отработанных целей, — и читается она ДО действия.

Дверь эта невидимая: код без неё выглядит совершенно нормально и в тестах
проходит, потому что первый прогон ведёт себя одинаково с дверью и без неё.
Беда видна только людям, которым сообщение пришло дважды, — а это самая
заметная спам-сигнатура и прямой путь к бану флота. Поэтому на каждую дверь
здесь стоит храповик: убрали чтение отметок — тест красный.

Храповик НЕ проверяет, что дверь правильная; он проверяет, что она есть.
Правильность каждой двери разобрана в комментарии на её месте.
"""
from __future__ import annotations

import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Отправляющая операция → по какой отметке она узнаёт, что цель уже закрыта.
# Любая из строк в наборе — достаточное доказательство, что отметки читаются.
GUARDED: dict[str, tuple[str, tuple[str, ...], str]] = {
    "_exec_bulk_dm_adhoc": (
        "services/op_worker.py", ("settled_targets",),
        "разовая рассылка в ЛС: журнал operation_log по целям, исходы ok/skip"),
    "_exec_self_promo_blast": (
        "services/op_worker.py", ("settled_targets",),
        "саморекламный залп в ЛС: тот же журнал целей"),
    "_exec_mass_invite": (
        "services/op_worker.py", ("_settled", "completed_targets"),
        "инвайт — самая баноопасная операция: цели помечаются ДО попытки"),
    "_exec_group_announce": (
        "services/op_worker.py", ("completed_targets",),
        "объявление по группам: второй пост в ту же группу — сигнатура спама"),
    "_exec_mass_publish": (
        "services/op_worker.py", ("completed_targets",),
        "публикация по каналам: повтор дал бы дубль поста в канале"),
    "_exec_bulk_post_chans": (
        "services/op_worker.py", ("completed_targets",),
        "пакетный постинг по каналам: то же, что у mass_publish"),
    "_exec_bulk_post_to_channel": (
        "services/op_worker.py", ("completed_targets",),
        "постинг в один канал с нескольких аккаунтов"),
    "_exec_mass_report": (
        "services/op_worker.py", ("completed_targets",),
        "жалобы: повторная жалоба с того же аккаунта бьёт по самому аккаунту"),
    "_exec_boost_subscribers": (
        "services/op_worker.py",
        ("completed_targets", "completed_account_targets"),
        "подписки: повтор гонит аккаунт в тот же канал второй раз"),
    "_exec_network_broadcast": (
        "services/op_worker.py", ("launched_bot_ids",),
        "сетевая рассылка: запущенные боты пишутся в params по ходу дела, "
        "журнал доставок ведётся по id рассылки и второй рассылке не указ"),
    "_exec_run_broadcast": (
        "services/op_worker.py", ("params[\"broadcast_id\"]",),
        "рассылка одного бота: id рассылки закрепляется в params ДО залпа, "
        "иначе повтор заводит вторую рассылку на всю аудиторию"),
    "_exec_niche_growth_post": (
        "services/op_worker.py", ("niche_growth_targets",),
        "Growth Agent: группа пишется в отработанные МЕЖДУ вступлением и "
        "постом — не записалась, значит поста нет"),
}

# Кампания ЛС отбирает получателей не в исполнителе, а в движке: отметка —
# статусы sent/blocked/skip в dm_campaign_log, и фильтр стоит в самом запросе
# списка. Поэтому дверь ищем там, где она стоит.
GUARDED_ELSEWHERE: dict[str, tuple[str, tuple[str, ...], str]] = {
    "dm_campaign": (
        "services/dm_engine.py",
        ("NOT IN (SELECT tg_user_id FROM dm_campaign_log",),
        "кампания ЛС: получатели с закрытым исходом исключены из выборки"),
}


def _source(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _function_body(src: str, name: str) -> str | None:
    """Текст функции `name` из исходника — по AST, а не по отступам."""
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            end = node.end_lineno or node.lineno
            return "".join(lines[node.lineno - 1:end])
    return None


def test_every_sending_operation_reads_its_marks():
    """Каждая отправляющая операция читает отметки уже закрытых целей."""
    naked = []
    for func, (rel, markers, _why) in GUARDED.items():
        body = _function_body(_source(rel), func)
        assert body is not None, f"{func} не найдена в {rel} — поправьте реестр"
        if not any(m in body for m in markers):
            naked.append(f"{func} (ищем любое из {markers})")
    assert not naked, (
        "операция отправляет людям сообщения, но отметки уже закрытых целей не "
        "читает: повтор прогона (рестарт, таймаут, ручной запуск) отправит "
        "второй раз — " + str(naked))


def test_the_campaign_door_stands_in_the_engine():
    """Дверь кампании ЛС — в движке: фильтр по dm_campaign_log в самой выборке."""
    for _name, (rel, markers, _why) in GUARDED_ELSEWHERE.items():
        src = _source(rel)
        assert any(m in src for m in markers), (
            f"{rel}: получатели кампании больше не фильтруются по закрытым "
            f"исходам — повтор напишет тем, кому уже написали ({markers})")


def test_the_registry_explains_itself():
    """Запись без причины — не документация, а список имён."""
    for func, (_rel, markers, why) in {**GUARDED, **GUARDED_ELSEWHERE}.items():
        assert markers, f"{func}: нет ни одной искомой отметки"
        assert len(why) > 20, f"{func}: причина слишком коротка, чтобы быть причиной"


def test_the_measurer_sees_a_missing_door():
    """Проверка измерителя: он обязан отличать дверь от её отсутствия.

    Без этой проверки `test_every_sending_operation_reads_its_marks` остался бы
    зелёным и в случае, когда вырезание функции из файла просто перестаёт
    находиться, — а зелёный храповик хуже отсутствующего.
    """
    src = (
        "async def _exec_bare(pool, bot, op_id, owner_id, params):\n"
        "    for t in params['targets']:\n"
        "        await send(t)\n"
        "\n"
        "async def _exec_guarded(pool, bot, op_id, owner_id, params):\n"
        "    done = await settled_targets(pool, op_id)\n"
        "    for t in params['targets']:\n"
        "        if t in done:\n"
        "            continue\n"
        "        await send(t)\n")
    bare = _function_body(src, "_exec_bare")
    guarded = _function_body(src, "_exec_guarded")
    assert bare is not None and guarded is not None
    assert "settled_targets" not in bare
    assert "settled_targets" in guarded
    assert _function_body(src, "_exec_missing") is None


def test_a_new_broadcast_row_is_never_created_without_a_mark():
    """Производное правило, а не список: новая строка рассылки теряет журнал.

    Список выше придётся пополнять руками, а это правило смотрит на сам
    необратимый шаг. Журнал доставок ведётся ПО id рассылки, поэтому повтор
    прогона, заведший по той же аудитории ВТОРУЮ рассылку с новым id, отправит
    всем сообщение заново: первой рассылке он не указ. Значит кто в исполнителе
    операций заводит рассылку, тот обязан сверяться с отметками.

    `broadcaster.start` сам под это правило НЕ попадает намеренно: поднять
    задачу по СУЩЕСТВУЮЩЕЙ строке (возобновление прерванных, перезапуск
    заглохшей) безопасно — журнал у неё свой и он помнит, кому уже ушло.
    """
    src = _source("services/op_worker.py")
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    creators: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        body = "".join(lines[node.lineno - 1:(node.end_lineno or node.lineno)])
        if re.search(r"\b_?db\.create_broadcast\(", body):
            creators[node.name] = body
    assert len(creators) >= 2, (
        f"нашлось {len(creators)} мест, заводящих рассылку, — измеритель ослеп "
        "(переименовали create_broadcast?), и правило перестало защищать")
    naked = [
        n for n, body in creators.items()
        if not any(m in body for m in (
            "launched_bot_ids", "launched_bc_ids", 'params["broadcast_id"]',
            "settled_targets", "completed_targets"))
    ]
    assert not naked, (
        "операция заводит новую рассылку, не сверившись с отметками уже "
        "запущенного: повтор прогона отправит сообщение той же аудитории "
        "второй раз, потому что журнал доставок ведётся по id рассылки — "
        + str(naked))


def test_the_derived_rule_knows_a_bare_creator():
    """Проверка измерителя производного правила на заведомо голом примере."""
    src = (
        "async def _exec_bare(pool, params):\n"
        "    bc = await db.create_broadcast(pool, 1, 'x', 2, 3)\n"
        "    broadcaster.start(pool, None, bc)\n")
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    found = {
        n.name: "".join(lines[n.lineno - 1:(n.end_lineno or n.lineno)])
        for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
    }
    body = found["_exec_bare"]
    assert re.search(r"\b_?db\.create_broadcast\(", body)
    assert not any(m in body for m in ("launched_bot_ids", 'params["broadcast_id"]'))
