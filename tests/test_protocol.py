"""The interoperability boundary: a vehicle the mothership did not build.

Two halves. The first is pure and instant -- manifest parsing, coercion and
the safety projection, with no broker and no event loop. The second flies real
missions over a real broker against vehicles that are only reachable by
message, to prove the mothership never reaches into a drone it did not create.

    python3 -m tests.test_protocol
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet.core.messages import Envelope, MsgType
from fleet.core.protocol import (PROTOCOL_VERSION, CapabilityContract,
                                 DroneManifest, PayloadContract, SensorContract,
                                 manifest_from_dict, record_from_announcement)
from fleet.core.registry import Constraints
from fleet.drone_node import PROFILES, ProtocolDrone, build_parser, make_manifest
from fleet.web.runtime import Runtime

PASS, FAIL = 0, 0


def check(label, got, want):
    global PASS, FAIL
    ok = got == want
    PASS, FAIL = PASS + ok, FAIL + (not ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f"   got={got!r} want={want!r}"))


def check_true(label, cond):
    check(label, bool(cond), True)


def raises(label, fn):
    try:
        fn()
    except ValueError:
        check(label, "ValueError", "ValueError")
        return
    except Exception as exc:                                  # noqa: BLE001
        check(label, type(exc).__name__, "ValueError")
        return
    check(label, "no error", "ValueError")


async def wait_until(pred, timeout=30.0, step=0.1):
    waited = 0.0
    while waited < timeout:
        if pred():
            return True
        await asyncio.sleep(step)
        waited += step
    return False


def manifest(drone_id, verbs, **kw):
    kw.setdefault("constraints", Constraints(endurance_min=90, comms_range_m=20000,
                                             cruise_ms=20, max_speed_ms=30))
    return DroneManifest(
        drone_id=drone_id, callsign=kw.pop("callsign", drone_id),
        vehicle_type="test-uav",
        capabilities=[CapabilityContract(name=v) for v in verbs],
        sensors=[SensorContract(sensor_id="thermal-1", kind="thermal",
                                modality="LWIR")],
        **kw)


# --------------------------------------------------------------------------
# 1. the manifest contract -- no network, no loop
# --------------------------------------------------------------------------
def test_manifest() -> None:
    print("\nmanifest parsing")
    good = manifest("v-1", ["area_search"]).to_dict()
    m = manifest_from_dict(good)
    check("round-trips through the wire form", m.drone_id, "v-1")
    check("stamps the protocol name", good["protocol"], "dronefleet")

    raises("a non-object manifest is refused", lambda: manifest_from_dict([1, 2]))
    raises("a foreign protocol is refused",
           lambda: manifest_from_dict({**good, "protocol": "someone-elses"}))
    raises("an unsupported major version is refused",
           lambda: manifest_from_dict({**good, "protocol_version": "2.0"}))
    check_true("a compatible minor version is accepted",
               manifest_from_dict({**good, "protocol_version": "1.7"}))
    raises("a manifest with no drone_id is refused",
           lambda: manifest_from_dict({**good, "drone_id": ""}))
    raises("a manifest with no callsign is refused",
           lambda: manifest_from_dict({**good, "callsign": ""}))

    print("\nmalformed fields are coerced, never fatal")
    # A vendor sending junk in one optional field must not cost us the vehicle.
    messy = {**good,
             "sensors": [{"sensor_id": "s-1"}, {"kind": "rgb"}, "not-an-object", 7],
             "payloads": [{"kind": "medical"}, {"payload_id": "p-1"}],
             "capabilities": ["area_search", {"name": "classify_survivor"}, 42, {}],
             "constraints": {"endurance_min": "sixty", "cruise_ms": 21,
                             "comms_range_m": True, "nonsense": 5}}
    m2 = manifest_from_dict(messy)
    check("a sensor with no kind still parses", m2.sensors[0].kind, "s-1")
    check("a sensor with no id still parses", m2.sensors[1].sensor_id, "rgb")
    check("a bare sensor name is accepted as one", m2.sensors[2].kind, "not-an-object")
    check("entries that are neither string nor object are dropped", len(m2.sensors), 3)
    check("a payload with no id is dropped", [p.payload_id for p in m2.payloads], ["p-1"])
    check("unusable capability entries are dropped",
          [c.name for c in m2.capabilities], ["area_search", "classify_survivor"])
    check("a non-numeric constraint falls back to the default",
          m2.constraints.endurance_min, Constraints().endurance_min)
    check("a numeric constraint is taken", m2.constraints.cruise_ms, 21.0)
    check("a boolean is not a number", m2.constraints.comms_range_m,
          Constraints().comms_range_m)

    print("\nout-of-envelope numbers are clamped, not believed")
    fast = manifest_from_dict({**good, "constraints": {"cruise_ms": 5000,
                                                       "endurance_min": 99999}})
    rec = fast.to_record()
    check("an absurd cruise speed is clamped", rec.constraints.cruise_ms, 90.0)
    check("an absurd endurance is clamped", rec.constraints.endurance_min, 600.0)
    check_true("and the operator is told", any("cruise_ms" in w for w in rec.withheld))

    print("\nthe safety projection")
    guarded = DroneManifest(
        drone_id="v-2", callsign="Guarded", vehicle_type="test-uav",
        capabilities=[
            CapabilityContract(name="area_search"),
            CapabilityContract(name="deliver_payload",
                               requires_human_authorization=True),
            CapabilityContract(name="guide_ground_team",
                               supports_autonomous_execution=False),
        ],
        sensors=[SensorContract(sensor_id="t-1", kind="thermal", modality="LWIR"),
                 SensorContract(sensor_id="t-2", kind="eo_ir", status="FAILED")],
        payloads=[PayloadContract(payload_id="kit", kind="medical")],
    )
    grec = guarded.to_record()
    check("a human-authorization capability is not offered to the planner",
          sorted(grec.verbs()), ["area_search"])
    check_true("and the reason is recorded",
               any("human authorization" in w for w in grec.withheld))
    check_true("so is a capability it cannot fly itself",
               any("guide_ground_team" in w for w in grec.withheld))
    check("a failed sensor is not counted", grec.sensors, ["thermal", "lwir"])
    check_true("and that is explained too",
               any("t-2" in w and "FAILED" in w for w in grec.withheld))
    check("kind ranks before modality, so packs still match",
          grec.sensors[0], "thermal")
    check("the full manifest is kept for audit",
          grec.metadata["manufacturer"], "unknown")
    check("and it is flagged as externally owned", grec.remote, True)

    print("\nthe in-process announcement uses the same door")
    legacy = {"id": "drone-1", "name": "Scout",
              "capabilities": [{"verb": "area_search", "params": {}}],
              "sensors": ["thermal"], "constraints": {"cruise_ms": 19.0},
              "source_text": "a scout with a thermal camera"}
    lrec = record_from_announcement(legacy)
    check("a legacy announcement still parses", sorted(lrec.verbs()), ["area_search"])
    check("and is not marked remote", lrec.remote, False)
    check("its sensors survive", lrec.sensors, ["thermal"])
    raises("an announcement with no id is refused",
           lambda: record_from_announcement({"name": "nobody"}))

    print("\nthe envelope")
    e = Envelope(type=MsgType.TASK_ASSIGN, src="master-0")
    check("is stamped with the protocol version", e.protocol_version, PROTOCOL_VERSION)
    check_true("understands itself", e.understood())
    check("rejects a future major version",
          Envelope(type="X", src="y", protocol_version="9.0").understood(), False)
    check("rejects foreign traffic",
          Envelope(type="X", src="y", protocol="other").understood(), False)
    check("tolerates an unstamped sender",
          Envelope(type="X", src="y", protocol="", protocol_version="").understood(), True)
    check("is not expired without a deadline", e.expired(), False)
    check("is expired past its deadline",
          Envelope(type="X", src="y", expires_at=1.0).expired(), True)

    print("\nthe reference profiles")
    for name in sorted(PROFILES):
        args = build_parser().parse_args(["--id", f"p-{name}", "--profile", name])
        rec = make_manifest(args).to_record()
        check(f"profile '{name}' produces a usable record",
              bool(rec.verbs()) and rec.id == f"p-{name}", True)


# --------------------------------------------------------------------------
# 2. live: real broker, vehicles the mothership does not own
# --------------------------------------------------------------------------
def node(drone_id, verbs, port, **kw):
    return ProtocolDrone(manifest(drone_id, verbs, **kw), "127.0.0.1", port,
                         speedup=25)


async def test_remote_mission() -> None:
    print("\na mission flown entirely by a vehicle the master did not create")
    rt = Runtime(mqtt_port=18841, web_port=18091)
    drone = node("remote-rescue-1", ["area_search", "classify_survivor",
                                     "deliver_payload"], 18841,
                 callsign="RemoteRescue", manufacturer="TestVendor", model="XR-1",
                 firmware_version="1.2.3")
    await rt.start()
    await drone.start()
    try:
        check("the master learns it from a manifest alone",
              await wait_until(lambda: rt.master.registry.get("remote-rescue-1")), True)
        rec = rt.master.registry.get("remote-rescue-1")
        check("with its vendor identity intact",
              rec.metadata["manufacturer"], "TestVendor")
        check("and no simulator body",
              "remote-rescue-1" in rt.world.drones, False)

        await rt.dispatch("find survivors in grid E5 and deliver supplies")
        plan = rt.master.plan
        check_true("it can be planned against", plan and plan.verdict != "INSUFFICIENT")
        check("every task binds to it",
              all(t.assignee == "remote-rescue-1" for t in plan.tasks), True)

        await rt.dispatch("launch")
        check("the whole task graph runs to completion", await wait_until(
            lambda: all(t.state == "DONE" for t in rt.master.plan.tasks)), True)
        check_true("the master knows where it is",
                   rt.master.views["remote-rescue-1"].x is not None)

        # Task ids restart at T1 every plan. Without the mission id in the
        # envelope the vehicle would dedupe the second mission away entirely.
        first_mission = rt.master.mission_id
        await rt.dispatch("find survivors in grid E5")
        await rt.dispatch("launch")
        check("a second mission reuses the same ids and still flies",
              await wait_until(
                  lambda: all(t.state == "DONE" for t in rt.master.plan.tasks)), True)
        check("under a new mission id", rt.master.mission_id != first_mission, True)
    finally:
        await drone.stop()
        await rt.stop()


async def test_vehicle_side_refusal() -> None:
    print("\nthe vehicle checks the order for itself")
    rt = Runtime(mqtt_port=18842, web_port=18092)
    drone = node("picky-1", ["area_search"], 18842)
    await rt.start()
    await drone.start()
    try:
        check("it is registered",
              await wait_until(lambda: rt.master.registry.get("picky-1")), True)

        from fleet.net import topics
        rejected: list[str] = []

        async def spy(topic, payload):
            env = Envelope.from_json(payload)
            if env.type == MsgType.TASK_REJECT:
                rejected.append(env.payload.get("reason", ""))
        await rt.master.mqtt.subscribe(topics.MASTER_INBOX, spy)

        async def order(**kw):
            await rt.master.mqtt.publish(topics.drone_inbox("picky-1"), Envelope(
                type=MsgType.TASK_ASSIGN, src="master-0", dst="picky-1",
                **kw).to_json())

        await order(corr_id="X1", payload={"task_id": "X1", "verb": "intercept"})
        check("an undeclared verb is refused", await wait_until(
            lambda: any("not declared" in r for r in rejected), timeout=5), True)

        await order(corr_id="X2", expires_at=1.0,
                    payload={"task_id": "X2", "verb": "area_search"})
        check("an expired order is refused", await wait_until(
            lambda: any("expired" in r for r in rejected), timeout=5), True)

        await order(corr_id="X3", payload={"task_id": "X3", "verb": "area_search",
                                           "params": "not-a-dict"})
        check("malformed params are refused", await wait_until(
            lambda: any("invalid params" in r for r in rejected), timeout=5), True)

        drone.battery_pct = 1.0
        await order(corr_id="X4", payload={"task_id": "X4", "verb": "area_search"})
        check("a flat battery is refused", await wait_until(
            lambda: any("battery" in r for r in rejected), timeout=5), True)
    finally:
        await drone.stop()
        await rt.stop()


async def test_duplicate_order() -> None:
    print("\na repeated order is answered, not ignored")
    rt = Runtime(mqtt_port=18843, web_port=18093)
    drone = node("dupe-1", ["area_search"], 18843)
    await rt.start()
    await drone.start()
    try:
        from fleet.net import topics
        acks: list[Envelope] = []

        async def spy(topic, payload):
            env = Envelope.from_json(payload)
            if env.type == MsgType.TASK_ACK:
                acks.append(env)
        await rt.master.mqtt.subscribe(topics.MASTER_INBOX, spy)
        check("it is registered",
              await wait_until(lambda: rt.master.registry.get("dupe-1")), True)

        order = Envelope(type=MsgType.TASK_ASSIGN, src="master-0", dst="dupe-1",
                         corr_id="T1", mission_id="m-1",
                         payload={"task_id": "T1", "verb": "area_search",
                                  "est_duration_s": 5}).to_json()
        await rt.master.mqtt.publish(topics.drone_inbox("dupe-1"), order)
        check("the order is acked",
              await wait_until(lambda: len(acks) >= 1, timeout=5), True)
        await rt.master.mqtt.publish(topics.drone_inbox("dupe-1"), order)
        check("the repeat is acked again rather than dropped",
              await wait_until(lambda: len(acks) >= 2, timeout=5), True)
        check("and marked as a duplicate", acks[1].payload.get("duplicate"), True)
        check("not flown twice", len([a for a in acks
                                      if not a.payload.get("duplicate")]), 1)
    finally:
        await drone.stop()
        await rt.stop()


async def test_failover() -> None:
    print("\nwork moves to a survivor when a vehicle drops mid-task")
    rt = Runtime(mqtt_port=18844, web_port=18094)
    primary = node("primary-scout", ["area_search", "classify_survivor"], 18844)
    backup = node("backup-scout", ["area_search", "classify_survivor"], 18844)
    await rt.start()
    await primary.start()
    await backup.start()
    try:
        check("both vehicles register",
              await wait_until(lambda: len(rt.master.registry) == 2), True)
        await rt.dispatch("find survivors in grid J9")
        check_true("the mission is runnable",
                   rt.master.plan and rt.master.plan.verdict != "INSUFFICIENT")
        await rt.dispatch("launch")
        check("work starts", await wait_until(
            lambda: any(t.state in ("ASSIGNED", "ACKED", "RUNNING")
                        for t in rt.master.plan.tasks)), True)

        holder = next((t.assignee for t in rt.master.plan.tasks
                       if t.state in ("ASSIGNED", "ACKED", "RUNNING")), None)
        victim = primary if holder == "primary-scout" else backup
        survivor = "backup-scout" if holder == "primary-scout" else "primary-scout"

        # Power/network loss: it simply stops talking. No notification exists.
        await victim.stop()
        check("the master notices the silence itself", await wait_until(
            lambda: rt.master.views[holder].phase == "LOST"), True)
        check("its registry record goes offline",
              rt.master.registry.get(holder).online, False)
        check("and the abandoned work is rebound to the survivor",
              await wait_until(lambda: any(
                  t.assignee == survivor and t.state in
                  ("PENDING", "ASSIGNED", "ACKED", "RUNNING", "DONE")
                  for t in rt.master.plan.tasks)), True)
    finally:
        await primary.stop()
        await backup.stop()
        await rt.stop()


async def test_dynamic_join() -> None:
    print("\na vehicle that boots mid-mission is planned into it")
    rt = Runtime(mqtt_port=18845, web_port=18095)
    scout = node("joining-scout", ["area_search"], 18845)
    medic = node("late-medic", ["classify_survivor", "deliver_payload"], 18845)
    await rt.start()
    await scout.start()
    try:
        check("the scout registers",
              await wait_until(lambda: rt.master.registry.get("joining-scout")), True)
        await rt.dispatch("find survivors in grid J9 and deliver supplies")
        # Nothing on station can classify or deliver, so the engine must say so
        # rather than plan around a capability that is not there.
        check("the mission is blocked while the fleet cannot do it",
              rt.master.plan.verdict, "INSUFFICIENT")
        missing = {g.needed for g in rt.master.plan.gaps}
        check_true("naming the capabilities it lacks",
                   {"classify_survivor", "deliver_payload"} & missing)

        await medic.start()
        check("the late arrival is re-planned in", await wait_until(
            lambda: rt.master.plan.verdict != "INSUFFICIENT"), True)
        check("and the previously unbindable work is bound to it",
              await wait_until(lambda: all(
                  t.assignee == "late-medic"
                  for t in rt.master.plan.tasks
                  if t.verb in ("classify_survivor", "deliver_payload"))), True)

        await rt.dispatch("launch")
        check("the joint mission completes", await wait_until(
            lambda: all(t.state == "DONE" for t in rt.master.plan.tasks)), True)
    finally:
        await medic.stop()
        await scout.stop()
        await rt.stop()


async def test_relay_join() -> None:
    print("\na relay vehicle joining closes a comms gap mid-mission")
    rt = Runtime(mqtt_port=18847, web_port=18097)
    # A short radio, and a mission far enough out to be beyond it.
    scout = ProtocolDrone(
        manifest("short-scout", ["area_search", "classify_survivor"],
                 constraints=Constraints(endurance_min=200, comms_range_m=3000,
                                         cruise_ms=20, max_speed_ms=30)),
        "127.0.0.1", 18847, speedup=25)
    relay = ProtocolDrone(
        manifest("late-relay", ["relay_comms"],
                 constraints=Constraints(endurance_min=200, comms_range_m=40000,
                                         cruise_ms=20, max_speed_ms=30)),
        "127.0.0.1", 18847, speedup=25)
    await rt.start()
    await scout.start()
    try:
        check("the scout registers",
              await wait_until(lambda: rt.master.registry.get("short-scout")), True)
        await rt.dispatch("search grid J9 for survivors")
        check_true("no relay is tasked while none is on station",
                   not any(t.verb == "relay_comms" for t in rt.master.plan.tasks))
        check_true("and the comms shortfall is reported",
                   any(g.needed == "comms_range" for g in rt.master.plan.gaps))

        await relay.start()
        check("the relay is planned in as soon as it announces", await wait_until(
            lambda: any(t.verb == "relay_comms" and t.assignee == "late-relay"
                        for t in rt.master.plan.tasks)), True)
        check("which closes the comms gap", await wait_until(
            lambda: not any(g.needed == "comms_range"
                            for g in rt.master.plan.gaps)), True)
        check_true("and the plan says why the relay is there",
                   any("relay" in n for n in rt.master.plan.notes))
    finally:
        await relay.stop()
        await scout.stop()
        await rt.stop()


async def test_authorization_boundary() -> None:
    print("\nthe human-authorization boundary")
    rt = Runtime(mqtt_port=18846, web_port=18096)
    m = manifest("armed-1", ["area_search", "classify_survivor"])
    m.capabilities.append(CapabilityContract(name="deliver_payload",
                                             requires_human_authorization=True))
    drone = ProtocolDrone(m, "127.0.0.1", 18846, speedup=25)
    await rt.start()
    await drone.start()
    try:
        check("it registers",
              await wait_until(lambda: rt.master.registry.get("armed-1")), True)
        rec = rt.master.registry.get("armed-1")
        check("the gated capability never reaches the planner",
              "deliver_payload" in rec.verbs(), False)
        check("but it is still recorded in the manifest",
              any(c["name"] == "deliver_payload"
                  for c in rec.metadata["capabilities"]), True)

        await rt.dispatch("find survivors in grid E5 and deliver supplies")
        check("so the delivery shows as a gap, not as a plan",
              rt.master.plan.verdict, "INSUFFICIENT")
        check_true("naming the withheld capability",
                   any(g.needed == "deliver_payload" for g in rt.master.plan.gaps))

        # Second gate: even bound directly, the dispatcher will not send it.
        rt.master.plan.tasks[0].verb = "deliver_payload"
        rt.master.plan.tasks[0].assignee = "armed-1"
        rt.master.plan.tasks[0].depends_on = []
        rt.master.plan.tasks[0].state = "PENDING"
        rt.master.mission_active = True
        await rt.master._dispatch_ready()
        check("the dispatcher refuses it outright",
              rt.master.plan.tasks[0].state, "FAILED")
        check_true("and says why",
                   "human authorization" in rt.master.plan.tasks[0].note)
    finally:
        await drone.stop()
        await rt.stop()


async def main() -> int:
    test_manifest()
    await test_remote_mission()
    await test_vehicle_side_refusal()
    await test_duplicate_order()
    await test_failover()
    await test_dynamic_join()
    await test_relay_join()
    await test_authorization_boundary()
    print(f"\n  {PASS} passed, {FAIL} failed\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
