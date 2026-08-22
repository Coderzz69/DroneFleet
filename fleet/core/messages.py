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

# Every envelope is stamped with these so a receiver can tell a DroneFleet
# message from anything else sharing the broker, and refuse a version it does
# not actually understand.
PROTOCOL = "dronefleet"
PROTOCOL_VERSION = "1.0"


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
    RECALL = "RECALL"          # your part is done -- come home
    # The explicit human-approval boundary. These exist in the vocabulary so a
    # reviewed authorization service can be dropped in; the master never
    # synthesises a grant, and nothing here issues one.
    AUTHORIZATION_REQUEST = "AUTHORIZATION_REQUEST"
    AUTHORIZATION_GRANT = "AUTHORIZATION_GRANT"
    AUTHORIZATION_DENY = "AUTHORIZATION_DENY"


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
    protocol: str = PROTOCOL
    protocol_version: str = PROTOCOL_VERSION
    mission_id: Optional[str] = None      # groups one task graph
    expires_at: Optional[float] = None    # after this, the order is void

    def understood(self) -> bool:
        """Is this ours, and a version we speak?

        Tolerant of a missing stamp so an older sender still works, strict
        about a stamp that disagrees: acting on half a contract is how a
        vehicle ends up doing something nobody ordered.
        """
        if self.protocol and self.protocol != PROTOCOL:
            return False
        if not self.protocol_version:
            return True
        return self.protocol_version.split(".")[0] == PROTOCOL_VERSION.split(".")[0]

    def expired(self, now: Optional[float] = None) -> bool:
        if self.expires_at is None:
            return False
        return (now if now is not None else time.time()) > self.expires_at

    def to_json(self) -> bytes:
        return json.dumps(asdict(self)).encode()

    @staticmethod
    def from_json(raw: bytes | str) -> "Envelope":
        d = json.loads(raw)
        known = Envelope.__dataclass_fields__.keys()
        return Envelope(**{k: v for k, v in d.items() if k in known})
