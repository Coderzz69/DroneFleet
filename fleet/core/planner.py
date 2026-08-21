"""The feasibility engine.

Backward-chains a task DAG from the mission goal, binds each task to a drone
that can actually do it, then runs a DETERMINISTIC validator. No model gets to
decide whether a mission is possible -- it may only propose. This module is
plain code and is fully testable with no AI, no network and no UI.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from .ontology import Pack, Verb
from .registry import Registry, DroneRecord

FEASIBLE = "FEASIBLE"
DEGRADED = "DEGRADED"
INSUFFICIENT = "INSUFFICIENT"


@dataclass
class Task:
    id: str
    verb: str
    assignee: Optional[str] = None
    assignee_name: str = ""
    requires: list[str] = field(default_factory=list)
    produces: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    est_duration_s: float = 0.0
    state: str = "PENDING"
    progress: float = 0.0
    note: str = ""


@dataclass
class Gap:
    reason: str
    needed: str
    why: str
    suggestion: str = ""
    severity: str = "blocking"


@dataclass
class Plan:
    verdict: str
    domain: str
    goal: str
    tasks: list[Task] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "domain": self.domain,
            "goal": self.goal,
            "tasks": [asdict(t) for t in self.tasks],
            "gaps": [asdict(g) for g in self.gaps],
            "notes": self.notes,
        }


@dataclass
class MissionSpec:
    """What the operator asked for, in structured form."""
    goal_verb: str
    region: dict[str, float] = field(default_factory=dict)   # x, y, w, h in metres
    hazard: str = ""            # flood | fire | earthquake | storm | chemical
    raw: str = ""

    def area_km2(self) -> float:
        if not self.region:
            return 1.0
        return max(0.01, (self.region.get("w", 1000) * self.region.get("h", 1000)) / 1_000_000.0)

    def distance_from_base_m(self, base=(0.0, 0.0)) -> float:
        if not self.region:
            return 0.0
        cx = self.region.get("x", 0) + self.region.get("w", 0) / 2
        cy = self.region.get("y", 0) + self.region.get("h", 0) / 2
        return math.hypot(cx - base[0], cy - base[1])


class Planner:
    def __init__(self, pack: Pack, registry: Registry) -> None:
        self.pack = pack
        self.registry = registry

    # -- proposal -----------------------------------------------------------
    def _chain(self, goal_verb: str) -> tuple[list[Verb], list[Gap]]:
        """Backward-chain from the goal, resolving each required token to the
        verb that produces it. A token nobody produces is a provable gap."""
        pack = self.pack
        produces = pack.produces_index()
        ordered: list[Verb] = []
        gaps: list[Gap] = []
        seen: set[str] = set()

        def resolve(verb_name: str, depth: int = 0) -> None:
            if verb_name in seen or depth > 12:
                return
            seen.add(verb_name)
            verb = pack.verbs.get(verb_name)
            if verb is None:
                gaps.append(Gap("UNKNOWN_VERB", verb_name,
                                f"'{verb_name}' is not in the {pack.domain} vocabulary",
                                "Add it to the pack, or rephrase the mission"))
                return
            for token in verb.requires:
                if token == "region":
                    continue  # supplied by the operator, not by another task
                suppliers = produces.get(token, [])
                # prefer a supplier the fleet can actually fly
                flyable = [s for s in suppliers if self.registry.providers_of(s)]
                pick = flyable[0] if flyable else (suppliers[0] if suppliers else None)
                if pick is None:
                    gaps.append(Gap(
                        "MISSING_TOKEN", token,
                        f"'{verb_name}' requires '{token}' and no verb in this domain produces it",
                        f"Extend the {pack.domain} pack with a verb producing '{token}'"))
                    continue
                resolve(pick, depth + 1)
            ordered.append(verb)

        resolve(goal_verb)
        return ordered, gaps

    def plan(self, spec: MissionSpec) -> Plan:
        pack = self.pack
        chain, gaps = self._chain(spec.goal_verb)
        plan = Plan(verdict=FEASIBLE, domain=pack.domain, goal=spec.goal_verb, gaps=list(gaps))

        produced_by: dict[str, str] = {}
        for i, verb in enumerate(chain, start=1):
            task = Task(
                id=f"T{i}",
                verb=verb.name,
                requires=list(verb.requires),
                produces=list(verb.produces),
                params={"region": spec.region} if "region" in verb.requires else {},
            )
            for token in verb.requires:
                if token in produced_by:
                    task.depends_on.append(produced_by[token])
            for token in verb.produces:
                produced_by.setdefault(token, task.id)
            task.est_duration_s = self._estimate(verb, spec)
            plan.tasks.append(task)

        self._bind(plan, spec)
        self._ensure_relay(plan, spec)
        self._validate(plan, spec)
        return plan

    def _ensure_relay(self, plan: Plan, spec: MissionSpec) -> None:
        """If the mission is out of radio range and the fleet owns a relay, task
        it. A support drone nobody assigns is the same as not having one."""
        if "relay_comms" not in self.pack.verbs:
            return
        if any(t.verb == "relay_comms" for t in plan.tasks):
            return

        dist = spec.distance_from_base_m()
        needs_relay = any(
            (d := self.registry.get(t.assignee)) and dist > d.constraints.comms_range_m
            for t in plan.tasks if t.assignee)
        if not needs_relay:
            return

        tasked = {t.assignee for t in plan.tasks if t.assignee}
        providers = self.registry.providers_of("relay_comms")
        # prefer a drone not already busy, and one whose own radio actually reaches
        free = [d for d in providers if d.id not in tasked] or providers
        free.sort(key=lambda d: -d.constraints.comms_range_m)
        if not free:
            return

        pick = free[0]
        verb = self.pack.verbs["relay_comms"]
        relay = Task(
            id="T0", verb="relay_comms", assignee=pick.id, assignee_name=pick.name,
            requires=list(verb.requires), produces=list(verb.produces),
            params={"region": spec.region},
            est_duration_s=0.0,   # holds station for the whole mission
            note="station-keeping for the duration of the mission",
        )
        plan.tasks.insert(0, relay)
        plan.notes.append(f"{pick.name} tasked as an airborne relay — without it the "
                          f"fleet is beyond radio range")

    def _estimate(self, verb: Verb, spec: MissionSpec) -> float:
        if verb.duration_per_km2:
            return verb.fixed_duration_s + verb.duration_per_km2 * 60.0 * spec.area_km2()
        return verb.fixed_duration_s

    # -- binding ------------------------------------------------------------
    def _bind(self, plan: Plan, spec: MissionSpec) -> None:
        """Match each task to a drone by capability, spreading load where possible."""
        load: dict[str, float] = {}
        for task in plan.tasks:
            verb = self.pack.verbs.get(task.verb)
            candidates = self.registry.providers_of(task.verb)

            if verb and verb.sensors_any:
                sensored = [d for d in candidates if set(d.sensors) & set(verb.sensors_any)]
                if sensored:
                    candidates = sensored
                elif candidates:
                    task.note = f"no {'/'.join(verb.sensors_any)} sensor -- degraded quality"

            if not candidates:
                plan.gaps.append(Gap(
                    "MISSING_CAPABILITY", task.verb,
                    f"task {task.id} needs '{task.verb}' and no registered drone provides it",
                    self._suggest(task.verb)))
                continue

            candidates.sort(key=lambda d: load.get(d.id, 0.0))
            pick = candidates[0]
            task.assignee = pick.id
            task.assignee_name = pick.name
            load[pick.id] = load.get(pick.id, 0.0) + task.est_duration_s

    def _suggest(self, verb_name: str) -> str:
        v = self.pack.verbs.get(verb_name)
        if not v:
            return f"Add a drone that can '{verb_name}'"
        bits = f"Add a drone that can '{verb_name}'"
        if v.sensors_any:
            bits += f" with a {' or '.join(v.sensors_any)} sensor"
        return bits

    # -- deterministic validation ------------------------------------------
    def _validate(self, plan: Plan, spec: MissionSpec) -> None:
        by_id = {t.id: t for t in plan.tasks}

        # 1. every dependency exists and produces what the dependant needs
        for task in plan.tasks:
            for dep in task.depends_on:
                if dep not in by_id:
                    plan.gaps.append(Gap("BROKEN_DEPENDENCY", dep,
                                         f"{task.id} depends on {dep}, which is not in the plan"))

        # 2. no cycles
        if self._has_cycle(plan.tasks):
            plan.gaps.append(Gap("CYCLE", "dag", "the task graph contains a dependency cycle",
                                 "This is a planner bug -- the mission was not scheduled"))

        # 3. physical constraints: endurance and comms range
        dist = spec.distance_from_base_m()
        for task in plan.tasks:
            if not task.assignee:
                continue
            drone = self.registry.get(task.assignee)
            if not drone:
                continue
            c = drone.constraints

            transit_s = (dist / max(1.0, c.cruise_ms)) * 2.0
            needed_min = (task.est_duration_s + transit_s) / 60.0
            if needed_min > c.endurance_min:
                plan.gaps.append(Gap(
                    "CONSTRAINT_VIOLATION", "endurance",
                    f"{task.id} ({task.verb}) needs ~{needed_min:.0f} min including transit; "
                    f"{drone.name} has {c.endurance_min:.0f} min",
                    "Add a longer-endurance drone, or shrink the search region"))

            if dist > c.comms_range_m:
                has_relay = any(
                    self.registry.get(t.assignee) and t.verb == "relay_comms"
                    for t in plan.tasks if t.assignee)
                if has_relay:
                    plan.notes.append(
                        f"{drone.name} is beyond direct radio range ({dist/1000:.1f} km > "
                        f"{c.comms_range_m/1000:.1f} km) -- covered by the relay")
                else:
                    plan.gaps.append(Gap(
                        "CONSTRAINT_VIOLATION", "comms_range",
                        f"target is {dist/1000:.1f} km out; {drone.name}'s radio reaches "
                        f"{c.comms_range_m/1000:.1f} km",
                        "Add a drone that can relay_comms, or accept return-on-signal-loss",
                        severity="degraded"))

        # 4. policy interlocks declared by the pack
        for policy in self.pack.policies:
            targets = [t for t in plan.tasks if t.verb == policy.applies_to]
            for task in targets:
                if policy.kind == "fresh":
                    ok = any(policy.token in by_id[d].produces
                             for d in task.depends_on if d in by_id)
                    if not ok:
                        plan.gaps.append(Gap(
                            "INTERLOCK", policy.token,
                            policy.message or
                            f"{task.verb} is locked until a fresh '{policy.token}' token exists",
                            f"Add a drone producing '{policy.token}'",
                            severity=policy.severity))
                    else:
                        task.note = (task.note + " " if task.note else "") + \
                            f"interlocked on {policy.token} (ttl {policy.ttl_s:.0f}s)"
                elif policy.kind == "requires_link":
                    if dist > 0 and not any(t.verb == "relay_comms" and t.assignee for t in plan.tasks):
                        drone = self.registry.get(task.assignee) if task.assignee else None
                        if drone and dist > drone.constraints.comms_range_m:
                            plan.gaps.append(Gap(
                                "INTERLOCK", "comms_link",
                                policy.message or f"{task.verb} needs a live radio link",
                                "Add a relay drone", severity=policy.severity))

        # 5. verdict from the worst gap
        if any(g.severity == "blocking" for g in plan.gaps):
            plan.verdict = INSUFFICIENT
        elif plan.gaps:
            plan.verdict = DEGRADED
        elif plan.notes:
            plan.verdict = FEASIBLE
        else:
            plan.verdict = FEASIBLE

        if plan.verdict != INSUFFICIENT and not plan.tasks:
            plan.verdict = INSUFFICIENT
            plan.gaps.append(Gap("EMPTY_PLAN", "tasks", "nothing to do -- no tasks were produced"))

    @staticmethod
    def _has_cycle(tasks: list[Task]) -> bool:
        graph = {t.id: list(t.depends_on) for t in tasks}
        state: dict[str, int] = {}

        def walk(n: str) -> bool:
            if state.get(n) == 1:
                return True
            if state.get(n) == 2:
                return False
            state[n] = 1
            for m in graph.get(n, []):
                if m in graph and walk(m):
                    return True
            state[n] = 2
            return False

        return any(walk(n) for n in graph)
