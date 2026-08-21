"""Standalone reference drone process.

Run this in a separate terminal from the mothership.  It is a protocol
reference implementation, not a flight controller: the task executor is a
simulated hardware adapter that can later be replaced by MAVLink, ROS 2, or a
vendor SDK without changing the wire contract.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import time
from typing import Optional

from .core.messages import BROADCAST, Envelope, MsgType
from .net import topics
from .net.mqtt_client import MQTTClient
from .protocol import (CapabilityContract, DroneManifest, PayloadContract,
                       SensorContract)
from .core.registry import Constraints

log = logging.getLogger("drone.node")


PROFILES: dict[str, dict] = {
    "recon": {
        "vehicle_type": "multirotor-recon",
        "capabilities": ["area_search", "classify_survivor"],
        "sensors": [
            {"sensor_id": "eo-1", "kind": "rgb", "modality": "rgb",
             "resolution": "4K", "max_range_m": 1200, "fov_deg": 75},
            {"sensor_id": "thermal-1", "kind": "thermal", "modality": "thermal",
             "resolution": "640x512", "max_range_m": 600, "fov_deg": 45},
        ],
        "constraints": Constraints(endurance_min=60, cruise_ms=18, max_speed_ms=25,
                                    comms_range_m=8000, altitude_m=120,
                                    sensor_fov_deg=75, mass_kg=6),
    },
    "courier": {
        "vehicle_type": "multirotor-courier",
        "capabilities": ["deliver_payload"],
        "sensors": [{"sensor_id": "eo-1", "kind": "rgb", "modality": "rgb",
                      "resolution": "1080p", "max_range_m": 800}],
        "payloads": [{"payload_id": "medical-kit", "kind": "medical_supplies",
                       "quantity": 1, "unit": "kit", "capacity_kg": 5}],
        "constraints": Constraints(endurance_min=45, cruise_ms=20, max_speed_ms=28,
                                    comms_range_m=8000, payload_kg=5, mass_kg=9),
    },
    "relay": {
        "vehicle_type": "communications-relay",
        "capabilities": ["relay_comms"],
        "sensors": [],
        "constraints": Constraints(endurance_min=120, cruise_ms=16, max_speed_ms=22,
                                    comms_range_m=40000, altitude_m=300, mass_kg=8),
    },
    "rescue": {
        "vehicle_type": "rescue-support",
        "capabilities": ["guide_ground_team", "assess"],
        "sensors": [{"sensor_id": "eo-1", "kind": "rgb", "modality": "rgb",
                      "resolution": "4K", "max_range_m": 1500}],
        "constraints": Constraints(endurance_min=75, cruise_ms=18, max_speed_ms=25,
                                    comms_range_m=12000, mass_kg=7),
    },
    "mission": {
        "vehicle_type": "multi-role-uav",
        "capabilities": ["area_search", "classify_survivor", "deliver_payload",
                          "guide_ground_team", "assess", "relay_comms"],
        "sensors": [{"sensor_id": "eo-1", "kind": "rgb", "modality": "rgb",
                      "resolution": "4K", "max_range_m": 1500},
                     {"sensor_id": "thermal-1", "kind": "thermal", "modality": "thermal",
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
    sensors = [SensorContract(**s) for s in profile.get("sensors", [])]
    payloads = [PayloadContract(**p) for p in profile.get("payloads", [])]
    constraints = profile["constraints"]
    return DroneManifest(
        drone_id=args.id,
        callsign=args.name or args.id,
        vehicle_type=profile["vehicle_type"],
        manufacturer=args.manufacturer,
        model=args.model,
        serial_number=args.serial or args.id,
        firmware_version=args.firmware,
        capabilities=contracts,
        sensors=sensors,
        payloads=payloads,
        constraints=constraints,
        communications={"transport": "mqtt", "qos": 0,
                         "broker": f"{args.mqtt_host}:{args.mqtt_port}",
                         "supports_reconnect": True},
    )


class ProtocolDrone:
    def __init__(self, manifest: DroneManifest, host: str, port: int,
                 speedup: float = 8.0) -> None:
        self.manifest = manifest
        self.mqtt = MQTTClient(f"drone-{manifest.drone_id}", host, port)
        self.speedup = max(0.1, speedup)
        self.running = True
        self.battery_pct = float(manifest.state.get("battery_pct", 100.0))
        self.mode = "LANDED"
        # Reference-world coordinates match sim.World's base.  A hardware
        # adapter would initialize these from its navigation solution instead.
        self.x, self.y = 600.0, 11400.0
        self.target_x, self.target_y = self.x, self.y
        self.heading_deg = 0.0
        self.speed_ms = 0.0
        self._loops: list[asyncio.Task] = []
        self._task: Optional[asyncio.Task] = None
        self._handled: set[str] = set()

    async def start(self) -> None:
        await self.mqtt.connect()
        await self.mqtt.subscribe(topics.BROADCAST, self._on_message)
        await self.mqtt.subscribe(topics.drone_inbox(self.manifest.drone_id), self._on_message)
        self._loops.extend([
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self._telemetry_loop()),
        ])
        await self.announce()

    async def stop(self) -> None:
        self.running = False
        for task in self._loops:
            task.cancel()
        if self._task and not self._task.done():
            self._task.cancel()
        await self.mqtt.disconnect()

    async def _send(self, env: Envelope) -> None:
        await self.mqtt.publish(topics.MASTER_INBOX, env.to_json())

    async def announce(self) -> None:
        self.manifest.state.update({"availability": "AVAILABLE", "mode": self.mode,
                                    "battery_pct": round(self.battery_pct, 1)})
        await self._send(Envelope(
            type=MsgType.CAPABILITY_ANNOUNCE, src=self.manifest.drone_id,
            dst="master-0", payload=self.manifest.to_dict(), requires_ack=True))

    async def _heartbeat_loop(self) -> None:
        try:
            while self.running:
                await self._send(Envelope(
                    type=MsgType.HEARTBEAT, src=self.manifest.drone_id,
                    dst="master-0", payload={
                        "availability": "BUSY" if self._task else "AVAILABLE",
                        "mode": self.mode, "battery_pct": round(self.battery_pct, 1),
                        "health": "NOMINAL", "link_ok": self.mqtt.connected,
                        "firmware_version": self.manifest.firmware_version,
                    }))
                await asyncio.sleep(2.0)
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
                                 "airborne": self.mode in ("TRANSIT", "WORKING", "ON_STATION"),
                                 "battery_pct": round(self.battery_pct, 1),
                                 "mode": self.mode, "link_via": "direct",
                             }).to_json())
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass

    async def _on_message(self, _topic: str, payload: bytes) -> None:
        env = Envelope.from_json(payload)
        if env.dst not in (self.manifest.drone_id, BROADCAST):
            return
        if env.type == MsgType.CAPABILITY_QUERY:
            await self.announce()
        elif env.type == MsgType.TASK_ASSIGN:
            await self._accept(env)
        elif env.type == MsgType.RECALL:
            self.mode = "RETURNING"
            if self._task and not self._task.done():
                self._task.cancel()
            self._task = None
            await self._send(Envelope(type=MsgType.ALERT, src=self.manifest.drone_id,
                                       dst="master-0", payload={"text": "returning to home"}))
            self.mode = "LANDED"
        elif env.type == MsgType.ABORT:
            if self._task and not self._task.done():
                self._task.cancel()
            self._task = None
            self.mode = "ABORTED"

    async def _accept(self, env: Envelope) -> None:
        task_id = str(env.payload.get("task_id", env.corr_id or ""))
        verb = str(env.payload.get("verb", ""))
        mission_key = env.mission_id or "unscoped"
        task_key = f"{mission_key}:{task_id}"
        if not task_id or task_key in self._handled:
            return
        if verb not in {c.name for c in self.manifest.capabilities}:
            await self._send(Envelope(type=MsgType.TASK_REJECT, src=self.manifest.drone_id,
                                       dst="master-0", corr_id=task_id,
                                       payload={"task_id": task_id,
                                                "reason": "capability not declared"}))
            return
        reserve = float(self.manifest.safety.get("min_battery_reserve_pct", 15.0))
        if self.battery_pct <= reserve:
            await self._send(Envelope(type=MsgType.TASK_REJECT, src=self.manifest.drone_id,
                                       dst="master-0", corr_id=task_id,
                                       payload={"task_id": task_id,
                                                "reason": "battery reserve reached"}))
            return
        self._handled.add(task_key)
        await self._send(Envelope(type=MsgType.TASK_ACK, src=self.manifest.drone_id,
                                   dst="master-0", corr_id=task_id,
                                   payload={"task_id": task_id, "verb": verb,
                                            "accepted_at": time.time()}))
        self._task = asyncio.create_task(self._execute(env))

    async def _execute(self, env: Envelope) -> None:
        task_id = str(env.payload.get("task_id", env.corr_id or ""))
        verb = str(env.payload.get("verb", ""))
        params = env.payload.get("params") or {}
        estimated = max(2.0, float(env.payload.get("est_duration_s", 20.0)) / self.speedup)
        region = params.get("region") or {}
        target_x = float(region.get("x", self.x)) + float(region.get("w", 0.0)) / 2.0
        target_y = float(region.get("y", self.y)) + float(region.get("h", 0.0)) / 2.0
        self.target_x, self.target_y = target_x, target_y
        start_x, start_y = self.x, self.y
        distance = math.hypot(target_x - start_x, target_y - start_y)
        self.mode = "TRANSIT"
        started = time.monotonic()
        try:
            while self.running:
                await asyncio.sleep(0.5)
                elapsed = time.monotonic() - started
                progress = min(1.0, elapsed / estimated)
                self.mode = "WORKING" if progress > 0.25 else "TRANSIT"
                if distance > 0:
                    self.x = start_x + (target_x - start_x) * progress
                    self.y = start_y + (target_y - start_y) * progress
                    self.heading_deg = math.degrees(math.atan2(
                        target_y - start_y, target_x - start_x)) % 360.0
                    self.speed_ms = distance / estimated
                else:
                    self.speed_ms = 0.0
                self.battery_pct = max(0.0, self.battery_pct - 0.15)
                await self._send(Envelope(
                    type=MsgType.TASK_PROGRESS, src=self.manifest.drone_id,
                    dst="master-0", corr_id=task_id,
                    payload={"task_id": task_id, "progress": round(progress, 3),
                             "activity": self.mode}))
                if progress >= 1.0:
                    break
            self.mode = "ON_STATION"
            self.speed_ms = 0.0
            result = {"task_id": task_id, "verb": verb, "simulation": True,
                      "observations": [], "reported_at": time.time()}
            await self._send(Envelope(type=MsgType.TASK_COMPLETE,
                                       src=self.manifest.drone_id, dst="master-0",
                                       corr_id=task_id,
                                       payload={"task_id": task_id, "verb": verb,
                                                "result": result}))
            self.mode = "AVAILABLE"
            self._task = None
        except asyncio.CancelledError:
            self.mode = "ABORTED"
            self.speed_ms = 0.0
            self._task = None


async def run(args: argparse.Namespace) -> None:
    node = ProtocolDrone(make_manifest(args), args.mqtt_host, args.mqtt_port,
                         args.speedup)
    await node.start()
    print(f"Drone online: {node.manifest.drone_id} ({node.manifest.callsign})")
    print(f"  profile={args.profile} capabilities={','.join(c.name for c in node.manifest.capabilities)}")
    print(f"  protocol={node.manifest.protocol_version} mqtt={args.mqtt_host}:{args.mqtt_port}")
    try:
        await asyncio.Event().wait()
    finally:
        await node.stop()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Standalone DroneFleet protocol drone")
    p.add_argument("--id", required=True, help="globally unique vehicle identifier")
    p.add_argument("--name", help="operator callsign; defaults to --id")
    p.add_argument("--profile", choices=sorted(PROFILES), default="recon")
    p.add_argument("--capability", action="append", help="override/add one declared capability")
    p.add_argument("--manufacturer", default="DroneFleet Reference")
    p.add_argument("--model", default="DF-Reference-1")
    p.add_argument("--serial", default="")
    p.add_argument("--firmware", default="0.1-demo")
    p.add_argument("--mqtt-host", default="127.0.0.1")
    p.add_argument("--mqtt-port", type=int, default=1883)
    p.add_argument("--speedup", type=float, default=8.0,
                   help="simulation execution speed; 1 means real estimated time")
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
