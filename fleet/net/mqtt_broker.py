"""A minimal but genuine MQTT 3.1.1 broker (QoS 0), pure asyncio.

Exists so the demo runs with zero installs. It speaks real MQTT on the wire --
you can point `mosquitto_sub -t 'fleet/#' -v` at it and watch the fleet talk.
To use a production broker instead, set FLEET_MQTT_HOST/PORT and skip this.

Supported: CONNECT/CONNACK, PUBLISH (QoS 0), SUBSCRIBE/SUBACK,
UNSUBSCRIBE/UNSUBACK, PINGREQ/PINGRESP, DISCONNECT, retained messages,
'+' and '#' wildcards.
Not supported: QoS 1/2, sessions, wills, auth. None are needed here.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

log = logging.getLogger("mqtt.broker")

CONNECT, CONNACK, PUBLISH, SUBSCRIBE, SUBACK = 1, 2, 3, 8, 9
UNSUBSCRIBE, UNSUBACK, PINGREQ, PINGRESP, DISCONNECT = 10, 11, 12, 13, 14


def encode_len(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            return bytes(out)


async def read_len(reader: asyncio.StreamReader) -> int:
    mult, value = 1, 0
    while True:
        b = (await reader.readexactly(1))[0]
        value += (b & 0x7F) * mult
        if not (b & 0x80):
            return value
        mult *= 128
        if mult > 128 ** 3:
            raise ValueError("malformed remaining length")


def enc_str(s: str) -> bytes:
    raw = s.encode()
    return len(raw).to_bytes(2, "big") + raw


def dec_str(buf: bytes, i: int) -> tuple[str, int]:
    n = int.from_bytes(buf[i:i + 2], "big")
    return buf[i + 2:i + 2 + n].decode(errors="replace"), i + 2 + n


def topic_matches(filt: str, topic: str) -> bool:
    f, t = filt.split("/"), topic.split("/")
    for i, part in enumerate(f):
        if part == "#":
            return True
        if i >= len(t):
            return False
        if part != "+" and part != t[i]:
            return False
    return len(f) == len(t)


class _Client:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.client_id = ""
        self.subs: set[str] = set()
        self.lock = asyncio.Lock()

    async def send(self, data: bytes) -> None:
        async with self.lock:
            self.writer.write(data)
            await self.writer.drain()


class MQTTBroker:
    def __init__(self, host: str = "127.0.0.1", port: int = 1883) -> None:
        self.host, self.port = host, port
        self.clients: set[_Client] = set()
        self.retained: dict[str, bytes] = {}
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        log.info("MQTT broker listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # -- publishing ---------------------------------------------------------
    @staticmethod
    def _publish_packet(topic: str, payload: bytes, retain: bool = False) -> bytes:
        body = enc_str(topic) + payload
        return bytes([(PUBLISH << 4) | (0x01 if retain else 0)]) + encode_len(len(body)) + body

    async def dispatch(self, topic: str, payload: bytes, retain: bool = False) -> None:
        if retain:
            if payload:
                self.retained[topic] = payload
            else:
                self.retained.pop(topic, None)
        pkt = self._publish_packet(topic, payload)
        for c in list(self.clients):
            if any(topic_matches(f, topic) for f in c.subs):
                try:
                    await c.send(pkt)
                except Exception:
                    self.clients.discard(c)

    # -- connection handling ------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = _Client(writer)
        self.clients.add(client)
        try:
            while True:
                header = await reader.readexactly(1)
                ptype = header[0] >> 4
                length = await read_len(reader)
                body = await reader.readexactly(length) if length else b""

                if ptype == CONNECT:
                    i = 0
                    _proto, i = dec_str(body, i)
                    i += 1                      # protocol level
                    flags = body[i]; i += 1
                    i += 2                      # keepalive
                    client.client_id, i = dec_str(body, i)
                    if flags & 0x04:            # will topic/message present
                        _wt, i = dec_str(body, i)
                        _wm, i = dec_str(body, i)
                    await client.send(bytes([CONNACK << 4, 2, 0, 0]))
                    log.debug("connected: %s", client.client_id)

                elif ptype == SUBSCRIBE:
                    pid = int.from_bytes(body[0:2], "big")
                    i, codes = 2, []
                    while i < len(body):
                        filt, i = dec_str(body, i)
                        i += 1                  # requested QoS
                        client.subs.add(filt)
                        codes.append(0)
                    ack = pid.to_bytes(2, "big") + bytes(codes)
                    await client.send(bytes([SUBACK << 4]) + encode_len(len(ack)) + ack)
                    for topic, payload in self.retained.items():
                        if any(topic_matches(f, topic) for f in client.subs):
                            await client.send(self._publish_packet(topic, payload, retain=True))

                elif ptype == UNSUBSCRIBE:
                    pid = int.from_bytes(body[0:2], "big")
                    i = 2
                    while i < len(body):
                        filt, i = dec_str(body, i)
                        client.subs.discard(filt)
                    await client.send(bytes([UNSUBACK << 4, 2]) + pid.to_bytes(2, "big"))

                elif ptype == PUBLISH:
                    qos = (header[0] >> 1) & 0x03
                    retain = bool(header[0] & 0x01)
                    topic, i = dec_str(body, 0)
                    if qos:
                        i += 2                  # packet id (QoS 0 only, but be tolerant)
                    await self.dispatch(topic, body[i:], retain=retain)

                elif ptype == PINGREQ:
                    await client.send(bytes([PINGRESP << 4, 0]))

                elif ptype == DISCONNECT:
                    break

        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as exc:                # noqa: BLE001
            log.debug("client error (%s): %s", client.client_id, exc)
        finally:
            self.clients.discard(client)
            try:
                writer.close()
            except Exception:
                pass
