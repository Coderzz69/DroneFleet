"""MQTT topic map. The whole protocol surface, in one place.

The version lives in the topic root, not just in the envelope: a v2 fleet can
share a broker with a v1 fleet without either one having to parse and discard
the other's traffic.
"""

VERSION = "v1"
ROOT = f"fleet/{VERSION}"

BROADCAST = f"{ROOT}/broadcast"                      # master -> everyone
MASTER_INBOX = f"{ROOT}/master/inbox"                # drones -> master
ALL = f"{ROOT}/#"                                    # observers (the UI bridge)

# UI-facing, published by the runtime rather than by a drone
WORLD_TICK = f"{ROOT}/world/tick"
PLAN_UPDATE = f"{ROOT}/mission/plan"
CONSOLE = f"{ROOT}/console"

# These carry UI text, not protocol envelopes. Anything reading the bus as a
# protocol stream should skip them rather than render them as "? -> ?".
UI_TOPICS = frozenset({CONSOLE, PLAN_UPDATE, WORLD_TICK})


def drone_inbox(drone_id: str) -> str:
    return f"{ROOT}/drone/{drone_id}/inbox"


ALL_TELEMETRY = f"{ROOT}/drone/+/telemetry"     # master + observers


def drone_telemetry(drone_id: str) -> str:
    return f"{ROOT}/drone/{drone_id}/telemetry"


def shorten(topic: str) -> str:
    """Drop the root for display. `fleet/v1/drone/d-1/inbox` -> `drone/d-1/inbox`."""
    return topic[len(ROOT) + 1:] if topic.startswith(ROOT + "/") else topic
