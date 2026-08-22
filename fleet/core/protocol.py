"""Versioned DroneFleet wire contract.

This module is the boundary between a mothership and an independently built
vehicle. A vendor adapter only has to produce a `DroneManifest` and exchange
`Envelope` messages; it never imports the simulator, the broker or the UI.

It lives in `core/` for the same reason everything else here does: deciding
what a vehicle is allowed to be asked to do is engine work, and must stay
testable with no network and no browser.

The contract deliberately covers observation, transport, rescue, relay and
inspection work. A capability a vendor marks as requiring human authorization
is carried through honestly but is NOT offered to the autonomous planner --
see `DroneManifest.to_record`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .nlp import clamp_constraints
from .registry import Capability, Constraints, DroneRecord

PROTOCOL_NAME = "dronefleet"
PROTOCOL_VERSION = "1.0"
SCHEMA_VERSION = 1

# A vehicle speaking 1.x is understood. A 2.x vehicle is not, and guessing is
# worse than refusing: a manifest we half-understand becomes a mission we
# think is possible.
SUPPORTED_MAJOR = PROTOCOL_VERSION.split(".")[0]

_OPERATIONAL = "OPERATIONAL"


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    """Coerce a wire number. `bool` is deliberately excluded -- JSON `true`
    arriving in a numeric field is a malformed manifest, not the value 1."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any, default: str = "") -> str:
    return str(value) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else default


@dataclass
class CapabilityContract:
    name: str
    version: str = "1.0"
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    supports_autonomous_execution: bool = True
    requires_human_authorization: bool = False

    def autonomous(self) -> bool:
        return self.supports_autonomous_execution and not self.requires_human_authorization


@dataclass
class SensorContract:
    sensor_id: str
    kind: str = "unknown"
    modality: str = ""
    resolution: str = ""
    max_range_m: Optional[float] = None
    fov_deg: Optional[float] = None
    accuracy: Optional[str] = None
    status: str = _OPERATIONAL

    def usable(self) -> bool:
        return self.status.upper() == _OPERATIONAL

    def names(self) -> list[str]:
        """Sensor names the pack can match against.

        `kind` comes first on purpose. A pack declares `sensors_any: [thermal,
        eo_ir]`; a vendor reporting modality "LWIR" for a thermal camera would
        silently fail to match if modality won the tie, and the mission would
        come back INSUFFICIENT for no visible reason.
        """
        out = [self.kind.strip().lower()]
        mod = self.modality.strip().lower()
        if mod and mod not in out:
            out.append(mod)
        return [n for n in out if n and n != "unknown"]


@dataclass
class PayloadContract:
    payload_id: str
    kind: str = "unknown"
    quantity: float = 0.0
    unit: str = ""
    capacity_kg: float = 0.0
    status: str = "READY"


