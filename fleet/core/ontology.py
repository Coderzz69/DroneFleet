"""Domain packs: the ONLY mission-specific part of the system.

A pack declares a vocabulary of verbs (each with what it requires and what it
produces) plus policy rules. Swap the pack, get a different mission domain --
with zero engine changes.

Packs are YAML. PyYAML is used when installed; otherwise a small parser here
handles the restricted subset the packs are written in (nested maps, block
lists, inline [a, b] lists, scalars). No third-party dependency required.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

PACK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "packs")


# --------------------------------------------------------------------------
# minimal YAML subset parser (used only if PyYAML is absent)
# --------------------------------------------------------------------------
def _scalar(tok: str) -> Any:
    tok = tok.strip()
    if tok.startswith("[") and tok.endswith("]"):
        inner = tok[1:-1].strip()
        return [_scalar(x) for x in inner.split(",")] if inner else []
    if len(tok) >= 2 and tok[0] in "'\"" and tok[-1] == tok[0]:
        return tok[1:-1]
    low = tok.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~", ""):
        return None
    try:
        return int(tok)
    except ValueError:
        pass
    try:
        return float(tok)
    except ValueError:
        return tok


def _mini_yaml(text: str) -> Any:
    lines = []
    for raw in text.splitlines():
        if "#" in raw:  # packs never quote a '#', so a naive strip is safe here
            raw = raw.split("#", 1)[0]
        if raw.strip():
            lines.append((len(raw) - len(raw.lstrip()), raw.strip()))

    def block(i: int, indent: int):
        """Parse one block at `indent`; return (value, next_index)."""
        if i < len(lines) and lines[i][1].startswith("- "):
            out = []
            while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
                body = lines[i][1][2:].strip()
                if ":" in body and not body.startswith("["):
                    #  "- id: foo"  starts an inline map; siblings follow indented
                    k, _, v = body.partition(":")
                    item = {k.strip(): _scalar(v)}
                    i += 1
                    while i < len(lines) and lines[i][0] > indent and not lines[i][1].startswith("- "):
                        k2, _, v2 = lines[i][1].partition(":")
                        item[k2.strip()] = _scalar(v2)
                        i += 1
                    out.append(item)
                else:
                    out.append(_scalar(body))
                    i += 1
            return out, i
        out = {}
        while i < len(lines) and lines[i][0] == indent:
            key, _, val = lines[i][1].partition(":")
            key = key.strip()
            if val.strip():
                out[key] = _scalar(val)
                i += 1
            else:
                i += 1
                if i < len(lines) and lines[i][0] > indent:
                    out[key], i = block(i, lines[i][0])
                else:
                    out[key] = {}
        return out, i

    value, _ = block(0, lines[0][0]) if lines else ({}, 0)
    return value


def _load_yaml(path: str) -> dict:
    with open(path) as fh:
        text = fh.read()
    try:
        import yaml  # optional
        return yaml.safe_load(text)
    except ImportError:
        return _mini_yaml(text)


# --------------------------------------------------------------------------
# pack model
# --------------------------------------------------------------------------
@dataclass
class Verb:
    name: str
    requires: list[str] = field(default_factory=list)
    produces: list[str] = field(default_factory=list)
    sensors_any: list[str] = field(default_factory=list)
    duration_per_km2: float = 0.0
    fixed_duration_s: float = 20.0
    description: str = ""


@dataclass
class Policy:
    id: str
    applies_to: str          # verb name this rule guards
    kind: str                # fresh | requires_link | exclusive | human_approval
    token: str = ""
    ttl_s: float = 600.0
    severity: str = "blocking"   # blocking | degraded
    message: str = ""


@dataclass
class Pack:
    domain: str
    verbs: dict[str, Verb]
    policies: list[Policy]
    keywords: list[str] = field(default_factory=list)
    subject: str = ""        # what this domain looks for: "survivors", "intruders"

    def produces_index(self) -> dict[str, list[str]]:
        idx: dict[str, list[str]] = {}
        for v in self.verbs.values():
            for token in v.produces:
                idx.setdefault(token, []).append(v.name)
        return idx


# Core vocabulary every pack inherits, so a drone registered once is reusable
# across domains and `area_search` / `area_scan` never fragment into two verbs.
CORE_VERBS = {
    "loiter": Verb("loiter", [], ["station_held"], [], 0.0, 30.0, "Hold position on station"),
    "relay_comms": Verb("relay_comms", [], ["comms_link"], [], 0.0, 999.0, "Act as an airborne radio repeater"),
    "area_search": Verb("area_search", ["region"], ["contacts"], ["thermal", "eo_ir", "rgb", "sar"], 1.4, 30.0,
                        "Sweep a region and report contacts"),
    "track": Verb("track", ["contacts"], ["track_lock"], ["eo_ir", "rgb"], 0.0, 25.0, "Maintain a lock on a contact"),
    "assess": Verb("assess", ["contacts"], ["assessment"], ["eo_ir", "rgb"], 0.0, 20.0, "Post-action assessment"),
}


def load_pack(name: str) -> Pack:
    path = os.path.join(PACK_DIR, f"{name}.yaml")
    if not os.path.exists(path):
        raise FileNotFoundError(f"no pack named {name!r} in {PACK_DIR}")
    raw = _load_yaml(path) or {}

    verbs = {k: Verb(**{**vars(v)}) for k, v in CORE_VERBS.items()}
    for vname, vbody in (raw.get("verbs") or {}).items():
        vbody = vbody or {}
        base = verbs.get(vname)
        verbs[vname] = Verb(
            name=vname,
            requires=vbody.get("requires", base.requires if base else []) or [],
            produces=vbody.get("produces", base.produces if base else []) or [],
            sensors_any=vbody.get("sensors_any", base.sensors_any if base else []) or [],
            duration_per_km2=vbody.get("duration_per_km2", base.duration_per_km2 if base else 0.0),
            fixed_duration_s=vbody.get("fixed_duration_s", base.fixed_duration_s if base else 20.0),
            description=vbody.get("description", base.description if base else ""),
        )

    policies = []
    for p in raw.get("policies") or []:
        policies.append(Policy(
            id=p.get("id", "policy"),
            applies_to=p.get("applies_to", ""),
            kind=p.get("kind", "fresh"),
            token=p.get("token", ""),
            ttl_s=float(p.get("ttl_s", 600)),
            severity=p.get("severity", "blocking"),
            message=p.get("message", ""),
        ))

    return Pack(domain=raw.get("domain", name), verbs=verbs, policies=policies,
                keywords=raw.get("keywords") or [],
                subject=str(raw.get("subject") or ""))


def available_packs() -> list[str]:
    if not os.path.isdir(PACK_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(PACK_DIR) if f.endswith(".yaml"))


def infer_pack(prompt: str) -> str:
    """Pick a domain from the mission prompt. Always reported back to the user,
    never a silent choice -- and overridable with `domain <name>`."""
    best, best_score = "search_and_rescue", 0
    low = prompt.lower()
    for name in available_packs():
        try:
            pack = load_pack(name)
        except Exception:
            continue
        score = sum(1 for kw in pack.keywords if kw in low)
        if score > best_score:
            best, best_score = name, score
    return best
