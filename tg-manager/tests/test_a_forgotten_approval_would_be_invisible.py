"""Гейт подтверждения не подключён — и подключать его придётся вместе с глазами.

ЧТО ЕСТЬ СЕЙЧАС. `database/db.py::enqueue_op_with_approval` ставит операцию в
`waiting_approval` при `total_items > threshold`; `bot/handlers/approval_flow.py`
принимает нажатия «Подтвердить» и «Отмена», и его роутер подключён в `main.py`.
Принимающий конец живой, ставящего — нет: у `enqueue_op_with_approval` нет
вызывающих в рабочем коде (это уже отмечено в её докстринге), а клавиатуру
`_approval_kb` никто ни к одному сообщению не прикрепляет. Значит в проде
операция в `waiting_approval` не появляется НИКОГДА, и сам статус — пока
мёртвое слово, как 'scheduled'.

ПОЧЕМУ ЭТО ПРОВЕРКА, А НЕ ЗАМЕТКА. В тот день, когда гейт подключат, операция
начнёт вставать и ждать нажатия — и ждать молча, потому что смотреть на неё
нечем:

* `op_worker._watchdog_alerts` ловит три класса — долгий 'pending', долгий
  'running' и то, что откладываем МЫ (есть `scheduled_for` в будущем и
  причина). У `waiting_approval` нет ни срока, ни причины, и ни под один класс
  он не попадает;
* `database/db.py::get_pending_approvals` — ровно та функция, которая показала
  бы владельцу список ожидающих, — не вызывается ниоткуда;
* карточка с кнопками уходит один раз (и пока вообще не уходит): не нажал —
  напомнить нечему.

То есть владелец нажимает «запустить», операция честно встаёт в очередь и не
делается никогда, а ни один сторож об этом не говорит. Поэтому проверка
закрепляет СЕГОДНЯШНЮЮ правду: гейта нет. Станет красной — значит гейт
подключают, и вместе с ним надо закрыть глаза: класс в `_watchdog_alerts` на
нетерминальную операцию без срока и без причины, и поверхность, которая
показывает ожидающие подтверждения.
"""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Объявление функции вызовом не считается: перепись смотрит только на ast.Call.
GATE = "enqueue_op_with_approval"


def _runtime_files() -> list[pathlib.Path]:
    out = []
    for d in ("bot", "services", "database", "codex_bridge"):
        out.extend(sorted((ROOT / d).rglob("*.py")))
    out.append(ROOT / "main.py")
    return [p for p in out if p.is_file()]


def _callers_of(name: str) -> list[str]:
    """Файлы рабочего кода, где это имя РЕАЛЬНО вызывается."""
    found = []
    for p in _runtime_files():
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            called = (f.id if isinstance(f, ast.Name)
                      else f.attr if isinstance(f, ast.Attribute) else None)
            if called == name:
                found.append(f"{p.relative_to(ROOT)}:{n.lineno}")
    return sorted(found)


def test_the_approval_gate_has_no_caller_yet():
    callers = _callers_of(GATE)
    assert not callers, (
        f"гейт подтверждения подключили: {callers}. Теперь операция может "
        "встать в 'waiting_approval' и ждать нажатия молча — ни один класс "
        "_watchdog_alerts её не видит (нет ни scheduled_for, ни причины), а "
        "get_pending_approvals никто не вызывает. Прежде чем снимать эту "
        "проверку, добавьте класс алерта и поверхность со списком ожидающих — "
        "см. докстринг файла")


def _function_source(rel: str, name: str) -> str:
    """Текст функции по ГРАНИЦАМ из ast, а не по окну фиксированной длины.

    Отрицательная проверка («слова здесь нет») внутри окна от `index()` плюс
    N символов выключается молча: код сдвинулся, окно промахнулось, «искомого
    нет» стало правдой. Поэтому границы берём у разбора, а не на глаз, и
    отсутствие самой функции — тоже провал, а не тихое «нет слова».
    """
    src = (ROOT / rel).read_text(encoding="utf-8")
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return "\n".join(src.splitlines()[n.lineno - 1:n.end_lineno])
    raise AssertionError(f"{name} не найдена в {rel} — проверку надо перечитать")


def test_the_eyes_are_really_absent():
    """Не догадка: вот чего именно нет."""
    body = _function_source("services/op_worker.py", "_watchdog_alerts")
    assert "waiting_approval" not in body, (
        "сторож алертов научился видеть ожидающие подтверждения — значит "
        "половина долга закрыта, перечитайте докстринг этого файла")

    assert not _callers_of("get_pending_approvals"), (
        "список ожидающих подтверждения кто-то начал показывать — "
        "перечитайте докстринг этого файла")


def test_the_census_bites():
    """Перепись обязана находить вызов, когда он есть."""
    assert _callers_of("classify_final"), (
        "перепись вызовов слепа: заведомо живой вызов не найден")
