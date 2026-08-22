"""Standalone reference vehicle.

Run this in its own terminal, separately from the mothership. It is a protocol
reference, not a flight controller: the executor below is a simulated hardware
adapter. Replacing it with MAVLink, ROS 2 or a vendor SDK must not require
changing a single message on the wire -- that is the whole claim this module
exists to make good on.

    python3 -m fleet.drone_node --id scout-1 --profile recon --mqtt-port 18830
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import time
from typing import Optional

from .core.messages import BROADCAST, Envelope, MsgType
from .core.protocol import (CapabilityContract, DroneManifest, PayloadContract,
                            SensorContract)
from .core.registry import Constraints
from .net import topics
from .net.mqtt_client import MQTTClient

log = logging.getLogger("drone.node")

HEARTBEAT_S = 2.0
TELEMETRY_S = 0.5

# Flight modes that mean "this thing is off the ground". The master renders
# position from telemetry alone, so getting this wrong parks a flying drone on
# the map at the depot.
AIRBORNE_MODES = frozenset({"TRANSIT", "WORKING", "ON_STATION", "RETURNING"})


PROFILES: dict[str, dict] = {
    "recon": {
        "vehicle_type": "multirotor-recon",
        "capabilities": ["area_search", "classify_survivor"],
        "sensors": [
            {"sensor_id": "eo-1", "kind": "rgb", "modality": "visible",
             "resolution": "4K", "max_range_m": 1200, "fov_deg": 75},
            {"sensor_id": "thermal-1", "kind": "thermal", "modality": "LWIR",
             "resolution": "640x512", "max_range_m": 600, "fov_deg": 45},
        ],
        "constraints": Constraints(endurance_min=60, cruise_ms=18, max_speed_ms=25,
                                   comms_range_m=8000, altitude_m=120,
                                   sensor_fov_deg=75, mass_kg=6),
    },
    "courier": {
        "vehicle_type": "multirotor-courier",
        "capabilities": ["deliver_payload"],
        "sensors": [{"sensor_id": "eo-1", "kind": "rgb", "modality": "visible",
                     "resolution": "1080p", "max_range_m": 800}],
        "payloads": [{"payload_id": "medical-kit", "kind": "medical_supplies",
                      "quantity": 1, "unit": "kit", "capacity_kg": 5}],
        "constraints": Constraints(endurance_min=45, cruise_ms=20, max_speed_ms=28,
                                   comms_range_m=8000, payload_kg=5, mass_kg=9),
    },
    "relay": {
        # `relay_comms` is a CORE_VERB, so every pack has it. The planner's
        # `_ensure_relay` tasks one of these automatically when the mission is
        # beyond the working drones' radio range.
        "vehicle_type": "communications-relay",
        "capabilities": ["relay_comms"],
        "sensors": [],
        "constraints": Constraints(endurance_min=120, cruise_ms=16, max_speed_ms=22,
                                   comms_range_m=40000, altitude_m=300, mass_kg=8),
    },
    "rescue": {
        "vehicle_type": "rescue-support",
        "capabilities": ["classify_survivor", "guide_ground_team"],
        "sensors": [{"sensor_id": "thermal-1", "kind": "thermal", "modality": "LWIR",
                     "resolution": "640x512", "max_range_m": 900}],
        "constraints": Constraints(endurance_min=75, cruise_ms=18, max_speed_ms=25,
                                   comms_range_m=12000, mass_kg=7),
    },
    "mission": {
        "vehicle_type": "multi-role-uav",
        "capabilities": ["area_search", "classify_survivor", "deliver_payload",
                         "guide_ground_team"],
        "sensors": [{"sensor_id": "eo-1", "kind": "rgb", "modality": "visible",
                     "resolution": "4K", "max_range_m": 1500},
                    {"sensor_id": "thermal-1", "kind": "thermal", "modality": "LWIR",
                     "resolution": "640x512", "max_range_m": 700}],
        "payloads": [{"payload_id": "medical-kit", "kind": "medical_supplies",
                      "quantity": 1, "unit": "kit", "capacity_kg": 5}],
        "constraints": Constraints(endurance_min=90, cruise_ms=20, max_speed_ms=30,
                                   comms_range_m=15000, payload_kg=5, mass_kg=10),
    },
}


def make_manifest(args: argparse.Namespace) -> DroneManifest:
    profile = PROFILES[args.profile]
    caps = args.capability or profile["capabilities"]
    contracts = [CapabilityContract(
        name=name,
        description=f"Reference {name.replace('_', ' ')} capability",
        parameters={"region": "object", "task_id": "string"},
    ) for name in caps]
    return DroneManifest(
        drone_id=args.id,
        callsign=args.name or args.id,
        vehicle_type=profile["vehicle_type"],
        manufacturer=args.manufacturer,
        model=args.model,
        serial_number=args.serial or args.id,
        firmware_version=args.firmware,
        capabilities=contracts,
        sensors=[SensorContract(**s) for s in profile.get("sensors", [])],
        payloads=[PayloadContract(**p) for p in profile.get("payloads", [])],
        constraints=profile["constraints"],
        communications={"transport": "mqtt", "qos": 0,
                        "broker": f"{args.mqtt_host}:{args.mqtt_port}",
                        "supports_reconnect": True},
    )


class ProtocolDrone:
    """One vehicle, speaking only the wire contract."""

    def __init__(self, manifest: DroneManifest, host: str, port: int,
                 speedup: float = 8.0, home: tuple[float, float] = (600.0, 11400.0)) -> None:
        self.manifest = manifest
        self.mqtt = MQTTClient(f"drone-{manifest.drone_id}", host, port)
        self.speedup = max(0.1, speedup)
        self.running = True
        self.battery_pct = float(manifest.state.get("battery_pct", 100.0) or 100.0)
        self.mode = "LANDED"
        # Reference-world coordinates. A hardware adapter would take these from
        # its navigation solution instead.
        self.home = home
        self.x, self.y = home
        self.target_x, self.target_y = home
        self.heading_deg = 0.0
        self.speed_ms = 0.0
        self._loops: list[asyncio.Task] = []
        self._task: Optional[asyncio.Task] = None
        # task key -> the ACK we already sent, so a repeated order is answered
        # again instead of silently dropped.
        self._acked: dict[str, dict] = {}

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        await self.mqtt.connect()
        await self.mqtt.subscribe(topics.BROADCAST, self._on_message)
        await self.mqtt.subscribe(topics.drone_inbox(self.manifest.drone_id),
                                  self._on_message)
        self._loops.append(asyncio.create_task(self._heartbeat_loop()))
        self._loops.append(asyncio.create_task(self._telemetry_loop()))
        await self.announce()

    async def stop(self) -> None:
        self.running = False
        for task in self._loops:
            task.cancel()
        self._loops.clear()
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        await self.mqtt.disconnect()

    @property
    def airborne(self) -> bool:
        return self.mode in AIRBORNE_MODES

    def _availability(self) -> str:
        if self._task and not self._task.done():
            return "BUSY"
        return "AVAILABLE"

    async def _send(self, env: Envelope) -> None:
        await self.mqtt.publish(topics.MASTER_INBOX, env.to_json())

    # -- outbound ------------------------------------------------------------
    async def announce(self) -> None:
        self.manifest.state.update({
            "availability": self._availability(), "mode": self.mode,
            "battery_pct": round(self.battery_pct, 1),
        })
        await self._send(Envelope(
            type=MsgType.CAPABILITY_ANNOUNCE, src=self.manifest.drone_id,
            dst="master-0", payload=self.manifest.to_dict(), requires_ack=True))

    async def _heartbeat_loop(self) -> None:
        try:
            while self.running:
                await self._send(Envelope(
                    type=MsgType.HEARTBEAT, src=self.manifest.drone_id,
                    dst="master-0", payload={
                        "availability": self._availability(), "mode": self.mode,
                        "battery_pct": round(self.battery_pct, 1),
                        "health": "NOMINAL", "link_ok": self.mqtt.connected,
                        "status": self.mode,
                        "firmware_version": self.manifest.firmware_version,
                    }))
                await asyncio.sleep(HEARTBEAT_S)
        except asyncio.CancelledError:
            pass

    async def _telemetry_loop(self) -> None:
        try:
            while self.running:
                await self.mqtt.publish(
                    topics.drone_telemetry(self.manifest.drone_id),
                    Envelope(type=MsgType.TELEMETRY, src=self.manifest.drone_id,
                             dst="master-0", payload={
                                 "x": round(self.x, 1), "y": round(self.y, 1),
                                 "heading_deg": round(self.heading_deg, 1),
                                 "speed_ms": round(self.speed_ms, 2),
                                 "target_x": round(self.target_x, 1),
                                 "target_y": round(self.target_y, 1),
                                 "airborne": self.airborne,
                                 "battery_pct": round(self.battery_pct, 1),
                                 "mode": self.mode, "link_via": "direct",
                             }).to_json())
                await asyncio.sleep(TELEMETRY_S)
        except asyncio.CancelledError:
            pass

    # -- inbound -------------------------------------------------------------
    async def _on_message(self, _topic: str, payload: bytes) -> None:
        try:
            env = Envelope.from_json(payload)
        except Exception:                                    # noqa: BLE001
            return                                           # not our traffic
        if not env.understood():
            log.warning("ignoring %s from %s: protocol %s/%s",
                        env.type, env.src, env.protocol, env.protocol_version)
            return
        if env.dst not in (self.manifest.drone_id, BROADCAST):
            return

        if env.type == MsgType.CAPABILITY_QUERY:
            await self.announce()
        elif env.type == MsgType.TASK_ASSIGN:
            await self._accept(env)
        elif env.type == MsgType.RECALL:
            await self._recall()
        elif env.type == MsgType.ABORT:
            await self._abort()

    async def _recall(self) -> None:
        """Ordered home. The work is finished, not cancelled."""
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self.mode = "RETURNING"
        self.target_x, self.target_y = self.home
        self.x, self.y = self.home
        self.speed_ms = 0.0
        await self._send(Envelope(type=MsgType.ALERT, src=self.manifest.drone_id,
                                  dst="master-0",
                                  payload={"text": "returning to home"}))
        self.mode = "LANDED"

    async def _abort(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self.mode = "ABORTED"
        self.speed_ms = 0.0

    def _reject(self, task_id: str, reason: str) -> Envelope:
        return Envelope(type=MsgType.TASK_REJECT, src=self.manifest.drone_id,
                        dst="master-0", corr_id=task_id,
                        payload={"task_id": task_id, "reason": reason})

    async def _accept(self, env: Envelope) -> None:
        """Validate an order against our own declared contract and state.

        The mothership already checked feasibility. That is not a reason to
        skip checking here: a vehicle that executes whatever it is sent is one
        bad plan away from flying itself into the ground.
        """
        task_id = str(env.payload.get("task_id") or env.corr_id or "")
        verb = str(env.payload.get("verb", ""))
        if not task_id:
            return
        # Task ids restart at T1 every plan, so they are only unique within a
        # mission. Scoping the dedup key stops a second mission's T1 from
        # being mistaken for one we already flew.
        key = f"{env.mission_id or 'unscoped'}:{task_id}"

        if key in self._acked:
            # Idempotent, as the contract requires: re-answer rather than go
            # quiet, because the likeliest reason for a repeat is a lost ACK.
            await self._send(Envelope(
                type=MsgType.TASK_ACK, src=self.manifest.drone_id, dst="master-0",
                corr_id=task_id, mission_id=env.mission_id,
                payload={**self._acked[key], "duplicate": True}))
            return

        if env.expired():
            await self._send(self._reject(task_id, "order expired before it was received"))
            return
        if verb not in {c.name for c in self.manifest.capabilities}:
            await self._send(self._reject(task_id, "capability not declared"))
            return
        contract = next(c for c in self.manifest.capabilities if c.name == verb)
        if not contract.autonomous():
            await self._send(self._reject(
                task_id, "capability requires human authorization"))
            return
        reserve = float(self.manifest.safety.get("min_battery_reserve_pct", 15.0) or 0.0)
        if self.battery_pct <= reserve:
            await self._send(self._reject(task_id, "battery reserve reached"))
            return
        if not isinstance(env.payload.get("params", {}), dict):
            await self._send(self._reject(task_id, "invalid params"))
            return

        ack = {"task_id": task_id, "verb": verb, "accepted_at": time.time()}
        self._acked[key] = ack
        await self._send(Envelope(
            type=MsgType.TASK_ACK, src=self.manifest.drone_id, dst="master-0",
            corr_id=task_id, mission_id=env.mission_id, payload=dict(ack)))

        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._execute(env))

    # -- execution -----------------------------------------------------------
    def _target_for(self, params: dict) -> tuple[float, float]:
        region = params.get("region") or {}
        if not isinstance(region, dict) or not region:
            return self.x, self.y
        return (float(region.get("x", self.x)) + float(region.get("w", 0.0)) / 2.0,
                float(region.get("y", self.y)) + float(region.get("h", 0.0)) / 2.0)

    def _result_for(self, verb: str, task_id: str) -> dict:
        """Report what this task produced.

        The mothership unlocks dependent steps from the tokens a completion
        carries, and enforces freshness policies from `issued_at`/`ttl_s`. A
        completion without them advances the graph on nothing.
        """
        result = {"task_id": task_id, "verb": verb, "simulation": True,
                  "issued_at": time.time(), "ttl_s": 600,
                  "reported_at": time.time()}
        if verb == "area_search":
            result["contacts"] = [f"{self.manifest.drone_id}-c1"]
            result["count"] = 1
        elif verb.startswith("classify"):
            result["contact"] = f"{self.manifest.drone_id}-c1"
            result["kind"] = "unknown"
        return result

    async def _execute(self, env: Envelope) -> None:
        task_id = str(env.payload.get("task_id") or env.corr_id or "")
        verb = str(env.payload.get("verb", ""))
        params = env.payload.get("params") or {}
        estimated = max(2.0, float(env.payload.get("est_duration_s", 20.0) or 20.0)
                        / self.speedup)

        target_x, target_y = self._target_for(params)
        self.target_x, self.target_y = target_x, target_y
        start_x, start_y = self.x, self.y
        distance = math.hypot(target_x - start_x, target_y - start_y)
        if distance > 0:
            self.heading_deg = math.degrees(
                math.atan2(target_y - start_y, target_x - start_x)) % 360.0
        self.mode = "TRANSIT"
        started = time.monotonic()
        try:
            while self.running:
                await asyncio.sleep(0.25)
                progress = min(1.0, (time.monotonic() - started) / estimated)
                self.mode = "WORKING" if progress > 0.25 else "TRANSIT"
                if distance > 0:
                    self.x = start_x + (target_x - start_x) * progress
                    self.y = start_y + (target_y - start_y) * progress
                    self.speed_ms = distance / estimated
                else:
                    self.speed_ms = 0.0
                # Rough but monotonic: the point is that endurance is finite
                # and the master watches it, not that the model is exact.
                self.battery_pct = max(0.0, self.battery_pct - 0.05)
                await self._send(Envelope(
                    type=MsgType.TASK_PROGRESS, src=self.manifest.drone_id,
                    dst="master-0", corr_id=task_id, mission_id=env.mission_id,
                    payload={"task_id": task_id, "progress": round(progress, 3),
                             "activity": self.mode}))
                if progress >= 1.0:
                    break

            self.mode = "ON_STATION"
            self.speed_ms = 0.0
            await self._send(Envelope(
                type=MsgType.TASK_COMPLETE, src=self.manifest.drone_id,
                dst="master-0", corr_id=task_id, mission_id=env.mission_id,
                payload={"task_id": task_id, "verb": verb,
                         "result": self._result_for(verb, task_id)}))
        except asyncio.CancelledError:
            self.speed_ms = 0.0
            raise
        finally:
            self._task = None


async def run(args: argparse.Namespace) -> None:
    node = ProtocolDrone(make_manifest(args), args.mqtt_host, args.mqtt_port,
                         args.speedup)
    await node.start()
    print(f"Drone online: {node.manifest.drone_id} ({node.manifest.callsign})")
    print(f"  profile={args.profile} "
          f"capabilities={','.join(c.name for c in node.manifest.capabilities)}")
    print(f"  protocol={node.manifest.protocol_version} "
          f"mqtt={args.mqtt_host}:{args.mqtt_port}")
    try:
        await asyncio.Event().wait()
    finally:
        await node.stop()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Standalone DroneFleet protocol drone")
    p.add_argument("--id", required=True, help="globally unique vehicle identifier")
    p.add_argument("--name", help="operator callsign; defaults to --id")
    p.add_argument("--profile", choices=sorted(PROFILES), default="recon")
    p.add_argument("--capability", action="append",
                   help="declare this capability instead of the profile's set "
                        "(repeatable)")
    p.add_argument("--manufacturer", default="DroneFleet Reference")
    p.add_argument("--model", default="DF-Reference-1")
    p.add_argument("--serial", default="")
    p.add_argument("--firmware", default="0.1-demo")
    p.add_argument("--mqtt-host", default="127.0.0.1")
    p.add_argument("--mqtt-port", type=int, default=1883)
    p.add_argument("--speedup", type=float, default=8.0,
                   help="execution speed; 1 means the full estimated duration")
    return p


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    args = build_parser().parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
