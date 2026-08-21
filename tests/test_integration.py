"""End-to-end: real MQTT broker, real client connections, real physics.

Runs the whole stack headlessly (no browser) and asserts a mission actually
flies to completion, then that killing a drone flips the verdict live.

    python3 -m tests.test_integration
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet.web.runtime import Runtime

PASS, FAIL = 0, 0


def check(label, got, want):
    global PASS, FAIL
    ok = got == want
    PASS, FAIL = PASS + ok, FAIL + (not ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f"   got={got!r} want={want!r}"))


async def wait_until(pred, timeout=45.0, step=0.2):
    waited = 0.0
    while waited < timeout:
        if pred():
            return True
        await asyncio.sleep(step)
        waited += step
    return False


async def main() -> int:
    rt = Runtime(mqtt_port=18830, web_port=18080)
    await rt.start()
    log: list[str] = []
    orig = rt.master.console

    async def spy(kind, text, **extra):
        log.append(f"{kind}: {text}")
        await orig(kind, text, **extra)
    rt.master.console = spy  # type: ignore[assignment]

    try:
        print("\nMQTT plumbing")
        await rt.dispatch("demo")
        ok = await wait_until(lambda: len(rt.master.registry) == 3, 10)
        check("3 drones registered over MQTT", ok, True)
        check("3 agents connected",
              all(a.mqtt.connected for a in rt.agents.values()), True)
        ok = await wait_until(lambda: len(rt.master.last_seen) >= 3, 10)
        check("master heard all 3 announce", ok, True)

        print("\nplanning")
        await rt.dispatch("find survivors in the north flood zone and deliver supplies")
        plan = rt.master.plan
        check("a plan exists", plan is not None, True)
        check("verdict is runnable", plan.verdict in ("FEASIBLE", "DEGRADED"), True)
        check("tasks bound to drones", all(t.assignee for t in plan.tasks), True)

        print("\nexecution (real physics, real MQTT round-trips)")
        await rt.dispatch("launch")
        ok = await wait_until(lambda: any(t.state == "RUNNING" for t in rt.master.plan.tasks), 15)
        check("a task reached RUNNING via ACK", ok, True)

        ok = await wait_until(
            lambda: any(t.state == "DONE" and t.verb == "area_search"
                        for t in rt.master.plan.tasks), 160)
        check("area_search completed", ok, True)
        check("contacts were physically detected",
              any(c["found"] for c in rt.world.contacts), True)
        check("coverage was painted", len(rt.world.coverage) > 50, True)

        ok = await wait_until(
            lambda: any(t.state == "DONE" and t.verb.startswith("classify")
                        for t in rt.master.plan.tasks), 160)
        check("classification completed (interlock satisfied)", ok, True)

        ok = await wait_until(
            lambda: any(t.state == "DONE" and t.verb == "deliver_payload"
                        for t in rt.master.plan.tasks), 160)
        check("payload delivered after the interlock cleared", ok, True)

        print("\nphysics sanity")
        st = next(iter(rt.world.drones.values()))
        check("drone actually moved", st.distance_flown_m > 500, True)
        check("battery was consumed", st.battery_pct() < 100.0, True)
        check("speed respects the airframe limit",
              all(s.speed <= rt.world.records[s.id].constraints.max_speed_ms + 0.01
                  for s in rt.world.drones.values()), True)

        print("\nfault injection -> live re-validation")
        await rt.dispatch("clear")
        await rt.dispatch("demo")
        await wait_until(lambda: len(rt.master.registry) == 3, 10)
        await rt.dispatch("find survivors in the north flood zone and deliver supplies")
        await rt.dispatch("launch")
        await wait_until(lambda: any(t.state in ("RUNNING", "ACKED")
                                     for t in rt.master.plan.tasks), 15)
        before = rt.master.plan.verdict

        # kill the only drone that can confirm a survivor
        victim = next(d for d in rt.master.registry.all()
                      if any(c.verb.startswith("classify") for c in d.capabilities))
        log.clear()
        rt.world.kill(victim.id)
        ok = await wait_until(lambda: rt.master.plan.verdict == "INSUFFICIENT", 25)
        check(f"killing the classifier flips {before} -> INSUFFICIENT", ok, True)
        check("loss was reported on the console",
              any("not responding" in m for m in log), True)
        check("a gap explains what is now missing",
              any(g.reason == "MISSING_CAPABILITY" for g in rt.master.plan.gaps), True)

    finally:
        await rt.stop()

    print(f"\n  {PASS} passed, {FAIL} failed\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
