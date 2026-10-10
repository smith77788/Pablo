"""Какие операции можно перезапустить — и почему это не только «failed».

Разрыв с живого прогона. Массовый инвайт остановился на 203 целях из 380 со
177 ошибками, но завершился со статусом `done` — потому что технически
исполнитель отработал и вернул сводку. Кнопка повтора показывалась ТОЛЬКО для
`failed`, поэтому перезапустить такую операцию было нечем: пользователь видел
«✅ done», недостигнутую цель и ни одного способа продолжить.

«Упавшая» с точки зрения человека — это не статус в базе, а НЕДОВЕДЁННАЯ ДО
КОНЦА работа: провалилась, была отменена, или закрылась `done`, не добрав цель
(остались необработанные элементы либо были ошибки).

Здесь только решение — без базы и сети, поэтому проверяется напрямую.
"""
from __future__ import annotations

# Статусы, где операция ещё живёт: повторять нечего, надо ждать или отменить.
# Источник правды — services/op_status.IN_FLIGHT; локальный кортеж остаётся
# фолбэком, чтобы модуль сохранил свойство «без зависимостей» (его импортируют
# из тестов и из мест, где services может быть не поднят целиком).
try:  # pragma: no cover - тривиальный фолбэк импорта
    from services.op_status import IN_FLIGHT as _IN_FLIGHT
except Exception:  # pragma: no cover
    _IN_FLIGHT = ("pending", "running", "paused", "scheduled", "waiting_approval")


def can_retry(status: str, done_items=0, total_items=0, err_count=0) -> tuple[bool, str]:
    """Можно ли перезапустить операцию. (можно, причина).

    Причина возвращается всегда — и для отказа, и для согласия: она уходит в
    интерфейс, чтобы кнопка не выглядела произвольной.
    """
    st = (status or "").strip().lower()

    def _int(v) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    done, total, errs = _int(done_items), _int(total_items), _int(err_count)

    if st in _IN_FLIGHT:
        return False, "операция ещё в работе — дождитесь завершения или отмените"
    if st == "failed":
        return True, "операция провалилась"
    if st == "cancelled":
        return True, "операция была отменена — можно запустить заново"
    if st == "partial":
        # Собственный статус недоведённой работы (services/op_status.py). Раньше
        # такая операция приходила сюда под видом «done», и правду приходилось
        # восстанавливать по счётчикам — ниже. Теперь статус говорит сам, а
        # счётчики лишь уточняют формулировку для интерфейса.
        if total > 0 and done < total:
            return True, f"обработано {done} из {total} — остаток не доработан"
        if errs > 0:
            return True, f"завершена с ошибками ({errs}) — можно повторить неудавшиеся"
        return True, "операция выполнена частично"
    if st == "done":
        # Главный случай: «done», но цель не достигнута.
        if total > 0 and done < total:
            return True, f"обработано {done} из {total} — остаток не доработан"
        if errs > 0:
            return True, f"завершена с ошибками ({errs}) — можно повторить неудавшиеся"
        return False, "операция выполнена полностью"
    # Неизвестный статус трактуем как незавершённый: лучше дать повторить, чем
    # запереть работу из-за статуса, которого мы не знаем.
    return True, f"статус «{st or 'неизвестен'}» — операция не подтверждена как выполненная"


def pick_retryable(rows) -> list[dict]:
    """Отобрать из списка операций те, что имеет смысл перезапустить.

    Ожидает записи с ключами id/status/done_items/total_items/err_cnt.
    Порядок входа сохраняется — вызывающий сам решает, как их сортировать.
    """
    out: list[dict] = []
    for r in (rows or []):
        try:
            ok, why = can_retry(r.get("status"), r.get("done_items"),
                                r.get("total_items"), r.get("err_cnt"))
        except AttributeError:
            continue
        if ok:
            out.append({"id": r.get("id"), "reason": why,
                        "op_type": r.get("op_type"), "label": r.get("label")})
    return out
