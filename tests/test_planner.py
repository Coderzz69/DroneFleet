"""Feasibility engine tests. No AI, no network, no UI -- which is the point:
the part that decides whether a mission is possible is plain, testable code.

    python3 -m tests.test_planner
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet.agents.drone import DroneAgent
from fleet.core.nlp import detect_hazard, parse_drone, parse_mission
from fleet.sim.world import World
from fleet.core.ontology import load_pack
from fleet.core.planner import DEGRADED, FEASIBLE, INSUFFICIENT, Planner
from fleet.core.registry import Registry

PASS, FAIL = 0, 0


def check(label, got, want):
    global PASS, FAIL
    ok = got == want
    PASS, FAIL = PASS + ok, FAIL + (not ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f"   got={got!r} want={want!r}"))


def fleet(pack, *descriptions):
    reg = Registry()
    for i, d in enumerate(descriptions):
        reg.add(parse_drone(d, pack, reg.next_id(), i))
    return reg


def verdict(pack, reg, prompt):
    spec = parse_mission(prompt, pack)
    return Planner(pack, reg).plan(spec)


def main():
    sar = load_pack("search_and_rescue")
    sec = load_pack("perimeter_security")
    insp = load_pack("infrastructure_inspection")

    print("\nsearch_and_rescue")
    # full fleet, target close to base -> feasible
    reg = fleet(sar,
                "thermal camera, sweeps wide areas, 90 min endurance, 20km radio",
                "carries and drops a 5kg medical kit, 60 min endurance, 20km radio",
                "identifies survivors with eo/ir, 60 min endurance, 20km radio")
    p = verdict(sar, reg, "find survivors and deliver supplies in grid E5")
    check("complete fleet -> FEASIBLE", p.verdict, FEASIBLE)
    check("chain has 3 tasks", len(p.tasks), 3)
    check("goal is deliver_payload", p.goal, "deliver_payload")
    check("every task is assigned", all(t.assignee for t in p.tasks), True)

    # no way to confirm a survivor -> the interlock blocks it
    reg2 = fleet(sar,
                 "thermal camera, sweeps wide areas, 90 min endurance, 20km radio",
                 "carries and drops a 5kg medical kit, 60 min endurance, 20km radio")
    p2 = verdict(sar, reg2, "find survivors and deliver supplies in grid E5")
    check("no classifier -> INSUFFICIENT", p2.verdict, INSUFFICIENT)
    check("gap names the missing capability",
          any("classify_survivor" in (g.needed or "") for g in p2.gaps), True)

    # far target, short radio, no relay -> degraded (not fatal)
    reg3 = fleet(sar,
                 "thermal camera, sweeps wide areas, 200 min endurance, 1km radio range",
                 "carries and drops a 5kg medical kit, 200 min endurance, 1km radio range",
                 "identifies survivors with eo/ir, 200 min endurance, 1km radio range")
    p3 = verdict(sar, reg3, "find survivors and deliver supplies in grid J9")
    check("out of radio range -> DEGRADED", p3.verdict, DEGRADED)
    check("gap is comms, severity degraded",
          any(g.needed == "comms_range" and g.severity == "degraded" for g in p3.gaps), True)

    # same, plus a relay -> feasible again
    reg4 = fleet(sar,
                 "thermal camera, sweeps wide areas, 200 min endurance, 1km radio range",
                 "carries and drops a 5kg medical kit, 200 min endurance, 1km radio range",
                 "identifies survivors with eo/ir, 200 min endurance, 1km radio range",
                 "radio repeater that hovers high up, 200 min endurance, 40km range")
    p4 = verdict(sar, reg4, "find survivors and deliver supplies in grid J9")
    check("relay added -> FEASIBLE", p4.verdict, FEASIBLE)

    # endurance physics
    reg5 = fleet(sar,
                 "thermal camera, sweeps wide areas, 3 min endurance, 20km radio",
                 "carries and drops a 5kg medical kit, 60 min endurance, 20km radio",
                 "identifies survivors with eo/ir, 60 min endurance, 20km radio")
    p5 = verdict(sar, reg5, "find survivors and deliver supplies in grid E5")
    check("battery too small -> INSUFFICIENT", p5.verdict, INSUFFICIENT)
    check("gap is endurance", any(g.needed == "endurance" for g in p5.gaps), True)

    print("\nperimeter_security (same engine, different pack)")
    reg6 = fleet(sec,
                 "radar that patrols wide areas, 90 min endurance, 20km radio",
                 "intercepts and shadows intruders, 90 min endurance, 20km radio")
    p6 = verdict(sec, reg6, "patrol the perimeter and intercept intruders in grid E5")
    check("no IFF -> INSUFFICIENT", p6.verdict, INSUFFICIENT)
    check("blocked on iff_clearance",
          any("classify_iff" in (g.needed or "") or "iff" in (g.needed or "") for g in p6.gaps), True)

    reg7 = fleet(sec,
                 "radar that patrols wide areas, 90 min endurance, 20km radio",
                 "identifies friend or foe with eo/ir, 90 min endurance, 20km radio",
                 "intercepts and shadows intruders, 90 min endurance, 20km radio")
    p7 = verdict(sec, reg7, "patrol the perimeter and intercept intruders in grid E5")
    check("IFF drone added -> FEASIBLE", p7.verdict, FEASIBLE)
    check("intercept is interlocked",
          any("iff_clearance" in t.note for t in p7.tasks if t.verb == "intercept"), True)

    print("\ninfrastructure_inspection (third pack, zero engine changes)")
    reg8 = fleet(insp,
                 "lidar and rgb camera that surveys pipelines, confirms cracks, measures defects and files a report, 90 min endurance, 20km radio")
    p8 = verdict(insp, reg8, "inspect the pipeline in grid E5 and file a report")
    check("one drone doing every verb -> FEASIBLE", p8.verdict, FEASIBLE)
    check("four-step chain", len(p8.tasks), 4)

    print("\nparser")
    d = parse_drone("a drone that hovers high up as a radio repeater, 12km range", sar, "drone-x")
    check("relay verb recognised", "relay_comms" in d.verbs(), True)
    check("12km range parsed", d.constraints.comms_range_m, 12000.0)
    d2 = parse_drone("a drone that carries a 5kg medical kit", sar, "drone-y")
    check("payload parsed", d2.constraints.payload_kg, 5.0)
    check("deliver verb recognised", "deliver_payload" in d2.verbs(), True)
    d3 = parse_drone("a shiny blue drone", sar, "drone-z")
    check("nonsense -> no capabilities", len(d3.capabilities), 0)
    check("nonsense -> flagged, not invented", len(d3.unmapped_text) > 0, True)

    m = parse_mission("sweep grid B7 for survivors", sar)
    check("grid ref parsed", (round(m.region["x"]), round(m.region["y"])), (1260, 8460))

    print("\nhazards (scene-setting, never feasibility)")
    for text, want in [("find survivors in the north flood zone", "flood"),
                       ("search the burning warehouse for casualties", "fire"),
                       ("rescue people trapped in rubble after the earthquake", "earthquake"),
                       ("survey the coast during the storm", "storm"),
                       ("check the toxic spill area", "chemical"),
                       ("patrol sector D5 for intruders", "")]:
        check(f"{want or '(none)':11} <- {text[:34]}", detect_hazard(text), want)
    check("hazard reaches the spec",
          parse_mission("find survivors in the north flood zone", sar).hazard, "flood")
    check("a mission with no disaster names none",
          parse_mission("search grid B3 for casualties", sar).hazard, "")

    print("\ncontact ground truth is hidden until classified")
    w = World()
    region = {"x": 0, "y": 0, "w": 1000, "h": 1000}
    w.seed_contacts(region, 4, "perimeter_security")
    check("security seeds both sides",
          {c["truth"] for c in w.contacts}, {"hostile", "friendly"})
    w.seed_contacts(region, 3, "search_and_rescue")
    check("rescue seeds survivors", w.contacts[0]["truth"], "survivor")
    snap = w.snapshot()
    check("nothing is classified up front",
          {c["kind"] for c in snap["contacts"]}, {"unknown"})
    check("ground truth never reaches the UI",
          any("truth" in c for c in snap["contacts"]), False)
    w.set_hazard("flood", region)
    check("hazard is published", w.snapshot()["hazard"]["kind"], "flood")

    print("\nclassification can say no")
    check("intercept acts on a hostile",
          DroneAgent._actionable("intercept", {"kind": "hostile"}), True)
    check("intercept refuses a friendly",
          DroneAgent._actionable("intercept", {"kind": "friendly"}), False)
    check("delivery serves a survivor",
          DroneAgent._actionable("deliver_payload", {"kind": "survivor"}), True)
    check("delivery skips a hostile",
          DroneAgent._actionable("deliver_payload", {"kind": "hostile"}), False)

    print(f"\n  {PASS} passed, {FAIL} failed\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
