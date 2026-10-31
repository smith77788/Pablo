"""Каждый словарь подписей статусов аккаунта знает ВЕСЬ словарь мёртвых статусов.

Нашлось при проверке распознавания заморозки: статус `frozen` распознавался
верно, но владелец его не видел. Подписи статусов в продукте написаны
словарями в двух десятках мест, и у каждого — свой запасной вариант для
незнакомого ключа:

  * `_STATUS_EMOJI.get(acc_status, "✅")` в списке аккаунтов бота — замороженный
    и удалённый аккаунт показывались ЗЕЛЁНОЙ ГАЛОЧКОЙ «здоров»;
  * `labels.get(status, status)` / `_STATUS_LABELS.get(status, status)` — в итог
    проверки, на экран апелляции и в причину пропуска прогрева уходило сырое
    английское слово `frozen`, а владелец английского не читает.

Ни одна такая ошибка не падает — она молча показывает неправду. Поэтому
правило по классу, а не по найденным местам: словарь, в котором есть ключи
`spamblock` и `banned` (то есть словарь статусов аккаунта) и значения —
строки (подписи или значки), обязан знать каждый статус из
`account_status.DEAD_STATUSES`. Словари СЧЁТЧИКОВ (значения — числа и
выражения) правило не судит.
"""
from __future__ import annotations

import ast
import os
import re

from services.account_status import DEAD_STATUSES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_ROOTS = ("services", "bot", "database")
_MARK = {"spamblock", "banned"}


def _py_label_dicts(src: str):
    """(строка, ключи) для словарей-подписей статусов в исходнике Python."""
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if not isinstance(n, ast.Dict) or not n.keys:
            continue
        keys = {k.value for k in n.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if not _MARK <= keys:
            continue
        # подписи — строковые константы (в т.ч. склейка соседних литералов)
        if not all(isinstance(v, ast.Constant) and isinstance(v.value, str)
                   or isinstance(v, ast.JoinedStr) for v in n.values):
            continue
        yield n.lineno, keys


def _js_label_objects(src: str):
    for m in re.finditer(r"\{[^{}]{0,1500}\}", src):
        body = m.group(0)
        keys = set(re.findall(r"""['"]?([a-z_]+)['"]?\s*:\s*['"`]""", body))
        if _MARK <= keys:
            yield src[:m.start()].count("\n") + 1, keys


def _gaps():
    out = []
    for root in PY_ROOTS:
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, root)):
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(dirpath, f)
                with open(path, encoding="utf-8") as fh:
                    src = fh.read()
                for line, keys in _py_label_dicts(src):
                    miss = sorted(set(DEAD_STATUSES) - keys)
                    if miss:
                        out.append(f"{os.path.relpath(path, ROOT)}:{line} нет {miss}")
    ui = [os.path.join(ROOT, "mini_app", "index.html")] + [
        os.path.join(ROOT, "mini_app", "screens", f)
        for f in sorted(os.listdir(os.path.join(ROOT, "mini_app", "screens")))
        if f.endswith(".js")]
    for path in ui:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for line, keys in _js_label_objects(src):
            miss = sorted(set(DEAD_STATUSES) - keys)
            if miss:
                out.append(f"{os.path.relpath(path, ROOT)}:{line} нет {miss}")
    return out


def test_every_status_label_dict_knows_every_dead_status():
    gaps = _gaps()
    assert not gaps, (
        "Словарь подписей статусов не знает часть мёртвых статусов — владелец "
        "увидит английский ключ или ✅ на мёртвом аккаунте:\n  " + "\n  ".join(gaps))


def test_the_rule_actually_sees_label_dicts():
    # Проба на заведомо больном и заведомо здоровом: иначе ноль находок
    # неотличим от ослепшего правила.
    bad = 'X = {"active": "✅", "spamblock": "⚠️", "banned": "❌"}'
    assert list(_py_label_dicts(bad)), "правило не видит словарь подписей"
    counts = 'X = {"banned": f(1), "spamblock": g(2)}'
    assert not list(_py_label_dicts(counts)), "словарь счётчиков судить нельзя"
    js = "const A = {banned:'🚫', spamblock:'🔇', active:'🟢'};"
    assert list(_js_label_objects(js)), "правило не видит JS-объект подписей"


def test_the_rule_covers_the_places_it_was_written_for():
    # Если эти словари переедут или переименуются, правило не должно
    # молча перестать их видеть.
    files = set()
    for root in PY_ROOTS:
        for dirpath, _d, fs in os.walk(os.path.join(ROOT, root)):
            for f in fs:
                if f.endswith(".py"):
                    p = os.path.join(dirpath, f)
                    with open(p, encoding="utf-8") as fh:
                        if any(True for _ in _py_label_dicts(fh.read())):
                            files.add(os.path.relpath(p, ROOT))
    for must in ("bot/handlers/accounts.py", "services/op_worker.py",
                 "services/warmup_status.py", "bot/handlers/health_dashboard.py"):
        assert must in files, f"правило перестало видеть словарь подписей в {must}"
