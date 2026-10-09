"""Перепись: исполнитель, меняющий внешний мир, не повторяет сделанное.

ЧТО ЗАЩИЩАЕМ. Повтор поднимает исполнителя заново с `done_items=0` и ведёт его
по ВСЕМУ списку целей сначала — и это не решение человека: так работает
`_maybe_requeue` после сетевого сбоя, так сбрасывает зависшую операцию сторож,
так воскрешает незавершённые операции старт воркера (ветка едет на Railway,
то есть перезапуск — штатное событие каждого деплоя). Для операции, которая
что-то СОЗДАЁТ во внешнем мире, второй проход — не лишняя работа, а видимый
результат: второе промо-сообщение человеку, второй пост в канал, второй
joinChannel в счёт суточного лимита и давления, ведущего к PEER_FLOOD.

ЧЕМ ЭТА ПРОВЕРКА ОТЛИЧАЕТСЯ ОТ СОСЕДНЕЙ. `test_op_resume_no_duplicate_actions`
стережёт СПИСОК из восьми исполнителей: проверяет, что каждый из них читает
журнал и что ключ пропуска совпадает с ключом записи. Это сильнее по глубине,
но список ведётся руками — новый исполнитель, рассылающий сообщения, в него не
попадёт, и никто об этом не узнает. Здесь перепись ЗАКРЫТАЯ: берутся ВСЕ
исполнители, и каждый, кто мутирует внешний мир, обязан либо пользоваться
известной дверью идемпотентности, либо стоять в `HARMLESS` с написанной
причиной. Третьего варианта нет.

ПОЧЕМУ ДВЕРЕЙ НЕСКОЛЬКО. Их действительно несколько, и это не беспорядок:
единица работы у исполнителей разная. `completed_targets` — цель-строка;
`completed_account_targets` — пара (аккаунт, цель), иначе успех одного аккаунта
закрыл бы цель для всего флота; `completed_steps` — шаг многошаговой операции;
`settled_targets` — цель, по которой вопрос закрыт (успех ИЛИ отказ, который
повторять незачем). У инвайта дверь своя и более сильная: `invite_dedup`
переживает не только повтор, но и следующие операции.

Проверка по исходнику: op_worker импортирует telethon и в тестовой среде не
поднимается — так же делают соседние тесты очереди.
"""
from __future__ import annotations

import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "services", "op_worker.py")

# Вызовы, которые МЕНЯЮТ внешний мир: отправляют, вступают, создают, правят.
# Список сознательно консервативный — лучше не заметить новый метод, чем
# объявить мутацией чтение (детектор, дающий находки в зрелом коде, почти
# всегда сломан).
MUTATING_CALLS = re.compile(
    r"\b(SendMessage|SendMedia|SendReaction|SendVote|JoinChannel|"
    r"ImportChatInvite|InviteToChannel|AddChatUser|CreateChannel|"
    r"EditMessage|DeleteMessages|ForwardMessages|SendReport|"
    r"ReportPeer|ReportSpam|send_message|send_file|send_read_acknowledge|"
    r"join_channel|invite_to|forward_messages|edit_message)\b"
)

# Двери идемпотентности. Ключ — как дверь выглядит в коде, значение — что
# служит единицей работы (для сообщения об ошибке).
DOORS = {
    "completed_targets(": "цель-строка",
    "completed_account_targets(": "пара (аккаунт, цель)",
    "completed_steps(": "шаг многошаговой операции",
    "settled_targets(": "цель, по которой вопрос закрыт",
    "_record_invited_targets(": "дедуп инвайта (переживает и следующие операции)",
    "invite_dedup": "дедуп инвайта",
    "niche_growth_targets": "свои таблица уже отработанных групп",
}

# Исполнители, которым повтор не вредит. Причина обязательна и проверяется:
# пустая строка не проходит. Запись здесь — осознанное решение, а не способ
# обойти проверку.
HARMLESS = {
    "_exec_read_all_dialogs":
        "помечает прочитанными только НЕпрочитанные диалоги; повторное "
        "send_read_acknowledge по уже прочитанному не делает ничего и ничего "
        "не создаёт",
    "_exec_channel_add":
        "одна цель на операцию: повтор стоит одного joinChannel по каналу, в "
        "котором аккаунт уже состоит, а запись в managed_channels идёт через "
        "ON CONFLICT DO UPDATE",
    "_exec_community_liven":
        "вступление во свою же ноду: членство пишется идемпотентно "
        "(add_node_member), «уже в чате» засчитывается как успех. Повтор может "
        "привести в ноду ЛИШНИЕ аккаунты сверх count — это ограничено "
        "потолком 50 и не создаёт ничего видимого людям",
}


