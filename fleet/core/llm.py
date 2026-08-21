"""Local LLM adapter — Ollama, a small model, no cloud, no API key.

This is the *only* place a model touches the system, and it is deliberately
boxed in three ways:

1. **Grammar-constrained decoding.** The JSON schema sent to Ollama puts the
   loaded pack's verbs in an `enum`. The sampler cannot emit a token sequence
   outside that set, so the model physically cannot invent a capability. This
   is enforced at the decoder, not merely checked afterwards.
2. **Coercion.** Whatever comes back is still passed through the pack's
   `resolve_verb`, so a stale or wrong verb lands in `unmapped_text`.
3. **Re-validation.** `planner.py` re-checks the whole plan regardless of who
   proposed it. The model never decides feasibility.

The model is asked only for the things language is good for: which verbs a
sentence implies, what the numbers are, what to call the drone. It is never
asked for coordinates, feasibility, or scheduling.

Enable with:  python3 run.py --llm            (default model: gemma2:2b)
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any, Optional

from . import nlp
from .nlp import HAZARDS
from .ontology import Pack

log = logging.getLogger("llm")

DEFAULT_HOST = "http://127.0.0.1:11434"
# qwen2.5:1.5b matched gemma2:2b for accuracy on the fixture set and is
# ~19x faster on CPU, which is the difference between usable and not
# in an interactive UI. `llm model gemma2:2b` switches at runtime.
DEFAULT_MODEL = "qwen2.5:1.5b-instruct"
SENSORS = ["thermal", "eo_ir", "rgb", "lidar", "radar"]
DIRECTIONS = ["north", "south", "east", "west",
              "northeast", "northwest", "southeast", "southwest", "center"]


class OllamaAdapter:
    """Callable matching the `nlp.LLM_ADAPTER` contract: (text, mode) -> dict."""

    def __init__(self, pack: Pack, model: str = DEFAULT_MODEL,
                 host: str = DEFAULT_HOST, timeout: float = 150.0) -> None:
        self.pack = pack
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.calls = 0
        self.failures = 0
        self.last_error = ""

    # -- transport -----------------------------------------------------------
    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.host + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())

    def available(self) -> tuple[bool, str]:
        """Is the daemon up and is the model actually pulled?"""
        try:
            req = urllib.request.Request(self.host + "/api/tags")
            with urllib.request.urlopen(req, timeout=5) as resp:
                tags = json.loads(resp.read())
            names = [m.get("name", "") for m in tags.get("models", [])]
            if not any(n == self.model or n.split(":")[0] == self.model.split(":")[0]
                       for n in names):
                return False, (f"model {self.model!r} not pulled — "
                               f"run: ollama pull {self.model}")
            return True, f"{self.model} ready on {self.host}"
        except Exception as exc:                      # noqa: BLE001
            return False, f"ollama unreachable at {self.host} ({exc})"

    def _generate(self, system: str, prompt: str, schema: dict,
                  max_tokens: int = 320) -> dict:
        self.calls += 1
        try:
            out = self._post("/api/generate", {
                "model": self.model,
                "system": system,
                "prompt": prompt,
                "format": schema,        # grammar-constrained decoding
                "stream": False,
                "keep_alive": "15m",
                "options": {"temperature": 0, "num_predict": max_tokens,
                            "top_p": 0.9, "repeat_penalty": 1.0},
            })
            return json.loads(out.get("response") or "{}")
        except Exception as exc:                      # noqa: BLE001
            self.failures += 1
            self.last_error = str(exc)
            log.warning("LLM call failed (%s) — falling back to the parser", exc)
            raise

    # -- the two jobs --------------------------------------------------------
    def __call__(self, text: str, mode: str) -> dict[str, Any]:
        if mode == "register":
            return self.register(text)
        if mode == "mission":
            return self.mission(text)
        raise ValueError(f"unknown mode {mode!r}")

    # ---- job 1: free text -> capability record -----------------------------
    def _verb_menu(self) -> str:
        lines = []
        for name, v in sorted(self.pack.verbs.items()):
            bits = [f"- {name}"]
            if v.description:
                bits.append(f": {v.description}")
            if v.sensors_any:
                bits.append(f" (needs one of: {', '.join(v.sensors_any)})")
            lines.append("".join(bits))
        return "\n".join(lines)

    def register_system(self) -> str:
        return f"""You extract structured data about ONE drone from a description.

