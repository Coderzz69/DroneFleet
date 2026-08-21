"""Standalone protocol-drone integration test.

This exercises the boundary that matters for real deployments: the
mothership does not create the vehicle or register it in-process.  It learns
the unit from a manifest, plans against that manifest, and dispatches a
multi-step task graph over MQTT.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet.agents.master import Master
from fleet.core.registry import Constraints
from fleet.drone_node import ProtocolDrone
from fleet.protocol import CapabilityContract, DroneManifest, SensorContract
from fleet.sim.world import World
from fleet.web.runtime import Runtime


async def wait_until(pred, timeout=15.0, step=0.1):
    waited = 0.0
    while waited < timeout:
        if pred():
            return True
        await asyncio.sleep(step)
        waited += step
    return False


async def main() -> int:
    rt = Runtime(mqtt_port=18831, web_port=18081)
    manifest = DroneManifest(
        drone_id="remote-rescue-1",
        callsign="RemoteRescue",
        vehicle_type="multi-role-uav",
        manufacturer="TestVendor",
        model="XR-1",
        firmware_version="1.2.3",
        capabilities=[CapabilityContract(name=v) for v in (
            "area_search", "classify_survivor", "deliver_payload")],
        sensors=[SensorContract(sensor_id="thermal-1", kind="thermal",
                                modality="thermal")],
        constraints=Constraints(endurance_min=90, comms_range_m=20000,
                                cruise_ms=20, max_speed_ms=30),
    )
    node = ProtocolDrone(manifest, "127.0.0.1", 18831, speedup=20)
    await rt.start()
    await node.start()
    try:
        assert await wait_until(lambda: rt.master.registry.get(manifest.drone_id) is not None)
        assert rt.master.registry.get(manifest.drone_id).metadata["manufacturer"] == "TestVendor"

        await rt.dispatch("find survivors in grid E5 and deliver supplies")
        assert rt.master.plan is not None
        assert rt.master.plan.verdict in ("FEASIBLE", "DEGRADED")
        assert all(t.assignee == manifest.drone_id for t in rt.master.plan.tasks)

        await rt.dispatch("launch")
        assert await wait_until(
            lambda: rt.master.plan is not None and
            all(t.state == "DONE" for t in rt.master.plan.tasks), timeout=20)
        # Task IDs are reused per plan; a new mission ID must still allow the
        # same standalone vehicle to accept T1/T2/T3 again.
        await rt.dispatch("find survivors in grid E5")
        await rt.dispatch("launch")
        assert await wait_until(
            lambda: rt.master.plan is not None and
            all(t.state == "DONE" for t in rt.master.plan.tasks), timeout=20)
        print("remote protocol: manifest, planning, ACK, progress and task DAG passed")
        return 0
    finally:
        await node.stop()
        await rt.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
