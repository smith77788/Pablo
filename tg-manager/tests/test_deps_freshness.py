"""Гейт: пины зависимостей пересматриваются, а не гниют молча (класс 16).

Тот же принцип, что у отпечатков клиента: версия библиотеки — стареющий актив.
Код не меняется, а совместимость с Telegram уходит САМА, без единой ошибки в
логах: Bot API выпускает новые версии, aiogram их поддерживает, а мы остаёмся
на старом пине и просто не получаем обновлений.

Гейт не заставляет обновляться (бамп фреймворка — деплой-риск, решение владельца),
он заставляет РЕГУЛЯРНО СМОТРЕТЬ: отставание должно быть осознанным выбором.

При красном: сверить актуальные версии (aiogram/telethon/asyncpg), обновить
заметку «ИЗВЕСТНОЕ ОТСТАВАНИЕ» в requirements.txt и поднять DEPS_REVIEWED.
"""
from __future__ import annotations

import datetime as dtm
import re
from pathlib import Path

REQ = Path(__file__).resolve().parents[1] / "requirements.txt"


def _meta(name: str) -> str:
    m = re.search(rf"^#\s*{name}:\s*(\S+)", REQ.read_text(encoding="utf-8"), re.M)
    assert m, f"в requirements.txt нет строки '# {name}: ...'"
    return m.group(1)


def test_dependencies_reviewed_recently():
    reviewed = dtm.date.fromisoformat(_meta("DEPS_REVIEWED"))
    max_age = int(_meta("DEPS_MAX_AGE_DAYS"))
    age = (dtm.date.today() - reviewed).days
    assert age <= max_age, (
        f"пины зависимостей не пересматривали {age} дн. (лимит {max_age}). "
        "Telegram и Bot API обновляются сами — отставание должно быть осознанным. "
        "Сверьте актуальные версии, обновите заметку об отставании в "
        "requirements.txt и поднимите DEPS_REVIEWED."
    )
    assert age >= 0, "DEPS_REVIEWED из будущего — дата ревизии недостоверна"


def test_hard_pin_is_explained():
    """Жёсткий пин критичной либы обязан быть ОБЪЯСНЁН — свежий он или намеренно
    отстающий. Пин без причины неотличим от забытого: следующий разработчик не
    знает, можно ли двигать, и либо боится трогать, либо ломает совместимость."""
    txt = REQ.read_text(encoding="utf-8")
    assert re.search(r"^aiogram==", txt, re.M), "aiogram должен быть запинен явно"
    assert re.search(r"#.*aiogram", txt), (
        "жёсткий пин aiogram без пояснения выглядит забытым — опишите, почему "
        "версия именно такая и что проверить при следующем апгрейде"
    )


def test_telethon_not_hard_pinned():
    """Telethon говорит с MTProto напрямую: жёсткий пин здесь опаснее всего —
    при смене слоя Telegram старый клиент отваливается. Держим нижнюю границу."""
    txt = REQ.read_text(encoding="utf-8")
    m = re.search(r"^telethon\s*([=><~!]+)", txt, re.M)
    assert m, "telethon должен присутствовать в requirements"
    assert m.group(1) != "==", (
        "жёсткий пин telethon блокирует обновления MTProto-слоя — используйте >="
    )
