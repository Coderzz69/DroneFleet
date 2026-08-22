# DroneFleet Interoperability Protocol v1

This project treats the mothership and each vehicle as independent network
participants. A vendor-specific vehicle adapter only has to implement this
contract; the mothership never instantiates, imports or inspects its code.

The reference implementation uses MQTT 3.1.1 and JSON. The demo broker is
deliberately unauthenticated and QoS 0. A production deployment must put the
same message contract behind a broker configured for TLS/mTLS, ACLs, durable
sessions, appropriate QoS and signed device identity. Nothing below assumes a
trusted network, but nothing below provides one either.

---

## 1. Vehicle lifecycle

```text
OFFLINE
  -> CONNECTING
  -> ONLINE      (CAPABILITY_ANNOUNCE, then HEARTBEAT)
  -> AVAILABLE
  -> TASKED      (TASK_ASSIGN / TASK_ACK)
  -> EXECUTING   (TASK_PROGRESS + TELEMETRY)
  -> AVAILABLE   (TASK_COMPLETE)
  -> RETURNING   (RECALL)
  -> OFFLINE
```

The mothership considers a vehicle lost when its heartbeat deadline expires
(`HEARTBEAT_TIMEOUT_S`, 5 s). It must never infer a vehicle's position or
health from the command it last sent; those come only from received telemetry
and heartbeats. That separation is enforced in the implementation:
`sim.world.DroneState` is simulator ground truth, `agents.master.DroneView` is
belief built purely from received messages, and only the second is allowed to
drive a decision.

## 2. Topics

All v1 traffic lives under `fleet/v1/`. The version is in the topic root, not
only in the envelope, so a v2 fleet can share a broker without either side
parsing and discarding the other's traffic.

| Topic | Direction | Purpose |
|---|---|---|
| `fleet/v1/broadcast` | mothership → fleet | discovery, abort, global notices |
| `fleet/v1/master/inbox` | vehicle → mothership | registration, ACKs, events |
| `fleet/v1/drone/{id}/inbox` | mothership → vehicle | commands for one vehicle |
| `fleet/v1/drone/{id}/telemetry` | vehicle → mothership | position and live state |

`fleet/v1/console`, `fleet/v1/mission/plan` and `fleet/v1/world/tick` also
exist, but they carry UI text rather than protocol envelopes. A protocol
client should ignore them; `fleet.net.topics.UI_TOPICS` names them.

## 3. Envelope

Every message on every topic has the same shape:

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
  "corr_id": "T2",
  "requires_ack": true,
  "expires_at": null,
  "payload": {}
}
```

- `protocol` / `protocol_version` — a receiver must ignore a message whose
  protocol name differs, or whose **major** version it does not implement.
  A missing stamp is tolerated so an older sender still works; a stamp that
  disagrees is not. `Envelope.understood()` is that check.
- `msg_id` — deduplication.
- `corr_id` — ties an ACK, every progress report and the completion back to
  the order that caused them.
- `mission_id` — groups one task graph. **Task ids are only unique within a
  mission**: the planner numbers them `T1, T2, …` positionally, so every plan
  restarts at `T1`. A vehicle that dedupes on `task_id` alone will discard the
  second mission entirely. Dedupe on `(mission_id, task_id)`.
- `expires_at` — an order past this time is void. Reject it rather than fly it.

### Message types

| Type | Meaning |
|---|---|
| `CAPABILITY_QUERY` / `CAPABILITY_ANNOUNCE` | Discovery and registration |
| `TASK_ASSIGN` / `TASK_ACK` / `TASK_REJECT` | Tasking |
| `TASK_PROGRESS` / `TASK_COMPLETE` | Execution |
| `TELEMETRY` / `HEARTBEAT` / `ALERT` | Housekeeping |
| `RECALL` / `ABORT` | Come home / stop now |
| `AUTHORIZATION_REQUEST` / `AUTHORIZATION_GRANT` / `AUTHORIZATION_DENY` | The human-approval boundary — see §7 |

## 4. The manifest

The first message after connecting is `CAPABILITY_ANNOUNCE`, carrying a full
manifest. It has to contain enough for the mothership to decide whether the
vehicle can be tasked safely **without guessing**.

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
    "endurance_min": 60, "cruise_ms": 18, "max_speed_ms": 25,
    "accel_ms2": 3, "turn_rate_deg_s": 60, "comms_range_m": 8000,
    "altitude_m": 120, "sensor_fov_deg": 75, "payload_kg": 0, "mass_kg": 6
  },
  "state": {
    "availability": "AVAILABLE", "mode": "LANDED",
    "battery_pct": 98.4, "health": "NOMINAL", "nav_fix": "3D"
  },
  "navigation": {
    "coordinate_frame": "WGS84",
    "position": {"lat": 0.0, "lon": 0.0, "alt_m": 0.0},
    "home_position": {"lat": 0.0, "lon": 0.0, "alt_m": 0.0},
    "accuracy_m": 2.5
  },
  "communications": {
    "transport": "mqtt", "qos": 1,
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
  "security": {"authentication": "mtls", "identity_key": "sha256:fingerprint"}
}
```

