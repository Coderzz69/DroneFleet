"""A running mission continues on a surviving drone after a vehicle drops."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet.core.registry import Constraints
from fleet.drone_node import ProtocolDrone
from fleet.protocol import CapabilityContract, DroneManifest, SensorContract
from fleet.web.runtime import Runtime


async def wait_until(pred, timeout=25.0, step=0.1):
    for _ in range(int(timeout / step)):
        if pred():
            return True
        await asyncio.sleep(step)
    return False


def make_drone(drone_id: str) -> ProtocolDrone:
    manifest = DroneManifest(
        drone_id=drone_id, callsign=drone_id, vehicle_type="recon-uav",
        capabilities=[CapabilityContract(name="area_search"),
                      CapabilityContract(name="classify_survivor")],
        sensors=[SensorContract(sensor_id="thermal-1", kind="thermal")],
        constraints=Constraints(endurance_min=90, comms_range_m=20000,
                                cruise_ms=20, max_speed_ms=30),
    )
    return ProtocolDrone(manifest, "127.0.0.1", 18833, speedup=20)


async def main() -> int:
    rt = Runtime(mqtt_port=18833, web_port=18083)
    primary, backup = make_drone("primary-scout"), make_drone("backup-scout")
    await rt.start()
    await primary.start()
    await backup.start()
    try:
        assert await wait_until(lambda: len(rt.master.registry) == 2)
        await rt.dispatch("find survivors in grid J9")
        assert rt.master.plan and rt.master.plan.verdict in ("FEASIBLE", "DEGRADED")
        await rt.dispatch("launch")
        assert await wait_until(lambda: any(t.state == "RUNNING"
                                             for t in rt.master.plan.tasks))

        # Simulate the primary losing power/network while its task is active.
        await primary.stop()
        assert await wait_until(lambda: rt.master.views["primary-scout"].phase == "LOST")
        assert await wait_until(lambda: any(
            t.verb == "area_search" and t.assignee == "backup-scout"
            and t.state in ("ASSIGNED", "RUNNING", "DONE")
            for t in rt.master.plan.tasks))
        print("failover: lost task was re-planned onto the surviving drone")
        return 0
    finally:
        await backup.stop()
        await rt.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
