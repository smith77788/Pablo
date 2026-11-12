"""Присмотр за фоновым циклом: перезапуск после падения — и НИКОГДА вхолостую.

ПОЧЕМУ ЭТО ОТДЕЛЬНЫЙ МОДУЛЬ. Присмотр жил вложенной функцией внутри `main()` и
был поэтому непроверяем: тест не может вызвать функцию, объявленную внутри
корутины запуска процесса. Ровно в этом месте и пряталась авария, которую
описывает следующий абзац.

ЧТО ЛОМАЛОСЬ. Присмотр выглядел так:

    while True:
        try:
            await fn(*args)
        except Exception:
            ... sleep(30)

Все фоновые сервисы — бесконечные циклы, поэтому `fn` не возвращается никогда, и
ветки «вернулся штатно» как будто не существует. Но три сервиса возвращаются
СРАЗУ и СИНХРОННО, если выключены настройкой:

    db_backup.run_backup_loop    — DB_BACKUP_ENABLED=0 или не задан чат назначения
    account_rehab.run_rehab_loop — INFRAGRAM_DISABLE_REHAB
    payment_webhook.run          — сервер, который вообще может завершиться

Возврат без единого `await` означает, что корутина не отдала управление циклу
событий. `while True` тут же вызывает её снова — и снова, и снова, не уступая
никому. Это не «лишний перезапуск», это ЗАВИСАНИЕ ВСЕГО ПРОЦЕССА: вместе с
выключенным бэкапом встают бот, HTTP-API, мини-апп и воркер операций. То есть
штатный выключатель одной второстепенной подсистемы убивал продукт целиком.

ЧТО ТЕПЕРЬ. Штатный возврат — это решение сервиса («я выключен» или «я
закончил»), а не сбой. Для фонового цикла присмотр на этом прекращается: снова
звать того, кто только что сказал «мне нечего делать», бессмысленно. Для
сетевого сервиса (`restart_on_return=True`) перезапуск осмыслен, но идёт через
ту же паузу, что и после падения, — вхолостую не крутимся никогда.
"""

from __future__ import annotations

import asyncio
import logging

from services.platform_alert import Alerter, esc, safe_alert

log = logging.getLogger(__name__)


async def supervise(
    name: str,
    fn,
    *args,
    restart_delay: float = 30.0,
    start_delay: float = 0.0,
    restart_on_return: bool = False,
    alert: Alerter | None = None,
) -> None:
    """Держать фоновый сервис живым, перезапуская его после падений.

    fn(*args) вызывается заново на каждый перезапуск — корутину нельзя
    переиспользовать, повторный await по уже завершённой бросает RuntimeError.

    Аргументы:
        name             — имя для логов.
        restart_delay    — пауза перед перезапуском после падения (и после
                           штатного возврата, если он разрешён).
        start_delay      — пауза перед первым запуском (разнос стартов, чтобы
                           сервисы не били в БД одновременно).
        restart_on_return— перезапускать ли после ШТАТНОГО возврата. False для
                           фоновых циклов (вернулся значит выключен), True для
                           сетевых сервисов, которым положено подниматься снова.
        alert            — оповещение админов (`platform_alert.make_alerter`).
                           Падение и неожиданный возврат сетевого сервиса
                           сообщаются админу, а не только пишутся в лог (D27);
                           повтор по одному сервису — не чаще раза в час.

    Отмена (CancelledError) пробрасывается: остановка процесса — не сбой.
    """
    # Имя на самой задаче: без него в логе видно только «Task-37», и сообщение
    # об упавшей фоновой задаче не говорит, КАКОЙ сервис отвалился.
    try:
        _task = asyncio.current_task()
        if _task is not None:
            _task.set_name(f"svc:{name}")
            # Для сторожа зависаний (run_stall_watch ниже).
            _RUNNING[name] = _task
    except Exception:
        pass
    if start_delay > 0:
        await asyncio.sleep(start_delay)
    try:
        await _supervise_loop(name, fn, args, restart_delay,
                              restart_on_return, alert)
    finally:
        if _RUNNING.get(name) is asyncio.current_task():
            _RUNNING.pop(name, None)


