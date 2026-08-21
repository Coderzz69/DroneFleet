"""Fleet registry: what drones exist and what they can actually do.

The master knows nothing in advance -- drones announce themselves and this
registry is built from those announcements. Adding a new kind of drone costs
zero code changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class Capability:
    verb: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Constraints:
    endurance_min: float = 30.0
    cruise_ms: float = 22.0          # cruise speed, m/s
    max_speed_ms: float = 30.0
    accel_ms2: float = 3.0
    turn_rate_deg_s: float = 60.0
    comms_range_m: float = 6000.0
    altitude_m: float = 250.0        # survey altitude
    sensor_fov_deg: float = 90.0     # full cone angle -> ground swath
    payload_kg: float = 0.0
    mass_kg: float = 4.0


@dataclass
class DroneRecord:
    id: str
    name: str
    capabilities: list[Capability] = field(default_factory=list)
    sensors: list[str] = field(default_factory=list)
    constraints: Constraints = field(default_factory=Constraints)
    unmapped_text: list[str] = field(default_factory=list)
    source_text: str = ""
    # Rich vendor/device information is carried opaquely so the planner stays
    # domain-neutral while the mothership can still display and audit it.
    metadata: dict[str, Any] = field(default_factory=dict)
    online: bool = False

    def verbs(self) -> set[str]:
        return {c.verb for c in self.capabilities}

    def has(self, verb: str) -> bool:
        return verb in self.verbs()

    def swath_m(self) -> float:
        """Ground sensor swath width from altitude and field of view."""
        import math
        half = math.radians(self.constraints.sensor_fov_deg / 2.0)
        return 2.0 * self.constraints.altitude_m * math.tan(half)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["swath_m"] = round(self.swath_m(), 1)
        return d

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> "DroneRecord":
        """Coerce a protocol manifest or legacy capability announcement."""
        from ..protocol import manifest_from_dict

        if "protocol" in data or "vehicle_type" in data:
            return manifest_from_dict(data).to_record()

        raw_constraints = data.get("constraints") or {}
        constraints = Constraints(**{
            k: float(v) for k, v in raw_constraints.items()
            if k in Constraints.__dataclass_fields__ and isinstance(v, (int, float))
        })
        caps = []
        for item in data.get("capabilities") or []:
            if isinstance(item, str):
                caps.append(Capability(item))
            elif isinstance(item, dict) and item.get("verb"):
                caps.append(Capability(str(item["verb"]), dict(item.get("params") or {})))
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", data.get("id", "unknown"))),
            capabilities=caps,
            sensors=[str(s) for s in data.get("sensors") or []],
            constraints=constraints,
            unmapped_text=[str(x) for x in data.get("unmapped_text") or []],
            source_text=str(data.get("source_text", "protocol announcement")),
            metadata=dict(data),
            online=True,
        )


class Registry:
    def __init__(self) -> None:
        self._drones: dict[str, DroneRecord] = {}
        self._counter = 0

    def next_id(self) -> str:
        self._counter += 1
        return f"drone-{self._counter}"

    def add(self, rec: DroneRecord) -> DroneRecord:
        self._drones[rec.id] = rec
        return rec

    def remove(self, drone_id: str) -> bool:
        return self._drones.pop(drone_id, None) is not None

    def get(self, drone_id: str) -> Optional[DroneRecord]:
        return self._drones.get(drone_id)

    def all(self) -> list[DroneRecord]:
        return list(self._drones.values())

    def providers_of(self, verb: str) -> list[DroneRecord]:
        return [d for d in self._drones.values() if d.has(verb)]

    def __len__(self) -> int:
        return len(self._drones)
