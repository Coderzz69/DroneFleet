# DroneFleet Interoperability Protocol v1

This project treats the mothership and each vehicle as independent network
participants. A vendor-specific vehicle adapter only needs to implement the
protocol; the mothership does not instantiate or inspect its code.

The reference implementation uses MQTT 3.1.1 and JSON. The demo broker is
intentionally unauthenticated and QoS 0. A production deployment must put the
same message contract behind a broker configured for TLS/mTLS, ACLs, durable
sessions, appropriate QoS, and signed device identity.

## Vehicle lifecycle

```text
OFFLINE
  -> CONNECTING
  -> ONLINE (CAPABILITY_ANNOUNCE + HEARTBEAT)
  -> AVAILABLE
  -> TASKED (TASK_ASSIGN / TASK_ACK)
  -> EXECUTING (TASK_PROGRESS + TELEMETRY)
  -> AVAILABLE (TASK_COMPLETE)
  -> RETURNING (RECALL)
  -> OFFLINE
```

The mothership considers a vehicle lost when its heartbeat deadline expires.
It must never infer a vehicle’s location or health from its last known command;
those values come only from received telemetry and heartbeats.

## Topics

All v1 topics are under `fleet/v1/`:

| Topic | Direction | Purpose |
|---|---|---|
| `fleet/v1/broadcast` | mothership → fleet | discovery, abort, global notices |
| `fleet/v1/master/inbox` | vehicle → mothership | registration, ACKs, events |
| `fleet/v1/drone/{id}/inbox` | mothership → vehicle | commands for one vehicle |
| `fleet/v1/drone/{id}/telemetry` | vehicle → mothership | position and live state |

The envelope is the same for every message:

```json
{
  "protocol": "dronefleet",
  "protocol_version": "1.0",
  "type": "TASK_ASSIGN",
  "msg_id": "unique-message-id",
  "ts": 1730000000.0,
  "src": "mother-01",
  "dst": "scout-07",
  "mission_id": "mission-42",
  "corr_id": "task-2",
  "requires_ack": true,
  "expires_at": null,
  "payload": {}
}
```

`msg_id` provides deduplication, `corr_id` links ACK/progress/result messages,
and `mission_id` groups a task graph. Implementations should reject expired
messages and retain processed message IDs for the configured replay window.

## Initial manifest

The first message after connecting is `CAPABILITY_ANNOUNCE`. It must contain
enough information for the mothership to decide whether the vehicle can be
tasked safely without guessing:

```json
{
  "protocol": "dronefleet",
  "schema_version": 1,
  "drone_id": "vendorA-scout-07",
  "callsign": "Scout 07",
  "vehicle_type": "multirotor-recon",
  "manufacturer": "Vendor A",
  "model": "R-4",
  "serial_number": "R4-0007",
  "firmware_version": "4.8.1",
  "protocol_version": "1.0",
  "capabilities": [
    {
      "name": "area_search",
      "version": "1.0",
      "description": "search a bounded region",
      "parameters": {"region": "object"},
      "supports_autonomous_execution": true,
      "requires_human_authorization": false
    }
  ],
  "sensors": [
    {
      "sensor_id": "thermal-1",
      "kind": "thermal",
      "modality": "LWIR",
      "resolution": "640x512",
      "max_range_m": 600,
      "fov_deg": 45,
      "accuracy": "manufacturer-declared",
      "status": "OPERATIONAL"
    }
  ],
  "payloads": [],
  "constraints": {
    "endurance_min": 60,
    "cruise_ms": 18,
    "max_speed_ms": 25,
    "accel_ms2": 3,
    "turn_rate_deg_s": 60,
    "comms_range_m": 8000,
    "altitude_m": 120,
    "sensor_fov_deg": 75,
    "payload_kg": 0,
    "mass_kg": 6
  },
  "state": {
    "availability": "AVAILABLE",
    "mode": "LANDED",
    "battery_pct": 98.4,
    "health": "NOMINAL",
    "nav_fix": "3D"
  },
  "navigation": {
    "coordinate_frame": "WGS84",
    "position": {"lat": 0.0, "lon": 0.0, "alt_m": 0.0},
    "home_position": {"lat": 0.0, "lon": 0.0, "alt_m": 0.0},
    "accuracy_m": 2.5
  },
  "communications": {
    "transport": "mqtt",
    "qos": 1,
    "supports_reconnect": true,
    "link_address": "fleet/v1/drone/vendorA-scout-07"
  },
  "safety": {
    "return_to_home": true,
    "min_battery_reserve_pct": 15,
    "geofence": null,
    "human_authorization_required_for": [],
    "failsafe_actions": ["HOLD", "RETURN_TO_HOME", "LAND"]
  },
  "security": {
    "authentication": "mtls",
    "identity_key": "sha256:fingerprint"
  }
}
```

