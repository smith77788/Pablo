"""Brotli для мини-аппа: страница и скрипты экранов на четверть легче gzip.

Зачем. Первое открытие мини-аппа по мобильной сети упирается в байты: до
первого экрана браузер тянет страницу (420 КБ в gzip) и скрипты экранов
(~200 КБ). Замер 09.11.2026 (Playwright, 3G 1,6 Мбит/с, CPU 4x): 4,1 с до
готовой главной, из них ~3 с — сеть. Brotli на максимальном качестве жмёт ту
же страницу в 299 КБ вместо 420 КБ, скрипты — в 172 КБ вместо 205 КБ.
Android WebView и iOS WKWebView принимают `br` по HTTPS.

Цена. Качество 11 стоит ~3,6 с CPU на страницу. Поэтому сжатие идёт ОДИН раз
на версию файла, в фоновом потоке (brotli отпускает GIL — цикл событий при
этом не стоит, замер: задержка цикла 10 мс), а пока оно не готово, отдаётся
gzip. То есть после деплоя первые открытия получают gzip, как раньше, а
следующие — brotli.

Нет пакета `brotli` — всё молча остаётся на gzip.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Hashable

log = logging.getLogger(__name__)

try:  # pragma: no cover - зависит от окружения
    import brotli as _brotli
except Exception:  # noqa: BLE001
    _brotli = None

QUALITY = 11

# Сжатое тело по имени файла: имя → (версия, байты). Одна версия на имя:
# деплой с новой версией вытесняет старую, кэш не растёт.
_br_cache: dict[str, tuple[Hashable, bytes]] = {}
# Сжатия, которые уже идут: не запускать одно и то же дважды.
_br_pending: set[tuple[str, Hashable]] = set()
# Ссылки на фоновые задачи, чтобы их не собрал сборщик мусора на полпути.
_br_tasks: set[asyncio.Task] = set()


def available() -> bool:
    return _brotli is not None


def accepts(accept_encoding: str | None) -> bool:
    """Клиент согласен на br. Точное слово, а не подстрока: «xbr» — не br."""
    for part in (accept_encoding or "").lower().split(","):
        if part.split(";", 1)[0].strip() == "br":
            return not _q_zero(part)
    return False


def _q_zero(part: str) -> bool:
    for p in part.split(";")[1:]:
        p = p.strip().lower()
        if p.startswith("q="):
            try:
                return float(p[2:]) == 0.0
            except ValueError:
                return False
    return False


def get_or_schedule(name: str, version: Hashable,
                    make_bytes: Callable[[], bytes]) -> bytes | None:
    """Готовое brotli-тело этой версии — или None и запуск сжатия в фоне.

    ``make_bytes`` зовётся только при запуске сжатия и должен быть дешёвым
    (тело уже лежит в памяти). Сбой сжатия не ломает ответ: вызывающий отдаёт
    gzip.
    """
    if _brotli is None:
        return None
    hit = _br_cache.get(name)
    if hit is not None and hit[0] == version:
        return hit[1]
    job = (name, version)
    if job in _br_pending:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    data = make_bytes()
    _br_pending.add(job)

    async def _run() -> None:
        try:
            body = await asyncio.to_thread(_brotli.compress, data, quality=QUALITY)
            _br_cache[name] = (version, body)
        except Exception:
            log.warning("miniapp_brotli: сжатие %s не удалось, остаётся gzip",
                        name, exc_info=True)
        finally:
            _br_pending.discard(job)

    task = loop.create_task(_run())
    _br_tasks.add(task)
    task.add_done_callback(_br_tasks.discard)
    return None
