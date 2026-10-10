"""Стартовый смоук: Dispatcher действительно собирается.

Зачем.

Сборка диспетчера — единственный крупный кусок старта процесса, который можно
выполнить в тесте: ему нужно только FSM-хранилище, ни базы, ни сети. При этом
ошибка в нём фатальна: main() падает ДО dp.start_polling, платформа рапортует
об успешном деплое (контейнер собрался и запустился), а бот не отвечает ни на
что и уходит в цикл перезапусков.

Пока этот код жил внутри main(), проверить его не мог ни один тест — тесты
импортируют модули и вызывают функции, а main() не вызывает никто. 2026-09-04
из-за одной лишней строки dp.include_router прод пролежал пять часов при 4540
зелёных тестах и зелёном CI на каждом коммите. Разбор исходника
(test_no_duplicate_router_include) закрывает ровно тот дубль; этот тест
закрывает класс целиком — что бы в сборке ни сломалось, здесь оно упадёт.

Собрать диспетчер можно РОВНО ОДИН РАЗ за процесс: роутеры — модульные
синглтоны, и aiogram намертво привязывает каждый к первому родителю. Поэтому
сборка здесь одна на весь файл, а её единственность проверяется отдельным
тестом ниже — это не досадное ограничение, а свойство, о котором стоит знать.
"""
from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture(scope="module")
def dispatcher():
    """Единственная сборка на весь модуль. Само её отсутствие падения и есть
    главная проверка — ровно это не поднималось в проде."""
    os.environ.setdefault("MANAGER_BOT_TOKEN", "1:test-token")
    os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:5432/test")
    main = importlib.import_module("main")

    from aiogram.fsm.storage.memory import MemoryStorage

    return main.build_dispatcher(MemoryStorage())


def test_dispatcher_builds(dispatcher):
    assert dispatcher is not None


def test_all_routers_attached(dispatcher):
    """Роутеры реально подключены, а не потерялись по дороге.

    Порог намеренно грубый: точное число меняется с каждым новым разделом
    продукта, а проверить надо другое — что подключение вообще произошло.
    """
    assert len(dispatcher.sub_routers) > 50, (
        f"подключено всего {len(dispatcher.sub_routers)} роутеров — "
        "похоже, часть разделов бота не зарегистрирована"
    )


def test_router_names_unique(dispatcher):
    """Два роутера с одним именем — почти всегда копипаста при добавлении
    раздела: один из них потом молча не получит апдейты."""
    names = [r.name for r in dispatcher.sub_routers]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"роутеры с повторяющимися именами: {sorted(dupes)}"


def test_error_handler_registered(dispatcher):
    """Без обработчика ошибок любое исключение в хендлере уходит в лог и
    молчание — пользователь видит бота, который «не отвечает»."""
    assert dispatcher.errors.handlers, "глобальный обработчик ошибок не зарегистрирован"


def test_second_build_is_rejected(dispatcher):
    """Фиксируем ограничение: роутеры-синглтоны позволяют собрать диспетчер
    один раз за процесс. Если однажды это перестанет быть правдой, тест
    покраснеет и скажет, что модуль можно упростить."""
    main = importlib.import_module("main")

    from aiogram.fsm.storage.memory import MemoryStorage

    with pytest.raises(RuntimeError, match="already attached"):
        main.build_dispatcher(MemoryStorage())