@dataclass
class DroneManifest:
    """Identity, capability, physical, safety and current-state snapshot."""

    drone_id: str
    callsign: str
    vehicle_type: str = "UAS"
    manufacturer: str = "unknown"
    model: str = "unknown"
    serial_number: str = ""
    firmware_version: str = "unknown"
    protocol_version: str = PROTOCOL_VERSION
    capabilities: list[CapabilityContract] = field(default_factory=list)
    sensors: list[SensorContract] = field(default_factory=list)
    payloads: list[PayloadContract] = field(default_factory=list)
    constraints: Constraints = field(default_factory=Constraints)
    state: dict[str, Any] = field(default_factory=lambda: {
        "availability": "AVAILABLE",
        "mode": "LANDED",
        "battery_pct": 100.0,
        "health": "NOMINAL",
        "nav_fix": "UNKNOWN",
    })
    navigation: dict[str, Any] = field(default_factory=dict)
    communications: dict[str, Any] = field(default_factory=lambda: {
        "transport": "mqtt",
        "qos": 0,
        "supports_reconnect": True,
    })
    safety: dict[str, Any] = field(default_factory=lambda: {
        "return_to_home": True,
        "min_battery_reserve_pct": 15.0,
        "geofence": None,
        "human_authorization_required_for": [],
        "failsafe_actions": ["HOLD", "RETURN_TO_HOME", "LAND"],
    })
    security: dict[str, Any] = field(default_factory=lambda: {
        "authentication": "none-in-demo",
        "identity_key": None,
    })

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["protocol"] = PROTOCOL_NAME
        data["schema_version"] = SCHEMA_VERSION
        return data

    def to_record(self) -> DroneRecord:
        """Project the rich wire manifest onto the planner's stable core model.

        Only what the vehicle says it can do *by itself* becomes a planner
        capability. Anything it flags for human authorization, anything it
        cannot execute autonomously, and any sensor it reports as unhealthy is
        withheld and explained. The bias matches the rest of the engine: an
        absent capability produces a visible gap, a phantom one makes an
        impossible mission look possible.
        """
        withheld: list[str] = []
        capabilities: list[Capability] = []
        for cap in self.capabilities:
            if cap.autonomous():
                capabilities.append(Capability(cap.name, dict(cap.parameters)))
            elif cap.requires_human_authorization:
                withheld.append(f"'{cap.name}' is declared but requires human "
                                f"authorization — not offered for autonomous tasking")
            else:
                withheld.append(f"'{cap.name}' is declared but the vehicle cannot "
                                f"execute it autonomously")

        sensors: list[str] = []
        for sensor in self.sensors:
            if not sensor.usable():
                withheld.append(f"sensor '{sensor.sensor_id}' reports "
                                f"{sensor.status} — not counted")
                continue
            for name in sensor.names():
                if name not in sensors:
                    sensors.append(name)

        constraints = Constraints(**asdict(self.constraints))
        withheld.extend(clamp_constraints(constraints))

        return DroneRecord(
            id=self.drone_id,
            name=self.callsign or self.drone_id,
            capabilities=capabilities,
            sensors=sensors,
            constraints=constraints,
            source_text=f"protocol manifest from {self.manufacturer} {self.model}",
            withheld=withheld,
            metadata=self.to_dict(),
            remote=True,
            online=True,
        )


def _constraints_from(raw: Any) -> Constraints:
    """Take only the fields we model, only when they are really numbers."""
    if not isinstance(raw, dict):
        return Constraints()
    fields = Constraints.__dataclass_fields__
    values: dict[str, float] = {}
    for key, value in raw.items():
        if key not in fields:
            continue
        num = _num(value)
        if num is not None:
            values[key] = num
    return Constraints(**values)


def _capabilities_from(raw: Any) -> list[CapabilityContract]:
    out: list[CapabilityContract] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str) and item:
            out.append(CapabilityContract(name=item))
            continue
        if not isinstance(item, dict):
            continue
        # `verb` is what the in-process announcement calls it; accept both so a
        # single reader handles legacy and protocol manifests.
        name = _text(item.get("name") or item.get("verb"))
        if not name:
            continue
        params = item.get("parameters")
        if not isinstance(params, dict):
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
        out.append(CapabilityContract(
            name=name,
            version=_text(item.get("version"), "1.0"),
            description=_text(item.get("description")),
            parameters=dict(params),
            supports_autonomous_execution=bool(
                item.get("supports_autonomous_execution", True)),
            requires_human_authorization=bool(
                item.get("requires_human_authorization", False)),
        ))
    return out


def _sensors_from(raw: Any) -> list[SensorContract]:
    out: list[SensorContract] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str) and item:
            # the legacy announcement carries bare sensor names
            out.append(SensorContract(sensor_id=item, kind=item))
            continue
        if not isinstance(item, dict):
            continue
        sensor_id = _text(item.get("sensor_id"))
        kind = _text(item.get("kind"))
        if not sensor_id and not kind:
            continue
        out.append(SensorContract(
            sensor_id=sensor_id or kind,
            kind=kind or sensor_id,
            modality=_text(item.get("modality")),
            resolution=_text(item.get("resolution")),
            max_range_m=_num(item.get("max_range_m")),
            fov_deg=_num(item.get("fov_deg")),
            accuracy=_text(item.get("accuracy")) or None,
            status=_text(item.get("status"), _OPERATIONAL) or _OPERATIONAL,
        ))
    return out


