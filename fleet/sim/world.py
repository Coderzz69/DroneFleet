"""The world: 2D kinematics with real physics where it changes the outcome.

Modelled properly, because each one can flip a mission's verdict:
  * acceleration- and turn-rate-limited flight (a drone cannot pivot instantly)
  * wind, added as a vector to airspeed -- upwind legs really do take longer
  * a power model (hover draw + parasitic drag ~ v^3) draining a real battery
  * free-space path loss for the radio link, with relay chaining
  * sensor swath from altitude and field of view, painting real coverage

Deliberately NOT modelled: 6-DOF attitude, rotor aerodynamics, terrain. They
cost a lot and change nothing about the protocol, which is the point of this
project.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional

from ..core.registry import DroneRecord

G = 9.81
AIR_DENSITY = 1.225
HOVER_POWER_PER_KG = 165.0     # W/kg, typical small multirotor
PARASITIC_K = 0.0125           # W per (m/s)^3, lumped drag coefficient


@dataclass
class Waypoint:
    x: float
    y: float
    hold_s: float = 0.0
    label: str = ""


@dataclass
class DroneState:
    id: str
    name: str
    x: float
    y: float
    z: float = 120.0
    heading: float = 0.0          # radians, 0 = +x
    speed: float = 0.0            # airspeed, m/s
    battery_wh: float = 100.0
    battery_wh_max: float = 100.0
    alive: bool = True
    waypoints: list[Waypoint] = field(default_factory=list)
    hold_timer: float = 0.0
    current_task: Optional[str] = None
    current_verb: str = ""
    next_task: Optional[str] = None
    next_verb: str = ""
    task_progress: float = 0.0
    status: str = "IDLE"
    link_ok: bool = True
    link_dbm: float = -60.0
    link_via: str = "direct"
    distance_flown_m: float = 0.0

    def battery_pct(self) -> float:
        return max(0.0, min(100.0, 100.0 * self.battery_wh / max(1e-6, self.battery_wh_max)))


class World:
    """Fixed-timestep simulation. Tick it; read `snapshot()`."""

    def __init__(self, size_m: float = 12000.0, base=(600.0, 11400.0), seed: int = 7) -> None:
        self.size_m = size_m
        self.base = base
        self.rng = random.Random(seed)
        self.drones: dict[str, DroneState] = {}
        self.records: dict[str, DroneRecord] = {}
        self.contacts: list[dict] = []
        self.coverage: set[tuple[int, int]] = set()
        self.coverage_cell_m = size_m / 60.0
        self.t = 0.0
        # wind: a steady vector plus slow drift, in m/s
        self.wind_dir = self.rng.uniform(0, 2 * math.pi)
        self.wind_speed = self.rng.uniform(2.0, 6.0)
        # fault injection
        self.packet_loss = 0.0
        self.extra_latency_s = 0.0

    # -- fleet ---------------------------------------------------------------
    def spawn(self, rec: DroneRecord) -> DroneState:
        c = rec.constraints
        # battery capacity implied by the stated endurance at hover
        hover_w = HOVER_POWER_PER_KG * c.mass_kg
        cap_wh = hover_w * (c.endurance_min / 60.0)
        jitter = self.rng.uniform(-180, 180)
        st = DroneState(
            id=rec.id, name=rec.name,
            x=self.base[0] + jitter, y=self.base[1] + jitter,
            z=c.altitude_m,
            heading=self.rng.uniform(0, 2 * math.pi),
            battery_wh=cap_wh, battery_wh_max=cap_wh,
        )
        self.drones[rec.id] = st
        self.records[rec.id] = rec
        return st

    def despawn(self, drone_id: str) -> None:
        self.drones.pop(drone_id, None)
        self.records.pop(drone_id, None)

    def kill(self, drone_id: str) -> bool:
        st = self.drones.get(drone_id)
        if not st or not st.alive:
            return False
        st.alive = False
        st.status = "LOST"
        st.speed = 0.0
        st.waypoints.clear()
        return True

    # -- tasking -------------------------------------------------------------
    def assign_route(self, drone_id: str, waypoints: list[Waypoint], task_id: str, verb: str) -> None:
        st = self.drones.get(drone_id)
        if not st or not st.alive:
            return
        st.waypoints = list(waypoints)
        st.current_task = task_id
        st.current_verb = verb
        st.task_progress = 0.0
        st.status = "TRANSIT"

    def lawnmower(self, rec: DroneRecord, region: dict) -> list[Waypoint]:
        """Boustrophedon search pattern, spaced by the real sensor swath."""
        swath = max(120.0, rec.swath_m() * 0.85)   # 15% sidelap
        x0, y0 = region["x"], region["y"]
        w, h = region["w"], region["h"]
        legs = max(1, int(h / swath))
        pts: list[Waypoint] = []
        for i in range(legs):
            y = y0 + (i + 0.5) * (h / legs)
            if i % 2 == 0:
                pts.append(Waypoint(x0, y, label="leg"))
                pts.append(Waypoint(x0 + w, y, label="leg"))
            else:
                pts.append(Waypoint(x0 + w, y, label="leg"))
                pts.append(Waypoint(x0, y, label="leg"))
        return pts

    def seed_contacts(self, region: dict, n: int = 3) -> None:
        self.contacts = []
        for i in range(n):
            self.contacts.append({
                "id": f"C{i+1}",
                "x": self.rng.uniform(region["x"], region["x"] + region["w"]),
                "y": self.rng.uniform(region["y"], region["y"] + region["h"]),
                "found": False,
                "classified": False,
                "kind": "unknown",
                "served": False,
            })

    # -- physics -------------------------------------------------------------
    def _wind_vector(self) -> tuple[float, float]:
        # slow direction drift so the wind is not perfectly constant
        self.wind_dir += 0.004 * math.sin(self.t * 0.05)
        return (self.wind_speed * math.cos(self.wind_dir),
                self.wind_speed * math.sin(self.wind_dir))

    def _power_w(self, rec: DroneRecord, airspeed: float) -> float:
        """Hover induced power plus parasitic drag. Real enough that flying
        fast into wind visibly costs battery."""
        hover = HOVER_POWER_PER_KG * (rec.constraints.mass_kg + rec.constraints.payload_kg * 0.0)
        parasitic = PARASITIC_K * (airspeed ** 3)
        return hover + parasitic

    def _step_drone(self, st: DroneState, rec: DroneRecord, dt: float) -> list[dict]:
        events: list[dict] = []
        if not st.alive:
            return events
        c = rec.constraints

        if st.hold_timer > 0:
            st.hold_timer -= dt
            st.speed = max(0.0, st.speed - c.accel_ms2 * dt)
        elif st.waypoints:
            wp = st.waypoints[0]
            dx, dy = wp.x - st.x, wp.y - st.y
            dist = math.hypot(dx, dy)

            # capture radius must exceed the distance covered in one tick,
            # or a fast drone steps straight over the waypoint and orbits it
            capture = max(35.0, (st.speed + self.wind_speed) * dt * 1.6)
            if dist < capture:
                st.waypoints.pop(0)
                st.hold_timer = wp.hold_s
                if not st.waypoints:
                    st.status = "ON_STATION"
            else:
                # --- heading control, rate-limited -------------------------
                desired = math.atan2(dy, dx)
                err = (desired - st.heading + math.pi) % (2 * math.pi) - math.pi
                max_turn = math.radians(c.turn_rate_deg_s) * dt
                st.heading += max(-max_turn, min(max_turn, err))
                st.heading %= 2 * math.pi

                # --- speed control, acceleration-limited -------------------
                # slow down in time to stop: v = sqrt(2*a*d)
                v_arrive = math.sqrt(max(0.0, 2 * c.accel_ms2 * max(0.0, dist - capture)))
                # a floor on approach speed: decelerating to exactly zero at the
                # capture ring lets the wind hold the drone just short of the
                # waypoint indefinitely. It must always out-fly the wind.
                v_floor = min(c.cruise_ms, self.wind_speed * 1.5 + 2.0)
                # a large heading error means bleed speed off to turn tightly
                turn_penalty = max(0.25, math.cos(min(abs(err), math.pi / 2)))
                v_target = min(c.cruise_ms * turn_penalty,
                               max(v_arrive, v_floor), c.max_speed_ms)
                dv = v_target - st.speed
                st.speed += max(-c.accel_ms2 * dt, min(c.accel_ms2 * dt, dv))
                st.speed = max(0.0, min(st.speed, c.max_speed_ms))
                st.status = "TRANSIT" if len(st.waypoints) > 1 else "WORKING"

        # --- integrate with wind ---------------------------------------------
        wx, wy = self._wind_vector()
        vx = st.speed * math.cos(st.heading) + wx
        vy = st.speed * math.sin(st.heading) + wy
        st.x += vx * dt
        st.y += vy * dt
        st.distance_flown_m += math.hypot(vx, vy) * dt
        st.x = max(0.0, min(self.size_m, st.x))
        st.y = max(0.0, min(self.size_m, st.y))

        # --- battery ---------------------------------------------------------
        st.battery_wh -= self._power_w(rec, st.speed) * (dt / 3600.0)
        if st.battery_wh <= 0 and st.alive:
            st.battery_wh = 0.0
            st.alive = False
            st.status = "BATTERY_DEAD"
            events.append({"kind": "drone_lost", "drone": st.id, "why": "battery exhausted"})

        # --- sensing ----------------------------------------------------------
        if st.current_verb in ("area_search",) and st.speed > 0.5:
            self._paint_coverage(st, rec)
            events += self._detect(st, rec)

        return events

    def _paint_coverage(self, st: DroneState, rec: DroneRecord) -> None:
        half = rec.swath_m() / 2.0
        cell = self.coverage_cell_m
        steps = max(1, int(half / cell))
        nx, ny = -math.sin(st.heading), math.cos(st.heading)   # across-track
        for s in range(-steps, steps + 1):
            px = st.x + nx * s * cell
            py = st.y + ny * s * cell
            self.coverage.add((int(px / cell), int(py / cell)))

    def _detect(self, st: DroneState, rec: DroneRecord) -> list[dict]:
        out = []
        half = rec.swath_m() / 2.0
        for ct in self.contacts:
            if ct["found"]:
                continue
            if math.hypot(ct["x"] - st.x, ct["y"] - st.y) <= half:
                ct["found"] = True
                out.append({"kind": "contact_found", "contact": ct["id"],
                            "by": st.id, "x": ct["x"], "y": ct["y"]})
        return out

    # -- radio link budget ----------------------------------------------------
    def _fspl_dbm(self, dist_m: float, tx_dbm: float = 20.0, freq_hz: float = 2.4e9) -> float:
        d = max(1.0, dist_m)
        fspl = 20 * math.log10(d) + 20 * math.log10(freq_hz) - 147.55
        return tx_dbm - fspl

    def _update_links(self) -> list[dict]:
        events = []
        relays = [s for s in self.drones.values()
                  if s.alive and self.records[s.id].has("relay_comms")]
        for st in self.drones.values():
            if not st.alive:
                continue
            rec = self.records[st.id]
            rng = rec.constraints.comms_range_m
            d_direct = math.hypot(st.x - self.base[0], st.y - self.base[1])
            was = st.link_ok

            if d_direct <= rng:
                st.link_ok, st.link_via, st.link_dbm = True, "direct", self._fspl_dbm(d_direct)
            else:
                st.link_ok = False
                st.link_via = "none"
                st.link_dbm = self._fspl_dbm(d_direct)
                for r in relays:
                    if r.id == st.id:
                        continue
                    rrec = self.records[r.id]
                    d1 = math.hypot(st.x - r.x, st.y - r.y)
                    d2 = math.hypot(r.x - self.base[0], r.y - self.base[1])
                    if d1 <= min(rng, rrec.constraints.comms_range_m) and \
                       d2 <= rrec.constraints.comms_range_m:
                        st.link_ok, st.link_via = True, r.id
                        st.link_dbm = min(self._fspl_dbm(d1), self._fspl_dbm(d2))
                        break

            if was != st.link_ok:
                events.append({"kind": "link_change", "drone": st.id,
                               "ok": st.link_ok, "via": st.link_via})
        return events

    # -- main loop ------------------------------------------------------------
    def tick(self, dt: float) -> list[dict]:
        self.t += dt
        events: list[dict] = []
        for st in list(self.drones.values()):
            rec = self.records.get(st.id)
            if rec:
                events += self._step_drone(st, rec, dt)
        events += self._update_links()
        return events

    def snapshot(self) -> dict:
        return {
            "t": round(self.t, 2),
            "size_m": self.size_m,
            "base": {"x": self.base[0], "y": self.base[1]},
            "wind": {"dir_deg": round(math.degrees(self.wind_dir) % 360, 1),
                     "speed_ms": round(self.wind_speed, 1)},
            "drones": [{
                "id": s.id, "name": s.name,
                "x": round(s.x, 1), "y": round(s.y, 1), "z": round(s.z, 1),
                "heading_deg": round(math.degrees(s.heading) % 360, 1),
                "speed_ms": round(s.speed, 2),
                "battery_pct": round(s.battery_pct(), 1),
                "alive": s.alive, "status": s.status,
                "current_task": s.current_task, "current_verb": s.current_verb,
                "next_task": s.next_task, "next_verb": s.next_verb,
                "task_progress": round(s.task_progress, 3),
                "link_ok": s.link_ok, "link_dbm": round(s.link_dbm, 1), "link_via": s.link_via,
                "swath_m": round(self.records[s.id].swath_m(), 1) if s.id in self.records else 0,
                "distance_km": round(s.distance_flown_m / 1000.0, 2),
                "waypoints": [{"x": round(w.x, 1), "y": round(w.y, 1)} for w in s.waypoints[:40]],
            } for s in self.drones.values()],
            "contacts": self.contacts,
            "coverage": [[c[0], c[1]] for c in self.coverage],
            "coverage_cell_m": self.coverage_cell_m,
        }
