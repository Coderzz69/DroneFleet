"""A slave drone: its own process-like agent with its own MQTT connection.

It knows only its own capabilities. It never sees the mission plan, the other
drones, or the world model beyond its own sensors -- everything it learns
arrives as a message.
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Optional

from ..core.messages import BROADCAST, Envelope, MsgType
from ..core.registry import DroneRecord
from ..net import topics
from ..net.mqtt_client import MQTTClient
from ..sim.world import Waypoint, World

HEARTBEAT_S = 1.5
TELEMETRY_S = 0.5


class DroneAgent:
    def __init__(self, record: DroneRecord, world: World, host: str, port: int) -> None:
        self.rec = record
        self.world = world
        self.mqtt = MQTTClient(f"drone-{record.id}", host, port)
        self._task: Optional[asyncio.Task] = None
        self._loops: list[asyncio.Task] = []
        self.running = True

    @property
    def state(self):
        return self.world.drones.get(self.rec.id)

    async def start(self) -> None:
        await self.mqtt.connect()
        await self.mqtt.subscribe(topics.BROADCAST, self._on_msg)
        await self.mqtt.subscribe(topics.drone_inbox(self.rec.id), self._on_msg)
        self._loops.append(asyncio.create_task(self._heartbeat_loop()))
        self._loops.append(asyncio.create_task(self._telemetry_loop()))
        await self._announce()

    async def stop(self) -> None:
        self.running = False
        for t in self._loops:
            t.cancel()
        if self._task:
            self._task.cancel()
        await self.mqtt.disconnect()

    # -- outbound ------------------------------------------------------------
    async def _send(self, env: Envelope) -> None:
        await self.mqtt.publish(topics.MASTER_INBOX, env.to_json())

    async def _announce(self) -> None:
        await self._send(Envelope(
            type=MsgType.CAPABILITY_ANNOUNCE, src=self.rec.id, dst="master-0",
            payload=self.rec.to_dict()))

    async def _heartbeat_loop(self) -> None:
        try:
            while self.running:
                st = self.state
                if st and st.alive:
                    await self._send(Envelope(
                        type=MsgType.HEARTBEAT, src=self.rec.id, dst="master-0",
                        payload={"battery_pct": round(st.battery_pct(), 1),
                                 "status": st.status, "link_ok": st.link_ok}))
                await asyncio.sleep(HEARTBEAT_S)
        except asyncio.CancelledError:
            pass

    async def _telemetry_loop(self) -> None:
        try:
            while self.running:
                st = self.state
                if st:
                    # a drone with no radio link cannot be heard -- the physics
                    # decides this, not a coin flip
                    if st.link_ok:
                        await self.mqtt.publish(
                            topics.drone_telemetry(self.rec.id),
                            Envelope(type=MsgType.TELEMETRY, src=self.rec.id, dst="master-0",
                                     payload={"x": round(st.x, 1), "y": round(st.y, 1),
                                              "heading_deg": round(math.degrees(st.heading), 1),
                                              "speed_ms": round(st.speed, 2),
                                              "battery_pct": round(st.battery_pct(), 1),
                                              "link_dbm": round(st.link_dbm, 1),
                                              "link_via": st.link_via}).to_json())
                await asyncio.sleep(TELEMETRY_S)
        except asyncio.CancelledError:
            pass

    # -- inbound -------------------------------------------------------------
    async def _on_msg(self, topic: str, payload: bytes) -> None:
        env = Envelope.from_json(payload)
        if env.dst not in (self.rec.id, BROADCAST):
            return
        st = self.state
        if st and not st.alive and env.type != MsgType.CAPABILITY_QUERY:
            return  # dead drones stay silent; the master must notice by timeout

        if env.type == MsgType.CAPABILITY_QUERY:
            if st and st.alive:
                await self._announce()

        elif env.type == MsgType.TASK_ASSIGN:
            await self._accept(env)

        elif env.type == MsgType.ABORT:
            if self._task:
                self._task.cancel()
            if st:
                st.status = "ABORTED"
                st.current_task = None
                st.waypoints.clear()

    async def _accept(self, env: Envelope) -> None:
        st = self.state
        verb = env.payload.get("verb", "")
        task_id = env.payload.get("task_id", env.corr_id or "")

        # a drone rejects on its own terms -- the master does not decide for it
        if not st or not st.alive:
            return
        if not self.rec.has(verb):
            await self._send(Envelope(type=MsgType.TASK_REJECT, src=self.rec.id, dst="master-0",
                                      corr_id=task_id,
                                      payload={"task_id": task_id, "reason": "capability not held"}))
            return
        if st.battery_pct() < 12:
            await self._send(Envelope(type=MsgType.TASK_REJECT, src=self.rec.id, dst="master-0",
                                      corr_id=task_id,
                                      payload={"task_id": task_id, "reason": "battery below reserve"}))
            return

        await self._send(Envelope(type=MsgType.TASK_ACK, src=self.rec.id, dst="master-0",
                                  corr_id=task_id, payload={"task_id": task_id, "verb": verb}))

        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._execute(env))

    # -- execution -----------------------------------------------------------
    def _route_for(self, verb: str, params: dict) -> list[Waypoint]:
        region = params.get("region") or {}
        w = self.world

        if verb == "area_search" and region:
            return w.lawnmower(self.rec, region)

        if verb == "relay_comms":
            # park midway between base and the region: that is what makes the
            # link budget close
            if region:
                cx = region["x"] + region["w"] / 2
                cy = region["y"] + region["h"] / 2
            else:
                cx, cy = w.size_m / 2, w.size_m / 2
            return [Waypoint((w.base[0] + cx) / 2, (w.base[1] + cy) / 2, hold_s=2.0, label="relay station")]

        # everything else flies to a specific contact if one is known
        target = None
        for ct in w.contacts:
            if verb.startswith("classify") and ct["found"] and not ct["classified"]:
                target = ct
                break
            if verb in ("deliver_payload", "intercept", "guide_ground_team",
                        "measure_defect", "file_report", "assess") \
                    and ct["classified"] and not ct["served"] \
                    and self._actionable(verb, ct):
                target = ct
                break
        if target:
            return [Waypoint(target["x"], target["y"], hold_s=3.0, label=target["id"])]
        if region:
            return [Waypoint(region["x"] + region["w"] / 2, region["y"] + region["h"] / 2, hold_s=3.0)]
        return [Waypoint(w.size_m / 2, w.size_m / 2, hold_s=3.0)]

    async def _execute(self, env: Envelope) -> None:
        st = self.state
        verb = env.payload["verb"]
        task_id = env.payload.get("task_id", "")
        params = env.payload.get("params") or {}
        est = float(env.payload.get("est_duration_s", 30.0))

        route = self._route_for(verb, params)
        total_wps = max(1, len(route))
        self.world.assign_route(self.rec.id, route, task_id, verb)

        started = time.time()
        last_report = 0.0
        try:
            while self.running:
                await asyncio.sleep(0.25)
                st = self.state
                if not st or not st.alive:
                    return  # silence. the master will time us out.

                if verb == "area_search":
                    progress = 1.0 - (len(st.waypoints) / total_wps)
                else:
                    progress = min(1.0, (time.time() - started) / max(1.0, est * 0.25))
                st.task_progress = max(0.0, min(1.0, progress))

                if time.time() - last_report > 1.2:
                    last_report = time.time()
                    if st.link_ok:
                        await self._send(Envelope(
                            type=MsgType.TASK_PROGRESS, src=self.rec.id, dst="master-0",
                            corr_id=task_id,
                            payload={"task_id": task_id, "progress": round(st.task_progress, 3)}))

                done = (not st.waypoints and st.hold_timer <= 0) or st.task_progress >= 1.0
                if done:
                    break

            st = self.state
            if not st or not st.alive:
                return
            st.task_progress = 1.0
            st.status = "ON_STATION"
            result = self._result_for(verb)
            st.current_task = None
            st.current_verb = ""
            await self._send(Envelope(
                type=MsgType.TASK_COMPLETE, src=self.rec.id, dst="master-0",
                corr_id=task_id, payload={"task_id": task_id, "verb": verb, "result": result}))
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _actionable(verb: str, contact: dict) -> bool:
        """A classification is only useful if it can say no. Identifying a
        contact as friendly must stop an intercept, or the interlock is
        theatre -- it would gate on timing and never on the answer."""
        if verb == "intercept":
            return contact.get("kind") != "friendly"
        if verb in ("deliver_payload", "guide_ground_team"):
            return contact.get("kind") in ("survivor", "friendly", "unknown")
        return True

    def _result_for(self, verb: str) -> dict:
        """Produce the token this verb is contracted to produce."""
        w = self.world
        if verb == "area_search":
            found = [c["id"] for c in w.contacts if c["found"]]
            return {"contacts": found, "count": len(found)}
        if verb.startswith("classify"):
            for ct in w.contacts:
                if ct["found"] and not ct["classified"]:
                    # reveal the ground truth seeded with the contact. This is
                    # the whole point of a classify step: before it runs nobody
                    # knows whether that heat signature is a survivor or a goat.
                    ct["classified"] = True
                    ct["kind"] = ct.get("truth") or "survivor"
                    return {"contact": ct["id"], "kind": ct["kind"],
                            "issued_at": time.time(), "ttl_s": 600}
            return {"contact": None}
        if verb in ("deliver_payload", "intercept", "guide_ground_team", "file_report"):
            for ct in w.contacts:
                if ct["classified"] and not ct["served"] and self._actionable(verb, ct):
                    ct["served"] = True
                    return {"contact": ct["id"], "action": verb}
            return {"contact": None}
        return {"ok": True}
