#!/usr/bin/env python3
"""Publish a message onto the fleet bus from outside the app.

Proof the bus is two-way: nothing here touches the simulation, it just puts a
packet on the broker and the real drones answer it.

    # ask every drone to re-announce itself, then watch them reply
    python3 tools/mqtt_send.py discover

    # order one drone home
    python3 tools/mqtt_send.py recall drone-2

    # stop everything
    python3 tools/mqtt_send.py abort

    # anything at all
    python3 tools/mqtt_send.py raw fleet/broadcast '{"type":"CAPABILITY_QUERY","src":"cli"}'

Run `tools/mqtt_tail.py` in another terminal to see the responses.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet.core.messages import Envelope, MsgType    # noqa: E402
from fleet.net import topics                          # noqa: E402
from fleet.net.mqtt_client import MQTTClient          # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["discover", "recall", "abort", "raw"])
    ap.add_argument("target", nargs="?", default="",
                    help="drone id for `recall`, or topic for `raw`")
    ap.add_argument("body", nargs="?", default="", help="JSON payload for `raw`")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    args = ap.parse_args()

    client = MQTTClient(f"cli-{os.getpid()}", args.host, args.port)
    try:
        await client.connect()
    except Exception as exc:                              # noqa: BLE001
        print(f"could not reach the broker at {args.host}:{args.port} — is the app "
              f"running?\n  {exc}", file=sys.stderr)
        return 1

    if args.action == "discover":
        topic = topics.BROADCAST
        data = Envelope(type=MsgType.CAPABILITY_QUERY, src="cli").to_json()
    elif args.action == "abort":
        topic = topics.BROADCAST
        data = Envelope(type=MsgType.ABORT, src="cli").to_json()
    elif args.action == "recall":
        if not args.target:
            print("recall needs a drone id, e.g. `recall drone-2`", file=sys.stderr)
            return 2
        topic = topics.drone_inbox(args.target)
        data = Envelope(type=MsgType.RECALL, src="cli", dst=args.target,
                        payload={"reason": "recalled from the command line"}).to_json()
    else:                                                  # raw
        if not args.target or not args.body:
            print("raw needs a topic and a JSON body", file=sys.stderr)
            return 2
        topic = args.target
        data = json.dumps(json.loads(args.body)).encode()

    await client.publish(topic, data)
    await asyncio.sleep(0.3)                               # let it flush
    await client.disconnect()
    print(f"published to {topic}\n  {data.decode()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
