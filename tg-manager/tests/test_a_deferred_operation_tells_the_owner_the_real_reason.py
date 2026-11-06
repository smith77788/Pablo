"""Операция, отложенная не из-за занятого флота, говорит СВОЮ причину.

КАК УСТРОЕНО ОЖИДАНИЕ. Исполнитель может попросить вернуть операцию в очередь
(`status: requeue`) — обычно потому, что свободных аккаунтов сейчас нет.
Ожидание ограничено по времени (`_ACCT_WAIT_MAX_MIN`), и когда предел
наступает, владельцу сообщают, из-за чего операция так и не пошла. Текст берётся
из ключа `reason`, который исполнитель кладёт рядом со статусом; без него
подставляется готовый текст про занятый флот.

ЧЕМ ПЛОХ ЧУЖОЙ ТЕКСТ. Просьба о повторе приходит не только от дефицита флота:
BotFather ограничил создание ботов, не прочитался журнал уже обработанных
целей, не прочиталась проверка живого двойника рассылки. Владелец в ответ на
любую из них читал «все аккаунты N минут были заняты другими операциями» и шёл
разгружать очередь вместо того, чтобы смотреть на настоящую помеху.

ЧТО ЗАКРЕПЛЕНО. Если текст для владельца (`summary`) говорит не про занятый
флот, рядом обязан стоять `reason` (или `defer_s` — у отложенных платформой
свой путь и свой текст). Перепись по исполнителю нашла ровно одно такое место
(проверка двойника рассылки); остальные сорок просьб — честно про флот, и
готовый текст им подходит.
"""

import ast
import pathlib
import re

OW = pathlib.Path(__file__).resolve().parent.parent / "services" / "op_worker.py"

# Слова, по которым видно, что речь действительно про занятый флот.
FLEET = re.compile(r"занят|флот|аккаунт|свободн", re.I)


def _requeues(src: str) -> list[tuple[str, int, set[str], str]]:
    """(функция, строка, ключи возврата, исходник summary) для каждого requeue."""
    out = []
    tree = ast.parse(src)
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
                continue
            keys = {}
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys[k.value] = v
            st = keys.get("status")
            if not (isinstance(st, ast.Constant) and st.value == "requeue"):
                continue
            summary = keys.get("summary")
            text = ast.get_source_segment(src, summary) if summary is not None else ""
            out.append((fn.name, node.lineno, set(keys), text or ""))
    return out


def _findings(src: str) -> list[str]:
    bad = []
    for name, lineno, keys, text in _requeues(src):
        if "reason" in keys or "defer_s" in keys:
            continue
        if FLEET.search(text):
            continue
        bad.append(f"{name} (строка {lineno})")
    return bad


def test_every_other_reason_reaches_the_owner_in_its_own_words():
    src = OW.read_text(encoding="utf-8")
    bad = _findings(src)
    assert not bad, (
        "исполнитель откладывает операцию не из-за занятого флота, но причину "
        "отдельным ключом не передаёт — по истечении предела ожидания владелец "
        "прочитает про занятые аккаунты и пойдёт разгружать очередь вместо "
        "настоящей помехи: " + ", ".join(sorted(bad))
    )


def test_the_census_sees_the_requeues_at_all():
    src = OW.read_text(encoding="utf-8")
    found = _requeues(src)
    assert len(found) >= 40, (
        f"просьб о повторе найдено {len(found)} — разбор сломан, и проверка "
        "была бы вечно зелёной"
    )
    assert any("reason" in keys for _n, _l, keys, _t in found), (
        "ни одна просьба не передаёт причину — ключ переименовали, и проверка "
        "смотрит в пустоту"
    )


def test_the_measurer_tells_the_cases_apart():
    sick = (
        "async def _exec_probe():\n"
        "    return {'status': 'requeue',\n"
        "            'summary': 'Не прочиталась проверка двойника'}\n")
    fleet = (
        "async def _exec_probe():\n"
        "    return {'status': 'requeue',\n"
        "            'summary': '⏳ Все аккаунты заняты другими операциями'}\n")
    told = (
        "async def _exec_probe():\n"
        "    return {'status': 'requeue', 'reason': 'BotFather ограничил',\n"
        "            'summary': 'Создание ботов пока недоступно'}\n")
    assert _findings(sick) == ["_exec_probe (строка 2)"], (
        "измеритель не видит умолчания о настоящей причине")
    assert _findings(fleet) == [], "измеритель ругается на честную просьбу про флот"
    assert _findings(told) == [], "измеритель ругается на переданную причину"


def test_the_reason_actually_reaches_the_deferring_call():
    """Ключ должен быть тем самым, который читает путь откладывания."""
    src = OW.read_text(encoding="utf-8")
    assert 'reason=str(result.get("reason") or "")' in src, (
        "путь откладывания больше не читает ключ reason — проверка выше "
        "закрепляет ключ, которого никто не слушает"
    )