async def _supervise_loop(name, fn, args, restart_delay, restart_on_return,
                          alert) -> None:
    crashes = 0
    while True:
        try:
            await fn(*args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(
                "Сервис %s упал: %s — перезапуск через %.0fс",
                name, e, restart_delay, exc_info=True,
            )
            crashes += 1
            await safe_alert(
                alert, f"svc-crash:{name}",
                f"⚠️ <b>Фоновый сервис упал:</b> <code>{esc(name)}</code>\n"
                f"{esc(type(e).__name__)}: {esc(e)}\n"
                f"Перезапуск через {restart_delay:.0f}с. "
                f"Падений с запуска процесса: {crashes}.",
            )
            await asyncio.sleep(restart_delay)
            continue

        # Сюда попадаем ТОЛЬКО при штатном возврате. Раньше этой ветки не было,
        # и управление уходило прямо в начало while — без единого await, то есть
        # без шанса для цикла событий выполнить хоть что-то ещё.
        if not restart_on_return:
            log.info(
                "Сервис %s завершился штатно (обычно это «выключен настройкой») "
                "— присмотр прекращён, перезапускать нечего",
                name,
            )
            return
        log.warning(
            "Сервис %s завершился штатно, хотя должен работать постоянно — "
            "перезапуск через %.0fс", name, restart_delay,
        )
        await safe_alert(
            alert, f"svc-returned:{name}",
            f"⚠️ <b>Сервис остановился сам:</b> <code>{esc(name)}</code>\n"
            f"Он должен работать постоянно. Перезапуск через {restart_delay:.0f}с.",
        )
        await asyncio.sleep(restart_delay)


# ── Сторож зависших проходов ──────────────────────────────────────────────────
#
# ЗАЧЕМ. Присмотр выше лечит ПАДЕНИЕ сервиса: исключение → перезапуск и
# оповещение. Но сервис может не упасть, а ПОВИСНУТЬ: ожидание без потолка
# (полуоткрытый сокет, блокировка, ответ, которого не будет). Снаружи это
# неотличимо от «работает»: задача жива, исключения нет, в логе тишина, а
# рассылки не уходят и операции не берутся — пока владелец не заметит сам.
#
# КАК. Каждый присматриваемый сервис регистрируется по имени. Раз в минуту
# сторож смотрит, чего ждёт его задача (`_fut_waiter` — объект, на котором она
# сейчас стоит). Нормальный цикл в простое ждёт `asyncio.sleep`, события или
# очереди — это не зависание. Проход, который работает, ждёт каждый раз НОВЫЙ
# объект (следующий запрос, следующая отправка). Зависание — это ОДИН И ТОТ ЖЕ
# объект ожидания дольше `stall_after`: проход стоит на одном месте. Тогда —
# метрика, предупреждение в лог со стеком ожидания (только места в коде, без
# значений) и оповещение админу, один раз на зависание. Поведение сервисов
# сторож не меняет и ничего не прерывает.

_RUNNING: dict[str, asyncio.Task] = {}

STALLED_SECONDS = "infragram_service_stalled_seconds"
STALLS_TOTAL = "infragram_service_stalls_total"

# Ожидания простоя: внутри них цикл «спит по плану», а не висит. wait_for —
# ожидание с потолком: оно закончится само, зависанием не считается.
_IDLE_AWAITS = {
    ("tasks.py", "sleep"), ("tasks.py", "wait_for"),
    ("locks.py", "wait"), ("queues.py", "get"),
    ("timeouts.py", "__aexit__"),
}


def _await_chain(task: asyncio.Task) -> list[tuple[str, str, int]]:
    """Цепочка ожидания задачи: (файл, функция, строка) от внешней к внутренней."""
    out: list[tuple[str, str, int]] = []
    c = task.get_coro()
    for _ in range(64):
        if c is None:
            break
        fr = getattr(c, "cr_frame", None) or getattr(c, "gi_frame", None)
        if fr is None:
            break
        out.append((fr.f_code.co_filename, fr.f_code.co_name, fr.f_lineno))
        c = getattr(c, "cr_await", None) or getattr(c, "gi_yieldfrom", None)
    return out


def _is_idle(task: asyncio.Task, chain) -> bool:
    import os as _os
    waiter = getattr(task, "_fut_waiter", None)
    if waiter is None:
        return True       # задача выполняется прямо сейчас (CPU — забота loop_lag)
    if type(waiter).__name__ == "_GatheringFuture":
        # Сервис, собранный из нескольких вечных циклов через gather, ждёт их
        # всю жизнь — это его штатное состояние, а не зависание.
        return True
    asyncio_dir = _os.path.dirname(asyncio.__file__)
    for fname, func, _ln in chain:
        if _os.path.dirname(fname) == asyncio_dir and \
                (_os.path.basename(fname), func) in _IDLE_AWAITS:
            return True
    return False


class StallWatch:
    """Состояние сторожа: что и с какого момента ждёт каждый сервис."""

    def __init__(self, stall_after: float, alert: Alerter | None = None):
        self.stall_after = stall_after
        self.alert = alert
        self._seen: dict[str, tuple[object, float]] = {}
        self._reported: dict[str, object] = {}

    async def check_once(self, now: float | None = None) -> list[str]:
        """Один осмотр. Возвращает имена сервисов, о зависании которых сообщено."""
        import time as _time

        from services import metrics as _m

        now = _time.monotonic() if now is None else now
        reported: list[str] = []
        for name, task in list(_RUNNING.items()):
            if task.done():
                _RUNNING.pop(name, None)
                self._seen.pop(name, None)
                continue
            chain = _await_chain(task)
            if _is_idle(task, chain):
                self._seen.pop(name, None)
                _m.gauge(STALLED_SECONDS, 0.0, {"service": name})
                continue
            waiter = getattr(task, "_fut_waiter", None)
            prev = self._seen.get(name)
            if prev is None or prev[0] is not waiter:
                self._seen[name] = (waiter, now)
                _m.gauge(STALLED_SECONDS, 0.0, {"service": name})
                continue
            age = now - prev[1]
            _m.gauge(STALLED_SECONDS, age, {"service": name})
            if age < self.stall_after or self._reported.get(name) is waiter:
                continue
            self._reported[name] = waiter
            _m.inc(STALLS_TOTAL, {"service": name})
            where = "\n".join(f"  {f}:{ln} в {fn}" for f, fn, ln in chain[-6:])
            log.warning(
                "Сервис %s стоит на одном ожидании уже %.0f мин — проход "
                "повис, сервис жив, но не работает. Ждёт здесь:\n%s",
                name, age / 60, where)
            last = chain[-1] if chain else ("?", "?", 0)
            import os as _os
            await safe_alert(
                self.alert, f"svc-stalled:{name}",
                f"⏸ <b>Фоновый сервис завис:</b> <code>{esc(name)}</code>\n"
                f"Стоит на одном ожидании {age / 60:.0f} мин: "
                f"<code>{esc(_os.path.basename(last[0]))}:{last[2]}</code> "
                f"в <code>{esc(last[1])}</code>.\n"
                "Процесс жив, но этот сервис не работает. Помогает перезапуск.")
            reported.append(name)
        return reported


async def run_stall_watch(
    alert: Alerter | None = None, interval: float = 60.0,
    stall_after: float = 3600.0,
) -> None:
    """Раз в `interval` с осматривать присматриваемые сервисы (см. выше)."""
    watch = StallWatch(stall_after=stall_after, alert=alert)
    while True:
        await asyncio.sleep(interval)
        try:
            await watch.check_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("service_supervisor: осмотр зависаний не выполнен",
                        exc_info=True)


async def supervise_while_leader(
    name: str,
    fn,
    *args,
    is_leader,
    poll_s: float = 5.0,
    **supervise_kwargs,
) -> None:
    """`supervise`, но сервис живёт ТОЛЬКО пока процесс — лидер.

    Нужен горизонтальному исполнителю (OP_SHARDED_EXECUTOR=1): операции там
    разбирают все процессы-исполнители, а фоновые циклы (прогрев, призрак,
    восстановление, авто-ответы, расписания…) обязаны идти в ОДНОМ экземпляре
    — иначе каждый лишний процесс удваивал бы реальные действия аккаунтов.
    Лидер — держатель аренды исполнителя (`replica_guard.holds_executor_lease`).

    Не лидер — ждём. Стали лидером — запускаем обычный `supervise`. Потеряли
    лидерство — гасим сервис и снова ждём: новый лидер поднимет свой
    экземпляр, и двух не будет дольше одного опроса. Штатный возврат сервиса
    (выключен настройкой) завершает и эту обёртку — как у `supervise`.
    """
    while True:
        while not is_leader():
            await asyncio.sleep(poll_s)
        task = asyncio.create_task(
            supervise(name, fn, *args, **supervise_kwargs),
            name=f"svc:{name}")
        try:
            while not task.done():
                await asyncio.sleep(poll_s)
                if not is_leader():
                    log.warning(
                        "%s: процесс больше не лидер — фоновый сервис "
                        "останавливается до возврата лидерства", name)
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        log.exception("%s: сбой при остановке после потери лидерства", name)
                    break
            else:
                # Сервис закончился сам (выключен) — как у supervise.
                if not task.cancelled():
                    task.result()
                return
        except asyncio.CancelledError:
            task.cancel()
            raise