The nine categories, and why each one is not optional:

1. **Identity** — stable id, callsign, serial, manufacturer, model, firmware,
   protocol/schema versions.
2. **Capabilities** — action name, version, parameters, execution mode, and
   whether a human authorization is required.
3. **Sensors** — identity, modality, range, resolution, field of view,
   accuracy and health. `kind` is what a domain pack matches on; `modality` is
   extra detail. Both are accepted, `kind` first.
4. **Payloads** — type, quantity, capacity, unit, readiness. Payloads must
   never be inferred from the vehicle type.
5. **Performance** — endurance, speed, altitude, mass, radio range, sensor
   envelope. This is what feasibility is computed from.
6. **State** — availability, flight mode, battery, health, nav fix.
7. **Navigation** — coordinate frame, current and home position, accuracy.
8. **Safety** — reserve battery, geofence, RTH behaviour, failsafe actions.
9. **Security** — authenticated identity and key/certificate fingerprint.

### How the mothership reads it

`fleet/core/protocol.py` is the only reader. Three rules govern it:

- **Identity and version are strict.** No `drone_id`, no `callsign`, a foreign
  `protocol`, or an unsupported major version — the manifest is rejected and
  the operator is told which. A vehicle may only announce *itself*: a manifest
  whose `drone_id` differs from the envelope `src` is refused, or anything on
  the bus could overwrite another vehicle's capabilities.
- **Everything else is coerced, never trusted.** A malformed optional field
  falls back to its default instead of raising. One bad field should not cost
  you the aircraft. Numbers outside the physical envelope are clamped by the
  same `nlp.LIMITS` table the typed-description path uses, and the clamp is
  reported rather than hidden.
- **The projection is conservative.** See §7.

## 5. Task contract

The mothership sends `TASK_ASSIGN` with `task_id`, `verb`, typed `params` and
an estimated duration:

```text
TASK_ASSIGN → TASK_ACK or TASK_REJECT
             → TASK_PROGRESS* → TASK_COMPLETE
```

A vehicle **must** independently validate the order against its own declared
capability and current state, and reject:

- a verb it never declared,
- a capability it declared as needing human authorization,
- an order past `expires_at`,
- insufficient battery against its own `min_battery_reserve_pct`,
- invalid or malformed parameters,
- a target outside its geofence.

The mothership having already checked feasibility is not a reason to skip
this. A vehicle that executes whatever it is sent is one bad plan away from
flying itself into the ground.

A repeated `(mission_id, task_id)` must be **idempotent, and answered**: re-send
the ACK rather than going silent, because the likeliest reason for a repeat is
that the first ACK was lost.

`TASK_COMPLETE` should carry the tokens the verb is contracted to produce, plus
`issued_at` and `ttl_s`. The mothership unlocks dependent steps from those
tokens and enforces pack freshness policies from the timestamps; a completion
without them advances the graph on nothing.

## 6. Multi-agent behaviour

The planner represents a mission as a dependency graph:

```text
area_search
    ↓ contacts
classify_survivor
    ↓ survivor_confirmed
deliver_payload + guide_ground_team
```

