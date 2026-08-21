#!/usr/bin/env python3
"""Watch the fleet's MQTT bus from outside the app.

This is a SEPARATE PROCESS that connects to the broker over TCP and subscribes
like any other client. Nothing here imports the simulation, the master or the
world -- if it prints traffic, the traffic is real.

    python3 tools/mqtt_tail.py                    # everything except heartbeats
    python3 tools/mqtt_tail.py --heartbeats       # everything
    python3 tools/mqtt_tail.py -t 'fleet/drone/+/inbox'
    python3 tools/mqtt_tail.py --types TASK_ASSIGN,TASK_COMPLETE
    python3 tools/mqtt_tail.py --raw              # show the bytes on the wire
    python3 tools/mqtt_tail.py --port 18997       # a non-default broker

Equivalent to `mosquitto_sub -t 'fleet/#' -v`, for machines without it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet.net.mqtt_client import MQTTClient      # noqa: E402  (path set above)

C = {"reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
     "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
     "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m"}

TYPE_COLOR = {
    "CAPABILITY_QUERY": "cyan", "CAPABILITY_ANNOUNCE": "cyan",
    "TASK_ASSIGN": "blue", "TASK_ACK": "green", "TASK_REJECT": "red",
    "TASK_PROGRESS": "dim", "TASK_COMPLETE": "green",
    "TELEMETRY": "dim", "HEARTBEAT": "dim",
    "ALERT": "yellow", "ABORT": "red", "RECALL": "magenta",
}


def paint(s: str, colour: str, on: bool) -> str:
    return f"{C.get(colour, '')}{s}{C['reset']}" if on and colour else s


def summarise(env: dict) -> str:
    """One line of the interesting bits, per message type."""
    t, p = env.get("type", ""), env.get("payload") or {}
    if t == "CAPABILITY_ANNOUNCE":
        verbs = ",".join(c.get("verb", "?") for c in p.get("capabilities", []))
        return f"[{verbs}] sensors={'/'.join(p.get('sensors') or []) or 'none'}"
    if t == "TASK_ASSIGN":
        r = (p.get("params") or {}).get("region") or {}
        where = f" region=({r.get('x',0):.0f},{r.get('y',0):.0f} {r.get('w',0):.0f}x{r.get('h',0):.0f})" if r else ""
        return f"{p.get('verb','')}{where} est={p.get('est_duration_s',0):.0f}s"
    if t == "TASK_PROGRESS":
        return f"{round(p.get('progress', 0) * 100)}% {p.get('activity','')}"
    if t == "TASK_COMPLETE":
        return f"{p.get('verb','')} -> {json.dumps(p.get('result') or {})[:70]}"
    if t in ("TASK_ACK", "ABORT"):
        return p.get("verb", "")
    if t == "TASK_REJECT":
        return f"reason={p.get('reason','')}"
    if t == "TELEMETRY":
        return (f"({p.get('x',0):.0f},{p.get('y',0):.0f}) {p.get('speed_ms',0):.1f}m/s "
                f"bat={p.get('battery_pct',0):.0f}% link={p.get('link_via','')}")
    if t == "HEARTBEAT":
        return f"bat={p.get('battery_pct',0):.0f}% {p.get('status','')}"
    if t == "RECALL":
        return p.get("reason", "")
    return json.dumps(p)[:80] if p else ""


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-t", "--topic", default="fleet/#", help="topic filter")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--heartbeats", action="store_true",
                    help="include HEARTBEAT and TELEMETRY (noisy)")
    ap.add_argument("--types", default="", help="comma-separated types to keep")
    ap.add_argument("--raw", action="store_true", help="also print the JSON payload")
    ap.add_argument("--all", action="store_true",
                    help="include the UI feeds (console, mission/plan) too")
    ap.add_argument("--no-colour", action="store_true")
    args = ap.parse_args()

    colour = not args.no_colour and sys.stdout.isatty()
    keep = {t.strip().upper() for t in args.types.split(",") if t.strip()}
    noisy = {"HEARTBEAT", "TELEMETRY"}
    # these carry UI text, not protocol envelopes -- they have no src/dst and
    # would render as "? -> ?"
    ui_topics = {"fleet/console", "fleet/mission/plan", "fleet/world/tick"}
    seen = {"n": 0}

    client = MQTTClient(f"tail-{os.getpid()}", args.host, args.port)

    async def on_msg(topic: str, payload: bytes) -> None:
        if topic in ui_topics and not args.all:
            return
        try:
            env = json.loads(payload)
        except Exception:                                    # noqa: BLE001
            print(f"{time.strftime('%H:%M:%S')}  {topic}  <{len(payload)}B non-JSON>")
            return
        mtype = env.get("type", "")
        if keep and mtype not in keep:
            return
        if not args.heartbeats and not keep and mtype in noisy:
            return
        seen["n"] += 1

        short = topic.replace("fleet/", "")
        line = (f"{paint(time.strftime('%H:%M:%S'), 'dim', colour)}  "
                f"{paint(f'{mtype:<20}', TYPE_COLOR.get(mtype, ''), colour)} "
                f"{paint(env.get('src','?'), 'bold', colour)} → {env.get('dst','?')}"
                f"{'  [' + env['corr_id'] + ']' if env.get('corr_id') else ''}"
                f"  {summarise(env)}"
                f"   {paint(short, 'dim', colour)}")
        print(line, flush=True)
        if args.raw:
            print(paint("    " + json.dumps(env), "dim", colour), flush=True)

    try:
        await client.connect()
    except Exception as exc:                                 # noqa: BLE001
        print(f"could not reach the broker at {args.host}:{args.port} — is the app "
              f"running?\n  {exc}", file=sys.stderr)
        return 1

    await client.subscribe(args.topic, on_msg)
    print(f"subscribed to {args.topic} on {args.host}:{args.port} "
          f"— ctrl-c to stop\n", file=sys.stderr)
    try:
        while client.connected:
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        pass
    finally:
        await client.disconnect()
        print(f"\n{seen['n']} messages", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print()
