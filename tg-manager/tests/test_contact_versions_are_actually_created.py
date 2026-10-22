"""Версия контакта создаётся при правке — иначе экран «Версии» обещает зря.

НАХОДКА (22.10.2026). Экран версий контакта есть, ручка
/api/miniapp/uch/contacts/{id}/versions есть, откат
(rollback_to_version) есть, и на пустом списке экран писал владельцу
«Версии создаются при изменениях». Создавала версии РОВНО ОДНА функция,
versioning_engine.create_version, и её не звал НИКТО: ни правка контакта, ни
синхронизация, ни импорт — только тесты. Значит список версий был пуст
всегда, обещание на экране было ложным, а откату было некуда откатываться.

Это тот же дефект, что и с доверием контакта в тот же день: показатель
показан, движок написан и протестирован, писать его некому.

ПОЧЕМУ СНИМОК — ДО ПРАВКИ. Откат возвращает поля из снимка. Значит «вернуть
версию N» должно означать «вернуть, как было до правки N», и снимать надо
состояние ДО обновления, а не после.

ОБРАТНЫЙ КОНТРОЛЬ (22.10.2026, каждая проверка краснела на своей поломке):
убрал create_version из правки → «правка контакта снова не создаёт версию»;
переставил снимок после update_contact → «снимок снимается после правки —
откатываться будет некуда»; вернул откатчику собственный список полей →
«у откатчика снова свой список полей, разойдётся со снимком»; убрал
приведение даты в snapshot_of → TypeError: Object of type date is not JSON
serializable, и ровно на этой одной проверке.

ОБРАТНЫЙ КОНТРОЛЬ ВТОРОГО ЗАХОДА (подпись версии): заставил
`changed_against` возвращать всё присланное → «версия называет не только
изменённое»; убрал проверку `if changed:` → пустая правка снова пишет версию;
вернул подпись к именам столбцов → «телефоны, день рождения» перестало
совпадать.
"""
from __future__ import annotations

from services.contacts_hub import versioning_engine as ve
from tests.miniapp_source import py_function


def test_editing_a_contact_creates_a_version():
    handler = py_function("services/mini_app_uch.py", "uch_contact_update")
    assert "create_version(" in handler, (
        "правка контакта снова не создаёт версию: экран «Версии» останется "
        "пустым навсегда, а откат — бесполезным")
    # Снимок берётся ДО обновления: иначе версия хранит уже новое состояние и
    # откат не возвращает ничего.
    assert handler.index("get_contact(") < handler.index("update_contact("), (
        "снимок снимается после правки — откатываться будет некуда")
    assert "snapshot_of(" in handler, "в версию уходит сырая строка из базы"


def test_a_failed_version_does_not_fail_the_edit():
    """Правка уже применена: ронять ответ из-за версии значит соврать об ошибке."""
    handler = py_function("services/mini_app_uch.py", "uch_contact_update")
    i = handler.index("create_version(")
    around = handler[max(0, i - 200):i + 400]
    assert "try:" in around and "except Exception:" in around, (
        "непишущаяся версия уронит успешную правку")


def test_the_snapshot_covers_everything_rollback_can_restore():
    """Один список полей, а не два: иначе откат вернёт не всё, и молча.

    Список был внутри rollback_to_version, а снимок собирался отдельно —
    поле, добавленное в откат, в снимок бы не попало.
    """
    rollback = py_function("services/contacts_hub/versioning_engine.py",
                           "rollback_to_version")
    assert "RESTORABLE_FIELDS" in rollback, (
        "у откатчика снова свой список полей, разойдётся со снимком")
    snap = py_function("services/contacts_hub/versioning_engine.py", "snapshot_of")
    assert "RESTORABLE_FIELDS" in snap, "снимок собирается не по тому списку"
    assert len(ve.RESTORABLE_FIELDS) >= 19, (
        f"восстановимых полей стало {len(ve.RESTORABLE_FIELDS)} — список усох")


def test_the_snapshot_survives_json():
    """Дата рождения из базы приходит как date, а снимок уходит в json.dumps."""
    import datetime
    import json

    snap = ve.snapshot_of({
        "first_name": "Иван",
        "birthday": datetime.date(1990, 5, 1),
        "created_at": datetime.datetime(2026, 1, 1, 12, 0),   # не восстановимое
        "tags": ["vip"],
    })
    json.dumps(snap)                      # падало бы на date
    assert snap["birthday"] == "1990-05-01"
    assert "created_at" not in snap, (
        "в снимок попало поле, которого откат не восстанавливает")
    assert snap["tags"] == ["vip"]


def test_a_version_names_only_what_changed():
    """Экран отправляет карточку ЦЕЛИКОМ — «пришло» не значит «изменилось».

    Версия подписывалась перечислением всех присланных полей, то есть всегда
    всей карточки: владелец правил имя, а в истории читал, что изменились и
    телефоны, и теги, и день рождения. Сравниваем по значению, с приведением:
    jsonb приходит из базы строкой, дата — `date`, пустое поле бывает и NULL,
    и пустой строкой, и пустым списком.
    """
    snapshot = {
        "first_name": "Иван",
        "phones": '["+79990000000"]',     # jsonb из базы — строкой
        "birthday": "1990-05-01",
        "notes": None,
        "tags": [],
    }
    updates = {
        "first_name": "Пётр",             # изменилось
        "phones": ["+79990000000"],       # то же, но списком
        "birthday": "1990-05-01",         # то же
        "notes": "",                      # пусто было, пусто и осталось
        "tags": [],
    }
    assert ve.changed_against(snapshot, updates) == ["first_name"], (
        ve.changed_against(snapshot, updates))


def test_an_edit_that_changes_nothing_makes_no_version():
    """Открыть карточку и нажать «Сохранить» — не событие истории."""
    snapshot = {"first_name": "Иван", "phones": '["+7"]'}
    assert ve.changed_against(snapshot, {"first_name": "Иван", "phones": ["+7"]}) == []
    handler = py_function("services/mini_app_uch.py", "uch_contact_update")
    assert "if changed:" in handler, (
        "версия пишется без проверки изменений — история забьётся пустыми правками")
    assert handler.index("if changed:") < handler.index("create_version("), (
        "версия пишется до проверки изменений — история забьётся пустыми правками")


def test_the_summary_is_read_by_the_owner_so_it_is_in_russian():
    """`change_summary` печатается на экране «Версии» как есть."""
    missing = [f for f in ve.RESTORABLE_FIELDS if f not in ve.FIELD_LABELS]
    assert not missing, f"поля без подписи попадут на экран именами столбцов: {missing}"
    s = ve.summary_of(["phones", "birthday"])
    assert s == "телефоны, день рождения", s
    assert ve.summary_of([]) is None, "пустая подпись должна отсутствовать, а не быть пустой строкой"
    # Имя собственное в подписи законно («имя в Telegram»), имя столбца — нет.
    for f, label in ve.FIELD_LABELS.items():
        assert any("а" <= c.lower() <= "я" or c == "ё" for c in label), (
            f"{f}: подпись «{label}» не по-русски")
        assert "_" not in label, f"{f}: на экран уедет имя столбца «{label}»"