def _payloads_from(raw: Any) -> list[PayloadContract]:
    out: list[PayloadContract] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        payload_id = _text(item.get("payload_id"))
        if not payload_id:
            continue
        out.append(PayloadContract(
            payload_id=payload_id,
            kind=_text(item.get("kind"), "unknown") or "unknown",
            quantity=_num(item.get("quantity"), 0.0) or 0.0,
            unit=_text(item.get("unit")),
            capacity_kg=_num(item.get("capacity_kg"), 0.0) or 0.0,
            status=_text(item.get("status"), "READY") or "READY",
        ))
    return out


def manifest_from_dict(data: Any) -> DroneManifest:
    """Validate and coerce a JSON manifest received from another vendor.

    Everything past the identity and version checks is coerced rather than
    trusted: a field with the wrong type falls back to its default instead of
    raising, because one bad optional field should not cost us the aircraft.
    """
    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")

    protocol = data.get("protocol")
    if protocol not in (None, PROTOCOL_NAME):
        raise ValueError(f"unsupported protocol {protocol!r}")

    version = _text(data.get("protocol_version"), PROTOCOL_VERSION) or PROTOCOL_VERSION
    if version.split(".")[0] != SUPPORTED_MAJOR:
        raise ValueError(f"unsupported protocol version {version!r} "
                         f"(this mothership speaks {PROTOCOL_VERSION})")

    drone_id = _text(data.get("drone_id") or data.get("id")).strip()
    callsign = _text(data.get("callsign") or data.get("name")).strip()
    if not drone_id:
        raise ValueError("manifest requires a drone_id")
    if not callsign:
        raise ValueError("manifest requires a callsign")

    return DroneManifest(
        drone_id=drone_id,
        callsign=callsign,
        vehicle_type=_text(data.get("vehicle_type"), "UAS") or "UAS",
        manufacturer=_text(data.get("manufacturer"), "unknown") or "unknown",
        model=_text(data.get("model"), "unknown") or "unknown",
        serial_number=_text(data.get("serial_number")),
        firmware_version=_text(data.get("firmware_version"), "unknown") or "unknown",
        protocol_version=version,
        capabilities=_capabilities_from(data.get("capabilities")),
        sensors=_sensors_from(data.get("sensors")),
        payloads=_payloads_from(data.get("payloads")),
        constraints=_constraints_from(data.get("constraints")),
        state=dict(data["state"]) if isinstance(data.get("state"), dict) else {},
        navigation=dict(data["navigation"]) if isinstance(data.get("navigation"), dict) else {},
        communications=dict(data["communications"]) if isinstance(data.get("communications"), dict) else {},
        safety=dict(data["safety"]) if isinstance(data.get("safety"), dict) else {},
        security=dict(data["security"]) if isinstance(data.get("security"), dict) else {},
    )


def is_manifest(data: Any) -> bool:
    """Tell a protocol manifest from the in-process announcement."""
    return isinstance(data, dict) and bool(
        data.get("protocol") or data.get("vehicle_type") or data.get("drone_id"))


def record_from_announcement(data: Any) -> DroneRecord:
    """Turn any CAPABILITY_ANNOUNCE payload into a registry record.

    Handles both shapes the bus carries: a full protocol manifest from an
    independently launched vehicle, and the compact `DroneRecord.to_dict()`
    that the in-process simulator drones announce. Raises `ValueError` on a
    payload that cannot be understood -- the master reports that rather than
    registering a half-built vehicle.
    """
    if not isinstance(data, dict):
        raise ValueError("announcement must be a JSON object")
    if is_manifest(data):
        return manifest_from_dict(data).to_record()

    drone_id = _text(data.get("id")).strip()
    if not drone_id:
        raise ValueError("announcement requires an id")

    constraints = _constraints_from(data.get("constraints"))
    withheld = list(clamp_constraints(constraints))
    capabilities = [Capability(c.name, dict(c.parameters))
                    for c in _capabilities_from(data.get("capabilities"))]
    sensors: list[str] = []
    for item in data.get("sensors") or []:
        name = _text(item).strip().lower()
        if name and name not in sensors:
            sensors.append(name)

    return DroneRecord(
        id=drone_id,
        name=_text(data.get("name"), drone_id) or drone_id,
        capabilities=capabilities,
        sensors=sensors,
        constraints=constraints,
        unmapped_text=[_text(x) for x in data.get("unmapped_text") or []],
        source_text=_text(data.get("source_text")),
        withheld=withheld,
        metadata={},
        remote=False,
        online=True,
    )
