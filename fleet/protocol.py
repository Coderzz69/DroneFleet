"""Versioned DroneFleet wire contract.

This module is the boundary between a mothership and an independently
implemented vehicle.  A real vehicle adapter only needs to produce a
``DroneManifest`` and exchange ``Envelope`` messages; it does not need to
know about the simulator or the web UI.

The contract intentionally describes observation, transport, rescue and
inspection capabilities.  Any capability that can apply force or release a
weapon is outside the autonomous protocol and must be represented by an
explicit, separately authorized human-controlled workflow.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .core.registry import Capability, Constraints, DroneRecord

PROTOCOL_NAME = "dronefleet"
PROTOCOL_VERSION = "1.0"
SCHEMA_VERSION = 1


@dataclass
class CapabilityContract:
    name: str
    version: str = "1.0"
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    supports_autonomous_execution: bool = True
    requires_human_authorization: bool = False


@dataclass
class SensorContract:
    sensor_id: str
    kind: str
    modality: str = ""
    resolution: str = ""
    max_range_m: Optional[float] = None
    fov_deg: Optional[float] = None
    accuracy: Optional[str] = None
    status: str = "OPERATIONAL"


@dataclass
class PayloadContract:
    payload_id: str
    kind: str
    quantity: float = 0.0
    unit: str = ""
    capacity_kg: float = 0.0
    status: str = "READY"


@dataclass
class DroneManifest:
    """Identity, capability, physical, safety and current-state snapshot."""

    drone_id: str
    callsign: str
    vehicle_type: str
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
        """Project the rich wire manifest into the planner's stable core model."""
        metadata = self.to_dict()
        return DroneRecord(
            id=self.drone_id,
            name=self.callsign,
            capabilities=[Capability(c.name, dict(c.parameters)) for c in self.capabilities],
            sensors=[s.kind if not s.modality else s.modality for s in self.sensors],
            constraints=self.constraints,
            source_text=f"protocol manifest from {self.manufacturer} {self.model}",
            metadata=metadata,
            online=True,
        )


def manifest_from_dict(data: dict[str, Any]) -> DroneManifest:
    """Validate and coerce a JSON manifest received from another vendor."""
    if not isinstance(data, dict):
        raise ValueError("manifest must be an object")
    if data.get("protocol") not in (None, PROTOCOL_NAME):
        raise ValueError(f"unsupported protocol {data.get('protocol')!r}")
    if not data.get("drone_id") or not data.get("callsign"):
        raise ValueError("manifest requires drone_id and callsign")

    raw_constraints = data.get("constraints") or {}
    constraints = Constraints(**{
        k: float(v) for k, v in raw_constraints.items()
        if k in Constraints.__dataclass_fields__ and isinstance(v, (int, float))
    })
    capabilities = []
    for item in data.get("capabilities") or []:
        if isinstance(item, str):
            capabilities.append(CapabilityContract(name=item))
        elif isinstance(item, dict) and item.get("name"):
            capabilities.append(CapabilityContract(
                name=str(item["name"]),
                version=str(item.get("version", "1.0")),
                description=str(item.get("description", "")),
                parameters=dict(item.get("parameters") or {}),
                supports_autonomous_execution=bool(
                    item.get("supports_autonomous_execution", True)),
                requires_human_authorization=bool(
                    item.get("requires_human_authorization", False)),
            ))
    sensors = [SensorContract(**{
        k: item[k] for k in SensorContract.__dataclass_fields__ if k in item
    }) for item in data.get("sensors") or [] if isinstance(item, dict) and item.get("sensor_id")]
    payloads = [PayloadContract(**{
        k: item[k] for k in PayloadContract.__dataclass_fields__ if k in item
    }) for item in data.get("payloads") or [] if isinstance(item, dict) and item.get("payload_id")]

    return DroneManifest(
        drone_id=str(data["drone_id"]),
        callsign=str(data["callsign"]),
        vehicle_type=str(data.get("vehicle_type", "UAS")),
        manufacturer=str(data.get("manufacturer", "unknown")),
        model=str(data.get("model", "unknown")),
        serial_number=str(data.get("serial_number", "")),
        firmware_version=str(data.get("firmware_version", "unknown")),
        protocol_version=str(data.get("protocol_version", PROTOCOL_VERSION)),
        capabilities=capabilities,
        sensors=sensors,
        payloads=payloads,
        constraints=constraints,
        state=dict(data.get("state") or {}),
        navigation=dict(data.get("navigation") or {}),
        communications=dict(data.get("communications") or {}),
        safety=dict(data.get("safety") or {}),
        security=dict(data.get("security") or {}),
    )