Domain: "{self.pack.domain}". These are the ONLY capabilities that exist:

{self._verb_menu()}

Sensors that exist: {', '.join(SENSORS)}.

For each capability the description gives the drone, output the capability name
and quote the words that show it ("evidence"), copied exactly, under six words.

Rules:
- Use only capabilities from the list above.
- The evidence must be about that capability. Owning a radio is not relaying
  through it; having a camera is not identifying things with it.
- If you cannot quote words for a capability, leave that capability out.
- Never quote from these instructions. Only quote the description.
- name: ONE short callsign word. Never a letter, never a sentence.
- constraints: only for quantities written in words ("about an hour" ->
  endurance_min 60). Digits are read separately; skip those.
- unmapped_text: any stated ability that no capability above can express.

Example description: "a quadcopter that drops a 2kg first aid kit"
Example output: capabilities [{{"verb":"deliver_payload","evidence":"drops a 2kg first aid kit"}}]

Example description: "a drone that hovers high up as a signal repeater"
Example output: capabilities [{{"verb":"relay_comms","evidence":"as a signal repeater"}},
{{"verb":"loiter","evidence":"hovers high up"}}]"""

    def register_schema(self) -> dict:
        verbs = sorted(self.pack.verbs.keys())
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "capabilities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "verb": {"type": "string", "enum": verbs},
                            "evidence": {"type": "string"},
                        },
                        "required": ["verb", "evidence"],
                    },
                },
                "sensors": {"type": "array",
                            "items": {"type": "string", "enum": SENSORS}},
                "constraints": {
                    "type": "object",
                    "properties": {
                        "endurance_min": {"type": "number"},
                        "cruise_ms": {"type": "number"},
                        "comms_range_m": {"type": "number"},
                        "altitude_m": {"type": "number"},
                        "payload_kg": {"type": "number"},
                    },
                },
                "unmapped_text": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name", "capabilities", "sensors", "constraints"],
        }

    def _supports(self, verb: str, evidence: str) -> Optional[str]:
        """Does the citation actually support the capability claimed?

        Grounding catches invented quotes, not misattributed ones: the model
        justified `relay_comms` with "8km radio" -- really in the text, but
        owning a radio is not relaying through it. So require POSITIVE signal:
        the quoted words must map to this verb through the curated hint table,
        or name it outright. Returns None when supported, else the reason.

        The bias is deliberate. A dropped capability is visible to the operator
        and explained; a phantom one silently makes an impossible mission look
        possible, which is the exact failure this project exists to prevent."""
        ev = (evidence or "").lower()
        hinted = {r for r in (nlp.resolve_verb(h, self.pack)
                              for h in nlp._hits(ev, nlp.VERB_HINTS)) if r}
        if verb in hinted:
            return None
        words = set(re.findall(r"[a-z]+", ev))
        if {t for t in verb.split("_") if len(t) > 3} & words:
            return None
        if hinted:
            return f"the quote points at {'/'.join(sorted(hinted))}, not {verb}"
        return f"the quote does not mention {verb.replace('_', ' ')}"

    @staticmethod
    def _grounded(evidence: str, text: str) -> bool:
        """Is the model's quote actually in the description? A 2B model happily
        asserts capabilities nobody mentioned; making it cite chapter and verse,
        then checking the citation, is cheap and deterministic."""
        e = " ".join((evidence or "").lower().split())
        src = " ".join(text.lower().split())
        if len(e) < 3:
            return False
        if e in src:
            return True
        # tolerate light paraphrase: most content words must appear
        words = [w.strip(".,;:!?\"'()") for w in e.split() if len(w) > 3]
        if not words:
            return False
        return sum(w in src for w in words) / len(words) >= 0.6

    def register(self, text: str) -> dict[str, Any]:
        data = self._generate(self.register_system(), f"Drone description:\n{text}",
                              self.register_schema())

        # drop any capability whose citation is not in the source text
        kept, unmapped = [], list(data.get("unmapped_text") or [])
        for item in data.get("capabilities") or []:
            if not isinstance(item, dict):
                kept.append(item)
                continue
            verb, ev = item.get("verb"), item.get("evidence", "")
            if verb == "loiter":
                kept.append(verb)
                continue
            wrong = None if not self._grounded(ev, text) else self._supports(verb, ev)
            if wrong:
                unmapped.append(f"dropped '{verb}': cited {ev!r} — {wrong}")
                log.info("misattributed capability %r (evidence %r): %s", verb, ev, wrong)
            elif self._grounded(ev, text):
                kept.append(verb)
            else:
                unmapped.append(f"dropped '{verb}': the model cited "
                                f"{ev!r}, which is not in the description")
                log.info("ungrounded capability %r (evidence %r) dropped", verb, ev)
        data["capabilities"] = kept
        data["unmapped_text"] = unmapped
        c = {k: float(v) for k, v in (data.get("constraints") or {}).items()
             if isinstance(v, (int, float))}
        if "payload_kg" in c:
            c["mass_kg"] = 3.0 + c["payload_kg"]
        data["constraints"] = c   # nlp.parse_drone clamps, for every source
        return data

    # ---- job 2: free text -> mission spec ----------------------------------
    def mission_system(self) -> str:
        return f"""You convert a plain-English mission order into JSON.

