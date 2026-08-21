# DroneFleet — User Manual

Master/slave drone coordination over **MQTT**, with a feasibility engine that
decides whether a mission is *possible* before anything takes off, and a
GBA-era overworld as the operator interface.

You describe drones in plain English. The master works out whether they can
actually do the job — and if they can't, it tells you exactly what is missing.

```
python3 run.py          # then open http://127.0.0.1:8080
```

---

## Contents

1. [Quick start](#1-quick-start)
2. [Installation](#2-installation)
3. [The interface](#3-the-interface)
4. [Command reference](#4-command-reference)
5. [Registering drones](#5-registering-drones)
6. [Writing mission prompts](#6-writing-mission-prompts)
7. [Domains](#7-domains)
8. [Reading the verdict](#8-reading-the-verdict)
9. [Running a mission](#9-running-a-mission)
10. [Fault injection](#10-fault-injection)
11. [Local LLM](#11-local-llm)
12. [Configuration](#12-configuration)
13. [Writing your own domain pack](#13-writing-your-own-domain-pack)
14. [How it works](#14-how-it-works)
15. [Tests](#15-tests)
16. [Troubleshooting](#16-troubleshooting)

---

## 1. Quick start

Start it:

```
python3 run.py
```

Open `http://127.0.0.1:8080` and type into the dialogue box at the bottom:

```
demo
find survivors in the north flood zone and deliver supplies
launch
```

Watch the overworld. Three drones fly out, one holds station as a radio relay,
the sweeper runs a lawnmower search pattern, and a survivor appears when it is
actually detected.

Then break it on purpose:

```
kill drone-1
```

`drone-1` is the only drone that can confirm a survivor. The master notices the
missed heartbeats, re-runs the *same* validator on the surviving fleet, and the
verdict flips `FEASIBLE → INSUFFICIENT` live, naming the capability that went
missing.

**The core loop is:** describe a fleet → give a mission → read the verdict →
`why` → add what's missing → `retry` → `launch`.

---

## 2. Installation

### Requirements

Python **3.10+**. That is the entire list.

**No dependencies.** No `pip install`, no broker to install, no web framework.
The MQTT 3.1.1 broker, the MQTT client and the WebSocket server are all
implemented in this repo in pure `asyncio`.

```
cd DroneFleet
python3 run.py
```

### Optional: local LLM

On by default, but entirely optional — if Ollama is not installed the app
starts anyway and uses the deterministic parser. See
[section 11](#11-local-llm).

### Optional: watch the bus from outside

If you have `mosquitto-clients` installed, the protocol is real MQTT:

```
mosquitto_sub -h 127.0.0.1 -p 1883 -t 'fleet/#' -v
```

---

## 3. The interface

```
┌──────────────────────────────────────┬──────────────────┐
│                                      │ PARTY MISSION    │
│           OVERWORLD                  │       PROTOCOL   │
│      (map, drones, contacts)         ├──────────────────┤
│                                      │  side panel      │
├──────────────────────────────────────┴──────────────────┤
│  DIALOGUE BOX — log + command input                     │
└─────────────────────────────────────────────────────────┘
```

### The overworld

| Action | Result |
|---|---|
| **Click a drone** | Opens its summary in the PARTY panel |
| **Click empty ground** | Deselects |
| **Drag** | Pan the camera |
| **Scroll** | Zoom in/out |
| `+` / `−` | Zoom buttons |
| `FOLLOW` | Camera tracks the selected drone |
| `FOG` | Toggle the unswept-area shading |

What you are looking at:

- **Yellow dashed box** — the mission search area. The dimmed part inside it is
  ground the sensors have not covered yet; it clears as the sweep actually
  covers it. This is real coverage, not a timer.
- **Teal circle** under a searching drone — its live sensor footprint, sized
  from altitude and field of view.
- **Faint dotted lines** — radio links. Teal to base means a direct link,
  yellow means it is going through a relay drone.
- **Red pulsing ring** — that drone has lost its radio link.
- **White dashed path** — the drone's remaining waypoints.
- **Grey drone** — lost (killed, or battery dead).
- **Drone inside a dark ring, rotors still** — landed on the pad.

### Drone lifecycle

A registered drone is **parked**, not deployed. On the pad it does not drift,
does not burn battery, and reports `LANDED`. Tasking is the only thing that
launches it.

| Status | Meaning |
|---|---|
| `LANDED` | On the pad. No drift, no power draw |
| `TRANSIT` | Flying to a waypoint |
| `WORKING` | On its final leg, doing the job |
| `ON_STATION` | Holding position |
| `RETURNING` | Idle too long — flying home to land |

Holding station is **not** free hovering: the drone tilts into the wind to
cancel it, holds position exactly over the ground, and is charged for the
airspeed that costs. With nothing to do for 25 simulated seconds it returns to
base and shuts down, so an aborted or finished mission ends with the fleet home
rather than drifting downwind until the batteries die.

### Contacts: two independent axes

A contact's **kind** (what it is) and its **stage** (how far through the
pipeline it is) are encoded separately, so you can read either without
decoding the other.

**Kind sets hue and silhouette:**

| Kind | Marker |
|---|---|
| Unknown | grey hollow circle with `?` |
| Survivor | amber civilian figure |
| Friendly | green civilian figure |
| Hostile | red angular spike, dark core |
| Defect | yellow hazard diamond on the asset — not a person |

**Stage sets how solidly it is painted:**

| Stage | Painting |
|---|---|
| Found, unclassified | outline only, grey, bobbing `!` — identity genuinely unknown |
| Classified | tinted in its kind colour, labelled |
| Served | solid fill plus a white tick |

Before a classify task runs, *every* kind draws as the same hollow `?`. That is
the point: the ground truth exists server-side but is never sent to the browser
until a drone has actually identified it. A legend appears bottom-left listing
only the kinds currently on the map.

Classification can also say **no**. A contact identified as friendly is skipped
by `intercept`, and a hostile is skipped by `deliver_payload` — so the IFF
interlock gates on the answer, not merely on timing.

### Hazards

If the mission order names a disaster, the incident area is drawn as one:

| Hazard | Wording that triggers it | Appearance |
|---|---|---|
| `flood` | flood, inundated, submerged, dam burst, tsunami | standing water with moving wave crests |
| `fire` | fire, wildfire, blaze, burning, smoke | burnt ground, flames, rising smoke |
| `earthquake` | earthquake, quake, rubble, collapsed, landslide | rubble heaps and ground fissures |
| `storm` | storm, hurricane, cyclone, blizzard, gale | driving rain and lightning flashes |
| `chemical` | chemical, hazmat, toxic, contaminated | drifting acid-green cloud on violet |

Hazards are **scene-setting only** — they never affect feasibility, task
timing, or physics. The verdict for "find survivors in the flood zone" is
identical to the same mission without the word "flood". They exist so the
operator can see the situation they were sent into, and so the unswept fog
lightens automatically to keep both readable.

### PARTY panel

Each drone as a party member: sprite, callsign, current status, capabilities,
and a battery bar (green → yellow → red). Click one to select it.

Selecting a drone opens the **summary screen** below the list:

| Section | Contents |
|---|---|
| **OBJECTIVE** | Current task with live progress, and the next task queued, including what it is waiting on |
| **POSITION** | Easting, northing, altitude, heading, speed, distance flown |
| **SYSTEMS** | Battery, link state, signal strength in dBm, radio range, endurance, sensor swath, payload |
| **CAPABILITIES** | The verbs this drone announced |
| **SENSORS** | What it actually carries |

### MISSION panel

The task graph. Verdict and domain at the top, then any gaps (red = blocking,
amber = degraded), then every task with its live state colour-coded down the
left edge: grey pending, amber assigned, teal running, green done, red failed.
Interlocks appear as a note on the task they hold.

### PROTOCOL panel

The raw MQTT traffic, mirrored from the bus. Heartbeats are hidden by default
because they are ~80% of the volume — `HEARTBEATS: ON` shows them. `CLEAR`
empties the log.

### Dialogue box

Type anywhere on the page and focus jumps here.

| Key | Action |
|---|---|
| `Enter` | Send |
| `↑` / `↓` | Command history |

You can deep-link a panel: `http://127.0.0.1:8080/#mission` or `#wire`.

---

## 4. Command reference

Anything that is not a recognised command is treated as a **mission prompt**.

### Fleet

| Command | Description |
|---|---|
| `add <description>` | Register a drone from plain English |
| `list` | Show the fleet with capabilities, sensors and limits |
| `remove <id>` | Remove a drone (e.g. `remove drone-2`) |
| `demo` | Load a ready-made 3-drone rescue team |
| `clear` | Reset everything — fleet, plan, world |

### Planning and execution

| Command | Description |
|---|---|
| *(any other text)* | Evaluate it as a mission prompt |
| `why` | Full breakdown of the current verdict: every task, gap and interlock |
| `retry` | Re-evaluate the last mission prompt against the current fleet |
| `launch` | Run the current plan (aliases: `run`, `go`) |
| `abort` | Broadcast ABORT and recall everyone |
| `domain <name>` | Force a domain pack |

### Fault injection

| Command | Description |
|---|---|
| `kill <id>` | Disable a drone instantly |
| `loss <pct>` | Set packet loss on every radio, e.g. `loss 30` |
| `lag <seconds>` | Add latency to every link, e.g. `lag 2` |

### Local model

| Command | Description |
|---|---|
| `llm status` | Is it on, which model, how many calls and fallbacks |
| `llm on` / `llm off` | Toggle the local model |
| `llm model <tag>` | Switch model, e.g. `llm model qwen2.5:1.5b-instruct` |

### Other

| Command | Description |
|---|---|
| `help` or `?` | Print this list in the dialogue box |

---

## 5. Registering drones

Use `add` followed by a plain English capability description. Include the
things the planner can validate.

### Words that map to capabilities

| Capability | Example wording |
|---|---|
| Search / sweep | `thermal search drone`, `rgb survey aircraft`, `radar patrol drone` |
| Classification | `classify survivors`, `identify friend or foe`, `confirm defects` |
| Payload delivery | `deliver supplies`, `medical kit drop`, `2 kg payload courier` |
| Relay | `radio relay`, `high altitude comms relay`, `extend range` |
| Inspection | `lidar crack measurement`, `corrosion inspection`, `file report` |

### Words that map to sensors and limits

| Field | Example wording |
|---|---|
| Sensors | `thermal`, `eo/ir`, `rgb camera`, `lidar`, `radar` |
| Endurance | `45 min endurance`, `1 hour endurance` |
| Payload | `2 kg payload` |
| Radio range | `8 km radio`, `12 km comms range` |
| Speed | `12 m/s`, `45 km/h` |
| Altitude | `120 m altitude`, `high altitude` |
| Name | `called Sweeper`, `named Relay` |

### Examples

```
add thermal and eo/ir search drone called Spotter with 50 min endurance and 9 km radio
add courier named Mule, deliver payload, medical kit, 3 kg payload, 35 min endurance
add high altitude radio relay called Tower with 1 hour endurance
add lidar inspection drone called Gauge, measure cracks and corrosion, 40 min endurance
```

### Reading the confirmation

Registration always echoes back what it understood:

```
✓ drone-1 "Spotter" — [area_search, classify_survivor, loiter] · thermal/eo_ir · 50min · swath 500m
```

Anything it could **not** map is reported rather than guessed at:

```
  not understood: no recognised capability in this description
  not understood: endurance_min 900000 is out of range — clamped to 600
```

A drone never receives a capability you did not give it. If a capability you
expected is missing, rephrase using the wording table above — that is the
correction loop, and it is faster than it looks.

Values are clamped to physical envelopes (endurance ≤ 600 min, speed ≤ 90 m/s,
radio ≤ 80 km, payload ≤ 200 kg) so a typo cannot poison the physics.

---

## 6. Writing mission prompts

Name the **final** thing you want done. The planner backward-chains the
prerequisites for you. In the rescue domain, `deliver supplies` implies search
first, then survivor confirmation, then delivery — you do not list the steps.

The app responds best when a prompt names:

- the **objective**: search, confirm/classify, deliver, intercept, measure,
  report, guide
- the **area**: a compass direction, a grid reference, or an approximate size
- optionally the **hazard**: flood, fire, earthquake, storm or chemical, which
  is drawn on the map

### Rescue

```
find survivors in the north flood zone and deliver supplies
search grid B3 for casualties
guide ground team to confirmed survivor in a 4 km2 earthquake area
```

### Perimeter security

```
domain perimeter_security
patrol the west perimeter, identify friend or foe, then intercept the intruder
search sector D5 for an unauthorized breach
classify and shadow hostile movement in the northeast zone
```

### Infrastructure inspection

```
domain infrastructure_inspection
inspect the bridge in grid E2, measure any crack, and file a report
survey the south pipeline corridor for corrosion
find and measure a leak in a 2 km2 asset area
```

### Location wording

Use one of these forms:

```
north flood zone          southwest perimeter
grid C4                   sector D5
quadrant A2               4 km2 search area
```

Grid references run `A`–`J` across and `0`–`9` down. If no area is given, the
mission is planned around the centre of the map.

### A good session shape

```
clear
domain search_and_rescue
add thermal search drone called Sweeper, 45 min endurance, 8 km radio
add payload courier called Courier, deliver supplies, 2 kg payload, 35 min endurance
add high altitude relay called Relay, 60 min endurance, 12 km comms range
find survivors in grid C4 and deliver medical supplies
why
launch
```

---

## 7. Domains

A **domain pack** is the vocabulary of a mission type — the verbs that exist,
what each needs and produces, and the safety rules between them. Three ship:

| Domain | Interlock it enforces |
|---|---|
| `search_and_rescue` | Confirm a survivor before an airdrop |
| `perimeter_security` | Identify friend-or-foe before an intercept |
| `infrastructure_inspection` | Measure a defect before filing a report |

The domain is inferred from your prompt and always reported:

```
[domain: search_and_rescue] — override with `domain <name>`
```

Keywords that steer the inference:

| Domain | Keywords that help |
|---|---|
| `search_and_rescue` | rescue, survivor, flood, earthquake, casualty, medical, supplies |
| `perimeter_security` | perimeter, intruder, breach, hostile, intercept, patrol |
| `infrastructure_inspection` | inspect, pipeline, bridge, turbine, corrosion, crack, leak |

When a prompt is ambiguous, set it explicitly with `domain <name>` before the
mission prompt.

### Switching domain re-reads the fleet

A capability record only means something relative to a pack, so when the domain
changes every registered drone is re-read from the description you originally
typed:

```
> domain perimeter_security
  Spotter re-read for perimeter_security: [classify_survivor, loiter] → [classify_iff, loiter]
```

This matters because you usually register drones *before* the mission prompt
that selects the domain. Without it, a drone registered under the default
rescue pack would sit there holding verbs the new domain does not define.

If a description has no capability in the current pack but another pack would
understand it, the error says which:

```
> add a drone that intercepts intruders
  No search_and_rescue capability in that description — but perimeter_security
  has one. Try `domain perimeter_security` first.
```

---

## 8. Reading the verdict

Three outcomes, never two:

| Verdict | Meaning | Can you launch? |
|---|---|---|
| `FEASIBLE` | Every task has a drone, every dependency resolves, all limits hold | Yes |
| `DEGRADED` | Runnable, with a named risk | Yes — you accept the risk |
| `INSUFFICIENT` | A provable hole | No |

`DEGRADED` is the interesting one. It means the mission will run but something
is thin — usually a radio link that will drop mid-sweep, or a drone with no
backup.

### Gap types

| Reason | Meaning |
|---|---|
| `MISSING_CAPABILITY` | No registered drone can do a required task |
| `MISSING_TOKEN` | No verb in this domain produces something a task needs |
| `CONSTRAINT_VIOLATION` | Endurance or radio range does not reach |
| `INTERLOCK` | A safety rule blocks a task until a prerequisite exists |
| `BROKEN_DEPENDENCY` / `CYCLE` | Structural problem in the task graph |

Every gap names what is missing **and** what would fix it:

```
INSUFFICIENT
task T2 needs 'classify_survivor' and no registered drone provides it
→ Add a drone that can 'classify_survivor' with a thermal or eo_ir sensor
target is 6.5 km out; Courier's radio reaches 6.0 km
→ Add a drone that can relay_comms, or accept return-on-signal-loss
```

Run `why` for the full breakdown, including every task, its assignee, its
dependencies and any interlock holding it.

**The verdict is computed by plain deterministic code**, not by a model. When
it says a mission is impossible, that is a proof, not an opinion.

---

## 9. Running a mission

`launch` sends the plan. From there:

1. The master dispatches every task whose dependencies are satisfied.
2. Each drone **acknowledges** or **rejects** on its own terms — a drone with
   battery below reserve refuses the job.
3. Progress streams back while the drone flies.
4. On completion, the task's output token is recorded and dependent tasks
   unlock.

### Interlocks

Safety rules are enforced **twice**: once at plan time, and again at dispatch
time. A clearance that expires while the courier is in transit blocks the drop:

```
T3 deliver_payload → Courier after T2 · interlocked on survivor_confirmed (ttl 600s)
```

If the token is stale when the task is due, the task is held and the reason is
printed. This is deliberate — ordering alone is a suggestion, an interlock is a
lock.

### Live re-validation

The fleet is re-checked whenever it changes. Lose a drone mid-mission and the
verdict is recomputed on what is left, so you can watch
`FEASIBLE → DEGRADED → INSUFFICIENT` happen in real time.

`abort` broadcasts a recall to every drone.

---

## 10. Fault injection

The point of the protocol is what happens when things go wrong, so you can
cause things to go wrong:

```
kill drone-2      disable a drone instantly
loss 30           30% packet loss on every radio
lag 2             two seconds of latency on every link
```

What to watch:

- **`kill`** — heartbeats stop, the watchdog times the drone out after three
  missed beats, its tasks are marked failed, and the plan is re-validated.
- **`loss`** — progress reports get patchy. Task assignment still works because
  the master re-dispatches, but you can see the log thin out.
- **`lag`** — the gap between an assignment and its ACK stretches visibly in
  the PROTOCOL panel.

Radio loss is not simulated with a coin flip: it comes out of the link budget.
Fly a drone past its range with no relay in the air and it genuinely stops
being heard.

---

## 11. Local LLM

**On by default.** A **local** model via Ollama does the text→structure step.
No API key, no cloud, no telemetry. If Ollama is not running the app still
starts and falls back to the deterministic keyword parser — it just says so:

```
Parser: deterministic  (toggle in the app with `llm on` / `llm off`)
```

### Setup

```
export PATH="$HOME/.local/ollama/bin:$PATH"
ollama serve &                          # daemon on 127.0.0.1:11434
ollama pull qwen2.5:1.5b-instruct       # the default

python3 run.py                          # LLM on
python3 run.py --llm-model gemma2:2b    # a 2B model instead
python3 run.py --no-llm                 # deterministic parser only
```

`./start.sh` starts the daemon, pulls the model if missing, and launches.

Toggle live in the dialogue box: `llm on`, `llm off`, `llm status`,
`llm model <tag>`.

### Which model

Measured on this machine, CPU only, no GPU:

| Model | Size | Accuracy | Phantom verbs | Latency |
|---|---|---|---|---|
| `qwen2.5:1.5b-instruct` **(default)** | 1.0 GB | 7/7 | 0 | ~1.8 s/call |
| `gemma2:2b` | 1.6 GB | 7/7 | 0 | ~34 s/call |

Identical accuracy on the fixture set; the 1.5B is nineteen times faster, which
is the difference between usable and not in an interactive UI, so it is the
default. Switch any time with `llm model gemma2:2b`. Calls run in a worker
thread, so the simulation never stalls while the model thinks.

### What it buys you

Negation, which a keyword table structurally cannot handle:

```
"a thermal drone that searches wide areas. it cannot carry or drop anything."

  keyword parser -> [area_search, deliver_payload, loiter]   ← wrong
  local model    -> [area_search, loiter]                    ← correct
```

The table sees "carry" and "drop" and grants a capability the sentence
explicitly denies — and that phantom capability would make an infeasible fleet
look feasible. The model also handles paraphrase the table misses ("acts as a
signal bridge" → `relay_comms`) and picks the real objective out of a compound
order.

### What it is not allowed to do

The model is asked only for what language is genuinely needed for: which verbs
a sentence implies, which sensors, the callsign, quantities written in words,
and which objective is the real goal. It is **never** asked for coordinates,
feasibility, scheduling, or physics.

Five guard rails, in order:

1. **Grammar-constrained decoding.** The JSON schema puts the loaded pack's
   verbs in an `enum`, so the sampler *cannot* emit a verb the pack does not
   define. Enforced at the decoder, not checked afterwards. Switch domain and
   the enum switches with it.
2. **Evidence grounding.** Every capability must come with a verbatim quote
   from your description. If the quote is not in the text, the capability is
   dropped.
3. **Support checking.** The quote must actually point at the claimed verb.
   Grounding catches invented quotes, not misattributed ones — the model
   justified `relay_comms` with *"8km radio"*, which really is in the text, but
   owning a radio is not relaying through it. Sensors get the same treatment.
4. **Numbers are not the model's job.** A regex reads stated quantities
   ("8km radio", "5kg", "60 min") and wins wherever it fires; the model only
   fills gaps like "flies for about an hour". Every value is then clamped to a
   physical envelope, whoever produced it.
5. **Re-validation.** The planner re-checks the entire plan regardless of who
   proposed it.

Anything dropped is shown to you, never silently discarded:

```
✓ drone-1 "Sweeper" — [area_search, classify_survivor, loiter] · thermal · 60min
  not understood: dropped 'relay_comms': cited '60 min endurance'
                  — the quote does not mention relay comms
```

The bias is deliberately toward dropping. A missing capability is visible and
explained; a phantom one silently makes an impossible mission look possible,
which is the exact failure this project exists to prevent.

### Fallback

If Ollama is not running, the model is not pulled, or a call fails or times
out, both parsers fall through to the deterministic path and the app carries
on. That behaviour is asserted in the tests.

### Swapping in something else

`fleet/core/nlp.py` exposes `LLM_ADAPTER`, a plain `(text, mode) -> dict`
callable. `fleet/core/llm.py` implements it for Ollama; anything returning the
same shape drops straight in. The guard rails apply to any implementation,
because they live on the consuming side.

---

## 12. Configuration

### Command-line flags

| Flag | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Web bind address |
| `--port` | `8080` | Web port |
| `--mqtt-host` | `127.0.0.1` | MQTT broker host |
| `--mqtt-port` | `1883` | MQTT broker port |
| `--no-embed-broker` | off | Use an external broker instead of the built-in one |
| `--llm` | on | Use the local model (this is the default) |
| `--no-llm` | off | Skip the model; deterministic parser only |
| `--llm-model` | `qwen2.5:1.5b-instruct` | Ollama model tag |
| `--llm-host` | `http://127.0.0.1:11434` | Ollama endpoint |
| `--open` | off | Open a browser automatically |
| `-v`, `--verbose` | off | Debug logging |

### Using a production broker

```
python3 run.py --no-embed-broker --mqtt-host localhost --mqtt-port 1883
```

Works unchanged against mosquitto, EMQX or HiveMQ — the client speaks standard
MQTT 3.1.1.

### Simulation tunables

In `fleet/web/runtime.py`:

| Constant | Default | Meaning |
|---|---|---|
| `SIM_SPEED` | `12.0` | Simulated seconds per real second |
| `TICK_HZ` | `10.0` | Physics steps per second |
| `UI_HZ` | `10.0` | World snapshots pushed to the browser |

World size, base position and wind live in `fleet/sim/world.py`.

---

## 13. Writing your own domain pack

A pack is the **only** mission-specific part of the system. Nothing in the
protocol, registry, planner or executor mentions any particular mission. Drop a
YAML file in `packs/` and it is picked up automatically.

```yaml
domain: my_domain
keywords: [words, that, hint, at, this, domain]

verbs:
  my_verb:
    requires: [some_token]          # what must exist first
    produces: [another_token]       # what this creates
    sensors_any: [thermal, eo_ir]   # at least one of these
    fixed_duration_s: 30
    duration_per_km2: 1.4           # for area-covering verbs
    description: Shown to the operator and to the model

policies:
  - id: check_before_act
    applies_to: my_verb
    kind: fresh                     # fresh | requires_link | exclusive | human_approval
    token: some_token
    ttl_s: 600
    severity: blocking              # blocking -> INSUFFICIENT, degraded -> DEGRADED
    message: Explanation shown when this rule blocks a task
```

### How it fits together

- **`requires` / `produces` is the whole planner.** Chaining is type matching:
  a task needing `some_token` is wired to whatever verb produces it. A token
  nobody produces is a provable gap.
- **`severity` sets your risk posture.** `blocking` makes a violation fatal;
  `degraded` makes it a warning you can launch through. Same engine, different
  attitude, one word.
- **Core verbs are inherited by every pack** — `loiter`, `relay_comms`,
  `area_search`, `track`, `assess` — so a drone registered once is reusable
  across domains. Redefine one in your pack to override it.

Then just use it:

```
domain my_domain
```

---

## 14. How it works

### Isolation

`fleet/core/` imports nothing from `net/`, `web/` or `sim/`. The browser holds
no mission state and only ever receives events and sends command strings.

```
fleet/
  core/          the engine — no MQTT, no web, no I/O
    messages.py    wire envelope + message types
    ontology.py    domain packs (verbs, policies) + pack loader
    registry.py    fleet records and capabilities
    planner.py     backward chaining, binding, DETERMINISTIC validation
    nlp.py         text -> structure (deterministic parser + LLM merge)
    llm.py         local Ollama adapter, schemas, prompts, guard rails
  net/           transport only
    mqtt_broker.py MQTT 3.1.1 broker (QoS 0)
    mqtt_client.py MQTT client, with drop-rate / latency injection
    topics.py      the whole protocol surface, one file
  agents/
    master.py      discovery, planning, dispatch, interlocks, re-validation
    drone.py       one autonomous agent per drone, its own MQTT connection
  sim/world.py   physics
  web/           HTTP + WebSocket + the MQTT->browser bridge
frontend/        the game (index.html, style.css, app.js) — pure observer
packs/           *.yaml — the only mission-specific part
tests/
```

### The protocol

Every message uses one envelope: `msg_id`, `ts`, `src`, `dst`, `type`,
`corr_id`, `requires_ack`, `payload`. `corr_id` is what ties an ACK, a progress
report and a completion back to the original order.

| Type | Meaning |
|---|---|
| `CAPABILITY_QUERY` / `CAPABILITY_ANNOUNCE` | Discovery |
| `TASK_ASSIGN` / `TASK_ACK` / `TASK_REJECT` | Tasking |
| `TASK_PROGRESS` / `TASK_COMPLETE` | Execution |
| `TELEMETRY` / `HEARTBEAT` / `ALERT` / `ABORT` | Housekeeping |

Task states: `PENDING → ASSIGNED → ACKED → RUNNING → DONE`, with `FAILED` and
`ABORTED` as exits.

### MQTT topics

| Topic | Direction |
|---|---|
| `fleet/broadcast` | master → all drones |
| `fleet/drone/{id}/inbox` | master → one drone |
| `fleet/master/inbox` | drones → master |
| `fleet/drone/{id}/telemetry` | drone → anyone |
| `fleet/mission/plan`, `fleet/console` | runtime → UI (retained) |

### Physics

Modelled, because each of these can flip a verdict:

- **Flight** — acceleration- and turn-rate-limited waypoint following. A drone
  cannot pivot in place, and it bleeds speed to make a tight turn.
- **Wind** — a drifting vector added to airspeed. Upwind legs genuinely take
  longer and cost more battery.
- **Power** — hover draw (≈165 W/kg) plus parasitic drag ∝ v³, draining a
  battery whose capacity is implied by the stated endurance.
- **Radio** — free-space path loss at 2.4 GHz, with relay chaining.
- **Sensing** — ground swath from altitude × field of view, driving both the
  lawnmower search spacing and what actually gets detected.

Deliberately *not* modelled: 6-DOF attitude, rotor aerodynamics, terrain. They
cost a lot and change nothing about the protocol, which is the point.

---

## 15. Tests

```
python3 -m tests.test_planner       # 24 assertions, no AI / network / UI
python3 -m tests.test_integration   # 18 assertions, real broker + real physics
python3 -m tests.test_llm           # 28 assertions, real Ollama (skips if absent)

FLEET_LLM_MODEL=qwen2.5:1.5b-instruct python3 -m tests.test_llm
```

All three pass; `test_llm` passes against both `gemma2:2b` and
`qwen2.5:1.5b-instruct`.

`test_planner` is the important one: the component that decides whether a
mission is possible is plain, deterministic code, so its verdicts are asserted
against fixtures rather than eyeballed.

---

## 16. Troubleshooting

**Port already in use**
```
python3 run.py --port 8090 --mqtt-port 18090
```
If a previous run is still alive, find it with `ss -ltnp | grep 8080` and kill
that PID.

**"No drones registered yet"**
Register some with `add`, or type `demo`.

**A capability I described is missing**
Check the confirmation line and any `not understood:` notes. Rephrase using the
wording table in [section 5](#5-registering-drones). With the LLM on, the note
tells you exactly why it was dropped.

**Verdict is INSUFFICIENT and I don't see why**
Run `why`. Every gap names the missing piece and the fix.

**`launch` refuses**
You cannot launch an `INSUFFICIENT` plan. Fix the gaps and `retry`.
`DEGRADED` plans do launch.

**Drones sit at base doing nothing**
They are waiting on a dependency or an interlock. The MISSION panel shows what
each task is blocked on.

**The map is blank / the page is stuck**
The dialogue box shows `Connection lost. Retrying…` if the backend went away.
Check the terminal running `run.py`.

**LLM is off when I expected it on**
`llm status` reports the model, call count and fallback count. If it says
unreachable, start the daemon with `ollama serve &`. If it says not pulled, run
`ollama pull <tag>`.

**LLM is very slow**
Expected on CPU with `gemma2:2b` (~34 s/call). Switch with
`llm model qwen2.5:1.5b-instruct`.

**The wire log is empty**
Heartbeats are hidden by default and a fresh tab only replays the last 80
messages. Do something — register a drone or launch — and traffic appears.
