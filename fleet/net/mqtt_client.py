"""Minimal asyncio MQTT 3.1.1 client (QoS 0). Works against this project's
broker or any standard one (mosquitto, EMQX, HiveMQ) with no changes."""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, Optional

from .mqtt_broker import (PINGREQ, PUBLISH, SUBSCRIBE, dec_str, enc_str,
                          encode_len, read_len, topic_matches)

log = logging.getLogger("mqtt.client")

Handler = Callable[[str, bytes], Awaitable[None]]


class MQTTClient:
    def __init__(self, client_id: str, host: str = "127.0.0.1", port: int = 1883) -> None:
        self.client_id = client_id
        self.host, self.port = host, port
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self._handlers: list[tuple[str, Handler]] = []
        self._pid = 1
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self.connected = False
        # simulated link impairment, driven by the fault-injection panel
        self.drop_rate = 0.0
        self.extra_latency_s = 0.0

    async def connect(self, retries: int = 40) -> None:
        for attempt in range(retries):
            try:
                self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
                break
            except OSError:
                await asyncio.sleep(0.05 * (attempt + 1))
        else:
            raise ConnectionError(f"could not reach MQTT broker at {self.host}:{self.port}")

        payload = enc_str("MQTT") + bytes([4, 0x02]) + (60).to_bytes(2, "big") + enc_str(self.client_id)
        await self._raw(bytes([0x10]) + encode_len(len(payload)) + payload)
        await self.reader.readexactly(4)  # CONNACK
        self.connected = True
        self._tasks.append(asyncio.create_task(self._read_loop()))
        self._tasks.append(asyncio.create_task(self._ping_loop()))

    async def _raw(self, data: bytes) -> None:
        if not self.writer:
            return
        async with self._lock:
            self.writer.write(data)
            await self.writer.drain()

    async def subscribe(self, topic_filter: str, handler: Handler) -> None:
        self._handlers.append((topic_filter, handler))
        body = self._next_pid().to_bytes(2, "big") + enc_str(topic_filter) + bytes([0])
        await self._raw(bytes([(SUBSCRIBE << 4) | 0x02]) + encode_len(len(body)) + body)

    async def publish(self, topic: str, payload: bytes, retain: bool = False) -> None:
        # impairment is applied here, on the sender, exactly like a lossy radio
        if self.drop_rate and random.random() < self.drop_rate:
            return
        if self.extra_latency_s:
            await asyncio.sleep(self.extra_latency_s)
        body = enc_str(topic) + payload
        await self._raw(bytes([(PUBLISH << 4) | (0x01 if retain else 0)]) + encode_len(len(body)) + body)

    def _next_pid(self) -> int:
        self._pid = (self._pid % 65535) + 1
        return self._pid

    async def _ping_loop(self) -> None:
        try:
            while self.connected:
                await asyncio.sleep(20)
                await self._raw(bytes([PINGREQ << 4, 0]))
        except asyncio.CancelledError:
            pass

    async def _read_loop(self) -> None:
        try:
            while self.reader:
                header = await self.reader.readexactly(1)
                length = await read_len(self.reader)
                body = await self.reader.readexactly(length) if length else b""
                if header[0] >> 4 != PUBLISH:
                    continue
                topic, i = dec_str(body, 0)
                if (header[0] >> 1) & 0x03:
                    i += 2
                payload = body[i:]
                for filt, handler in list(self._handlers):
                    if topic_matches(filt, topic):
                        asyncio.create_task(self._safe(handler, topic, payload))
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.connected = False

    @staticmethod
    async def _safe(handler: Handler, topic: str, payload: bytes) -> None:
        try:
            await handler(topic, payload)
        except Exception:
            log.exception("handler failed for %s", topic)

    async def disconnect(self) -> None:
        self.connected = False
        for t in self._tasks:
            t.cancel()
        if self.writer:
            try:
                self.writer.write(bytes([0xE0, 0]))
                self.writer.close()
            except Exception:
                pass