The mothership dispatches only tasks whose dependencies are complete, records
the tokens each result produced, and re-validates the remaining graph whenever
the fleet changes. Independently manufactured vehicles cooperate as long as
their manifests expose compatible capability contracts — nothing in the graph
knows or cares which vendor is flying which step.

**When a vehicle is lost**, completed steps stay complete, its incomplete steps
return to `PENDING`, and the fresh planner binding picks a survivor for them.
A task still in flight on a *live* vehicle is left alone; rebinding it would
send a second drone to redo work already underway.

**When a vehicle joins**, the active mission is re-planned so the newcomer can
take pending work, and a verdict that was `INSUFFICIENT` can become runnable
mid-mission. What it cannot do is duplicate a step another vehicle already
completed.

Carry-over between the old and fresh graph is keyed by **verb, not task id**.
Ids are positional — `T2` is simply the second link of the chain — and the
chain is re-derived from the surviving fleet, so `T2` can be a different verb
than it was a moment ago. Keying on the id would mark an untouched task done.
Each verb appears at most once per plan, which is what makes it a safe key.

## 7. Authorization boundary

Observation, search, relay, rescue, reporting and delivery workflows may be
automated subject to the declared safety constraints. **Capabilities that apply
force or release a weapon are not part of the autonomous protocol.**

The wire model has explicit authorization message types, and the reference
mothership never synthesizes or grants one. There is deliberately no code path
anywhere in this repository that issues an `AUTHORIZATION_GRANT`.

Two gates enforce it:

1. `DroneManifest.to_record()` does not offer a capability marked
   `requires_human_authorization`, or one the vehicle says it cannot execute
   autonomously, to the planner at all. It is kept in the manifest for audit
   and display, and the reason is recorded in `DroneRecord.withheld`. The
   mission then comes back with an honest gap naming the missing capability,
   rather than a plan that quietly stalls.
2. The dispatcher checks again before sending. A task that reaches it needing
   authorization is failed with a stated reason and an `AUTHORIZATION_REQUEST`
   on the console — not held silently, because a task stuck in `PENDING` looks
   like a bug, and not retried by re-planning, because no replan can fix it.

Any future high-risk integration must require authenticated human approval, a
bounded target and time window, positive identification, and an auditable
decision. This document is not that design.

## 8. Reference standalone node

Start the mothership in one terminal:

```bash
python3 run.py --no-llm --mqtt-port 18830
```

Start independently launched vehicles in others:

```bash
python3 -m fleet.drone_node --id scout-1   --profile recon   --mqtt-port 18830
python3 -m fleet.drone_node --id courier-1 --profile courier --mqtt-port 18830
python3 -m fleet.drone_node --id medic-1   --profile rescue  --mqtt-port 18830
```

Profiles: `recon`, `courier`, `rescue`, `mission`, `relay`. Use
`--capability <verb>` (repeatable) to declare a different set.

`relay_comms` is one of the `CORE_VERBS` every pack inherits, so a `relay`
vehicle is real tasking, not decoration: when the mission region is beyond the
working drones' `comms_range_m`, `planner._ensure_relay` inserts a `T0`
station-keeping task and binds it to the longest-ranged relay on station. Boot
a scout with a short radio and a far-off mission, then start a relay, and the
verdict moves off the comms gap.

The UI shows these units as they announce, on the map and in the party panel,
driven purely by their telemetry. Type a rescue mission, and the mothership
plans and dispatches across whatever is online. Kill one mid-mission and watch
the work rebind; boot another and watch it get planned in.

Watch the traffic from a fourth terminal:

```bash
python3 tools/mqtt_tail.py --port 18830
python3 tools/mqtt_send.py discover --port 18830
```

The node's executor is deliberately a reference simulation. A hardware adapter
replaces that executor while leaving the manifest and every message on the wire
unchanged — that is the whole claim this module exists to make good on.

## 9. Tests

```bash
python3 -m tests.test_protocol
```

The pure half (manifest parsing, coercion, clamping, the safety projection,
envelope versioning) runs with no broker and no event loop. The live half runs
real missions over a real broker against vehicles the mothership did not
create: a full multi-vehicle DAG, vehicle-side refusals, duplicate orders,
failover mid-task, a vehicle joining mid-mission, and the authorization
boundary.
