# CLAUDE.md

Working notes for this repo. The [README](README.md) is the user manual — this
file is the stuff you only learn by breaking it.

## What this is

Master/slave drone coordination over MQTT with a **deterministic feasibility
engine**, a physics sim, and a GBA-style overworld UI. The contribution is the
protocol and the feasibility engine; the graphics are a wrapper.

## Commands

```bash
python3 run.py                      # LLM on by default, self-starting
python3 run.py --no-llm             # deterministic parser only
python3 run.py --port 8090 --mqtt-port 18090

python3 -m tests.test_planner       # 58 assertions · no AI, no network, no UI
python3 -m tests.test_integration   # 29 assertions · real broker, real physics
python3 -m tests.test_llm           # 28 assertions · real Ollama, skips if absent
FLEET_LLM_MODEL=gemma2:2b python3 -m tests.test_llm

python3 tools/mqtt_tail.py          # watch the bus from a separate process
python3 tools/mqtt_send.py discover # drive the fleet from outside
```

There is **no pytest**. Tests are plain scripts with a `check()` helper that
print `ok`/`FAIL` and exit non-zero. Keep it that way — zero dependencies is a
hard constraint (see below).

## Hard constraints

**No third-party dependencies.** Not a preference. The MQTT 3.1.1 broker, MQTT
client, WebSocket server and YAML subset parser are all hand-written here
because the target machine had none of them. Before reaching for a library,
assume it is not installed. PyYAML is used *if present* and falls back to
`ontology._mini_yaml` otherwise.

**Python 3.10+** (as the README states; not tested below that — developed on
3.14). Modules with annotations all start with
`from __future__ import annotations`.

## Architecture invariants

These are load-bearing. Violating one silently destroys the point of the
project rather than causing an error.

1. **`fleet/core/` imports nothing from `net/`, `web/` or `sim/`.**
   It is the whole engine and must stay testable with no broker and no browser.
   `test_planner` depends on this.

2. **The planner decides feasibility. A model never does.**
   `nlp.LLM_ADAPTER` may *propose*; `planner._validate()` re-checks everything
   regardless of source. If you find yourself asking the model whether something
   is possible, stop.

3. **Packs are the only mission-specific part.**
   Adding a domain must be a YAML file, not a code change. `world.py` and
   `master.py` name no verbs at all. Two deliberate exceptions, both guarded:
   - `planner._ensure_relay()` names `relay_comms`, so a fleet that owns a
     relay actually gets it tasked when the target is out of radio range. It
     no-ops if the pack has no such verb.
   - `drone._actionable()` encodes "do not intercept a friendly" and
     "do not airdrop to a hostile".

   Anything beyond these belongs in the pack.

4. **The frontend is a pure observer.**
   It holds no mission state, only receives events and sends command strings.
   That discipline is what would let the same page drive real hardware.

5. **Ground truth ≠ what the master knows.**
   `world.DroneState` is omniscient simulator truth. `master.DroneView` is
   belief built *only* from received messages. They must stay separate — the
   divergence when a link drops is the demo. A contact's `truth` field is
   stripped in `world.snapshot()` and must never reach the browser.

6. **Hazards are scene-setting.** They never affect feasibility, timing or
   physics. `spec.hazard` is display-only.

## Layout

```
fleet/core/     engine — no I/O
  messages.py     Envelope + MsgType (the whole wire vocabulary)
  ontology.py     Pack loading, CORE_VERBS, mini-YAML parser
  registry.py     DroneRecord, Constraints
  planner.py      backward chaining → binding → deterministic validation
  nlp.py          text→struct; keyword parser + LLM merge; LIMITS clamping
  llm.py          Ollama adapter, prompts, schemas, guard rails, daemon start
fleet/net/      transport only (broker, client, topics)
fleet/agents/   master.py (planning, dispatch, interlocks, DroneView)
                drone.py  (one autonomous agent per drone)
fleet/sim/      world.py — kinematics, wind, power, link budget, sensing
fleet/web/      server.py (HTTP+WS), runtime.py (the only module wiring all layers)
frontend/       app.js is ~1000 lines of canvas + panels, vanilla, no build step
packs/          *.yaml — the domain vocabularies
tools/          mqtt_tail.py, mqtt_send.py — external bus access
```

