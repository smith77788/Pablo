"""`partial` — терминальный статус, и он обязан доходить до истории и уборки.

ЧТО БЫЛО. `services/op_status.py` ввёл честный статус недоведённой работы:
операция, взявшая 203 цели из 380, больше не закрывается зелёным `done`. Но
списки статусов по продукту писались руками и остались на трёх состояниях. Из-за
этого частично выполненная операция проваливалась в щель:

  * в активных её нет — она не pending и не running;
  * в истории её нет — `operation_bus.list_recent` её не выбирал;
  * в уборке её нет — `db_maintenance` и «очистить завершённые» её не трогали.

Снаружи это выглядело так: массовая операция отработала наполовину, прислала
итог и ИСЧЕЗЛА из интерфейса, а её строка осталась в `operation_queue` навсегда.

Тест держит списки статусов сверенными с моделью состояний, а не с памятью того,
кто правил файл последним.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def test_partial_is_terminal_in_the_state_model():
    """Отправная точка: если это перестанет быть правдой, тест ниже бессмыслен."""
    from services import op_status

    assert op_status.PARTIAL in op_status.TERMINAL
    assert op_status.PARTIAL not in op_status.IN_FLIGHT


def test_history_lists_partial_operations():
    src = _read("services", "operation_bus.py")
    m = re.search(r"async def list_recent[\s\S]{0,1500}", src)
    assert m, "list_recent не найден"
    assert "'partial'" in m.group(0), (
        "частично выполненная операция не попадает в историю — владелец видит, "
        "как она исчезает из интерфейса после завершения"
    )


def test_maintenance_prunes_partial_operations():
    src = _read("services", "db_maintenance.py")
    m = re.search(r"_DONE_STATUSES\s*=\s*\(([^)]*)\)", src)
    assert m, "_DONE_STATUSES не найден"
    assert '"partial"' in m.group(1), (
        "строки partial не вычищаются никогда — operation_queue растёт бессрочно"
    )


def test_manual_cleanup_covers_every_terminal_status():
    """Кнопки «очистить завершённые» обязаны видеть то же, что модель состояний."""
    from services import op_status

    for path in (
        ("bot", "handlers", "mass_ops.py"),
        ("bot", "handlers", "admin.py"),
    ):
        src = _read(*path)
        idx = src.index("DELETE FROM operation_queue")
        seg = src[idx:idx + 400]
        for status in (op_status.DONE, op_status.PARTIAL, op_status.FAILED):
            assert f"'{status}'" in seg, (
                f"{path[-1]}: уборка не знает статус {status!r} — такие строки "
                f"копятся в очереди вечно"
            )
