"""The master. Discovers a fleet, decides whether a mission is possible, and
if it is, runs it -- re-checking feasibility whenever the fleet changes.

It holds no special powers: it talks to drones over the same MQTT bus anyone
else could subscribe to.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

from ..core.messages import Envelope, MsgType
from ..core.nlp import parse_mission
from ..core.ontology import Pack, load_pack
from ..core.planner import (DEGRADED, FEASIBLE, INSUFFICIENT, Gap, MissionSpec,
                            Plan, Planner)
from ..core.registry import DroneRecord, Registry
from ..net import topics
from ..net.mqtt_client import MQTTClient
from ..sim.world import World

HEARTBEAT_TIMEOUT_S = 5.0


class Master:
    def __init__(self, world: World, pack: Pack, host: str, port: int) -> None:
        self.world = world
        self.pack = pack
        self.registry = Registry()
        self.mqtt = MQTTClient("master-0", host, port)
        self.plan: Optional[Plan] = None
        self.spec: Optional[MissionSpec] = None
        self.mission_active = False
        self.last_seen: dict[str, float] = {}
        self._loops: list[asyncio.Task] = []
        self._tokens: dict[str, dict] = {}   # token name -> {issued_at, ttl_s, by}

    async def start(self) -> None:
        await self.mqtt.connect()
        await self.mqtt.subscribe(topics.MASTER_INBOX, self._on_msg)
        self._loops.append(asyncio.create_task(self._watchdog()))

    async def stop(self) -> None:
        for t in self._loops:
            t.cancel()
        await self.mqtt.disconnect()

    # -- console -------------------------------------------------------------
    async def console(self, kind: str, text: str, **extra) -> None:
        await self.mqtt.publish(topics.CONSOLE, json.dumps(
            {"kind": kind, "text": text, "ts": time.time(), **extra}).encode())

    async def push_plan(self) -> None:
        data = self.plan.to_dict() if self.plan else {"verdict": None, "tasks": [], "gaps": []}
        data["fleet"] = [d.to_dict() for d in self.registry.all()]
        data["domain"] = self.pack.domain
        data["mission_active"] = self.mission_active
        await self.mqtt.publish(topics.PLAN_UPDATE, json.dumps(data).encode(), retain=True)

    # -- discovery -----------------------------------------------------------
    async def discover(self) -> None:
        await self.mqtt.publish(topics.BROADCAST, Envelope(
            type=MsgType.CAPABILITY_QUERY, src="master-0").to_json())

    async def _on_msg(self, topic: str, payload: bytes) -> None:
        env = Envelope.from_json(payload)
        self.last_seen[env.src] = time.time()

        if env.type == MsgType.CAPABILITY_ANNOUNCE:
            await self.console("wire", f"{env.src} announced "
                                       f"[{', '.join(c['verb'] for c in env.payload.get('capabilities', []))}]",
                               src=env.src, type=env.type)

        elif env.type == MsgType.TASK_ACK:
            self._set_state(env.payload.get("task_id"), "ACKED")
            await self.console("wire", f"{env.src} ACK {env.payload.get('task_id')}",
                               src=env.src, type=env.type)
            self._set_state(env.payload.get("task_id"), "RUNNING")
            await self.push_plan()

        elif env.type == MsgType.TASK_REJECT:
            tid = env.payload.get("task_id")
            self._set_state(tid, "FAILED")
            await self.console("reject", f"{env.src} REJECTED {tid}: {env.payload.get('reason')}",
                               src=env.src, type=env.type)
            await self._replan_live(f"{env.src} rejected {tid}")

        elif env.type == MsgType.TASK_PROGRESS:
            task = self._task(env.payload.get("task_id"))
            if task:
                task.progress = float(env.payload.get("progress", 0.0))

        elif env.type == MsgType.TASK_COMPLETE:
            tid = env.payload.get("task_id")
            task = self._task(tid)
            self._set_state(tid, "DONE")
            if task:
                task.progress = 1.0
                # record the tokens this task produced, with their expiry
                for token in task.produces:
                    result = env.payload.get("result") or {}
                    self._tokens[token] = {
                        "issued_at": result.get("issued_at", time.time()),
                        "ttl_s": result.get("ttl_s", 600),
                        "by": env.src,
                    }
            await self.console("done", f"{env.src} COMPLETED {tid} "
                                       f"({env.payload.get('verb')})", src=env.src, type=env.type)
            await self.push_plan()
            await self._dispatch_ready()

        elif env.type == MsgType.ALERT:
            await self.console("alert", f"{env.src}: {env.payload.get('text','alert')}", src=env.src)

    # -- planning ------------------------------------------------------------
    def register(self, rec: DroneRecord) -> None:
        self.registry.add(rec)

    def set_pack(self, name: str) -> None:
        self.pack = load_pack(name)

    def evaluate(self, prompt: str) -> Plan:
        """Propose + validate. Never executes anything."""
        self.spec = parse_mission(prompt, self.pack, self.world.size_m)
        planner = Planner(self.pack, self.registry)
        self.plan = planner.plan(self.spec)
        # show the operator the environment they are being sent into, as soon
        # as the mission is understood -- before launch, alongside the region box
        self.world.set_hazard(self.spec.hazard, self.spec.region)
        return self.plan

    async def launch(self) -> bool:
        if not self.plan or self.plan.verdict == INSUFFICIENT:
            return False
        self.mission_active = True
        self._tokens.clear()
        if self.spec and self.spec.region:
            self.world.seed_contacts(self.spec.region, n=3, domain=self.pack.domain)
            self.world.coverage.clear()
        for t in self.plan.tasks:
            t.state = "PENDING"
            t.progress = 0.0
        await self.console("mission", f"Mission launched: {self.plan.goal} "
                                      f"[{self.plan.verdict}] {len(self.plan.tasks)} tasks")
        await self._dispatch_ready()
        await self.push_plan()
        return True

    async def abort(self) -> None:
        self.mission_active = False
        await self.mqtt.publish(topics.BROADCAST, Envelope(
            type=MsgType.ABORT, src="master-0").to_json())
        if self.plan:
            for t in self.plan.tasks:
                if t.state in ("ASSIGNED", "ACKED", "RUNNING"):
                    t.state = "ABORTED"
        await self.console("mission", "ABORT broadcast to all drones")
        await self.push_plan()

    def _task(self, tid):
        if not self.plan or not tid:
            return None
        return next((t for t in self.plan.tasks if t.id == tid), None)

    def _set_state(self, tid, state: str) -> None:
        t = self._task(tid)
        if t and t.state not in ("DONE", "ABORTED"):
            t.state = state

    def _interlock_ok(self, task) -> tuple[bool, str]:
        """Enforce the pack's policies at DISPATCH time, not just at plan time.
        A clearance that expired while the courier was in transit blocks it."""
        for policy in self.pack.policies:
            if policy.applies_to != task.verb or policy.kind != "fresh":
                continue
            tok = self._tokens.get(policy.token)
            if not tok:
                return False, f"missing '{policy.token}'"
            age = time.time() - tok["issued_at"]
            if age > policy.ttl_s:
                return False, f"'{policy.token}' is stale ({age:.0f}s > {policy.ttl_s:.0f}s)"
        return True, ""

    async def _dispatch_ready(self) -> None:
        """Send every task whose dependencies are DONE and whose interlocks hold."""
        if not self.plan or not self.mission_active:
            return
        by_id = {t.id: t for t in self.plan.tasks}
        done = {t.id for t in self.plan.tasks if t.state == "DONE"}

        for task in self.plan.tasks:
            if task.state != "PENDING" or not task.assignee:
                continue
            if not all(d in done for d in task.depends_on):
                continue
            ok, why = self._interlock_ok(task)
            if not ok:
                task.note = f"interlock held: {why}"
                continue

            task.state = "ASSIGNED"
            await self.mqtt.publish(topics.drone_inbox(task.assignee), Envelope(
                type=MsgType.TASK_ASSIGN, src="master-0", dst=task.assignee,
                corr_id=task.id, requires_ack=True,
                payload={"task_id": task.id, "verb": task.verb, "params": task.params,
                         "est_duration_s": task.est_duration_s}).to_json())
            await self.console("assign", f"→ {task.assignee_name} : {task.verb} ({task.id})",
                               src="master-0", type=MsgType.TASK_ASSIGN)

            # tell the drone what comes next, purely so the UI can show it
            follow = next((t for t in self.plan.tasks
                           if task.id in t.depends_on and t.assignee == task.assignee), None)
            st = self.world.drones.get(task.assignee)
            if st:
                st.next_task = follow.id if follow else None
                st.next_verb = follow.verb if follow else ""

        if all(t.state in ("DONE", "ABORTED", "FAILED") for t in self.plan.tasks):
            if self.mission_active:
                self.mission_active = False
                await self.console("mission", "Mission complete.")
                await self.push_plan()

    # -- live re-validation --------------------------------------------------
    async def _replan_live(self, why: str) -> None:
        """The fleet changed mid-mission. Re-run the SAME validator on what is
        left -- this is what makes FEASIBLE -> DEGRADED -> INSUFFICIENT happen
        live rather than only at planning time."""
        if not self.spec:
            return
        alive = {d.id for d in self.registry.all()
                 if (st := self.world.drones.get(d.id)) and st.alive}
        surviving = Registry()
        for d in self.registry.all():
            if d.id in alive:
                surviving.add(d)

        verdict_before = self.plan.verdict if self.plan else None
        fresh = Planner(self.pack, surviving).plan(self.spec)

        if self.plan:
            # keep progress already made, adopt the new verdict and gaps
            done_ids = {t.id: t for t in self.plan.tasks if t.state == "DONE"}
            for t in fresh.tasks:
                if t.id in done_ids:
                    t.state, t.progress = "DONE", 1.0
            self.plan.verdict = fresh.verdict
            self.plan.gaps = fresh.gaps
            self.plan.notes = fresh.notes
            for t in self.plan.tasks:
                if t.state in ("PENDING", "ASSIGNED"):
                    match = next((f for f in fresh.tasks if f.id == t.id), None)
                    if match:
                        t.assignee, t.assignee_name = match.assignee, match.assignee_name

        if verdict_before != fresh.verdict:
            await self.console("verdict", f"Re-validated after {why}: "
                                          f"{verdict_before} → {fresh.verdict}",
                               verdict=fresh.verdict)
            for g in fresh.gaps:
                await self.console("gap", f"{g.reason}: {g.why}", severity=g.severity)
        await self.push_plan()
        await self._dispatch_ready()

    async def _watchdog(self) -> None:
        """Three missed heartbeats and a drone is presumed lost."""
        try:
            while True:
                await asyncio.sleep(1.0)
                if not self.mission_active:
                    continue
                now = time.time()
                for rec in self.registry.all():
                    st = self.world.drones.get(rec.id)
                    if not st:
                        continue
                    silent = now - self.last_seen.get(rec.id, now) > HEARTBEAT_TIMEOUT_S
                    if (not st.alive or silent) and not getattr(st, "_mourned", False):
                        st._mourned = True          # type: ignore[attr-defined]
                        await self.console("lost", f"{rec.name} ({rec.id}) is not responding "
                                                   f"— {st.status.lower().replace('_',' ')}")
                        if self.plan:
                            for t in self.plan.tasks:
                                if t.assignee == rec.id and t.state in ("ASSIGNED", "ACKED", "RUNNING"):
                                    t.state = "FAILED"
                        await self._replan_live(f"loss of {rec.name}")
        except asyncio.CancelledError:
            pass