## Gotchas that cost real debugging time

**MQTT fixed header needs `<< 4`.** Packet type lives in the *high* nibble.
Writing `bytes([PUBLISH | flags])` produces a packet nothing decodes, and the
symptom is a silent no-op — subscriptions match, nothing arrives.

**Waypoint capture radius must exceed per-tick travel.** At 22 m/s with a 1.2 s
effective step a drone moves 26 m; a fixed 30 m capture radius makes it orbit
the waypoint forever. It scales with `(speed + wind) * dt`.

**Never let approach speed reach zero.** A drone decelerating to exactly zero at
the capture ring gets held just outside it by a 2.6 m/s crosswind, forever.
`v_floor` keeps it out-flying the wind.

**Wind must not apply to parked drones.** Applying it unconditionally makes
untasked drones sail downwind and pin against the map edge while draining
battery. `airborne` gates this; `assign_route` is the only thing that sets it.

**Press Start 2P is an 8×8 bitmap face.** It only renders cleanly at multiples
of 8px. At 7.5px "BAT" reads as "DAT". Snap every font size to the 8px grid.

**A class selector beats the UA `[hidden]` rule.** `.hud{display:flex}` made
`hidden` do nothing, so the verdict banner never hid. `style.css` restates
`.hud[hidden]{display:none}` explicitly.

**HTML collapses runs of spaces.** The `prompts` guide aligns columns with
spaces, so `.line` needs `white-space: pre-wrap`.

**`.gitignore` has no trailing comments.** `*.log  # comment` is a literal
pattern matching nothing. Comments must be on their own line.

**Python heredocs and JS comments don't mix.** A `//` line inside a
`python3 - <<'PY'` block is a SyntaxError that aborts the whole script before
any edit lands — and the failure looks like "nothing happened".

**Whitespace-sensitive string replacement.** Much of this codebase was edited by
script. `const rec = droneRecord(...)` vs `const rec=droneRecord(...)` silently
no-matches. Always verify the replacement count.

## LLM integration

Off-limits to the model: coordinates, feasibility, scheduling, physics. It is
asked only which verbs a sentence implies, which sensors, the callsign, word-form
quantities, and which objective is the real goal.

Five guard rails, in `llm.py` and `nlp.py`:

1. **Grammar-constrained decoding** — the pack's verbs go in the JSON Schema
   `enum`, so the sampler cannot emit a verb the pack does not define.
2. **Evidence grounding** — every capability must quote the description
   verbatim; unquotable ones are dropped.
3. **Support checking** (`_supports`) — the quote must actually point at the
   claimed verb. The model justified `relay_comms` with *"8km radio"*, which is
   in the text but does not mean relaying.
4. **Regex owns numbers** — `extract_numbers()` wins wherever it fires; the
   model only fills word-form gaps. Everything is then clamped by `LIMITS`.
5. **Re-validation** — the planner re-checks regardless.

Bias is deliberately toward **dropping** a capability. A missing one is visible
and explained; a phantom one silently makes an impossible mission look possible.

Default model is `qwen2.5:1.5b-instruct` (~1.8 s/call) not `gemma2:2b`
(~34 s/call) — identical accuracy on the fixtures, 19× faster, and every `add`
blocks on it. LLM calls run in `asyncio.to_thread`; never on the event loop.

## Conventions

- Comments explain **why**, especially where a naive implementation is wrong.
  Several comments in `world.py` and `llm.py` exist to stop a future edit
  reintroducing a fixed bug. Do not strip them.
- New behaviour gets a test in the matching suite. Physics and parsing go in
  `test_planner`; anything needing the bus goes in `test_integration`.
- Verify UI changes by screenshotting headless Firefox against a running
  instance. Three real bugs were only visible that way.
- When adding a message type: `MsgType` → drone handler → master handler →
  `DroneView` if it changes belief → `tools/mqtt_tail.py` summarise().

## Things deliberately not done

- No QoS 1/2, sessions, wills or auth in the broker. Not needed here.
- No 6-DOF attitude, rotor aerodynamics or terrain in the physics. They cost a
  lot and change nothing about the protocol.
- Terrain on the map is decorative — a "flood zone" is not really wet, and the
  region is the only spatial fact the planner knows.
- No build step for the frontend. Vanilla JS, no bundler, no framework.
