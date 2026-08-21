"""Local LLM adapter against the real Ollama daemon.

Skips cleanly if Ollama is not running or the model is not pulled. The point
of these assertions is not that the model is clever -- it is that the model
CANNOT do damage: the verb enum is enforced by the decoder, numbers are
clamped, and the deterministic parser catches it when the model falls over.

    python3 -m tests.test_llm
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet.core import llm as llm_mod
from fleet.core import nlp
from fleet.core.nlp import parse_drone, parse_mission
from fleet.core.ontology import load_pack
from fleet.core.planner import FEASIBLE, INSUFFICIENT, Planner
from fleet.core.registry import Registry

PASS = FAIL = 0
MODEL = os.environ.get("FLEET_LLM_MODEL", llm_mod.DEFAULT_MODEL)
FAST = "qwen2.5:1.5b-instruct"


def check(label, got, want):
    global PASS, FAIL
    ok = got == want
    PASS, FAIL = PASS + ok, FAIL + (not ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f"   got={got!r} want={want!r}"))


def check_true(label, cond, detail=""):
    check(label + (f" [{detail}]" if detail and not cond else ""), bool(cond), True)


def main() -> int:
    sar = load_pack("search_and_rescue")
    sec = load_pack("perimeter_security")

    probe = llm_mod.OllamaAdapter(sar, model=MODEL)
    ok, msg = probe.available()
    if not ok:
        print(f"\n  SKIPPED — {msg}\n")
        return 0
    print(f"\nusing {msg}")

    adapter = llm_mod.build(sar, model=MODEL)
    nlp.LLM_ADAPTER = adapter
    try:
        print("\nregistration (free text -> capability record)")
        t0 = time.time()
        d = parse_drone("a drone called Sweeper with a thermal camera that sweeps "
                        "wide areas and identifies survivors, 60 min endurance, 8km radio",
                        sar, "drone-1")
        dt = time.time() - t0
        print(f"      -> {d.name} {sorted(d.verbs())} sensors={d.sensors} "
              f"endurance={d.constraints.endurance_min} radio={d.constraints.comms_range_m} "
              f"({dt:.1f}s)")
        check_true("search capability found", "area_search" in d.verbs())
        check_true("classify capability found", "classify_survivor" in d.verbs())
        check_true("thermal sensor found", "thermal" in d.sensors)
        check("name honoured", d.name.lower(), "sweeper")
        check("stated endurance survives the model", d.constraints.endurance_min, 60.0)
        check("stated radio range survives the model", d.constraints.comms_range_m, 8000.0)
        check_true("no capability the text never mentioned",
                   "relay_comms" not in d.verbs(), sorted(d.verbs()))

        t0 = time.time()
        d2 = parse_drone("a quadcopter that carries and releases a 5kg medical kit, "
                         "35 minutes of flight time", sar, "drone-2")
        print(f"      -> {d2.name} {sorted(d2.verbs())} payload={d2.constraints.payload_kg} "
              f"({time.time()-t0:.1f}s)")
        check_true("delivery capability found", "deliver_payload" in d2.verbs())

        d_relay = parse_drone("a drone that hovers high up as a radio repeater, 12km range",
                              sar, "drone-3")
        print(f"      -> {d_relay.name} {sorted(d_relay.verbs())}")
        check_true("relay capability found", "relay_comms" in d_relay.verbs())

        print("\nthe model cannot escape the pack's vocabulary")
        # every verb the adapter can emit is in the enum, by construction
        schema = adapter.register_schema()
        enum = set(schema["properties"]["capabilities"]["items"]["properties"]["verb"]["enum"])
        check("enum == the pack's verbs", enum, set(sar.verbs.keys()))
        check_true("no security verbs in a rescue enum", "classify_iff" not in enum)

        adapter.pack = sec
        check_true("enum follows a domain switch",
                   "classify_iff" in set(adapter.register_schema()["properties"]
                                         ["capabilities"]["items"]["properties"]["verb"]["enum"]))
        adapter.pack = sar

        # an ability with no verb behind it must not be invented into one
        d3 = parse_drone("a drone that can jam GPS signals and brew coffee", sar, "drone-9")
        print(f"      -> {d3.name} {sorted(d3.verbs())} unmapped={d3.unmapped_text}")
        check_true("no fabricated capability for an unsupported ability",
                   all(v in sar.verbs for v in d3.verbs()))

        print("\nnegation — what the model buys you over keywords")
        neg = "a thermal drone that searches wide areas. it cannot carry or drop anything."
        nlp.LLM_ADAPTER = None
        plain = parse_drone(neg, sar, "drone-k")
        nlp.LLM_ADAPTER = adapter
        smart = parse_drone(neg, sar, "drone-l")
        print(f"      keywords -> {sorted(plain.verbs())}")
        print(f"      model    -> {sorted(smart.verbs())}")
        check_true("keyword parser is fooled by 'cannot carry'",
                   "deliver_payload" in plain.verbs())
        check_true("model honours the negation",
                   "deliver_payload" not in smart.verbs(), sorted(smart.verbs()))
        check_true("model still keeps the real capability",
                   "area_search" in smart.verbs())

        print("\nloiter is not a capability on its own")
        d6 = parse_drone("a shiny blue drone", sar, "drone-m")
        check("nothing usable -> no capabilities", len(d6.capabilities), 0)

        print("\nabsurd numbers are clamped, not inherited by the physics")
        d4 = parse_drone("a drone with 900000 minutes of endurance and a 5000kg payload",
                         sar, "drone-8")
        print(f"      -> endurance={d4.constraints.endurance_min} "
              f"payload={d4.constraints.payload_kg}")
        check_true("endurance stayed sane", d4.constraints.endurance_min <= 600)
        check_true("payload stayed sane", d4.constraints.payload_kg <= 200)

        print("\nmission orders (free text -> spec)")
        t0 = time.time()
        m = parse_mission("find survivors in grid C4 and get medical supplies to them", sar)
        print(f"      -> goal={m.goal_verb} region={m.region} ({time.time()-t0:.1f}s)")
        check("goal is the final objective, not the first step",
              m.goal_verb, "deliver_payload")
        check_true("grid C4 located",
                   abs(m.region["x"] - 2652) < 400 and abs(m.region["y"] - 4860) < 400,
                   str(m.region))

        m2 = parse_mission("sweep the northern sector for anyone stranded", sar)
        print(f"      -> goal={m2.goal_verb} region={m2.region}")
        check_true("north is north of centre", m2.region["y"] < 6000, str(m2.region))
        check_true("region stays inside the world",
                   m2.region["x"] >= 0 and m2.region["y"] >= 0)

        print("\nend to end: LLM proposes, plain code still decides")
        reg = Registry()
        for text in ["thermal drone that searches wide areas and identifies survivors, "
                     "90 min endurance, 20km radio",
                     "drone that drops a 5kg medical kit, 60 min endurance, 20km radio"]:
            reg.add(parse_drone(text, sar, reg.next_id(), len(reg)))
        spec = parse_mission("find survivors in grid E5 and deliver supplies", sar)
        plan = Planner(sar, reg).plan(spec)
        print(f"      -> {plan.verdict}, {len(plan.tasks)} tasks")
        check("a complete LLM-parsed fleet is FEASIBLE", plan.verdict, FEASIBLE)

        reg2 = Registry()
        reg2.add(parse_drone("drone that drops a 5kg medical kit, 60 min endurance, "
                             "20km radio", sar, "drone-1"))
        plan2 = Planner(sar, reg2).plan(spec)
        print(f"      -> {plan2.verdict}, gaps={[g.needed for g in plan2.gaps]}")
        check("the validator still blocks an incomplete fleet",
              plan2.verdict, INSUFFICIENT)

        print("\nfallback when the daemon is gone")
        broken = llm_mod.OllamaAdapter(sar, model=MODEL, host="http://127.0.0.1:1")
        broken.timeout = 2
        nlp.LLM_ADAPTER = broken
        d5 = parse_drone("a thermal drone that sweeps wide areas, 45 min endurance", sar, "drone-7")
        check_true("deterministic parser took over", "area_search" in d5.verbs())
        m3 = parse_mission("search grid B7 for survivors", sar)
        check("mission parsing fell back too", m3.goal_verb, "classify_survivor")
        check_true("the failure was recorded", broken.failures > 0)

    finally:
        nlp.LLM_ADAPTER = None

    print(f"\n  {PASS} passed, {FAIL} failed")
    print(f"  {adapter.calls} model calls, {adapter.failures} fallbacks\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
