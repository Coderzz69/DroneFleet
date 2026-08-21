#!/usr/bin/env python3
"""DroneFleet — master/slave drone coordination over MQTT.

    python3 run.py                 # embedded broker + web UI on :8080
    python3 run.py --port 9000
    python3 run.py --mqtt-host localhost --mqtt-port 1883 --no-embed-broker

No third-party dependencies. Open the printed URL.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import webbrowser

from fleet.web.runtime import Runtime


async def main() -> None:
    ap = argparse.ArgumentParser(description="DroneFleet simulation")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--mqtt-host", default="127.0.0.1")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--no-embed-broker", action="store_true",
                    help="use an external MQTT broker instead of the built-in one")
    ap.add_argument("--llm", action="store_true",
                    help="use a local Ollama model for text parsing")
    ap.add_argument("--llm-model", default="gemma2:2b", help="Ollama model tag")
    ap.add_argument("--llm-host", default="http://127.0.0.1:11434")
    ap.add_argument("--open", action="store_true", help="open a browser automatically")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)-14s %(message)s", datefmt="%H:%M:%S")

    rt = Runtime(mqtt_host=args.mqtt_host, mqtt_port=args.mqtt_port,
                 web_host=args.host, web_port=args.port,
                 embed_broker=not args.no_embed_broker)
    rt.llm_model, rt.llm_host = args.llm_model, args.llm_host
    await rt.start()
    if args.llm:
        await rt.enable_llm(args.llm_model, args.llm_host)

    url = f"http://{args.host}:{args.port}"
    print(f"\n  DroneFleet ready  →  {url}")
    print(f"  MQTT bus on {args.mqtt_host}:{args.mqtt_port}"
          f"{' (embedded)' if not args.no_embed_broker else ''}")
    print(f"  Parser: {'LLM ' + args.llm_model if args.llm else 'deterministic'}"
          f"  (toggle in the app with `llm on` / `llm off`)")
    print("  Watch the raw protocol:  mosquitto_sub -t 'fleet/#' -v\n")
    if args.open:
        webbrowser.open(url)

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await rt.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  shutting down.")
        sys.exit(0)
