"""Static file server + WebSocket, pure asyncio. No framework needed.

The browser is a strict OBSERVER: it receives events and sends command strings.
It holds no mission state. That discipline is what would let the same frontend
be pointed at real hardware later.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import struct
from typing import Awaitable, Callable, Optional

log = logging.getLogger("web")
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# --------------------------------------------------------------------------
# websocket framing
# --------------------------------------------------------------------------
def encode_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < (1 << 16):
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    return bytes(header) + payload


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    b1, b2 = await reader.readexactly(2)
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else b""
    data = await reader.readexactly(length) if length else b""
    if masked:
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
    return opcode, data


class WSConnection:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.open = True
        self._lock = asyncio.Lock()

    async def send_json(self, obj) -> None:
        if not self.open:
            return
        try:
            async with self._lock:
                self.writer.write(encode_frame(json.dumps(obj).encode()))
                await self.writer.drain()
        except Exception:
            self.open = False


class WebServer:
    def __init__(self, static_dir: str, host: str = "127.0.0.1", port: int = 8080) -> None:
        self.static_dir = os.path.abspath(static_dir)
        self.host, self.port = host, port
        self.connections: set[WSConnection] = set()
        self.on_command: Optional[Callable[[str, dict], Awaitable[None]]] = None
        self.on_connect: Optional[Callable[[WSConnection], Awaitable[None]]] = None
        self._server = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        log.info("web on http://%s:%s", self.host, self.port)

    async def broadcast(self, obj) -> None:
        dead = []
        for c in list(self.connections):
            await c.send_json(obj)
            if not c.open:
                dead.append(c)
        for c in dead:
            self.connections.discard(c)

    # -- request handling ---------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=15)
            if not line:
                writer.close()
                return
            parts = line.decode(errors="replace").split()
            if len(parts) < 2:
                writer.close()
                return
            method, path = parts[0], parts[1]

            headers = {}
            while True:
                h = await reader.readline()
                if h in (b"\r\n", b"\n", b""):
                    break
                k, _, v = h.decode(errors="replace").partition(":")
                headers[k.strip().lower()] = v.strip()

            if headers.get("upgrade", "").lower() == "websocket":
                await self._websocket(reader, writer, headers)
            else:
                await self._static(writer, method, path)
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.TimeoutError):
            pass
        except Exception:
            log.exception("request failed")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _static(self, writer: asyncio.StreamWriter, method: str, path: str) -> None:
        rel = path.split("?")[0].lstrip("/") or "index.html"
        full = os.path.abspath(os.path.join(self.static_dir, rel))
        if not full.startswith(self.static_dir) or not os.path.isfile(full):
            body = b"404 not found"
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: %d\r\n"
                         b"Connection: close\r\n\r\n%s" % (len(body), body))
            await writer.drain()
            return
        with open(full, "rb") as fh:
            body = fh.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        writer.write(
            f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
            f"Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()

    async def _websocket(self, reader, writer, headers: dict) -> None:
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      "Connection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        await writer.drain()

        conn = WSConnection(writer)
        self.connections.add(conn)
        if self.on_connect:
            await self.on_connect(conn)

        try:
            while conn.open:
                opcode, data = await read_frame(reader)
                if opcode == 0x8:                       # close
                    break
                if opcode == 0x9:                       # ping -> pong
                    writer.write(encode_frame(data, 0xA))
                    await writer.drain()
                    continue
                if opcode != 0x1:
                    continue
                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if self.on_command:
                    await self.on_command(msg.get("cmd", ""), msg)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            conn.open = False
            self.connections.discard(conn)