The important categories are:

1. **Identity:** stable ID, callsign, serial, manufacturer, model, firmware,
   protocol/schema versions.
2. **Capabilities:** action name, version, parameters, execution mode, and
   whether a human authorization is required.
3. **Sensors:** sensor identity, modality, range, resolution, field of view,
   accuracy and health.
4. **Payloads:** type, quantity, capacity, unit and readiness. Payloads must
   never be inferred from the vehicle type.
5. **Performance:** endurance, speed, altitude, mass, radio range and sensor
   envelope.
6. **State:** availability, flight mode, battery, health, navigation fix and
   current task.
7. **Navigation:** coordinate reference, current/home position and position
   accuracy.
8. **Safety:** reserve battery, geofence, return-to-home behavior and failsafe
   actions.
9. **Security:** authenticated identity and key/certificate fingerprint.

## Task contract

The mothership sends `TASK_ASSIGN` with `task_id`, `verb`, typed `params`, and
an estimated duration. The vehicle must independently validate the command
against its declared capability and current safety state:

```text
TASK_ASSIGN → TASK_ACK or TASK_REJECT
             → TASK_PROGRESS* → TASK_COMPLETE
```

Vehicles must reject unsupported verbs, expired tasks, insufficient battery,
invalid parameters, unavailable sensors/payloads, or commands outside their
geofence. They must be idempotent for a repeated `task_id`.

## Multi-agent behavior

The planner represents a mission as a dependency graph. For example:

```text
area_search
    ↓ contacts
classify_survivor
    ↓ survivor_confirmed
deliver_payload + guide_ground_team
```

The mothership dispatches only tasks whose dependencies are complete, records
the result token, and re-validates the remaining graph whenever a vehicle is
lost or rejects a task. This permits independently manufactured vehicles to
cooperate as long as their manifests expose compatible capability contracts.

If a vehicle disappears during execution, completed steps remain complete,
the lost vehicle's incomplete steps return to `PENDING`, and the planner binds
those steps to an online compatible vehicle. A vehicle that joins later is
also included in the next re-plan, so it can take over pending work without
duplicating a task already completed by another vehicle.

## Authorization boundary

Observation, search, relay, rescue, reporting and delivery workflows may be
automated subject to the declared safety constraints. Capabilities involving
weapon release, bombardment, or application of force are not part of the
autonomous protocol. The wire model has explicit authorization message types,
but the reference mothership does not synthesize or grant them. Any future
high-risk integration must require authenticated human approval, a bounded
target and time window, positive identification, and an auditable decision.

## Reference standalone node

Start the mothership in one terminal:

```bash
python3 run.py --no-llm --mqtt-port 18830
```

Start independently launched reference vehicles in other terminals:

```bash
python3 -m fleet.drone_node --id scout-1 --profile recon --mqtt-port 18830
python3 -m fleet.drone_node --id courier-1 --profile courier --mqtt-port 18830
python3 -m fleet.drone_node --id relay-1 --profile relay --mqtt-port 18830
```

The UI should show these units as they announce. Type a rescue mission in the
UI; the mothership will plan and dispatch across the online units. The node's
executor is deliberately a reference simulation. A hardware adapter replaces
that executor while keeping the manifest and message contract unchanged.
