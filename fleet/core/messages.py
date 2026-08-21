"""Wire protocol: one envelope shape for every message on the bus.

Deliberately transport-agnostic -- nothing here knows about MQTT.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

BROADCAST = "broadcast"


class MsgType:
    CAPABILITY_QUERY = "CAPABILITY_QUERY"
    CAPABILITY_ANNOUNCE = "CAPABILITY_ANNOUNCE"
    TASK_ASSIGN = "TASK_ASSIGN"
    TASK_ACK = "TASK_ACK"
    TASK_REJECT = "TASK_REJECT"
    TASK_PROGRESS = "TASK_PROGRESS"
    TASK_COMPLETE = "TASK_COMPLETE"
    TELEMETRY = "TELEMETRY"
    ALERT = "ALERT"
    HEARTBEAT = "HEARTBEAT"
    ABORT = "ABORT"


@dataclass
class Envelope:
    type: str
    src: str
    dst: str = BROADCAST
    payload: dict[str, Any] = field(default_factory=dict)
    corr_id: Optional[str] = None
    requires_ack: bool = False
    msg_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: float = field(default_factory=time.time)

    def to_json(self) -> bytes:
        return json.dumps(asdict(self)).encode()

    @staticmethod
    def from_json(raw: bytes | str) -> "Envelope":
        d = json.loads(raw)
        known = Envelope.__dataclass_fields__.keys()
        return Envelope(**{k: v for k, v in d.items() if k in known})
