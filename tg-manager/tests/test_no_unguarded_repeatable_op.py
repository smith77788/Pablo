"""Храповик: новый исполнитель операций не должен молча остаться без защиты
от повтора.

Что этот тест не даёт сделать: завести исполнителя, который ведёт журнал целей
и совершает внешнее действие, не ответив на вопрос «что будет, если прогон
пойдёт второй раз».

Вопрос не теоретический. Повтор — штатное событие, а не авария: его делает
`_maybe_requeue` после сетевого сбоя, сторож при сбросе зависшей операции,
старт воркера после перезапуска контейнера (ветка едет на Railway, перезапуск
происходит на каждом деплое). Исполнитель при этом идёт по списку целей
СНАЧАЛА, с `done_items=0`.

Для операций, которые что-то создают во внешнем мире, это не лишняя работа:
вторая публикация в канал, вторая жалоба с того же аккаунта, второй комплект
ботов при пределе BotFather в 20 штук, повторный joinChannel в давление к
PEER_FLOOD. Каждый такой случай находили ПОСЛЕ того, как он срабатывал, и
чинили по одному. Этот храповик закрывает класс целиком.

Ответить можно двумя способами, и оба честные:

  * подключить механизм возобновления — `completed_targets` (ключ — цель),
    `completed_steps` (ключ — номер шага, когда у цели нет устойчивого имени),
    `settled_targets` (для ЛС: закрыто и успехом, и недостижимостью),
    `completed_account_targets` (пара аккаунт+цель), либо свой, как у Global
    Presence с его `global_presence_targets`;
  * внести исполнителя в ALLOWED ниже с объяснением, почему повтор безвреден.

Второе — не лазейка: объяснение придётся написать, и оно останется в коде
рядом с именем исполнителя. Разница между «забыл» и «проверил, повтор
безопасен» в этом и состоит.

op_worker импортирует telethon и в тестовой среде не поднимается — разбираем
исходник, как и соседние тесты очереди.
"""
from __future__ import annotations

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Как исполнитель может узнать, что было сделано прошлым прогоном.
RESUME_MARKERS = (
    "completed_targets(",
    "completed_steps(",
    "settled_targets(",
    "completed_account_targets(",
    "_load_invited_targets(",
    "_revive_abandoned_gp_targets(",
)

# Как исполнитель ведёт журнал целей.
JOURNAL_MARKERS = (
    "INSERT INTO operation_log",
    "_log_target(",
    "_log_user(",
)

# Исполнители без механизма возобновления — и почему повтор для них безвреден.
# Добавляя сюда строку, отвечайте на один вопрос: что увидит владелец, если
# этот прогон пойдёт второй раз с нуля.
ALLOWED = {
    "_exec_check_accounts_health":
        "только читает состояние аккаунтов и пишет результат проверки; "
        "повторная проверка даёт тот же ответ и ничего не создаёт",
    "_exec_scan_owned_resources":
        "сканирование собственных ресурсов — чтение, наружу ничего не уходит",
    "_exec_compliance_scan":
        "проверка на соответствие правилам — чтение, наружу ничего не уходит",
    "_exec_find_contact":
        "поиск контакта — чтение; повтор стоит вызовов, но ничего не создаёт",
    "_exec_bulk_bot_edit":
        "ЗАДАЁТ значение поля бота через Bot API; повтор приводит к тому же "
        "состоянию, а не ко второму необратимому действию",
    "_exec_bulk_set_profile":
        "ЗАДАЁТ оформление аккаунта (имя, bio, аватар, 2FA); повтор приводит "
        "к тому же состоянию либо к честной ошибке, но не создаёт второй объект",
    "_exec_bulk_seo_apply":
        "ЗАДАЁТ SEO-поля канала; повтор приводит к тому же состоянию",
}


def _source() -> str:
    with open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8") as f:
        return f.read()


def _executors() -> dict[str, str]:
    """Имя исполнителя → его исходный текст."""
    src = _source()
    lines = src.splitlines()
    out: dict[str, str] = {}
    for node in ast.parse(src).body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name.startswith("_exec_"):
            out[node.name] = "\n".join(lines[node.lineno - 1:node.end_lineno])
    return out


def _keeps_a_journal(body: str) -> bool:
    return any(m in body for m in JOURNAL_MARKERS)


def _can_resume(body: str) -> bool:
    return any(m in body for m in RESUME_MARKERS)


# ── Сам храповик ─────────────────────────────────────────────────────────────

def test_every_journalling_executor_answers_the_repeat_question():
    unguarded = sorted(
        name for name, body in _executors().items()
        if _keeps_a_journal(body) and not _can_resume(body) and name not in ALLOWED
    )
    assert not unguarded, (
        "эти исполнители ведут журнал целей, но не переживут повтор:\n  "
        + "\n  ".join(unguarded)
        + "\n\nПодключите механизм возобновления (completed_targets / "
          "completed_steps / settled_targets / completed_account_targets) либо "
          "внесите исполнителя в ALLOWED в этом файле с объяснением, почему "
          "повтор безвреден."
    )


# ── Сторожа самого храповика ─────────────────────────────────────────────────
#
# Детектор, который ничего не находит, бесполезен и при этом выглядит зелёным.
# Проверяем, что он видит и защищённых, и незащищённых.

def test_detector_sees_the_executors_at_all():
    execs = _executors()
    assert len(execs) > 50, f"исполнителей найдено {len(execs)} — разбор сломан"


def test_detector_recognises_a_known_guarded_executor():
    """`_exec_mass_publish` защищён давно — детектор обязан это видеть."""
    body = _executors()["_exec_mass_publish"]
    assert _keeps_a_journal(body) and _can_resume(body)


def test_detector_would_catch_an_unguarded_executor():
    """Заведомо незащищённый образец обязан ловиться.

    Без этой проверки опечатка в JOURNAL_MARKERS сделала бы храповик пустым, и
    он бы молча пропускал всё подряд.
    """
    sample = (
        "async def _exec_sample(pool, bot, op_id, owner_id, params):\n"
        "    for t in params['targets']:\n"
        "        await do_something(t)\n"
        "        await pool.execute(\"INSERT INTO operation_log(op_id) VALUES($1)\", op_id)\n"
    )
    assert _keeps_a_journal(sample)
    assert not _can_resume(sample)


def test_allowlist_has_no_stale_entries():
    """Исполнителя переименовали или удалили — строка обязана уйти вместе с ним."""
    execs = _executors()
    gone = sorted(name for name in ALLOWED if name not in execs)
    assert not gone, f"в ALLOWED остались несуществующие исполнители: {gone}"


def test_allowlist_has_no_contradictions():
    """Исполнитель получил защиту — строка в ALLOWED больше не нужна и врёт."""
    execs = _executors()
    contradictory = sorted(
        name for name, why in ALLOWED.items()
        if name in execs and _can_resume(execs[name])
    )
    assert not contradictory, (
        f"у этих исполнителей защита от повтора уже есть, уберите их из ALLOWED: "
        f"{contradictory}")


def test_every_allowlist_entry_explains_itself():
    """Пустая отговорка — это «забыл», записанное в другом месте."""
    for name, why in ALLOWED.items():
        assert why and len(why) >= 40, f"{name}: объяснение слишком короткое"
