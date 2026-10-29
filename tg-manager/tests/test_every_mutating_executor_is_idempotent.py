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

ЧЕМ ПЕРЕПИСЬ СИЛЬНА. Ровно настолько, насколько детектор узнаёт действие.
Сначала он знал только запросы Telethon и имена aiogram — и не видел
исполнителя, который ходит наружу через `services/account_manager` или через
движок: пост в канал, ЛС, создание канала, выдача админки, пересылка,
кампания. Таких в файле больше, чем прямых запросов, так что закрытой перепись
была только на вид. Поэтому в список действий добавлены НАШИ глаголы, а в
список дверей — те, что подсистемы ведут сами (статус цели плана, курсор
связки, закреплённый в params id рассылки): защита у них не слабее, состояние
переживает рестарт.

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
    r"join_channel|invite_to|forward_messages|edit_message|"
    # Наши собственные глаголы. Перепись знала только запросы Telethon и имена
    # aiogram, то есть НЕ ВИДЕЛА исполнителя, который ходит наружу через
    # services/account_manager или движок: пост в канал, ЛС, создание канала,
    # выдача админки, пересылка, кампания. Таких исполнителей в файле больше,
    # чем прямых запросов, — перепись была закрытой только на вид.
    r"post_to_channel|send_dm|create_channel|create_forum_supergroup|"
    r"promote_to_admin|set_discussion_group|forward_new_posts|run_campaign|"
    r"update_profile|EditAdminRequest|ImportContactsRequest|"
    r"DeleteContactsRequest|invite_batch|add_via_promote|"
    r"invite_via_link_batch|invite_by_phones)\b"
    r"|broadcaster\.start"
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
    "niche_growth_targets": "своя таблица уже отработанных групп",
    # Двери, которые ведёт сама подсистема. Общими ключами они не пользуются,
    # но защита у них не слабее: состояние живёт в своей таблице и переживает
    # рестарт — ровно то, что от двери и требуется.
    "global_presence_targets": "цель плана присутствия (статус на строке, "
                               "захват через UPDATE ... WHERE status='pending')",
    "launched_bot_ids": "уже запущенный бот сети (список в params операции)",
    # Ключ — ИМЯ функции, а не имя таблицы: `community_node_members` читает и
    # `_exec_community_set_staff`, чтобы знать кого промоутить, и там это не
    # дверь. Реестр по имени таблицы прятал бы настоящую находку.
    "_node_members_already(": "кто в ноде уже состоит: прогон исключает своих "
                              "из выбора и доводит ноду ДО count, а не "
                              "добавляет ещё count",
    "last_msg_id": "курсор связки: двигается после КАЖДОГО пересланного поста",
    "_record_network_node(": "созданный узел связки (каталог и ref_id узла "
                             "ложатся ОДНОЙ транзакцией, с повтором, и узел с "
                             "ref_id повтор пропускает)",
    'params["broadcast_id"]': "строка рассылки закрепляется в params, повтор "
                              "подключается к ней, а не заводит вторую",
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
    "_exec_dm_campaign":
        "сам ничего не отправляет: вся работа в dm_engine.run_campaign, и дверь "
        "стоит там — журнал dm_campaign_log, из которого _get_targets исключает "
        "уже отправленных, заблокированных и пропущенных. Повтор продолжает "
        "кампанию с места обрыва, а прогресс считается от кумулятива",
    "_exec_bulk_update_profile":
        "ставит профилю ЗАДАННОЕ значение (имя, био, фото), а не добавляет: "
        "второй проход приводит аккаунт в то же состояние и ничего не создаёт. "
        "Цена повтора — лишние вызовы в счёт лимитов, не второй объект",
    "_exec_delete_contacts":
        "удаление идемпотентно по смыслу: повтор по уже удалённому контакту "
        "ничего не удаляет второй раз и ничего не создаёт",
    "_exec_community_set_staff":
        "выдаёт админку аккаунтам СВОЕЙ ноды: повторная выдача тех же прав "
        "людям не видна и второго объекта не создаёт (права те же). Цена "
        "повтора — лишний EditAdmin в счёт лимитов",
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


def test_the_census_sees_our_own_verbs(execs):
    """Перепись, которая не узнаёт мутацию, вечно зелёная.

    Проверка выше сильна ровно настолько, насколько детектор видит действия.
    Пока он знал только запросы Telethon, исполнитель, постящий через
    `account_manager.post_to_channel`, проходил переписью как читающий.
    """
    mutating = sorted(n for n, body in execs.items() if _mutates(body))
    assert len(mutating) >= 20, (
        f"распознано мутирующих исполнителей: {len(mutating)} — детектор сломан")
    for name in ("_exec_niche_growth_post", "_exec_crosspost_run",
                 "_exec_deploy_network", "_exec_dm_campaign",
                 "_exec_global_presence_channel"):
        assert name in mutating, f"{name} меняет внешний мир, а перепись его не видит"


def test_no_door_token_is_dead(execs):
    """Опечатка в токене двери не освобождает никого — и это надо заметить."""
    src = _read()
    dead = sorted(t for t in DOORS if t not in src)
    assert not dead, f"эти двери не встречаются в исходнике: {dead}"


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
