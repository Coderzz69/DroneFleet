"""A new protocol drone joining while a mission is already running."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet.core.registry import Constraints
from fleet.drone_node import ProtocolDrone
from fleet.protocol import CapabilityContract, DroneManifest, SensorContract
from fleet.web.runtime import Runtime


async def wait_until(pred, timeout=20.0, step=0.1):
    for _ in range(int(timeout / step)):
        if pred():
            return True
        await asyncio.sleep(step)
    return False


def manifest(drone_id: str, capabilities: list[str], radio: float) -> DroneManifest:
    return DroneManifest(
        drone_id=drone_id, callsign=drone_id, vehicle_type="test-uav",
        capabilities=[CapabilityContract(name=v) for v in capabilities],
        sensors=[SensorContract(sensor_id="thermal-1", kind="thermal")],
        constraints=Constraints(endurance_min=90, comms_range_m=radio,
                                cruise_ms=20, max_speed_ms=30),
    )


async def main() -> int:
    rt = Runtime(mqtt_port=18832, web_port=18082)
    scout = ProtocolDrone(manifest("joining-scout", ["area_search", "classify_survivor"], 8000),
                         "127.0.0.1", 18832, speedup=20)
    relay = ProtocolDrone(manifest("late-relay", ["relay_comms"], 40000),
                          "127.0.0.1", 18832, speedup=20)
    await rt.start()
    await scout.start()
    try:
        assert await wait_until(lambda: rt.master.registry.get("joining-scout") is not None)
        await rt.dispatch("find survivors in grid J9")
        assert rt.master.plan and rt.master.plan.verdict in ("FEASIBLE", "DEGRADED")
        await rt.dispatch("launch")
        assert await wait_until(lambda: any(t.state == "RUNNING"
                                             for t in rt.master.plan.tasks))

        # This is the real-world event: an additional aircraft boots and joins
        # while the first task is already in progress.
        await relay.start()
        assert await wait_until(lambda: any(t.verb == "relay_comms"
                                             for t in rt.master.plan.tasks))
        assert await wait_until(lambda: any(
            v.id == "late-relay" and v.phase in ("ASSIGNED", "WORKING", "DONE")
            for v in rt.master.views.values()))
        print("dynamic join: active mission replanned and late relay was tasked")
        return 0
    finally:
        await relay.stop()
        await scout.stop()
        await rt.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
