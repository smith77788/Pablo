"""Ни один маршрут мини-аппа не теряется молча.

`services/mini_app_api.py` — 24 тысячи строк и 926 маршрутов, зарегистрированных
внутри ОДНОЙ функции `setup_routes`. Файл такого размера приходится разрезать на
модули, и у этой работы есть ровно один способ провалиться незаметно: маршрут,
который забыли зарегистрировать после переноса. Синтаксис цел, тесты обработчика
зелёные, а экран мини-аппа молча мёртв — именно так уже ломалась «Топология»
(дубль-хендлер при зелёном юните).

Поэтому список маршрутов зафиксирован слепком `tests/miniapp_routes_snapshot.txt`.
Пропал маршрут — тест называет его по имени. Добавился новый — тест тоже
падает, и это намеренно: слепок обновляется ОСОЗНАННО, одной командой

    python tests/update_miniapp_routes.py

и обновление видно в диффе рядом с кодом, который его оправдывает.
"""
from __future__ import annotations

from pathlib import Path

from tests.miniapp_routes import registered_routes

SNAPSHOT = Path(__file__).resolve().parent / "miniapp_routes_snapshot.txt"


def _snapshot() -> list[str]:
    return [ln for ln in SNAPSHOT.read_text("utf-8").splitlines() if ln.strip()]


def test_the_inventory_is_not_empty():
    """Страховка измерителя: пустой список проходил бы любое сравнение."""
    live = registered_routes()
    assert len(live) > 800, (
        f"маршрутов найдено всего {len(live)} — сломан обход роутера, "
        "и тогда сравнение ниже ничего не стережёт")
    assert len(_snapshot()) > 800, "слепок пуст или обрезан"


def test_no_route_disappeared():
    live = set(registered_routes())
    lost = sorted(set(_snapshot()) - live)
    assert not lost, (
        "маршруты пропали при переносе — экраны мини-аппа, которые их зовут, "
        "молча мертвы:\n  " + "\n  ".join(lost))


def test_new_routes_are_recorded_deliberately():
    live = set(registered_routes())
    added = sorted(live - set(_snapshot()))
    assert not added, (
        "появились маршруты, которых нет в слепке. Это нормально — но слепок "
        "обновляется осознанно:\n  python tests/update_miniapp_routes.py\n"
        "новые:\n  " + "\n  ".join(added))