Domain: "{self.pack.domain}". The only objectives that exist:

{self._verb_menu()}

Rules:
- goal_verb is the FINAL objective of the mission, not the first step. \
"find survivors and drop supplies" -> the delivery verb, because searching is \
only how you get there.
- Pick the verb the order literally asks for. Choose an after-the-fact verb \
such as "assess" ONLY if the order explicitly asks for an assessment or damage \
report. An order that just says to search is a search.
- grid: a grid reference like "C4" ONLY if one is stated. Otherwise null.
- direction: the compass word used to describe where the area is, or "center" \
if none is given.
- area_km2: only if a size is stated, else null.
- hazard: the environment the incident is happening in, if the order names one
  (a flood, a fire, an earthquake, a storm, a chemical release). "none" if the
  order does not describe one. Never guess a disaster that is not mentioned.
Never invent a location. If the order does not say where, use direction \
"center" and null grid."""

    def mission_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "goal_verb": {"type": "string", "enum": sorted(self.pack.verbs.keys())},
                "grid": {"type": ["string", "null"]},
                "direction": {"type": "string", "enum": DIRECTIONS},
                "area_km2": {"type": ["number", "null"]},
                "hazard": {"type": "string", "enum": HAZARDS},
            },
            "required": ["goal_verb", "direction"],
        }

    def mission(self, text: str) -> dict[str, Any]:
        return self._generate(self.mission_system(), f"Mission order:\n{text}",
                              self.mission_schema(), max_tokens=90)


def build(pack: Pack, model: str = DEFAULT_MODEL,
          host: str = DEFAULT_HOST) -> Optional[OllamaAdapter]:
    """Construct an adapter, verify it works, and warm the model.

    The first call on CPU pays ~40s of weight loading. Doing that here, at
    enable time, keeps it out of the operator's first command."""
    a = OllamaAdapter(pack, model=model, host=host)
    ok, msg = a.available()
    log.info("LLM: %s", msg)
    if not ok:
        return None
    try:
        a._post("/api/generate", {"model": model, "prompt": "ok", "stream": False,
                                  "keep_alive": "15m",
                                  "options": {"num_predict": 1}})
        log.info("LLM: model warm")
    except Exception as exc:                          # noqa: BLE001
        log.warning("LLM: warm-up failed (%s)", exc)
    return a
