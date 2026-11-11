"""LocalProxy: a plain HTTP proxy on 127.0.0.1 that relays to TunnelBear and adds the credentials, so that
any program can go through TunnelBear without knowing about it."""

import asyncio
import base64
import logging
import ssl
import threading

from xfcrawl.proxy import ProxyDown
from xfcrawl.tunnelbear.rotation import ServerRotation

_LOGGER = logging.getLogger(__name__)
_CONNECT_TIMEOUT = 15
_HEADER_TIMEOUT = 30
_RENEW_EVERY = 300
_DROPPED = (b"proxy-authorization", b"proxy-connection")


class LocalProxy:
    def __init__(self, rotation: ServerRotation, port: int, upstream_ssl: ssl.SSLContext) -> None:
        self._rotation = rotation
        self.port = port
        self._ssl = upstream_ssl

    async def serve(self, ready: threading.Event | None = None) -> None:
        server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self.port = server.sockets[0].getsockname()[1]
        if ready is not None:
            ready.set()
        _LOGGER.info("TunnelBear proxy on http://127.0.0.1:%d (%s)", self.port, self._rotation.describe())
        renewing = asyncio.create_task(self._keep_renewing())
        try:
            async with server:
                await server.serve_forever()
        finally:
            renewing.cancel()

    def _authorization(self) -> bytes:
        token = self._rotation.token
        return b"Basic " + base64.b64encode(f"{token}:{token}".encode())

    async def _keep_renewing(self) -> None:
        while True:
            await asyncio.sleep(_RENEW_EVERY)
            try:
                await asyncio.get_running_loop().run_in_executor(None, self._rotation.renew)
            except ProxyDown as error:  # keep serving with the old token
                _LOGGER.warning("warning: renewing the TunnelBear token failed: %s", error)

    async def _open_upstream(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        last_error: Exception | None = None
        for _ in self._rotation.hosts:
            host = self._rotation.host
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(host, self._rotation.port, ssl=self._ssl, server_hostname=host),
                    _CONNECT_TIMEOUT,
                )
            except (OSError, asyncio.TimeoutError, ssl.SSLError) as error:
                last_error = error
                self._rotation.failed(auth_problem=False)
        raise ConnectionError(f"no TunnelBear server reachable ({last_error})")

    async def _handle(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), _HEADER_TIMEOUT)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
            client_writer.close()
            return
        lines = head[:-4].split(b"\r\n")
        is_connect = lines[0].upper().startswith(b"CONNECT ")
        dropped = _DROPPED if is_connect else (*_DROPPED, b"connection")
        out = [lines[0]] + [h for h in lines[1:] if h.split(b":", 1)[0].strip().lower() not in dropped]
        out.append(b"Proxy-Authorization: " + self._authorization())
        if not is_connect:
            out.append(b"Connection: close")  # one request per upstream connection: credentials on each
        try:
            upstream_reader, upstream_writer = await self._open_upstream()
        except ConnectionError as error:
            client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"
                                + str(error).encode() + b"\n")
            await client_writer.drain()
            client_writer.close()
            return
        upstream_writer.write(b"\r\n".join(out) + b"\r\n\r\n")
        await upstream_writer.drain()
        await asyncio.gather(_pipe(client_reader, upstream_writer), _pipe(upstream_reader, client_writer))


async def _pipe(source: asyncio.StreamReader, destination: asyncio.StreamWriter) -> None:
    try:
        while data := await source.read(65536):
            destination.write(data)
            await destination.drain()
    except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
        pass  # either side hung up; closing below is all there is to do
    finally:
        destination.close()
