"""Wiring. Broker + world + master + drone agents + web bridge.

This is the only module that knows about all the layers at once. Everything
below it (core/, net/, agents/, sim/) stays independently testable.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from ..agents.drone import DroneAgent
from ..agents.master import Master
from ..core.messages import Envelope
from ..core import llm as llm_mod
from ..core import nlp
from ..core.nlp import parse_drone
from ..core.nlp import resolve_verb
from ..core.ontology import available_packs, infer_pack, load_pack
from ..core.planner import INSUFFICIENT
from ..net import topics
from ..net.mqtt_broker import MQTTBroker
from ..net.mqtt_client import MQTTClient
from ..sim.world import World
from .server import WebServer, WSConnection

log = logging.getLogger("runtime")

TICK_HZ = 10.0
SIM_SPEED = 12.0       # simulated seconds per real second
UI_HZ = 10.0

HELP = [
    "add <description>      register a drone from plain English",
    "list                   show the fleet",
    "remove <id>            drop a drone",
    "<mission prompt>       ask for a plan (e.g. 'find survivors north of the river')",
    "launch                 run the current plan",
    "abort                  recall everyone",
    "why                    explain the last verdict",
    "retry                  re-evaluate the last mission prompt",
    "domain <name>          force a pack: " + ", ".join(available_packs()),
    "kill <id>              fault injection: drone loss",
    "loss <pct>             fault injection: packet loss",
    "lag <seconds>          fault injection: extra latency",
    "llm on|off|status      use the local Ollama model for parsing",
    "llm model <tag>        switch model (e.g. qwen2.5:1.5b-instruct)",
    "demo                   load a 3-drone rescue fleet",
    "clear                  reset everything",
]

DEMO_FLEET = [
    "a drone called Sweeper with a thermal camera that sweeps wide areas and "
    "identifies survivors, 60 min endurance, 8km radio",
    "a drone called Courier that carries and drops a 5kg medical kit, 35 min endurance",
    "a drone called Relay that hovers high up as a radio repeater, 12km range",
]


class Runtime:
    def __init__(self, mqtt_host="127.0.0.1", mqtt_port=1883,
                 web_host="127.0.0.1", web_port=8080, embed_broker=True) -> None:
        self.mqtt_host, self.mqtt_port = mqtt_host, mqtt_port
        self.embed_broker = embed_broker
        self.broker = MQTTBroker(mqtt_host, mqtt_port) if embed_broker else None
        self.world = World()
        self.pack = load_pack("search_and_rescue")
        self.master = Master(self.world, self.pack, mqtt_host, mqtt_port)
        self.agents: dict[str, DroneAgent] = {}
        self.observer = MQTTClient("ui-bridge", mqtt_host, mqtt_port)
        static = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "frontend")
        self.web = WebServer(static, web_host, web_port)
        self.web.on_command = self.handle_command
        self.web.on_connect = self.on_connect
        self.last_prompt = ""
        self.history: list[dict] = []      # recent console lines, replayed to new tabs
        self.wire_history: list[dict] = []  # recent bus traffic, replayed to new tabs
        self.llm = None                     # OllamaAdapter when enabled
        self.llm_model = llm_mod.DEFAULT_MODEL
        self.llm_host = llm_mod.DEFAULT_HOST
        self._tasks: list[asyncio.Task] = []

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        if self.broker:
            await self.broker.start()
        await self.master.start()
        await self.observer.connect()
        await self.observer.subscribe(topics.ALL, self._relay)
        await self.web.start()
        self._tasks.append(asyncio.create_task(self._sim_loop()))
        self._tasks.append(asyncio.create_task(self._ui_loop()))
        await self.master.console("system", "Fleet command online. Type `demo` then `help`.")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for a in self.agents.values():
            await a.stop()
        await self.master.stop()
        await self.observer.disconnect()
        if self.broker:
            await self.broker.stop()

    async def on_connect(self, conn: WSConnection) -> None:
        await conn.send_json({"ev": "hello", "packs": available_packs(),
                              "domain": self.pack.domain, "help": HELP})
        for line in self.history[-60:]:
            await conn.send_json({"ev": "console", "data": {**line, "replay": True}})
        for w in self.wire_history[-80:]:
            await conn.send_json({"ev": "wire", "topic": w["topic"], "data": w["data"]})
        await conn.send_json({"ev": "world", "data": self.world.snapshot()})
        await self.master.push_plan()

    # -- MQTT -> browser -----------------------------------------------------
    async def _relay(self, topic: str, payload: bytes) -> None:
        """Everything on the bus is mirrored to the UI. The browser sees the
        real protocol, not a summary of it."""
        try:
            data = json.loads(payload)
        except Exception:
            return
        if topic == topics.CONSOLE:
            self.history.append(data)
            del self.history[:-120]
            await self.web.broadcast({"ev": "console", "data": data})
        elif topic == topics.PLAN_UPDATE:
            await self.web.broadcast({"ev": "plan", "data": data})
        elif topic.endswith("/telemetry"):
            return          # too chatty for the wire log; the world tick covers it
        elif topic == topics.WORLD_TICK:
            return
        else:
            self.wire_history.append({"topic": topic, "data": data})
            del self.wire_history[:-80]
            await self.web.broadcast({"ev": "wire", "topic": topic, "data": data})

    # -- loops ---------------------------------------------------------------
    async def _sim_loop(self) -> None:
        dt = 1.0 / TICK_HZ
        try:
            while True:
                await asyncio.sleep(dt)
                events = self.world.tick(dt * SIM_SPEED)
                for ev in events:
                    if ev["kind"] == "contact_found":
                        await self.master.console(
                            "contact", f"Contact {ev['contact']} detected by "
                                       f"{self.world.drones[ev['by']].name}")
                    elif ev["kind"] == "link_change":
                        st = self.world.drones.get(ev["drone"])
                        if st:
                            await self.master.console(
                                "link", f"{st.name} radio link "
                                        f"{'restored via ' + ev['via'] if ev['ok'] else 'LOST'}")
                    elif ev["kind"] == "drone_lost":
                        st = self.world.drones.get(ev["drone"])
                        if st:
                            await self.master.console("lost", f"{st.name}: {ev['why']}")
        except asyncio.CancelledError:
            pass

    async def _ui_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(1.0 / UI_HZ)
                if self.web.connections:
                    await self.web.broadcast({"ev": "world", "data": self.world.snapshot()})
        except asyncio.CancelledError:
            pass

    # -- commands ------------------------------------------------------------
    async def handle_command(self, cmd: str, msg: dict) -> None:
        text = (msg.get("text") or "").strip()
        if cmd == "input":
            await self.dispatch(text)
        elif cmd == "select":
            pass   # selection is purely a frontend concern

    async def say(self, kind: str, text: str, **extra) -> None:
        await self.master.console(kind, text, **extra)

    async def dispatch(self, text: str) -> None:
        if not text:
            return
        await self.say("you", text)
        low = text.lower().strip()
        word = low.split()[0] if low.split() else ""

        if word in ("help", "?"):
            for line in HELP:
                await self.say("help", line)
            return

        if word == "demo":
            await self.dispatch("clear")
            for d in DEMO_FLEET:
                await self._add_drone(d)
            await self.say("system", "Demo fleet ready. Try: find survivors in the north flood zone")
            return

        if word == "clear":
            for a in list(self.agents.values()):
                await a.stop()
            self.agents.clear()
            for d in self.master.registry.all():
                self.master.registry.remove(d.id)
                self.world.despawn(d.id)
            self.master.plan = None
            self.master.mission_active = False
            self.world.contacts.clear()
            self.world.coverage.clear()
            self.world.hazard = {}
            await self.say("system", "Everything reset.")
            await self.master.push_plan()
            return

        if word == "add":
            await self._add_drone(text[3:].strip())
            return

        if word == "list":
            if not len(self.master.registry):
                await self.say("system", "No drones registered. Try `demo`.")
            for d in self.master.registry.all():
                await self.say("fleet", f"{d.id} \"{d.name}\" — [{', '.join(sorted(d.verbs()))}] · "
                                        f"{'/'.join(d.sensors) or 'no sensors'} · "
                                        f"{d.constraints.endurance_min:.0f}min · "
                                        f"{d.constraints.comms_range_m/1000:.0f}km")
            return

        if word == "remove" and len(low.split()) > 1:
            did = low.split()[1]
            if self.master.registry.remove(did):
                agent = self.agents.pop(did, None)
                if agent:
                    await agent.stop()
                self.world.despawn(did)
                await self.say("system", f"Removed {did}.")
                await self.master.push_plan()
            else:
                await self.say("error", f"No drone {did}.")
            return

        if word == "domain" and len(low.split()) > 1:
            name = low.split()[1]
            if name not in available_packs():
                await self.say("error", f"Unknown pack. Available: {', '.join(available_packs())}")
                return
            await self.say("system", f"Domain pack → {name}")
            await self._switch_pack(name)
            return

        if word == "llm":
            parts = text.split()
            await self._llm_cmd(parts[1].lower() if len(parts) > 1 else "status",
                                " ".join(parts[2:]))
            return

        if word in ("launch", "run", "go"):
            if not self.master.plan:
                await self.say("error", "No plan yet. Describe a mission first.")
            elif self.master.plan.verdict == INSUFFICIENT:
                await self.say("error", "Cannot launch an INSUFFICIENT plan. Fix the gaps first.")
            else:
                await self.master.launch()
            return

        if word == "abort":
            await self.master.abort()
            return

        if word == "why":
            plan = self.master.plan
            if not plan:
                await self.say("system", "Nothing evaluated yet.")
                return
            await self.say("verdict", f"{plan.verdict} — domain {plan.domain}, goal {plan.goal}",
                           verdict=plan.verdict)
            for t in plan.tasks:
                dep = f" after {','.join(t.depends_on)}" if t.depends_on else ""
                who = t.assignee_name or "UNASSIGNED"
                await self.say("plan", f"{t.id} {t.verb} → {who}{dep} "
                                       f"(~{t.est_duration_s:.0f}s){' · ' + t.note if t.note else ''}")
            for g in plan.gaps:
                await self.say("gap", f"[{g.severity}] {g.reason}: {g.why}"
                                      + (f" → {g.suggestion}" if g.suggestion else ""),
                               severity=g.severity)
            for n in plan.notes:
                await self.say("note", n)
            return

        if word == "retry":
            if not self.last_prompt:
                await self.say("error", "Nothing to retry.")
                return
            await self._evaluate(self.last_prompt)
            return

        # -- fault injection --
        if word == "kill" and len(low.split()) > 1:
            did = low.split()[1]
            if self.world.kill(did):
                await self.say("fault", f"{did} disabled.")
            else:
                await self.say("error", f"No live drone {did}.")
            return

        if word == "loss" and len(low.split()) > 1:
            try:
                pct = float(low.split()[1].rstrip("%"))
            except ValueError:
                await self.say("error", "Usage: loss 30")
                return
            for a in self.agents.values():
                a.mqtt.drop_rate = pct / 100.0
            self.master.mqtt.drop_rate = pct / 100.0
            await self.say("fault", f"Packet loss set to {pct:.0f}% on every radio.")
            return

        if word == "lag" and len(low.split()) > 1:
            try:
                secs = float(low.split()[1].rstrip("s"))
            except ValueError:
                await self.say("error", "Usage: lag 2")
                return
            for a in self.agents.values():
                a.mqtt.extra_latency_s = secs
            self.master.mqtt.extra_latency_s = secs
            await self.say("fault", f"Added {secs:.1f}s latency to every link.")
            return

        # anything else is a mission prompt
        await self._evaluate(text)

    # -- llm ------------------------------------------------------------------
    def _sync_llm_pack(self) -> None:
        """The verb enum in the grammar comes from the pack, so it must follow
        a domain switch."""
        if self.llm:
            self.llm.pack = self.pack
            nlp.LLM_ADAPTER = self.llm

    async def enable_llm(self, model: str = None, host: str = None) -> bool:
        self.llm_model = model or self.llm_model
        self.llm_host = host or self.llm_host
        adapter = await asyncio.to_thread(
            llm_mod.build, self.pack, self.llm_model, self.llm_host)
        if adapter is None:
            probe = llm_mod.OllamaAdapter(self.pack, self.llm_model, self.llm_host)
            _, why = await asyncio.to_thread(probe.available)
            self.llm = None
            nlp.LLM_ADAPTER = None
            await self.say("error", f"LLM off — {why}")
            return False
        self.llm = adapter
        nlp.LLM_ADAPTER = adapter
        await self.say("llm", f"LLM on — {self.llm_model} via {self.llm_host}. "
                              f"Verbs are grammar-constrained to the "
                              f"{self.pack.domain} pack.")
        return True

    async def _llm_cmd(self, arg: str, rest: str = "") -> None:
        if arg == "model" and rest:
            await self.enable_llm(model=rest.strip())
        elif arg == "on":
            await self.enable_llm()
        elif arg == "off":
            self.llm = None
            nlp.LLM_ADAPTER = None
            await self.say("system", "LLM off — using the deterministic parser.")
        else:
            if self.llm:
                await self.say("llm", f"LLM on · {self.llm_model} · "
                                      f"{self.llm.calls} calls, "
                                      f"{self.llm.failures} fallbacks"
                                      + (f" · last error: {self.llm.last_error}"
                                         if self.llm.last_error else ""))
            else:
                await self.say("system", "LLM off. `llm on` to enable "
                                         f"({self.llm_model} via Ollama).")

    async def _switch_pack(self, name: str) -> None:
        """Load a pack and re-read every drone against the new vocabulary.

        A capability record is only meaningful relative to a pack. The durable
        truth is what the operator typed, which is why `source_text` is kept --
        so a fleet registered before the domain was known is not stranded
        holding verbs that no longer exist."""
        if name == self.pack.domain:
            return
        self.pack = load_pack(name)
        self.master.pack = self.pack
        self._sync_llm_pack()

        changed = []
        for rec in self.master.registry.all():
            if not rec.source_text:
                continue
            fresh = await asyncio.to_thread(
                parse_drone, rec.source_text, self.pack, rec.id, 0)
            fresh.name = rec.name          # a callsign is not pack-specific
            if fresh.verbs() != rec.verbs():
                changed.append((rec.name, sorted(rec.verbs()), sorted(fresh.verbs())))
            self.master.registry.add(fresh)        # replaces in place, by id
            self.world.records[rec.id] = fresh     # the physics reads this too
            agent = self.agents.get(rec.id)
            if agent:
                agent.rec = fresh
                await agent._announce()            # re-announce on the bus

        for name_, before, after in changed:
            await self.say("system", f"{name_} re-read for {self.pack.domain}: "
                                     f"[{', '.join(before) or '—'}] → "
                                     f"[{', '.join(after) or '—'}]")
        if changed:
            await self.master.push_plan()

    # -- helpers -------------------------------------------------------------
    async def _add_drone(self, description: str) -> None:
        if not description:
            await self.say("error", "Usage: add <description of the drone>")
            return
        did = self.master.registry.next_id()
        if self.llm:
            await self.say("llm", f"[{self.llm_model}] reading that description…")
        # a 2B model on CPU takes seconds -- keep it off the event loop or the
        # whole simulation stutters while it thinks
        rec = await asyncio.to_thread(
            parse_drone, description, self.pack, did, len(self.master.registry))

        if not rec.capabilities:
            elsewhere = self._pack_that_knows(description)
            if elsewhere:
                await self.say("error", f"No {self.pack.domain} capability in that "
                                        f"description — but {elsewhere} has one. "
                                        f"Try `domain {elsewhere}` first.")
            else:
                await self.say("error", f"Could not find a capability in that description. "
                                        f"Known verbs: {', '.join(sorted(self.pack.verbs))}")
            return

        self.master.register(rec)
        self.world.spawn(rec)
        agent = DroneAgent(rec, self.world, self.mqtt_host, self.mqtt_port)
        await agent.start()
        self.agents[did] = agent

        await self.say("register", f"✓ {did} \"{rec.name}\" — "
                                   f"[{', '.join(sorted(rec.verbs()))}] · "
                                   f"{'/'.join(rec.sensors) or 'no sensors'} · "
                                   f"{rec.constraints.endurance_min:.0f}min · "
                                   f"swath {rec.swath_m():.0f}m")
        for u in rec.unmapped_text:
            await self.say("warn", f"  not understood: {u}")
        await self.master.push_plan()

        if self.last_prompt:
            await self._evaluate(self.last_prompt, quiet_prompt=True)

    def _pack_that_knows(self, description: str) -> str:
        """Which other domain would understand this drone? Turns a dead end
        into a one-line fix instead of a guessing game."""
        for name in available_packs():
            if name == self.pack.domain:
                continue
            try:
                other = load_pack(name)
            except Exception:
                continue
            probe = parse_drone(description, other, "probe", 0)
            if probe.capabilities:
                return name
        return ""

    async def _evaluate(self, prompt: str, quiet_prompt: bool = False) -> None:
        if not len(self.master.registry):
            await self.say("error", "No drones registered yet — describe some first, or type `demo`.")
            return

        self.last_prompt = prompt
        inferred = infer_pack(prompt)
        if inferred != self.pack.domain:
            await self.say("system", f"[domain: {inferred}] — override with `domain <name>`")
            await self._switch_pack(inferred)

        if self.llm:
            await self.say("llm", f"[{self.llm_model}] reading the mission order…")
        plan = await asyncio.to_thread(self.master.evaluate, prompt)
        verdict = plan.verdict
        await self.say("verdict", f"{verdict}", verdict=verdict)

        if verdict == "FEASIBLE":
            await self.say("plan", f"{len(plan.tasks)} tasks across "
                                   f"{len({t.assignee for t in plan.tasks if t.assignee})} drones. "
                                   f"Type `launch` to fly it, or `why` for the breakdown.")
        else:
            for g in plan.gaps:
                await self.say("gap", g.why, severity=g.severity)
                if g.suggestion:
                    await self.say("suggest", f"→ {g.suggestion}", severity=g.severity)
            if verdict == "DEGRADED":
                await self.say("plan", "Runnable, with the risk above. `launch` to accept it.")
        for n in plan.notes:
            await self.say("note", n)
        await self.master.push_plan()
