"""MQTT topic map. The whole protocol surface, in one place."""

ROOT = "fleet/v1"

BROADCAST = f"{ROOT}/broadcast"                      # master -> everyone
MASTER_INBOX = f"{ROOT}/master/inbox"                # drones -> master
ALL = f"{ROOT}/#"                                    # observers (the UI bridge)

# UI-facing, published by the runtime rather than by a drone
WORLD_TICK = f"{ROOT}/world/tick"
PLAN_UPDATE = f"{ROOT}/mission/plan"
CONSOLE = f"{ROOT}/console"


def drone_inbox(drone_id: str) -> str:
    return f"{ROOT}/drone/{drone_id}/inbox"


ALL_TELEMETRY = f"{ROOT}/drone/+/telemetry"     # master + observers


def drone_telemetry(drone_id: str) -> str:
    return f"{ROOT}/drone/{drone_id}/telemetry"
