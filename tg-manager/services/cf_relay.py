"""
CF Relay — маршрутизация Telethon через Cloudflare Worker WebSocket→TCP relay.

Проблема: Railway (и другие PaaS) предоставляют IP адреса датацентров.
Telegram идентифицирует их и применяет более жёсткие ограничения к аккаунтам,
которые подключаются с таких IP — флуд-баны, спамблоки, сокращённые rate limits.

Решение: Cloudflare Worker принимает WebSocket соединения и проксирует TCP
трафик к Telegram DC. С точки зрения Telegram соединение приходит с Cloudflare
edge IP (крупная CDN сеть, не датацентр), что снижает риски.

Схема работы:
  Telethon (obfuscated MTProto)
    → WebSocket (TLS)
    → Cloudflare Worker (edge IP)
    → TCP
    → Telegram DC

Настройка:
  1. Задеплойте infra/cf_relay_worker.js в Cloudflare Workers
  2. Установите CF_RELAY_URL=https://your-worker.workers.dev в Railway env
  3. Без других настроек — аккаунты без индивидуальных прокси автоматически
     используют CF relay

Когда CF relay НЕ используется:
  - Если аккаунт имеет собственный прокси (proxy_url в device dict)
  - Если CF_RELAY_URL не задан
  В обоих случаях используется ConnectionTcpObfuscated напрямую.

Совместимость:
  - Python 3.12+
  - Telethon 1.28+
  - aiohttp 3.x (уже требуется aiogram)
  - Cloudflare Workers с поддержкой cloudflare:sockets API
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class _WSReader:
    """asyncio.StreamReader-совместимый адаптер поверх WebSocket бинарных фреймов.

    Телеграм-кодек вызывает reader.readexactly(n) для чтения ровно N байт.
    Этот адаптер буферизует входящие WebSocket фреймы и выдаёт данные по запросу.
    Потокобезопасность: только один coroutine читает в любой момент (recv_loop).
    """

    def __init__(self) -> None:
        self._buf: bytearray = bytearray()
        self._event: asyncio.Event = asyncio.Event()
        self._eof: bool = False

    def feed_data(self, data: bytes) -> None:
        """Добавить данные из WebSocket фрейма в буфер."""
        self._buf.extend(data)
        self._event.set()

    def feed_eof(self) -> None:
        """Сигнализировать закрытие WebSocket соединения."""
        self._eof = True
        self._event.set()

    async def readexactly(self, n: int) -> bytes:
        """Прочитать ровно n байт, ожидая если данных недостаточно."""
        while len(self._buf) < n:
            if self._eof:
                raise asyncio.IncompleteReadError(bytes(self._buf), n)
            self._event.clear()
            await self._event.wait()
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data


class _WSWriter:
    """asyncio.StreamWriter-совместимый адаптер поверх WebSocket соединения.

    Telethon вызывает write() синхронно для буферизации, затем await drain()
    для фактической отправки. Этот адаптер накапливает данные и отправляет
    их одним WebSocket бинарным фреймом при drain().
    """

    def __init__(self, ws: object) -> None:
        self._ws = ws
        self._buf: bytearray = bytearray()
        self._closed: asyncio.Event = asyncio.Event()

    def write(self, data: bytes) -> None:
        """Буферизовать данные для отправки (синхронно, как StreamWriter.write)."""
        self._buf.extend(data)

    async def drain(self) -> None:
        """Отправить буферизованные данные через WebSocket."""
        if self._buf:
            data = bytes(self._buf)
            self._buf.clear()
            await self._ws.send_bytes(data)

    def close(self) -> None:
        """Сигнализировать закрытие (вызывается disconnect())."""
        self._closed.set()

    async def wait_closed(self) -> None:
        """Ожидать закрытия (compat с asyncio.StreamWriter)."""
        try:
            await asyncio.wait_for(self._closed.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass


class ConnectionTcpObfuscatedViaWS:
    """Telethon connection class: обфускованный MTProto через WebSocket CF relay.

    Совместима с ConnectionTcpObfuscated по протоколу (obfuscate2 / AES-CTR),
    но вместо прямого TCP подключения к Telegram DC использует WebSocket
    соединение к Cloudflare Worker, который пробрасывает трафик к DC.

    URL формат: {relay_url}/{dc_id}
    Например: https://my-relay.workers.dev/2  →  DC2 (149.154.167.51:443)

    Используйте make_cf_relay_connection(relay_url) для создания класса
    с заданным relay_url, пригодного для передачи в TelegramClient(connection=...).
    """

    # Telethon ожидает что у connection class есть packet_codec и obfuscated_io.
    # Импортируем их из ConnectionTcpObfuscated.
    from telethon.network.connection.tcpobfuscated import (
        ConnectionTcpObfuscated as _Base,
    )
    packet_codec = _Base.packet_codec
    obfuscated_io = _Base.obfuscated_io

    def __init__(
        self,
        ip: str,
        port: int,
        dc_id: int,
        *,
        loggers: object,
        proxy: object = None,
        local_addr: object = None,
        relay_url: str = "",
    ) -> None:
        self._ip = ip
        self._port = port
        self._dc_id = dc_id
        self._proxy = proxy
        self._local_addr = local_addr
        self._relay_url = relay_url.rstrip("/")
        import logging as _log
        self._log = _log.getLogger(__name__)

        self._reader: _WSReader | None = None
        self._writer: _WSWriter | None = None
        self._connected: bool = False
        self._codec = None
        self._obfuscation = None
        self._send_queue: asyncio.Queue = asyncio.Queue(1)
        self._recv_queue: asyncio.Queue = asyncio.Queue(1)
        self._send_task: asyncio.Task | None = None
        self._recv_task: asyncio.Task | None = None

        self._aiohttp_session = None
        self._ws = None
        self._ws_feed_task: asyncio.Task | None = None

    # ── Compat stubs (Connection.connect() calls these internally) ────────────

    def _init_conn(self) -> None:
        """Инициализация обфускации — записывает заголовок в _writer."""
        self._obfuscation = self.obfuscated_io(self)
        self._writer.write(self._obfuscation.header)

    def _send(self, data: bytes) -> None:
        """Синхронная запись (как в ObfuscatedConnection._send).

        Кодирует пакет через codec, шифрует через obfuscation, пишет в _writer.
        _writer — наш _WSWriter, который буферизует до drain().
        """
        self._obfuscation.write(self._codec.encode_packet(data))

    async def _recv(self) -> bytes:
        """Асинхронное чтение пакета (как в ObfuscatedConnection._recv).

        _obfuscation.readexactly() дешифрует данные из _reader (_WSReader).
        """
        return await self._codec.read_packet(self._obfuscation)

    # ── Core connect / disconnect ─────────────────────────────────────────────

    async def _connect(self, timeout: float | None = None, ssl: object = None) -> None:
        """Открыть WebSocket к CF Worker и инициализировать обфускацию."""
        import aiohttp

        ws_url = f"{self._relay_url}/{self._dc_id}"
        self._log.debug("cf_relay._connect dc=%d url=%s", self._dc_id, ws_url)

        self._aiohttp_session = aiohttp.ClientSession()
        try:
            self._ws = await asyncio.wait_for(
                self._aiohttp_session.ws_connect(
                    ws_url,
                    heartbeat=30,
                    receive_timeout=None,
                ),
                timeout=timeout or 30,
            )
        except Exception as exc:
            await self._aiohttp_session.close()
            self._aiohttp_session = None
            raise ConnectionError(f"CF relay WebSocket connect failed ({ws_url}): {exc}") from exc

        self._reader = _WSReader()
        self._writer = _WSWriter(self._ws)

        # Background: pump WebSocket frames → _reader
        self._ws_feed_task = asyncio.ensure_future(self._feed_from_ws())

        # Initialize obfuscation codec (writes 64-byte header to _writer buffer)
        self._codec = self.packet_codec(self)
        self._init_conn()
        # Flush header (actual WebSocket send)
        await self._writer.drain()

    async def _feed_from_ws(self) -> None:
        """Фоновая задача: читает WebSocket фреймы и кормит _reader."""
        import aiohttp
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    self._reader.feed_data(msg.data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    self._log.debug("cf_relay WS closed dc=%d type=%s", self._dc_id, msg.type)
                    break
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log.debug("cf_relay feed error dc=%d: %s", self._dc_id, exc)
        finally:
            self._reader.feed_eof()

    # ── Telethon Connection.connect() / disconnect() interface ────────────────

    async def connect(self, timeout: float | None = None, ssl: object = None) -> None:
        """Публичный connect — вызывается TelegramClient."""
        if self._connected:
            return
        await self._connect(timeout=timeout, ssl=ssl)
        self._connected = True
        self._send_task = asyncio.ensure_future(self._send_loop())
        self._recv_task = asyncio.ensure_future(self._recv_loop())

    async def disconnect(self) -> None:
        """Публичный disconnect — корректное завершение всех соединений."""
        if not self._connected:
            return
        self._connected = False

        # Cancel Telethon loops
        for task in (self._send_task, self._recv_task, self._ws_feed_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        # Close WebSocket
        if self._ws and not self._ws.closed:
            try:
                await asyncio.wait_for(self._ws.close(), timeout=5.0)
            except Exception:
                pass

        # Close aiohttp session
        if self._aiohttp_session and not self._aiohttp_session.closed:
            try:
                await asyncio.wait_for(self._aiohttp_session.close(), timeout=5.0)
            except Exception:
                pass

        # Signal writer closed (for Connection.disconnect() compatibility)
        if self._writer:
            self._writer.close()

    # ── Internal send/recv loops (mirrors Connection._send_loop/_recv_loop) ──

    async def _send_loop(self) -> None:
        try:
            while self._connected:
                self._send(await self._send_queue.get())
                await self._writer.drain()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log.info("cf_relay send_loop ended: %s", exc)
            await self.disconnect()

    async def _recv_loop(self) -> None:
        from telethon.errors import InvalidChecksumError, InvalidBufferError
        try:
            while self._connected:
                try:
                    data = await self._recv()
                except asyncio.CancelledError:
                    break
                except (IOError, asyncio.IncompleteReadError) as exc:
                    self._log.warning("cf_relay recv closed: %s", exc)
                    await self._recv_queue.put((None, exc))
                    await self.disconnect()
                except Exception as exc:
                    self._log.warning("cf_relay recv error: %s", exc)
                    await self._recv_queue.put((None, exc))
                    await self.disconnect()
                else:
                    await self._recv_queue.put((data, None))
        finally:
            await self.disconnect()

    # ── Public send/recv (called by MTProtoSender) ────────────────────────────

    async def send(self, data: bytes) -> None:
        await self._send_queue.put(data)

    async def recv(self) -> bytes:
        data, error = await self._recv_queue.get()
        if error:
            raise error
        return data


def make_cf_relay_connection(relay_url: str) -> type:
    """Создать Telethon-совместимый connection class с заданным CF relay URL.

    Использование:
        conn_cls = make_cf_relay_connection("https://my-relay.workers.dev")
        client = TelegramClient(..., connection=conn_cls)

    Возвращает класс (не экземпляр), который Telethon инстанциирует при connect().
    relay_url встроен через замыкание, чтобы не нарушать сигнатуру __init__.
    """
    _url = relay_url.rstrip("/")

    class _CFRelayConn(ConnectionTcpObfuscatedViaWS):
        def __init__(
            self,
            ip: str,
            port: int,
            dc_id: int,
            *,
            loggers: object,
            proxy: object = None,
            local_addr: object = None,
        ) -> None:
            super().__init__(
                ip, port, dc_id,
                loggers=loggers,
                proxy=proxy,
                local_addr=local_addr,
                relay_url=_url,
            )

    _CFRelayConn.__name__ = "CFRelayConn"
    _CFRelayConn.__qualname__ = "CFRelayConn"
    return _CFRelayConn