def _read() -> str:
    with open(SRC, encoding="utf-8") as f:
        return f.read()


def _executors() -> dict[str, str]:
    """Имя исполнителя → его исходник. По дереву разбора, не регуляркой."""
    src = _read()
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("_exec_"):
            continue
        out[node.name] = "".join(lines[node.lineno - 1:(node.end_lineno or node.lineno)])
    return out


def _mutates(body: str) -> bool:
    return bool(MUTATING_CALLS.search(body))


def _door(body: str) -> str | None:
    for token in DOORS:
        if token in body:
            return token
    return None


@pytest.fixture(scope="module")
def execs() -> dict[str, str]:
    return _executors()


# ── Главное: перепись закрыта ────────────────────────────────────────────────

def test_every_mutating_executor_has_a_door_or_a_written_reason(execs):
    unguarded = sorted(
        name for name, body in execs.items()
        if _mutates(body) and not _door(body) and name not in HARMLESS
    )
    assert not unguarded, (
        "исполнитель меняет внешний мир и не умеет пропускать сделанное: "
        f"{unguarded}. Повтор (сетевой сбой, сброс зависшей, деплой) проведёт "
        "его по всем целям заново — это второе сообщение человеку, второй пост, "
        "второй joinChannel в счёт лимита. Добавьте дверь идемпотентности или, "
        "если повтор действительно безвреден, запись в HARMLESS с причиной"
    )


def test_every_harmless_entry_carries_a_real_reason():
    for name, reason in sorted(HARMLESS.items()):
        assert reason and reason.strip(), f"{name}: причина не написана"
        assert re.search(r"[а-яА-Я]", reason), (
            f"{name}: причина обязана быть по-русски — её читает владелец кода")
        assert len(reason) > 40, (
            f"{name}: причина слишком короткая, чтобы быть разбором: {reason!r}")


def test_no_stale_names_in_the_harmless_list(execs):
    """Переименовали исполнителя — запись перестаёт его покрывать молча."""
    missing = sorted(set(HARMLESS) - set(execs))
    assert not missing, (
        f"в HARMLESS есть исполнители, которых больше нет: {missing}")


def test_a_harmless_entry_is_not_hiding_a_guarded_executor(execs):
    """Запись в HARMLESS для исполнителя С дверью — мёртвая и путает."""
    with_door = sorted(n for n in HARMLESS if _door(execs.get(n, "")))
    assert not with_door, (
        f"эти исполнители пользуются дверью, запись в HARMLESS лишняя: {with_door}")


# ── Самопроверка измерителя ──────────────────────────────────────────────────

def test_the_detector_finds_both_mutations_and_doors(execs):
    """Пустая перепись проходит сама собой — убедимся, что она не пустая."""
    assert len(execs) > 40, f"исполнителей найдено всего {len(execs)} — разбор сломан"
    assert sum(1 for b in execs.values() if _mutates(b)) >= 5, (
        "мутирующих исполнителей не найдено вовсе — список MUTATING_CALLS разъехался")
    assert sum(1 for b in execs.values() if _door(b)) >= 8, (
        "дверей идемпотентности не найдено — список DOORS разъехался с кодом")


def test_every_listed_door_is_actually_used_by_someone(execs):
    """Опечатка в имени двери иначе молча пропускала бы всех подряд."""
    bodies = list(execs.values())
    unused = sorted(t for t in DOORS if not any(t in b for b in bodies))
    assert not unused, (
        f"эти двери не использует ни один исполнитель: {unused}. Либо опечатка "
        "в названии (и тогда дверь никого не проверяет), либо дверь мертва")


def test_the_detector_would_notice_a_door_that_disappeared(execs):
    """Укус: у исполнителя с дверью её убираем — перепись обязана его назвать."""
    guarded = next((n for n, b in execs.items() if _mutates(b) and _door(b)), None)
    assert guarded, "нет ни одного мутирующего исполнителя с дверью — нечем проверить"
    bitten = execs[guarded]
    for token in DOORS:
        bitten = bitten.replace(token, "СТЁРТО(")
    assert _mutates(bitten) and _door(bitten) is None, (
        f"{guarded}: после удаления двери перепись его всё равно считает "
        "защищённым — значит она не проверяет ничего")


def test_a_reading_only_executor_is_not_called_a_mutation(execs):
    """Обратная сторона: детектор не должен объявлять мутацией чтение."""
    reading_only = [n for n, b in execs.items() if not _mutates(b)]
    assert len(reading_only) >= 10, (
        "детектор считает мутирующими почти всех — список MUTATING_CALLS "
        f"слишком широкий (не мутируют всего {len(reading_only)})")
