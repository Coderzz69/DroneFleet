"""The master. Discovers a fleet, decides whether a mission is possible, and
if it is, runs it -- re-checking feasibility whenever the fleet changes.

It holds no special powers: it talks to drones over the same MQTT bus anyone
else could subscribe to.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

from ..core.messages import Envelope, MsgType
from ..core.nlp import parse_mission
from ..core.ontology import Pack, load_pack
from ..core.planner import (DEGRADED, FEASIBLE, INSUFFICIENT, Gap, MissionSpec,
                            Plan, Planner)
from ..core.protocol import record_from_announcement
from ..core.registry import DroneRecord, Registry
from ..net import topics
from ..net.mqtt_client import MQTTClient
from ..sim.world import World

HEARTBEAT_TIMEOUT_S = 5.0
STALE_S = 4.0            # no word for this long -> the master's belief is stale
# Marks a task the dispatcher refused on the authorization boundary. A replan
# must recognise it and leave it alone -- no fleet change can clear it.
AUTH_HOLD = "human authorization required"


@dataclass
class DroneView:
    """What the MASTER believes about one drone.

    This is deliberately separate from `world.DroneState`, which is ground
    truth the simulator owns. The master only knows what arrived over the
    radio, so these two diverge exactly when a real ground station would be
    flying blind -- link lost, drone dead, telemetry dropped. Showing that
    divergence is the point; a control station that reads the simulator is
    lying about what it knows.
    """
    id: str
    name: str = ""
    # tasking -- the master's own record of what it ordered
    task_id: Optional[str] = None
    verb: str = ""
    phase: str = "IDLE"          # IDLE|ASSIGNED|ACKED|WORKING|DONE|REJECTED|LOST
    progress: float = 0.0
    activity: str = ""           # TRANSIT | WORKING | ON_STATION -- as reported
    reports: int = 0             # how many progress reports have arrived
    assigned_at: float = 0.0
    last_result: dict = field(default_factory=dict)
    completed: list[str] = field(default_factory=list)
    # last reported state -- only ever written from a received message
    battery_pct: Optional[float] = None
    x: Optional[float] = None
    y: Optional[float] = None
    heading_deg: Optional[float] = None
    speed_ms: Optional[float] = None
    target_x: Optional[float] = None
    target_y: Optional[float] = None
    airborne: bool = False
    link_ok: bool = True
    link_via: str = ""
    last_heard: float = 0.0
    last_telemetry: float = 0.0
    reason: str = ""             # why it rejected / how it was lost

    def stale(self, now: float) -> bool:
        return self.last_heard > 0 and (now - self.last_heard) > STALE_S

    def to_dict(self, now: float) -> dict:
        d = asdict(self)
        d["stale"] = self.stale(now)
        d["silent_for_s"] = round(now - self.last_heard, 1) if self.last_heard else None
        return d


class Master:
    def __init__(self, world: World, pack: Pack, host: str, port: int) -> None:
        self.world = world
        self.pack = pack
        self.registry = Registry()
        self.mqtt = MQTTClient("master-0", host, port)
        self.plan: Optional[Plan] = None
        self.spec: Optional[MissionSpec] = None
        self.mission_id: Optional[str] = None
        self.mission_active = False
        self.last_seen: dict[str, float] = {}
        self.views: dict[str, DroneView] = {}   # the master's belief about the fleet
        self._loops: list[asyncio.Task] = []
        self._tokens: dict[str, dict] = {}   # token name -> {issued_at, ttl_s, by}
        self._plan_dirty = False

    async def start(self) -> None:
        await self.mqtt.connect()
        await self.mqtt.subscribe(topics.MASTER_INBOX, self._on_msg)
        # the master must hear telemetry to know where anything is -- without
        # this it is tracking tasks it cannot see the execution of
        await self.mqtt.subscribe(topics.ALL_TELEMETRY, self._on_telemetry)
        self._loops.append(asyncio.create_task(self._watchdog()))
        self._loops.append(asyncio.create_task(self._plan_pump()))

    async def stop(self) -> None:
        for t in self._loops:
            t.cancel()
        await self.mqtt.disconnect()

    # -- console -------------------------------------------------------------
    async def console(self, kind: str, text: str, **extra) -> None:
        await self.mqtt.publish(topics.CONSOLE, json.dumps(
            {"kind": kind, "text": text, "ts": time.time(), **extra}).encode())

    def mark_plan_dirty(self) -> None:
        """Ask for a plan push soon, without doing one per message.

        A remote vehicle's position only reaches the browser through this
        channel, so it has to be republished as telemetry arrives -- but at
        2 Hz per vehicle, doing it inline means serialising the whole fleet
        snapshot once per report. `_plan_pump` coalesces them.
        """
        self._plan_dirty = True

    async def _plan_pump(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.25)
                if self._plan_dirty:
                    await self.push_plan()
        except asyncio.CancelledError:
            pass

    async def push_plan(self) -> None:
        self._plan_dirty = False
        data = self.plan.to_dict() if self.plan else {"verdict": None, "tasks": [], "gaps": []}
        data["fleet"] = [d.to_dict() for d in self.registry.all()]
        data["domain"] = self.pack.domain
        data["mission_active"] = self.mission_active
        data["mission_id"] = self.mission_id
        now = time.time()
        data["views"] = [self.views[d.id].to_dict(now)
                         for d in self.registry.all() if d.id in self.views]
        await self.mqtt.publish(topics.PLAN_UPDATE, json.dumps(data).encode(), retain=True)

    # -- discovery -----------------------------------------------------------
    async def discover(self) -> None:
        await self.mqtt.publish(topics.BROADCAST, Envelope(
            type=MsgType.CAPABILITY_QUERY, src="master-0").to_json())

    def view(self, drone_id: str) -> DroneView:
        v = self.views.get(drone_id)
        if v is None:
            rec = self.registry.get(drone_id)
            v = DroneView(id=drone_id, name=rec.name if rec else drone_id)
            self.views[drone_id] = v
        return v

    async def _on_telemetry(self, topic: str, payload: bytes) -> None:
        """Position reports. This is the only way the master learns where
        anything is -- a drone out of radio range simply stops appearing."""
        env = Envelope.from_json(payload)
        now = time.time()
        self.last_seen[env.src] = now
        v = self.view(env.src)
        p = env.payload
        v.x, v.y = p.get("x"), p.get("y")
        v.heading_deg = p.get("heading_deg")
        v.speed_ms = p.get("speed_ms")
        v.target_x = p.get("target_x", v.target_x)
        v.target_y = p.get("target_y", v.target_y)
        v.airborne = bool(p.get("airborne", v.airborne))
        v.battery_pct = p.get("battery_pct", v.battery_pct)
        v.link_via = p.get("link_via", v.link_via)
        v.last_telemetry = now
        v.last_heard = now
        if (rec := self.registry.get(env.src)) and not rec.online:
            rec.online = True
        # An independently launched vehicle has no simulator body, so the world
        # tick cannot carry its position. The plan channel is the only route
        # its telemetry has to the browser.
        if env.src not in self.world.drones:
            self.mark_plan_dirty()

    async def _on_msg(self, topic: str, payload: bytes) -> None:
        env = Envelope.from_json(payload)
        self.last_seen[env.src] = time.time()
        if env.src != "master-0":
            self.view(env.src).last_heard = time.time()

        if env.type == MsgType.CAPABILITY_ANNOUNCE:
            await self._on_announce(env)

        elif env.type == MsgType.TASK_ACK:
            v = self.view(env.src)
            v.phase, v.progress, v.reason = "WORKING", 0.0, ""
            v.activity, v.reports = "", 0
            self._set_state(env.payload.get("task_id"), "ACKED")
            await self.console("wire", f"{env.src} ACK {env.payload.get('task_id')}",
                               src=env.src, type=env.type)
            self._set_state(env.payload.get("task_id"), "RUNNING")
            await self.push_plan()

        elif env.type == MsgType.TASK_REJECT:
            tid = env.payload.get("task_id")
            v = self.view(env.src)
            v.phase, v.task_id, v.verb = "REJECTED", None, ""
            v.reason = env.payload.get("reason", "")
            self._set_state(tid, "FAILED")
            await self.console("reject", f"{env.src} REJECTED {tid}: {env.payload.get('reason')}",
                               src=env.src, type=env.type)
            await self._replan_live(f"{env.src} rejected {tid}")

        elif env.type == MsgType.TASK_PROGRESS:
            pr = float(env.payload.get("progress", 0.0))
            task = self._task(env.payload.get("task_id"))
            if task:
                task.progress = pr
            v = self.view(env.src)
            v.progress, v.phase = pr, "WORKING"
            v.activity = env.payload.get("activity", v.activity)
            v.reports += 1

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
            v = self.view(env.src)
            v.completed.append(tid)
            v.last_result = env.payload.get("result") or {}
            v.phase, v.progress, v.activity = "DONE", 1.0, ""
            v.task_id, v.verb = None, ""
            await self.console("done", f"{env.src} COMPLETED {tid} "
                                       f"({env.payload.get('verb')})", src=env.src, type=env.type)
            await self.push_plan()
            await self._dispatch_ready()

        elif env.type == MsgType.HEARTBEAT:
            v = self.view(env.src)
            v.battery_pct = env.payload.get("battery_pct", v.battery_pct)
            v.link_ok = bool(env.payload.get("link_ok", True))

        elif env.type == MsgType.ALERT:
            await self.console("alert", f"{env.src}: {env.payload.get('text','alert')}", src=env.src)

    async def _on_announce(self, env: Envelope) -> None:
        """A vehicle introduced itself.

        This is the only way a fleet is ever built. An in-process simulator
        drone and an independently launched vehicle arrive through exactly the
        same door -- the master has no privileged channel to either.
        """
        try:
            announced = record_from_announcement(env.payload)
        except (TypeError, ValueError) as exc:
            await self.console("error", f"Rejected announcement from {env.src}: {exc}",
                               src=env.src, type=env.type)
            return
        if announced.id != env.src:
            # A vehicle may not introduce someone else. Without this, anything
            # on the bus could overwrite another vehicle's capabilities.
            await self.console("error", f"Rejected announcement from {env.src}: "
                                        f"manifest claims id {announced.id!r}",
                               src=env.src, type=env.type)
            return

        existing = self.registry.get(announced.id)
        rejoined = existing is not None and not existing.online
        capabilities_changed = existing is not None and existing.verbs() != announced.verbs()
        if existing is not None:
            # Update in place: the simulator's world object and the runtime's
            # agent both hold this record by identity.
            existing.capabilities = announced.capabilities
            existing.sensors = announced.sensors
            existing.constraints = announced.constraints
            existing.withheld = announced.withheld
            if announced.metadata:
                existing.metadata = announced.metadata
            if announced.source_text:
                existing.source_text = announced.source_text
            existing.online = True
            rec = existing
        else:
            rec = self.registry.add(announced)

        self.last_seen[rec.id] = time.time()
        v = self.view(rec.id)
        v.name = rec.name
        if v.phase == "LOST":
            v.phase, v.reason = "IDLE", ""

        await self.console("wire", f"{env.src} announced "
                                   f"[{', '.join(sorted(rec.verbs())) or 'nothing usable'}]",
                           src=env.src, type=env.type)
        if rec.remote and (existing is None or rejoined or capabilities_changed):
            await self.console("register", f"{rec.id} online — {rec.name} "
                                           f"[{', '.join(sorted(rec.verbs())) or 'no usable capability'}] · "
                                           f"{'/'.join(rec.sensors) or 'no sensors'}",
                               src=rec.id, type=env.type)
            for why in rec.withheld:
                await self.console("warn", f"  {rec.name}: {why}", src=rec.id)

        if existing is None or rejoined or capabilities_changed:
            await self.fleet_changed(f"{rec.name} joined the fleet")
        else:
            self.mark_plan_dirty()

    # -- planning ------------------------------------------------------------
    def register(self, rec: DroneRecord) -> None:
        rec.online = True
        self.registry.add(rec)

    async def fleet_changed(self, reason: str) -> None:
        """The fleet gained or lost a vehicle. Re-check the mission against it.

        Feasibility is a statement about a specific fleet, so it stops being
        true the moment the fleet changes -- whether that is a loss mid-flight
        or an extra aircraft booting up in another terminal.
        """
        if self.spec:
            await self._replan_live(reason)
        else:
            await self.push_plan()

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
        # A fresh mission id: task ids restart at T1 every plan, so without
        # this a vehicle could not tell a repeat of T1 from the one it already
        # completed, and would dedupe away real work.
        self.mission_id = f"mission-{uuid.uuid4().hex[:12]}"
        self._tokens.clear()
        for v in self.views.values():          # last mission's beliefs are not this one's
            v.phase, v.task_id, v.verb = "IDLE", None, ""
            v.progress, v.reason, v.completed = 0.0, "", []
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

    def _authorization_required(self, task) -> bool:
        """True for a manifest-declared capability that a human must approve.

        `DroneManifest.to_record` already keeps such capabilities away from the
        planner, so in practice nothing binds to one. This is the second gate:
        a vehicle that re-announces mid-mission, or a record built some other
        way, must not slip an unapproved action through the dispatcher. There
        is deliberately no code path anywhere that issues a grant.
        """
        rec = self.registry.get(task.assignee) if task.assignee else None
        if not rec or not rec.metadata:
            return False
        for item in rec.metadata.get("capabilities") or []:
            if not isinstance(item, dict):
                continue
            if item.get("name") == task.verb and item.get("requires_human_authorization"):
                return True
        return False

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
            if self._authorization_required(task):
                # Fail it rather than hold it forever. A task silently stuck in
                # PENDING looks like a bug; a failed one with a reason is a
                # decision the operator can see and act on.
                task.state = "FAILED"
                task.note = f"{AUTH_HOLD}: not dispatched autonomously"
                await self.console("approval",
                                   f"{task.id} ({task.verb}) needs explicit human "
                                   f"authorization — not sent",
                                   src="master-0", type=MsgType.AUTHORIZATION_REQUEST)
                continue
            ok, why = self._interlock_ok(task)
            if not ok:
                task.note = f"interlock held: {why}"
                continue

            task.state = "ASSIGNED"
            await self.mqtt.publish(topics.drone_inbox(task.assignee), Envelope(
                type=MsgType.TASK_ASSIGN, src="master-0", dst=task.assignee,
                corr_id=task.id, mission_id=self.mission_id, requires_ack=True,
                payload={"task_id": task.id, "verb": task.verb, "params": task.params,
                         "est_duration_s": task.est_duration_s}).to_json())
            v = self.view(task.assignee)
            v.name = task.assignee_name or v.name
            v.task_id, v.verb = task.id, task.verb
            v.phase, v.progress = "ASSIGNED", 0.0
            v.assigned_at = time.time()
            await self.console("assign", f"→ {task.assignee_name} : {task.verb} ({task.id})",
                               src="master-0", type=MsgType.TASK_ASSIGN)

            # tell the drone what comes next, purely so the UI can show it
            follow = next((t for t in self.plan.tasks
                           if task.id in t.depends_on and t.assignee == task.assignee), None)
            st = self.world.drones.get(task.assignee)
            if st:
                st.next_task = follow.id if follow else None
                st.next_verb = follow.verb if follow else ""

        # Anyone with no remaining work is explicitly sent home. Leaving a
        # drone on station because nobody told it otherwise is how you lose an
        # airframe to a flat battery.
        await self._release_idle()

        if all(t.state in ("DONE", "ABORTED", "FAILED") for t in self.plan.tasks):
            if self.mission_active:
                self.mission_active = False
                await self.console("mission", "Mission complete.")
                await self._release_idle(force=True)
                await self.push_plan()

    async def _release_idle(self, force: bool = False) -> None:
        """Recall every drone the plan has no further use for."""
        if not self.plan:
            return
        pending = {t.assignee for t in self.plan.tasks
                   if t.state in ("PENDING", "ASSIGNED", "ACKED", "RUNNING")}
        for rec in self.registry.all():
            v = self.views.get(rec.id)
            if not v or v.phase in ("IDLE", "RECALLED", "LOST"):
                continue
            if v.task_id:                      # still working on something
                continue
            if rec.id in pending and not force:  # still owed a later task
                continue
            v.phase = "RECALLED"
            await self.mqtt.publish(topics.drone_inbox(rec.id), Envelope(
                type=MsgType.RECALL, src="master-0", dst=rec.id,
                payload={"reason": "no further tasking"}).to_json())
            await self.console("recall", f"← {v.name or rec.id} : nothing further, return to base",
                               src="master-0", type=MsgType.RECALL)

    # -- live re-validation --------------------------------------------------
    def _is_alive(self, rec: DroneRecord, now: float) -> bool:
        """Is this vehicle still flyable?

        Two different questions depending on where it lives. A simulator drone
        has a body, so the physics answers. An independently launched vehicle
        has none here, so the only honest answer is whether it is still talking
        -- which is exactly what a real ground station has to go on.
        """
        st = self.world.drones.get(rec.id)
        if st is not None:
            return st.alive
        last = self.last_seen.get(rec.id, 0.0)
        return rec.online and last > 0 and (now - last) <= HEARTBEAT_TIMEOUT_S

    async def _replan_live(self, why: str) -> None:
        """The fleet changed mid-mission. Re-run the SAME validator on what is
        left -- this is what makes FEASIBLE -> DEGRADED -> INSUFFICIENT happen
        live rather than only at planning time."""
        if not self.spec:
            return
        now = time.time()
        surviving = Registry()
        for d in self.registry.all():
            if self._is_alive(d, now):
                surviving.add(d)

        verdict_before = self.plan.verdict if self.plan else None
        fresh = Planner(self.pack, surviving).plan(self.spec)

        if self.plan:
            # Adopt the fresh graph wholesale, so a step that only became
            # possible now (a supplier verb the old fleet had no provider for)
            # actually appears, then carry the old execution state across.
            #
            # Carry-over is keyed by VERB, not by task id. Ids are positional
            # -- `T2` is simply the second link of the chain -- and the chain
            # itself is re-derived from the surviving fleet, so `T2` can be a
            # different verb than it was a moment ago. Keying on the id would
            # mark an untouched task DONE. Each verb appears at most once per
            # plan, which is what makes it a safe key.
            previous_by_verb = {t.verb: t for t in self.plan.tasks}
            for task in fresh.tasks:
                previous = previous_by_verb.get(task.verb)
                if previous is None:
                    continue
                task.progress = previous.progress
                if previous.state == "DONE":
                    task.state, task.progress = "DONE", 1.0
                    task.assignee = previous.assignee
                    task.assignee_name = previous.assignee_name
                elif previous.state in ("ASSIGNED", "ACKED", "RUNNING"):
                    holder = self.registry.get(previous.assignee) if previous.assignee else None
                    if holder is not None and self._is_alive(holder, now):
                        # Still in flight. Leave it alone -- rebinding a live
                        # task sends a second drone to redo it.
                        task.state = previous.state
                        task.assignee = previous.assignee
                        task.assignee_name = previous.assignee_name
                    else:
                        # The execution went down with the vehicle. The fresh
                        # binding has already picked a survivor for it.
                        task.state, task.progress = "PENDING", 0.0
                elif previous.state == "ABORTED":
                    task.state = "ABORTED"
                elif previous.state == "FAILED":
                    if previous.note.startswith(AUTH_HOLD):
                        # Not a fleet problem, and no replan can fix it. Only a
                        # human grant can, and nothing here issues one.
                        task.state, task.note = "FAILED", previous.note
                    else:
                        task.state, task.progress = "PENDING", 0.0
            self.plan.tasks = fresh.tasks
            self.plan.verdict = fresh.verdict
            self.plan.gaps = fresh.gaps
            self.plan.notes = fresh.notes

        if verdict_before != fresh.verdict:
            await self.console("verdict", f"Re-validated after {why}: "
                                          f"{verdict_before} → {fresh.verdict}",
                               verdict=fresh.verdict)
            for g in fresh.gaps:
                await self.console("gap", f"{g.reason}: {g.why}", severity=g.severity)
        await self.push_plan()
        await self._dispatch_ready()

    async def _watchdog(self) -> None:
        """Three missed heartbeats and a vehicle is presumed lost.

        This runs whether or not a mission is flying: a fleet that quietly
        shrank between missions would otherwise produce a confident FEASIBLE
        verdict for aircraft that are no longer there.
        """
        try:
            while True:
                await asyncio.sleep(1.0)
                now = time.time()
                for rec in self.registry.all():
                    if not rec.online:
                        continue          # already mourned; `online` is the latch
                    st = self.world.drones.get(rec.id)
                    last = self.last_seen.get(rec.id, 0.0)
                    # Never heard from and no body to inspect: it has not
                    # arrived yet, which is not the same as having gone.
                    if st is None and last == 0.0:
                        continue
                    silent = last > 0.0 and (now - last) > HEARTBEAT_TIMEOUT_S
                    dead = st is not None and not st.alive
                    if not (dead or silent):
                        continue

                    rec.online = False
                    v = self.view(rec.id)
                    v.phase, v.reason = "LOST", "no heartbeat"
                    v.task_id, v.verb = None, ""
                    why = st.status.lower().replace("_", " ") if st is not None \
                        else "link silent"
                    await self.console("lost", f"{rec.name} ({rec.id}) is not responding "
                                               f"— {why}")
                    if self.plan and self.mission_active:
                        for t in self.plan.tasks:
                            if t.assignee == rec.id and t.state in ("ASSIGNED", "ACKED", "RUNNING"):
                                t.state = "FAILED"
                    await self.fleet_changed(f"loss of {rec.name}")
        except asyncio.CancelledError:
            pass
