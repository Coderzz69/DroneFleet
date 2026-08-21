"""Text -> structure. Two separate jobs, never one blended prompt.

  parse_drone(text)   : free text  -> a capability record   (registration)
  parse_mission(text) : free text  -> a MissionSpec         (planning)

Both are deterministic keyword parsers so the whole system runs with no API
key and no network. `LLM_ADAPTER` is the seam: set it to a callable and these
functions will use it and validate its output against the same rules. The
planner never trusts either source -- everything is re-checked in planner.py.
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable, Optional

from .ontology import Pack
from .registry import Capability, Constraints, DroneRecord

LLM_ADAPTER: Optional[Callable[[str, str], dict[str, Any]]] = None

# phrase -> verb. Longest phrases are tried first so "search and rescue" style
# wording maps cleanly.
VERB_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("relay", "repeater", "repeat radio", "extend comms", "extend range", "comms relay"), "relay_comms"),
    (("drop", "deliver", "payload", "supplies", "medical kit", "medkit", "carry", "release", "airdrop"), "deliver_payload"),
    (("identify", "friend or foe", "iff", "friendly", "friendlies", "classify", "confirm", "recognis", "recogniz"), "classify"),
    (("survivor", "casualty", "victim", "person", "people"), "classify_survivor"),
    (("defect", "crack", "corrosion", "leak"), "classify_defect"),
    (("measure", "dimension", "gauge"), "measure_defect"),
    (("report", "file", "upload findings"), "file_report"),
    (("intercept", "shadow", "deter", "engage", "pursue"), "intercept"),
    (("guide", "ground team", "talk in", "vector"), "guide_ground_team"),
    (("track", "follow", "keep eyes"), "track"),
    (("assess", "damage", "after-action", "bda"), "assess"),
    (("search", "scan", "sweep", "survey", "patrol", "look for", "find", "cover area", "recon"), "area_search"),
    (("loiter", "hover", "station", "orbit", "hold position"), "loiter"),
]

SENSOR_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("thermal", "infrared", "ir camera", "heat"), "thermal"),
    (("eo/ir", "eo-ir", "eoir", "electro-optical", "gimbal camera"), "eo_ir"),
    (("lidar", "laser scanner", "point cloud"), "lidar"),
    (("radar", "sar", "synthetic aperture"), "radar"),
    (("camera", "rgb", "optical", "video", "visual"), "rgb"),
]

# Environmental hazards. These change nothing about feasibility -- they are
# scene-setting -- but an operator should see the flood they were sent to.
HAZARD_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("flood", "flooding", "inundat", "deluge", "submerged", "dam burst",
      "tsunami", "high water", "washed out"), "flood"),
    (("wildfire", "fire", "blaze", "burning", "ablaze", "arson", "smoke"), "fire"),
    (("earthquake", "quake", "seismic", "rubble", "collapsed", "aftershock",
      "debris field", "landslide"), "earthquake"),
    (("storm", "hurricane", "cyclone", "typhoon", "blizzard", "gale",
      "heavy rain", "downpour"), "storm"),
    (("chemical", "hazmat", "toxic", "contaminat", "gas cloud"), "chemical"),
]

HAZARDS = ["flood", "fire", "earthquake", "storm", "chemical", "none"]


def detect_hazard(text: str) -> str:
    """Which environment the incident is happening in, if the order says."""
    hits = _hits(text.lower(), HAZARD_HINTS)
    return hits[0] if hits else ""


NAME_POOL = ["Sweeper", "Courier", "Relay", "Spotter", "Sentinel", "Ranger", "Kestrel",
             "Harrier", "Vulcan", "Osprey", "Falcon", "Nomad", "Pelican", "Swift"]


def _num(pattern: str, text: str) -> Optional[float]:
    m = re.search(pattern, text, re.I)
    return float(m.group(1)) if m else None


def _hits(text: str, table) -> list[str]:
    out = []
    for phrases, target in table:
        if any(p in text for p in phrases) and target not in out:
            out.append(target)
    return out


# Plausible physical envelopes. Applied to EVERY parse, whoever produced it:
# a model can hallucinate a 900000-minute endurance, and a human can typo one.
# The physics must never inherit an absurd number.
LIMITS = {
    "endurance_min": (1.0, 600.0),
    "cruise_ms": (2.0, 90.0),
    "max_speed_ms": (2.0, 120.0),
    "comms_range_m": (100.0, 80_000.0),
    "altitude_m": (20.0, 3000.0),
    "payload_kg": (0.0, 200.0),
    "mass_kg": (0.2, 400.0),
    "accel_ms2": (0.2, 20.0),
    "turn_rate_deg_s": (5.0, 360.0),
    "sensor_fov_deg": (5.0, 170.0),
}


def clamp_constraints(c: Constraints) -> list[str]:
    """Pull every field back inside its envelope. Returns notes for the operator
    describing anything that had to be corrected."""
    notes = []
    for field_name, (lo, hi) in LIMITS.items():
        v = getattr(c, field_name, None)
        if not isinstance(v, (int, float)):
            continue
        v = float(v)
        if v < lo or v > hi:
            fixed = max(lo, min(hi, v))
            setattr(c, field_name, fixed)
            notes.append(f"{field_name} {v:g} is out of range — clamped to {fixed:g}")
    if c.max_speed_ms < c.cruise_ms:
        c.max_speed_ms = c.cruise_ms
    return notes


def resolve_verb(verb: str, pack: Pack) -> Optional[str]:
    """Map a generic hint onto the verb this pack actually defines."""
    if verb in pack.verbs:
        return verb
    if verb == "classify":
        for cand in ("classify_survivor", "classify_iff", "classify_defect"):
            if cand in pack.verbs:
                return cand
    if verb.startswith("classify") and "classify" in pack.verbs:
        return "classify"
    return None


def extract_numbers(text: str) -> dict[str, float]:
    """Pull stated quantities out of a description. Regex beats a small model
    at this decisively -- '8km radio' is a pattern, not a judgement call -- so
    this runs for the LLM path too and takes precedence over what it returns."""
    low = text.lower()
    out: dict[str, float] = {}

    endurance = _num(r"(\d+(?:\.\d+)?)\s*(?:min|minute)", low)
    if endurance is None:
        hours = _num(r"(\d+(?:\.\d+)?)\s*(?:hr|hour)", low)
        endurance = hours * 60 if hours else None
    if endurance:
        out["endurance_min"] = endurance

    payload = _num(r"(\d+(?:\.\d+)?)\s*kg", low)
    if payload:
        out["payload_kg"] = payload
        out["mass_kg"] = 3.0 + payload

    rng_km = _num(r"(\d+(?:\.\d+)?)\s*km\s*(?:radio|range|comms|link)", low)
    if rng_km is None and ("range" in low or "radio" in low or "comms" in low):
        rng_km = _num(r"(\d+(?:\.\d+)?)\s*km", low)
    if rng_km:
        out["comms_range_m"] = rng_km * 1000

    speed = _num(r"(\d+(?:\.\d+)?)\s*(?:m/s|ms-1|mps)", low)
    kmh = _num(r"(\d+(?:\.\d+)?)\s*(?:km/h|kph)", low)
    if kmh:
        speed = kmh / 3.6
    if speed:
        out["cruise_ms"] = speed
        out["max_speed_ms"] = speed * 1.4

    alt = _num(r"(\d+(?:\.\d+)?)\s*m\s*(?:altitude|high|up)", low)
    if alt:
        out["altitude_m"] = alt
    return out


def stated_name(text: str) -> Optional[str]:
    m = (re.search(r"(?:called|named)\s+([A-Za-z][\w-]*)", text)
         or re.search(r"[\"']([^\"']{2,20})[\"']", text))
    return m.group(1).strip().title() if m else None


def parse_drone(text: str, pack: Pack, drone_id: str, index: int = 0) -> DroneRecord:
    """Free text -> capability record. Anything it cannot map is preserved in
    `unmapped_text` and shown back to the operator rather than dropped."""
    if LLM_ADAPTER is not None:
        try:
            return _from_llm(text, pack, drone_id, index)
        except Exception:
            pass  # fall through to the deterministic parser

    low = text.lower()
    sensors = _hits(low, SENSOR_HINTS)
    verbs: list[str] = []
    for v in _hits(low, VERB_HINTS):
        resolved = resolve_verb(v, pack)
        if resolved and resolved not in verbs:
            verbs.append(resolved)

    # a drone that can do nothing is useless -- but never silently invent a skill
    unmapped: list[str] = []
    if not verbs:
        unmapped.append("no recognised capability in this description")

    if verbs and "loiter" not in verbs:
        verbs.append("loiter")   # every airframe can hold station

    c = Constraints()
    for k, v in extract_numbers(text).items():
        setattr(c, k, v)

    low_alt = text.lower()
    if "high altitude" in low_alt or "hovers high" in low_alt or "at altitude" in low_alt:
        c.altitude_m = max(c.altitude_m, 400.0)
        c.comms_range_m = max(c.comms_range_m, 12000.0)

    if "relay_comms" in verbs:
        c.endurance_min = max(c.endurance_min, 45.0)

    name = stated_name(text) or NAME_POOL[index % len(NAME_POOL)]

    unmapped += clamp_constraints(c)

    return DroneRecord(
        id=drone_id,
        name=name,
        capabilities=[Capability(verb=v) for v in verbs],
        sensors=sensors,
        constraints=c,
        unmapped_text=unmapped,
        source_text=text.strip(),
    )


def _ground_sensors(claimed, text: str, unmapped: list[str]) -> list[str]:
    """Same grounding rule as capabilities. A model will cheerfully fit a drone
    with an EO/IR gimbal nobody mentioned, and a sensor it does not have must
    not be shown to the operator as fact."""
    low = text.lower()
    supported = set(_hits(low, SENSOR_HINTS))
    out = []
    for s in claimed or []:
        if not isinstance(s, str):
            continue
        if s in supported or s.replace("_", "/") in low or s in low:
            out.append(s)
        else:
            unmapped.append(f"dropped sensor '{s}': not mentioned in the description")
    return out


def _clean_name(n) -> Optional[str]:
    """A 2B model sometimes answers 'X' or a whole sentence. Neither is a callsign."""
    if not isinstance(n, str):
        return None
    n = n.strip().strip("\"'").split(",")[0].strip()
    if not (2 <= len(n) <= 18) or " " in n.strip() and len(n.split()) > 2:
        return None
    return n.title()


def _from_llm(text: str, pack: Pack, drone_id: str, index: int) -> DroneRecord:
    """Adapter seam. The model's output is coerced into the same closed
    vocabulary -- it cannot invent a verb the pack does not define."""
    data = LLM_ADAPTER(text, "register")  # type: ignore[misc]
    verbs, unmapped = [], []
    for v in data.get("capabilities", []):
        name = v.get("verb") if isinstance(v, dict) else v
        resolved = resolve_verb(name, pack)
        if resolved:
            verbs.append(resolved)
        else:
            unmapped.append(f"unknown verb from model: {name}")
    # the model fills gaps ("flies for about an hour"); the regex wins wherever
    # it found something, because on stated quantities it is simply better
    c = Constraints(**{k: float(v) for k, v in (data.get("constraints") or {}).items()
                       if k in Constraints.__dataclass_fields__
                       and isinstance(v, (int, float))})
    for k, v in extract_numbers(text).items():
        setattr(c, k, v)
    if "payload_kg" in (data.get("constraints") or {}) and "mass_kg" not in extract_numbers(text):
        c.mass_kg = 3.0 + c.payload_kg
    unmapped += clamp_constraints(c)

    # loiter alone is not a capability -- it is what any airframe does when it
    # has nothing to do, so it must never make a useless drone look useful
    if verbs and set(verbs) == {"loiter"}:
        verbs = []
    if verbs and "loiter" not in verbs:
        verbs.append("loiter")
    if not verbs:
        unmapped.append("no usable capability in this description")

    return DroneRecord(
        id=drone_id,
        name=stated_name(text) or _clean_name(data.get("name"))
             or NAME_POOL[index % len(NAME_POOL)],
        capabilities=[Capability(verb=v) for v in verbs],
        sensors=_ground_sensors(data.get("sensors"), text, unmapped),
        constraints=c,
        unmapped_text=unmapped + list(data.get("unmapped_text", [])),
        source_text=text.strip(),
    )


# --------------------------------------------------------------------------
# mission parsing
# --------------------------------------------------------------------------
GRID_RE = re.compile(r"\b(?:grid|sector|zone|quadrant)\s*([a-j])\s*-?\s*([0-9])\b", re.I)
COMPASS = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0),
           "northeast": (1, -1), "northwest": (-1, -1), "southeast": (1, 1), "southwest": (-1, 1)}


def _build_region(grid: Optional[str], direction: Optional[str],
                  area_km2: Optional[float], world_size_m: float) -> dict:
    """Geometry stays here, in code. The model is never asked for coordinates --
    only for which grid square or compass word was mentioned."""
    cx, cy = world_size_m / 2, world_size_m / 2
    w = h = 2000.0

    if grid:
        m = re.match(r"\s*([a-jA-J])\s*-?\s*([0-9])\s*$", grid)
        if m:
            col = ord(m.group(1).lower()) - ord("a")
            row = int(m.group(2))
            cell = world_size_m / 10.0
            cx, cy = (col + 0.5) * cell, (min(row, 9) + 0.5) * cell
            w = h = cell * 0.9
    elif direction and direction in COMPASS:
        dx, dy = COMPASS[direction]
        cx += dx * world_size_m * 0.3
        cy += dy * world_size_m * 0.3

    if area_km2 and area_km2 > 0:
        w = h = math.sqrt(area_km2) * 1000

    # never let a region hang off the edge of the world
    w = min(w, world_size_m); h = min(h, world_size_m)
    cx = max(w / 2, min(world_size_m - w / 2, cx))
    cy = max(h / 2, min(world_size_m - h / 2, cy))
    return {"x": round(cx - w / 2, 1), "y": round(cy - h / 2, 1),
            "w": round(w, 1), "h": round(h, 1)}


def goal_depth(vname: str, pack: Pack, seen=()) -> int:
    """How long a dependency chain this verb sits on top of. The deepest verb
    mentioned is the real objective -- searching is only how you get there."""
    v = pack.verbs.get(vname)
    if not v or vname in seen:
        return 0
    idx = pack.produces_index()
    best = 0
    for token in v.requires:
        if token == "region":
            continue
        for supplier in idx.get(token, []):
            best = max(best, 1 + goal_depth(supplier, pack, seen + (vname,)))
    return best


def parse_mission(text: str, pack: Pack, world_size_m: float = 12000.0):
    """Free text -> MissionSpec."""
    from .planner import MissionSpec

    if LLM_ADAPTER is not None:
        try:
            data = LLM_ADAPTER(text, "mission")
            goal = resolve_verb(data.get("goal_verb") or "", pack)
            if goal:
                area = data.get("area_km2")
                hz = data.get("hazard") or ""
                return MissionSpec(
                    goal_verb=goal,
                    region=_build_region(data.get("grid"), data.get("direction"),
                                         float(area) if isinstance(area, (int, float)) else None,
                                         world_size_m),
                    # the keyword table is authoritative when it fires; the
                    # model only fills in what it did not catch
                    hazard=detect_hazard(text) or (hz if hz in HAZARDS and hz != "none" else ""),
                    raw=text.strip())
        except Exception:
            pass  # fall through to the deterministic parser

    low = text.lower()
    hints = _hits(low, VERB_HINTS)
    resolved = [r for r in (resolve_verb(h, pack) for h in hints) if r]
    goal = max(resolved, key=lambda v: goal_depth(v, pack)) if resolved else "area_search"

    gm = GRID_RE.search(text)
    grid = f"{gm.group(1)}{gm.group(2)}" if gm else None
    # longest first, or "northeast" matches as plain "north"
    direction = next((wd for wd in sorted(COMPASS, key=len, reverse=True) if wd in low), None)
    size = _num(r"(\d+(?:\.\d+)?)\s*km2|(\d+(?:\.\d+)?)\s*square", low)

    return MissionSpec(goal_verb=goal,
                       region=_build_region(grid, direction, size, world_size_m),
                       hazard=detect_hazard(text),
                       raw=text.strip())
