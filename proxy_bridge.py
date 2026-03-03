"""
Локальный HTTP CONNECT → SOCKS5 мост.

Нужен потому что Chromium/Playwright не поддерживает SOCKS5 с авторизацией.
Playwright подключается к http://127.0.0.1:LOCAL_PORT (без авторизации),
мост туннелирует трафик через SOCKS5 с логином/паролем.
"""
import asyncio

import socks  # PySocks
from loguru import logger

LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 8899


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    try:
        writer.close()
    except Exception:
        pass


async def _handle(reader, writer, socks5_host, socks5_port, username, password):
    try:
        line = await reader.readline()
        parts = line.decode(errors="replace").split()

        if len(parts) < 2 or parts[0] != "CONNECT":
            writer.close()
            return

        target_host, target_port_str = parts[1].rsplit(":", 1)
        target_port = int(target_port_str)

        # Пропускаем HTTP-заголовки
        while True:
            hdr = await reader.readline()
            if hdr in (b"\r\n", b"\n", b""):
                break

        # Подключаемся через SOCKS5 в отдельном потоке (blocking API)
        loop = asyncio.get_event_loop()

        def connect_socks5():
            s = socks.socksocket()
            s.set_proxy(socks.SOCKS5, socks5_host, socks5_port, True, username, password)
            s.settimeout(30)
            s.connect((target_host, target_port))
            s.settimeout(None)
            return s

        sock = await loop.run_in_executor(None, connect_socks5)

        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()

        remote_reader, remote_writer = await asyncio.open_connection(sock=sock)
        await asyncio.gather(
            _pipe(reader, remote_writer),
            _pipe(remote_reader, writer),
        )

    except Exception as e:
        logger.debug(f"proxy_bridge: ошибка туннеля: {e}")
        try:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
        except Exception:
            pass
        try:
            writer.close()
        except Exception:
            pass


async def start_bridge(
    socks5_host: str,
    socks5_port: int,
    username: str,
    password: str,
    local_port: int = LOCAL_PORT,
) -> asyncio.Server:
    """
    Запускает локальный HTTP CONNECT прокси на 127.0.0.1:{local_port},
    который туннелирует трафик через SOCKS5 с авторизацией.
    Возвращает asyncio.Server — вызови server.close() при завершении.
    """
    async def handler(r, w):
        await _handle(r, w, socks5_host, socks5_port, username, password)

    server = await asyncio.start_server(handler, LOCAL_HOST, local_port)
    logger.info(
        f"proxy_bridge: HTTP→SOCKS5 мост запущен "
        f"127.0.0.1:{local_port} → {socks5_host}:{socks5_port}"
    )
    return server
