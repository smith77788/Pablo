"""Массовое удаление в боте спрашивает и называет число.

В мини-аппе необратимые массовые действия уже спрашивают
(`test_miniapp_mass_ops_confirm_scale.py`). В боте — нет: четыре экрана сносили
данные с одного нажатия, без вопроса и без числа.

* «🗑 Очистить всё» на экране алертов — `DELETE FROM restriction_events` по
  всему владельцу. Это журнал банов и ограничений, по нему потом разбирают,
  что привело к бану. Соседняя кнопка — «Назад».
* «Очистить участников» экосистемы — весь состав, собранный руками и
  автообнаружением.
* «Очистка мёртвых прокси» — все неназначенные прокси, которые не ответили на
  проверке. Проверка ошибается: провайдер мог лежать полчаса, а прокси куплены.
* «Удалить мёртвые аккаунты» — удаляло аккаунты сразу после проверки сессий.
  Соседняя кнопка на том же экране, «Перепроверить и удалить просроченные»,
  список показывает и спрашивает; эта — нет.

Правило теста: удаление, которое НЕ ограничено одной строкой по `id=$…`,
обязано иметь экран-вопрос с кнопкой отказа. Разделение по форме запроса, а не
по списку имён: новый массовый DELETE попадёт под правило сам.
"""
from __future__ import annotations

import ast
import functools
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
BOT = ROOT / "bot"

#: Массовое по форме, но безопасное по смыслу — с причиной у каждого.
ALLOWED_WITHOUT_QUESTION = {
    "cb_eco_sync_execute": "убирает участников, чьих аккаунтов уже нет; экран синхронизации перед этим показывает план",
    "cb_fn_step_delete": "одна строка по составному ключу funnel_id + step_order",
    "cb_clear_completed": "только done/failed старше 24 часов и только те, на чей журнал не опирается повтор",
}

#: Что считается экраном-вопросом: рядом с действием стоит отказ либо прямое «да».
_ASKS = re.compile(
    r'text\s*=\s*f?["\'][^"\']*(Отмена|Не удалять|Нет,|Да,|Да!|Подтверд|Удалить навсегда)'
)
_WORDED = re.compile(r"(Точно|точно ли|Уверен|уверен|необратим|нельзя вернуть)")
#: Удаление ровно одной строки: ключ задан равенством по id.
_SINGLE_ROW = re.compile(r"DELETE FROM\s+\w+\s+WHERE\s+id\s*=\s*\$", re.I)
_FILTER = re.compile(
    r'(\w+Cb)\.filter\(F\.action(?:\.in_\(\{([^}]*)\}|\s*==\s*"([^"]+)")'
)


@functools.lru_cache(maxsize=1)
def _all_functions() -> tuple:
    """Все функции бота: путь, имя, строка, тело, текст декораторов.

    Разбор всего каталога `bot/` стоит секунды, а нужен он в каждой проверке —
    поэтому считается один раз за прогон.
    """
    out = []
    for path in sorted(BOT.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        lines = src.splitlines()

        def seg(node) -> str:
            # срез по строкам, а не ast.get_source_segment: тот перечитывает
            # весь файл на каждый узел и превращает разбор bot/ в полминуты
            return "\n".join(lines[node.lineno - 1 : (node.end_lineno or node.lineno)])

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decs = " ".join(seg(d) for d in node.decorator_list)
            out.append((path, node.name, node.lineno, seg(node), decs))
    return tuple(out)


def _is_question_screen(body: str) -> bool:
    return bool(_ASKS.search(body) or _WORDED.search(body)) or ("⚠️" in body and "?" in body)


def _screens_asking_for(cb: str, action: str, exclude: str):
    pat = re.compile(re.escape(cb) + r'\s*\(\s*action\s*=\s*["\']' + re.escape(action) + r'["\']')
    for path, name, line, body, _decs in _all_functions():
        if name == exclude:
            continue
        if pat.search(body) and _is_question_screen(body):
            return f"{path.name}:{line} {name}()"
    return None


def _bulk_deleters():
    """Обработчики, чей DELETE не ограничен одной строкой по id."""
    out = []
    for path, name, line, body, decs in _all_functions():
        if "handlers" not in str(path) or "DELETE FROM" not in body:
            continue
        statements = re.findall(r"DELETE FROM[^\"']{0,200}", body)
        if statements and all(_SINGLE_ROW.search(st) for st in statements):
            continue
        m = _FILTER.search(decs)
        if not m:
            continue
        actions = [
            a.strip().strip("\"'")
            for a in (m.group(2) or m.group(3) or "").split(",")
            if a.strip()
        ]
        out.append((path, line, name, m.group(1), actions))
    return out


def test_detector_sees_deleters_and_questions():
    """Измеритель проверяется на заведомо известных случаях: он должен видеть
    и удаляющие обработчики, и экраны-вопросы, которые уже есть."""
    bulk = _bulk_deleters()
    assert len(bulk) >= 6, f"массовых удалений найдено всего {len(bulk)} — разбор обработчиков сломался"
    # экран-вопрос, который в репозитории был и до этой проверки
    assert _screens_asking_for("PersonaCb", "delete_confirm", exclude="") is not None, \
        "не найден известный экран-вопрос (удаление персоны) — распознавание вопроса сломалось"


def test_bulk_delete_has_a_question_screen():
    missing = []
    for path, line, name, cb, actions in _bulk_deleters():
        if name in ALLOWED_WITHOUT_QUESTION:
            continue
        if any(_screens_asking_for(cb, a, exclude=name) for a in actions):
            continue
        missing.append(f"{path.name}:{line} {name}() — {cb}(action={actions})")
    assert not missing, (
        "массовое удаление выполняется с одного нажатия, без вопроса и без "
        "числа удаляемого:\n  " + "\n  ".join(missing))


def test_question_screens_say_how_many():
    """Вопрос без числа бесполезен: «Удалить?» и «Удалить 412 записей?» — разные
    решения. У четырёх исправленных экранов число должно быть в кнопке."""
    for fname, fn in (
        ("botmother_menu.py", "cb_alerts_clear"),
        ("ecosystems.py", "cb_eco_members_clear"),
        ("proxy_manager.py", "cb_proxy_cleanup_dead"),
        ("accounts.py", "cb_del_dead_accounts"),
    ):
        body = next(
            (b for p, n, _l, b, _d in _all_functions() if p.name == fname and n == fn), None
        )
        assert body, f"{fname}: обработчик {fn} не найден"
        assert re.search(r'text\s*=\s*f"[^"]*Да, удалить \{', body), \
            f"{fname}:{fn} — в кнопке подтверждения нет числа удаляемого"
