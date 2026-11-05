"""Ловушка для будущего рефакторинга инвайта ссылкой в ЛС.

ЧТО ЗДЕСЬ ОХРАНЯЕТСЯ. Предзапись цели инвайта (`_premark_invite_targets`)
ставится ДО сети, а снимается с тех, кого «Telegram не рассматривал»
(`_unmark_untried_invites`). Решает, с кого снимать, вот эта ветка в op_worker:

    elif ("added" in res) or ("dead" in res):
        _settled = list(res.get("added") or []) + list(res.get("dead") or [])
    else:
        _settled = list(tried) if attempted else []

То есть движок, который возвращает `added`/`dead`, получает точное снятие (с
таймаутов метка снимается, и следующий прогон зовёт цель снова), а движок без
этих ключей — консервативный фолбэк: помеченными остаются ВСЕ, кого пробовали.

Для прямого инвайта точное снятие безопасно: повтор по уже добавленному
человеку упирается в `UserAlreadyParticipantError`, до самого человека ничего
не доходит. Для ссылки в ЛС это не так — там повтор это второе СООБЩЕНИЕ
человеку, то есть ровно тот риск, от которого предзапись и защищает.

`invite_via_link_batch` сегодня `added`/`dead` не возвращает и поэтому попадает
в консервативную ветку. Это не случайность, а то, что делает метод безопасным,
— но выглядит как «движку просто не дописали ключи». Этот тест не даёт дописать
их молча: если они понадобятся (например, для отчёта), в op_worker сначала
нужна отдельная корзина «ответ не получен», метка с которой НЕ снимается.
"""
from __future__ import annotations

import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "services", "mass_inviter_engine.py")


def _returned_dict_keys(func_name: str) -> set[str]:
    """Имена ключей во всех словарях, которые функция возвращает напрямую."""
    with open(ENGINE, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == func_name), None)
    assert fn is not None, f"в mass_inviter_engine нет функции {func_name}"
    keys: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for k in node.value.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    return keys


@pytest.mark.parametrize("key", ["added", "dead"])
def test_the_link_engine_does_not_report_settled_targets(key):
    """Движок ссылки не отдаёт поимённых исходов — иначе включится снятие метки."""
    keys = _returned_dict_keys("invite_via_link_batch")
    assert key not in keys, (
        f"invite_via_link_batch стал возвращать '{key}'. В op_worker это "
        "переключает пачку с консервативного фолбэка на точное снятие "
        "предзаписи, и с целей, по которым ответ не получен (таймаут, обрыв), "
        "метка снимется. Для ссылки в ЛС это второе сообщение человеку — тот "
        "самый риск, от которого предзапись защищает. Сначала сделайте в "
        "op_worker отдельную корзину «ответ не получен» и не снимайте с неё "
        "метку, и только потом добавляйте этот ключ."
    )


def test_the_direct_engine_does_report_settled_targets():
    """Самопроверка измерителя на заведомо положительном примере.

    Если разбор ключей перестанет что-либо находить, тест выше станет
    бессмысленно зелёным. Прямой инвайт эти ключи возвращает — и обязан.
    """
    keys = _returned_dict_keys("invite_batch")
    assert "added" in keys and "dead" in keys, (
        "invite_batch больше не возвращает added/dead — либо изменился контракт "
        "движка (тогда в op_worker ветка _settled читает не то), либо разбор "
        "ключей в этом тесте сломался и ничего не проверяет"
    )


def test_the_worker_still_decides_by_those_keys():
    """И сама ветка, вокруг которой всё это построено, на месте."""
    with open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8") as f:
        src = f.read()
    assert '("added" in res) or ("dead" in res)' in src, (
        "ветка выбора решённых целей в op_worker переписана — проверьте, что "
        "движок ссылки в ЛС по-прежнему попадает в консервативный путь, где "
        "метка не снимается ни с кого, кого пробовали"
    )
