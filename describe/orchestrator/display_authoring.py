"""
display_authoring.py — "describe a display": the LLM drafts, the engine does everything governable.

design_displays.md §6 / §6.1 (D5), R-30..R-35. The pattern is orchestrator/task_authoring.py:

- The model only ever fills a SMALL schema (AUTHORING_SCHEMA on a percent grid for a first
  request, OPS_SCHEMA for a follow-up) — never the §3 document itself.
- Everything else is deterministic here: grounding (what exists, under a budget), the hand-written
  schema checker and its bounded repair loop, percent → px on the 8 px grid, names → bindings
  (exact, then a UNIQUE prefix, else a visible placeholder — never a search's top hit, never an
  invented id), severity words → ISA-101 tokens, lossless rule thresholds (the model's decimal
  text as written, never through a float), `group` symbols, the tidy pass, the lock rule for
  follow-ups, and the provenance record.
- Dependency-injected: `ground` (duck-typed: search_points / element_by_path / children /
  search_elements / templates — UI3 passes orchestrator.premhist_rest.PremhistRest) and
  `llm_call` (default orchestrator.llm_runtime.call_ollama_chat). Nothing on any runtime path
  calls this; a human publishes what it drafts.

D5.1 (§6.2, Jim's first runs): the user's own name ("call it X") becomes that symbol's label
deterministically; a placeholder keeps its label (the viewer shows "not found: <name>"); a noun no
search matches gets a note; the worked examples use a fictional plant and a binding equal to an
example name is a placeholder; per-kind size caps in the tidy pass (lifted by big / large / full /
whole / fill); repair rounds live in `authoring.repairs`, not in the notes.

The one public entry point is the coroutine `draft_display(...)`.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import re
import time
import uuid
from fractions import Fraction
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# ── Fixed vocabulary ───────────────────────────────────────────────────

CANVAS_W = 1600
CANVAS_H = 900
GRID = 8

KINDS: Tuple[str, ...] = ("value", "trend", "gauge", "bar", "state", "table", "text", "shape", "button",
                          "equipment")
KIND_MEANINGS: Dict[str, str] = {
    "value": "one live number with its units (exactly 1 binding)",
    "trend": "a line chart over time (1-8 bindings)",
    "gauge": "a radial gauge (exactly 1 binding)",
    "bar": "a level / bar (exactly 1 binding)",
    "state": "a digital state as a lamp with its name, e.g. a run state (exactly 1 binding)",
    "table": "rows of name, value, units (bindings, or none = every attribute of the context element)",
    "text": "static text; the label is the text (no bindings)",
    "shape": "a rectangle (decoration; 0 or 1 binding for rules)",
    "button": "a navigation button; the label is its text (no bindings)",
    "equipment": "a plant equipment drawing (pump, valve, tank, ...): set \"glyph\" (see EQUIPMENT); "
                 "0-1 binding (0-2 on the five named there)",
}
# (min, max) bindings per kind in the AUTHORING schema (design_displays §3.4).
BIND_RANGE: Dict[str, Tuple[int, int]] = {
    "value": (1, 1), "trend": (1, 8), "gauge": (1, 1), "bar": (1, 1), "state": (1, 1),
    "table": (0, 64), "text": (0, 0), "shape": (0, 1), "button": (0, 0), "equipment": (0, 2),
}
# The same bounds on the document's symbol types (for rebind ops on any symbol).
_DOC_BIND_MAX: Dict[str, int] = {
    "value": 1, "trend": 8, "gauge": 1, "bar": 1, "state": 1, "table": 64, "text": 0,
    "shape": 1, "image": 1, "button": 0, "group": 0, "repeat": 0, "equipment": 2,
}

# D3.4 / D3.5 (design_displays §5.7.8, §5.7.9): the `equipment` symbol — the 90 glyphs of the
# catalog, their classes, sizes, states, fields and words live in orchestrator/display_glyphs.py
# (Describe's copy of the one table, item 14); re-exported here under their D3.4 names.
from orchestrator.display_glyphs import (  # noqa: E402  (re-exported)
    AREAS, CLASS_STATES, EQUIPMENT_ABS_FLOORS, EQUIPMENT_FLOORS, EQUIPMENT_SIZES, GLYPH_AREA,
    GLYPH_CATALOG_NAMES, GLYPH_CLASS, GLYPH_REF_WORDS, GLYPH_WORDS, GLYPHS, GLYPHS_BY_AREA,
    LETTERS_GLYPHS, LIMITS_GLYPHS, NORMAL_GLYPHS, RANGE_GLYPHS, ROTATIONS, SECOND_BINDING,
    SYNC_GLYPHS, VESSEL_GLYPHS, binding_need, equipment_help, glyph_bind_max, glyph_fields, glyph_of,
    glyph_states, letters_of, letters_valid, request_glyphs,
)
from orchestrator.display_glyphs import GLYPH_ALIASES as _GLYPH_ALIASES  # noqa: E402,F401
from orchestrator.display_glyphs import GLYPH_PHRASES as _GLYPH_PHRASES  # noqa: E402,F401
from orchestrator.display_glyphs import GLYPH_PHRASE_RE as _GLYPH_PHRASE_RE  # noqa: E402,F401
# D3.6 (design_displays §5.7.10 item 9): connected piping — the connect op, the draft pipe
from orchestrator import display_pipes  # noqa: E402

# The outcome log (the counterpart of calc_authoring's calc_describe_log). Guarded, because a
# missing or broken LOG must never be the reason a display cannot be drafted - on an install where
# this module did not land, Describe keeps working and simply writes nothing.
try:                                                                             # noqa: E402
    from orchestrator import display_describe_log as _dlog                       # noqa: E402
except Exception:                                                                # noqa: BLE001
    class _dlog:                                                                 # type: ignore[no-redef]
        @staticmethod
        def record(*_a: Any, **_k: Any) -> None:
            return None

# How notes and the prompt name a glyph ("the pump symbol", "added a control valve symbol 'FV-101'"):
# item 1's Words column.
GLYPH_NAMES: Dict[str, str] = dict(GLYPH_WORDS)


def _kkey(s: Dict[str, Any]) -> str:
    """A symbol's kind as references and "in its place" compare it: its type, and for an
    equipment symbol its glyph too ("equipment:pump")."""
    t = str(s.get("type") or "")
    return f"equipment:{s.get('glyph')}" if t == "equipment" else t


def _kind_noun(s: Dict[str, Any]) -> str:
    """How a note names a symbol's kind: its type ("gauge"), an equipment symbol by its glyph
    ("pump symbol")."""
    t = str(s.get("type") or "symbol")
    if t == "equipment":
        g = s.get("glyph")
        return f"{GLYPH_NAMES[g]} symbol" if g in GLYPH_NAMES else "equipment symbol"
    return t


def _article(noun: str) -> str:
    """`noun` with its indefinite article ("an analog indicator symbol", "an HRSG symbol")."""
    return ("an " if re.match(r"(?:[aeiou]|(?:HRSG|SCR|FGD|MCC)\b)", noun, re.I) else "a ") + noun


def _a_kind(s: Dict[str, Any]) -> str:
    """A symbol's kind noun with its article ("a gauge", "an alarm indicator symbol")."""
    return _article(_kind_noun(s))


def _glyph_words_of(s: Dict[str, Any]) -> frozenset:
    """The reference words that name `s` (an equipment symbol) by its glyph ("pump", "valves")."""
    if s.get("type") != "equipment":
        return frozenset()
    g = s.get("glyph")
    return frozenset(w for w, gs in GLYPH_REF_WORDS.items() if g in gs)


def _eq_matches(word: str, syms: Iterable[Any]) -> List[Dict[str, Any]]:
    """The equipment symbols among `syms` a glyph reference word names ("the pump", "both valves")."""
    gs = GLYPH_REF_WORDS.get(word)
    if not gs:
        return []
    return [s for s in syms if isinstance(s, dict) and s.get("type") == "equipment" and s.get("glyph") in gs]


def decide_glyphs(request: str, symbols: Sequence[Any], existing: Sequence[Any] = ()) -> List[str]:
    """§5.7.8 item 5 — the engine decides the glyph it can decide: for each equipment symbol (in
    order) a glyph the request names wins over a model's glyph that disagrees. A model glyph that
    one of the request's glyph words can mean is kept (and uses up that word); otherwise the glyph
    whose words occur in the symbol's label, else the first glyph word not yet used by an earlier
    symbol, else — when the request names one glyph only — that glyph. A word that alone cannot
    tell ("turbine") decides nothing; on a follow-up, "the <glyph word>" that names an equipment
    symbol already on the display (`existing`) is a reference, not a request for one. Mutates the
    symbols' `glyph`; returns notes-free descriptions of what changed (for tests / provenance)."""
    mentions = request_glyphs(request)
    if existing:
        low = (request or "").lower()
        keep = []
        for gs, d, pos in mentions:
            before = low[max(0, pos - 4):pos]
            # D3.5: "the pump" also names a boiler-feed pump already here (its reference words)
            said = _GLYPH_PHRASE_RE.match(low, pos)
            refs = set(gs)
            for w in re.findall(r"[a-z0-9]+", said.group(0) if said else ""):
                if w != "equipment":
                    refs |= GLYPH_REF_WORDS.get(w, frozenset())
            if before.endswith("the ") and any(isinstance(s, dict) and s.get("type") == "equipment"
                                               and s.get("glyph") in refs for s in _walk(existing)):
                continue
            keep.append((gs, d, pos))
        mentions = keep
    changed: List[str] = []
    if not mentions:
        return changed
    used = [False] * len(mentions)
    eq = [s for s in symbols if isinstance(s, dict) and s.get("kind") == "equipment"]
    agrees: List[bool] = []
    for s in eq:                                       # pass 1: the model glyphs the words can mean
        g = glyph_of(s.get("glyph"))
        ok = g is not None and any(g in gs for gs, _d, _p in mentions)
        agrees.append(ok)
        if ok:
            hit = next((i for i, (gs, _d, _p) in enumerate(mentions) if not used[i] and g in gs), None)
            if hit is not None:
                used[hit] = True
            s["glyph"] = g
    for s, ok in zip(eq, agrees):                      # pass 2: the ones that disagree
        if ok:
            continue
        g = glyph_of(s.get("glyph"))
        pick: Optional[str] = None
        label = str(s.get("label") or "")
        for gs, d, _p in request_glyphs(label):
            if d is not None:
                pick = d
                break
        if pick is None:
            for i, (gs, d, _p) in enumerate(mentions):
                if not used[i] and d is not None:
                    used[i] = True
                    pick = d
                    break
        if pick is None:
            named = {d for _gs, d, _p in mentions if d is not None}
            if len(named) == 1:
                pick = next(iter(named))
        if pick is not None and pick != g:
            changed.append(f"{s.get('glyph')!s} -> {pick}")
            s["glyph"] = pick
        elif g is not None and g != s.get("glyph"):
            s["glyph"] = g
    return changed


def _equipment_of(obj: Any) -> List[Dict[str, Any]]:
    """The equipment symbols of a draft (`symbols`) or of an ops reply's `add`s."""
    if not isinstance(obj, dict):
        return []
    if isinstance(obj.get("symbols"), list):
        syms = obj["symbols"]
    elif isinstance(obj.get("ops"), list):
        syms = [o.get("symbol") for o in obj["ops"] if isinstance(o, dict) and o.get("op") == "add"]
    else:
        return []
    return [s for s in syms if isinstance(s, dict) and s.get("kind") == "equipment"]


def normalize_equipment(obj: Any, request: str = "", existing: Sequence[Any] = ()) -> Any:
    """Deterministic repairs of the equipment symbols of a draft (`symbols`) or an ops reply (the
    `add`s' symbols) before they are checked: a glyph written as the kind ("kind": "pump") → kind
    equipment + that glyph; the glyph text folded (`glyph_of`); then the request's glyph words win
    (`decide_glyphs`); `rotate` given as text → its number."""
    if not isinstance(obj, dict):
        return obj
    if isinstance(obj.get("symbols"), list):
        syms = [s for s in obj["symbols"] if isinstance(s, dict)]
    elif isinstance(obj.get("ops"), list):
        syms = [o["symbol"] for o in obj["ops"] if isinstance(o, dict) and o.get("op") == "add"
                and isinstance(o.get("symbol"), dict)]
    else:
        return obj
    for s in syms:
        k = s.get("kind")
        if isinstance(k, str) and k not in KINDS and glyph_of(k) is not None:
            s.setdefault("glyph", glyph_of(k))
            s["kind"] = "equipment"
        if s.get("kind") != "equipment":
            continue
        if glyph_of(s.get("glyph")) is not None:
            s["glyph"] = glyph_of(s.get("glyph"))
        r = s.get("rotate")
        if isinstance(r, str) and _as_int(r.strip().rstrip("°").strip()) is not None:
            s["rotate"] = _as_int(r.strip().rstrip("°").strip())
    decide_glyphs(request, syms, existing)
    return obj
WINDOWS: Tuple[str, ...] = ("1h", "8h", "24h")
THENS: Tuple[str, ...] = ("amber", "red", "gray", "hide")      # US English (D5.2); "grey" is read too
_THEN_SET: Dict[str, Dict[str, Any]] = {
    "amber": {"severity": 1},
    "red": {"severity": 2},
    "gray": {"fill": "normal"},
    "grey": {"fill": "normal"},
    "hide": {"visible": False},
}
OPS: Tuple[str, ...] = ("add", "remove", "rebind", "restyle", "move", "resize", "rule", "retitle",
                        "align", "place", "match", "distribute", "center", "swap", "scale", "nudge",
                        "set", "clear", "make_room", "connect")
EDGES: Tuple[str, ...] = ("left", "center", "right", "top", "middle", "bottom")
# D5.4 (design_displays §6.5 item 1): placement is per-axis constraints against a reference.
WHERES: Tuple[str, ...] = ("below", "above", "left_of", "right_of", "between", "center_of", "inside",
                           "at", "region")
# §6.6 item 1 as settled at the D3.5 gate: an inside / center_of place is containment (no overlap
# to refuse) only onto a container kind, or when the request itself says so.
_CONTAINERS = frozenset(("shape", "group", "image"))
_CONTAIN_SAID_RE = re.compile(r"\b(?:inside|within|into|centered\s+(?:on|in)|centred\s+(?:on|in)|in\s+the\s+(?:middle|center)\s+of)\b", re.I)
PLACE_ALIGNS: Tuple[str, ...] = ("start", "center", "end")     # the cross axis of a place
# Canvas regions (`of: "canvas:<region>"`): the 3 x 3 grid with a 2-grid-step margin, then the
# halves and thirds (each splits one axis).
REGIONS_3X3: Tuple[str, ...] = ("center", "top", "bottom", "left", "right", "top_left", "top_right",
                                "bottom_left", "bottom_right")
REGIONS_BANDS: Tuple[str, ...] = ("left_half", "right_half", "top_half", "bottom_half", "left_third",
                                  "middle_third", "right_third", "top_third", "bottom_third")
REGIONS: Tuple[str, ...] = REGIONS_3X3 + REGIONS_BANDS
SIZES: Tuple[str, ...] = ("width", "height", "both")
AXES: Tuple[str, ...] = ("horizontal", "vertical", "both")

# design_displays §3.7 — the only colors / fonts / alignments ever written.
TOKENS: Tuple[str, ...] = ("bg", "panel", "line", "fg", "muted", "normal", "sev1", "sev2", "bad", "run")
FONTS: Tuple[str, ...] = ("s", "m", "l", "xl")
ALIGNS: Tuple[str, ...] = ("left", "center", "right")
# §6.3 item 4: the US-English color names a user may say → #rrggbb (the model never writes hex).
COLOR_NAMES: Dict[str, str] = {
    "red": "#d32f2f", "dark red": "#8b0000", "orange": "#f57c00", "amber": "#ffbf00",
    "yellow": "#fdd835", "green": "#2e7d32", "dark green": "#1b5e20", "light green": "#81c784",
    "teal": "#00897b", "cyan": "#00bcd4", "blue": "#1e88e5", "dark blue": "#0d47a1",
    "light blue": "#64b5f6", "navy": "#000080", "purple": "#8e24aa", "magenta": "#d81b60",
    "pink": "#f06292", "brown": "#795548", "white": "#ffffff", "black": "#000000",
    "gray": "#9e9e9e", "grey": "#9e9e9e", "light gray": "#d3d3d3", "light grey": "#d3d3d3",
    "dark gray": "#616161", "dark grey": "#616161", "silver": "#c0c0c0",
}
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

MAX_REQUEST_CHARS = 4000
MAX_TAGS = 80
MAX_ELEMENTS = 40
MAX_TEMPLATES = 10
MAX_PROMPT_TOKENS = 6000           # estimated as characters / 4 (design_displays §6.1)
MAX_TRIES = 3                      # one draft + at most 2 repairs
NUM_PREDICT = 2000
TAGS_PER_NOUN = 20
# No repair round STARTS unless it can finish inside this, as in calc_authoring: ph-server's
# Describe forward gives up at 180 s (PremHist/API.md §6.1 / §11.3.1), and a late answer is worse
# than an honest "couldn't draft this". There was no budget here at all - MAX_TRIES alone, with
# the default 120 s per-call timeout and llm_runtime's retry, allows 486 s of model calls against
# that 180 s deadline, so the forward timed out and the tester saw a failure with no reason.
TOTAL_BUDGET_S = 150.0

NOT_FOUND = "not found: "

try:    # the one definition lives in llm_runtime; the literal is the fallback for a partial install
    from orchestrator.llm_runtime import OLLAMA_TIMEOUT_SENTINEL as _OLLAMA_TIMEOUT_SENTINEL
except Exception:
    _OLLAMA_TIMEOUT_SENTINEL = "My local LLM backend (Ollama) didn't respond"

# A fixed English stop-word list (design_displays §6.1), plus the display-building words that
# name a kind of symbol rather than a thing in the plant.
STOP_WORDS = frozenset("""
a about above across after again against all also am an and any are around as at be because been
before being below between both but by can could did do does doing done down during each either
every few for from further get give had has have having her here hers him his how i if in into is
it its itself just let like make makes made me more most my need new next no nor not now of off on
once one only or other our out over own please put same see set she should show shows showing so
some such than that the their them then there these they this those through to too two three four
under until up use using very want was we were what when where which while who whom why will with
within without would you your yours
display displays overview page screen dashboard panel view chart charts graph graphs trend trends
gauge gauges value values table tables button buttons text label labels shape shapes faceplate
bigger smaller larger left right top bottom middle center centre side half full big small add
remove change move resize put replace rename title called named
""".split())

_WORD_RE = re.compile(r"[\w\-.]+", re.UNICODE)
_NUM_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def name_key(s: str) -> str:
    """PremHist's `name_key` (ph_types::point::name_key): case-insensitive (lowercase)."""
    return (s or "").lower()


# ── The schemas the model fills (Ollama `format`) ───────────────────────

_NUMBER_TEXT = {"type": "string", "description": "a decimal number as text, e.g. \"7.1\""}
_RULE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "when": {
            "type": "object",
            "properties": {"above": _NUMBER_TEXT, "below": _NUMBER_TEXT, "state": {"type": "string"}},
        },
        "then": {"type": "string", "enum": list(THENS)},
    },
    "required": ["when", "then"],
}
_SYMBOL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "label": {"type": "string"},
        "bind": {"type": "array", "items": {"type": "string"}},
        "x": {"type": "number", "minimum": 0, "maximum": 100},
        "y": {"type": "number", "minimum": 0, "maximum": 100},
        "w": {"type": "number", "minimum": 1, "maximum": 100},
        "h": {"type": "number", "minimum": 1, "maximum": 100},
        "rules": {"type": "array", "items": _RULE_SCHEMA},
        "group": {"type": "string"},
        "min": _NUMBER_TEXT,
        "max": _NUMBER_TEXT,
        "props": {"type": "object"},
        # D3.4 (design_displays §5.7.8): an equipment symbol's fields
        "glyph": {"type": "string", "enum": list(GLYPHS)},            # D3.5: the 90
        "rotate": {"type": "integer", "enum": list(ROTATIONS)},
        "flip": {"type": "boolean"},
        "normal": {"type": "object", "properties": {"lo": _NUMBER_TEXT, "hi": _NUMBER_TEXT}},
        "limits": {"type": "object", "properties": {"hi": _NUMBER_TEXT, "lo": _NUMBER_TEXT}},
        "states": {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "integer"}}},
        "letters": {"type": "string"},                                # D3.5 item 3 (instrument bubble)
    },
    "required": ["kind", "bind", "x", "y", "w", "h"],
}
AUTHORING_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "context": {
            "type": "object",
            "properties": {"template": {"type": "string"}, "element": {"type": "string"}},
            "required": ["template", "element"],
        },
        "window": {"type": "string", "enum": list(WINDOWS)},
        "symbols": {"type": "array", "items": _SYMBOL_SCHEMA},
    },
    "required": ["title", "symbols"],
}
_STYLE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "decimals": {"type": "integer", "minimum": 0, "maximum": 9},
        "prefix": {"type": "string"},
        "suffix": {"type": "string"},
        "font": {"type": "string", "enum": list(FONTS)},
        "fill": {"type": "string"},
        "stroke": {"type": "string"},
        "color": {"type": "string"},
        "align": {"type": "string", "enum": list(ALIGNS)},
        "stroke_width": {"type": "integer", "minimum": 0, "maximum": 16},
    },
}
_IDS = {"type": "array", "items": {"type": "string"}}
# A reference (§6.5 item 1): "<id>" | ["<id>", "<id>", …] (their union box) | "canvas" |
# "canvas:<region>".
_REF = {"anyOf": [{"type": "string"}, _IDS]}
_OP_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": list(OPS)},
        "id": {"type": "string"},
        "ids": _IDS,
        "a": {"type": "string"},
        "b": {"type": "string"},
        "to": _REF,
        "of": _REF,
        "from": _REF,
        "edge": {"type": "string", "enum": list(EDGES)},
        "where": {"type": "string", "enum": list(WHERES)},
        "align": {"type": "string", "enum": list(PLACE_ALIGNS)},
        "gap": {"type": "number", "minimum": 0, "maximum": 400},
        "size": {"type": "string", "enum": list(SIZES)},
        "axis": {"type": "string", "enum": list(AXES)},
        "factor": {"type": "number", "minimum": 0.1, "maximum": 10},
        "dx": {"type": "number", "minimum": -100, "maximum": 100},
        "dy": {"type": "number", "minimum": -100, "maximum": 100},
        "props": {"type": "object"},
        "may_shrink": _IDS,
        "symbol": _SYMBOL_SCHEMA,
        "bind": {"type": "array", "items": {"type": "string"}},
        "style": _STYLE_SCHEMA,
        "x": {"type": "number", "minimum": 0, "maximum": 100},
        "y": {"type": "number", "minimum": 0, "maximum": 100},
        "w": {"type": "number", "minimum": 1, "maximum": 100},
        "h": {"type": "number", "minimum": 1, "maximum": 100},
        "rules": {"type": "array", "items": _RULE_SCHEMA},
        "title": {"type": "string"},
    },
    "required": ["op"],
}
OPS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"ops": {"type": "array", "items": _OP_SCHEMA}},
    "required": ["ops"],
}
# D3.6 (§5.7.10 item 9): the connect op's line / label / flow; a draft pipe's from / to / line / flow
_OP_SCHEMA["properties"].update(display_pipes.OP_PROPERTIES)
_SYMBOL_SCHEMA["properties"].update(display_pipes.DRAFT_PROPERTIES)
_SYMBOL_SCHEMA["properties"]["kind"]["enum"].append(display_pipes.DRAFT_KIND)


# ── Lossless JSON reading ──────────────────────────────────────────────

class _Num(str):
    """A JSON number kept as the text the model wrote (json.loads parse_int / parse_float hook),
    so a threshold reaches the document exactly as written and geometry is exact (Fraction)."""


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a number this schema accepts")


def _loads(text: str) -> Any:
    return json.loads(text, parse_int=_Num, parse_float=_Num, parse_constant=_reject_constant)


def _parse_reply(content: str) -> Tuple[Optional[Any], Optional[str]]:
    """The model's reply → (object, None) or (None, error). Fenced or prefixed JSON is accepted
    by taking the outermost {...} — deterministic, never a guess at content."""
    t = (content or "").strip()
    if not t:
        return None, "the reply was empty"
    if t.startswith("```"):
        t = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    try:
        return _loads(t), None
    except ValueError as e:
        first, last = t.find("{"), t.rfind("}")
        if 0 <= first < last:
            try:
                return _loads(t[first:last + 1]), None
            except ValueError:
                pass
        return None, f"the reply is not valid JSON ({e})"


def _is_num(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    return isinstance(v, str) and bool(_NUM_RE.match(v))


def _frac(v: Any) -> Fraction:
    if isinstance(v, Fraction):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return Fraction(str(v))
    return Fraction(str(v))


def _as_int(v: Any) -> Optional[int]:
    if not _is_num(v):
        return None
    f = _frac(v)
    return int(f) if f.denominator == 1 else None


def _num_text(v: Any) -> Optional[str]:
    """A rule threshold's decimal text as written, or None when it is not a plain decimal."""
    if isinstance(v, bool):
        return None
    if isinstance(v, str) and _NUM_RE.match(v):
        return str(v)
    if isinstance(v, int):
        return str(v)
    return None   # a Python float has already lost the text: refused (lossless rule)


def _clean_text(s: Any, limit: int) -> str:
    t = _CTRL_RE.sub(" ", str(s or "")).strip()
    return t[:limit].strip()


def _words(text: str) -> List[str]:
    """Words of letters / digits / - / . / _ of at least 3 characters (edge punctuation off)."""
    out: List[str] = []
    for m in _WORD_RE.findall(text or ""):
        w = m.strip(".-_")
        if len(w) >= 3:
            out.append(w)
    return out


def nouns_of(request: str) -> List[str]:
    """The request's nouns (design_displays §6.1): its words minus the stop words, de-duplicated
    under name_key, in first-seen order."""
    seen: set = set()
    out: List[str] = []
    for w in _words(request):
        k = name_key(w)
        if k in STOP_WORDS or k in seen:
            continue
        seen.add(k)
        out.append(w)
    return out


# ── The hand-written checkers (R-30) ───────────────────────────────────

def _check_rules(rules: Any, at: str, errs: List[str]) -> None:
    if not isinstance(rules, list):
        errs.append(f"{at}: rules must be a list")
        return
    if len(rules) > 16:
        errs.append(f"{at}: at most 16 rules")
    for j, r in enumerate(rules):
        rat = f"{at}.rules[{j}]"
        if not isinstance(r, dict):
            errs.append(f"{rat}: must be an object")
            continue
        when, then = r.get("when"), r.get("then")
        if then not in THENS and then != "grey":
            errs.append(f"{rat}.then: must be one of {', '.join(THENS)}")
        if not isinstance(when, dict):
            errs.append(f"{rat}.when: must be an object with one of above / below / state")
            continue
        keys = [k for k in ("above", "below", "state") if k in when and when[k] not in (None, "")]
        if len(keys) != 1:
            errs.append(f"{rat}.when: exactly one of above / below / state")
            continue
        k = keys[0]
        if k == "state":
            if not isinstance(when[k], str) or not when[k].strip():
                errs.append(f"{rat}.when.state: a state name")
        elif _num_text(when[k]) is None:
            errs.append(f"{rat}.when.{k}: a plain decimal number as text, e.g. \"7.1\" (no units)")


def _check_symbol(s: Any, at: str, errs: List[str], *, geometry_required: bool = True) -> None:
    if not isinstance(s, dict):
        errs.append(f"{at}: must be an object")
        return
    kind = s.get("kind")
    if kind not in KINDS:
        errs.append(f"{at}.kind: must be one of {', '.join(KINDS)}")
    if "label" in s and s["label"] is not None and not isinstance(s["label"], str):
        errs.append(f"{at}.label: must be text")
    bind = s.get("bind", [])
    if bind is None:
        bind = []
    if not isinstance(bind, list) or not all(isinstance(b, str) and b.strip() for b in bind):
        errs.append(f"{at}.bind: a list of attribute or tag names")
        bind = []
    if kind in BIND_RANGE:
        lo, hi = BIND_RANGE[kind]
        if not lo <= len(bind) <= hi:
            want = f"exactly {lo}" if lo == hi else f"{lo} to {hi}"
            errs.append(f"{at}.bind: a {kind} takes {want} binding(s), got {len(bind)}")
    if kind == "button" and not (isinstance(s.get("label"), str) and s["label"].strip()):
        errs.append(f"{at}.label: a button needs a label")
    for k, lo in (("x", 0), ("y", 0), ("w", 1), ("h", 1)):
        if k not in s or s[k] is None:
            if geometry_required:
                errs.append(f"{at}.{k}: required (percent of the canvas)")
            continue
        if not _is_num(s[k]):
            errs.append(f"{at}.{k}: a number {lo}-100")
        elif not lo <= _frac(s[k]) <= 100:
            errs.append(f"{at}.{k}: must be {lo}-100 (percent of the canvas)")
    if "rules" in s and s["rules"] is not None:
        _check_rules(s["rules"], at, errs)
    if "group" in s and s["group"] is not None and not isinstance(s["group"], str):
        errs.append(f"{at}.group: must be text")
    if kind in ("gauge", "bar"):                       # their range (Settled at the D5 gate)
        for k in ("min", "max"):
            if s.get(k) not in (None, "") and _num_text(s[k]) is None:
                errs.append(f"{at}.{k}: a plain decimal number as text (no units)")
    if kind == "equipment":
        _check_equipment(s, at, errs, len(bind))


def _check_equipment(s: Dict[str, Any], at: str, errs: List[str], n_bind: int) -> None:
    """An equipment symbol against the editor's allow-list (design_displays §5.7.8 items 1-2,
    §5.7.9 items 1, 3, 7, R1, R5): `glyph` one of the 90; at most 2 bindings for the five glyphs of
    R5, 1 otherwise; `rotate` 0 / 90 / 180 / 270; `flip` true / false; `min` / `max`, `normal`
    {lo, hi}, `limits` {hi?, lo?} plain decimals (a field written on a glyph that does not take it
    is dropped when mapped, as D3.4); `letters` on an instrument bubble 1-4 letters; `states` only
    the glyph's keys (its class's, plus `synchronized` for the generator and the synchronizing
    point), each a list of integer codes."""
    g = s.get("glyph")
    if g not in GLYPHS:
        errs.append(f"{at}.glyph: one of {', '.join(GLYPHS)}")
        return
    if n_bind > glyph_bind_max(g):
        errs.append(f"{at}.bind: {_article(GLYPH_NAMES[g] + ' symbol')} takes at most {glyph_bind_max(g)} binding(s), "
                    f"got {n_bind}")
    r = s.get("rotate")
    if r is not None and _as_int(r) not in ROTATIONS:
        errs.append(f"{at}.rotate: one of 0, 90, 180, 270")
    if s.get("flip") is not None and not isinstance(s.get("flip"), bool):
        errs.append(f"{at}.flip: true or false")
    for k in ("min", "max"):
        if s.get(k) not in (None, "") and _num_text(s[k]) is None:
            errs.append(f"{at}.{k}: a plain decimal number as text (no units)")
    for k, keys in (("normal", ("lo", "hi")), ("limits", ("hi", "lo"))):
        v = s.get(k)
        if v is None:
            continue
        if not isinstance(v, dict) or any(x not in keys for x in v) or any(
                v.get(x) not in (None, "") and _num_text(v[x]) is None for x in keys):
            errs.append(f"{at}.{k}: {{\"lo\": \"<number>\", \"hi\": \"<number>\"}}, numbers as text")
    lt = s.get("letters")
    if g in LETTERS_GLYPHS and lt not in (None, "") and not (isinstance(lt, str) and letters_valid(lt.strip().upper())):
        errs.append(f"{at}.letters: 1 to 4 letters A-Z (the instrument's function letters, e.g. \"FT\")")
    st = s.get("states")
    if st is not None:
        allowed = glyph_states(g)
        if not isinstance(st, dict) or any(k not in allowed for k in st) or any(
                not isinstance(v, list) or not all(_as_int(c) is not None for c in v) for v in st.values()):
            errs.append(f"{at}.states: " + (f"keys {', '.join(allowed)}, each a list of integer codes"
                                            if allowed else f"{_article(GLYPH_NAMES[g] + ' symbol')} has no states"))


def check_authoring(obj: Any) -> List[str]:
    """Every way `obj` misses AUTHORING_SCHEMA (empty = valid). Geometry may be absent on every
    symbol (the fallback layout then applies); a partial box is an error."""
    normalize_equipment(obj)
    errs: List[str] = []
    if not isinstance(obj, dict):
        return ["the reply must be a JSON object with title and symbols"]
    if not isinstance(obj.get("title"), str) or not _clean_text(obj.get("title"), 200):
        errs.append("title: required text")
    ctx = obj.get("context")
    if ctx is not None:
        if not isinstance(ctx, dict) or not isinstance(ctx.get("element"), str) or not ctx.get("element", "").strip():
            errs.append("context: an object {template, element} naming a listed element")
        elif "template" in ctx and ctx["template"] is not None and not isinstance(ctx["template"], str):
            errs.append("context.template: must be text")
    w = obj.get("window")
    if w is not None and w not in WINDOWS:
        errs.append(f"window: one of {', '.join(WINDOWS)}")
    syms = obj.get("symbols")
    if not isinstance(syms, list) or not syms:
        errs.append("symbols: a non-empty list")
        return errs
    if len(syms) > 200:
        errs.append("symbols: at most 200")
    for i, s in enumerate(syms):
        if isinstance(s, dict) and s.get("kind") == display_pipes.DRAFT_KIND:   # D3.6: no box of its own
            display_pipes.check_draft_pipe(s, f"symbols[{i}]", errs)
            continue
        _check_symbol(s, f"symbols[{i}]", errs, geometry_required=False)
        if isinstance(s, dict):
            have = [k for k in ("x", "y", "w", "h") if s.get(k) is not None]
            if 0 < len(have) < 4:
                errs.append(f"symbols[{i}]: give all of x, y, w, h")
    return errs


def _check_style(st: Any, at: str, errs: List[str]) -> None:
    if not isinstance(st, dict):
        errs.append(f"{at}: must be an object")


def _op_ids(op: Dict[str, Any]) -> List[str]:
    """An op's target ids: `ids`, else `id` (a model may use either for a one-symbol op)."""
    ids = op.get("ids")
    if isinstance(ids, list) and ids:
        return [str(i).strip() for i in ids if isinstance(i, str) and i.strip()]
    if isinstance(op.get("id"), str) and op["id"].strip():
        return [op["id"].strip()]
    return []


def _has_ref(v: Any) -> bool:
    """Whether `v` is a usable reference: a non-empty string or a non-empty list of strings."""
    if isinstance(v, str):
        return bool(v.strip())
    return isinstance(v, list) and any(isinstance(x, str) and x.strip() for x in v)


def _ref_list(v: Any) -> List[str]:
    """A reference → its parts as stripped strings ("s3" → ["s3"]; ["s1", "s2"] → both)."""
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, list):
        return [x.strip() for x in v if isinstance(x, str) and x.strip()]
    return []


def _is_canvas_ref(r: str) -> bool:
    return r == "canvas" or r.startswith("canvas:")


_REGION_ALIASES: Dict[str, str] = {
    "centre": "center", "middle": "center", "centered": "center", "mid": "center",
    "upper_left": "top_left", "upper_right": "top_right", "lower_left": "bottom_left",
    "lower_right": "bottom_right", "left_top": "top_left", "right_top": "top_right",
    "left_bottom": "bottom_left", "right_bottom": "bottom_right", "top_center": "top",
    "bottom_center": "bottom", "center_left": "left", "center_right": "right", "upper": "top",
    "lower": "bottom", "top_middle": "top", "bottom_middle": "bottom", "middle_left": "left",
    "middle_right": "right", "center_third": "middle_third", "upper_half": "top_half",
    "lower_half": "bottom_half", "upper_third": "top_third", "lower_third": "bottom_third",
}


def _region_name(v: Any) -> Optional[str]:
    """A model's region words → one of REGIONS ("canvas:top right" → top_right), else None."""
    if not isinstance(v, str):
        return None
    t = v.strip().lower()
    t = re.sub(r"^canvas\s*[:\-_ ]\s*", "", t)
    t = re.sub(r"^(?:the\s+)?", "", t)
    t = re.sub(r"[\s\-]+", "_", t.replace("centre", "center"))
    t = _REGION_ALIASES.get(t, t)
    return t if t in REGIONS else None


_WHERE_ALIASES: Dict[str, str] = {
    "under": "below", "beneath": "below", "underneath": "below", "below_of": "below",
    "above_of": "above", "left": "left_of", "right": "right_of", "to_the_left_of": "left_of",
    "to_the_right_of": "right_of", "center": "center_of", "centre": "center_of",
    "centered": "center_of", "middle": "center_of", "centered_on": "center_of",
    "center_on": "center_of", "in": "inside", "within": "inside", "into": "inside",
    "between_of": "between", "in_place_of": "at",
}
_ALIGN_ALIASES: Dict[str, str] = {
    "left": "start", "top": "start", "right": "end", "bottom": "end", "middle": "center",
    "centre": "center", "centered": "center", "begin": "start", "finish": "end",
}


def normalize_ops(obj: Any) -> Any:
    """Deterministic repairs of an ops reply before it is checked (a model's near-misses, never a
    guess): `to` ↔ `of` aliases between place / align / match; a `place` without `of` takes the
    `to` of a later align / match on the same id; `id` → `ids` for the multi-symbol ops; D5.4:
    `where` synonyms ("under" → below, "center" → center_of), a region name written as `where`
    (→ region of canvas:<it>), region words folded ("canvas:top right" → canvas:top_right), the
    cross-axis `align` folded (left / top → start, right / bottom → end)."""
    if not isinstance(obj, dict) or not isinstance(obj.get("ops"), list):
        return obj
    display_pipes.normalize_connect_ops(obj["ops"])   # D3.6: an add of kind pipe → connect, aliases
    ops = [o for o in obj["ops"] if isinstance(o, dict)]
    for i, o in enumerate(ops):
        k = o.get("op")
        if k == "add" and isinstance(o.get("symbol"), dict):
            # Settled at the D5.4 live battery: the engine sizes new symbols, so a missing or zero
            # box is not an error (the model wrote w 0, h 0 to mean "you decide"); a missing
            # position means the middle of the canvas (the no-overlap rule then finds room).
            sy = o["symbol"]
            def _n(key: str) -> Optional[Fraction]:
                return _frac(sy[key]) if _is_num(sy.get(key)) else None
            # no position (or "0, 0, 0, 0" = "you decide"): the middle of the canvas, exactly
            if _n("x") is None or _n("y") is None or (_n("x") == 0 and _n("y") == 0 and not (
                    (_n("w") or 0) >= 1 and (_n("h") or 0) >= 1)):
                o["mid"] = True
            for key, dflt in (("x", "45"), ("y", "45"), ("w", "10"), ("h", "10")):
                v = sy.get(key)
                lo = 1 if key in ("w", "h") else 0
                if not _is_num(v) or not lo <= _frac(v) <= 100:
                    sy[key] = _Num(dflt)
            if not isinstance(sy.get("bind"), list):
                sy["bind"] = []
            # a text's words written beside the symbol ("title" on the op — live battery E47)
            if sy.get("kind") == "text" and not any(
                    isinstance(sy.get(x), str) and sy[x].strip() for x in ("label", "text")) and not (
                    isinstance(sy.get("props"), dict) and any(
                        isinstance(sy["props"].get(x), str) and sy["props"][x].strip() for x in ("label", "text"))):
                for x in ("text", "label", "title"):
                    if isinstance(o.get(x), str) and o[x].strip():
                        sy["label"] = o[x].strip()
                        break
        if k == "place":
            w = o.get("where")
            if isinstance(w, str):
                wk = re.sub(r"[\s\-]+", "_", w.strip().lower())
                if wk not in WHERES and _region_name(wk) is not None and not _has_ref(o.get("of")):
                    o["where"], o["of"] = "region", "canvas:" + str(_region_name(wk))
                else:
                    o["where"] = _WHERE_ALIASES.get(wk, wk)
            a = o.get("align")
            if a is not None:
                ak = _ALIGN_ALIASES.get(str(a).strip().lower(), str(a).strip().lower())
                if ak in PLACE_ALIGNS:
                    o["align"] = ak
                else:
                    o.pop("align", None)               # tolerated: the default (center)
        if k == "place" and not _has_ref(o.get("of")):
            if _has_ref(o.get("to")):
                o["of"] = o["to"]
            else:
                me = _op_ids(o)
                for later in ops[i + 1:]:
                    if later.get("op") in ("align", "match") and _op_ids(later) == me \
                            and _has_ref(later.get("to")) and later["to"] != "canvas":
                        o["of"] = later["to"]
                        break
        if k == "place" and o.get("where") == "region":
            # (no `of`: the region the request names is taken at apply time, else the middle)
            r = _region_name(o.get("of")) if isinstance(o.get("of"), str) else None
            if r is None and o.get("of") == "canvas":
                r = "center"
            if r is not None:
                o["of"] = "canvas:" + r
        elif k in ("place", "align") and isinstance(o.get("of" if k == "place" else "to"), str):
            key = "of" if k == "place" else "to"
            v = o[key].strip()
            if v.lower().startswith("canvas") and v.lower() != "canvas":
                r = _region_name(v)
                if r is not None:
                    o[key] = "canvas:" + r
        if k in ("align", "match") and not _has_ref(o.get("to")) and _has_ref(o.get("of")):
            o["to"] = o["of"]
    return obj


def unknown_id_errors(obj: Any, existing: Sequence[str]) -> List[str]:
    """Ids an ops reply names that are neither on the display nor map to one of its `add`s (in
    order, as the engine maps them) — a repairable error, asked once (ruling, D5.2 final battery)."""
    if not isinstance(obj, dict) or not isinstance(obj.get("ops"), list):
        return []
    have = {str(x) for x in existing}
    adds = 0
    alias: Dict[str, int] = {}
    bad: List[str] = []
    for o in obj["ops"]:
        if not isinstance(o, dict):
            continue
        if o.get("op") == "add":
            adds += 1
            continue
        # (a make_room's optional `from` is not checked: an unknown one is simply not used)
        refs = _op_ids(o) + [r for k in ("of", "to", "a", "b") for r in _ref_list(o.get(k))]             + (_ref_list(o.get("from")) if o.get("op") == "connect" else [])
        for r in refs:
            if not r or _is_canvas_ref(r) or r in have:
                continue
            if r not in alias:
                if len(alias) < adds:
                    alias[r] = len(alias)
                elif r not in bad:
                    bad.append(r)
    if not bad:
        return []
    ids = ", ".join(sorted(have, key=lambda x: (len(x), x)))
    return [f"you referred to {r}, which is not on the display and was not added in your reply — include "
            f"the `add` for it before referring to it, or use an existing id: {ids}" for r in bad]


def check_ops(obj: Any) -> List[str]:
    """Every way `obj` misses OPS_SCHEMA (empty = valid)."""
    normalize_ops(obj)
    normalize_equipment(obj)
    if not isinstance(obj, dict) or not isinstance(obj.get("ops"), list):
        return ["the reply must be a JSON object {\"ops\": [...]}"]
    errs: List[str] = []
    for i, op in enumerate(obj["ops"]):
        at = f"ops[{i}]"
        if not isinstance(op, dict) or op.get("op") not in OPS:
            errs.append(f"{at}.op: one of {', '.join(OPS)}")
            continue
        kind = op["op"]
        ids = _op_ids(op)
        if kind in ("rebind", "restyle", "move", "resize", "rule") and len(ids) != 1:
            errs.append(f"{at}.id: the symbol id (e.g. \"s3\")")
        if kind in ("remove", "align", "match", "distribute", "center", "scale", "nudge", "set") and not ids:
            errs.append(f"{at}.ids: the symbol ids (e.g. [\"s3\"])")
        if kind == "add":
            _check_symbol(op.get("symbol"), f"{at}.symbol", errs)
        elif kind == "rebind":
            b = op.get("bind")
            if not isinstance(b, list) or not b or not all(isinstance(x, str) and x.strip() for x in b):
                errs.append(f"{at}.bind: a non-empty list of attribute or tag names")
        elif kind == "restyle":
            _check_style(op.get("style"), f"{at}.style", errs)
        elif kind in ("move", "resize"):
            keys = ("x", "y") if kind == "move" else ("w", "h")
            lo = 0 if kind == "move" else 1
            for k in keys:
                if not _is_num(op.get(k)) or not lo <= _frac(op[k]) <= 100:
                    errs.append(f"{at}.{k}: a number {lo}-100 (percent of the canvas)")
        elif kind == "rule":
            _check_rules(op.get("rules"), at, errs)
        elif kind == "retitle":
            if not isinstance(op.get("title"), str) or not _clean_text(op.get("title"), 200):
                errs.append(f"{at}.title: required text")
        elif kind == "align":
            if op.get("edge") not in EDGES:
                errs.append(f"{at}.edge: one of {', '.join(EDGES)}")
        elif kind == "place":
            if len(ids) != 1:
                errs.append(f"{at}.id: the symbol to place")
            if op.get("where") not in WHERES:
                errs.append(f"{at}.where: one of {', '.join(WHERES)}")
            elif op["where"] == "between":
                refs = [r for r in _ref_list(op.get("of")) if not _is_canvas_ref(r)]
                # fewer than two: taken from the request at apply time ("between the two gauges")
                if len(refs) > 2:
                    errs.append(f"{at}.of: the two symbol ids it goes between, e.g. [\"s1\", \"s2\"]")
            elif op["where"] == "region":
                if _has_ref(op.get("of")) and not (isinstance(op.get("of"), str) and op["of"].startswith("canvas:")
                        and op["of"][7:] in REGIONS):
                    errs.append(f"{at}.of: \"canvas:<region>\", region one of {', '.join(REGIONS)}")
            elif op.get("of") is not None and not _has_ref(op.get("of")):
                errs.append(f"{at}.of: a symbol id, a list of ids, \"canvas\" or \"canvas:<region>\"")
            if op.get("gap") is not None and (not _is_num(op["gap"]) or not 0 <= _frac(op["gap"]) <= 400):
                op.pop("gap", None)                    # tolerated: the default gap
        elif kind == "match":
            if op.get("size") not in SIZES:
                errs.append(f"{at}.size: one of {', '.join(SIZES)}")
        elif kind == "distribute":
            if op.get("axis") not in ("horizontal", "vertical"):
                errs.append(f"{at}.axis: horizontal or vertical")
        elif kind == "center":
            if op.get("axis") is not None and op["axis"] not in AXES:
                errs.append(f"{at}.axis: one of {', '.join(AXES)}")
        elif kind == "swap":
            for k in ("a", "b"):
                if not isinstance(op.get(k), str) or not op[k].strip():
                    errs.append(f"{at}.{k}: a symbol id")
        elif kind == "scale":
            if not _is_num(op.get("factor")) or not Fraction(1, 10) <= _frac(op["factor"]) <= 10:
                errs.append(f"{at}.factor: a number 0.1-10 (bigger = 1.5, smaller = 0.67)")
        elif kind == "nudge":
            for k in ("dx", "dy"):
                v = op.get(k, 0)
                if v is not None and (not _is_num(v) or not -100 <= _frac(v) <= 100):
                    errs.append(f"{at}.{k}: grid steps, -100 to 100")
        elif kind == "set":
            if not isinstance(op.get("props"), dict) or not op["props"]:
                errs.append(f"{at}.props: an object of the properties to set")
        elif kind == "connect":
            errs.extend(display_pipes.check_connect_op(op, at))
        elif kind == "make_room":
            if len(ids) != 1:
                errs.append(f"{at}.id: the symbol to move out of the way")
            if op.get("from") is not None and not _has_ref(op.get("from")):
                op.pop("from", None)                   # tolerated: nothing in particular
        if op.get("may_shrink") is not None and not (
                isinstance(op["may_shrink"], list) and all(isinstance(x, str) for x in op["may_shrink"])):
            op.pop("may_shrink", None)                 # tolerated (battery fix 1)
    return errs


# ── Grounding (R-32) ───────────────────────────────────────────────────

async def _g(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call one `ground` method off the event loop (PremhistRest blocks); await it if async."""
    res = await asyncio.to_thread(fn, *args, **kwargs)
    if inspect.isawaitable(res):
        res = await res
    return res


class _Grounding:
    def __init__(self) -> None:
        self.tags: List[Dict[str, Any]] = []
        self.elements: List[Dict[str, Any]] = []       # in prompt order
        self.named: List[Dict[str, Any]] = []          # named in the request (+ current's context)
        self.templates: List[Dict[str, Any]] = []
        self.notes: List[str] = []
        self.unmatched: List[str] = []                 # request nouns no search found (D5.1)

    def element_by_path_key(self, path: str) -> Optional[Dict[str, Any]]:
        k = name_key(path).rstrip("/")
        for e in self.elements:
            if name_key(e.get("path") or "").rstrip("/") == k:
                return e
        return None

    def find_element(self, ref: str) -> Optional[Dict[str, Any]]:
        """An element named by path (exact under name_key) or by a UNIQUE name among the grounded."""
        ref = (ref or "").strip()
        if not ref:
            return None
        e = self.element_by_path_key(ref)
        if e is not None:
            return e
        hits = [x for x in self.elements if name_key(x.get("name") or "") == name_key(ref.strip("/"))]
        return hits[0] if len(hits) == 1 else None


def _el_id(e: Dict[str, Any]) -> Any:
    return e.get("id") if e.get("id") is not None else e.get("path")


async def _ground(ground: Any, request: str, current: Optional[Dict[str, Any]]) -> _Grounding:
    g = _Grounding()
    nouns = nouns_of(request)

    # Tags: one search per noun, merged, ranked by how many nouns hit (then name, then id).
    by_id: Dict[Any, Dict[str, Any]] = {}
    hits: Dict[Any, int] = {}
    found: Dict[str, int] = {}                     # noun (name_key) → results of both searches
    for n in nouns:
        found.setdefault(name_key(n), 0)
        for p in (await _g(ground.search_points, n) or []):
            if not isinstance(p, dict) or p.get("id") is None or not p.get("name"):
                continue
            found[name_key(n)] += 1
            pid = p["id"]
            by_id.setdefault(pid, p)
            hits[pid] = hits.get(pid, 0) + 1
    ranked = sorted(by_id, key=lambda i: (-hits[i], name_key(str(by_id[i].get("name"))),
                                          str(by_id[i].get("dataset") or ""), str(i)))
    if len(ranked) > MAX_TAGS:
        g.notes.append(f"the assistant was shown the {MAX_TAGS} best-matching of {len(ranked)} tags")
    g.tags = [by_id[i] for i in ranked[:MAX_TAGS]]

    # Elements: every element a request word finds; the most-hit ones are "named".
    els: Dict[Any, Dict[str, Any]] = {}
    ehits: Dict[Any, int] = {}
    for n in nouns:
        for e in (await _g(ground.search_elements, q=n) or []):
            if not isinstance(e, dict) or not e.get("path"):
                continue
            found[name_key(n)] += 1
            k = _el_id(e)
            els.setdefault(k, e)
            ehits[k] = ehits.get(k, 0) + 1
    # D5.1: the nouns NOTHING in this historian matches (both searches empty), in request order.
    g.unmatched = [n for n in nouns if found.get(name_key(n), 0) == 0]

    order: List[Dict[str, Any]] = []
    seen: set = set()

    def put(e: Optional[Dict[str, Any]]) -> None:
        if isinstance(e, dict) and e.get("path") and _el_id(e) not in seen:
            seen.add(_el_id(e))
            order.append(e)

    named: List[Dict[str, Any]] = []
    # current's context element (and fixed elements it binds) are always grounded.
    if isinstance(current, dict):
        paths: List[str] = []
        ctx = current.get("context")
        if isinstance(ctx, dict) and isinstance(ctx.get("default"), str) and ctx["default"]:
            paths.append(ctx["default"])
        for sym in _walk(current.get("symbols") or []):
            for b in sym.get("bind") or []:
                if isinstance(b, dict) and isinstance(b.get("element"), str) and b["element"] not in paths:
                    paths.append(b["element"])
        for p in paths[:10]:
            try:
                e = await _g(ground.element_by_path, p)
            except Exception:
                e = None
            if isinstance(e, dict):
                named.append(e)
                put(e)
    if ehits:
        top = max(ehits.values())
        best = sorted((k for k in ehits if ehits[k] == top), key=lambda k: name_key(els[k]["path"]))
        for k in best:
            if _el_id(els[k]) not in {_el_id(x) for x in named}:
                named.append(els[k])
            put(els[k])
    expand = named[:10]
    if len(named) > 10:
        g.notes.append(f"{len(named)} elements match the request; the first 10 were expanded")
    async def kids(element_id: Any) -> List[Dict[str, Any]]:
        try:                                   # expansion is best effort; the searches are not
            return list(await _g(ground.children, element_id) or [])
        except Exception:
            return []

    for e in expand:
        if e.get("id") is not None:
            for c in await kids(e["id"]):
                put(c)
    for e in expand:
        for s in await kids(e.get("parent")):
            put(s)
    for k in sorted(ehits, key=lambda k: (-ehits[k], name_key(els[k]["path"]))):
        put(els[k])
    if len(order) > MAX_ELEMENTS:
        g.notes.append(f"the assistant was shown {MAX_ELEMENTS} of {len(order)} related elements")
    g.elements = order[:MAX_ELEMENTS]
    kept = {_el_id(e) for e in g.elements}
    g.named = [e for e in named if _el_id(e) in kept]

    # Templates: those of the grounded elements, then those a noun names, then by id; ≤ 10.
    all_t = [t for t in (await _g(ground.templates) or []) if isinstance(t, dict) and t.get("name")]
    want: List[Dict[str, Any]] = []

    def tput(t: Dict[str, Any]) -> None:
        if all(name_key(x["name"]) != name_key(t["name"]) for x in want):
            want.append(t)

    tby = {name_key(t["name"]): t for t in all_t}
    for e in g.elements:
        tn = e.get("template_name")
        if isinstance(tn, str) and name_key(tn) in tby:
            tput(tby[name_key(tn)])
    for t in all_t:
        if any(name_key(n) in name_key(t["name"]) for n in nouns):
            tput(t)
    for t in sorted(all_t, key=lambda t: (t.get("id") is None, t.get("id") or 0, name_key(t["name"]))):
        tput(t)
    if len(want) > MAX_TEMPLATES:
        g.notes.append(f"the assistant was shown {MAX_TEMPLATES} of {len(want)} templates")
    g.templates = want[:MAX_TEMPLATES]
    return g


# ── Prompts ────────────────────────────────────────────────────────────

# The worked examples come from a FICTIONAL plant whose names cannot collide with a real one
# (D5.1, design_displays §6.2: Jim's "PressMon" gauge was bound to "Flow", a name the model took
# from an example): `EX-` tags, "Example Unit" elements. A binding equal to one of these names is
# a placeholder (`_Resolver.resolve`), never a binding. Sizes respect the per-kind caps.
_EXAMPLES = """\
Example 1. Request: "overview for example unit 1 with discharge pressure trend, run state and
motor current gauge; amber when current is above 41.5".
{"title": "Example Unit 1", "context": {"template": "Example Unit", "element": "/Example Site/Example Unit 1"},
 "window": "8h", "symbols": [
  {"kind": "state", "label": "Run state", "bind": ["EX Run State"], "x": 2, "y": 4, "w": 16, "h": 12},
  {"kind": "gauge", "label": "Motor current", "bind": ["EX Motor Current"], "x": 20, "y": 4, "w": 22, "h": 30,
   "min": "0", "max": "60", "rules": [{"when": {"above": "41.5"}, "then": "amber"}]},
  {"kind": "trend", "label": "Discharge pressure", "bind": ["EX Discharge Pressure"], "x": 2, "y": 38, "w": 96, "h": 58}]}

Example 2. Request: "tank levels for tanks 1 and 2 side by side, red when a level is below 10".
{"title": "Tank levels", "symbols": [
  {"kind": "bar", "label": "Tank 1 level", "bind": ["EX-LT-001.PV"], "x": 4, "y": 6, "w": 14, "h": 36,
   "rules": [{"when": {"below": "10"}, "then": "red"}], "group": "Tanks"},
  {"kind": "bar", "label": "Tank 2 level", "bind": ["EX-LT-002.PV"], "x": 20, "y": 6, "w": 14, "h": 36,
   "rules": [{"when": {"below": "10"}, "then": "red"}], "group": "Tanks"},
  {"kind": "trend", "label": "Levels", "bind": ["EX-LT-001.PV", "EX-LT-002.PV"], "x": 38, "y": 6, "w": 58, "h": 60}]}

Example 3. Request: "a small status panel: header text, the feed valve position (call it Feed valve)
and a table of all attributes of the element".
{"title": "Feed status", "context": {"template": "Example Unit", "element": "/Example Site/Example Unit 2"}, "symbols": [
  {"kind": "text", "label": "Example unit status", "bind": [], "x": 2, "y": 2, "w": 48, "h": 8},
  {"kind": "value", "label": "Feed valve", "bind": ["EX Valve Position"], "x": 2, "y": 14, "w": 16, "h": 12,
   "rules": [{"when": {"state": "Fault"}, "then": "red"}]},
  {"kind": "table", "label": "Attributes", "bind": [], "x": 2, "y": 30, "w": 46, "h": 60}]}
(These examples come from a fictional plant: none of their names exist here. Bind only names
listed for this request.)"""

# Every name the examples bind or point at (name_key): binding one is a placeholder (D5.1).
EXAMPLE_NAMES = frozenset(name_key(n) for n in (
    "EX Run State", "EX Motor Current", "EX Discharge Pressure", "EX Valve Position",
    "EX-LT-001.PV", "EX-LT-002.PV", "Example Unit", "Example Unit 1", "Example Unit 2",
    "/Example Site/Example Unit 1", "/Example Site/Example Unit 2", "/Example Site", "Example Site",
))


def _is_example_name(name: str) -> bool:
    """A name taken from the worked examples (whole, or either side of "<element>|<attr>")."""
    n = (name or "").strip()
    parts = [n] + ([x.strip() for x in n.split("|")] if "|" in n else [])
    return any(name_key(x) in EXAMPLE_NAMES for x in parts if x)


_PROPS_HELP = (
    "Properties (\"props\" of set, or \"props\" on a new symbol): label; x, y, w, h (\"25%\" or px); "
    "decimals 0-9, prefix, suffix, font s|m|l|xl, align left|center|right, stroke_width 0-16; "
    "color, fill, stroke = a color NAME (red, dark red, orange, amber, yellow, green, dark green, "
    "light green, teal, cyan, blue, dark blue, light blue, navy, purple, magenta, pink, brown, white, "
    "black, gray, light gray, dark gray, silver) or a house token (sev1 amber alarm, sev2 red alarm, "
    "normal, run, fg, muted, panel, line, bg, bad) — never hex; gauge / bar: min, max (numbers as "
    "text), bar orientation vertical|horizontal; trend: window (\"1h\", \"8h\"), y_axis "
    "\"auto\"|\"per_tag\"|{min, max}, colors [names], show {legend, cursor}; table: rows "
    "bindings|attributes, element (a listed path); text: text; shape: shape rect|ellipse|line|polyline, "
    "flow true|false; image: image (a library name); nav {display: <display name>, context?, time?}; "
    "rules [...].\n"
)


# D3.4 / D3.5 (design_displays §5.7.8 item 5, §5.7.9 item 13): the glyphs by area as ids, the
# binding hint per class and the words that are not their id (orchestrator/display_glyphs.py).
_EQUIPMENT_HELP = equipment_help()


def _system_prompt(follow_up: bool) -> str:
    kinds = "\n".join(f"- {k}: {m}" for k, m in KIND_MEANINGS.items())
    common = (
        "You draft an industrial operator display for PremHist. You only ever reply with ONE JSON "
        "object and nothing else.\n"
        "Geometry is a PERCENT grid: x, y, w, h are 0-100 percent of the canvas (x, y = top-left).\n"
        "Bind ONLY names listed under ELEMENTS, TEMPLATES or TAGS below — never invent a name. When "
        "the display has a context element, bind its attribute names exactly as listed; to bind an "
        "attribute of another listed element write \"<element path>|<attribute>\"; otherwise bind a "
        "tag name exactly as listed. In short: bind only names from the lists above; when nothing "
        "in the lists fits, bind the user's own words (e.g. \"pressure\") — never a name from the "
        "examples.\n"
        "Labels: a name the user gives (call it …) is that item's label.\n"
        "Rule thresholds are numbers written as text without units (\"7.1\"); a rule's then is "
        "amber (warning), red (alarm), gray (normal) or hide. Symbols may share a group label.\n"
        "A gauge or bar may give its scale as \"min\" and \"max\", numbers as text (\"0\", \"20\").\n"
        + _PROPS_HELP +
        f"Symbol kinds:\n{kinds}\n" + _EQUIPMENT_HELP
    )
    if not follow_up:
        return common + (
            "Reply with {\"title\", \"context\"?: {\"template\", \"element\": <listed element path>}, "
            "\"window\"?: \"1h\" | \"8h\" | \"24h\", \"symbols\": [{\"kind\", \"label\"?, \"bind\": [names], "
            "\"x\", \"y\", \"w\", \"h\", \"rules\"?: [{\"when\": {\"above\" | \"below\": \"<number>\" | "
            "\"state\": \"<name>\"}, \"then\"}], \"group\"?, \"min\"?, \"max\"?, \"glyph\"?, \"props\"?}]}. "
            "Do not overlap symbols.\n" + display_pipes.PROMPT_FIRST + "\n" + _EXAMPLES
        )
    return common + (
        "You are EDITING the current display listed below. Reply with {\"ops\": [...]}: you say WHAT "
        "to do, the engine computes the exact geometry on the grid and keeps symbols from "
        "overlapping. Every op names symbols by their id (s1, s2, …); ops apply in order.\n"
        "{\"op\": \"add\", \"symbol\": {kind, label?, bind, x, y, w, h, rules?, group?, min?, max?, glyph?, props?}, "
        "\"may_shrink\"?: [ids]}   (the engine gives a new symbol a readable default size and fits a "
        "text to its words: give x, y, w, h only roughly, and the user's size only when the user asked "
        "for a size of THAT symbol — \"a large font\" is a font, not a size; then place it with a "
        "place op)\n"
        "{\"op\": \"remove\", \"ids\": [ids]}   {\"op\": \"clear\"} (remove every symbol)\n"
        + display_pipes.PROMPT_OPS +
        "{\"op\": \"place\", \"id\", \"where\", \"of\", \"align\"?: start|center|end, \"gap\"?: px, "
        "\"may_shrink\"?: [ids]} — where:\n"
        "  below | above | left_of | right_of: outside \"of\", gap px apart; align = the cross axis "
        "(default center)\n"
        "  between: \"of\" = [id, id], centered in the space between them\n"
        "  center_of: centered on \"of\"   inside: within \"of\" (align start = top-left corner, end = "
        "bottom-right)\n"
        "  at: take that symbol's place (\"put X in Y's place\")   region: \"of\" = \"canvas:<region>\"\n"
        "  \"of\" = an id, a list of ids (their combined box), \"canvas\", or \"canvas:<region>\"; regions: "
        + ", ".join(REGIONS) + "\n"
        "{\"op\": \"make_room\", \"id\", \"from\"?: id}   (\"move X out of the way\", \"make space\": X moves "
        "to the nearest free area at its own size)\n"
        "{\"op\": \"align\", \"ids\": [ids], \"to\": id | [ids] | \"canvas\" | \"canvas:<region>\", "
        "\"edge\": left|center|right|top|middle|bottom}\n"
        "{\"op\": \"match\", \"ids\": [ids], \"to\": id, \"size\": width|height|both}\n"
        "{\"op\": \"distribute\", \"ids\": [ids], \"axis\": horizontal|vertical}\n"
        "{\"op\": \"center\", \"ids\": [ids], \"axis\"?: horizontal|vertical|both}   (on the canvas)\n"
        "{\"op\": \"swap\", \"a\": id, \"b\": id}\n"
        "{\"op\": \"scale\", \"ids\": [ids], \"factor\": n}   (bigger 1.5, smaller 0.67, twice as big 2, half 0.5)\n"
        "{\"op\": \"nudge\", \"ids\": [ids], \"dx\": steps, \"dy\": steps}   (8 px grid steps; a bit = 2; "
        "right / down are positive)\n"
        "{\"op\": \"set\", \"ids\": [ids], \"props\": {...}}   (any property above, e.g. {\"color\": \"green\"}, "
        "{\"decimals\": 2}, {\"label\": \"Tank 1\"})\n"
        "{\"op\": \"rebind\", \"id\", \"bind\": [names]} (the full new list)   "
        "{\"op\": \"rule\", \"id\", \"rules\": [...]} (the full new list)   {\"op\": \"retitle\", \"title\"}\n"
        "{\"op\": \"move\", \"id\", \"x\", \"y\"} / {\"op\": \"resize\", \"id\", \"w\", \"h\"} only for explicit "
        "percent coordinates.\n"
        "Placement examples (ids as on the display; a new symbol is referred to by the next free id):\n"
        "  \"centered under the trend\" = {\"op\": \"place\", \"id\": \"s7\", \"where\": \"below\", \"of\": \"s5\"}\n"
        "  \"under the trend, left-aligned\" = place below with \"align\": \"start\"; right-aligned = \"end\"\n"
        "  \"above the gauge\" = place above; \"to the right of the gauge, top-aligned\" = place right_of "
        "with \"align\": \"start\"; \"left of it, vertically centered\" = place left_of\n"
        "  \"between the two gauges\" = place between with \"of\": [\"s1\", \"s2\"]\n"
        "  \"in the middle of the screen\" = place region \"of\": \"canvas:center\"; \"top right corner\" = "
        "\"canvas:top_right\"; \"along the bottom\" = \"canvas:bottom\"; \"in the middle third\" = "
        "\"canvas:middle_third\"\n"
        "  \"the value inside the box\" = place inside; \"centered on the shape\" = place center_of\n"
        "  \"line up the tops of s1 and s2 with s3\" = align edge top to s3; \"move the gauge out of the "
        "way\" = make_room\n"
        "  \"add a trend in the middle, move the gauge out of the way, and a title centered under the "
        "trend\" = add trend, place it region canvas:center, make_room the gauge from the trend, add "
        "text, place the text below the trend.\n"
        "A text the user asks for (\"a text that says X\", \"a title under it\") is its OWN add "
        "{\"kind\": \"text\", \"label\": \"X\"} followed by its place — never a title on another op.\n"
        "Never compute pixel or percent positions for a relation you can say with place / align / "
        "center / make_room; never give a size the user did not ask for.\n"
        "References: \"the larger gauge\" = the gauge marked larger; \"the left one\" = the one marked "
        "left; \"both gauges\" / \"all values\" = every symbol of that kind; \"everything\" = every "
        "symbol; \"the one showing flow\" = the symbol bound to a flow tag; a label in quotes = the "
        "symbol with that label. Move or reuse an EXISTING symbol by its id — never add a new one to "
        "stand for it. One op per change the user asks for, in order: \"make both gauges the same size "
        "and line them up along the top\" = a match, then an align with edge top. Give may_shrink "
        "only for symbols the user allows to be made smaller (\"decrease "
        "X if you need to\", \"make room\"). Change only what the request asks for; when the request "
        "asks for nothing you can do, reply {\"ops\": []}."
    )


def _units_of_point(p: Dict[str, Any]) -> str:
    return str(p.get("eng_units") or "")


def _attr_line(a: Dict[str, Any]) -> str:
    v = a.get("value") or {}
    bits: List[str] = []
    u = a.get("units") or (v.get("display_units") if isinstance(v, dict) else None)
    if v.get("kind") == "tag":
        tag = v.get("tag") or {}
        u = u or tag.get("eng_units")
        bits.append("tag " + (tag.get("name") or "deleted") if not v.get("missing") else "tag deleted")
    elif v.get("kind") == "static":
        bits.append("static")
    elif v.get("kind") == "unbound":
        bits.append("unbound")
    if u:
        bits.append(str(u))
    return f"{a.get('name')}" + (f" ({', '.join(bits)})" if bits else "")


def _grounding_text(g: _Grounding) -> str:
    lines: List[str] = []
    named_ids = {_el_id(e) for e in g.named}
    lines.append("ELEMENTS (path | template):")
    if not g.elements:
        lines.append("- (none matched)")
    for e in g.elements:
        line = f"- {e.get('path')} | {e.get('template_name') or 'no template'}"
        if _el_id(e) in named_ids and e.get("attributes"):
            line += " | attributes: " + "; ".join(_attr_line(a) for a in e["attributes"] if isinstance(a, dict))
        lines.append(line)
    lines.append("TEMPLATES (name: attributes):")
    if not g.templates:
        lines.append("- (none)")
    for t in g.templates:
        defs = []
        for d in t.get("attributes") or []:
            if isinstance(d, dict):
                extra = ", ".join(str(x) for x in (d.get("kind"), d.get("units"), d.get("category")) if x)
                defs.append(f"{d.get('name')}" + (f" ({extra})" if extra else ""))
        lines.append(f"- {t['name']}: " + ("; ".join(defs) if defs else "(no attributes)"))
    lines.append("TAGS (name | units | description):")
    if not g.tags:
        lines.append("- (none matched)")
    dup: Dict[str, int] = {}
    for p in g.tags:
        dup[name_key(str(p.get("name")))] = dup.get(name_key(str(p.get("name"))), 0) + 1
    if any(c > 1 for c in dup.values()):
        lines.append("(a tag name in several datasets is listed as <dataset>.<name>; bind it that way)")
    for p in g.tags:
        nm = str(p.get("name"))
        if dup.get(name_key(nm), 0) > 1 and p.get("dataset"):
            nm = f"{p['dataset']}.{nm}"
        line = f"- {nm} | {_units_of_point(p)} | {p.get('description') or ''}".rstrip(" |")
        st = p.get("states")
        if isinstance(st, dict) and st:
            try:
                items = sorted(st.items(), key=lambda kv: int(kv[0]))
            except (TypeError, ValueError):
                items = sorted(st.items())
            line += " | states: " + ", ".join(f"{v}" for _, v in items)
        lines.append(line)
    return "\n".join(lines)


def _pct(v: int, total: int) -> str:
    q = Fraction(v * 100, total)
    r = Fraction(int(q * 10 + Fraction(1, 2)), 10)  # half-up to 0.1 (display only)
    return f"{float(r):g}"


def _binding_name(b: Any, tags_by_id: Dict[Any, str]) -> str:
    if not isinstance(b, dict):
        return "?"
    if "attr" in b:
        return f"{b['element']}|{b['attr']}" if b.get("element") else str(b["attr"])
    if "point" in b:
        return tags_by_id.get(b["point"]) or f"point #{b['point']}"
    if "ref" in b:
        return str(b["ref"])
    return "?"


def _int0(v: Any) -> int:
    i = _as_int(v)
    return i if i is not None else 0


# ── Names → bindings ───────────────────────────────────────────────────

class _Resolver:
    """names → bindings (design_displays §6.1): context attribute → {attr}; an attribute of an
    element named in the request → {attr, element}; a grounded tag → {point}; else a UNIQUE
    prefix over the same sets; else None (a placeholder). Never a search's top hit."""

    def __init__(self, g: _Grounding, ctx_el: Optional[Dict[str, Any]],
                 known_points: Optional[Dict[Any, Dict[str, Any]]] = None) -> None:
        self.g = g
        self.ctx_el = ctx_el
        self.ctx_attrs: Dict[str, str] = {}
        if ctx_el:
            for a in ctx_el.get("attributes") or []:
                if isinstance(a, dict) and a.get("name"):
                    self.ctx_attrs.setdefault(name_key(a["name"]), a["name"])
        self.el_attrs: List[Tuple[str, str, str]] = []      # (key, attr name, element path)
        ctx_id = _el_id(ctx_el) if ctx_el else None
        for e in g.named:
            if ctx_el is not None and _el_id(e) == ctx_id:
                continue
            for a in e.get("attributes") or []:
                if isinstance(a, dict) and a.get("name"):
                    self.el_attrs.append((name_key(a["name"]), a["name"], e["path"]))
        self.tags: List[Tuple[str, Dict[str, Any]]] = [
            (name_key(str(p.get("name"))), p) for p in g.tags if p.get("name") and p.get("id") is not None]
        # Points the grounded asset model binds (the context element and the named elements):
        # the evidence that settles one tag name living in several datasets.
        self.bound_ids: set = set()
        for e in ([ctx_el] if ctx_el else []) + list(g.named):
            for a in e.get("attributes") or []:
                v = a.get("value") if isinstance(a, dict) else None
                if isinstance(v, dict) and v.get("kind") == "tag" and v.get("point") is not None                         and not v.get("missing"):
                    self.bound_ids.add(v["point"])
        self.ambiguous: Dict[str, List[str]] = {}       # name → the datasets it is in (unsettled)
        # D5.2: the points the display already binds (looked up by id) resolve by name too, and a
        # reply naming "point #N" maps back to {point: N} when N is one of them or grounded.
        self.known: set = {p.get("id") for p in g.tags if p.get("id") is not None}
        have = set(self.known)
        for pid, p in (known_points or {}).items():
            self.known.add(pid)
            if pid not in have and p.get("name"):
                self.tags.append((name_key(str(p["name"])), dict(p, id=pid)))

    def _attr_binding(self, e: Dict[str, Any], attr: str) -> Dict[str, Any]:
        if self.ctx_el is not None and _el_id(e) == _el_id(self.ctx_el):
            return {"attr": attr}
        return {"attr": attr, "element": e["path"]}

    def resolve(self, name: str) -> Optional[Dict[str, Any]]:
        n = (name or "").strip()
        if not n:
            return None
        k = name_key(n)
        if _is_example_name(n):                        # borrowed from the examples (D5.1)
            return None
        m = re.match(r"^point\s*#?\s*(\d+)$", n, re.I)
        if m:
            pid = int(m.group(1))
            return {"point": pid} if pid in self.known else None
        if "|" in n:                                   # "<element path or name>|<attribute>"
            el_part, _, attr = n.rpartition("|")
            e = self.g.find_element(el_part)
            if e is None:
                return None
            attrs = [a["name"] for a in e.get("attributes") or [] if isinstance(a, dict) and a.get("name")]
            exact = [a for a in attrs if name_key(a) == name_key(attr.strip())]
            if len(exact) == 1:
                return self._attr_binding(e, exact[0])
            pre = [a for a in attrs if attr.strip() and name_key(a).startswith(name_key(attr.strip()))]
            return self._attr_binding(e, pre[0]) if len(pre) == 1 else None
        # 1. an attribute of the context element
        if k in self.ctx_attrs:
            return {"attr": self.ctx_attrs[k]}
        # 2. an attribute of an element named in the request (unique)
        ea = {(nm, path) for key, nm, path in self.el_attrs if key == k}
        if len(ea) == 1:
            nm, path = next(iter(ea))
            return {"attr": nm, "element": path}
        # 3. a grounded tag (unique; a "<dataset>.<name>" form is read from the left)
        pts = {p["id"]: p for key, p in self.tags if key == k}
        if not pts:
            for key, p in self.tags:
                ds = name_key(str(p.get("dataset") or ""))
                if ds and k == f"{ds}.{key}":
                    pts[p["id"]] = p
        if len(pts) == 1:
            return {"point": next(iter(pts))}
        if len(pts) > 1:
            # One name in several datasets: the one the grounded asset model binds, when exactly
            # one is; otherwise unsettled (never a guess) and reported with its datasets.
            bound = [i for i in pts if i in self.bound_ids]
            if len(bound) == 1:
                return {"point": bound[0]}
            self.ambiguous[n] = sorted({str(p.get("dataset") or "?") for p in pts.values()}, key=name_key)
            return None
        if len(ea) > 1:
            return None                                # ambiguous exact: never a guess
        # 4. a unique prefix over the same sets
        cands: Dict[str, Dict[str, Any]] = {}
        for key, nm in self.ctx_attrs.items():
            if key.startswith(k):
                b = {"attr": nm}
                cands[json.dumps(b, sort_keys=True)] = b
        for key, nm, path in self.el_attrs:
            if key.startswith(k):
                b = {"attr": nm, "element": path}
                cands[json.dumps(b, sort_keys=True)] = b
        for key, p in self.tags:
            if key.startswith(k):
                b = {"point": p["id"]}
                cands[json.dumps(b, sort_keys=True)] = b
        if len(cands) == 1:
            return next(iter(cands.values()))
        return None


# ── Mapping (AUTHORING → §3) ───────────────────────────────────────────

def _to_px(pct: Any, total: int, grid: int, minimum: int = 0) -> int:
    """x = round(x% × total / 100 / grid) × grid, half-up, exact (Fraction of the text)."""
    cells = _frac(pct) * total / 100 / grid
    v = int(cells + Fraction(1, 2)) * grid
    return max(v, minimum)


def _map_rules(rules: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rules or []:
        when = r["when"]
        if when.get("state") not in (None, ""):
            st = when["state"]
            code = _as_int(st) if isinstance(st, _Num) else None
            cond: Dict[str, Any] = {"state": code if code is not None else str(st).strip()}
        else:
            k = "above" if when.get("above") not in (None, "") else "below"
            cond = {k: {"kind": "f64", "text": _num_text(when[k])}}
        out.append({"when": cond, "set": dict(_THEN_SET[r["then"]])})
    return out


def _binding_point_info(b: Any, res: "_Resolver") -> Tuple[Optional[str], Optional[str], str]:
    """What a resolved binding reaches: (its point's type class "digital" / "numeric" / "text" or
    None when unknown, its units or None, the name a note uses)."""
    names = {p.get("id"): str(p.get("name")) for _k, p in res.tags}
    shown = _binding_name(b, names)
    ptype: Any = None
    units: Any = None
    if isinstance(b, dict) and b.get("point") is not None:
        p = next((p for _k, p in res.tags if p.get("id") == b["point"]), None)
        if p is not None:
            ptype, units = p.get("point_type"), p.get("eng_units")
    elif isinstance(b, dict) and b.get("attr"):
        el = res.g.find_element(str(b["element"])) if b.get("element") else res.ctx_el
        for a in (el or {}).get("attributes") or []:
            if isinstance(a, dict) and name_key(str(a.get("name"))) == name_key(str(b["attr"])):
                v = a.get("value") or {}
                tag = (v.get("tag") or {}) if isinstance(v, dict) else {}
                units = a.get("units") or (v.get("display_units") if isinstance(v, dict) else None) \
                    or tag.get("eng_units")
                if isinstance(v, dict) and v.get("kind") == "tag":
                    ptype = tag.get("point_type")
                    if ptype is None and v.get("point") is not None:
                        p = next((p for _k, p in res.tags if p.get("id") == v["point"]), None)
                        ptype = (p or {}).get("point_type")
                elif isinstance(v, dict) and v.get("kind") == "static":
                    ptype = "float64"
                break
    t = str(ptype or "").lower()
    cls = None if not t else "digital" if t == "digital" else \
        "numeric" if re.match(r"^(?:float|int|uint)\d*$", t) else "text"
    return cls, (str(units) if units else None), shown


# What binding 1 of each class must reach (design_displays §5.7.8 item 2, §5.7.9 item 3);
# None = anything (display_glyphs.binding_need).
_NEED_WORDS: Dict[Tuple[str, ...], str] = {
    ("digital",): "a digital point", ("numeric",): "a numeric point",
    ("digital", "numeric"): "a digital or numeric point",
}


def equipment_binds(glyph: str, binds: List[Dict[str, Any]], res: "_Resolver"
                    ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """§5.7.8 item 2 / §5.7.9 R5, R8 — the bindings an equipment symbol of `glyph` keeps, in their
    places, and a note per binding that does not fit its class (a breaker on a float, a tank on a
    digital state; the 2nd binding of the five R5 glyphs — position % / speed / feed rate — only
    after a digital state). A point whose type is unknown is kept. Never a guess at a better point:
    a misfit leaves the place unbound."""
    need: Optional[Tuple[str, ...]] = binding_need(glyph)
    what = f"the {GLYPH_NAMES.get(glyph, 'equipment')} symbol"
    notes: List[str] = []
    info = [_binding_point_info(b, res) for b in binds]

    def fits(i: int, want: Optional[Tuple[str, ...]]) -> bool:
        return want is None or info[i][0] is None or info[i][0] in want

    keep: List[Dict[str, Any]] = []
    if glyph in SECOND_BINDING and len(binds) >= 2:
        if fits(0, ("digital",)) and fits(1, ("numeric",)):
            return binds[:2], notes
        ok = [i for i in range(2) if fits(i, need)]
        if ok:
            keep = [binds[ok[0]]]
            for i in range(2):
                if i != ok[0]:
                    notes.append(f"{what} keeps one binding: {info[i][2]} does not fit as "
                                 + (f"its {SECOND_BINDING[glyph]} (that needs a digital state first)" if i == 1
                                    else "its state"))
            return keep, notes
    for i, b in enumerate(binds[:glyph_bind_max(glyph)]):
        if fits(i, need):
            keep.append(b)
        else:
            notes.append(f"{what} needs {_NEED_WORDS.get(need or (), 'a point')}; {info[i][2]} is not one "
                         f"— drag one onto it")
    return keep, notes


def _binding_point_name(b: Any, res: "_Resolver") -> Optional[str]:
    """R10 — the name of the point a resolved binding reaches, for the letters prefill: a point's
    name; an `{attr}` binding's point's name (a tag attribute); None for a static attribute, a
    `ref` or anything unresolved."""
    if not isinstance(b, dict):
        return None
    if b.get("point") is not None:
        p = next((p for _k, p in res.tags if p.get("id") == b["point"]), None)
        return str(p.get("name")) if p is not None and p.get("name") else None
    if b.get("attr"):
        el = res.g.find_element(str(b["element"])) if b.get("element") else res.ctx_el
        for a in (el or {}).get("attributes") or []:
            if isinstance(a, dict) and name_key(str(a.get("name"))) == name_key(str(b["attr"])):
                v = a.get("value") or {}
                if not isinstance(v, dict) or v.get("kind") != "tag":
                    return None
                tag = v.get("tag") or {}
                if isinstance(tag, dict) and tag.get("name"):
                    return str(tag["name"])
                if v.get("point") is not None:
                    p = next((p for _k, p in res.tags if p.get("id") == v["point"]), None)
                    return str(p.get("name")) if p is not None and p.get("name") else None
                return None
    return None


def prefill_letters(sym: Dict[str, Any], res: "_Resolver") -> bool:
    """§5.7.9 item 3 / R10: an instrument bubble whose binding 1 is set while it has no `letters`
    gets them from the resolved point's name (`letters_of`); none derivable → left absent. Returns
    whether letters were written."""
    if sym.get("type") != "equipment" or sym.get("glyph") not in LETTERS_GLYPHS or sym.get("letters"):
        return False
    binds = sym.get("bind") or []
    lt = letters_of(_binding_point_name(binds[0], res)) if binds else None
    if lt:
        sym["letters"] = lt
        return True
    return False


def _map_equipment(s: Dict[str, Any], sym: Dict[str, Any], res: "_Resolver", label: str,
                   notes_range: List[str]) -> None:
    """One AUTHORING equipment symbol's own fields → §5.7.8 item 1 / §5.7.9 R1 (bindings already
    resolved in `sym`): glyph; rotate / flip; the class's bindings (`equipment_binds`; notes in
    `_notes`); every vessel glyph's and the analog indicator's `min` / `max` as written, else from a
    percent point (0 to 100), else 0 to 100 with the "set the range" note (D3.4); `normal` (vessels,
    analog indicator) and `limits` (vessels) as Values; an instrument bubble's `letters` as written
    (uppercased) or prefilled from its point's name (R10); `states` with only the glyph's keys
    (`synchronized` for the generator and the synchronizing point). Fields the glyph does not take
    are dropped silently."""
    g = s["glyph"]
    sym["glyph"] = g
    r = _as_int(s.get("rotate"))
    if r in ROTATIONS and r:
        sym["rotate"] = r
    if s.get("flip") is True:
        sym["flip"] = True
    binds, why = equipment_binds(g, list(sym.get("bind") or []), res)
    sym["bind"] = binds
    if why:
        sym.setdefault("_notes", []).extend(why)
    props = normalize_props(s["props"]) if isinstance(s.get("props"), dict) else {}
    if g in RANGE_GLYPHS:
        for k in ("min", "max"):
            v = s.get(k) if s.get(k) not in (None, "") else props.get(k)
            if _num_text(v) is not None:
                sym[k] = {"kind": "f64", "text": _num_text(v)}
        if "min" not in sym or "max" not in sym:
            # Required on these (R1): a percent point's range is 0 to 100; with nothing to derive it
            # from, 0 to 100 is written so the symbol is valid, and the user is asked to set it (the
            # gauges' note, D3.4)
            _cls, units, _n = _binding_point_info(binds[0], res) if binds else (None, None, "")
            if not (units and units.strip() == "%"):
                notes_range.append(label or _kind_noun(sym))
            sym.setdefault("min", {"kind": "f64", "text": "0"})
            sym.setdefault("max", {"kind": "f64", "text": "100"})
    for field in ("normal", "limits"):
        if field not in glyph_fields(g):
            continue
        v = s.get(field)
        if v is None:
            v = props.get(field)
        if isinstance(v, dict):
            out = {k: {"kind": "f64", "text": _num_text(v[k])} for k in ("lo", "hi")
                   if v.get(k) not in (None, "") and _num_text(v[k]) is not None}
            if field == "normal" and len(out) != 2:
                out = {}
            if out:
                sym[field] = out
    if g in LETTERS_GLYPHS:
        lt = s.get("letters") if s.get("letters") not in (None, "") else props.get("letters")
        if isinstance(lt, str) and letters_valid(lt.strip().upper()):
            sym["letters"] = lt.strip().upper()
        else:
            prefill_letters(sym, res)
    allowed = glyph_states(g)
    st = s.get("states")
    if isinstance(st, dict) and allowed:
        out_st = {k: [_as_int(c) for c in v] for k, v in st.items()
                  if k in allowed and isinstance(v, list) and all(_as_int(c) is not None for c in v)}
        if out_st:
            sym["states"] = out_st


def _map_symbol(s: Dict[str, Any], res: _Resolver, W: int, H: int, grid: int,
                notes_range: List[str]) -> Tuple[Dict[str, Any], List[str]]:
    """One AUTHORING symbol → a §3 symbol (absolute px, no id yet) + the names not found."""
    kind = s["kind"]
    label = _clean_text(s.get("label"), 200) if isinstance(s.get("label"), str) else ""
    sym: Dict[str, Any] = {"type": kind}
    if s.get("x") is not None:
        sym["x"] = _to_px(s["x"], W, grid)
        sym["y"] = _to_px(s["y"], H, grid)
        sym["w"] = _to_px(s["w"], W, grid, grid)
        sym["h"] = _to_px(s["h"], H, grid, grid)
    binds: List[Dict[str, Any]] = []
    missing: List[str] = []
    for name in s.get("bind") or []:
        b = res.resolve(name)
        if b is None:
            missing.append(name.strip())
        elif b not in binds:
            binds.append(b)
    if missing and (BIND_RANGE.get(kind, (0, 0))[1] <= 1 or kind == "equipment"):
        # a single-binding kind (and an equipment symbol, whose bindings have places): a pure placeholder
        binds = []
    # D5.1: an unresolved name no longer replaces the label — the symbol keeps the model's (or
    # the user's) label; the name is listed in `placeholders` and the viewer says "not found"
    # inside an unbound symbol. A trend / table that DID bind some names shows data, so the
    # viewer has nothing to say there: its label keeps its words and names the missing ones
    # after them (the D5 settled rule).
    if missing and binds:
        label = _clean_text((label + " — " if label else "") + NOT_FOUND + ", ".join(missing), 200)
    sym["bind"] = binds
    sym["style"] = {}
    sym["rules"] = _map_rules(s.get("rules"))
    if kind == "text":
        sym["text"] = _clean_text(s.get("label"), 2000)
        label = ""
    elif kind == "shape":
        sym["shape"] = "rect"
    elif kind == "table":
        sym["rows"] = "bindings" if (binds or missing) else "attributes"
    elif kind in ("gauge", "bar"):
        for k in ("min", "max"):                       # kept exactly as written (lossless)
            if s.get(k) not in (None, ""):
                sym[k] = {"kind": "f64", "text": _num_text(s[k])}
        if "min" not in sym or "max" not in sym:
            notes_range.append(_clean_text(s.get("label"), 200) or kind)
    elif kind == "equipment":
        _map_equipment(s, sym, res, label, notes_range)
    if label:
        sym["label"] = label
    if isinstance(s.get("group"), str) and _clean_text(s["group"], 200):
        sym["_group"] = _clean_text(s["group"], 200)
    return sym, missing


# ── The tidy pass ──────────────────────────────────────────────────────

def _box(s: Dict[str, Any]) -> Tuple[int, int, int, int]:
    return s["x"], s["y"], s["w"], s["h"]


def _snap(v: int, grid: int) -> int:
    return int(Fraction(v, grid) + Fraction(1, 2)) * grid


def _clamp(s: Dict[str, Any], W: int, H: int, grid: int) -> None:
    """Inside the canvas AND on the grid (a canvas edge need not be a grid multiple: 900 / 8)."""
    s["w"] = max(grid, min(s["w"], W // grid * grid))
    s["h"] = max(grid, min(s["h"], H // grid * grid))
    s["x"] = max(0, min(s["x"], (W - s["w"]) // grid * grid))
    s["y"] = max(0, min(s["y"], (H - s["h"]) // grid * grid))


def _contains(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    ax, ay, aw, ah = _box(a)
    bx, by, bw, bh = _box(b)
    return ax <= bx and ay <= by and bx + bw <= ax + aw and by + bh <= ay + ah


def _overlaps(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    ax, ay, aw, ah = _box(a)
    bx, by, bw, bh = _box(b)
    if not (ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah):
        return False
    # A shape drawn behind what it fully contains is a backdrop, not a collision.
    if (a.get("type") == "shape" and _contains(a, b)) or (b.get("type") == "shape" and _contains(b, a)):
        return False
    return True


def _push(a: Dict[str, Any], b: Dict[str, Any], W: int, H: int, grid: int,
          others: Sequence[Dict[str, Any]] = ()) -> None:
    """Move `a` off `b` along the axis of the smaller overlap (away from b's center), trying the
    other side and then the other axis when the canvas edge stops it. A move that also clears
    `others` (the symbols already placed) is preferred over one that only clears `b`."""
    ax, ay, aw, ah = _box(a)
    bx, by, bw, bh = _box(b)
    ox = min(ax + aw, bx + bw) - max(ax, bx)
    oy = min(ay + ah, by + bh) - max(ay, by)
    right = (2 * ax + aw) >= (2 * bx + bw)
    down = (2 * ay + ah) >= (2 * by + bh)
    x_moves = [bx + bw, bx - aw] if right else [bx - aw, bx + bw]
    y_moves = [by + bh, by - ah] if down else [by - ah, by + bh]
    tries: List[Tuple[str, int]] = ([("x", v) for v in x_moves] + [("y", v) for v in y_moves]) \
        if ox <= oy else ([("y", v) for v in y_moves] + [("x", v) for v in x_moves])
    orig = (a["x"], a["y"])
    for strict in (True, False):
        for axis, v in tries:
            a["x"], a["y"] = orig
            a[axis] = v
            _clamp(a, W, H, grid)
            if _overlaps(a, b):
                continue
            if strict and any(o is not a and _overlaps(a, o) for o in others):
                continue
            return
    a["x"], a["y"] = orig
    a[tries[0][0]] = tries[0][1]
    _clamp(a, W, H, grid)


# D5.1 (design_displays §6.2): the largest box per kind the tidy pass lets the model draw
# (Jim's TempMon gauge came back 1536 × 864, the whole canvas). Unlisted kinds are uncapped.
SIZE_CAPS: Dict[str, Tuple[int, int]] = {
    "value": (320, 160), "state": (320, 160), "gauge": (480, 360), "bar": (480, 360),
    "text": (800, 120), "button": (320, 96),
}
# A request with one of these words lifts the caps for that draft.
LIFT_WORDS = frozenset(("big", "large", "full", "whole", "fill"))


def caps_lifted(request: str) -> bool:
    """Whether the request asks for size (`big`, `large`, `full`, `whole`, `fill` as words)."""
    return any(name_key(w) in LIFT_WORDS for w in re.findall(r"[A-Za-z]+", request or ""))


def tidy(symbols: List[Dict[str, Any]], W: int = CANVAS_W, H: int = CANVAS_H, grid: int = GRID,
         fixed: Sequence[Dict[str, Any]] = (), caps: bool = True) -> List[str]:
    """The tidy pass (design_displays §6.1): keeps the arrangement, only cleans it — the per-kind
    size caps (D5.1, `SIZE_CAPS`, top-left kept; `caps=False` when the request lifts them), snap
    to the grid, clamp inside the canvas, push overlapping symbols apart along the smaller
    overlap (in output order, ≤ 3 sweeps; `fixed` symbols are obstacles that never move), align
    left / top edges ≤ 8 px apart. Mutates `symbols` (absolute x, y, w, h); returns notes."""
    notes: List[str] = []
    for s in symbols:
        cap = SIZE_CAPS.get(str(s.get("type"))) if caps else None
        if cap:
            s["w"] = min(s["w"], cap[0])
            s["h"] = min(s["h"], cap[1])
    for s in symbols:
        for k in ("x", "y"):
            s[k] = _snap(s[k], grid)
        for k in ("w", "h"):
            s[k] = max(grid, _snap(s[k], grid))
        _clamp(s, W, H, grid)
    for _sweep in range(3):
        moved = False
        for i, a in enumerate(symbols):
            placed = list(fixed) + symbols[:i]
            for b in placed:
                if _overlaps(a, b):
                    _push(a, b, W, H, grid, placed)
                    moved = True
        if not moved:
            break
    left = sum(1 for i, a in enumerate(symbols) for b in list(fixed) + symbols[:i] if _overlaps(a, b))
    if left:
        notes.append(f"{left} overlap(s) could not be tidied away; check the layout")
    everyone = list(fixed) + symbols
    for i, a in enumerate(symbols):
        for b in list(fixed) + symbols[:i]:
            for k in ("x", "y"):
                d = a[k] - b[k]
                if d != 0 and abs(d) <= 8:
                    old = a[k]
                    a[k] = b[k]
                    _clamp(a, W, H, grid)
                    if any(o is not a and _overlaps(a, o) for o in everyone):
                        a[k] = old
                        _clamp(a, W, H, grid)
    return notes


def fallback_layout(symbols: List[Dict[str, Any]], title: str, W: int = CANVAS_W,
                    H: int = CANVAS_H, grid: int = GRID) -> Tuple[List[Dict[str, Any]], int]:
    """The deterministic layout for a draft with no usable positions: a title text row, a row of
    values / states / gauges / bars, trends half-width, tables half-width, then the rest.
    Returns (symbols with the title first, the canvas height needed — ≥ H)."""
    m, gap = 16, 16
    inner = W - 2 * m
    title_sym = {"type": "text", "text": _clean_text(title, 2000), "x": m, "y": m, "w": inner,
                 "h": 48, "bind": [], "style": {"font": "l"}, "rules": []}
    y = m + 48 + gap
    row = ("value", "state", "gauge", "bar", "equipment")      # equipment symbols ride the KPI row
    kpi = [s for s in symbols if s["type"] in row]
    trends = [s for s in symbols if s["type"] == "trend"]
    tables = [s for s in symbols if s["type"] == "table"]
    rest = [s for s in symbols if s["type"] not in row + ("trend", "table")]

    def rows(items: List[Dict[str, Any]], per_row: int, h: int) -> None:
        nonlocal y
        for start in range(0, len(items), per_row):
            row = items[start:start + per_row]
            n = per_row if per_row == 2 else max(len(row), 4)   # a lone value is not full width
            w = ((inner - gap * (n - 1)) // n) // grid * grid
            for j, s in enumerate(row):
                s["x"], s["y"], s["w"], s["h"] = m + j * (w + gap), y, w, h
            y += h + gap

    rows(kpi, 6, 144)
    rows(trends, 2, 272)
    rows(tables, 2, 240)
    rows(rest, 6, 64)
    need = max(H, ((y - gap + m + grid - 1) // grid) * grid)
    return [title_sym] + symbols, min(need, 4320)


def _group(leaves: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`_group` labels → one `group` symbol per label (at its first member's place in the z-order),
    its box the members' bounding box, the members re-positioned relative to it."""
    members: Dict[str, List[Dict[str, Any]]] = {}
    keys: List[Optional[str]] = []
    for s in leaves:
        gk0 = name_key(s["_group"]) if s.get("_group") else None
        keys.append(gk0)
        if gk0 is not None:
            members.setdefault(gk0, []).append(s)
    out: List[Dict[str, Any]] = []
    done: set = set()
    for s, gk in zip(leaves, keys):
        if gk is None:
            out.append(s)
            continue
        if gk in done:
            continue
        done.add(gk)
        ms = members[gk]
        gx, gy = min(c["x"] for c in ms), min(c["y"] for c in ms)
        gw = max(c["x"] + c["w"] for c in ms) - gx
        gh = max(c["y"] + c["h"] for c in ms) - gy
        label = s["_group"]
        for c in ms:
            c["x"] -= gx
            c["y"] -= gy
            c.pop("_group", None)
        out.append({"type": "group", "label": label, "x": gx, "y": gy, "w": gw, "h": gh,
                    "bind": [], "style": {}, "rules": [], "children": ms})
    return out


def _assign_ids(symbols: List[Dict[str, Any]], start: int) -> int:
    """Ids s<start>… in output order (pre-order: a group before its children). Returns next."""
    n = start
    for s in symbols:
        s["id"] = f"s{n}"
        n += 1
        if isinstance(s.get("children"), list):
            n = _assign_ids(s["children"], n)
    return n


def _walk(symbols: Iterable[Any]) -> Iterable[Dict[str, Any]]:
    for s in symbols or []:
        if isinstance(s, dict):
            yield s
            if isinstance(s.get("children"), list):
                yield from _walk(s["children"])


def _max_id(symbols: Iterable[Any]) -> int:
    best = 0
    for s in _walk(symbols):
        m = re.match(r"^s([1-9]\d*)$", str(s.get("id") or ""))
        if m:
            best = max(best, int(m.group(1)))
    return best


def _canvas_of(current: Optional[Dict[str, Any]]) -> Tuple[int, int, int]:
    c = current.get("canvas") if isinstance(current, dict) else None
    if isinstance(c, dict):
        w, h, gr = _as_int(c.get("width")), _as_int(c.get("height")), _as_int(c.get("grid"))
        if w and h and 320 <= w <= 7680 and 200 <= h <= 4320:
            return w, h, (gr if gr and 1 <= gr <= 64 else GRID)
    return CANVAS_W, CANVAS_H, GRID


def _self_check(doc: Dict[str, Any]) -> List[str]:
    """A local structural check of what the engine wrote (PremHist's `validate` is the authority;
    this catches an engine bug before the editor sees it)."""
    probs: List[str] = []
    seen: set = set()
    nxt = _as_int(doc.get("next_symbol_id")) or 1

    def walk(syms: List[Any], depth: int, at: str) -> None:
        if depth > 4:
            probs.append(f"{at}: nested deeper than 4")
            return
        for i, s in enumerate(syms):
            p = f"{at}[{i}]"
            sid = str(s.get("id"))
            m = re.match(r"^s([1-9]\d*)$", sid)
            if not m or sid in seen or int(m.group(1)) >= nxt:
                probs.append(f"{p}: bad or duplicate id {sid}")
            seen.add(sid)
            for k, lo, hi in (("x", -100000, 100000), ("y", -100000, 100000), ("w", 1, 100000), ("h", 1, 100000)):
                v = s.get(k)
                if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                    probs.append(f"{p}.{k}: {v!r}")
            if s.get("type") == "trend" and len(s.get("bind") or []) > 8:
                probs.append(f"{p}: a trend binds at most 8")
            if len(s.get("rules") or []) > 16:
                probs.append(f"{p}: more than 16 rules")
            if isinstance(s.get("children"), list):
                walk(s["children"], depth + 1, p + ".children")

    walk(doc.get("symbols") or [], 1, "symbols")
    return probs


# ── The model call ─────────────────────────────────────────────────────

class _LlmUnavailable(Exception):
    pass


def _content_of(res: Any) -> str:
    if isinstance(res, str):
        return res
    if isinstance(res, dict):
        if isinstance(res.get("content"), str):
            return res["content"]
        msg = res.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            return msg["content"]
        if "symbols" in res or "ops" in res:
            return json.dumps(res)
    return ""


def _default_llm_call(call_ollama_chat: Callable[..., Any]) -> Callable[..., Any]:
    """`call_ollama_chat` with the authoring timeout from settings.py
    (`display_authoring_timeout_s`, default 120) and, for gpt-oss models, `think="low"`
    (Ollama's reasoning effort; omitted for other models) — Settled at the D5 gate."""
    try:
        from .settings import get_settings
        timeout_s = int(getattr(get_settings(), "display_authoring_timeout_s", 120))
    except Exception:
        timeout_s = 120

    def call(messages: List[Dict[str, Any]], *, format: Any, model: str, temperature: float,
             options: Dict[str, Any]) -> Any:
        extra: Dict[str, Any] = {"think": "low"} if "gpt-oss" in name_key(model or "") else {}
        return call_ollama_chat(messages, model=model, temperature=temperature, timeout_s=timeout_s,
                                format=format, options=options, **extra)

    return call


def _sha(messages: List[Dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _round_ceiling_s() -> float:
    """The worst case wall time of ONE model round. llm_runtime retries once on a failure, so a
    round costs more than the configured per-call timeout: a deadline check that compares only
    the elapsed time still lets a round start that cannot possibly finish in what is left."""
    try:
        from .settings import get_settings
        t = int(getattr(get_settings(), "display_authoring_timeout_s", 120))
    except Exception:
        t = 120
    try:
        from .llm_runtime import max_call_seconds
        return float(max_call_seconds(t))
    except Exception:
        return float(t + max(15, int(t * 0.35)))


async def _ask_model(llm_call: Callable[..., Any], messages: List[Dict[str, Any]], schema: Dict[str, Any],
                     model: str, checker: Callable[[Any], List[str]],
                     soft_checker: Optional[Callable[[Any], List[str]]] = None,
                     deadline: Optional[float] = None, round_ceiling_s: float = 0.0
                     ) -> Tuple[Optional[Any], Optional[Any], List[Dict[str, Any]], int, bool]:
    """The bounded repair loop: ≤ MAX_TRIES sends, and no further send once `deadline`
    (time.monotonic()) cannot fit another round of `round_ceiling_s`. Returns (valid object |
    None, the last parsed object, the messages of the last send, tries, ran out of time).
    Raises _LlmUnavailable."""
    convo = list(messages)
    last_parsed: Optional[Any] = None
    sent: List[Dict[str, Any]] = convo
    soft_asked = False      # a soft error (an id that is neither on the display nor added) re-asks ONCE
    fallback: Optional[Tuple[Any, List[Dict[str, Any]], int]] = None   # the valid reply before it
    out_of_time = False
    attempt = 0
    for attempt in range(1, MAX_TRIES + 1):
        # A repair round is only started when it can finish inside the budget. The first round
        # always runs: without it there is nothing to answer with at all.
        if attempt > 1 and deadline is not None and time.monotonic() + round_ceiling_s > deadline:
            out_of_time = True
            if fallback is not None:
                return fallback[0], fallback[0], fallback[1], fallback[2], True
            return None, last_parsed, sent, attempt - 1, True
        sent = [dict(m) for m in convo]
        try:
            res = llm_call(sent, format=schema, model=model, temperature=0,
                           options={"num_predict": NUM_PREDICT})
            if inspect.isawaitable(res):
                res = await res
        except Exception as e:  # the model backend itself failed
            if fallback is not None:                   # the soft re-ask failed: keep the valid reply
                return fallback[0], fallback[0], fallback[1], fallback[2], out_of_time
            raise _LlmUnavailable(f"{type(e).__name__}: {e}") from e
        content = _content_of(res)
        if content.strip().startswith(_OLLAMA_TIMEOUT_SENTINEL):
            if fallback is not None:
                return fallback[0], fallback[0], fallback[1], fallback[2], out_of_time
            raise _LlmUnavailable(content.strip().splitlines()[0])
        obj, perr = _parse_reply(content)
        errs = [perr] if perr else checker(obj)
        if obj is not None:
            last_parsed = obj
        if not errs and soft_checker is not None and not soft_asked and attempt < MAX_TRIES:
            errs = soft_checker(obj)
            soft_asked = bool(errs)
            if errs:
                fallback = (obj, sent, attempt)
        if not errs:
            return obj, last_parsed, sent, attempt, out_of_time
        if fallback is not None and attempt == MAX_TRIES:
            return fallback[0], fallback[0], fallback[1], fallback[2], out_of_time
        if attempt < MAX_TRIES:
            convo = sent + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": "That reply could not be used: " + "; ".join(errs[:12])
                 + ". Reply again with the corrected JSON object only."},
            ]
    return None, last_parsed, sent, MAX_TRIES, out_of_time


def _understood(obj: Any) -> Optional[str]:
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("ops"), list):
        kinds = [str(o.get("op")) for o in obj["ops"] if isinstance(o, dict) and o.get("op")]
        return ("changes: " + ", ".join(kinds)) if kinds else None
    title = obj.get("title") if isinstance(obj.get("title"), str) else ""
    kinds = [str(s.get("kind")) for s in obj.get("symbols") or [] if isinstance(s, dict) and s.get("kind")]
    if not title and not kinds:
        return None
    return (title.strip() or "a display") + (": " + ", ".join(kinds) if kinds else "")


# ── The engine ─────────────────────────────────────────────────────────

def _dedupe(notes: Iterable[str]) -> List[str]:
    """Identical notes once, first-seen order."""
    seen: set = set()
    return [n for n in notes if not (n in seen or seen.add(n))]


_QUANT = frozenset(("every", "all", "each"))
_QUANT_SKIP = frozenset(("the", "of", "other", "my", "our", "these", "those", "remaining", "rest"))


def _singular(w: str) -> str:
    for suf in ("es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def repeat_template_asked(request: str, templates: Sequence[Dict[str, Any]]) -> Optional[str]:
    """The template whose every element the request asks for ("do the same for all pumps",
    "every storage tank"), else None. Not produced in v1 (Settled at the D5 gate): the editor's
    Duplicate for... → as a collection does it. Deterministic: every / all / each, then within 4
    words (skipping the / of / other ...) a word that is, singular or plural, a word (≥ 3
    characters) of a template's name."""
    toks = [name_key(t) for t in re.findall(r"[\w\-]+", request or "")]
    words: Dict[str, str] = {}
    for t in templates:
        nm = str(t.get("name") or "")
        for w in re.findall(r"[\w\-]+", nm):
            if len(w) >= 3:
                words.setdefault(name_key(w), nm)
    for i, t in enumerate(toks):
        if t not in _QUANT:
            continue
        seen = 0
        for nxt in toks[i + 1:]:
            if nxt in _QUANT_SKIP:
                continue
            for cand in (nxt, _singular(nxt)):
                if cand in words:
                    return words[cand]
            seen += 1
            if seen >= 4:
                break
    return None


# ── The user's own names (D5.1, design_displays §6.2) ───────────────────

_NAME_TRIGGER_RE = re.compile(r"\b(?:(?:call|name|label)\s+it|called|named)\s+", re.IGNORECASE)
_QUOTES = {'"': '"', "'": "'", "“": "”", "‘": "’", "`": "`"}
# Where a clause starts: sentence / clause punctuation, or a joining word.
_CLAUSE_START_RE = re.compile(r"(?:[,;:!?\n]|\.(?=\s|$)|\b(?:and|with|plus|then)\b)", re.IGNORECASE)


def user_names(request: str) -> List[Tuple[str, str]]:
    """The names the user gives in the request: `call it X` / `name it X` / `label it X` /
    `called X` / `named X`, X = the quoted text, else the following word or words up to the next
    punctuation (a `.` only when it ends a sentence) or the next such phrase. Returns
    `[(X, the clause holding it)]` in request order; the clause runs from the previous clause
    boundary (punctuation, and / with / plus / then) to the phrase."""
    text = request or ""
    trig = list(_NAME_TRIGGER_RE.finditer(text))
    out: List[Tuple[str, str]] = []
    for i, m in enumerate(trig):
        rest_end = trig[i + 1].start() if i + 1 < len(trig) else len(text)
        rest = text[m.end():rest_end]
        name = ""
        if rest[:1] in _QUOTES:
            close = rest.find(_QUOTES[rest[0]], 1)
            if close > 1:
                name = rest[1:close]
        if not name:
            stop = re.search(r"[,;:!?\n\"“”()]|\.(?=\s|$)", rest)
            name = rest[:stop.start()] if stop else rest
        name = _clean_text(name, 200).strip(" '‘’")
        if not name:
            continue
        head = text[:m.start()]
        bounds = list(_CLAUSE_START_RE.finditer(head))
        clause = head[bounds[-1].end():] if bounds else head
        out.append((name, clause))
    return out


def _bind_words(s: Dict[str, Any]) -> set:
    """The words (≥ 3 characters, name_key) of an AUTHORING symbol's binding names."""
    out: set = set()
    for b in s.get("bind") or []:
        if isinstance(b, str):
            for part in b.split("|"):
                out.update(name_key(w) for w in _words(part))
                if part.strip():
                    out.add(name_key(part.strip()))
    return out


def apply_user_names(request: str, symbols: List[Dict[str, Any]]) -> List[str]:
    """Gives each name the user gave to the ONE symbol produced for it (design_displays §6.2):
    the only symbol when there is one, else the one symbol whose binding words occur in the clause
    holding the name (then, failing that, the one symbol whose kind the clause names). Its label
    becomes the name when it is empty or differs. Mutates the AUTHORING `symbols`; returns the
    names placed. Deterministic; never guesses between two candidates."""
    placed: List[str] = []
    taken: set = set()
    syms = [s for s in symbols if isinstance(s, dict)]
    for name, clause in user_names(request):
        free = [i for i in range(len(syms)) if i not in taken]
        target: Optional[int] = None
        if len(syms) == 1:
            target = 0 if 0 in free else None
        else:
            cw = {name_key(w) for w in _words(clause)}
            hits = [i for i in free if _bind_words(syms[i]) & cw]
            if len(hits) != 1:
                hits = [i for i in free if name_key(str(syms[i].get("kind") or "")) in cw]
            target = hits[0] if len(hits) == 1 else None
        if target is None:
            continue
        taken.add(target)
        s = syms[target]
        if not isinstance(s.get("label"), str) or s["label"].strip() != name:
            s["label"] = name
        placed.append(name)
    return placed


def _suggest(g: Optional["_Grounding"], avoid: Sequence[str] = ()) -> List[str]:
    """Up to three displays the grounding shows CAN be drawn - existing names only, never an
    invented one. The counterpart of calc_authoring._suggestions, and held to the same rule
    (R251 / R26: nothing is offered that the historian did not just return).

    An element with a template comes first (a template is what the symbol palette draws best),
    then any other grounded element, then a trend of a real tag. `avoid` drops the nouns that
    were not found, so the suggestions never echo the thing that just failed."""
    if g is None:
        return []
    avoid_k = {name_key(a) for a in avoid}
    out: List[str] = []

    def add(text: str) -> None:
        if text not in out:
            out.append(text)

    for e in list(g.named) + list(g.elements):
        name = str(e.get("name") or "").strip()
        if not name or name_key(name) in avoid_k:
            continue
        tmpl = str(e.get("template_name") or "").strip()
        add(f"A display of {name}" + (f" ({tmpl})" if tmpl else ""))
        if len(out) >= 2:
            break
    for t in g.tags:
        name = str(t.get("name") or "").strip()
        if not name or name_key(name) in avoid_k:
            continue
        add(f"A trend of {name}")
        break
    return out[:3]


def _fail(code: str, error: str, notes: List[str], understood: Optional[str] = None,
          *, missing: Sequence[str] = (), suggestions: Sequence[str] = ()) -> Dict[str, Any]:
    """A failure, in the SAME shape calc_authoring._fail returns.

    `reason`, `missing` and `suggestions` used to be calcs-only, so a display that failed gave the
    viewer a bare `error` string and nothing to offer the tester next - while an identical calc
    failure came back with the quantities the plant has no data for and up to three requests that
    would work. The viewer therefore needed two code paths for one feature, and the display one
    was the poorer of the two. These keys are now always present on both engines (empty when
    there is nothing to say), so one renderer serves both.

    `missing` = the request's nouns nothing in this historian matched (D5.1 `unmatched`), which is
    the display analogue of the calc engine's unmet quantities."""
    out: Dict[str, Any] = {"ok": False, "code": code, "error": error, "notes": _dedupe(notes),
                           "reason": error, "understood": understood,
                           "missing": list(missing), "suggestions": list(suggestions)}
    return out


async def draft_display(request: str, *, ground: Any, llm_call: Optional[Callable[..., Any]] = None,
                        model: Optional[str] = None, current: Optional[Dict[str, Any]] = None,
                        locked_ids: Iterable[str] = (), footprints: Any = None) -> Dict[str, Any]:
    """Draft (or edit) a PremHist display from a plain-English request. **Never raises** - see
    `_draft_display` for the arguments and the result. Every answer is one of the documented
    shapes, so an unexpected failure anywhere inside is a visible `no_draft`, not an exception.

    calc_authoring.draft_calc has always had this guard ("never raises ... a bug is a visible
    failure"); this one did not, although its docstring promised the same shapes. Anything that
    escaped - a model reply deep enough to make json.loads raise RecursionError rather than the
    ValueError `_parse_reply` catches, or a bug in any of the ops, geometry or mapping code - went
    straight through routes_displays, which does not wrap it either, and the tester got an opaque
    HTTP 500 from the Describe service instead of a reason.

    This is also where the outcome is written to `display_describe_log` - here rather than inside
    `_draft_display`, so that the line is written for EVERY answer including the caught-exception
    one above. A crash is the outcome most worth having on disk, and it is the one a log inside
    the drafting body would miss."""
    t0 = time.monotonic()
    try:
        res = await _draft_display(request, ground=ground, llm_call=llm_call, model=model,
                                   current=current, locked_ids=locked_ids, footprints=footprints)
    except Exception as e:  # noqa: BLE001 - the one place a bug becomes a visible failure
        res = _fail("no_draft", f"drafting failed: {type(e).__name__}: {e}", [])
    try:
        cur = current if isinstance(current, dict) else None
        _dlog.record(request, {"follow_up": bool(cur and cur.get("symbols")),
                               "locked": len(list(locked_ids or ())) or None},
                     res, time.monotonic() - t0)
    except Exception:          # a log must not break drafting (and `record` never raises anyway)
        pass
    return res


async def _draft_display(request: str, *, ground: Any, llm_call: Optional[Callable[..., Any]] = None,
                         model: Optional[str] = None, current: Optional[Dict[str, Any]] = None,
                         locked_ids: Iterable[str] = (), footprints: Any = None) -> Dict[str, Any]:
    """Draft (or edit) a PremHist display from a plain-English request. **A coroutine** — await it.

    - `footprints` (D5.2): the page's measured drawn boxes of the current symbols,
      `{"<id>": [x, y, w, h]}` canvas px; symbols without one get a predicted footprint.

    - `ground`: duck-typed `search_points(q)`, `element_by_path(path)`, `children(element_id)`,
      `search_elements(q=...)`, `templates()` (sync or async), e.g. premhist_rest.PremhistRest.
    - `llm_call(messages, *, format, model, temperature, options)` → a message dict
      (`{"content": ...}`) or text, awaited when it returns an awaitable; default
      `llm_runtime.call_ollama_chat`. `model` defaults to settings' chat model.
    - `current`: the editor's document. None, or one with no symbols = a first request (the model
      fills AUTHORING_SCHEMA); otherwise a follow-up (the model returns OPS_SCHEMA ops, applied
      here; `locked_ids` / `locked: true` symbols are never changed — design_displays §6.4).

    The result's `authoring` records `elapsed_s` and `model_calls` (repairs included); when more
    than one call was made the first note says "drafted in N s (M model calls) — …" (§6.4 item 8).

    Returns `{ok: True, display, placeholders, refused_ops, notes, authoring, seconds}` or
    `{ok: False, code: llm_unavailable | no_draft | too_broad | bad_request, error, understood?,
    notes}`. Nothing is saved or published here."""
    t0 = time.monotonic()
    notes: List[str] = []
    if not isinstance(request, str) or not request.strip():
        return _fail("bad_request", "describe the display you want", notes)
    if len(request) > MAX_REQUEST_CHARS:
        return _fail("bad_request", f"the description is over {MAX_REQUEST_CHARS} characters", notes)
    if current is not None and (not isinstance(current, dict)
                                or not isinstance(current.get("symbols", []), list)):
        return _fail("bad_request", "the current display is not a display document", notes)
    locked = {str(x) for x in (locked_ids or ())}
    follow_up = isinstance(current, dict) and bool(current.get("symbols"))
    W, H, grid = _canvas_of(current)

    try:
        g = await _ground(ground, request, current if isinstance(current, dict) else None)
    except Exception as e:
        return _fail("bad_request", f"could not read PremHist to ground the request: {e}", notes)
    internal: Dict[str, Any] = {"grounding": list(g.notes)} if g.notes else {}   # not user notes (§6.3)
    points: Dict[Any, Dict[str, Any]] = {}
    values: Dict[str, str] = {}
    if follow_up:
        points = await _lookup_points(ground, _bound_point_ids(current, g))  # type: ignore[arg-type]
        values = _value_texts(current, g, points)  # type: ignore[arg-type]

    system = _system_prompt(follow_up)
    user = "REQUEST: " + request.strip() + "\n\n" + _grounding_text(g)
    if follow_up:
        user = _compact_view(current, g, W, H, locked, points, _measured(footprints),  # type: ignore[arg-type]
                             values) + "\n\n" + user
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    est = sum(len(m["content"]) for m in messages) // 4
    if est > MAX_PROMPT_TOKENS:
        return _fail("too_broad", "that matches too much of the plant to draft at once — which unit / "
                     "area do you mean?", notes, missing=g.unmatched,
                     suggestions=_suggest(g, g.unmatched))

    if model is None:
        try:
            from .settings import get_settings
            model = get_settings().ollama_model
        except Exception:
            model = "default"

    if isinstance(current, dict) and out_of_scope_only(request):
        # Battery fix 4: undo / redo / publish / save only → no model call, no change, a pointer.
        authoring = {"kind": "llm_proposed", "model": str(model), "prompt_sha256": _sha(messages),
                     "run_id": uuid.uuid4().hex, "request": request,
                     "elapsed_s": round(time.monotonic() - t0, 3), "model_calls": 0}
        return {"ok": True, "display": copy.deepcopy(current), "placeholders": [], "refused_ops": [],
                "notes": _dedupe(_scope_notes(request) + ["no change was made — that is not something "
                                                          "Describe does"]),
                "missing": [], "suggestions": [],
                "authoring": dict(authoring, **internal), "seconds": round(time.monotonic() - t0, 3)}

    repeat_of = repeat_template_asked(request, g.templates)
    repeat_note = (f"to repeat this for every {repeat_of}, select it and use Duplicate for… → as a "
                   "collection") if repeat_of else None
    if repeat_note and follow_up:
        # Not produced in v1: the draft comes back unchanged, no model call (Settled at the D5 gate).
        notes.append(repeat_note)
        authoring = {"kind": "llm_proposed", "model": str(model), "prompt_sha256": _sha(messages),
                     "run_id": uuid.uuid4().hex, "request": request,
                     "elapsed_s": round(time.monotonic() - t0, 3), "model_calls": 0}
        return {"ok": True, "display": copy.deepcopy(current), "placeholders": [], "refused_ops": [],
                "notes": _dedupe(notes), "missing": [], "suggestions": [], "authoring": authoring,
                "seconds": round(time.monotonic() - t0, 3)}

    if llm_call is None:
        try:
            from .llm_runtime import call_ollama_chat
        except Exception as e:
            return _fail("llm_unavailable", f"the local model runtime is not available: {e}", notes)
        llm_call = _default_llm_call(call_ollama_chat)
    # §6.4 item 8: every call to the model in this Describe is counted, repairs included.
    calls = [0]
    base_call = llm_call

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls[0] += 1
        return base_call(*args, **kwargs)

    llm_call = counted

    try:
        existing = [str(x.get("id")) for x in _walk((current or {}).get("symbols") or [])] if follow_up else []
        on_display = list((current or {}).get("symbols") or []) if follow_up else []
        # D3.4 (§5.7.8 item 5): the request's glyph words are applied before the reply is checked;
        # a glyph nothing maps (no request word) is a schema error the model is asked to repair, and
        # remembered so the user is told when the final reply has no equipment symbol at all
        unknown_glyphs: List[str] = []

        def eq_check(o: Any) -> List[str]:
            normalize_equipment(o, request, on_display)
            for s in _equipment_of(o):
                gv = s.get("glyph")
                if glyph_of(gv) is None and isinstance(gv, str) and gv.strip() and \
                        _clean_text(gv, 40) not in unknown_glyphs:
                    unknown_glyphs.append(_clean_text(gv, 40))
            return check_ops(o) if follow_up else check_authoring(o)

        obj, last, sent, tries, timed_out = await _ask_model(
            llm_call, messages, OPS_SCHEMA if follow_up else AUTHORING_SCHEMA, str(model), eq_check,
            soft_checker=(lambda o: unknown_id_errors(o, existing)) if follow_up else None,
            deadline=t0 + TOTAL_BUDGET_S, round_ceiling_s=_round_ceiling_s())
    except _LlmUnavailable as e:
        return _fail("llm_unavailable", f"the local model did not answer: {e}", notes)
    if timed_out:
        notes.append(f"stopped after {calls[0]} model call(s): drafting has a {TOTAL_BUDGET_S:.0f} s budget, "
                     "because PremHist gives up on a Describe that takes longer")
    if obj is None:
        return _fail("no_draft", "could not build that — here is what I understood", notes,
                     _understood(last), missing=g.unmatched, suggestions=_suggest(g, g.unmatched))
    if unknown_glyphs and not _equipment_of(obj):
        notes.append(f"'{unknown_glyphs[0]}' is not an equipment symbol Describe can draw — the Equipment "
                     f"palette lists the {len(GLYPHS)} it draws (pump, valve, tank, turbine, breaker, ...)")

    authoring = {"kind": "llm_proposed", "model": str(model), "prompt_sha256": _sha(sent),
                 "run_id": uuid.uuid4().hex, "request": request}
    lift = caps_lifted(request)
    images = await _lookup_images(ground, obj)
    await _lookup_elements(ground, obj, g)
    if follow_up:
        # D5.1: a name the user gives goes to the symbol an `add` produced for it.
        apply_user_names(request, [o["symbol"] for o in obj["ops"]
                                   if o.get("op") == "add" and isinstance(o.get("symbol"), dict)])
        out = _apply_ops(obj, current, g, request, locked, W, H, grid, notes, lift,  # type: ignore[arg-type]
                         footprints=footprints, values=values, points=points, images=images)
    else:
        placed = apply_user_names(request, obj["symbols"])
        # The model put the user's name only in the title of a display that already has its
        # name (New): the title stays the display's name; the symbol carries the user's name.
        cur_name = _clean_text(current.get("name"), 200) if isinstance(current, dict) else ""
        if cur_name and isinstance(obj.get("title"), str) and \
                any(name_key(obj["title"].strip()) == name_key(n) for n in placed):
            obj["title"] = cur_name
        out = _build_first(obj, current, g, W, H, grid, notes, lift, request=request, images=images)
    doc = out["display"]
    if out.get("changed", True):
        doc["authoring"] = dict(authoring)
    notes.extend(_no_match_notes(g, doc, out["placeholders"]))
    checks = _self_check(doc)
    if checks:
        internal["checks"] = checks
    if repeat_note:
        notes.append(repeat_note)
    for rx, text in _OUT_OF_SCOPE:                     # §6.3 item 5: pointed elsewhere
        if rx.search(request):
            notes.append(text)
    # Repair rounds, grounding counts and self-checks are not user notes (D5.1, §6.3 item 5):
    # they stay in the result's provenance. (The document's own `authoring` keeps §3's fields.)
    elapsed = time.monotonic() - t0
    authoring = dict(authoring, repairs=tries - 1, elapsed_s=round(elapsed, 3), model_calls=calls[0],
                     **internal)
    notes = _dedupe(notes)
    if calls[0] > 1:                                   # §6.4 item 8: time is visible
        head = f"drafted in {elapsed:.1f} s ({calls[0]} model calls)"
        notes = [f"{head} — {notes[0]}" if notes else head] + notes[1:]
    # `missing` on a SUCCESSFUL draft is not a contradiction: a draft with placeholders is a
    # partial success, and the nouns nothing matched are why. `_no_match_notes` already says so in
    # prose; this is the same fact in a field the viewer can render next to the placeholders
    # instead of parsing a sentence. `suggestions` stays empty - something was drawn, so there is
    # nothing to offer instead.
    return {"ok": True, "display": doc, "placeholders": out["placeholders"],
            "refused_ops": out.get("refused_ops", []), "notes": notes,
            "missing": list(g.unmatched), "suggestions": [], "authoring": authoring,
            "seconds": round(elapsed, 3)}


async def preview_grounding(request: str, *, ground: Any, current: Optional[Dict[str, Any]] = None,
                            locked_ids: Iterable[str] = (), footprints: Any = None,
                            model: Optional[str] = None) -> Dict[str, Any]:
    """What the model WOULD be shown for this display request, without asking it. **A coroutine.**
    Never raises.

    The counterpart of calc_authoring.preview_grounding, and useful for the same reason: a display
    that came back with placeholders ("not found: Pump Flow") or with the wrong symbols is almost
    always a grounding problem, and the grounding is built per request and never stored. This runs
    the grounding and the prompt build and stops before the model.

    Returns `{ok: True, kind: "display", follow_up, messages: [{role, content}], chars,
    est_tokens, token_limit, too_broad, tags: [...], elements: [...], templates: [...], nouns:
    [...], seconds}`."""
    t0 = time.monotonic()
    try:
        if not isinstance(request, str) or not request.strip():
            return _fail("bad_request", "describe the display you want", [])
        if len(request) > MAX_REQUEST_CHARS:
            return _fail("bad_request", f"the description is over {MAX_REQUEST_CHARS} characters", [])
        if current is not None and (not isinstance(current, dict)
                                    or not isinstance(current.get("symbols", []), list)):
            return _fail("bad_request", "the current display is not a display document", [])
        locked = {str(x) for x in (locked_ids or ())}
        follow_up = isinstance(current, dict) and bool(current.get("symbols"))
        W, H, grid = _canvas_of(current)
        g = await _ground(ground, request, current if isinstance(current, dict) else None)
        points: Dict[Any, Dict[str, Any]] = {}
        values: Dict[str, str] = {}
        if follow_up:
            points = await _lookup_points(ground, _bound_point_ids(current, g))  # type: ignore[arg-type]
            values = _value_texts(current, g, points)  # type: ignore[arg-type]
        user = "REQUEST: " + request.strip() + "\n\n" + _grounding_text(g)
        if follow_up:
            user = _compact_view(current, g, W, H, locked, points, _measured(footprints),  # type: ignore[arg-type]
                                 values) + "\n\n" + user
        messages = [{"role": "system", "content": _system_prompt(follow_up)},
                    {"role": "user", "content": user}]
        chars = sum(len(m["content"]) for m in messages)
        return {
            "ok": True, "kind": "display", "follow_up": follow_up,
            "canvas": {"w": W, "h": H, "grid": grid},
            "messages": messages,
            "chars": chars, "est_tokens": chars // 4, "token_limit": MAX_PROMPT_TOKENS,
            "too_broad": chars // 4 > MAX_PROMPT_TOKENS,
            "nouns": nouns_of(request),
            "tags": [str(p.get("name")) for p in (g.tags or [])],
            "elements": [str(e.get("path") or e.get("name")) for e in (g.elements or [])],
            "named": [str(e.get("path") or e.get("name")) for e in (g.named or [])],
            "templates": [str(t.get("name")) for t in (g.templates or [])],
            # D5.1: request nouns no search matched. These are what come back as
            # "not found: <name>" placeholders, so this is the first field to read when a draft
            # arrives full of them.
            "unmatched": list(g.unmatched or []),
            "glyph_words": [list(h[0]) for h in request_glyphs(request)],
            "locked": sorted(locked),
            "notes": list(g.notes),
            "seconds": round(time.monotonic() - t0, 3),
        }
    except Exception as e:  # noqa: BLE001 - as draft_display: a bug is a visible failure
        return _fail("bad_request", f"could not build the grounding: {type(e).__name__}: {e}", [])


def _bound_point_ids(current: Dict[str, Any], g: _Grounding) -> List[Any]:
    """Every point id the current display reaches: {point} bindings and the points its attribute
    bindings are bound to (the context element's, a fixed element's)."""
    ids: List[Any] = []
    ctx = current.get("context") if isinstance(current.get("context"), dict) else None
    ctx_el = g.element_by_path_key(ctx["default"]) if ctx and isinstance(ctx.get("default"), str) else None
    for s in _walk(current.get("symbols") or []):
        for b in s.get("bind") or []:
            pid = _binding_point(b, ctx_el, g)
            if pid is not None and pid not in ids:
                ids.append(pid)
    return ids[:200]


def _binding_point(b: Any, ctx_el: Optional[Dict[str, Any]], g: _Grounding) -> Any:
    if not isinstance(b, dict):
        return None
    if b.get("point") is not None:
        return b["point"]
    if b.get("attr"):
        el = g.find_element(str(b["element"])) if b.get("element") else ctx_el
        for a in (el or {}).get("attributes") or []:
            if isinstance(a, dict) and name_key(str(a.get("name"))) == name_key(str(b["attr"])):
                v = a.get("value") or {}
                return v.get("point") if v.get("kind") == "tag" else None
    return None


async def _lookup_points(ground: Any, ids: List[Any]) -> Dict[Any, Dict[str, Any]]:
    """Points by id through `ground.points(ids)` / `ground.point(id)` when the ground has them
    (PremhistRest does; a test fake may not) — so the compact view names tags, never "point #N"."""
    if not ids:
        return {}
    try:
        if callable(getattr(ground, "points", None)):
            got = await _g(ground.points, list(ids))
            if isinstance(got, dict):
                return {k: v for k, v in got.items() if isinstance(v, dict)}
            return {p["id"]: p for p in (got or []) if isinstance(p, dict) and p.get("id") is not None}
        if callable(getattr(ground, "point", None)):
            out: Dict[Any, Dict[str, Any]] = {}
            for i in ids:
                p = await _g(ground.point, i)
                if isinstance(p, dict):
                    out[i] = p
            return out
    except Exception:
        return {}
    return {}


def _value_texts(current: Dict[str, Any], g: _Grounding, points: Dict[Any, Dict[str, Any]]) -> Dict[str, str]:
    """symbol id → its first binding's value text now (with units), for the compact view."""
    ctx = current.get("context") if isinstance(current.get("context"), dict) else None
    ctx_el = g.element_by_path_key(ctx["default"]) if ctx and isinstance(ctx.get("default"), str) else None
    by_id = {p.get("id"): p for p in g.tags}
    by_id.update(points)
    out: Dict[str, str] = {}
    for s in _walk(current.get("symbols") or []):
        for b in (s.get("bind") or [])[:1]:
            p = by_id.get(_binding_point(b, ctx_el, g))
            latest = (p or {}).get("latest") or {}
            vt = latest.get("value_text")
            if isinstance(vt, str) and vt:
                states = (p or {}).get("states") or {}
                if isinstance(states, dict) and vt in states:
                    vt = f"{states[vt]} ({vt})"
                u = (p or {}).get("eng_units")
                out[str(s.get("id"))] = vt + (f" {u}" if u and u != "state" else "")
    return out


async def _lookup_images(ground: Any, obj: Any) -> Dict[str, str]:
    """name_key(image name) → sha256 for the image names the reply uses (unique names only)."""
    wanted = False
    for d in _walk_dicts(obj):
        v = d.get("image")
        if isinstance(v, str) and not re.match(r"^[0-9a-fA-F]{64}$", v.strip()):
            wanted = True
            break
    if not wanted or not callable(getattr(ground, "images", None)):
        return {}
    try:
        rows = await _g(ground.images) or []
    except Exception:
        return {}
    seen: Dict[str, List[str]] = {}
    for r in rows:
        if isinstance(r, dict) and r.get("name") and r.get("sha256"):
            seen.setdefault(name_key(str(r["name"])), []).append(str(r["sha256"]))
    return {k: v[0] for k, v in seen.items() if len(v) == 1}


async def _lookup_elements(ground: Any, obj: Any, g: _Grounding) -> None:
    """Element paths the reply sets (a table's `element`) that the grounding did not list are read
    (`element_by_path`) so the property can be checked against what exists."""
    for d in _walk_dicts(obj):
        v = d.get("element")
        if isinstance(v, str) and v.strip().startswith("/") and g.find_element(v) is None:
            try:
                e = await _g(ground.element_by_path, v.strip())
            except Exception:
                e = None
            if isinstance(e, dict) and e.get("path"):
                g.elements.append(e)


def _walk_dicts(o: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk_dicts(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk_dicts(v)


def _no_match_notes(g: _Grounding, doc: Dict[str, Any], placeholders: List[Dict[str, Any]]) -> List[str]:
    """D5.1: a request noun that no search found and that the draft tried to bind (a word of a
    placeholder's name) → "nothing in this historian matches '<noun>' — drag a tag onto <label>
    to bind it"."""
    if not g.unmatched or not placeholders:
        return []
    by_id = {str(s.get("id")): s for s in _walk(doc.get("symbols") or [])}
    W, H, _ = _canvas_of(doc)
    names = _Editor(doc.get("symbols") or [], W, H, GRID).names     # never an id (§6.4 item 7)
    out: List[str] = []
    for noun in g.unmatched:
        nk = name_key(noun)
        for p in placeholders:
            nm = str(p.get("name") or "")
            if nk != name_key(nm.strip()) and nk not in {name_key(w) for w in _words(nm)}:
                continue
            s = by_id.get(str(p.get("symbol")), {})
            label = _clean_text(s.get("label") or "", 200) or (_sym_name(s, names) if s else "the symbol")
            out.append(f"nothing in this historian matches '{noun}' — drag a tag onto {label} to bind it")
    return out


def _resolve_context(draft_ctx: Any, g: _Grounding, notes: List[str]
                     ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """The draft's context → (document context, the context element for {attr}, an element to
    bind with {attr, element} when it has no template)."""
    if not isinstance(draft_ctx, dict) or not isinstance(draft_ctx.get("element"), str):
        return None, None, None
    e = g.find_element(draft_ctx["element"])
    if e is None:
        notes.append(f"context element not found: {draft_ctx['element'].strip()} — no context was set")
        return None, None, None
    tn = e.get("template_name")
    if not tn:
        notes.append(f"{e['path']} has no template, so the display has no context; its attributes "
                     "are bound to it directly")
        return None, None, e
    return {"template": tn, "default": e["path"]}, e, None


def _build_first(obj: Dict[str, Any], current: Optional[Dict[str, Any]], g: _Grounding,
                 W: int, H: int, grid: int, notes: List[str], lift: bool = False,
                 request: str = "", images: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    title = _clean_text(obj.get("title"), 200)
    ctx_doc, ctx_el, fixed_el = _resolve_context(obj.get("context"), g, notes)
    if fixed_el is not None and all(_el_id(x) != _el_id(fixed_el) for x in g.named):
        g.named.append(fixed_el)
    if ctx_doc is None and isinstance(current, dict) and isinstance(current.get("context"), dict):
        cpath = current["context"].get("default")
        ce = g.element_by_path_key(cpath) if isinstance(cpath, str) else None
        if ce is not None and ce.get("template_name"):
            ctx_doc, ctx_el = dict(current["context"]), ce
    res = _Resolver(g, ctx_el)

    range_missing: List[str] = []
    leaves: List[Dict[str, Any]] = []
    missing_of: List[Tuple[Dict[str, Any], List[str]]] = []
    pts = {p.get("id"): p for p in g.tags if p.get("id") is not None}
    # D3.6 (§5.7.10 item 9): a draft pipe has no box; it is made once every symbol has its id (below)
    pipe_drafts = [s for s in obj["symbols"] if isinstance(s, dict) and s.get("kind") == display_pipes.DRAFT_KIND]
    drafted: List[Dict[str, Any]] = []                 # the reply's other symbols, in its order
    for s in obj["symbols"]:
        if isinstance(s, dict) and s.get("kind") == display_pipes.DRAFT_KIND:
            continue
        sym, missing = _map_symbol(s, res, W, H, grid, range_missing)
        drafted.append(sym)
        notes.extend(sym.pop("_notes", []))
        for k_id in ("label", "text"):
            if isinstance(sym.get(k_id), str) and _ID_LIKE_RE.match(sym[k_id]):
                sym.pop(k_id)                          # an id where words belong (corpus C098)
        props = _symbol_props(s)                       # D5.2: every properties-panel field
        if props:
            _, bad = apply_props(sym, props, request=request, W=W, H=H, grid=grid, g=g, images=images)
            for b in bad:
                notes.append(f"{_sym_name(sym)}: {b}")
        default_decimals(sym, pts, ctx_el, g)          # D5.2: 2 decimals on float points
        leaves.append(sym)
        if missing:
            missing_of.append((sym, missing))

    boxes = [(_box(s) if "x" in s else None) for s in leaves]
    usable = all(b is not None for b in boxes) and not (len(boxes) > 1 and len(set(boxes)) == 1)
    canvas_h = H
    if usable:
        # §6.5 item 3: every symbol of a first draft is added by Describe too — the human-scale
        # default of its kind unless the request talks about its size (then the model's, never
        # below the floor); the D5.1 size caps are superseded (the tidy pass runs uncapped).
        for s in leaves:
            _, small = human_size(s, request, W, H, grid, keep_center=False)
            if small:
                notes.append(f"{_sym_name(s)} is smaller than is easy to read")
    if not usable:
        for s in leaves:
            for k in ("x", "y", "w", "h"):
                s.setdefault(k, 0)
        leaves, canvas_h = fallback_layout(leaves, title, W, H, grid)
        notes.append("the assistant gave no usable positions; laid out as header, values, trends, tables")
        if canvas_h > H:
            notes.append(f"the canvas was made {canvas_h} px tall to fit the standard layout")
    # The caps bound the MODEL's sizes; the fallback layout is the engine's own (its full-width
    # title row is not a model's oversized text).
    # The tidy pass (frames), then the D5.2 no-overlap rule on FOOTPRINTS: each symbol, in output
    # order, is kept clear of the ones before it (the nearest free spot, else smaller); what still
    # cannot fit is said.
    tidy(leaves, W, canvas_h, grid, caps=False)
    ed = _Editor(leaves, W, canvas_h, grid)
    for i, sym in enumerate(leaves):
        why, _more = ed.settle(sym, among=leaves[:i], own=True, H=canvas_h)
        if why:
            notes.append(f"{_sym_name(sym)} overlaps: {why}")
    for nm, dss in res.ambiguous.items():
        notes.append(f"{nm} is a tag in several datasets ({', '.join(dss)}); name it as "
                     f"<dataset>.{nm} to bind it")
    if range_missing:
        notes.append("set the range (min / max) of: " + ", ".join(range_missing))
    symbols = _group(leaves)

    start = 1
    if isinstance(current, dict):
        start = max(1, _as_int(current.get("next_symbol_id")) or 1, _max_id(current.get("symbols") or []) + 1)
    nxt = _assign_ids(symbols, start)
    placeholders = [{"symbol": sym["id"], "name": n} for sym, ms in missing_of for n in ms]
    if pipe_drafts or display_pipes.parse_connects(request):
        nxt = _first_draft_pipes(request, pipe_drafts, drafted, symbols, nxt, res, W, canvas_h, grid, notes)

    doc: Dict[str, Any] = copy.deepcopy(current) if isinstance(current, dict) else {}
    doc.update({"format": 1, "kind": "premhist-display", "name": title})
    doc.setdefault("id", 0)
    doc.setdefault("description", None)
    doc.setdefault("role", "display")
    doc.setdefault("version", 0)
    doc.setdefault("scale", "fit")
    canvas = dict(doc["canvas"]) if isinstance(doc.get("canvas"), dict) else {}
    canvas.update({"width": W, "height": canvas_h, "grid": grid})
    canvas.setdefault("background", "theme")
    canvas.setdefault("layout", "free")
    doc["canvas"] = canvas
    if ctx_doc is not None:
        doc["context"] = ctx_doc
    else:
        doc.setdefault("context", None)
    tm = doc.get("time") if isinstance(doc.get("time"), dict) and doc["time"].get("mode") == "live" else None
    tm = dict(tm) if tm else {"mode": "live", "window": "1h", "refresh_ms": 1000}
    if obj.get("window") in WINDOWS:
        tm["window"] = obj["window"]
    doc["time"] = tm
    doc["symbols"] = symbols
    doc["next_symbol_id"] = nxt
    by_id = {str(s.get("id")): _sym_name(s) for s in _walk(symbols) if isinstance(s, dict)}
    notes[:] = [_scrub_ids(n, by_id) for n in notes]   # never a bare symbol id in a note (§6.4 item 7)
    return {"display": doc, "placeholders": placeholders, "refused_ops": []}


def _first_draft_pipes(request: str, pipe_drafts: List[Dict[str, Any]], drafted: List[Dict[str, Any]],
                       symbols: List[Any], nxt: int, res: "_Resolver", W: int, H: int, grid: int,
                       notes: List[str]) -> int:
    """D3.6 (§5.7.10 item 9) on a first draft: the reply's `kind: "pipe"` symbols and the request's
    own connect phrases → auto-routed pipes at the bottom of the stacking order, once every drafted
    symbol has its id. A draft ref "s<N>" is the N-th symbol of the reply (the model guesses the ids
    in order, §6.3); a label or equipment words name a drafted symbol as every reference does.
    Returns the next free id."""
    box = [nxt]

    def new_id() -> str:
        box[0] += 1
        return f"s{box[0] - 1}"

    def resolve(r: str) -> Optional[List[Dict[str, Any]]]:
        m = re.fullmatch(r"\s*s([1-9]\d*)\s*", r or "")
        if m and int(m.group(1)) <= len(drafted):
            return [drafted[int(m.group(1)) - 1]]
        return None

    ed = _Editor(symbols, W, H, grid)
    conn = display_pipes.Connector(request, symbols=lambda: [x for x in _walk(symbols) if isinstance(x, dict)],
                                   new_id=new_id, resolve=resolve, box_of=ed.abs_box, bind_name=res.resolve)
    made = [conn.from_draft(s) for s in pipe_drafts] + conn.rest()
    symbols[0:0] = [p for p, _n in made if p is not None]     # the bottom of the stacking order
    notes.extend(n for _p, n in made if n)
    return box[0]


def _index(symbols: List[Any]) -> Dict[str, Tuple[Dict[str, Any], List[Any], Tuple[int, int]]]:
    """id → (symbol, the list holding it, the parent's absolute origin)."""
    out: Dict[str, Tuple[Dict[str, Any], List[Any], Tuple[int, int]]] = {}

    def walk(lst: List[Any], ox: int, oy: int) -> None:
        for s in lst:
            if isinstance(s, dict) and s.get("id") is not None:
                out[str(s["id"])] = (s, lst, (ox, oy))
                if isinstance(s.get("children"), list):
                    walk(s["children"], ox + _int0(s.get("x")), oy + _int0(s.get("y")))

    walk(symbols, 0, 0)
    return out


def _abs_leaves(symbols: List[Any], skip: Any = None) -> List[Dict[str, Any]]:
    """Every placed non-container symbol as an absolute box (copies): obstacles for an added one."""
    out: List[Dict[str, Any]] = []

    def walk(lst: List[Any], ox: int, oy: int) -> None:
        for s in lst:
            if not isinstance(s, dict) or s is skip:
                continue
            if not all(_as_int(s.get(k)) is not None for k in ("x", "y", "w", "h")):
                continue
            ax, ay = ox + _int0(s["x"]), oy + _int0(s["y"])
            if isinstance(s.get("children"), list) and s.get("type") in ("group", "repeat"):
                walk(s["children"], ax, ay)
            else:
                out.append({"type": s.get("type"), "x": ax, "y": ay, "w": _int0(s["w"]), "h": _int0(s["h"])})

    walk(symbols, 0, 0)
    return out


# The lock rule (design_displays §6.4 item 3, superseding R-35 and §6.3 item 3's "unless named"
# exception): a symbol in `locked_ids` or with `locked: true` is never changed by Describe and is
# always an obstacle; only the user locks and unlocks (the properties box, Ctrl+L, the badge).


# ── D5.2: footprints, the no-overlap rule, properties, references (design_displays §6.3) ──

# The viewer's type sizes (displays.css): a value's text 16 px, a caption 11 px, a text 14 px;
# style.font s / m / l / xl = 11 / 14 / 19 / 26 px. Line height 1.2; a character ≈ 0.6 em.
FONT_PX: Dict[str, int] = {"s": 11, "m": 14, "l": 19, "xl": 26}
CAP_PX = 11
# D5.4 (design_displays §6.5 item 3): the human-scale default size of a symbol Describe adds, on
# the reference canvas 1600 x 900 (each length scales by min(W / 1600, H / 900), on the grid). A bar
# is vertical (horizontal swaps the two); a text is fitted to its words (`text_box`).
REF_W, REF_H = 1600, 900
DEFAULT_SIZES: Dict[str, Tuple[int, int]] = {
    "trend": (640, 320), "table": (480, 280), "gauge": (240, 240), "bar": (96, 280),
    "value": (200, 64), "state": (200, 64), "button": (160, 48), "image": (320, 240),
    "shape": (240, 160), "group": (640, 400), "repeat": (640, 400),
}
# §6.5 item 4: the readability floors (replacing §6.3's KIND_MIN) — scaled like the defaults but
# never below the absolute floor; nothing is shrunk below them (by permission, by making room, by
# `scale`). A text's floor is the box its words need at its font.
FLOORS: Dict[str, Tuple[int, int]] = {
    "trend": (400, 240), "table": (320, 200), "gauge": (200, 200), "bar": (64, 200),
    "value": (140, 48), "state": (140, 48), "button": (120, 40), "image": (96, 96), "shape": (64, 48),
}
ABS_FLOORS: Dict[str, Tuple[int, int]] = {
    "trend": (320, 200), "table": (240, 160), "gauge": (160, 160), "bar": (48, 160),
    "value": (112, 40), "state": (112, 40), "button": (120, 40), "image": (96, 96), "shape": (64, 48),
}
OTHER_MIN = (64, 32)
# D3.4 / D3.5 (design_displays §5.7.8 item 4, §5.7.9 item 1, R20): an equipment symbol's default
# size and floor by glyph on 1600 x 900 (scaled like the other kinds), and its absolute floor (four
# fifths of the floor, up to the grid): EQUIPMENT_SIZES / EQUIPMENT_FLOORS / EQUIPMENT_ABS_FLOORS,
# imported from orchestrator/display_glyphs.py above.
# What the old name meant, kept for readers of D5.2 / D5.3 code: the floors on 1600 x 900.
KIND_MIN = FLOORS
GAP = 0                                   # px between footprints the free-space search leaves (nearest = touching)
PLACE_GAP = 16                            # px for place below / above / left_of / right_of

# §6.6 item 1 (D5.5): the overlap family. It lifts the no-overlap rule only where a clause asks
# for it positively (not negated), and only for the symbols that clause refers to
# (`positive_clauses`, `_ClauseRefs`); "over to / there / here / by" is not in the family.
_OVERLAP_ASK_RE = re.compile(r"\b(?:on\s+top\s+of|overlay(?:s|ed|ing)?|overlap(?:s|ped|ping)?|over\s+the|inside|"
                             r"within|behind|in\s+front\s+of|layered)\b", re.I)
_PERMIT_RE = re.compile(r"\b(if (?:you )?(?:need|must|have) to|if needed|if necessary|as needed|make room|"
                        r"resize what you must|resize (?:anything|whatever|others)|shrink|decrease|"
                        r"make (?:it|them|the \w+(?: \w+)?) smaller)\b", re.I)
_CLEAR_ONLY_RE = re.compile(r"^\s*(?:please\s+)?(?:(?:clear|empty|wipe|reset)\s+(?:the\s+|this\s+)?"
                            r"(?:display|canvas|screen|page|everything|all)|(?:remove|delete)\s+(?:everything|"
                            r"all(?:\s+(?:the\s+)?symbols)?|every\s+symbol)|start\s+over)\s*[.!]*\s*$", re.I)
_RANGE_ASK_RE = re.compile(r"\b(min|max|minimum|maximum|range|scale|span|limits?)\b", re.I)
_OUT_OF_SCOPE: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(undo|redo)\b", re.I), "to undo or redo, use Undo (Ctrl+Z) / Redo (Ctrl+Y)"),
    (re.compile(r"\bpublish(ed|ing)?\b", re.I), "to publish, press Publish"),
    (re.compile(r"\bsave(d)?\b", re.I), "drafts save automatically"),
)
_KIND_WORDS: Dict[str, str] = {
    "gauge": "gauge", "gauges": "gauge", "bar": "bar", "bars": "bar", "trend": "trend",
    "trends": "trend", "chart": "trend", "table": "table", "tables": "table", "value": "value",
    "values": "value", "text": "text", "texts": "text", "button": "button", "buttons": "button",
    "shape": "shape", "shapes": "shape", "lamp": "state", "lamps": "state", "image": "image",
    "images": "image", "pipe": "pipe", "pipes": "pipe",
}
_AFTER_QUALIFIERS = frozenset("""showing shows that which with for bound on at in labeled labelled
called named near next""".split())


def _floor_g(v: Any, grid: int = GRID) -> int:
    return int(Fraction(v) // grid) * grid


def _ceil_g(v: Any, grid: int = GRID) -> int:
    return -int(-Fraction(v) // grid) * grid


def _ceil(v: Fraction) -> int:
    return -int(-v // 1)


# A value / state / text symbol is predicted to draw its whole frame (Settled at the D5.2 live
# battery: a new or moved symbol's predicted footprint is conservative — the gates and the page
# check frames for these kinds, and the page's measured footprints take over once it is drawn).
TEXT_FOOTPRINTS = False


def predict_footprint(sym: Dict[str, Any], ax: int, ay: int,
                      value_text: Optional[str] = None) -> Tuple[int, int, int, int]:
    """What a symbol DRAWS, predicted by kind (design_displays §6.3 item 3), absolute px:
    gauge = the centered square of side min(w, h); value / state / text = the text's size (font
    size × lines; characters × 0.6 em, the caption line included), placed per `style.align`
    (value / state centered vertically, text from the top); a value / state / text with a fill or
    stroke draws its frame; bar, trend, table, image, shape, button, group = the frame."""
    t = str(sym.get("type") or "")
    w, h = max(1, _int0(sym.get("w"))), max(1, _int0(sym.get("h")))
    st = sym.get("style") if isinstance(sym.get("style"), dict) else {}
    if t == "gauge":
        s = min(w, h)
        return ax + (w - s) // 2, ay + (h - s) // 2, s, s
    if TEXT_FOOTPRINTS and t in ("value", "state", "text") and not (st.get("fill") or st.get("stroke")):
        if t == "text":
            fs = FONT_PX.get(str(st.get("font")), 14)
            lines = str(sym.get("text") or "").split("\n") or [""]
            chars = max((len(x) for x in lines), default=0)
            fw = min(w, _ceil(Fraction(chars * fs * 6, 10)) + 4)
            fh = min(h, _ceil(Fraction(len(lines) * fs * 12, 10)) + 4)
            fy = ay
        else:
            fs = FONT_PX.get(str(st.get("font")), 16)
            vt = value_text if value_text else "0000.00"
            row = Fraction(len(vt) * fs * 6, 10) + Fraction(4 * CAP_PX * 6, 10) + (14 if t == "state" else 0)
            cap = Fraction(len(str(sym.get("label") or "")) * CAP_PX * 6, 10)
            th = Fraction(fs * 12, 10) + (Fraction(CAP_PX * 12, 10) if sym.get("label") else 0)
            fw = min(w, _ceil(max(row, cap)) + 12)
            fh = min(h, _ceil(th) + 6)
            fy = ay + (h - fh) // 2
        align = st.get("align") if st.get("align") in ALIGNS else "left"
        fx = ax if align == "left" else (ax + (w - fw) // 2 if align == "center" else ax + w - fw)
        return fx, fy, max(1, fw), max(1, fh)
    return ax, ay, w, h


def _canvas_scale(W: int, H: int) -> Fraction:
    """§6.5 item 3: how the reference sizes scale to this canvas — min(W / 1600, H / 900)."""
    return min(Fraction(W, REF_W), Fraction(H, REF_H))


def _scaled(v: int, f: Fraction, grid: int) -> int:
    """A reference length scaled by `f`, rounded (half-up) to the grid, at least one grid step."""
    return max(grid, int(Fraction(v) * f / grid + Fraction(1, 2)) * grid)


def text_box(sym: Dict[str, Any], grid: int = GRID) -> Tuple[int, int]:
    """§6.5 item 3: the box a text's words need at its font — width = characters x 0.6 x font px
    + 16, height = lines x 1.2 x font px + 8, each rounded up to the grid (a text with no font
    draws at 14 px, the viewer's text size)."""
    st = sym.get("style") if isinstance(sym.get("style"), dict) else {}
    fs = FONT_PX.get(str(st.get("font")), 14)
    lines = str(sym.get("text") or sym.get("label") or "").split("\n") or [""]
    chars = max(1, max(len(x) for x in lines))
    w = _ceil_g(Fraction(chars * fs * 6, 10) + 16, grid)
    h = _ceil_g(Fraction(len(lines) * fs * 12, 10) + 8, grid)
    return w, h


def _horizontal(sym: Dict[str, Any]) -> bool:
    return sym.get("type") == "bar" and sym.get("orientation") == "horizontal"


def default_size(sym: Dict[str, Any], W: int, H: int, grid: int = GRID) -> Tuple[int, int]:
    """The human-scale default size of `sym`'s kind on a W x H canvas (§6.5 item 3), inside the
    canvas; a kind without a default keeps its size."""
    t = str(sym.get("type") or "")
    if t == "text":
        w, h = text_box(sym, grid)
    elif t == "equipment":                             # §5.7.8 item 4: by glyph
        bw, bh = EQUIPMENT_SIZES.get(str(sym.get("glyph")), EQUIPMENT_SIZES["pump"])
        f = _canvas_scale(W, H)
        w, h = _scaled(bw, f, grid), _scaled(bh, f, grid)
    elif t in DEFAULT_SIZES:
        bw, bh = DEFAULT_SIZES[t]
        if _horizontal(sym):
            bw, bh = bh, bw
        f = _canvas_scale(W, H)
        w, h = _scaled(bw, f, grid), _scaled(bh, f, grid)
    else:
        w, h = max(grid, _int0(sym.get("w"))), max(grid, _int0(sym.get("h")))
    return min(w, W // grid * grid), min(h, H // grid * grid)


def floor_size(sym: Dict[str, Any], W: int, H: int, grid: int = GRID) -> Tuple[int, int]:
    """§6.5 item 4: the smallest box `sym` is ever shrunk to — its kind's readability floor scaled
    like the defaults, never below the absolute floor; a text's = the box its words need."""
    t = str(sym.get("type") or "")
    if t == "text":
        w, h = text_box(sym, grid)
    else:
        if t == "equipment":                           # §5.7.8 item 4: by glyph
            gl = str(sym.get("glyph")) if sym.get("glyph") in EQUIPMENT_FLOORS else "pump"
            fl, ab = EQUIPMENT_FLOORS[gl], EQUIPMENT_ABS_FLOORS[gl]
        else:
            fl = FLOORS.get(t, OTHER_MIN)
            ab = ABS_FLOORS.get(t, fl)
        if _horizontal(sym):
            fl, ab = (fl[1], fl[0]), (ab[1], ab[0])
        f = _canvas_scale(W, H)
        w, h = max(ab[0], _scaled(fl[0], f, grid)), max(ab[1], _scaled(fl[1], f, grid))
    return min(w, W // grid * grid), min(h, H // grid * grid)


# §6.5 item 3: the size words (what each one means for a symbol is §6.6 item 2's `size_request`).
_SIZE_WORDS = ("small", "smaller", "smallest", "tiny", "big", "bigger", "biggest", "large", "larger",
               "largest", "huge", "wide", "wider", "narrow", "narrower", "tall", "taller", "short",
               "shorter", "half", "twice", "double", "triple")
_SIZE_WORD_RX = r"(?:" + "|".join(_SIZE_WORDS) + r")"
_EXPLICIT_SIZE_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:px|pixels?|%|percent)\b", re.I)
# Size words that are about the FONT ("a large font", "big text", "font size", "the font large"),
# and "half" naming a region ("the left half") — removed before looking for size talk.
_NOT_BOX_SIZE_RES: Tuple[re.Pattern, ...] = (
    re.compile(r"\b" + _SIZE_WORD_RX + r"(?:\s+[\w\-]+)?\s+(?:font|fonts|text|type|letters|lettering|print|"
               r"writing|characters)\b", re.I),
    re.compile(r"\b(?:font|fonts|text|type|letters|lettering|characters)(?:\s+size)?(?:\s+(?:is|be|are|to|in|"
               r"of|a|an|the|very|extra|really|much|more))*\s+" + _SIZE_WORD_RX + r"\b", re.I),
    re.compile(r"\bfont\s+size\b(?:\s+(?:of\s+)?\d+(?:\s*px)?)?", re.I),
    re.compile(r"\b(?:left|right|top|bottom|upper|lower)\s+half\b", re.I),
    # a size word that MODIFIES a reference ("the larger gauge") names a symbol, not a size (§6.3)
    # (the noun is kept — "the larger gauge" → "the gauge" — so the clause still refers to it)
    re.compile(r"\bthe\s+" + _SIZE_WORD_RX + r"\s+(?:[\w\-]+\s+)?(?P<noun>trends?|charts?|graphs?|plots?|gauges?|"
               r"dials?|bars?|lamps?|lights?|states?|tables?|lists?|texts?|labels?|titles?|values?|numbers?|"
               r"readings?|shapes?|box(?:es)?|rectangles?|circles?|buttons?|images?|pictures?|ones?|"
               r"pumps?|motors?|fans?|valves?|tanks?|vessels?|turbines?|generators?|hrsgs?|condensers?|"
               r"towers?|breakers?|transformers?|indicators?|symbols?)\b", re.I),
)


def _strip_not_box_size(text: str) -> str:
    """`text` without the size words that are not about a box (`_NOT_BOX_SIZE_RES`); a reference
    "the larger gauge" keeps its noun ("the gauge")."""
    for rx in _NOT_BOX_SIZE_RES:
        text = rx.sub(lambda m: f" the {m.group('noun')} " if "noun" in rx.groupindex else " ", text)
    return text


def _shrunk(w: int, h: int, f: Fraction, minw: int, minh: int, grid: int) -> Tuple[int, int]:
    """w × h scaled by f, aspect kept, on the grid, never below (minw, minh): when one side hits its
    minimum the other follows the aspect (and then its own minimum)."""
    nw = max(minw, _floor_g(w * f, grid))
    nh = max(grid, _snap(int(Fraction(nw * h, max(1, w))), grid))
    if nh < minh:
        nh = minh
        nw = max(minw, _snap(int(Fraction(nh * w, max(1, h))), grid))
    return nw, nh


def _rect_overlap(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def _rect_contains(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> bool:
    return a[0] <= b[0] and a[1] <= b[1] and b[0] + b[2] <= a[0] + a[2] and b[1] + b[3] <= a[1] + a[3]


# Words that are a symbol id ("s6"): the model put an id where a text's words or a label belong
# (corpus C098); never shown, never used to name a symbol.
_ID_LIKE_RE = re.compile(r"^\s*s\d+\s*$", re.I)
_BARE_ID_RE = re.compile(r"(['‘\"“]?)\b(s\d+)\b(['’\"”]?)")


def _scrub_ids(note: str, by_id: Optional[Dict[str, str]] = None) -> str:
    """`note` with every bare symbol id ("s6", "'s6'") replaced by that symbol's name, else by
    "that symbol" — a last guard; the names themselves never use ids (`_sym_name`)."""
    def sub(m: re.Match) -> str:
        return (by_id or {}).get(m.group(2), "that symbol")
    return _BARE_ID_RE.sub(sub, note)


def _sym_name(s: Dict[str, Any], names: Optional[Dict[str, str]] = None) -> str:
    """How a note names a symbol (§6.4 item 7 — never by its internal id): its label, a text's
    text, else what tells it apart among its kind (`names`, from _distinct_names), else "the <kind>"."""
    lbl = _clean_text(s.get("label"), 60)
    if lbl and not _ID_LIKE_RE.match(lbl):
        return lbl
    if s.get("type") == "text" and _clean_text(s.get("text"), 40) and not _ID_LIKE_RE.match(str(s.get("text"))):
        return f"the text '{_clean_text(s.get('text'), 40)}'"
    if names and str(s.get("id")) in names:
        return names[str(s.get("id"))]
    return f"the {_kind_noun(s)}"


_ORD_WORDS = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth",
              "tenth", "eleventh", "twelfth")
_H_PHRASE = {"left": "on the left", "middle": "in the middle", "right": "on the right"}
_V_PHRASE = {"top": "at the top", "middle": "in the middle", "bottom": "at the bottom"}


def _distinct_names(items: List[Tuple[Dict[str, Any], Tuple[int, int, int, int]]],
                    W: int, H: int) -> Dict[str, str]:
    """The names of the symbols without a label (and texts without text), §6.4 item 7, from
    (symbol, absolute frame) pairs: the only one of its kind → "the gauge"; two → "the larger /
    smaller gauge" (frame area); three or more → "the largest / smallest gauge" when it is one of
    those; otherwise (and for equal areas) by position — its frame center in thirds of the canvas,
    horizontal words first ("the gauge on the left", "… in the middle", "… at the top"), ties →
    "the second gauge from the left". Returns {id: name}."""
    by_kind: Dict[str, List[Tuple[Dict[str, Any], Tuple[int, int, int, int]]]] = {}
    for s, b in items:
        by_kind.setdefault(_kind_noun(s), []).append((s, b))   # an equipment symbol by its glyph
    out: Dict[str, str] = {}

    def hpos(b: Tuple[int, int, int, int]) -> str:
        cx = b[0] + b[2] / 2
        return "left" if cx < W / 3 else ("right" if cx > 2 * W / 3 else "middle")

    def vpos(b: Tuple[int, int, int, int]) -> str:
        cy = b[1] + b[3] / 2
        return "top" if cy < H / 3 else ("bottom" if cy > 2 * H / 3 else "middle")

    for kind, ss in by_kind.items():
        n = len(ss)
        areas = [b[2] * b[3] for _, b in ss]
        for i, (s, b) in enumerate(ss):
            if _clean_text(s.get("label"), 60) or (kind == "text" and _clean_text(s.get("text"), 40)):
                continue
            a = areas[i]
            name = ""
            if n == 1:
                name = f"the {kind}"
            elif n == 2:
                other = areas[1 - i]
                if a != other:
                    name = f"the {'larger' if a > other else 'smaller'} {kind}"
            elif areas.count(a) == 1 and a in (max(areas), min(areas)):
                name = f"the {'largest' if a == max(areas) else 'smallest'} {kind}"
            if not name:
                hp = hpos(b)
                vp = vpos(b)
                if sum(1 for _, o in ss if hpos(o) == hp) == 1:
                    name = f"the {kind} {_H_PHRASE[hp]}"
                elif sum(1 for _, o in ss if vpos(o) == vp) == 1:
                    name = f"the {kind} {_V_PHRASE[vp]}"
                else:
                    order = sorted(range(n), key=lambda j: (ss[j][1][0] + ss[j][1][2] / 2,
                                                            ss[j][1][1] + ss[j][1][3] / 2))
                    k = order.index(i)
                    word = _ORD_WORDS[k] if k < len(_ORD_WORDS) else _ordinal(k + 1)
                    name = f"the {word} {kind} from the left"
            out[str(s.get("id"))] = name
    return out


def _holdings(symbols: List[Any], limit: int = 12, names: Optional[Dict[str, str]] = None) -> str:
    """What the display holds, for a note: each symbol by its name (never its id) and kind."""
    if names is None:
        names = _Editor(symbols, CANVAS_W, CANVAS_H, GRID).names
    items: List[str] = []
    for s in _walk(symbols):
        if not isinstance(s, dict) or s.get("type") in ("group",):
            continue
        nm = _sym_name(s, names)
        items.append(nm if nm.startswith(f"the {_kind_noun(s)}") else f"{nm} ({_kind_noun(s)})")
    if not items:
        return "nothing yet"
    return ", ".join(items[:limit]) + (f", and {len(items) - limit} more" if len(items) > limit else "")


def color_value(v: Any, request: str = "") -> Tuple[Optional[str], Optional[str]]:
    """A color from the model → (a house token or #rrggbb, None) or (None, why). Names go through
    COLOR_NAMES; hex is accepted only when the user typed it in the request (§6.3 item 4)."""
    if not isinstance(v, str) or not v.strip():
        return None, "a color name is needed"
    t = re.sub(r"\s+", " ", v.strip().lower())
    if t in TOKENS:
        return t, None
    if t in COLOR_NAMES:
        return COLOR_NAMES[t], None
    if _HEX_RE.match(t):
        if t in (request or "").lower():
            return t, None
        return None, f"'{v.strip()}' is not a color name (say e.g. green or dark blue)"
    return None, f"'{v.strip()}' is not a color I know (say e.g. green, dark blue, light gray)"


_DURATION_RE = re.compile(r"^([1-9]\d{0,5})([smhdw])$")
_DUR_S = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
# §6.4 item 7: the model's `decimals` counts only when the request talks about decimals.
_DECIMALS_ASK_RE = re.compile(r"\b(decimals?|places|precision|digits|whole\s+numbers?|integers?|round(?:ed)?)\b",
                              re.I)
# The kinds whose text the viewer draws with `style.font` / `style.align` (display_render.js +
# displays.css: .ds-val / .ds-txt take the font; .ds-row / .ds-cap / .ds-txt take the alignment).
_FONT_KINDS = frozenset(("value", "state", "bar", "text"))
_ALIGN_KINDS = frozenset(("value", "state", "gauge", "bar", "table", "text"))
# How a request refers to a symbol of each kind (singular and plural, name_key).
_KIND_REFS: Dict[str, frozenset] = {
    "trend": frozenset("trend trends chart charts graph graphs plot plots".split()),
    "gauge": frozenset("gauge gauges dial dials".split()),
    "bar": frozenset("bar bars".split()),
    "state": frozenset("lamp lamps light lights state states".split()),
    "table": frozenset("table tables list lists".split()),
    "text": frozenset("text texts label labels title titles caption captions words font".split()),
    "value": frozenset("value values number numbers reading readings font".split()),
    "shape": frozenset("shape shapes box boxes frame frames rectangle rectangles circle circles line lines"
                       .split()),
    # D3.6: a pipe symbol (no longer a shape's word)
    "pipe": frozenset("pipe pipes piping pipeline pipelines duct ducts".split()),
    "button": frozenset("button buttons".split()),
    "image": frozenset("image images picture pictures".split()),
}
_EVERY_REFS = frozenset(("everything", "all", "both", "each", "every"))
_ALL_REF_WORDS = frozenset().union(*_KIND_REFS.values())


def _ref_words(s: Dict[str, Any]) -> frozenset:
    """The words a request refers to `s` by as a kind: its kind's words, an equipment symbol's
    glyph words ("pump", "valves", "equipment")."""
    t = str(s.get("type") or "")
    if t == "equipment":
        return _glyph_words_of(s)
    return frozenset(_KIND_REFS.get(t, frozenset((t,))))
_SENTENCE_RE = re.compile(r"[.!?;\n]+(?=\s|$)|[!?;\n]+")
_COLOR_WORD_RE = re.compile(r"\b(colou?r(?:ed|s)?|grey|" + "|".join(
    re.escape(n) for n in sorted(set(COLOR_NAMES) | {"sev1", "sev2"}, key=len, reverse=True)) + r")\b|#[0-9a-fA-F]{6}\b", re.I)


# ── D5.5 (design_displays §6.6): clauses, negation, scoped permissions, predictable sizes ──

_CLAUSE_END_RE = re.compile(r"[.!?;\n]+(?=\s|$)|[!?;\n]+")
_CLAUSE_WORD_RE = re.compile(r"\b(?:and|but|so|then|also)\b", re.I)
# §6.6 item 1 (as settled after Fable 5.1's review): a negation anywhere in the phrase's SENTENCE,
# before or after it (typed without the apostrophe too): not / n't / no / never / without / avoid /
# prevent / instead of / rather than / stop / free of / clear of / away from / nothing / non- /
# -free / must not / keep … from / make sure … not ("so it doesn't …" is a negation in the sentence).
_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|without|avoid(?:s|ed|ing)?|prevent(?:s|ed|ing)?|instead\s+of|rather\s+than|stop|"
    r"free\s+of|clear\s+of|away\s+from|nothing|none|neither|nor|cannot|non|dont|doesnt|didnt|isnt|arent|"
    r"wont|cant|shouldnt|wouldnt|mustnt)\b|n['’]t\b|\w-free\b|\bkeep(?:s|ing)?\b[^.!?;\n]*\bfrom\b", re.I)
_PRONOUNS = frozenset("it its itself them they their themselves".split())
_ANY_REFS = _EVERY_REFS | frozenset(("anything", "others", "other"))


def _clause_split(text: str) -> List[Tuple[str, bool, str]]:
    """(clause, follows "so", its sentence) for every clause of `text`, in order (`_clauses`)."""
    out: List[Tuple[str, bool, str]] = []
    for sent in _CLAUSE_END_RE.split(text or ""):
        pos, after_so = 0, False
        for m in _CLAUSE_WORD_RE.finditer(sent):
            out.append((sent[pos:m.start()].strip(), after_so, sent))
            after_so = m.group(0).lower() == "so"
            pos = m.end()
        out.append((sent[pos:].strip(), after_so, sent))
    return [x for x in out if x[0]]


def _clauses(text: str) -> List[Tuple[str, bool]]:
    """§6.6 item 1: the clauses of a request — the text between sentence ends (. ! ? ; newline) and
    the words and / but / so / then / also — each with whether it follows "so"."""
    return [(c, so) for c, so, _s in _clause_split(text)]


def _negated(sentence: str) -> bool:
    """§6.6 item 1 (settled after Fable 5.1's review): a phrase is negated when its sentence holds
    a negation anywhere — before or after it ("overlap is not OK"); a clause split at "and" never
    turns "don't let the trend and the gauge overlap" into a permission."""
    return bool(_NEGATION_RE.search(sentence))


def positive_clauses(request: str, rx: re.Pattern) -> List[int]:
    """The indexes (into `_clauses(request)`) of the clauses where `rx` matches in a sentence with
    no negation (§6.6 item 1). Anything unclear grants nothing: a request that also negates the
    phrase in another sentence ("Put B on top of A. Overlap is not OK.") grants nothing at all."""
    parts = _clause_split(request)
    if any(rx.search(sent) and _negated(sent) for _c, _so, sent in parts):
        return []
    return [i for i, (c, _so, _sent) in enumerate(parts) if rx.search(c)]


def _tokens(text: str) -> List[str]:
    """The words of a request or a name for reference matching: possessives off ("A's" → a), any
    length (a symbol may be labeled "A"), name_key."""
    t = re.sub(r"['’]s\b", "", text or "")
    return [name_key(w) for w in re.findall(r"[\w\-]+", t)]


def _name_lists(s: Dict[str, Any]) -> List[List[str]]:
    """The word lists a request names `s` by: its label, and a text's text."""
    out = [_tokens(str(s.get("label") or ""))]
    if s.get("type") == "text" and s.get("text"):
        out.append(_tokens(str(s.get("text"))))
    return [w for w in out if w]


class _ClauseRefs:
    """§6.6: which symbols each clause of a request refers to (the §6.4 naming rules, per clause): a
    symbol whose whole label (a text's text too) occurs in the clause; a kind word or synonym outside
    the quoted words and outside a label it is part of → every symbol of that kind; everything / all /
    anything / others in a clause that names nothing else → every symbol; a clause that says it / them
    → also (or, for sizes, only when it names nothing itself) what the clause before it names."""

    def __init__(self, text: str) -> None:
        self.clauses = _clauses(text)
        self.allw: List[set] = []
        self.unq: List[List[str]] = []
        for c, _so in self.clauses:
            self.allw.append(set(_tokens(c)))
            self.unq.append(_tokens(re.sub(r"[\"“][^\"”]*[\"”]", " ", c)))
        self._kinds: Dict[Tuple[int, Tuple[int, ...]], set] = {}

    def _named(self, i: int, s: Dict[str, Any]) -> bool:
        return any(all(w in self.allw[i] for w in ws) for ws in _name_lists(s))

    def kind_words(self, i: int, pool: Sequence[Dict[str, Any]]) -> set:
        """The clause's kind words that are not part of a label it names."""
        key = (i, tuple(id(o) for o in pool))
        got = self._kinds.get(key)
        if got is None:
            unq = self.unq[i]
            used: set = set()
            for o in pool:
                for ws in _name_lists(o):
                    if all(w in unq for w in ws):
                        used |= set(ws)
            # D3.4: a glyph word ("pump") is a kind word only when an equipment symbol it names is here
            eq_words = frozenset().union(*(_glyph_words_of(o) for o in pool)) if pool else frozenset()
            got = {w for w in unq if w in _ALL_REF_WORDS or w in eq_words} - used
            self._kinds[key] = got
        return got

    def talks_of_any(self, i: int, pool: Sequence[Dict[str, Any]]) -> bool:
        return bool(self.kind_words(i, pool)) or any(self._named(i, o) for o in pool) \
            or bool(set(self.unq[i]) & _ANY_REFS)

    def direct(self, i: int, s: Dict[str, Any], pool: Sequence[Dict[str, Any]]) -> bool:
        if self._named(i, s):
            return True
        kinds = self.kind_words(i, pool)
        if kinds & _ref_words(s):
            return True
        return not kinds and not any(self._named(i, o) for o in pool) and bool(set(self.unq[i]) & _ANY_REFS)

    def pronoun(self, i: int) -> bool:
        return i > 0 and bool(set(self.unq[i]) & _PRONOUNS)

    def refers(self, i: int, s: Dict[str, Any], pool: Sequence[Dict[str, Any]],
               inherit_always: bool = False) -> bool:
        if self.direct(i, s, pool):
            return True
        if self.pronoun(i) and (inherit_always or not self.talks_of_any(i, pool)):
            return self.direct(i - 1, s, pool)
        return False


# §6.6 item 2: what the request may say about a symbol's size, in order of precedence.
_VAGUE_SIZE_RE = re.compile(
    r"\b(?:big|large)\s+enough\b|\b(?:readable|legible)\b|\beasy\s+to\s+(?:read|see)\b|"
    r"\bfor\s+(?:a\s+|the\s+)?(?:humans?|people|persons?|operators?|users?|viewers?)\b|"
    r"\b(?:decent|reasonable|normal|good|sensible)[\s\-]+(?:size|sized)\b", re.I)
# ("clear" and "visible" are not readability phrases — they collide with the `clear` op, "clear of"
# and the `visible` rule; settled after Fable 5.1's review.)
_DIM_PAIR_RE = re.compile(r"\b(\d{2,4})\s*(?:px|pixels?)?\s*(?:x|×|by)\s*(\d{2,4})\s*(?:px|pixels?)?(?!\w)", re.I)
_DIM_W_RE = re.compile(r"\b(\d{1,4}(?:\.\d+)?)\s*(px|pixels?|%|percent)?\s+(?:wide|in\s+width|across)\b|"
                       r"\bwidth\s+(?:of\s+|to\s+|=\s*|:\s*)?(\d{1,4}(?:\.\d+)?)\s*(px|pixels?|%|percent)?", re.I)
_DIM_H_RE = re.compile(r"\b(\d{1,4}(?:\.\d+)?)\s*(px|pixels?|%|percent)?\s+(?:tall|high|in\s+height)\b|"
                       r"\bheight\s+(?:of\s+|to\s+|=\s*|:\s*)?(\d{1,4}(?:\.\d+)?)\s*(px|pixels?|%|percent)?", re.I)
_DIM_ANY_RE = re.compile(r"\b(\d{1,4}(?:\.\d+)?)\s*(px|pixels?|%|percent)(?!\w)", re.I)
_COMPARE_RE = re.compile(
    r"\b(?:(twice|double|half|two\s+times|three\s+times)\s+)?(?:(?:the\s+)?same\s+(size|width|height)\s+as|"
    r"as\s+(big|large|wide|tall|high|small|narrow|short)\s+as|the\s+(size|width|height)\s+of)\s+"
    r"((?:[\w\-'’.]+\s*){1,6})", re.I)
# Whether the user compared sizes at all (then the model's `match` on a symbol it adds counts).
_SIZE_VERB_RE = re.compile(r"\b(?:shrink(?:s|ing)?|shrunk|decrease[sd]?|decreasing|reduce[sd]?|reducing|"
                           r"enlarge[sd]?|enlarging|grow(?:s|ing|n)?|expand(?:s|ed|ing)?|resize[sd]?|resizing|"
                           r"rescale[sd]?|scale[sd]?|size[sd]?|sizing|same\s+(?:size|width|height)|"
                           r"same\s+(?:\w+\s+)?as|match(?:es|ed|ing)?|"
                           r"width|height|wide|tall|stretch(?:es|ed|ing)?)\b", re.I)
_COMPARE_ASK_RE = re.compile(r"\bsame\s+(?:size|width|height)\b|\bas\s+(?:big|large|wide|tall|high|small|narrow|"
                             r"short)\s+as\b|\b(?:size|width|height)\s+of\b|\bmatch(?:es|ed|ing)?\b", re.I)
_FULL_RES: Tuple[Tuple[re.Pattern, Tuple[bool, bool]], ...] = (
    (re.compile(r"\bfull[\s\-]*screen\b|\b(?:whole|entire)\s+(?:canvas|screen|display|page)\b|"
                r"\bfill(?:s|ing)?\s+the\s+(?:whole\s+|entire\s+)?(?:canvas|screen|display)\b", re.I), (True, True)),
    (re.compile(r"\bfull[\s\-]+width\b|\b(?:whole|entire)\s+width\b", re.I), (True, False)),
    (re.compile(r"\bfull[\s\-]+height\b|\b(?:whole|entire)\s+height\b", re.I), (False, True)),
)
_TIER_RES: Tuple[Tuple[re.Pattern, Fraction], ...] = (
    (re.compile(r"\b(?:very|extra|really)\s+(?:large|big)\b|\b(?:huge|enormous|giant|gigantic)\b", re.I),
     Fraction(2)),
    (re.compile(r"\b(?:twice|double)\b", re.I), Fraction(2)),
    (re.compile(r"\bhalf\b", re.I), Fraction(1, 2)),
    (re.compile(r"\b(?:big|bigger|biggest|large|larger|largest)\b", re.I), Fraction(3, 2)),
    (re.compile(r"\b(?:small|smaller|smallest|compact|tiny)\b", re.I), Fraction(3, 4)),
)
_AXIS_TIER_RES: Tuple[Tuple[re.Pattern, Fraction, Fraction], ...] = (
    (re.compile(r"\b(?:wide|wider|widest)\b", re.I), Fraction(3, 2), Fraction(1)),
    (re.compile(r"\b(?:narrow|narrower)\b", re.I), Fraction(3, 4), Fraction(1)),
    (re.compile(r"\b(?:tall|taller|tallest)\b", re.I), Fraction(1), Fraction(3, 2)),
    (re.compile(r"\b(?:short|shorter)\b", re.I), Fraction(1), Fraction(3, 4)),
)


def _user_numbers(text: str, W: int, H: int) -> Tuple[Optional[int], Optional[int], set, set]:
    """§6.6 item 2: the sizes the user wrote — (width given for the width, height given for the
    height, every number that may be a width, every number that may be a height), px; a percent is
    of the canvas on its axis; a number tagged with one axis counts only for that axis."""
    pw = ph = None
    any_w: set = set()
    any_h: set = set()

    def val(n: str, unit: Optional[str], total: int) -> int:
        v = Fraction(n)
        if unit and unit.lower() in ("%", "percent"):
            v = v * total / 100
        return int(v)

    spans: List[Tuple[int, int]] = []
    for m in _DIM_PAIR_RE.finditer(text or ""):
        w, h = int(m.group(1)), int(m.group(2))
        pw, ph = (pw if pw is not None else w), (ph if ph is not None else h)
        any_w.add(w)
        any_h.add(h)
        spans.append(m.span())
    for rx, axis in ((_DIM_W_RE, "w"), (_DIM_H_RE, "h")):
        for m in rx.finditer(text or ""):
            n, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            if n is None:
                continue
            v = val(n, unit, W if axis == "w" else H)
            if axis == "w":
                pw = v if pw is None else pw
                any_w.add(v)
            else:
                ph = v if ph is None else ph
                any_h.add(v)
            spans.append(m.span())
    for m in _DIM_ANY_RE.finditer(text or ""):
        if any(a <= m.start() < b for a, b in spans):
            continue
        any_w.add(val(m.group(1), m.group(2), W))
        any_h.add(val(m.group(1), m.group(2), H))
    return pw, ph, any_w, any_h


def _compared_to(words: str, sym: Dict[str, Any],
                 others: Sequence[Tuple[Dict[str, Any], Tuple[int, int, int, int]]]
                 ) -> Optional[Tuple[int, int, int, int]]:
    """The frame of the real symbol a comparison names ("… as the gauge", "… of the Wide Trend"):
    its whole label, else a kind word that leaves one candidate (a size adjective before it picks
    the largest / smallest); else None."""
    toks = _tokens(words)
    named = [(o, b) for o, b in others if o is not sym and any(all(w in toks for w in ws) for ws in _name_lists(o))]
    if named:
        return max(named, key=lambda ob: max(len(ws) for ws in _name_lists(ob[0])))[1]
    for i, t in enumerate(toks):
        kind = _KIND_WORDS.get(t)
        eq = _eq_matches(t, [o for o, _b in others if o is not sym]) if kind is None else []
        if kind is None and not eq:
            continue
        same = [(o, b) for o, b in others if o is not sym and (
            o.get("type") == kind if kind is not None else any(o is e for e in eq))]
        prev = toks[i - 1] if i else ""
        if prev in ("larger", "largest", "bigger", "biggest", "big", "large", "smaller", "smallest", "small"):
            pick = (max if prev in ("larger", "largest", "bigger", "biggest", "big", "large") else min)(
                same, key=lambda ob: ob[1][2] * ob[1][3], default=None)
            return pick[1] if pick is not None else None
        return same[0][1] if len(same) == 1 else None
    return None


def size_request(sym: Dict[str, Any], request: str, W: int, H: int, grid: int = GRID,
                 others: Sequence[Tuple[Dict[str, Any], Tuple[int, int, int, int]]] = (),
                 existing: bool = False, op_target: bool = False) -> Optional[Dict[str, Any]]:
    """What the request says about `sym`'s size (§6.6 item 2), read from the clauses that refer to
    it (its kind, its label, everything — or, for a symbol being added or the target of the op
    being checked (`op_target`), a clause that names no symbol at all: "make it bigger", where "it"
    has no antecedent and so means what the op targets), font / text sizes and region halves taken
    out first. Returns
    None (nothing said) or {"how": …, "w", "h"} with, in order of precedence:
    - "explicit": the user's numbers (px, %, "400 wide", "400 x 300"); the model's number on an axis
      only where it equals one the user wrote; an axis without a number → None (the default);
    - "compare": computed from the real symbol named ("the same size as the gauge", "twice as big as
      the table", "half the width of the trend");
    - "word": a size word — small / smaller / compact 0.75 ×, big / bigger / large / larger 1.5 ×, huge /
      very large / very big 2 ×, twice / double 2 ×, half 0.5 × the default; wide / narrow / tall /
      short on one axis; full width / height / screen / the whole canvas = the canvas minus the
      2-grid-step margin on that axis (`full_w` / `full_h`) — capped to that, never the model's size;
    - "vague": only a readability phrase (big enough, readable, easy to read, for a human, …)."""
    t = request or ""
    t = _strip_not_box_size(t)
    others = [(o, b) for o, b in others if o is not sym]
    pool = [sym] + [o for o, _b in others]
    cr = _ClauseRefs(t)
    mine: List[str] = []
    for i, (c, _so) in enumerate(cr.clauses):
        if cr.refers(i, sym, pool):
            mine.append(c)
        elif (not existing or op_target) and not cr.talks_of_any(i, pool) and \
                not (cr.pronoun(i) and cr.talks_of_any(i - 1, pool)):
            mine.append(c)
    if not mine:
        return None

    def unlabeled(c: str) -> str:
        """The clause without the display's names (a trend labeled "Wide" is not a size word); a
        symbol being added keeps its own label's words — the model named it from the request."""
        for o in (pool if existing else pool[1:]):
            for lab in (o.get("label"), o.get("text") if o.get("type") == "text" else None):
                if isinstance(lab, str) and lab.strip():
                    c = re.sub(r"(?<![\w\-])" + re.escape(lab.strip()) + r"(?![\w\-])", " ", c, flags=re.I)
        return c

    comparing = mine
    mine = [unlabeled(c) for c in mine]
    margin = 2 * grid
    full_w_px = (W - 2 * margin) // grid * grid
    full_h_px = (H - 2 * margin) // grid * grid
    dw, dh = default_size(sym, W, H, grid)
    text = " ; ".join(mine)
    # Each layer gives a width and / or a height; per axis the first layer that gives one wins
    # (settled after Fable 5.1's review: explicit number > comparison > size word > vague > default).
    ex_w = ex_h = cmp_w = cmp_h = wd_w = wd_h = None
    pw, ph, any_w, any_h = _user_numbers(text, W, H)
    if pw is not None or ph is not None or any_w:
        mw, mh = _int0(sym.get("w")), _int0(sym.get("h"))
        ew = mw if any(abs(mw - a) < grid for a in any_w) else pw
        eh = mh if any(abs(mh - a) < grid for a in any_h) else ph
        ex_w = None if ew is None else max(grid, _snap(ew, grid))
        ex_h = None if eh is None else max(grid, _snap(eh, grid))
    for c in comparing:
        m = _COMPARE_RE.search(c)
        ref = _compared_to(m.group(5), sym, others) if m else None
        if m is None or ref is None:
            continue
        mul = (m.group(1) or "").lower()
        f = Fraction(2) if mul in ("twice", "double", "two times") else Fraction(3) if mul == "three times"             else Fraction(1, 2) if mul == "half" else Fraction(1)
        what = (m.group(2) or m.group(3) or m.group(4) or "size").lower()
        both = what in ("size", "big", "large", "small")
        if both or what in ("width", "wide", "narrow"):
            cmp_w = min(W // grid * grid, max(grid, _snap(int(ref[2] * f), grid)))
        if both or what in ("height", "tall", "high", "short"):
            cmp_h = min(H // grid * grid, max(grid, _snap(int(ref[3] * f), grid)))
        break
    vague = any(_VAGUE_SIZE_RE.search(c) for c in mine)
    # an unresolved comparison sizes nothing; the user's numbers are not size words ("600 px wide")
    plain = [unlabeled(_COMPARE_RE.sub(" ", _VAGUE_SIZE_RE.sub(" ", c))) for c in comparing]
    for rx in (_DIM_PAIR_RE, _DIM_W_RE, _DIM_H_RE, _DIM_ANY_RE):
        plain = [rx.sub(" ", c) for c in plain]
    fw_full = fh_full = False
    for c in plain:
        for rx, (a, b) in _FULL_RES:
            if rx.search(c):
                fw_full, fh_full = fw_full or a, fh_full or b
    # an existing symbol's "smaller" keeps §6.3's meaning (1 / 1.5); an added one's is 0.75 × default
    less = Fraction(2, 3) if existing else Fraction(3, 4)
    fx = fy = Fraction(1)
    tier = next((f for rx, f in _TIER_RES if any(rx.search(c) for c in plain)), None)
    if tier is not None:
        fx = fy = less if tier == Fraction(3, 4) else tier
    ax_x = ax_y = False
    for rx, ax, ay in _AXIS_TIER_RES:
        if any(rx.search(c) for c in plain):
            ax, ay = (less if ax == Fraction(3, 4) else ax), (less if ay == Fraction(3, 4) else ay)
            fx, fy = fx * ax, fy * ay
            ax_x, ax_y = ax_x or ax != 1, ax_y or ay != 1
    if fw_full or tier is not None or ax_x:
        wd_w = full_w_px if fw_full else min(full_w_px, max(grid, _snap(int(dw * fx), grid)))
    if fh_full or tier is not None or ax_y:
        wd_h = full_h_px if fh_full else min(full_h_px, max(grid, _snap(int(dh * fy), grid)))
    w = next((v for v in (ex_w, cmp_w, wd_w) if v is not None), None)
    h = next((v for v in (ex_h, cmp_h, wd_h) if v is not None), None)
    how = "explicit" if (ex_w, ex_h) != (None, None) else "compare" if (cmp_w, cmp_h) != (None, None)         else "word" if (wd_w, wd_h) != (None, None) else None
    if how is not None:
        return {"how": how, "w": w, "h": h, "full_w": fw_full and w == full_w_px and ex_w is None and cmp_w is None,
                "full_h": fh_full and h == full_h_px and ex_h is None and cmp_h is None, "fx": fx, "fy": fy}
    if vague:
        return {"how": "vague", "w": None, "h": None}
    # Size talk with no amount the engine reads ("shrink the gauge", "decrease its size", "make
    # both gauges the same size", "resize it"): for an existing symbol the model's op applies
    # (§6.3 / §6.5 meaning); for a symbol being added it changes nothing (the default).
    if any(_SIZE_VERB_RE.search(c) for c in plain):
        return {"how": "verb", "w": None, "h": None}
    return None


def talks_size(request: str, sym: Dict[str, Any]) -> bool:
    """Whether the request says something about `sym`'s size other than a readability phrase
    (§6.5 item 3 as refined by §6.6 item 2)."""
    r = size_request(sym, request, CANVAS_W, CANVAS_H)
    return r is not None and r["how"] != "vague"


def human_size(sym: Dict[str, Any], request: str, W: int, H: int, grid: int = GRID,
               keep_center: bool = True,
               others: Sequence[Tuple[Dict[str, Any], Tuple[int, int, int, int]]] = ()) -> Tuple[bool, bool]:
    """§6.5 items 3-4 as refined by §6.6 item 2, on a symbol Describe adds (absolute x, y, w, h set):
    the engine decides how big — its kind's human-scale default (a text fitted to its words) unless
    the request gives this symbol an explicit number, a comparison to a real symbol (`others`: the
    display's symbols with their frames) or a size word (`size_request`); a readability phrase means
    the default; the model's own size is never used except where it equals a number the user wrote.
    Clamped to the canvas and the floor (only an explicit number goes below the floor). A default
    box is centered where the model drafted it (`keep_center`; else its top-left kept, as a first
    draft's layout is the model's arrangement); a box the request sized keeps the drafted top-left
    (as when §6.5 trusted the model's size); a full-width / full-height axis starts at the margin.
    Mutates `sym`. Returns (fitted to the default, explicitly smaller than the floor)."""
    for k in ("x", "y", "w", "h"):
        sym.setdefault(k, 0 if k in ("x", "y") else grid)
    req = size_request(sym, request, W, H, grid, others)
    how = req["how"] if req else None
    dw, dh = default_size(sym, W, H, grid)
    cx2, cy2 = 2 * sym["x"] + sym["w"], 2 * sym["y"] + sym["h"]
    fitted = how in (None, "vague", "verb") or (sym.get("type") == "text" and how != "explicit")
    small = False
    if fitted:
        nw, nh = dw, dh
    else:
        nw = req["w"] if req.get("w") is not None else dw          # type: ignore[union-attr]
        nh = req["h"] if req.get("h") is not None else dh          # type: ignore[union-attr]
        fw, fh = floor_size(sym, W, H, grid)
        if nw < fw or nh < fh:
            if how == "explicit":
                small = True
            else:
                nw, nh = max(nw, fw), max(nh, fh)
        nw, nh = min(nw, W // grid * grid), min(nh, H // grid * grid)
    sym["w"], sym["h"] = nw, nh
    if keep_center and fitted:
        sym["x"] = _snap(max(0, (cx2 - nw) // 2), grid)
        sym["y"] = _snap(max(0, (cy2 - nh) // 2), grid)
    if req and req.get("full_w"):
        sym["x"] = 2 * grid
    if req and req.get("full_h"):
        sym["y"] = 2 * grid
    return fitted, small


def _color_meant_for(request: str, sym: Dict[str, Any]) -> bool:
    """Whether a color the model put on `sym` was asked for on it (Settled at the D5.3 live
    battery: "Make the font red" had also turned the new trend red). True when some sentence of
    the request that holds a color word refers to `sym` — its kind or a synonym, a word of its
    label (a text's text too), or everything / all / both / each — or when no such sentence refers
    to any symbol at all ("make it green", where the request cannot tell)."""
    kind = str(sym.get("type") or "")
    own = set(_ref_words(sym))
    own |= {name_key(w) for w in _words(f"{sym.get('label') or ''} "
                                         f"{sym.get('text') if kind == 'text' else ''}")}
    colored = [c for c in _SENTENCE_RE.split(request or "") if _COLOR_WORD_RE.search(c)]
    if not colored:
        return True                                    # no color word: nothing to tell it by (kept)
    referring = False
    for c in colored:
        words = {name_key(w) for w in re.findall(r"[\w'’\-]+", c)}
        if words & (own | _EVERY_REFS):
            return True
        if words & _ALL_REF_WORDS:
            referring = True
    return not referring


_SET_STYLE = ("decimals", "prefix", "suffix", "font", "fill", "stroke", "color", "align", "stroke_width")
_FILL_KINDS = ("bar", "gauge", "shape")     # where a user's "color" is the drawn fill (the renderer's --ds-fill)


def _geom_value(v: Any, total: int, grid: int, minimum: int = 0) -> Optional[int]:
    """A set's x / y / w / h: "25%" (percent of the canvas) or px; on the grid."""
    if isinstance(v, str) and v.strip().endswith("%"):
        t = v.strip()[:-1].strip()
        if not _NUM_RE.match(t):
            return None
        return max(minimum, _to_px(t, total, grid))
    if _is_num(v):
        return max(minimum, _snap(int(_frac(v)), grid))
    return None


def apply_props(sym: Dict[str, Any], props: Dict[str, Any], *, request: str, W: int, H: int,
                grid: int, origin: Tuple[int, int] = (0, 0), g: Optional["_Grounding"] = None,
                images: Optional[Dict[str, str]] = None,
                ignored: Optional[List[str]] = None) -> Tuple[List[str], List[str]]:
    """`set {props}` on one symbol, every properties-panel field (§6.3 item 4), validated against the
    editor's allow-list (§3.7). Mutates `sym`. Returns (applied descriptions, refusal reasons).
    A key the panel does not have (§6.4 item 7): the symbol's own kind, `kind`, `type`, `id` or
    `symbol` is ignored silently; any other is appended to `ignored` (when given; else it becomes a
    reason "ignored '<key>' — Describe cannot set that on a <kind>")."""
    done: List[str] = []
    bad: List[str] = []
    t = str(sym.get("type") or "")
    props = normalize_props(props)                     # battery fix 3: the model's property names
    flat: Dict[str, Any] = {}
    for k, v in props.items():
        if k == "style" and isinstance(v, dict):
            flat.update(v)
        else:
            flat[k] = v
    style = dict(sym.get("style") or {})
    for k, v in flat.items():
        if k == "label":
            if isinstance(v, str) and len(v.strip()) <= 200:
                if v.strip():
                    sym["label"] = _clean_text(v, 200)
                else:
                    sym.pop("label", None)
                done.append(f"label '{_clean_text(v, 200)}'")
            else:
                bad.append("a label is text of at most 200 characters")
        elif k in ("x", "y", "w", "h"):
            total = W if k in ("x", "w") else H
            px = _geom_value(v, total, grid, grid if k in ("w", "h") else 0)
            if px is None:
                bad.append(f"{k} must be px or a percent like \"25%\"")
                continue
            sym[k] = px - (origin[0] if k == "x" else origin[1] if k == "y" else 0)
            done.append(f"{k} {px} px")
        elif k in _SET_STYLE:
            key = "fill" if (k == "color" and t in _FILL_KINDS) else k
            if k in ("fill", "stroke", "color"):
                c, why = color_value(v, request)
                if c is None:
                    bad.append(why or "not a color")
                    continue
                style[key] = c
                said = re.sub(r"\bgrey\b", "gray", re.sub(r"\s+", " ", str(v).strip().lower()))
                done.append(f"{'color' if k == 'color' else k} {said}")
            elif k == "decimals":
                i = _as_int(v)
                if i is None or not 0 <= i <= 9:
                    bad.append("decimals must be 0-9")
                    continue
                style[k] = i
                done.append(f"{i} decimal{'' if i == 1 else 's'}")
            elif k in ("prefix", "suffix"):
                if isinstance(v, str) and len(v) <= 32 and not _CTRL_RE.search(v):
                    style[k] = v
                    if v:
                        done.append(f"{k} '{v}'")
                else:
                    bad.append(f"a {k} is text of at most 32 characters")
            elif k == "font":
                f = {"small": "s", "medium": "m", "large": "l", "extra large": "xl"}.get(str(v).lower(), v)
                if f in FONTS:
                    style[k] = f
                    done.append(f"font {f}")
                else:
                    bad.append("font must be s, m, l or xl")
            elif k == "align":
                if v in ALIGNS:
                    style[k] = v
                    done.append(f"aligned {v}")
                else:
                    bad.append("align must be left, center or right")
            elif k == "stroke_width":
                i = _as_int(v)
                if i is None or not 0 <= i <= 16:
                    bad.append("stroke_width must be 0-16")
                    continue
                style[k] = i
                done.append(f"stroke width {i}")
        elif k in ("glyph", "rotate", "flip", "normal", "limits", "states", "letters") and t == "equipment":
            why = _set_equipment_field(sym, k, v)
            if why:
                bad.append(why)
            else:
                done.append({"glyph": f"now {_a_kind(sym)}", "rotate": f"rotated {sym.get('rotate', 0)}",
                             "flip": "flipped" if sym.get("flip") else "not flipped", "normal": "normal band",
                             "limits": "limits", "states": "state mapping",
                             "letters": f"letters {sym.get('letters', 'cleared')}"}[k])
                if sym.pop("_range_defaulted", False):
                    done.append("range 0 to 100 (set its range)")
        elif k in ("min", "max") and t == "equipment" and sym.get("glyph") in RANGE_GLYPHS:
            txt = _num_text(v)
            if txt is None:
                bad.append(f"{k} must be a number")
                continue
            sym[k] = {"kind": "f64", "text": txt}
            done.append(f"{k} {txt}")
        elif k in ("min", "max"):
            if t not in ("gauge", "bar"):
                # Said only when the user asked for a range; a min / max the model added to a
                # trend or value on its own is dropped silently (live battery E37 / E40).
                if _RANGE_ASK_RE.search(request or ""):
                    bad.append(f"only a vessel or analog indicator symbol has a {k}" if t == "equipment"
                               else f"only a gauge or bar has a {k}")
                continue
            txt = _num_text(v)
            if txt is None:
                bad.append(f"{k} must be a number")
                continue
            sym[k] = {"kind": "f64", "text": txt}
            done.append(f"{k} {txt}")
        elif k == "orientation":
            if t != "bar" or v not in ("vertical", "horizontal"):
                bad.append("orientation is a bar's, vertical or horizontal")
                continue
            sym[k] = v
            done.append(f"{v}")
        elif k == "window":
            m = _DURATION_RE.match(str(v).strip()) if isinstance(v, str) else None
            secs = int(m.group(1)) * _DUR_S[m.group(2)] if m else 0
            if t != "trend" or not (10 <= secs <= 400 * 86400):
                bad.append("window is a trend's, like \"1h\" or \"8h\" (10 s - 400 d)")
                continue
            sym[k] = str(v).strip()
            done.append(f"window {v}")
        elif k == "y_axis":
            if t != "trend":
                bad.append("only a trend has a y axis")
            elif v in ("auto", "per_tag"):
                sym[k] = v
                done.append(f"y axis {v}")
            elif isinstance(v, dict) and _num_text(v.get("min")) and _num_text(v.get("max")):
                sym[k] = {"min": {"kind": "f64", "text": _num_text(v["min"])},
                          "max": {"kind": "f64", "text": _num_text(v["max"])}}
                done.append(f"y axis {_num_text(v['min'])} to {_num_text(v['max'])}")
            else:
                bad.append("y_axis is \"auto\", \"per_tag\" or {min, max}")
        elif k == "colors":
            if t != "trend" or not isinstance(v, list):
                bad.append("colors is a trend's list of color names")
                continue
            cs = [color_value(c, request) for c in v]
            if any(c is None for c, _ in cs):
                bad.append(next(why for c, why in cs if c is None) or "not a color")
                continue
            sym[k] = [c for c, _ in cs]
            done.append("trend colors")
        elif k == "show":
            if t != "trend" or not isinstance(v, dict) or not all(isinstance(v.get(x, True), bool) for x in ("legend", "cursor")):
                bad.append("show is a trend's {legend, cursor} (true / false)")
                continue
            sym[k] = {"legend": bool(v.get("legend", True)), "cursor": bool(v.get("cursor", True))}
            done.append("trend legend / cursor")
        elif k == "rows":
            if t != "table" or v not in ("bindings", "attributes"):
                bad.append("rows is a table's, bindings or attributes")
                continue
            sym[k] = v
            done.append(f"rows {v}")
        elif k == "element":
            e = g.find_element(str(v)) if (g is not None and isinstance(v, str)) else None
            if t != "table" or e is None:
                bad.append(f"element is a table's, a listed element path ('{v}' was not found)")
                continue
            sym[k] = e["path"]
            done.append(f"element {e['path']}")
        elif k == "text":
            if t != "text" or not isinstance(v, str) or len(v) > 2000:
                bad.append("text is a text symbol's, at most 2,000 characters")
                continue
            sym[k] = v
            done.append("text")
        elif k == "shape":
            if t != "shape" or v not in ("rect", "ellipse", "line", "polyline"):
                bad.append("shape is rect, ellipse, line or polyline")
                continue
            sym[k] = v
            done.append(f"shape {v}")
        elif k == "flow":
            if t != "shape" or not isinstance(v, bool):
                bad.append("flow is a shape's, true or false")
                continue
            sym[k] = v
            done.append("flow" if v else "no flow")
        elif k == "image":
            sha = str(v).strip().lower() if isinstance(v, str) else ""
            if t != "image":
                bad.append("only an image symbol has an image")
                continue
            if not re.match(r"^[0-9a-f]{64}$", sha):
                sha = (images or {}).get(name_key(str(v).strip()), "")
            if not sha:
                bad.append(f"no image named '{v}' in the image library")
                continue
            sym[k] = sha
            done.append("image")
        elif k == "nav":
            nv = {"display": v} if isinstance(v, str) else v
            if not isinstance(nv, dict) or not isinstance(nv.get("display"), str) \
                    or not 1 <= len(nv["display"].strip()) <= 200:
                bad.append("nav names a display by name")
                continue
            out: Dict[str, Any] = {"display": nv["display"].strip()}
            if isinstance(nv.get("context"), str) and nv["context"].strip():
                out["context"] = nv["context"].strip()
            if nv.get("time") in ("same", "live"):
                out["time"] = nv["time"]
            sym["nav"] = out
            done.append(f"opens {out['display']}")
        elif k == "rules":
            errs: List[str] = []
            _check_rules(v, "rules", errs)
            if errs:
                bad.append("rules: " + "; ".join(errs[:3]))
                continue
            sym["rules"] = _map_rules(v)
            done.append("rules")
        elif k in (t, "kind", "type", "id", "symbol"):
            continue                                   # the model restating what the symbol is
        elif ignored is not None:
            ignored.append(str(k))
        else:
            bad.append(f"ignored '{k}' — Describe cannot set that on a {t or 'symbol'}")
    sym["style"] = style
    return done, bad


def _set_equipment_field(sym: Dict[str, Any], k: str, v: Any) -> Optional[str]:
    """`set` of an equipment symbol's own field (design_displays §5.7.8 items 1, 3; §5.7.9 R1, R12),
    validated against the editor's allow-list. Changing the glyph keeps the bindings that still have
    a place (a 2nd binding only on the five glyphs of R5), adds `min` / `max` 0 / 100 when the new
    glyph requires them and they are absent, and drops every key the new glyph does not accept
    (`min` / `max` / `normal` / `limits` / `letters` / `states` keys). Returns the refusal reason,
    or None (applied)."""
    if sym.get("type") != "equipment":
        return f"{k} is an equipment symbol's"
    g = sym.get("glyph")
    if k == "glyph":
        ng = glyph_of(v)
        if ng is None:
            return "glyph must be one of the equipment symbols of the Equipment palette"
        sym["glyph"] = ng
        sym["bind"] = list(sym.get("bind") or [])[:glyph_bind_max(ng)]
        accepts = glyph_fields(ng)
        for f in ("min", "max", "normal", "limits", "letters"):
            if f not in accepts:
                sym.pop(f, None)
        if ng in RANGE_GLYPHS and ("min" not in sym or "max" not in sym):    # R1 / R12: required
            sym.setdefault("min", {"kind": "f64", "text": "0"})
            sym.setdefault("max", {"kind": "f64", "text": "100"})
            sym["_range_defaulted"] = True
        if isinstance(sym.get("states"), dict):
            keep = {s: c for s, c in sym["states"].items() if s in glyph_states(ng)}
            if keep:
                sym["states"] = keep
            else:
                sym.pop("states", None)
        else:
            sym.pop("states", None)
        return None
    if k == "rotate":
        r = _as_int(v.strip().rstrip("°").strip() if isinstance(v, str) else v)
        if r not in ROTATIONS:
            return "rotate must be 0, 90, 180 or 270"
        if r:
            sym["rotate"] = r
        else:
            sym.pop("rotate", None)
        return None
    if k == "flip":
        if not isinstance(v, bool):
            return "flip is true or false"
        if v:
            sym["flip"] = True
        else:
            sym.pop("flip", None)
        return None
    if k == "letters":
        if g not in LETTERS_GLYPHS:
            return "only an instrument bubble symbol has letters"
        if v in (None, ""):
            sym.pop("letters", None)
            return None
        if not (isinstance(v, str) and letters_valid(v.strip().upper())):
            return "letters are 1 to 4 letters A-Z"
        sym["letters"] = v.strip().upper()
        return None
    if k in ("normal", "limits"):
        if k not in glyph_fields(g):
            return ("only a vessel or analog indicator symbol has a normal band" if k == "normal"
                    else "only a vessel symbol (a tank, silo, drum, ...) has limits")
        if not isinstance(v, dict) or any(x not in ("lo", "hi") for x in v) or any(
                v.get(x) not in (None, "") and _num_text(v[x]) is None for x in ("lo", "hi")):
            return f"{k} is {{lo, hi}}, numbers"
        out = {x: {"kind": "f64", "text": _num_text(v[x])} for x in ("lo", "hi") if v.get(x) not in (None, "")}
        if k == "normal" and len(out) != 2:
            return "normal needs both lo and hi"
        if out:
            sym[k] = out
        else:
            sym.pop(k, None)
        return None
    allowed = glyph_states(g)
    if not allowed:
        return f"{_a_kind(sym)} has no states"
    if not isinstance(v, dict) or any(s not in allowed for s in v) or any(
            not isinstance(c, list) or not all(_as_int(x) is not None for x in c) for c in v.values()):
        return f"states are {', '.join(allowed)}, each a list of codes"
    sym["states"] = {s: [_as_int(x) for x in c] for s, c in v.items()}
    return None


def _float_binding(b: Any, points: Dict[Any, Dict[str, Any]], ctx_el: Optional[Dict[str, Any]],
                   g: Optional["_Grounding"]) -> bool:
    """Whether a binding reaches a FLOAT point (decimals default, §6.3 item 6)."""
    if not isinstance(b, dict):
        return False
    if "point" in b:
        p = points.get(b["point"])
        return bool(p) and str(p.get("point_type") or "").startswith("float")
    if "attr" in b:
        el = None
        if b.get("element") and g is not None:
            el = g.find_element(str(b["element"]))
        elif ctx_el is not None:
            el = ctx_el
        for a in (el or {}).get("attributes") or []:
            if isinstance(a, dict) and name_key(str(a.get("name"))) == name_key(str(b["attr"])):
                v = a.get("value") or {}
                if v.get("kind") == "tag":
                    tag = v.get("tag") or {}
                    pt = tag.get("point_type") or (points.get(v.get("point")) or {}).get("point_type")
                    return str(pt or "").startswith("float")
    return False


def default_decimals(sym: Dict[str, Any], points: Dict[Any, Dict[str, Any]],
                     ctx_el: Optional[Dict[str, Any]], g: Optional["_Grounding"]) -> bool:
    """A new value / gauge / bar / trend / table bound to a float point gets `style.decimals: 2`
    unless it already has decimals. Returns whether it was set."""
    if sym.get("type") not in ("value", "gauge", "bar", "trend", "table"):
        return False
    st = sym.get("style") if isinstance(sym.get("style"), dict) else {}
    if "decimals" in st:
        return False
    if any(_float_binding(b, points, ctx_el, g) for b in sym.get("bind") or []):
        sym["style"] = dict(st, decimals=2)
        return True
    return False


class _Editor:
    """The geometry the engine computes for a follow-up (and the first draft's settling): absolute
    boxes, footprints (measured for untouched symbols, predicted for new / changed ones), the
    free-space search and the permitted shrink. Coordinates are canvas px."""

    def __init__(self, top: List[Any], W: int, H: int, grid: int,
                 measured: Optional[Dict[str, Tuple[int, int, int, int]]] = None,
                 values: Optional[Dict[str, str]] = None) -> None:
        self.top = top
        self.W, self.H, self.grid = W, H, grid
        self.measured = dict(measured or {})
        self.values = dict(values or {})
        self.orig: Dict[str, Tuple[int, int, int, int]] = {}
        for s in self._all():
            ax, ay, w, h = self.abs_box(s)
            self.orig[str(s.get("id"))] = (ax, ay, w, h)
        self.reshaped: set = set()
        # The symbols ACTUALLY removed so far in this batch (§6.4 item 4) — never the model's
        # intended removals: a refused removal leaves its symbol in every overlap check.
        self.removing: set = set()
        self.ghosts: Dict[str, Dict[str, Any]] = {}
        # How notes name the symbols without a label, computed on the display as it is now
        # (before the batch), so a removed symbol is named as the user saw it (§6.4 item 7).
        self.names: Dict[str, str] = _distinct_names([(s, self.abs_box(s)) for s in self.leaves()], W, H)
        # Whether a symbol is locked (the caller sets it to include the page's `locked_ids`); a
        # refusal's fix then says "unlock …" for a locked obstacle, never "allow resizing" it.
        self.is_locked: Callable[[Dict[str, Any]], bool] = lambda s: s.get("locked") is True
        # The obstacles the last settle moved out of the way (§6.5 item 2b).
        self.last_moved: List[Dict[str, Any]] = []
        # Whether two symbols may overlap because the request asked for it (§6.6 item 1); None =
        # never (a first draft, or a request with no positive overlap clause).
        self.overlap_ok: Optional[Callable[[Dict[str, Any], Dict[str, Any]], bool]] = None
        # id → the size it never shrinks below (an added symbol asked bigger keeps its default).
        self.min_size: Dict[str, Tuple[int, int]] = {}

    def _all(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []

        def walk(lst: List[Any]) -> None:
            for s in lst:
                if isinstance(s, dict):
                    out.append(s)
                    if s.get("type") == "group" and isinstance(s.get("children"), list):
                        walk(s["children"])

        walk(self.top)
        return out

    def origin(self, sym: Dict[str, Any]) -> Tuple[int, int]:
        def walk(lst: List[Any], ox: int, oy: int) -> Optional[Tuple[int, int]]:
            for s in lst:
                if s is sym:
                    return ox, oy
                if isinstance(s, dict) and s.get("type") == "group" and isinstance(s.get("children"), list):
                    r = walk(s["children"], ox + _int0(s.get("x")), oy + _int0(s.get("y")))
                    if r is not None:
                        return r
            return None

        return walk(self.top, 0, 0) or (0, 0)

    def abs_box(self, sym: Dict[str, Any]) -> Tuple[int, int, int, int]:
        ox, oy = self.origin(sym)
        return ox + _int0(sym.get("x")), oy + _int0(sym.get("y")), _int0(sym.get("w")), _int0(sym.get("h"))

    def set_abs(self, sym: Dict[str, Any], ax: int, ay: int) -> None:
        ox, oy = self.origin(sym)
        sym["x"], sym["y"] = ax - ox, ay - oy

    def leaves(self) -> List[Dict[str, Any]]:
        return [s for s in self._all() if s.get("type") != "group"
                and str(s.get("id")) not in self.removing
                and all(_as_int(s.get(k)) is not None for k in ("x", "y", "w", "h"))]

    def fp(self, sym: Dict[str, Any]) -> Tuple[int, int, int, int]:
        ax, ay, w, h = self.abs_box(sym)
        sid = str(sym.get("id"))
        m = self.measured.get(sid)
        o = self.orig.get(sid)
        if m is not None and o is not None and sid not in self.reshaped and (w, h) == (o[2], o[3]):
            return m[0] + ax - o[0], m[1] + ay - o[1], m[2], m[3]
        return predict_footprint(sym, ax, ay, self.values.get(sid))

    def overlap(self, a: Dict[str, Any], b: Dict[str, Any]) -> bool:
        if display_pipes.is_pipe(a) or display_pipes.is_pipe(b):
            return False                               # D3.6: a pipe is never "overlapping" (item 9)
        fa, fb = self.fp(a), self.fp(b)
        if not _rect_overlap(fa, fb):
            return False
        ba, bb = self.abs_box(a), self.abs_box(b)
        # A shape drawn behind what it fully contains is a backdrop — for an arrangement that was
        # already there (D5.4: a symbol an edit moves or adds onto a shape collides with it unless
        # the request asks for "inside" / overlap; the caller skips the check then).
        if (a.get("type") == "shape" and _rect_contains(ba, bb)) or (b.get("type") == "shape" and _rect_contains(bb, ba)):
            if self.untouched(a) and self.untouched(b):
                return False
        return True

    def untouched(self, sym: Dict[str, Any]) -> bool:
        """Whether `sym` is where and as big as it was when this editor started (a first draft's
        symbols have no ids yet: the whole draft is the model's arrangement, so untouched)."""
        if sym.get("id") is None:
            return True
        o = self.orig.get(str(sym.get("id")))
        return o is not None and self.abs_box(sym) == o

    def conflicts(self, sym: Dict[str, Any], among: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        """The symbols `sym`'s footprint overlaps — except a pair the request permits to overlap
        (`overlap_ok`, §6.6 item 1: scoped to the symbols of the clause that asked)."""
        pool = self.leaves() if among is None else among
        ok = self.overlap_ok
        return [o for o in pool if o is not sym and self.overlap(sym, o) and not (ok is not None and ok(sym, o))]

    def clamp(self, sym: Dict[str, Any], H: Optional[int] = None) -> None:
        H = self.H if H is None else H
        ax, ay, w, h = self.abs_box(sym)
        w = max(self.grid, min(w, self.W // self.grid * self.grid))
        h = max(self.grid, min(h, H // self.grid * self.grid))
        sym["w"], sym["h"] = w, h
        ax = max(0, min(_snap(ax, self.grid), (self.W - w) // self.grid * self.grid))
        ay = max(0, min(_snap(ay, self.grid), (H - h) // self.grid * self.grid))
        self.set_abs(sym, ax, ay)

    def floor(self, sym: Dict[str, Any]) -> Tuple[int, int]:
        """The smallest box `sym` may be shrunk to (§6.5 item 4): its readability floor — for a
        symbol this request added and asked bigger, its kind default (`min_size`, settled after
        Fable 5.1's review) — or its own size when it is already smaller (never grows a side)."""
        fw, fh = floor_size(sym, self.W, self.H, self.grid)
        mw, mh = self.min_size.get(str(sym.get("id")), (0, 0))
        fw, fh = max(fw, mw), max(fh, mh)
        return min(fw, max(self.grid, _int0(sym.get("w")))), min(fh, max(self.grid, _int0(sym.get("h"))))

    def free_spot(self, sym: Dict[str, Any], among: Optional[List[Dict[str, Any]]] = None,
                  H: Optional[int] = None, fixed: Tuple[bool, bool] = (False, False),
                  avoid: Sequence[Tuple[int, int, int, int]] = (),
                  side_of: Optional[Tuple[int, int, int, int]] = None) -> Optional[Tuple[int, int]]:
        """The free position nearest the symbol's current one (its footprint touching nothing),
        inside the canvas, on the grid. Candidates: the asked spot, the canvas edges, and every
        position that sets its footprint GAP px beside another footprint. D5.4: `fixed` (x, y)
        keeps a constrained axis where it is (§6.5 item 2a); `avoid` = extra boxes to keep clear
        of (what a make_room gets out of the way of); `side_of` prefers staying on the same side of
        that box (§6.5 item 1, make_room)."""
        H = self.H if H is None else H
        pool = [o for o in (self.leaves() if among is None else among) if o is not sym]
        ax, ay, w, h = self.abs_box(sym)
        fx, fy, fw, fh = self.fp(sym)
        offx, offy = fx - ax, fy - ay
        xmax = (self.W - w) // self.grid * self.grid
        ymax = (H - h) // self.grid * self.grid
        xs = {ax, 0, xmax}
        ys = {ay, 0, ymax}
        boxes = [self.fp(o) for o in pool if not display_pipes.is_pipe(o)] + list(avoid)   # D3.6: exempt
        for ox, oy, ow, oh in boxes:
            xs.add(_ceil_g(ox + ow + GAP - offx, self.grid))
            xs.add(_floor_g(ox - GAP - fw - offx, self.grid))
            ys.add(_ceil_g(oy + oh + GAP - offy, self.grid))
            ys.add(_floor_g(oy - GAP - fh - offy, self.grid))
        if fixed[0]:
            xs = {ax}
        if fixed[1]:
            ys = {ay}

        def side(p: Tuple[int, int]) -> int:
            if side_of is None:
                return 0
            rcx2, rcy2 = 2 * side_of[0] + side_of[2], 2 * side_of[1] + side_of[3]
            cx2, cy2 = 2 * ax + w, 2 * ay + h
            nx2, ny2 = 2 * p[0] + w, 2 * p[1] + h
            same_x = (nx2 - rcx2) * (cx2 - rcx2) > 0
            same_y = (ny2 - rcy2) * (cy2 - rcy2) > 0
            return 0 if (same_x or same_y) else 1

        cands = sorted(((x, y) for x in xs for y in ys if 0 <= x <= xmax and 0 <= y <= ymax),
                       key=lambda p: (side(p), (p[0] - ax) ** 2 + (p[1] - ay) ** 2, abs(p[1] - ay),
                                      abs(p[0] - ax), p[1], p[0]))
        for x, y in cands:
            self.set_abs(sym, x, y)
            if self.conflicts(sym, pool):
                continue
            f = self.fp(sym)
            if any(_rect_overlap(f, b) for b in avoid):
                continue
            return x, y
        self.set_abs(sym, ax, ay)
        return None

    def move_obstacles(self, sym: Dict[str, Any], hit: List[Dict[str, Any]],
                       movable: Callable[[Dict[str, Any]], bool],
                       among: Optional[List[Dict[str, Any]]] = None,
                       H: Optional[int] = None) -> Optional[List[Dict[str, Any]]]:
        """§6.5 item 2b: move every symbol `sym` overlaps out of its way, each AT ITS OWN SIZE to
        the free spot nearest where it is — only when every one of them is `movable`. Returns the
        moved symbols, or None (all of them back where they were)."""
        if not hit or not all(movable(o) for o in hit):
            return None
        start = [(o, self.abs_box(o)) for o in hit]
        for o in hit:
            if not self.conflicts(o, among):
                continue
            if self.free_spot(o, among, H) is None:
                for x, b in start:
                    self.set_abs(x, b[0], b[1])
                return None
        if self.conflicts(sym, among):
            for x, b in start:
                self.set_abs(x, b[0], b[1])
            return None
        return hit

    def trim_clear(self, sym: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """§6.4 item 5: `sym` just took a removed symbol's frame; cut that frame back, one edge at a
        time, from each footprint it overlaps (the cut keeping the most area wins; on the grid;
        never below the kind minimum). The result lies inside the frame, so a gauge's drawn square
        is clear too. Returns the symbols it was cut back from ([] = nothing overlapped), or None
        when cutting cannot clear it (the frame restored; the ordinary settle takes over)."""
        start = self.abs_box(sym)
        had = str(sym.get("id")) in self.reshaped
        minw, minh = self.floor(sym)
        cut: List[Dict[str, Any]] = []
        for _ in range(16):
            hit = self.conflicts(sym)
            if not hit:
                return cut
            o = hit[0]
            ox, oy, ow, oh = self.fp(o)
            ax, ay, w, h = self.abs_box(sym)
            x1, y1 = ax + w, ay + h
            best: Optional[Tuple[int, Tuple[int, int, int, int]]] = None
            for a0, b0, a1, b1 in ((ax, _ceil_g(oy + oh, self.grid), x1, y1),
                                   (ax, ay, x1, _floor_g(oy, self.grid)),
                                   (_ceil_g(ox + ow, self.grid), ay, x1, y1),
                                   (ax, ay, _floor_g(ox, self.grid), y1)):
                if a1 - a0 < minw or b1 - b0 < minh:
                    continue
                if _rect_overlap((a0, b0, a1 - a0, b1 - b0), (ox, oy, ow, oh)):
                    continue
                area = (a1 - a0) * (b1 - b0)
                if best is None or area > best[0]:
                    best = (area, (a0, b0, a1 - a0, b1 - b0))
            if best is None:
                break
            nx, ny, nw, nh = best[1]
            sym["w"], sym["h"] = nw, nh
            self.set_abs(sym, nx, ny)
            self.reshaped.add(str(sym.get("id")))
            if all(o is not c for c in cut):
                cut.append(o)
        sym["w"], sym["h"] = start[2], start[3]
        self.set_abs(sym, start[0], start[1])
        if not had:
            self.reshaped.discard(str(sym.get("id")))
        return None

    def make_room_in(self, ref: Dict[str, Any], sym: Dict[str, Any], where: str,
                     spot: Callable[[Tuple[int, int, int, int]], Tuple[int, int]],
                     allow_overlap: bool = False,
                     movable: Optional[Callable[[Dict[str, Any]], bool]] = None) -> Optional[str]:
        """§6.4 item 6: `sym` was placed `where` of `ref`, a symbol this request created. When that
        spot is outside the canvas or over another symbol, shrink `ref` on the side facing `sym`
        (its other three edges kept, aspect not kept, grid steps, never below the floor) just
        enough for `sym` to fit there. D5.4: a symbol that can be moved out of the way at its own
        size (`movable`, §6.5 item 2b) does not count as in the way. Returns "shorter" /
        "narrower" when it did, else None (`ref` and `sym` back as they were; the free-space
        search takes over)."""
        def fits() -> bool:
            ax, ay, w, h = self.abs_box(sym)
            inside = 0 <= ax and ax + w <= self.W and 0 <= ay and ay + h <= self.H
            hit = [] if allow_overlap else [o for o in self.conflicts(sym) if movable is None or not movable(o)]
            return inside and not hit

        if fits():
            return None
        r0 = self.abs_box(ref)
        s0 = self.abs_box(sym)
        rid = str(ref.get("id"))
        had = rid in self.reshaped
        minw, minh = self.floor(ref)
        vertical = where in ("below", "above")
        for k in range(1, max(self.W, self.H) // self.grid + 1):
            if vertical:
                nh = r0[3] - k * self.grid
                if nh < minh:
                    break
                ref["h"] = nh
                self.set_abs(ref, r0[0], r0[1] + (r0[3] - nh if where == "above" else 0))
            else:
                nw = r0[2] - k * self.grid
                if nw < minw:
                    break
                ref["w"] = nw
                self.set_abs(ref, r0[0] + (r0[2] - nw if where == "left_of" else 0), r0[1])
            self.reshaped.add(rid)
            self.set_abs(sym, *spot(self.fp(ref)))
            if fits():
                return "shorter" if vertical else "narrower"
        ref["w"], ref["h"] = r0[2], r0[3]
        self.set_abs(ref, r0[0], r0[1])
        if not had:
            self.reshaped.discard(rid)
        self.set_abs(sym, s0[0], s0[1])
        return None

    def settle(self, sym: Dict[str, Any], *, may_shrink: Sequence[Dict[str, Any]] = (),
               own: bool = True, among: Optional[List[Dict[str, Any]]] = None,
               H: Optional[int] = None, fixed: Tuple[bool, bool] = (False, False),
               movable: Optional[Callable[[Dict[str, Any]], bool]] = None) -> Tuple[Optional[str], List[str]]:
        """Resolve `sym`'s overlaps (design_displays §6.3 item 3, under §6.5 item 2): (a) the
        nearest free position — along the axes `fixed` does not hold; (b) move the overlapped
        symbols out of the way at their own size, when every one is `movable`; (c) shrink the
        permitted symbols (`may_shrink`, then `sym` itself when `own` and no axis is fixed),
        keeping aspect, never below the floor; else (d) a reason. Returns (reason | None, notes)."""
        hit = self.conflicts(sym, among)
        if not hit:
            return None, []
        start = self.abs_box(sym)
        if not (fixed[0] and fixed[1]) and self.free_spot(sym, among, H, fixed=fixed) is not None:
            return None, [f"kept {_sym_name(sym, self.names)} clear of {', '.join(_sym_name(o, self.names) for o in hit[:3])}"]
        if movable is not None:
            gone = self.move_obstacles(sym, hit, movable, among, H)
            if gone:
                self.last_moved = list(gone)
                return None, [f"moved {_sym_name(o, self.names)} out of the way of {_sym_name(sym, self.names)}"
                              for o in gone]
        cands: List[Dict[str, Any]] = []
        for c in list(may_shrink) + ([sym] if own and not (fixed[0] or fixed[1]) else []):
            if all(c is not x for x in cands):
                cands.append(c)
        for c in cands:
            cx, cy, cw, ch = self.abs_box(c)
            had = str(c.get("id")) in self.reshaped
            minw, minh = self.floor(c)
            sx, sy, sw, sh = self.abs_box(sym)
            for k in range(1, 25):
                f = Fraction(9, 10) ** k
                nw, nh = _shrunk(cw, ch, f, minw, minh, self.grid)
                if nw >= _int0(c.get("w")) and nh >= _int0(c.get("h")):
                    break
                nx = cx if (c is sym or 2 * cx + cw <= 2 * sx + sw) else _floor_g(cx + cw - nw, self.grid)
                ny = cy if (c is sym or 2 * cy + ch <= 2 * sy + sh) else _floor_g(cy + ch - nh, self.grid)
                c["w"], c["h"] = nw, nh
                self.set_abs(c, nx, ny)
                self.reshaped.add(str(c.get("id")))
                if c is not sym:
                    self.set_abs(sym, start[0], start[1])
                if not self.conflicts(sym, among) or (not (fixed[0] and fixed[1])
                                                      and self.free_spot(sym, among, H, fixed=fixed) is not None):
                    note = [] if c is sym else [f"made {_sym_name(c, self.names)} smaller to make room"]
                    return None, note
            c["w"], c["h"] = cw, ch
            self.set_abs(c, cx, cy)
            if not had:
                self.reshaped.discard(str(c.get("id")))
        self.set_abs(sym, start[0], start[1])
        names = ", ".join(_sym_name(o, self.names) for o in hit[:3])
        locked_hit = [o for o in hit[:3] if self.is_locked(o)]
        free_hit = [o for o in hit[:3] if not self.is_locked(o)]
        parts: List[str] = []
        if locked_hit:
            parts.append(f"unlock {', '.join(_sym_name(o, self.names) for o in locked_hit)} (Ctrl+L)")
        if free_hit and not may_shrink:
            parts.append(f"allow resizing {', '.join(_sym_name(o, self.names) for o in free_hit)}")
        if parts:
            fix = ", ".join(parts) + ", or make the canvas larger"
        else:
            fix = "make the canvas larger or remove something"
        return f"{_sym_name(sym, self.names)} does not fit without overlapping {names} — {fix}", []


def _reference_problems(request: str, top: List[Any], ops: List[Dict[str, Any]],
                        names: Optional[Dict[str, str]] = None) -> Tuple[Dict[str, str], List[str]]:
    """Deterministic checks of "the <kind>" in the request against what the display holds (§6.3
    item 2): a kind the display does not have, or one of several with nothing to tell them apart.
    Returns ({symbol id: refusal reason} for the targets it rules out, notes)."""
    toks = [name_key(t) for t in re.findall(r"[\w'’\-]+", request or "")]
    syms = [s for s in _walk(top) if isinstance(s, dict) and s.get("type") != "group"]
    adds = {str((o.get("symbol") or {}).get("kind")) for o in ops if o.get("op") == "add"}
    req_words = {name_key(w) for w in _words(request)}
    everything = bool(req_words & {"everything", "all", "every", "each", "both"})
    out: Dict[str, str] = {}
    notes: List[str] = []
    mentioned = {_KIND_WORDS[t] for t in toks if t in _KIND_WORDS}

    def accounted(s: Dict[str, Any]) -> bool:
        if s.get("type") in mentioned or everything or (_glyph_words_of(s) & set(toks)):
            return True
        words = {name_key(w) for w in _words(str(s.get("label") or "") + " " + str(s.get("text") or ""))}
        return bool(words & req_words)

    for i, t in enumerate(toks):
        kind = _KIND_WORDS.get(t)
        if kind is None and i > 0 and toks[i - 1] == "the" and not t.endswith("s"):
            # D3.4: "the pump" among two or more pump symbols; "the valve" on a display whose
            # equipment holds no valve (on a display with no equipment at all a glyph word is a
            # plant word — "the pump flow" — not a reference to check)
            same = _eq_matches(t, syms)
            added_here = any(o.get("op") == "add" and isinstance(o.get("symbol"), dict)
                             and o["symbol"].get("kind") == "equipment"
                             and glyph_of(o["symbol"].get("glyph")) in GLYPH_REF_WORDS.get(t, ()) for o in ops)
            if not same and t in GLYPH_REF_WORDS and not added_here and \
                    any(s.get("type") == "equipment" for s in syms):
                notes.append(f"there is no '{t}' on this display — it has: {_holdings(top, names=names)}")
                for o in ops:
                    for sid in _op_ids(o) + [r for k in ("of", "to", "a", "b") for r in _ref_list(o.get(k))]:
                        s = next((x for x in syms if str(x.get("id")) == sid), None)
                        if s is not None and not accounted(s):
                            out.setdefault(sid, f"there is no {t} on this display")
            elif len(same) >= 2:
                kind = _kind_noun(same[0]) if len({_kkey(s) for s in same}) == 1 else f"{t} symbol"
                nxt = toks[i + 1] if i + 1 < len(toks) else ""
                labels = {name_key(str(s["label"]).strip()) for s in same if s.get("label")}
                label_words: set = set()
                for s in same:
                    label_words |= {name_key(w) for w in _words(str(s.get("label") or ""))} - {t}
                if nxt in _AFTER_QUALIFIERS or nxt in labels or req_words & label_words or nxt == "symbol" \
                        and i + 2 < len(toks) and toks[i + 2] in _AFTER_QUALIFIERS:
                    continue
                notes.append(f"I could not tell which {kind} 'the {t}' means — the display has: "
                             f"{', '.join(_sym_name(s, names) for s in same)}")
                for s in same:
                    out.setdefault(str(s.get("id")), f"'the {t}' could mean more than one {kind}")
            continue
        if kind is None or i == 0 or toks[i - 1] != "the" or t.endswith("s") and t not in ("text",) and t != kind:
            continue
        same = [s for s in syms if s.get("type") == kind]
        if not same and kind not in adds:
            notes.append(f"there is no '{t}' on this display — it has: {_holdings(top, names=names)}")
            for o in ops:
                for sid in _op_ids(o) + [r for k in ("of", "to", "a", "b") for r in _ref_list(o.get(k))]:
                    s = next((x for x in syms if str(x.get("id")) == sid), None)
                    if s is not None and not accounted(s):
                        out.setdefault(sid, f"there is no {t} on this display")
        elif len(same) >= 2:
            # Only what MODIFIES the reference tells them apart (ruling, D5.2 gate): "the larger
            # trend" never reaches here (the word before the kind is not "the"); "the trend on the
            # left", "the trend B", or a label word of one of them in the request do. A size /
            # position word that is the requested CHANGE ("make the trend bigger", "move the gauge
            # left") does not.
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            label_words = set()
            labels = set()
            for s in same:
                label_words |= {name_key(w) for w in _words(str(s.get("label") or ""))} - {kind, t}
                if s.get("label"):
                    labels.add(name_key(str(s["label"]).strip()))
            if nxt in _AFTER_QUALIFIERS or nxt in labels or req_words & label_words:
                continue
            notes.append(f"I could not tell which {kind} 'the {t}' means — the display has: "
                         f"{', '.join(f'{_sym_name(s, names)} ({kind})' for s in same)}")
            for s in same:
                out.setdefault(str(s.get("id")), f"'the {t}' could mean more than one {kind}")
    return out, notes


# Settled at the D5.5 corpus (C135, "delete the gauge in the middle" with both gauges on the right):
# a reference by POSITION must hold — the targets an op names must satisfy it, else nothing is guessed.
_POS_WORDS = r"(?:top|upper|bottom|lower)(?:[\s\-]+(?:left|right|middle|center|centre))?|left|right|middle|center|centre"
_KIND_ALT = "|".join(sorted(({w for ws in _KIND_REFS.values() for w in ws if not w.endswith("s") or w in ("text",)}
                             | {w for w in GLYPH_REF_WORDS if not w.endswith("s") and w != "equipment"})
                            - {"font", "words", "label", "title", "caption", "line", "light", "number"},
                            key=len, reverse=True))
# "the gauge on the left", "the trend that is at the top", "the gauge in the middle of the screen"
_POS_AFTER_RE = re.compile(r"\bthe\s+(" + _KIND_ALT + r")\s+(?:(?:that|which)\s+(?:is|'s|’s)\s+)?"
                           r"(?:on|at|in)\s+the\s+(" + _POS_WORDS + r")(?:\s+(?:hand\s+)?side)?\b", re.I)
# "the left gauge", "the leftmost trend", "the middle gauge", "the top-left value"
_POS_BEFORE_RE = re.compile(r"\bthe\s+(leftmost|rightmost|topmost|bottommost|uppermost|lowermost|left[\s\-]hand|"
                            r"right[\s\-]hand|" + _POS_WORDS + r")\s+(" + _KIND_ALT + r")\b", re.I)
# "the second gauge from the left"
_POS_ORDINAL_RE = re.compile(r"\bthe\s+(first|second|third|fourth|fifth|last|1st|2nd|3rd|4th|5th)\s+(" + _KIND_ALT +
                             r")\s+from\s+the\s+(left|right|top|bottom)\b", re.I)
# Verbs after which "the gauge on the left" is where it goes, not which one it is ("put the gauge on
# the left") — unless the clause names a destination after it ("move the gauge on the left below …").
_PLACING_VERBS = frozenset("put place position move drag shift relocate keep set show".split())
_DESTINATION_RE = re.compile(r"\b(?:to|into|onto|below|above|under|underneath|beneath|beside|next|between|over|"
                             r"out|left\s+of|right\s+of|in\s+its\s+place|where|instead)\b", re.I)
_ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3, "4th": 3,
             "fifth": 4, "5th": 4}


def _position_problems(request: str, top: List[Any], ops: List[Dict[str, Any]],
                       boxes: Dict[str, Tuple[int, int, int, int]], W: int, H: int,
                       names: Optional[Dict[str, str]] = None) -> Tuple[Dict[str, str], List[str]]:
    """A reference to an existing symbol by its position — "the gauge in the middle", "the left
    gauge", "the second trend from the left" — is checked against where the symbols ARE (frame
    centers in thirds of the canvas, the rule `_distinct_names` uses; "the left / leftmost <kind>"
    relative to the others of its kind). An op that names a symbol of that kind which does not
    satisfy the position is refused, and so is every op on that kind when no symbol satisfies it
    or several do with nothing else (a label word) to tell them apart — with a note naming what
    the display has. Nothing is guessed. Returns ({symbol id: refusal reason}, notes)."""
    syms = [s for s in _walk(top) if isinstance(s, dict) and s.get("type") != "group" and str(s.get("id")) in boxes]
    req_words = {name_key(w) for w in _words(request)}

    def cx(s: Dict[str, Any]) -> float:
        b = boxes[str(s.get("id"))]
        return b[0] + b[2] / 2

    def cy(s: Dict[str, Any]) -> float:
        b = boxes[str(s.get("id"))]
        return b[1] + b[3] / 2

    def hpos(s: Dict[str, Any]) -> str:
        return "left" if cx(s) < W / 3 else ("right" if cx(s) > 2 * W / 3 else "middle")

    def vpos(s: Dict[str, Any]) -> str:
        return "top" if cy(s) < H / 3 else ("bottom" if cy(s) > 2 * H / 3 else "middle")

    def absolute(pos: str, s: Dict[str, Any]) -> bool:
        ok = True
        for w in re.split(r"[\s\-]+", pos.lower()):
            if w in ("left", "right"):
                ok = ok and hpos(s) == w
            elif w in ("top", "upper"):
                ok = ok and vpos(s) == "top"
            elif w in ("bottom", "lower"):
                ok = ok and vpos(s) == "bottom"
            elif w in ("middle", "center", "centre"):
                ok = ok and hpos(s) == "middle"
        return ok

    def extreme(word: str, same: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The left / leftmost (… top / bottom) one among several of a kind: the unique extreme."""
        w = word.lower().replace("-", " ").split()[0].replace("most", "")
        key, pick = {"left": (cx, min), "right": (cx, max), "top": (cy, min), "upper": (cy, min),
                     "bottom": (cy, max), "lower": (cy, max)}.get(w, (None, None))
        if key is None:
            if w in ("middle", "center", "centre") and len(same) >= 3 and len(same) % 2 == 1:
                order = sorted(same, key=cx)
                return [order[len(order) // 2]]
            return [s for s in same if absolute(w, s)]
        v = pick(key(s) for s in same)
        return [s for s in same if key(s) == v]

    def destination(m: re.Match) -> bool:
        before = re.findall(r"[\w'’\-]+", (request or "")[:m.start()])
        clause_end = _CLAUSE_END_RE.search(request or "", m.end())
        after = (request or "")[m.end():clause_end.start() if clause_end else len(request or "")]
        after = _CLAUSE_WORD_RE.split(after)[0]
        return bool(before) and name_key(before[-1]) in _PLACING_VERBS and not _DESTINATION_RE.search(after)

    def kind_of(word: str) -> Optional[str]:
        """A kind word → its kind; a glyph word ("pump") → "eq:<word>" (D3.4)."""
        w = name_key(word)
        k = _KIND_WORDS.get(w) or next((k for k, ws in _KIND_REFS.items() if w in ws), None)
        if k is None and w in GLYPH_REF_WORDS and w != "equipment":
            k = "eq:" + w
        return k

    def of_kind(s: Dict[str, Any], kind: str) -> bool:
        if kind.startswith("eq:"):
            return any(s is e for e in _eq_matches(kind[3:], [s]))
        return s.get("type") == kind

    def noun(kind: str) -> str:
        return f"{kind[3:].rstrip('s')} symbol" if kind.startswith("eq:") else kind

    phrases: List[Tuple[str, str, List[Dict[str, Any]]]] = []      # (kind, the phrase, symbols satisfying it)
    for m in _POS_AFTER_RE.finditer(request or ""):
        kind = kind_of(m.group(1))
        if kind is None or destination(m):
            continue
        if kind.startswith("eq:") and not any(of_kind(s, kind) for s in syms):
            continue
        same = [s for s in syms if of_kind(s, kind)]
        phrases.append((kind, f"{noun(kind)} {m.group(0).split(m.group(1), 1)[1].strip()}",
                        [s for s in same if absolute(m.group(2), s)]))
    for m in _POS_BEFORE_RE.finditer(request or ""):
        kind = kind_of(m.group(2))
        if kind is None:
            continue
        if kind.startswith("eq:") and not any(of_kind(s, kind) for s in syms):
            continue
        same = [s for s in syms if of_kind(s, kind)]
        sat = extreme(m.group(1), same) if len(same) >= 2 else [s for s in same if absolute(
            m.group(1).replace("most", "").replace("hand", ""), s)]
        phrases.append((kind, f"{m.group(1).lower()} {noun(kind)}", sat))
    for m in _POS_ORDINAL_RE.finditer(request or ""):
        kind = kind_of(m.group(2))
        if kind is None:
            continue
        if kind.startswith("eq:") and not any(of_kind(s, kind) for s in syms):
            continue
        same = [s for s in syms if of_kind(s, kind)]
        side = m.group(3).lower()
        key = cx if side in ("left", "right") else cy
        order = sorted(same, key=key, reverse=side in ("right", "bottom"))
        k = len(order) - 1 if name_key(m.group(1)) == "last" else _ORDINALS.get(name_key(m.group(1)), 99)
        sat = [order[k]] if 0 <= k < len(order) else []
        if len(sat) == 1 and sum(1 for s in same if key(s) == key(sat[0])) > 1:
            sat = [s for s in same if key(s) == key(sat[0])]
        phrases.append((kind, f"{m.group(1).lower()} {noun(kind)} from the {side}", sat))
    if not phrases:
        return {}, []

    def named_by_label(s: Dict[str, Any]) -> bool:
        lw = {name_key(w) for w in _words(f"{s.get('label') or ''} {s.get('text') or ''}")} - set(_KIND_WORDS)
        return bool(lw) and lw <= req_words

    def where(s: Dict[str, Any]) -> str:
        h, v = hpos(s), vpos(s)
        return _H_PHRASE[h] if h != "middle" else (_V_PHRASE[v] if v != "middle" else "in the middle")

    targeted = {sid for o in ops for sid in _op_ids(o) + [r for k in ("of", "to", "a", "b") for r in _ref_list(o.get(k))]}
    out: Dict[str, str] = {}
    notes: List[str] = []
    for kind in dict.fromkeys(k for k, _p, _s in phrases):
        mine = [(p, sat) for k, p, sat in phrases if k == kind]
        allowed = {str(s.get("id")) for _p, sat in mine if len(sat) == 1 for s in sat}
        same = [s for s in syms if of_kind(s, kind)]
        has = ", ".join(f"{_sym_name(s, names)} ({_kind_noun(s)}, {where(s)})" for s in same) or "no " + noun(kind)
        bad = False
        for p, sat in mine:
            if not sat:
                notes.append(f"there is no {p} — the display has: {has}")
                bad = True
            elif len(sat) > 1 and not any(named_by_label(s) for s in sat):
                notes.append(f"I could not tell which {noun(kind)} 'the {p}' means — the display has: {has}")
                bad = True
        for s in same:
            sid = str(s.get("id"))
            if sid in allowed or named_by_label(s) or sid not in targeted:
                continue
            if bad or allowed:
                out[sid] = (f"there is no {mine[0][0]}" if not mine[0][1]
                            else f"'the {mine[0][0]}' is not {_sym_name(s, names)}")
        if not bad and allowed:
            # a resolved position that the op did not use is said once (the op is refused above)
            for sid in [x for x in out if x in {str(s.get('id')) for s in same}]:
                s = next(x for x in same if str(x.get("id")) == sid)
                notes.append(f"{_sym_name(s, names)} is not the {mine[0][0]} — it is {where(s)}")
    return out, notes


# ── Settled at the D5.2 live battery (design_displays §6.3) ─────────────────────────────

_KIND_PREFIXES = ("value", "gauge", "bar", "trend", "table", "text", "shape", "button", "image",
                  "state", "lamp", "symbol", "equipment")
_PROP_ALIASES: Dict[str, str] = {
    "font_size": "font", "size_font": "font", "text_size": "font",
    "text_color": "color", "colour": "color", "text_colour": "color", "font_color": "color",
    "font_colour": "color", "foreground": "color", "foreground_color": "color",
    "fill_color": "fill", "fill_colour": "fill", "background": "fill", "background_color": "fill",
    "background_colour": "fill", "bg_color": "fill",
    "stroke_color": "stroke", "stroke_colour": "stroke", "border": "stroke", "border_color": "stroke",
    "border_colour": "stroke", "outline": "stroke", "line_color": "stroke",
    "border_width": "stroke_width", "line_width": "stroke_width", "stroke_size": "stroke_width",
    "decimal_places": "decimals", "decimal": "decimals", "precision": "decimals", "places": "decimals",
    "minimum": "min", "min_value": "min", "range_min": "min", "maximum": "max", "max_value": "max",
    "range_max": "max", "title": "label", "caption": "label", "name": "label",
    "width": "w", "height": "h", "left": "x", "top": "y", "alignment": "align",
    "time_window": "window", "time_range": "window", "y_range": "y_axis", "axis": "y_axis",
    "legend": "show", "link": "nav", "navigate": "nav", "navigation": "nav",
    "rotation": "rotate", "angle": "rotate", "mirror": "flip", "mirrored": "flip", "flipped": "flip",
    "icon": "glyph", "glyph_type": "glyph", "normal_band": "normal", "normal_range": "normal",
    "state_map": "states", "state_mapping": "states",
}


def prop_key(k: Any) -> str:
    """A model's property name → the panel's (§6.3 battery fix 3): case and spaces / hyphens
    folded to `_`, a leading kind word dropped ("bar orientation" → orientation), then the alias
    table ("font size" → font, "text color" / "colour" → color, "fill color" → fill, "decimal
    places" → decimals, "minimum" / "maximum" → min / max, "title" → label)."""
    t = re.sub(r"[\s\-]+", "_", str(k or "").strip().lower())
    if t in _PROP_ALIASES:
        return _PROP_ALIASES[t]
    for pre in _KIND_PREFIXES:
        if t.startswith(pre + "_") and len(t) > len(pre) + 1:
            rest = t[len(pre) + 1:]
            return _PROP_ALIASES.get(rest, rest)
    return t


def normalize_props(props: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in props.items():
        if k == "style" and isinstance(v, dict):
            out["style"] = normalize_props(v)
        else:
            out[prop_key(k)] = v
    return out


_SCOPE_WORDS = frozenset("""undo redo revert go back undone publish published publishing save saved
saving store keep commit that this it my the work changes change last please now draft display
version all everything again previous step steps a an and to for me do just can you could would
""".split())
_SCOPE_CORE = re.compile(r"\b(undo|redo|revert|go\s+back|publish(?:ed|ing)?|save(?:d)?|commit)\b", re.I)


def out_of_scope_only(request: str) -> bool:
    """A request that is ONLY undo / redo / publish / save (and their synonyms): handled before the
    model (battery fix 4 — "undo that" once removed every symbol)."""
    if not _SCOPE_CORE.search(request or ""):
        return False
    words = re.findall(r"[a-z']+", (request or "").lower())
    return all(w in _SCOPE_WORDS for w in words)


def _scope_notes(request: str) -> List[str]:
    out: List[str] = []
    if re.search(r"\b(undo|redo|revert|go\s+back)\b", request or "", re.I):
        out.append("to undo or redo, use Undo (Ctrl+Z) / Redo (Ctrl+Y)")
    for rx, text in _OUT_OF_SCOPE[1:]:
        if rx.search(request or ""):
            out.append(text)
    return out


_NOT_A_THING = frozenset("""display canvas screen page top bottom left right middle center centre
same size color colour way corner side edge edges whole rest end other one ones two both all
everything grid default first second third last larger smaller bigger biggest largest smallest
height width""".split())


def absent_references(request: str, top: List[Any]) -> List[str]:
    """Phrases "the <thing>" in the request that name nothing the display holds (battery fix 5):
    no kind on the display and no word of any label / text / bound name matches."""
    syms = [s for s in _walk(top) if isinstance(s, dict)]
    kinds = {str(s.get("type")) for s in syms}
    words: set = set()
    for s in syms:
        words |= {name_key(w) for w in _words(f"{s.get('label') or ''} {s.get('text') or ''}")}
        words |= _glyph_words_of(s)                    # D3.4: "the pump" names a pump symbol
    out: List[str] = []
    for m in re.finditer(r"\bthe\s+([A-Za-z0-9][\w\-]*)(?:\s+([A-Za-z][\w\-]*))?", request or ""):
        w1, w2 = name_key(m.group(1)), name_key(m.group(2) or "")
        if w1 in _NOT_A_THING or w1 in STOP_WORDS - set(_KIND_WORDS):
            continue
        phrase = [w1] + ([w2] if w2 and w2 not in STOP_WORDS - set(_KIND_WORDS) and w2 not in _NOT_A_THING else [])
        known = False
        for w in phrase:
            k = _KIND_WORDS.get(w)
            if (k and k in kinds) or w in words or _singular(w) in words:
                known = True
        if not known:
            out.append(" ".join(phrase))
    return out


_SIZE_ADJ = {"large": "max", "larger": "max", "largest": "max", "big": "max", "bigger": "max",
             "biggest": "max", "small": "min", "smaller": "min", "smallest": "min"}
_POS_ADJ = {"left": ("x", "min"), "leftmost": ("x", "min"), "right": ("x", "max"),
            "rightmost": ("x", "max"), "top": ("y", "min"), "upper": ("y", "min"),
            "bottom": ("y", "max"), "lower": ("y", "max")}


def request_references(request: str, top: List[Any], ed: "_Editor", exclude: Sequence[str]) -> List[Dict[str, Any]]:
    """The symbols the request refers to (battery fix 2), excluding `exclude`: a label whose words
    all occur in the request; "the <kind>" when that kind is unique; "the <adjective> <kind>"
    with a size / position adjective modifying it ("the large gauge" = the biggest gauge)."""
    syms = [s for s in ed.leaves() if str(s.get("id")) not in set(exclude)]
    req = {name_key(w) for w in _words(request)}
    found: List[Dict[str, Any]] = []

    def put(s: Optional[Dict[str, Any]]) -> None:
        if s is not None and all(s is not x for x in found):
            found.append(s)

    for s in syms:
        lw = [name_key(w) for w in _words(str(s.get("label") or ""))]
        if lw and all(w in req for w in lw):
            put(s)
    toks = [name_key(t) for t in re.findall(r"[\w\-]+", request or "")]
    for i, t in enumerate(toks):
        kind = _KIND_WORDS.get(t)
        if i == 0 or (kind is None and t not in GLYPH_REF_WORDS):
            continue
        same = [s for s in syms if s.get("type") == kind] if kind is not None else _eq_matches(t, syms)
        if not same:
            continue
        prev = toks[i - 1]
        if prev in _SIZE_ADJ:
            area = lambda s: ed.fp(s)[2] * ed.fp(s)[3]
            pick = (max if _SIZE_ADJ[prev] == "max" else min)(same, key=area)
            if sum(1 for s in same if area(s) == area(pick)) == 1:
                put(pick)
        elif prev in _POS_ADJ:
            axis, how = _POS_ADJ[prev]
            pos = (lambda s: ed.fp(s)[0] + ed.fp(s)[2] / 2) if axis == "x" else (lambda s: ed.fp(s)[1] + ed.fp(s)[3] / 2)
            pick = (max if how == "max" else min)(same, key=pos)
            if sum(1 for s in same if pos(s) == pos(pick)) == 1:
                put(pick)
        elif prev == "the" and len(same) == 1:
            put(same[0])
    return found


def _compact_view(current: Dict[str, Any], g: "_Grounding", W: int, H: int, locked: set,
                  points: Optional[Dict[Any, Dict[str, Any]]] = None,
                  footprints: Optional[Dict[str, Tuple[int, int, int, int]]] = None,
                  values: Optional[Dict[str, str]] = None) -> str:
    """What the model sees of the current display (§6.3 item 2): per symbol its id, kind, label,
    bound tags BY NAME, the value text now, the box in percent, the footprint size, where it sits
    on screen and its order among its kind ("1st of 2 gauges, the larger")."""
    names: Dict[Any, str] = {p.get("id"): str(p.get("name")) for p in g.tags}
    for pid, p in (points or {}).items():
        if p.get("name"):
            names[pid] = str(p["name"])
    ctx = current.get("context") if isinstance(current.get("context"), dict) else None
    t = current.get("time") if isinstance(current.get("time"), dict) else {}
    head = f"CURRENT DISPLAY \"{current.get('name') or ''}\" (canvas {W} x {H} px"
    if ctx:
        head += f"; context: {ctx.get('default') or '-'}, template {ctx.get('template') or '-'}"
    if t.get("window"):
        head += f"; window {t['window']}"
    lines = [head + "):"]
    ed = _Editor(current.get("symbols") or [], W, H, GRID, footprints, values)
    leaves = ed.leaves()
    by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for s in leaves:
        by_kind.setdefault(_kind_noun(s), []).append(s)     # an equipment symbol by its glyph
    order: Dict[str, str] = {}
    for kind, ss in by_kind.items():
        if len(ss) < 2:
            continue
        lr = sorted(ss, key=lambda s: (ed.fp(s)[0] + ed.fp(s)[2] / 2, ed.fp(s)[1]))
        area = sorted(ss, key=lambda s: -(ed.fp(s)[2] * ed.fp(s)[3]))
        n = len(ss)
        for i, s in enumerate(lr):
            o = f"{_ordinal(i + 1)} of {n} {kind}s from the left"
            a = ed.fp(s)[2] * ed.fp(s)[3]
            if s is area[0] and a > ed.fp(area[1])[2] * ed.fp(area[1])[3]:
                o += ", the larger" if n == 2 else ", the largest"
            elif s is area[-1] and a < ed.fp(area[-2])[2] * ed.fp(area[-2])[3]:
                o += ", the smaller" if n == 2 else ", the smallest"
            order[str(s.get("id"))] = o

    def where(s: Dict[str, Any]) -> str:
        fx, fy, fw, fh = ed.fp(s)
        cx, cy = fx + fw / 2, fy + fh / 2
        hpos = "left" if cx < W / 3 else ("right" if cx > 2 * W / 3 else "center")
        vpos = "top" if cy < H / 3 else ("bottom" if cy > 2 * H / 3 else "middle")
        return f"{vpos}-{hpos}"

    def emit(syms: List[Any], depth: int) -> None:
        for s in syms:
            if not isinstance(s, dict):
                continue
            sid = str(s.get("id"))
            ax, ay, w, h = ed.abs_box(s)
            binds = ", ".join(_binding_name(b, names) for b in (s.get("bind") or []))
            parts = [f"{'  ' * depth}- {sid} {s.get('type')}"
                     + (f" {s.get('glyph')}" if s.get("type") == "equipment" else "")]
            if s.get("label"):
                parts.append(f"\"{s.get('label')}\"")
            if s.get("type") == "text" and s.get("text"):
                parts.append(f"text \"{_clean_text(s.get('text'), 80)}\"")
            if binds:
                parts.append(f"binds {binds}")
            if values and values.get(sid):
                parts.append(f"now {values[sid]}")
            parts.append(f"box x {_pct(ax, W)}% y {_pct(ay, H)}% w {_pct(w, W)}% h {_pct(h, H)}%")
            if s.get("type") != "group":
                fx, fy, fw, fh = ed.fp(s)
                parts.append(f"draws {fw}x{fh} px at {where(s)}")
            if sid in order:
                parts.append(order[sid])
            if s.get("locked") is True or sid in locked:
                parts.append("[locked: Describe cannot change it]")
            lines.append(" | ".join(parts))
            if s.get("type") == "group" and isinstance(s.get("children"), list):
                emit(s["children"], depth + 1)

    emit(current.get("symbols") or [], 0)
    if len(lines) == 1:
        lines.append("- (no symbols)")
    return "\n".join(lines)


def _center_text(s: Dict[str, Any]) -> bool:
    """A text / value / state being centered gets `style.align: center` when it has no alignment of
    its own (centering its box alone would leave left-aligned text off center). Returns whether."""
    if s.get("type") not in ("text", "value", "state"):
        return False
    st = s.get("style") if isinstance(s.get("style"), dict) else {}
    if st.get("align"):
        return False
    s["style"] = dict(st, align="center")
    return True


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _measured(footprints: Any) -> Dict[str, Tuple[int, int, int, int]]:
    """The page's measured footprints `{"<id>": [x, y, w, h]}` (canvas px); malformed entries are
    ignored (the engine then predicts that symbol's)."""
    out: Dict[str, Tuple[int, int, int, int]] = {}
    if not isinstance(footprints, dict):
        return out
    for k, v in footprints.items():
        if isinstance(v, (list, tuple)) and len(v) == 4:
            try:
                nums = [int((Fraction(str(x)) + Fraction(1, 2)) // 1) if not isinstance(x, bool) else None
                        for x in v]
            except (ValueError, ZeroDivisionError):
                continue
            if all(n is not None for n in nums) and nums[2] > 0 and nums[3] > 0:  # type: ignore[operator]
                out[str(k)] = (nums[0], nums[1], nums[2], nums[3])  # type: ignore[assignment]
    return out


# ── Settled at the D5.4 live battery: what the request itself says, when the model leaves it out ──

# A canvas region named in the request (corners first): "the top left corner", "the middle of the
# screen", "along the bottom", "the right side", "the left half", "the middle third".
_REGION_PHRASES: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(?:top|upper)[\s\-]+left\b", re.I), "top_left"),
    (re.compile(r"\b(?:top|upper)[\s\-]+right\b", re.I), "top_right"),
    (re.compile(r"\b(?:bottom|lower)[\s\-]+left\b", re.I), "bottom_left"),
    (re.compile(r"\b(?:bottom|lower)[\s\-]+right\b", re.I), "bottom_right"),
    (re.compile(r"\b(?:left|right|top|bottom|upper|lower)\s+half\b", re.I), "half"),
    (re.compile(r"\b(?:left|middle|center|centre|right|top|bottom|upper|lower)\s+third\b", re.I), "third"),
    (re.compile(r"\b(?:middle|center|centre)\s+of\s+(?:the\s+)?(?:screen|canvas|display|page|dashboard)\b"
                r"|\bin\s+the\s+(?:middle|center|centre)\b(?!\s+of\s+(?:the\s+)?(?!screen|canvas|display|page))"
                r"|\b(?:screen|canvas|display|page)'?s?\s+(?:middle|center|centre)\b|\bcenter\s+stage\b", re.I), "center"),
    (re.compile(r"\b(?:along|at|across|to)\s+the\s+top\b(?!\s+of\s+(?:the\s+)?(?!screen|canvas|display|page))"
                r"|\btop\s+(?:of|edge\s+of)\s+(?:the\s+)?(?:screen|canvas|display|page)\b", re.I), "top"),
    (re.compile(r"\b(?:along|at|across|to)\s+the\s+bottom\b(?!\s+of\s+(?:the\s+)?(?!screen|canvas|display|page))"
                r"|\bbottom\s+(?:of|edge\s+of)\s+(?:the\s+)?(?:screen|canvas|display|page)\b", re.I), "bottom"),
    (re.compile(r"\b(?:the\s+)?left\s+(?:side|edge)\b(?!\s+of\s+(?:the\s+)?(?!screen|canvas|display|page))", re.I), "left"),
    (re.compile(r"\b(?:the\s+)?right\s+(?:side|edge)\b(?!\s+of\s+(?:the\s+)?(?!screen|canvas|display|page))", re.I), "right"),
)


def request_region(request: str) -> Optional[str]:
    """The canvas region the request names, or None (the first match in the table's order)."""
    t = request or ""
    for rx, r in _REGION_PHRASES:
        m = rx.search(t)
        if not m:
            continue
        if r in ("half", "third"):
            return _region_name(m.group(0).replace(" ", "_"))
        return r
    return None


# A relation and what follows it: "centered under the trend", "to the right of the Temp Gauge",
# "between the two gauges", "inside the box", "above that table".
_RELATION_RE = re.compile(r"\b(below|under|underneath|beneath|above|right\s+of|left\s+of|next\s+to|beside|"
                          r"between|inside|within|in\s+the\s+middle\s+of|centered\s+on|center(?:ed)?\s+in)"
                          r"\s+((?:[\w\-.'’]+\s*){1,6})", re.I)
_REF_FILLER = frozenset("the that this a an it its two both of and with".split())
# "in its place" / "in place of X" / "where X was" / "instead of X" (§6.4 item 5 when the model
# forgets the `at`).
_IN_PLACE_RE = re.compile(r"\bin\s+(?:its|their|his|her|that\s+\w+'?s?)\s+place\b|\bin\s+place\s+of\b|"
                          r"\bwhere\s+(?:it|that|the\s+[\w\-]+(?:\s+[\w\-]+)?)\s+(?:was|used\s+to\s+be)\b|"
                          r"\binstead\s+of\b|\breplac(?:e|es|ed|ing)\b", re.I)
# A distance the user gave — only then does the model's `gap` count (else the 16 px default).
_GAP_ASK_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:px|pixels?|grid\s+steps?|steps?)\b|\bgap\b|\bapart\b|"
                         r"\b(?:touch|touching|flush|tight|no\s+space)\b", re.I)
# The display's own title / name — only then is a `retitle` a retitle (live battery E19: "change the
# text to Boiler Room" came back as a retitle).
_TITLE_ASK_RE = re.compile(r"\b(?:title|titled|rename|renamed|display'?s?\s+name|name\s+(?:of\s+)?(?:the\s+)?"
                           r"(?:display|page|screen|dashboard)|call\s+(?:the\s+)?(?:display|page|screen|"
                           r"dashboard)|(?:display|page|screen|dashboard)\s+(?:to|as)\b)", re.I)
# The font size the user said in words ("a large font", "make the font large", "big text").
_FONT_WORDS: Tuple[Tuple[str, str], ...] = (
    (r"extra[\s\-]*large|very\s+large|huge|giant|x[\s\-]*large", "xl"),
    (r"large|big|bigger|larger", "l"),
    (r"medium|normal|regular", "m"),
    (r"small|tiny|smaller", "s"),
)


_QUOTED_RE = re.compile(r"\b(?:says?|saying|reads?|reading|with\s+the\s+(?:text|words?)|text)\s*:?\s*"
                        r"[\"“'‘]([^\"”'’]{1,200})[\"”'’]", re.I)


# A text the request asks for with quoted words: 'a text box that says "X"', 'a title reading "X"',
# 'text "X"' (ruling, D5.4 live battery E36).
_ASKED_TEXT_RE = re.compile(
    r"(?:\b(?:text\s+box|text|label|title|caption|heading)\b[^\"“”.!?;]{0,60}?\b(?:says|say|reads|read|reading|"
    r"saying|showing|shows)\s*:?\s*|\btext\s+)[\"“]([^\"”]{1,200})[\"”]", re.I)


def asked_texts(request: str) -> List[str]:
    """Every quoted phrase the request asks a text to say, in order, once each."""
    out: List[str] = []
    for m in _ASKED_TEXT_RE.finditer(request or ""):
        w = _clean_text(m.group(1), 2000)
        if w and w not in out:
            out.append(w)
    return out


def _quoted_words(request: str) -> str:
    """The quoted words a text should say ('a text that says "Boiler 2"'), else ""."""
    m = _QUOTED_RE.search(request or "")
    return _clean_text(m.group(1), 2000) if m else ""


_PX_W_RE = re.compile(r"\b(\d{1,4})\s*(?:px|pixels?)\s+(?:wide|in\s+width|across)\b|"
                      r"\bwidth\s+(?:of\s+|to\s+)?(\d{1,4})\s*(?:px|pixels?)\b", re.I)
_PX_H_RE = re.compile(r"\b(\d{1,4})\s*(?:px|pixels?)\s+(?:tall|high|in\s+height)\b|"
                      r"\bheight\s+(?:of\s+|to\s+)?(\d{1,4})\s*(?:px|pixels?)\b", re.I)


def request_px(request: str) -> Tuple[Optional[int], Optional[int]]:
    """The width / height the user gave in px ("40 px wide", "a height of 200 px"), else None —
    it wins over the model's number for it (live battery E57: "40 px wide" came back as w 8)."""
    mw, mh = _PX_W_RE.search(request or ""), _PX_H_RE.search(request or "")
    w = int(mw.group(1) or mw.group(2)) if mw else None
    h = int(mh.group(1) or mh.group(2)) if mh else None
    return w, h


def request_font(request: str) -> Optional[str]:
    """The font size the request names in words for a font / text, else None."""
    t = request or ""
    for words, f in _FONT_WORDS:
        if re.search(r"\b(?:" + words + r")\s+(?:font|fonts|text|type|letters|lettering)\b", t, re.I) or \
                re.search(r"\b(?:font|fonts|text|letters)(?:\s+size)?(?:\s+(?:is|be|to|in|a|an|the|very|"
                          r"really|much|more))*\s+(?:" + words + r")\b", t, re.I):
            return f
    return None


# An align's reference in the request: "… with / to / against / as the bottom of the table".
_ALIGN_REF_RE = re.compile(r"\b(?:with|to|against|as)\s+((?:[\w\-.'’]+\s*){1,8})", re.I)
_ALIGN_EDGE_WORDS_RE = re.compile(r"\b(?:top|bottom|left|right|center|centre|middle|edges?|sides?|lines?|"
                                  r"of|its|their|same|level|height)\b", re.I)


def _infer_refs(ops: List[Dict[str, Any]], request: str, top: List[Any], nxt0: int,
                W: int, H: int) -> List[Dict[str, Any]]:
    """Settled at the D5.4 live battery: the references the model leaves out (or writes in a form
    the engine cannot use) are taken from the request itself, deterministically — never guessed:
    - a `place` with no usable `of`: a relation in the request naming a symbol ("centered under the
      trend" → the trend; a symbol added earlier in this batch is preferred for a kind word), then a
      canvas region the request names ("the top right corner" → region canvas:top_right, whatever
      `where` the model wrote), then `center_of` / `region` → the middle of the canvas;
    - `between` with fewer than two references: "between the two gauges" / "between A and B";
    - "in its place" / "replace" with exactly one removal: a place of an added symbol whose `of` is
      missing or the canvas becomes `at` the removed symbol, and an add that no op places gets that
      place appended (§6.4 item 5);
    - a `match` with no `to` and two or more ids: the largest of them (frame area; ties → the first);
    - a `retitle` when the request does not talk about the display's title or name, and there is
      exactly one text the request can mean: `set {text}` on that text (its label too, when the
      label was its text).
    Returns a new list; ids the model guessed for its adds are mapped as the engine maps them."""
    ops = [dict(o) for o in ops]
    syms = [s for s in _walk(top) if isinstance(s, dict) and s.get("type") != "group"]
    have = {str(s.get("id")): s for s in syms}
    # Ruling (D5.4 live battery E36): a text the request asks for with quoted words, that no op of
    # the batch adds (and no set puts on a text) and no text on the display already says, is added
    # by the engine — that text only, nothing else invented; its place comes from the relation in
    # its sentence (below), its font / color from the request's words for the font / text.
    def says(o: Dict[str, Any]) -> List[str]:
        out_w: List[str] = []
        sy = o.get("symbol") if isinstance(o.get("symbol"), dict) else {}
        pr = o.get("props") if isinstance(o.get("props"), dict) else {}
        spr = sy.get("props") if isinstance(sy.get("props"), dict) else {}
        for d in (sy, spr, pr, o):
            for key in ("text", "label", "title"):
                if isinstance(d.get(key), str) and d[key].strip():
                    out_w.append(_clean_text(d[key], 2000))
        return out_w

    present = {_clean_text(s.get("text"), 2000) for s in syms if s.get("type") == "text"}
    for words in asked_texts(request):
        if words in present:
            continue
        if any(o.get("op") == "add" and (o.get("symbol") or {}).get("kind") == "text" and words in says(o)
               for o in ops):
            continue
        if any(o.get("op") == "set" and words in says(o) for o in ops):
            continue
        ops.append({"op": "add", "from_request": True,
                    "symbol": {"kind": "text", "label": words, "bind": [],
                               "x": _Num("45"), "y": _Num("45"), "w": _Num("10"), "h": _Num("10")}})
    # The batch's adds in order: real id s<nxt0 + k>, kind, label / text.
    adds: List[Dict[str, Any]] = []
    guess: Dict[str, str] = {}

    def resolve(i: Any, upto: int) -> Optional[str]:
        """An id as the engine will read it at op `upto`: on the display, a real add id, or a guessed
        one mapped to the adds in order (unknown_id_errors / `real` semantics)."""
        i = str(i).strip()
        if i in have or any(a["id"] == i for a in adds if a["at"] < upto):
            return i
        if i in guess:
            return guess[i]
        free = [a["id"] for a in adds if a["at"] < upto and a["id"] not in guess.values()]
        if free:
            guess[i] = free[0]
            return free[0]
        return None

    for k, o in enumerate(ops):
        if o.get("op") == "add" and isinstance(o.get("symbol"), dict):
            sy = o["symbol"]
            ak = str(sy.get("kind") or "")
            if ak == "equipment":                      # D3.4: an equipment add by its glyph
                ak = f"equipment:{glyph_of(sy.get('glyph'))}"
            adds.append({"id": f"s{nxt0 + len(adds)}", "at": k, "type": ak,
                         "label": str(sy.get("label") or (sy.get("props") or {}).get("label") or
                                      sy.get("text") or (sy.get("props") or {}).get("text") or "")})
        for i in _op_ids(o) + [r for key in ("of", "to", "a", "b") for r in _ref_list(o.get(key))]:
            if not _is_canvas_ref(i):
                resolve(i, k)

    def candidates(upto: int) -> List[Tuple[str, str, str, bool]]:
        """(id, kind, label words, added in this batch) of what an op at `upto` can refer to."""
        out = [(sid, _kkey(s), f"{s.get('label') or ''} {s.get('text') or ''}", False)
               for sid, s in have.items()]
        out += [(a["id"], a["type"], a["label"], True) for a in adds if a["at"] < upto]
        return out

    def phrase_target(words: str, upto: int, exclude: str) -> Optional[str]:
        """The one symbol the words after a relation name: a whole label, else a kind word (an
        add of this batch first, else the only one of that kind)."""
        toks = [name_key(w) for w in re.findall(r"[\w\-.'’]+", words)]
        toks = [t for t in toks if t not in _REF_FILLER][:4]
        cands = [c for c in candidates(upto) if c[0] != exclude]
        for c in cands:
            lw = _label_words(c[2])
            if lw and all(w in toks for w in lw[:3]):
                return c[0]
        for t in toks[:2]:
            kind = next((kd for kd, refs in _KIND_REFS.items() if t in refs and t != "font"), None)
            if kind is None and t not in GLYPH_REF_WORDS:
                continue
            same = [c for c in cands if (c[1] == kind if kind is not None else _key_in_glyphs(c[1], t))]
            new = [c for c in same if c[3]]
            if new:
                return new[-1][0]
            if len(same) == 1:
                return same[0][0]
        return None

    rels = list(_RELATION_RE.finditer(request or ""))
    region = request_region(request)
    removed = [str(i).strip() for o in ops if o.get("op") == "remove" for i in _op_ids(o)]
    in_place = bool(_IN_PLACE_RE.search(request or "")) and len(set(removed)) == 1
    rm_at = next((k for k, o in enumerate(ops) if o.get("op") == "remove"), -1)
    # the symbol that takes the removed one's place: the first add after the removal (before it,
    # when the model added first)
    taker = next((a["id"] for a in adds if a["at"] > rm_at), adds[0]["id"] if adds else None) \
        if in_place else None
    out: List[Dict[str, Any]] = []
    for k, o in enumerate(ops):
        kind = o.get("op")
        ids = [resolve(i, k) or str(i).strip() for i in _op_ids(o)]
        me = ids[0] if ids else ""
        if kind == "place":
            where = o.get("where")
            refs = _ref_list(o.get("of"))
            usable = bool(refs) and not (where == "region" and not str(refs[0]).startswith("canvas:"))
            if in_place and me and me == taker and not (where == "at" and refs == [removed[0]]):
                o["where"], o["of"] = "at", removed[0]
                o.pop("align", None)
            elif where == "between" and len([r for r in refs if not _is_canvas_ref(r)]) < 2:
                pair = _between_pair(request, candidates(k), me)
                if pair:
                    o["of"] = pair
            elif not usable:
                got = None
                if where not in ("region", "center_of"):
                    for m in rels:
                        got = phrase_target(m.group(2), k, me)
                        if got:
                            break
                if got:
                    o["of"] = got
                elif region:
                    o["where"], o["of"] = "region", "canvas:" + region
                elif where in ("region", "center_of"):
                    o["where"], o["of"] = "region", "canvas:center"
        elif kind == "align" and (not _has_ref(o.get("to")) or str(o.get("to")).strip().lower() == "canvas"):
            # Settled at the D5.5 live battery (E10, "align the bottom of the bar with the bottom of
            # the table" came back as align [bar, table] against the canvas): the symbol the request
            # names after with / to / against / as is the reference — `to` — and not a subject; the
            # canvas only when the request names the screen / canvas / page, or no reference.
            got = None
            for m in reversed(list(_ALIGN_REF_RE.finditer(request or ""))):   # the reference comes last
                words = re.split(r"\b(?:with|to|against|as|and|but|so|then)\b|[.,;!?]", m.group(1), flags=re.I)[0]
                words = _ALIGN_EDGE_WORDS_RE.sub(" ", words)
                if re.match(r"\s*(?:the\s+)?(?:screen|canvas|page|display|window)\b", words, re.I):
                    got = None
                    break
                got = phrase_target(words, k, "")
                if got:
                    break
            if got:
                o["to"] = got
                rest = [i for i in ids if i != got]
                if rest:
                    o["ids"] = rest
                    o.pop("id", None)
        elif kind == "match" and not _has_ref(o.get("to")) and len(ids) >= 2:
            areas = []
            for i in ids:
                s = have.get(i)
                areas.append(_int0(s.get("w")) * _int0(s.get("h")) if s else 0)
            o["to"] = ids[areas.index(max(areas))]
        elif kind == "retitle" and not _TITLE_ASK_RE.search(request or "") \
                and re.search(r"\b(?:text|label|caption|words?|says?)\b", request or "", re.I):
            texts = [s for s in syms if s.get("type") == "text"]
            if len(texts) == 1 and isinstance(o.get("title"), str):
                t = texts[0]
                props: Dict[str, Any] = {"text": o["title"]}
                if str(t.get("label") or "") == str(t.get("text") or ""):
                    props["label"] = o["title"]
                o = {"op": "set", "ids": [str(t.get("id"))], "props": props}
        out.append(o)
    # An add no op places, when a sentence of the request that names its kind also states a
    # relation to another symbol ("Put a text box centered below that trend …" — live battery E36:
    # the model added the text but left out its place): that place is appended after the add.
    _WHERE_OF = {"below": "below", "under": "below", "underneath": "below", "beneath": "below",
                 "above": "above", "right of": "right_of", "left of": "left_of"}
    placed_ids = {resolve(i, len(ops)) for o in out if o.get("op") in ("place", "center", "align", "move")
                  for i in _op_ids(o)}
    inserts: List[Tuple[int, Dict[str, Any]]] = []
    for a in adds:
        if a["id"] in placed_ids or a["id"] == taker:
            continue
        refs_words = (_glyph_words_of({"type": "equipment", "glyph": a["type"][10:]})
                      if a["type"].startswith("equipment:")
                      else _KIND_REFS.get(a["type"], frozenset((a["type"],))) - {"font"})
        for sent in _SENTENCE_RE.split(request or ""):
            words = {name_key(w) for w in re.findall(r"[\w\-.'’]+", sent)}
            if not words & refs_words:
                continue
            m = next((m for m in _RELATION_RE.finditer(sent)
                      if re.sub(r"\s+", " ", m.group(1).lower()) in _WHERE_OF), None)
            if m is None:
                continue
            target = phrase_target(m.group(2), a["at"] + 1, a["id"])
            if target is None or target == a["id"]:
                continue
            al = "start" if re.search(r"\b(?:left|top)[\s\-]+aligned\b", sent, re.I) else \
                ("end" if re.search(r"\b(?:right|bottom)[\s\-]+aligned\b", sent, re.I) else "center")
            inserts.append((a["at"] + 1, {"op": "place", "id": a["id"],
                                          "where": _WHERE_OF[re.sub(r"\s+", " ", m.group(1).lower())],
                                          "of": target, "align": al}))
            break
    if taker is not None and not any(o.get("op") == "place" and resolve(_op_ids(o)[0] if _op_ids(o) else "", len(ops))
                                     == taker for o in out):
        k_add = next(a["at"] for a in adds if a["id"] == taker)
        out.insert(min(len(out), max(k_add, rm_at) + 1),
                   {"op": "place", "id": taker, "where": "at", "of": removed[0]})
    # the inferred relations go LAST: their references are placed (and sized) by then
    for _at, o in inserts:
        out.append(o)
    return out


def a_id_in(o: Dict[str, Any], sid: str, resolve: Callable[[Any, int], Optional[str]], upto: int) -> bool:
    return any(resolve(i, upto) == sid for i in _op_ids(o))


def _label_words(text: str) -> List[str]:
    """A label's words for matching it in the request — every word, short ones too ("T", "G1")."""
    return [name_key(w.strip(".-_'’")) for w in re.findall(r"[\w\-.'’]+", text or "") if w.strip(".-_'’")]


def _between_pair(request: str, cands: List[Tuple[str, str, str, bool]], exclude: str) -> Optional[List[str]]:
    """The two symbols "between …" names: two labels ("between A and B"), or a kind with exactly
    two of it ("between the two gauges", "between the gauges")."""
    m = re.search(r"\bbetween\s+(.+?)(?:[.,;!?]|$)", request or "", re.I)
    if not m:
        return None
    words = m.group(1)
    toks = {name_key(w) for w in re.findall(r"[\w\-.'’]+", words)}
    cands = [c for c in cands if c[0] != exclude]
    by_label = [c[0] for c in cands if _label_words(c[2]) and all(w in toks for w in _label_words(c[2])[:3])]
    if len(by_label) == 2:
        return by_label
    for kind, refs in _KIND_REFS.items():
        if toks & refs:
            same = [c[0] for c in cands if c[1] == kind]
            if len(same) == 2:
                return same
    for w in sorted(toks & set(GLYPH_REF_WORDS)):      # D3.4: "between the two pumps"
        same = [c[0] for c in cands if _key_in_glyphs(c[1], w)]
        if len(same) == 2:
            return same
    return None


def _key_in_glyphs(kkey: str, word: str) -> bool:
    """Whether a kind key ("equipment:pump") is a glyph the reference word names ("pump", "valves")."""
    return kkey.startswith("equipment:") and kkey[10:] in GLYPH_REF_WORDS.get(word, frozenset())


def _hoist_removals(ops: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`place {at: R}` written BEFORE the `remove` of R in the same batch ("remove C and put B in
    its place", in either order) means R's place once R is gone (§6.4 item 5): R's removal moves to
    just before the first such place (split out of its remove op), so the removal really happens
    — or is really refused — before the place uses it (§6.4 item 4). The implicit `at` target (no
    `of`, exactly one symbol removed in the batch) counts too. Returns a new list."""
    out = [dict(o) for o in ops]
    removed = [str(i).strip() for o in out if o.get("op") == "remove" for i in _op_ids(o)]
    i = 0
    while i < len(out):
        o = out[i]
        if o.get("op") == "place" and o.get("where") == "at":
            r = o.get("of") if isinstance(o.get("of"), str) and o.get("of").strip() else \
                (removed[0] if len(set(removed)) == 1 else None)
            r = str(r).strip() if r else None
            j = next((k for k in range(i + 1, len(out)) if out[k].get("op") == "remove"
                      and r in [str(x).strip() for x in _op_ids(out[k])]), None) if r else None
            if j is not None:
                rest = [x for x in _op_ids(out[j]) if str(x).strip() != r]
                if rest:
                    out[j] = {k: v for k, v in out[j].items() if k not in ("id", "ids")}
                    out[j]["ids"] = rest
                else:
                    out.pop(j)
                out.insert(i, {"op": "remove", "ids": [r]})
                i += 1
        i += 1
    return out


_MOVED_PREFIXES = ("moved", "centered", "put")
_RELATION_TAIL_RE = re.compile(r"^(?:below|above|left of|right of|between|inside|to the|on )")


def _final_notes(notes: List[str], n0: int, refused: List[Dict[str, Any]],
                 refusal_of: List[Tuple[str, Dict[str, Any]]], ed: "_Editor",
                 nm: Callable[[Dict[str, Any]], str]) -> None:
    """§6.6 item 3: the notes of a batch (`notes[n0:]`) describe its FINAL state, once per symbol
    per outcome. Applied to the notes in place (and to `refused`):
    - "did not move X …" is dropped (with its refusal) when X did move in the end — its goal was
      achieved by a later step (a make_room refused early, then a relation moved it clear);
    - "moved / centered / put X …" is dropped when X ended where it started;
    - a relation line for X is dropped when "could not keep X …" says the relation did not hold;
    - of the remaining moved-type lines about X, one is kept: the last relation line, else the
      last one ("moved X out of the way of Y" and "moved X left of Y" are one outcome)."""
    names: List[Tuple[str, str]] = []
    for s in ed._all():
        names.append((nm(s), str(s.get("id"))))
    for sid, gh in ed.ghosts.items():
        names.append((str(gh.get("name") or ""), sid))
    names = sorted({x for x in names if x[0]}, key=lambda t: -len(t[0]))

    def about(note: str, prefixes: Sequence[str]) -> Optional[Tuple[str, str]]:
        """(the rest after the name, the symbol id) when `note` starts with a prefix + a name."""
        for p in prefixes:
            if not note.startswith(p + " "):
                continue
            rest = note[len(p) + 1:]
            for name, sid in names:
                if rest.startswith(name) and (len(rest) == len(name) or rest[len(name)] in " :,)—"):
                    return rest[len(name):].strip(), sid
        return None

    def final_moved(sid: str) -> bool:
        s = next((x for x in ed._all() if str(x.get("id")) == sid), None)
        o = ed.orig.get(sid)
        return s is None or o is None or ed.abs_box(s)[:2] != o[:2]

    def unchanged(sid: str) -> bool:
        s = next((x for x in ed._all() if str(x.get("id")) == sid), None)
        o = ed.orig.get(sid)
        return s is not None and o is not None and ed.abs_box(s) == o

    drop: set = set()
    not_kept = {a[1] for a in (about(n, ("could not keep",)) for n in notes[n0:]) if a}
    moved_at: Dict[str, List[Tuple[int, bool]]] = {}
    for i in range(n0, len(notes)):
        a = about(notes[i], ("did not move",))
        if a is not None and final_moved(a[1]):
            drop.add(i)
            continue
        b = about(notes[i], _MOVED_PREFIXES)
        if b is not None:
            relation = bool(_RELATION_TAIL_RE.match(b[0])) and "out of the way" not in b[0]
            moved_at.setdefault(b[1], []).append((i, relation))
    for sid, idxs in moved_at.items():
        if unchanged(sid):
            drop.update(i for i, _r in idxs)
            continue
        if sid in not_kept:
            drop.update(i for i, r in idxs if r)
            idxs = [(i, r) for i, r in idxs if not r]
        if len(idxs) > 1:
            rel = [i for i, r in idxs if r]
            keep = rel[-1] if rel else idxs[-1][0]
            drop.update(i for i, _r in idxs if i != keep)
    # No note may hold a bare symbol id (§6.4 item 7; corpus C098): an id-like token naming a
    # symbol becomes its name, any other becomes "that symbol".
    by_id = {sid: name for name, sid in names}
    for i in range(n0, len(notes)):
        notes[i] = _scrub_ids(notes[i], by_id)
    for k, (rn, entry) in enumerate(refusal_of):
        refusal_of[k] = (_scrub_ids(rn, by_id), entry)
    if not drop:
        return
    for i in sorted(drop, reverse=True):
        note = notes.pop(i)
        for k, (rn, entry) in enumerate(refusal_of):
            if rn == note and any(entry is e for e in refused):
                refused[:] = [e for e in refused if e is not entry]
                del refusal_of[k]
                break


def _apply_ops(obj: Dict[str, Any], current: Dict[str, Any], g: _Grounding, request: str,
               locked: set, W: int, H: int, grid: int, notes: List[str],
               lift: bool = False, footprints: Any = None, values: Optional[Dict[str, str]] = None,
               points: Optional[Dict[Any, Dict[str, Any]]] = None,
               images: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Follow-up ops (design_displays §6.3 + §6.4): the model states intent by id; the engine
    computes every position on the grid, keeps footprints apart (unless the request asks for an
    overlap), refuses what does not fit or is locked or is ambiguous — with a reason — and writes
    one user-facing note per applied op. Locked symbols (`locked` ids or `locked: true`) are never
    changed and always obstacles; an op that depends on a refused removal is refused too; a place
    `at` a removed symbol takes its frame; a relation to a symbol this request created may shrink
    that symbol on the facing side; notes never name a symbol by its id."""
    doc = copy.deepcopy(current)
    n0 = len(notes)                                    # the notes this batch writes start here
    top: List[Any] = doc.setdefault("symbols", [])
    ctx = doc.get("context") if isinstance(doc.get("context"), dict) else None
    ctx_el = g.element_by_path_key(ctx["default"]) if ctx and isinstance(ctx.get("default"), str) else None
    pts: Dict[Any, Dict[str, Any]] = {p.get("id"): p for p in g.tags if p.get("id") is not None}
    pts.update(points or {})
    res = _Resolver(g, ctx_el, known_points=pts)
    nxt = max(1, _as_int(doc.get("next_symbol_id")) or 1, _max_id(top) + 1)
    ed = _Editor(top, W, H, grid, _measured(footprints), values)
    ed.is_locked = lambda s: str(s.get("id")) in locked or s.get("locked") is True
    # §6.6 item 1: the no-overlap rule is lifted only for the symbols of a clause that asks for
    # overlap positively (a negated or unclear ask grants nothing), and between a symbol placed
    # `inside` / `center_of` another and that symbol; the shrink permission needs a positive clause.
    permitted = bool(positive_clauses(request or "", _PERMIT_RE))
    overlap_clauses = positive_clauses(request or "", _OVERLAP_ASK_RE)
    overlap_refs = _ClauseRefs(request or "")
    ok_pairs: set = set()                              # inside / center_of: containment, not overlap
    op_pairs: set = set()                              # {subject, reference} of this batch's placing ops

    def may_overlap(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
        """§6.6 item 1 (settled after Fable 5.1's review): the permission is the unordered pair
        {subject, reference} of the op the positive phrase describes — both named by the phrase's
        clause, and either an op of this batch places one against the other, or one of them is a
        symbol this batch adds (an add's subject is the added symbol)."""
        ia, ib = str(a.get("id")), str(b.get("id"))
        if (ia, ib) in ok_pairs or (ib, ia) in ok_pairs:
            return True
        if not overlap_clauses:
            return False
        if not ((ia, ib) in op_pairs or (ib, ia) in op_pairs or ia in added_order or ib in added_order):
            return False
        pool = ed.leaves()
        return any(overlap_refs.refers(i, a, pool, True) and overlap_refs.refers(i, b, pool, True)
                   for i in overlap_clauses)

    ed.overlap_ok = may_overlap
    user_w, user_h = _user_numbers(request or "", W, H)[2:]

    def frames_now(skip: Optional[Dict[str, Any]] = None) -> List[Tuple[Dict[str, Any], Tuple[int, int, int, int]]]:
        return [(o, ed.abs_box(o)) for o in ed.leaves() if o is not skip]

    def existing_target(s: Dict[str, Any], op_target: bool = False) -> Optional[Tuple[int, int, str]]:
        """§6.6 item 2 for an EXISTING symbol, from what the request says about its size: only a
        readability phrase → its kind default where it is smaller (each side), else its size as it
        is ("readable"); a size word → its §6.3 / §6.5 meaning on its current size (bigger 1.5 ×,
        smaller 1 / 1.5, twice 2 ×, half 0.5 ×, full width / height = the canvas minus the margin),
        never below its floor ("word"); a comparison → computed from the real symbol ("compare");
        None = an explicit number or nothing said (the op applies as before)."""
        r = size_request(s, request or "", W, H, grid, frames_now(s), existing=True, op_target=op_target)
        cw, ch = _int0(s.get("w")), _int0(s.get("h"))
        if r is None:
            # the request says nothing about this symbol's size: a model resize / scale / set of w, h
            # / match on it is dropped silently (live battery E4: "put the larger gauge in its place"
            # came with a resize nobody asked for)
            return cw, ch, "drop"
        if r["how"] in ("explicit", "verb"):
            return None
        if r["how"] == "vague":
            dw, dh = default_size(s, W, H, grid)
            return max(cw, dw), max(ch, dh), "readable"
        if r["how"] == "compare":
            return (r["w"] if r["w"] is not None else cw), (r["h"] if r["h"] is not None else ch), "compare"
        minw, minh = ed.floor(s)
        top_w, top_h = (W - 4 * grid) // grid * grid, (H - 4 * grid) // grid * grid
        w = r["w"] if r["full_w"] else min(max(cw, top_w), max(minw, _snap(int(cw * r["fx"]), grid)))
        h = r["h"] if r["full_h"] else min(max(ch, top_h), max(minh, _snap(int(ch * r["fy"]), grid)))
        return w, h, "smaller" if (r["fx"] < 1 or r["fy"] < 1) else "word"

    def size_override(s: Dict[str, Any], want_w: Optional[int], want_h: Optional[int]) -> Optional[Tuple[int, int, str]]:
        """§6.6 item 2 over the model's resize / scale / set of w, h: a symbol this request added
        keeps the size its add gave it, the model's number counting only where it equals one the
        user wrote ("added"); an existing symbol follows `existing_target`; None = the op applies
        as before."""
        cw, ch = _int0(s.get("w")), _int0(s.get("h"))
        if str(s.get("id")) in added_order:
            w = want_w if want_w is not None and any(abs(want_w - a) < grid for a in user_w) else cw
            h = want_h if want_h is not None and any(abs(want_h - a) < grid for a in user_h) else ch
            return w, h, "added"
        return existing_target(s, op_target=True)
    ops: List[Dict[str, Any]] = _hoist_removals(_infer_refs(list(obj["ops"]), request, top, nxt, W, H))
    if not ops and _CLEAR_ONLY_RE.match(request or ""):
        # A request that only clears the display means `clear`, whatever the model returned
        # (live battery E21: the model once answered "clear the display" with no ops).
        ops = [{"op": "clear"}]
    placeholders: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []
    range_missing: List[str] = []
    added: List[Dict[str, Any]] = []
    applied = 0
    # §6.4 item 4: `ed.removing` / `ed.ghosts` grow only as removals actually happen; the ids
    # whose removal was refused in this batch → the name the user knows them by.
    refused_removal: Dict[str, str] = {}
    created: set = set()                               # ids added or placed `at` in this request
    asked_big: Dict[str, Tuple[int, int]] = {}         # added ids the request asked bigger → that size
    placed_at: Dict[str, str] = {}                     # id → the removed symbol whose frame it took
    decimals_asked = bool(_DECIMALS_ASK_RE.search(request or ""))
    explicit_size = bool(_EXPLICIT_SIZE_RE.search(request or ""))

    def nm(s: Dict[str, Any]) -> str:
        return _sym_name(s, ed.names)

    def is_locked(s: Dict[str, Any]) -> bool:
        return str(s.get("id")) in locked or s.get("locked") is True

    def props_of(p: Any, sym: Dict[str, Any], targeted: bool = True) -> Dict[str, Any]:
        """The model's props for `sym`, with what the user did not ask for on it dropped silently
        (§6.4 item 7, Settled at the D5.3 live battery): `decimals` unless the request mentions
        decimals; `font` / `align` on a kind the viewer does not draw text with them; a color
        (`color` / `fill` / `stroke`) unless a sentence of the request with a color word refers to
        this symbol (or refers to nothing at all — "make it green"). The legacy `restyle` op
        (`targeted=False`, not offered to the model since D5.2) keeps only the decimals rule."""
        out = normalize_props(p) if isinstance(p, dict) else {}
        drop = set()
        if not decimals_asked:
            drop.add("decimals")
        t = str(sym.get("type") or "")
        if targeted and t not in _FONT_KINDS:
            drop.add("font")
        if targeted and t not in _ALIGN_KINDS:
            drop.add("align")
        if targeted and not _color_meant_for(request, sym):
            drop |= {"color", "fill", "stroke"}
        for k in drop:
            out.pop(k, None)
        if isinstance(out.get("style"), dict):
            out["style"] = {k: v for k, v in out["style"].items() if k not in drop}
        # Settled at the D5.4 live battery: a font size the user said in words wins over the
        # model's reading of it ("a large font" = l; the model wrote xl).
        said = request_font(request)
        if said and targeted:
            if "font" in out:
                out["font"] = said
            if isinstance(out.get("style"), dict) and "font" in out["style"]:
                out["style"]["font"] = said
        return out

    ref_bad, ref_notes = _reference_problems(request, top, ops, ed.names)
    pos_bad, pos_notes = _position_problems(request, top, ops, {str(s.get("id")): ed.abs_box(s) for s in ed.leaves()},
                                            W, H, ed.names)
    for k, v in pos_bad.items():
        ref_bad.setdefault(k, v)
    ref_notes.extend(n for n in pos_notes if n not in ref_notes)
    notes.extend(ref_notes)
    removed_ids = [i for o in ops if o.get("op") == "remove" for i in _op_ids(o)]
    before_all = copy.deepcopy(top)
    # §6.6 item 2: a readability phrase about an existing symbol smaller than its kind's default
    # grows it to the default even when the model gave no op for it (one engine op, at the end).
    sized_by_op = {str(i).strip() for o in ops if o.get("op") in ("resize", "scale", "match", "remove", "readable")
                   for i in _op_ids(o)}
    sized_by_op |= {str(i).strip() for o in ops if o.get("op") == "set" and isinstance(o.get("props"), dict)
                    and {"w", "h"} & set(normalize_props(o["props"])) for i in _op_ids(o)}
    for s0 in ed.leaves():
        sid0 = str(s0.get("id"))
        if sid0 in sized_by_op or is_locked(s0):
            continue
        t0 = existing_target(s0)
        if t0 is not None and t0[2] == "readable" and (t0[0], t0[1]) != (_int0(s0.get("w")), _int0(s0.get("h"))):
            ops.append({"op": "readable", "ids": [sid0]})

    def target_of(op: Dict[str, Any], key: str) -> Any:
        """The `of` / `to` of a place / align / match (a string, or a list of ids — D5.4); when the
        model left it out (battery fix 2): a place `at` in a batch that removes exactly one symbol
        → that symbol; else the ONE symbol the request refers to, the op's own targets excluded;
        else None (refused, explained)."""
        v = op.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, list) and _ref_list(v):
            return _ref_list(v) if len(_ref_list(v)) > 1 else _ref_list(v)[0]
        if op.get("op") == "place" and op.get("where") == "at" and len(set(removed_ids)) == 1:
            return removed_ids[0]
        cands = request_references(request, top, ed, _op_ids(op) + [real(i) for i in _op_ids(op)])
        if len(cands) == 1:
            return str(cands[0].get("id"))
        return None

    added_order: List[str] = []                        # real ids of this batch's adds, in order
    alias: Dict[str, str] = {}                         # an id the model guessed → the add's real id

    def real(sid: str) -> str:
        """Within one batch, an id the display does not hold, after one or more adds, refers to the
        added symbols in order: the first such id → the first add's real id, and so on (ruling,
        D5.2 final battery). An id that matches nothing and cannot be mapped is left as is."""
        sid = str(sid).strip()
        if _is_canvas_ref(sid) or sid in ed.ghosts or _find(sid) is not None:
            return sid
        if sid in alias:
            return alias[sid]
        free = [a for a in added_order if a not in alias.values()]
        if free:
            alias[sid] = free[0]
            return free[0]
        return sid

    def _find(sid: str) -> Optional[Dict[str, Any]]:
        return next((s for s in ed._all() if str(s.get("id")) == str(sid)), None)

    def find(sid: str) -> Optional[Dict[str, Any]]:
        sid = real(sid)
        return next((s for s in ed._all() if str(s.get("id")) == str(sid)), None)

    # D3.6 (§5.7.10 item 9): this batch's pipes — the model's connect ops and the request's own
    # connect phrases; each new pipe goes to the bottom of the stacking order (in the batch's order)
    pipe_slot = [0]

    def pipe_id() -> str:
        nonlocal nxt
        nxt += 1
        return f"s{nxt - 1}"

    def pipe_ref(r: str) -> Optional[List[Dict[str, Any]]]:
        s0 = find(r)
        if s0 is not None:
            return [s0]
        return [] if re.fullmatch(r"\s*s\d+\s*", r or "") else None

    def put_pipe(pipe: Dict[str, Any]) -> None:
        top.insert(pipe_slot[0], pipe)
        pipe_slot[0] += 1
        ed.names[str(pipe["id"])] = "the new pipe"

    conn = display_pipes.Connector(request or "", symbols=ed.leaves, new_id=pipe_id, resolve=pipe_ref,
                                   name_of=lambda s: ed.names.get(str(s.get("id"))), box_of=ed.abs_box,
                                   bind_name=res.resolve)

    unexplained: List[str] = []                        # refusal reasons no note of their own says

    refusal_of: List[Tuple[str, Dict[str, Any]]] = []  # (note, refused entry) for §6.6 item 3

    def refuse(kind: str, sid: str, reason: str, note: Optional[str] = None) -> None:
        refused.append({"op": kind, "symbol": sid, "reason": reason})
        if note:
            notes.append(note)
            refusal_of.append((note, refused[-1]))
        elif reason not in unexplained and reason not in ref_bad.values():   # ref_notes say those
            unexplained.append(reason)

    def unknown_note() -> str:
        return f"I could not find a symbol that step names — the display has: {_holdings(top, names=ed.names)}"

    def lock_note(s: Dict[str, Any]) -> str:
        return (f"{nm(s)} is locked — unlock it (Ctrl+L, or the lock box in its properties) to let "
                "Describe change it")

    def readable_line(s: Dict[str, Any]) -> str:
        return f"made {nm(s)} bigger, to the usual size for {_a_kind(s)} ({s['w']} x {s['h']} px)"

    def size_stays(kind: str, s: Dict[str, Any], how: str = "readable") -> None:
        """§6.6 item 2: the size the request gives is the size `s` has — a readability phrase about a
        symbol already at least its kind's default, a "smaller" at its floor, a "bigger" at the
        canvas (said once per symbol); a symbol this request added says nothing."""
        sid = str(s.get("id"))
        if how == "readable":
            reason, note = "already readable", (f"left {nm(s)} at its size — it is already at least the "
                                                f"usual size for {_a_kind(s)}")
        elif how == "smaller":
            reason, note = "at its floor", f"{nm(s)} stops at its smallest size, {s.get('w')} x {s.get('h')} px"
        elif how == "word":
            reason, note = "at the canvas", f"{nm(s)} is already as large as the canvas allows"
        else:
            return
        if not any(r.get("symbol") == sid and r.get("reason") == reason for r in refused):
            refuse(kind, sid, reason, note)

    def targets(kind: str, ids: List[str]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for sid in [real(i) for i in ids]:
            s = find(sid)
            if s is None:
                refuse(kind, sid, "unknown", unknown_note())
            elif sid in ref_bad:
                refuse(kind, sid, ref_bad[sid])
            elif is_locked(s):                         # §6.4 item 3: whatever the request says
                refuse(kind, sid, "locked", lock_note(s))
            elif display_pipes.is_pipe(s) and kind in display_pipes.GEOMETRY_OPS:
                # D3.6 (§5.7.10 item 3): a pipe follows its equipment
                refuse(kind, sid, "a pipe follows its equipment", display_pipes.geometry_refusal(nm(s)))
            else:
                out.append(s)
        return out

    def ref_box(kind: str, rid: Any) -> Optional[Tuple[Tuple[int, int, int, int], Tuple[int, int, int, int], str]]:
        """(frame, footprint, name) of the symbol an op places against (a removed one too); D5.4:
        a list of ids → their union box, "canvas:<region>" → that region."""
        if isinstance(rid, list):
            parts = [ref_box(kind, r) for r in rid]
            if not parts or any(p is None for p in parts):
                return None
            def uni(bs: List[Tuple[int, int, int, int]]) -> Tuple[int, int, int, int]:
                x0, y0 = min(b[0] for b in bs), min(b[1] for b in bs)
                return x0, y0, max(b[0] + b[2] for b in bs) - x0, max(b[1] + b[3] for b in bs) - y0
            return (uni([p[0] for p in parts]), uni([p[1] for p in parts]),  # type: ignore[index]
                    name_of([real(r) for r in rid]))
        rid = real(rid)
        if rid == "canvas":
            return (0, 0, W, H), (0, 0, W, H), "the canvas"
        if rid.startswith("canvas:"):
            if rid[7:] not in REGIONS:
                refuse(kind, rid, "unknown", f"there is no canvas region '{rid[7:]}' — the regions are: "
                       + ", ".join(region_words(r) for r in REGIONS))
                return None
            b = region_box(rid[7:])
            return b, b, f"the {region_words(rid[7:])} of the canvas"
        if rid in ref_bad:
            refuse(kind, rid, ref_bad[rid])
            return None
        s = find(rid)
        if s is not None:
            return ed.abs_box(s), ed.fp(s), nm(s)
        gh = ed.ghosts.get(rid)
        if gh is not None:
            return gh["box"], gh["fp"], gh["name"]
        refuse(kind, rid, "unknown", unknown_note())
        return None

    def shrinkable(op: Dict[str, Any], exclude: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
        """Symbols the request permits shrinking for this op: the model's `may_shrink` (only when
        the request says so), else every unlocked symbol when it permits in general. Locked
        symbols never shrink (§6.4 item 3)."""
        if not permitted:
            return []
        out: List[Dict[str, Any]] = []
        named = [find(i) for i in (op.get("may_shrink") or []) if isinstance(i, str)]
        pool = [s for s in named if s is not None] or ed.leaves()
        for s in pool:
            if any(s is x for x in exclude) or is_locked(s):
                continue
            out.append(s)
        return out

    # ── D5.4: placement as per-axis constraints, kept through the batch (§6.5 items 1-2) ──
    # id → {"x": c, "y": c}; a constraint c = {"axis", "mode": after | before | start | center |
    # end, "refs": [reference parts] | None, "iv": a fixed canvas interval | None, "gap",
    # "between", "on": "frame" | "fp" (which box of the subject is placed), "desc": words}.
    cons: Dict[str, Dict[str, Dict[str, Any]]] = {}
    pinned: set = set()                                # ids this request positioned explicitly
    resized: set = set()                               # ids this request resized
    made_way: Dict[str, str] = {}                      # id → the note that moved it out of the way
    room_asked: List[Tuple[str, str]] = []             # make_room subjects (id, from-name), said at the end
    fitted: set = set()                                # added texts whose box is fitted to their words
    margin = 2 * grid

    def region_box(r: str) -> Tuple[int, int, int, int]:
        """A canvas region's box (§6.5 item 1): a cell of the 3 x 3 grid inside a 2-grid-step
        margin, or a half / third of the canvas (left / middle / right split x; top / bottom y)."""
        iw, ih = W - 2 * margin, H - 2 * margin
        if r in REGIONS_3X3:
            col = 0 if "left" in r else (2 if "right" in r else 1)
            row = 0 if "top" in r else (2 if "bottom" in r else 1)
            return margin + col * iw // 3, margin + row * ih // 3, iw // 3, ih // 3
        n = 2 if r.endswith("half") else 3
        first = r.split("_")[0]
        i = {"left": 0, "top": 0, "middle": 1, "right": n - 1, "bottom": n - 1}[first]
        if first in ("left", "right", "middle"):
            return i * W // n, 0, W // n, H
        return 0, i * H // n, W, H // n

    def region_words(r: str) -> str:
        return {"center": "middle", "top_left": "top left", "top_right": "top right",
                "bottom_left": "bottom left", "bottom_right": "bottom right"}.get(r, r.replace("_", " "))

    def ref_rect(r: str) -> Optional[Tuple[int, int, int, int]]:
        """The live box of one reference part: a symbol's footprint (a removed one's ghost), the
        canvas, or a canvas region."""
        if r == "canvas":
            return 0, 0, W, H
        if r.startswith("canvas:"):
            return region_box(r[7:]) if r[7:] in REGIONS else None
        s = _find(r)
        if s is not None and str(s.get("id")) not in ed.removing:
            return ed.fp(s)
        gh = ed.ghosts.get(r)
        return gh["fp"] if gh is not None else None

    def interval(c: Dict[str, Any]) -> Optional[Tuple[Fraction, Fraction]]:
        """The reference's interval on the constraint's axis, now (references move in a batch)."""
        if c.get("iv") is not None:
            return c["iv"]
        rects = [ref_rect(r) for r in c.get("refs") or []]
        if not rects or any(q is None for q in rects):
            return None
        i = 0 if c["axis"] == "x" else 1
        if c.get("between"):
            a, b = sorted(rects, key=lambda q: q[i])[:2]  # type: ignore[index]
            return Fraction(a[i] + a[i + 2]), Fraction(b[i])
        return Fraction(min(q[i] for q in rects)), Fraction(max(q[i] + q[i + 2] for q in rects))  # type: ignore[index]

    def cons_want(c: Dict[str, Any], s: Dict[str, Any]) -> Optional[Fraction]:
        """Where the constraint puts the subject's FRAME on its axis (exact, before the grid)."""
        iv = interval(c)
        if iv is None:
            return None
        lo, hi = iv
        i = 0 if c["axis"] == "x" else 1
        box = ed.abs_box(s)
        if c.get("on") == "fp":
            f = ed.fp(s)
            off, size = f[i] - box[i], f[i + 2]
        else:
            off, size = 0, box[i + 2]
        gp, mode = c.get("gap", 0), c["mode"]
        if mode == "after":
            p = hi + gp
        elif mode == "before":
            p = lo - gp - size
        elif mode == "start":
            p = lo + gp
        elif mode == "end":
            p = hi - gp - size
        else:
            p = (lo + hi - size) / 2
        return p - off

    def snapf(v: Fraction) -> int:
        """To the nearest grid line (half-up), so a centered subject is within half a grid step."""
        return int((v / grid + Fraction(1, 2)) // 1) * grid

    def apply_cons(sid: str) -> None:
        s = _find(sid)
        if s is None or not cons.get(sid):
            return
        ax, ay, _, _ = ed.abs_box(s)
        for axis, c in cons[sid].items():
            p = cons_want(c, s)
            if p is None:
                continue
            if axis == "x":
                ax = snapf(p)
            else:
                ay = snapf(p)
        ed.set_abs(s, ax, ay)
        ed.clamp(s)

    def cons_kept(sid: str, axis: str) -> bool:
        """§6.5 item 2: a constraint re-verified exactly — the frame within half a grid step of
        where the constraint puts it (a centered subject's center within half a step)."""
        s = _find(sid)
        c = cons.get(sid, {}).get(axis)
        if s is None or c is None:
            return True
        p = cons_want(c, s)
        if p is None:
            return True
        return abs(Fraction(ed.abs_box(s)[0 if axis == "x" else 1]) - p) <= Fraction(grid, 2)

    def movable(o: Dict[str, Any]) -> bool:
        """§6.5 item 2b: an obstacle this request did not create, place, resize or constrain, and
        not locked, may be moved out of the way at its own size."""
        oid = str(o.get("id"))
        return not is_locked(o) and oid not in created and oid not in pinned and oid not in cons \
            and oid not in resized

    def shift_reference(s: Dict[str, Any]) -> Optional[str]:
        """§6.5 item 2b for the reference itself: when a constraint would put `s` outside the
        canvas and its reference is ONE symbol that may be moved out of the way, that reference
        moves (at its own size, along that axis, just enough) so the relation holds exactly.
        Returns the note, or None (nothing moved)."""
        sid = str(s.get("id"))
        for axis, cc in list(cons.get(sid, {}).items()):
            p = cons_want(cc, s)
            refs = cc.get("refs") or []
            if p is None or len(refs) != 1 or _is_canvas_ref(refs[0]):
                continue
            i = 0 if axis == "x" else 1
            total = W if axis == "x" else H
            size = ed.abs_box(s)[i + 2]
            if p + size > total:
                shift = -_ceil_g(p + size - total, grid)
            elif p < 0:
                shift = _ceil_g(-p, grid)
            else:
                continue
            R = _find(refs[0])
            if R is None or R is s or not movable(R):
                continue
            rb = ed.abs_box(R)
            new = rb[i] + shift
            if new < 0 or new + rb[i + 2] > total:
                continue
            ed.set_abs(R, new if i == 0 else rb[0], new if i == 1 else rb[1])
            apply_cons(sid)
            if [o for o in ed.conflicts(R) if o is not s] or not cons_kept(sid, axis):
                ed.set_abs(R, rb[0], rb[1])
                apply_cons(sid)
                continue
            pinned.add(str(R.get("id")))
            way = ("left" if shift < 0 else "right") if i == 0 else ("up" if shift < 0 else "down")
            return f"moved {nm(R)} {way} so {nm(s)} fits {cc.get('desc') or 'where it was asked'}"
        return None

    def fixed_of(sid: str) -> Tuple[bool, bool]:
        c = cons.get(sid, {})
        return "x" in c, "y" in c

    def con(axis: str, mode: str, refs: Sequence[str] = (), gp: int = 0,
            iv: Optional[Tuple[Fraction, Fraction]] = None, between: bool = False,
            on: str = "frame", desc: str = "") -> Dict[str, Any]:
        return {"axis": axis, "mode": mode, "refs": None if iv is not None else list(refs), "iv": iv,
                "gap": gp, "between": between, "on": on, "desc": desc}

    _CROSS_WORD = {("x", "start"): "left-aligned", ("x", "end"): "right-aligned",
                   ("y", "start"): "top-aligned", ("y", "end"): "bottom-aligned"}

    def relation(where: str, parts: List[str], align: str, gap: int, gap_given: bool, rname: str,
                 align_given: bool) -> Tuple[Dict[str, Dict[str, Any]], str, Optional[str]]:
        """A place op's relation → ({axis: constraint}, the relation in words, a refusal reason)
        (§6.5 item 1). Edges are the reference's drawn edges; the subject's frame is placed."""
        out: Dict[str, Dict[str, Any]] = {}
        if where in ("below", "above", "left_of", "right_of"):
            main = "y" if where in ("below", "above") else "x"
            cross = "x" if main == "y" else "y"
            mode = "after" if where in ("below", "right_of") else "before"
            words = {"below": "under", "above": "above", "left_of": "left of", "right_of": "right of"}[where]
            if align == "center":
                desc = f"centered {words} {rname}" if main == "y" else f"{words} {rname}, vertically centered"
            else:
                desc = f"{words} {rname}, {_CROSS_WORD[(cross, align)]}"
            out[main] = con(main, mode, parts, gap, desc=desc)
            out[cross] = con(cross, align, parts, desc=desc)
        elif where == "between":
            rects = [ref_rect(p) for p in parts]
            if len(rects) != 2 or any(r is None for r in rects):
                return {}, "", "it goes between two symbols; name both"
            a, b = rects[0], rects[1]
            gx = max(b[0] - (a[0] + a[2]), a[0] - (b[0] + b[2]))  # type: ignore[index]
            gy = max(b[1] - (a[1] + a[3]), a[1] - (b[1] + b[3]))  # type: ignore[index]
            if gx < 0 and gy < 0:
                return {}, "", f"{rname} overlap, so there is no space between them"
            main = "x" if gx >= gy else "y"
            cross = "y" if main == "x" else "x"
            desc = f"centered between {rname}"
            out[main] = con(main, "center", parts, between=True, desc=desc)
            out[cross] = con(cross, align, parts, desc=desc)
        elif where == "center_of":
            desc = f"centered on {rname}"
            out = {"x": con("x", "center", parts, desc=desc), "y": con("y", "center", parts, desc=desc)}
        elif where == "inside":
            inset = gap if gap_given else grid
            desc = f"inside {rname}"
            out = {"x": con("x", align, parts, inset, desc=desc), "y": con("y", align, parts, inset, desc=desc)}
        elif where == "region":
            r = parts[0][7:] if parts and parts[0].startswith("canvas:") else ""
            if r not in REGIONS:
                return {}, "", "a region of the canvas is needed (e.g. the middle, the top right)"
            desc = f"in the {region_words(r)} of the canvas"

            def span(mode: str, total: int) -> Tuple[Fraction, Fraction]:
                # centered on the whole canvas; start / end against the margin
                return (Fraction(0), Fraction(total)) if mode == "center" else \
                    (Fraction(margin), Fraction(total - margin))

            if r in REGIONS_3X3:
                col = "start" if "left" in r else ("end" if "right" in r else "center")
                row = "start" if "top" in r else ("end" if "bottom" in r else "center")
                out = {"x": con("x", col, iv=span(col, W), desc=desc),
                       "y": con("y", row, iv=span(row, H), desc=desc)}
            else:
                bx, by, bw, bh = region_box(r)
                if r.split("_")[0] in ("left", "right", "middle"):
                    out = {"x": con("x", "center", iv=(Fraction(bx), Fraction(bx + bw)), desc=desc)}
                    cross = "y"
                else:
                    out = {"y": con("y", "center", iv=(Fraction(by), Fraction(by + bh)), desc=desc)}
                    cross = "x"
                if align_given:
                    out[cross] = con(cross, align, iv=span(align, H if cross == "y" else W), desc=desc)
        else:
            return {}, "", f"'{where}' is not a placement I know"
        return out, desc, None

    def place_line(s: Dict[str, Any], where: str, align: str, rname: str, centered_text: bool) -> str:
        """The note for a place, one line (§6.3 item 5)."""
        too = " (its text centered too)" if centered_text else ""
        if where in ("below", "above"):
            how = "centered" if align == "center" else _CROSS_WORD[("x", align)]
            return f"moved {nm(s)} {where} {rname}, {how}{too}"
        if where in ("left_of", "right_of"):
            how = "" if align == "center" else ", " + _CROSS_WORD[("y", align)]
            return f"moved {nm(s)} {'right of' if where == 'right_of' else 'left of'} {rname}{how}"
        if where == "between":
            return f"moved {nm(s)} between {rname}, centered{too}"
        if where == "center_of":
            return f"centered {nm(s)} on {rname}{too}"
        if where == "inside":
            how = {"start": ", in its top left corner", "end": ", in its bottom right corner"}.get(align, ", centered")
            return f"moved {nm(s)} inside {rname}{how}"
        return f"moved {nm(s)} to {rname}{too}"

    def name_of(sid: Any) -> str:
        """A symbol's name for a note, by an id the model wrote (present, removed, or unknown)."""
        if isinstance(sid, list):
            parts = [name_of(x) for x in sid]
            return " and ".join(parts) if len(parts) <= 2 else ", ".join(parts[:-1]) + " and " + parts[-1]
        if not isinstance(sid, str) or not sid.strip():
            return "that symbol"
        if sid.strip() == "canvas":
            return "the canvas"
        if sid.strip().startswith("canvas:"):
            return f"the {region_words(sid.strip()[7:])} of the canvas"
        rid = real(sid)
        if rid in refused_removal:
            return refused_removal[rid]
        s = _find(rid)
        if s is not None:
            return nm(s)
        gh = ed.ghosts.get(rid)
        return gh["name"] if gh is not None else "that symbol"

    def op_refs(op: Dict[str, Any]) -> List[str]:
        """Every symbol an op touches: its targets, then its `of` / `to` (the implicit one too), a
        swap's partners."""
        kind = op.get("op")
        ids = [str(i).strip() for i in _op_ids(op)]
        if kind == "swap":
            ids = [str(op.get("a") or "").strip(), str(op.get("b") or "").strip()]
        if kind in ("place", "align", "match"):
            key = "of" if kind == "place" else "to"
            r = target_of(op, key)
            ids.extend(_ref_list(r) if isinstance(r, (str, list)) else [])
        return [i for i in ids if i and not _is_canvas_ref(i)]

    def would_do(op: Dict[str, Any]) -> str:
        """What an op would have done, in words, for "did not <…>, because …" (§6.4 item 4)."""
        kind = op.get("op")
        ids = _op_ids(op)
        sname = name_of(ids[0]) if ids else "that symbol"
        if kind == "place":
            r = name_of(target_of(op, "of"))
            where = op.get("where")
            if where == "at":
                return f"put {sname} in {r}'s place"
            if where == "center_of":
                return f"center {sname} on {r}"
            if where == "region":
                return f"put {sname} in {r}"
            return f"put {sname} {str(where).replace('_of', ' of')} {r}"
        if kind == "make_room":
            return f"move {sname} out of the way"
        if kind in ("align", "match"):
            r = name_of(target_of(op, "to"))
            if kind == "align":
                return f"align {sname} to the {op.get('edge')} of {r}"
            what = {"width": "width", "height": "height"}.get(str(op.get("size")), "size")
            return f"make {sname} the same {what} as {r}"
        if kind == "swap":
            return f"swap {name_of(op.get('a'))} and {name_of(op.get('b'))}"
        verb = {"remove": "remove", "move": "move", "resize": "resize", "set": "change", "scale": "resize",
                "nudge": "nudge", "center": "center", "distribute": "space out", "restyle": "restyle",
                "rule": "change the rules of", "rebind": "rebind"}.get(str(kind), "change")
        return f"{verb} {sname}"

    def placed_later(idx: int, sid: str) -> bool:
        """Whether an op later in the batch places `sid` (by its id, or by an id the display does
        not hold — the model's guess for an added symbol)."""
        have = {str(x.get("id")) for x in ed._all()}
        for o in ops[idx + 1:]:
            if o.get("op") in ("place", "center", "align") and any(
                    str(i).strip() == sid or str(i).strip() not in have for i in _op_ids(o)):
                return True
        return False

    def later_sets_size(idx: int, sid: str, rid: str) -> bool:
        """§6.4 item 5: an op later in the batch sets `sid`'s size (resize, scale, a set of w / h, a
        match — except a match to the same removed symbol, which asks for the same frame)."""
        def names_it(i: Any) -> bool:
            i = str(i).strip()
            return i == sid or alias.get(i) == sid
        # §6.6 item 2: a size op on a symbol this request added counts only when the user wrote a
        # number (resize / scale / set) or compared sizes (match) — otherwise the engine drops it
        added_now = sid in added_order
        for o in ops[idx + 1:]:
            k = o.get("op")
            if not any(names_it(i) for i in _op_ids(o)):
                continue
            if k in ("resize", "scale"):
                if added_now and not explicit_size:
                    continue
                return True
            if k == "match" and str(o.get("to") or "").strip() != rid:
                if added_now and not _COMPARE_ASK_RE.search(request or ""):
                    continue
                return True
            if k == "set" and isinstance(o.get("props"), dict) and \
                    {"w", "h"} & set(normalize_props(o["props"])):
                if added_now and not explicit_size:
                    continue
                return True
        return False

    def note_made_way(more: List[str]) -> None:
        for o in ed.last_moved:
            n = next((x for x in more if x.startswith(f"moved {nm(o)} out of the way")), None)
            if n:
                made_way[str(o.get("id"))] = n

    def settle(syms: List[Dict[str, Any]], op: Dict[str, Any], own: bool = True) -> Optional[str]:
        """The no-overlap rule for what an op moved (§6.3 item 3 under §6.5 items 2 and 5): a
        constrained axis stays (obstacles move out of the way at their own size instead); only a
        symbol this request created — or one the request permits — shrinks itself; a relation that
        cannot be kept exactly goes as near as the rule allows, with a note. A pair the request
        permits to overlap is not a conflict (`ed.overlap_ok`, §6.6 item 1)."""
        for s in syms:
            ed.clamp(s)
            sid = str(s.get("id"))
            fixed = fixed_of(sid)
            own_ok = own and (sid in created or permitted)
            constrained = fixed[0] or fixed[1]
            if constrained:
                moved_ref = shift_reference(s)
                if moved_ref:
                    settle_notes.append(moved_ref)
            ed.last_moved = []
            why, more = ed.settle(s, may_shrink=shrinkable(op, syms), own=own_ok, fixed=fixed,
                                  movable=movable if constrained else None)
            if why and constrained:
                # §6.5 item 2d: the relation cannot be kept here — as near as the rule allows.
                hit = ed.conflicts(s)
                ed.last_moved = []
                why2, more2 = ed.settle(s, may_shrink=shrinkable(op, syms), own=own_ok)
                if why2 is None:
                    desc = next((c.get("desc") for c in cons[sid].values() if c.get("desc")), "where it was asked")
                    more = more2 + [f"could not keep {nm(s)} {desc}: "
                                    f"{', '.join(nm(o) for o in hit[:3]) or 'another symbol'} "
                                    f"{'is' if len(hit) <= 1 else 'are'} in the way"]
                    cons.pop(sid, None)
                    why = None
            if why:
                return why
            note_made_way(more)
            settle_notes.extend(more)
        return None

    settle_notes: List[str] = []                       # said after the op's own line
    deferred: List[str] = []                           # adds settled by a later place (or at the end)
    from_request: List[str] = []                       # texts the engine added from the request's words
    for idx, op in enumerate(ops):
        kind = op["op"]
        settle_notes.clear()
        # §6.4 item 4: an op that touches a symbol whose removal was refused is refused too.
        dep = next((real(r) for r in op_refs(op) if real(r) in refused_removal), None) \
            if refused_removal and kind not in ("add", "retitle", "clear", "remove") else None
        if dep is not None:
            ids0 = [real(i) for i in _op_ids(op)]
            t0s = _find(ids0[0]) if ids0 else None
            note = f"did not {would_do(op)}, because {refused_removal[dep]} was not removed"
            if kind == "place" and t0s is not None and str(t0s.get("id")) in added_order \
                    and str(t0s.get("id")) != dep:
                note += f" — {nm(t0s)} stays where it was added, clear of the other symbols"
            refuse(kind, ids0[0] if ids0 else dep, "depends on a refused removal", note)
            continue
        before = copy.deepcopy(top)
        reshaped_before = set(ed.reshaped)
        ghosts_before = dict(ed.ghosts)
        placed_before = dict(placed_at)
        cons_before = copy.deepcopy(cons)
        pinned_before, resized_before, fitted_before = set(pinned), set(resized), set(fitted)
        made_way_before = dict(made_way)
        line: Optional[str] = None
        moved: List[Dict[str, Any]] = []
        own = True
        fail: Optional[Tuple[str, str]] = None           # (symbol id, reason)

        if kind == "retitle":
            doc["name"] = _clean_text(op["title"], 200)
            line = f"renamed the display to '{doc['name']}'"
        elif kind == "clear":
            # §6.4 item 3 (reversing the D5.2 ruling): clear removes every UNLOCKED symbol; a
            # locked one stays (with the group holding it), and the note says how many.
            gone = 0
            kept = 0

            def prune(lst: List[Any]) -> List[Any]:
                nonlocal gone, kept
                out: List[Any] = []
                for x in lst:
                    if not isinstance(x, dict):
                        continue
                    if is_locked(x):
                        kept += sum(1 for y in _walk([x]) if isinstance(y, dict) and y.get("type") != "group") or 1
                        out.append(x)
                    elif x.get("type") == "group" and isinstance(x.get("children"), list):
                        x["children"] = prune(x["children"])
                        if x["children"]:
                            out.append(x)
                    else:
                        gone += 1
                return out

            top[:] = prune(top)
            if gone:
                line = f"cleared {gone} symbol{'' if gone == 1 else 's'}"
            if kept:
                settle_notes.append(f"kept {kept} locked symbol{'' if kept == 1 else 's'}")
                if not gone:
                    refuse(kind, "canvas", "every symbol is locked")
        elif kind == "add":
            sym, missing = _map_symbol(op["symbol"], res, W, H, grid, range_missing)
            notes.extend(sym.pop("_notes", []))
            sym["id"] = f"s{nxt}"
            added_order.append(sym["id"])
            created.add(sym["id"])
            ed.names[sym["id"]] = f"the new {_kind_noun(sym)}"
            nxt += 1
            props = props_of(_symbol_props(op["symbol"]), sym)
            add_done: List[str] = []
            if props:
                ign: List[str] = []
                add_done, bad = apply_props(sym, props, request=request, W=W, H=H, grid=grid, g=g,
                                            images=images, ignored=ign)
                for b in bad:
                    notes.append(f"{nm(sym)}: {b}")
                for k in ign:
                    notes.append(f"ignored '{k}' — Describe cannot set that on {_a_kind(sym)}")
            for k_id in ("label", "text"):
                if isinstance(sym.get(k_id), str) and _ID_LIKE_RE.match(sym[k_id]):
                    sym.pop(k_id)                      # an id where words belong (corpus C098)
            if sym["type"] == "text" and not sym.get("text") and sym.get("label"):
                sym["text"] = sym.pop("label")         # a text's words are its text (battery E56)
            if sym["type"] == "text" and not sym.get("text"):
                said = _quoted_words(request)
                if said:
                    sym["text"] = said                 # 'add a text that says "X"' with no words
            default_decimals(sym, pts, ctx_el, g)
            # §6.5 item 3 / §6.6 item 2: the engine decides how big — the kind's human-scale
            # default (a text fitted to its words), centered where the model drafted it, unless the
            # request gives this symbol a number, a comparison to a real symbol or a size word; the
            # model's size only where it equals a number the user wrote; never below the floor
            # (item 4) unless the user gave an explicit number (applied, with a note).
            is_default, small = human_size(sym, request, W, H, grid, others=frames_now())
            dw0, dh0 = default_size(sym, W, H, grid)
            if not is_default and sym["type"] != "text" and (sym["w"] > dw0 or sym["h"] > dh0):
                # asked bigger: to fit, it may shrink — never below its default (noted)
                ed.min_size[sym["id"]] = (min(dw0, sym["w"]), min(dh0, sym["h"]))
                asked_big[sym["id"]] = (sym["w"], sym["h"])
            if op.get("mid") or op.get("from_request"):
                # no position drafted: centered on the canvas (nearest grid step, so the center is
                # within half a step of the middle); a later place or the no-overlap rule moves it
                sym["x"] = int((Fraction(W - sym["w"], 2) / grid + Fraction(1, 2)) // 1) * grid
                sym["y"] = int((Fraction(H - sym["h"], 2) / grid + Fraction(1, 2)) // 1) * grid
            if is_default and sym["type"] == "text":
                fitted.add(sym["id"])
            if small:
                settle_notes.append(f"{nm(sym)} is now smaller than is easy to read")
            gl = sym.pop("_group", None)
            host = None
            if gl:
                host = next((s for s in top if isinstance(s, dict) and s.get("type") == "group"
                             and name_key(str(s.get("label") or "")) == name_key(gl)), None)
            ax, ay = sym["x"], sym["y"]
            if host is not None:
                host.setdefault("children", []).append(sym)
                ed.set_abs(sym, ax, ay)
            else:
                if gl:
                    sym["_group"] = gl
                    added.append(sym)
                top.append(sym)
            ed.reshaped.add(sym["id"])
            for n in missing:
                placeholders.append({"symbol": sym["id"], "name": n})
            # An add that a later op of this batch places is settled THERE (settling it at the
            # model's rough spot first could shrink it — live battery E47 / E54); if that place is
            # refused, the add is settled at the end of the batch.
            if placed_later(idx, sym["id"]):
                deferred.append(sym["id"])
            else:
                moved = [sym]
            said = [d for d in add_done if not d.startswith("label ") and d != "text"]
            shown = sym.get("label") or (_clean_text(sym.get("text"), 40) if sym["type"] == "text" else "")
            line = f"added {_a_kind(sym)}" + (f" '{shown}'" if shown else "") + \
                (f" ({', '.join(said)})" if said else "")
            if op.get("from_request"):
                line = f"added a text '{shown}' (from your request)"
                from_request.append(sym["id"])
        elif kind in ("remove",):
            want = [real(i) for i in _op_ids(op)]
            ss = targets(kind, want)
            for sid in want:                           # §6.4 item 4: refused, so still there
                x = _find(sid)
                if x is not None and all(x is not y for y in ss):
                    refused_removal[sid] = nm(x)
            for s in ss:
                ed.removing.add(str(s.get("id")))
                ed.ghosts[str(s.get("id"))] = {"box": ed.abs_box(s), "fp": ed.fp(s), "name": nm(s),
                                               "type": s.get("type"), "kkey": _kkey(s)}
                for lst in [top] + [x["children"] for x in ed._all() if isinstance(x.get("children"), list)]:
                    if any(x is s for x in lst):
                        lst.remove(s)
                        break
            if ss:
                line = "removed " + ", ".join(nm(s) for s in ss)
        elif kind in ("move", "resize"):
            ss = targets(kind, _op_ids(op))
            ov = None
            if ss and kind == "resize":
                ov = size_override(ss[0], _to_px(op["w"], W, grid, grid), _to_px(op["h"], H, grid, grid))
                if ov is not None and (ov[0], ov[1]) == (_int0(ss[0].get("w")), _int0(ss[0].get("h"))):
                    size_stays(kind, ss[0], ov[2])
                    ss = []                            # §6.6 item 2: no size change to make
            if ss:
                s = ss[0]
                ax, ay, w, h = ed.abs_box(s)
                sid_m = str(s.get("id"))
                pinned.add(sid_m)
                if kind == "move":
                    ed.set_abs(s, _to_px(op["x"], W, grid), _to_px(op["y"], H, grid))
                    cons.pop(sid_m, None)                  # explicit coordinates replace a relation
                    line = f"moved {nm(s)}"
                elif ov is not None:
                    # §6.6 item 2: the size the request gives (its top-left kept, like any resize)
                    if ov[2] == "smaller" and (ov[0], ov[1]) == ed.floor(s):
                        settle_notes.append(f"{nm(s)} stops at its smallest size, {ov[0]} x {ov[1]} px")
                    s["w"], s["h"] = ov[0], ov[1]
                    ed.reshaped.add(sid_m)
                    resized.add(sid_m)
                    fitted.discard(sid_m)
                    own = False
                    line = readable_line(s) if ov[2] == "readable" else f"resized {nm(s)} to {s['w']} x {s['h']} px"
                else:
                    fw, fh = ed.floor(s)
                    s["w"] = max(grid, min(_to_px(op["w"], W, grid, grid), W - max(0, ax)))
                    s["h"] = max(grid, min(_to_px(op["h"], H, grid, grid), H - max(0, ay)))
                    pw, ph = request_px(request)        # the user's px wins over the model's percent
                    if pw is not None:
                        s["w"] = max(grid, min(_snap(pw, grid), W - max(0, ax)))
                    if ph is not None:
                        s["h"] = max(grid, min(_snap(ph, grid), H - max(0, ay)))
                    ed.reshaped.add(sid_m)
                    resized.add(sid_m)
                    own = False
                    small = s["w"] < fw or s["h"] < fh
                    if small and not explicit_size:
                        # §6.5 item 4 (ruling, live battery E32): only a NUMBER the user gave goes
                        # below the floor; "half" / "smaller" stop there
                        s["w"], s["h"] = max(s["w"], fw), max(s["h"], fh)
                        settle_notes.append(f"{nm(s)} stops at its smallest size, {s['w']} x {s['h']} px")
                        small = False
                    line = f"resized {nm(s)} to {s['w']} x {s['h']} px"
                    if small:                          # §6.5 item 4: an explicit size is applied
                        settle_notes.append(f"{nm(s)} is now smaller than is easy to read")
                moved = [s]
        elif kind == "place":
            ss = targets(kind, _op_ids(op))
            rid = target_of(op, "of") if ss else None
            if ss and rid is None:
                refuse(kind, str(ss[0].get("id")), "no reference",
                       f"I could not tell where to put {nm(ss[0])} — the display has: {_holdings(top, names=ed.names)}")
            where = op["where"]
            if isinstance(rid, list) and where != "between" and len(rid) == 1:
                rid = rid[0]
            if isinstance(rid, list) and where == "at":
                rid = rid[0]
            rparts = [real(r) for r in _ref_list(rid)] if isinstance(rid, (str, list)) else []
            if ss:
                op_pairs.update((str(ss[0].get("id")), r) for r in rparts)
            rb = None
            if ss and rid is not None and str(ss[0].get("id")) in rparts:
                refuse(kind, str(ss[0].get("id")), "placed against itself",
                       f"I could not tell where to put {nm(ss[0])} — it cannot be placed against itself")
            elif ss and rid is not None:
                rb = ref_box(kind, rid)
            if ss and rb is not None:
                s = ss[0]
                (rx, ry, rw, rh), (qx, qy, qw, qh), rname = rb
                # the model's gap counts only when the user gave a distance (live battery E48-E51)
                gap_given = _is_num(op.get("gap")) and bool(_GAP_ASK_RE.search(request or ""))
                gap = int(_frac(op["gap"])) if gap_given else PLACE_GAP
                sid_s = str(s.get("id"))

                if where == "at":
                    rreal = real(rid)
                    pinned.add(sid_s)
                    cons.pop(sid_s, None)
                    if rreal in ed.ghosts and _find(rreal) is None:
                        # §6.4 item 5: "in its place" = the removed symbol's FRAME (position and
                        # size) unless a later op in this batch sets the size; then cut back from
                        # anything that frame would overlap (the removed symbol's old neighbors).
                        placed_at[sid_s] = rreal
                        same_kind = ed.ghosts[rreal].get("kkey", ed.ghosts[rreal].get("type")) == _kkey(s)
                        if same_kind or sid_s in added_order:
                            created.add(sid_s)
                        same_ask = re.search(r"\bsame\s+(size|width|height)\b", request or "", re.I)
                        if not same_kind and same_ask and not later_sets_size(idx, sid_s, rreal):
                            # Ruling: "… in its place, same size" — the comparison wins over "another
                            # kind keeps its own size": the removed frame's size, clamped to the floor
                            fw_, fh_ = floor_size(s, W, H, grid)
                            axis_ = same_ask.group(1).lower()
                            if axis_ in ("size", "width"):
                                s["w"] = max(rw, fw_)
                            if axis_ in ("size", "height"):
                                s["h"] = max(rh, fh_)
                            ed.reshaped.add(sid_s)
                            fitted.discard(sid_s)
                        if same_kind and not later_sets_size(idx, sid_s, rreal):
                            # Ruling after the D5.5 corpus (C103), new and existing symbols alike: the
                            # same kind takes the frame, never below its own floor (the floor is the
                            # harm rule), centered on the frame
                            fw_, fh_ = floor_size(s, W, H, grid)
                            s["w"], s["h"] = max(rw, fw_), max(rh, fh_)
                            ed.reshaped.add(sid_s)
                            fitted.discard(sid_s)
                        # another kind keeps its own size; either way centered on the removed frame
                        ed.set_abs(s, _snap(rx + (rw - _int0(s.get("w"))) // 2, grid),
                                   _snap(ry + (rh - _int0(s.get("h"))) // 2, grid))
                        line = f"put {nm(s)} in {rname}'s place"
                        ed.clamp(s)
                        if same_kind:
                            cut = ed.trim_clear(s)
                            if cut:
                                settle_notes.append(f"made {nm(s)} smaller to keep it clear of "
                                                    f"{', '.join(nm(o) for o in cut[:3])}")
                    else:
                        ed.set_abs(s, _snap(rx, grid), _snap(ry, grid))
                        line = f"moved {nm(s)} to {rname}'s place"
                    moved = [s]
                else:
                    # §6.5 items 1-2: the relation becomes one constraint per axis, applied now
                    # and kept (re-applied and re-verified) to the end of the batch.
                    align = op.get("align") if op.get("align") in PLACE_ALIGNS else "center"
                    new_c, desc, why_not = relation(where, rparts, align, gap, gap_given, rname,
                                                    op.get("align") in PLACE_ALIGNS)
                    if why_not:
                        refuse(kind, sid_s, why_not, f"did not {would_do(op)}: {why_not}")
                    else:
                        merged = dict(cons.get(sid_s, {})) if (where == "region" and len(new_c) == 1) else {}
                        merged.update(new_c)
                        cons[sid_s] = merged
                        pinned.add(sid_s)
                        if where in ("inside", "center_of"):
                            # §6.6 item 1: inside / centered on a symbol is not an overlap to refuse —
                            # when it is containment: the reference is a container (shape / group /
                            # image), or the request's own words say inside / within / into / centered
                            # on. A model's "inside" for "where the table is" (corpus C144, found at the
                            # D3.5 gate) is an ordinary place: the no-overlap rule decides.
                            said_in = bool(_CONTAIN_SAID_RE.search(request or ""))
                            ok_pairs.update((sid_s, r) for r in rparts if not _is_canvas_ref(r)
                                            and (said_in or str((_find(r) or {}).get("type")) in _CONTAINERS))
                        centered_text = False
                        cx = merged.get("x")
                        if cx is not None and cx["mode"] == "center" and _center_text(s):
                            centered_text = True       # a centered text is centered inside its box too
                            ed.reshaped.add(sid_s)
                        apply_cons(sid_s)
                        line = place_line(s, where, align, rname, centered_text)
                        R = _find(rparts[0]) if len(rparts) == 1 else None
                        if where in ("below", "above", "left_of", "right_of") and R is not None \
                                and rparts[0] in created and not is_locked(R) and R is not s:
                            def spot(_q: Optional[Tuple[int, int, int, int]]) -> Tuple[int, int]:
                                # where the relation puts it, NOT clamped into the canvas (a clamp
                                # would hide that it does not fit there)
                                b = ed.abs_box(s)
                                px_, py_ = b[0], b[1]
                                for axis_, c_ in cons.get(sid_s, {}).items():
                                    p_ = cons_want(c_, s)
                                    if p_ is not None:
                                        px_, py_ = (snapf(p_), py_) if axis_ == "x" else (px_, snapf(p_))
                                return px_, py_

                            ed.set_abs(s, *spot(None))
                            why_r = ed.make_room_in(R, s, where, spot, movable=movable)
                            if why_r:
                                resized.add(rparts[0])
                                settle_notes.append(f"made {nm(R)} {why_r} to fit {nm(s)} "
                                                    f"{str(where).replace('_of', ' of')} it")
                        moved = [s]
        elif kind == "align":
            ss = targets(kind, _op_ids(op))
            rid = target_of(op, "to") if ss else None
            if ss and rid is None:
                refuse(kind, str(ss[0].get("id")), "no reference",
                       f"I could not tell what to align {nm(ss[0])} with — the display has: {_holdings(top, names=ed.names)}")
            rb = ref_box(kind, rid) if (ss and rid is not None) else None
            if ss and rb is not None:
                _, (qx, qy, qw, qh), rname = rb
                edge = op["edge"]
                # §6.5 item 1: an align is a constraint on one axis against the reference (an id, a
                # list of ids = their union box, the canvas or a region), kept to the end of the batch.
                parts = [real(r) for r in _ref_list(rid)] if isinstance(rid, (str, list)) else []
                op_pairs.update((str(x.get("id")), r) for x in ss for r in parts)
                axis = "x" if edge in ("left", "center", "right") else "y"
                mode = {"left": "start", "top": "start", "center": "center", "middle": "center",
                        "right": "end", "bottom": "end"}[edge]
                desc = f"aligned to the {edge} of {rname}"
                for s in ss:
                    sid_a = str(s.get("id"))
                    if sid_a in parts:
                        continue                       # a symbol is already aligned with itself
                    if edge == "center" and _center_text(s):
                        ed.reshaped.add(sid_a)
                    cons.setdefault(sid_a, {})[axis] = con(axis, mode, parts, on="frame" if edge == "center" else "fp",
                                                           desc=desc)
                    pinned.add(sid_a)
                    apply_cons(sid_a)
                moved = ss
                line = f"aligned {', '.join(nm(s) for s in ss)} to the {edge} of {rname}"
        elif kind == "match":
            ss = targets(kind, _op_ids(op))
            rid = target_of(op, "to") if ss else None
            if ss and rid is None:
                refuse(kind, str(ss[0].get("id")), "no reference",
                       f"I could not tell whose size to match — the display has: {_holdings(top, names=ed.names)}")
            if ss and rid is not None and all(placed_at.get(str(x.get("id"))) == real(rid) and
                                              (ed.ghosts.get(real(rid)) or {}).get("kkey") == _kkey(x)
                                              for x in ss):
                continue                               # already in that frame (§6.4 item 5, same kind)
            if ss and not _COMPARE_ASK_RE.search(request or ""):
                # §6.6 item 2: a symbol this request added takes another's size only when the user
                # compared them ("the same size as …"); a match the model made up is dropped silently
                ss = [x for x in ss if str(x.get("id")) not in added_order]
                if not ss:
                    continue
            # an existing symbol whose size the request does not talk about keeps it (silently),
            # except one put in a removed symbol's place (§6.4 item 5 / the D5.5 rulings)
            ss = [x for x in ss if str(x.get("id")) in added_order or placed_at.get(str(x.get("id")))
                  or (existing_target(x, op_target=True) or (0, 0, ""))[2] != "drop"]
            if not ss:
                continue
            rb = ref_box(kind, rid) if (ss and rid is not None) else None
            if ss and rb is not None:
                (rx, ry, rw, rh), _, rname = rb
                for s in ss:
                    if op["size"] in ("width", "both"):
                        s["w"] = rw
                    if op["size"] in ("height", "both"):
                        s["h"] = rh
                    ed.reshaped.add(str(s.get("id")))
                    resized.add(str(s.get("id")))
                    fitted.discard(str(s.get("id")))
                    fw, fh = floor_size(s, W, H, grid)
                    # the D5.5 rulings: floors are the harm rule, only an explicit number goes below
                    # (on the matched axis; an axis the op does not touch stays as it is)
                    if op["size"] in ("width", "both"):
                        s["w"] = max(_int0(s.get("w")), fw)
                    if op["size"] in ("height", "both"):
                        s["h"] = max(_int0(s.get("h")), fh)
                    if placed_at.get(str(s.get("id"))) == real(rid) and real(rid) in ed.ghosts:
                        gb = ed.ghosts[real(rid)]["box"]       # in that place: centered on its frame
                        ed.set_abs(s, _snap(gb[0] + (gb[2] - s["w"]) // 2, grid),
                                   _snap(gb[1] + (gb[3] - s["h"]) // 2, grid))
                moved, own = ss, False
                what = {"width": "width", "height": "height", "both": "size"}[op["size"]]
                line = f"made {', '.join(nm(s) for s in ss)} the same {what} as {rname}"
        elif kind == "distribute":
            ss = targets(kind, _op_ids(op))
            if len(ss) >= 3:
                horiz = op["axis"] == "horizontal"
                i0 = 0 if horiz else 1
                ss = sorted(ss, key=lambda s: ed.fp(s)[i0])
                fps = [ed.fp(s) for s in ss]
                span = fps[-1][i0] + fps[-1][i0 + 2] - fps[0][i0]
                free = span - sum(f[i0 + 2] for f in fps)
                gapf = Fraction(free, len(ss) - 1)
                pos = Fraction(fps[0][i0])
                for s, f in zip(ss, fps):
                    ax, ay, w, h = ed.abs_box(s)
                    off = f[i0] - (ax if horiz else ay)
                    v = _snap(int(pos) - off, grid)
                    ed.set_abs(s, v, ay) if horiz else ed.set_abs(s, ax, v)
                    pos += f[i0 + 2] + gapf
                    pinned.add(str(s.get("id")))
                    cons.get(str(s.get("id")), {}).pop("x" if horiz else "y", None)
                moved = ss
                line = f"spaced {', '.join(nm(s) for s in ss)} evenly {'across' if horiz else 'down'}"
            elif ss:
                notes.append("spacing evenly needs at least three symbols")
        elif kind == "center":
            ss = targets(kind, _op_ids(op))
            axis = op.get("axis") or "both"
            for s in ss:
                # §6.5 item 1: centering on the canvas is a kept constraint per axis (what is
                # drawn, centered).
                sid_c = str(s.get("id"))
                c_s = cons.setdefault(sid_c, {})
                if axis in ("horizontal", "both"):
                    c_s["x"] = con("x", "center", ["canvas"], on="fp", desc="centered on the canvas")
                if axis in ("vertical", "both"):
                    c_s["y"] = con("y", "center", ["canvas"], on="fp", desc="centered on the canvas")
                pinned.add(sid_c)
                apply_cons(sid_c)
            if ss:
                moved = ss
                line = f"centered {', '.join(nm(s) for s in ss)} on the canvas" + \
                       ("" if axis == "both" else f" ({axis}ly)")
        elif kind == "make_room":
            # §6.5 item 1: "move X out of the way" — X goes to the nearest free area AT ITS OWN SIZE
            # (moving never shrinks, item 5), preferring the side of what it makes room for it is
            # on now; it stays movable for the rest of the batch (a later placement may move it on).
            ss = targets(kind, _op_ids(op))
            if ss:
                s = ss[0]
                sid_r = str(s.get("id"))
                fparts = [real(r) for r in _ref_list(op.get("from"))]
                frects = [b for b in (ref_rect(r) for r in fparts if r != sid_r and not _is_canvas_ref(r))
                          if b is not None]
                fname = name_of([r for r in fparts if (r != sid_r and ref_rect(r) is not None)]) if frects else ""
                avoid = frects
                side = None
                if frects:
                    x0, y0 = min(b[0] for b in frects), min(b[1] for b in frects)
                    side = (x0, y0, max(b[0] + b[2] for b in frects) - x0, max(b[1] + b[3] for b in frects) - y0)
                cons.pop(sid_r, None)
                pinned.discard(sid_r)
                f_now = ed.fp(s)
                if ed.conflicts(s) or any(_rect_overlap(f_now, b) for b in avoid):
                    if ed.free_spot(s, avoid=avoid, side_of=side) is None:
                        refuse(kind, sid_r, "no free area",
                               f"did not move {nm(s)} out of the way: there is no free area for it at its "
                               "size — make the canvas larger or remove something")
                    else:
                        line = f"moved {nm(s)} out of the way" + (f" of {fname}" if fname else "")
                        made_way[sid_r] = line
                else:
                    # Nothing is in its way yet (what it makes room for may be placed later in
                    # this batch); said once at the end — moved or already clear.
                    room_asked.append((sid_r, fname))
                    applied += 1
        elif kind == "swap":
            ss = targets(kind, [str(op["a"]).strip(), str(op["b"]).strip()])
            if len(ss) == 2:
                a, b = ss
                (ax, ay, _, _), (bx, by, _, _) = ed.abs_box(a), ed.abs_box(b)
                ed.set_abs(a, bx, by)
                ed.set_abs(b, ax, ay)
                for x in (a, b):
                    pinned.add(str(x.get("id")))
                    cons.pop(str(x.get("id")), None)
                moved = [a, b]
                line = f"swapped {nm(a)} and {nm(b)}"
        elif kind == "scale":
            ss = targets(kind, _op_ids(op))
            f = _frac(op["factor"])
            sized: List[Dict[str, Any]] = []
            how_s = ""
            for s in ss:
                ax, ay, w, h = ed.abs_box(s)
                minw, minh = ed.floor(s)
                ov = size_override(s, None, None)
                if ov is not None:
                    # §6.6 item 2: the size the request gives, not the model's factor
                    if (ov[0], ov[1]) == (w, h):
                        size_stays(kind, s, ov[2])
                        continue
                    nw, nh = ov[0], ov[1]
                    how_s = ov[2]
                    if how_s == "smaller" and (nw, nh) == (minw, minh):
                        notes.append(f"{nm(s)} stops at its smallest size, {nw} x {nh} px")
                elif f < 1:
                    nw, nh = _shrunk(w, h, f, minw, minh, grid)
                    if (nw, nh) != (_snap(int(w * f), grid), _snap(int(h * f), grid)):
                        notes.append(f"{nm(s)} stops at its smallest size, {nw} x {nh} px")
                else:
                    nw, nh = max(grid, _snap(int(w * f), grid)), max(grid, _snap(int(h * f), grid))
                s["w"], s["h"] = nw, nh
                ed.set_abs(s, _snap(ax + (w - nw) // 2, grid), _snap(ay + (h - nh) // 2, grid))
                ed.reshaped.add(str(s.get("id")))
                resized.add(str(s.get("id")))
                fitted.discard(str(s.get("id")))
                sized.append(s)
            if sized:
                moved, own = sized, False
                if how_s == "readable" and len(sized) == 1:
                    line = readable_line(sized[0])
                elif how_s in ("word", "smaller") and len(sized) == 1:
                    s0 = sized[0]
                    o0 = ed.orig.get(str(s0.get("id")), ed.abs_box(s0))
                    word = "bigger" if s0["w"] * s0["h"] > o0[2] * o0[3] else "smaller"
                    fx0 = Fraction(s0["w"], max(1, o0[2]))
                    tail = f"×{float(fx0):g}" if Fraction(s0["h"], max(1, o0[3])) == fx0 else \
                        f"{s0['w']} x {s0['h']} px"
                    line = f"made {nm(s0)} {word} ({tail})"
                elif how_s:
                    line = "resized " + ", ".join(f"{nm(s)} to {s['w']} x {s['h']} px" for s in sized)
                else:
                    word = "bigger" if f > 1 else "smaller"
                    line = f"made {', '.join(nm(s) for s in sized)} {word} (×{float(f):g})"
        elif kind == "nudge":
            ss = targets(kind, _op_ids(op))
            dx = int(_frac(op.get("dx") or 0)) * grid
            dy = int(_frac(op.get("dy") or 0)) * grid
            for s in ss:
                ax, ay, w, h = ed.abs_box(s)
                ed.set_abs(s, ax + dx, ay + dy)
                pinned.add(str(s.get("id")))
                cons.pop(str(s.get("id")), None)       # a nudge replaces a relation
            if ss:
                moved = ss
                dirs = ([f"{'right' if dx > 0 else 'left'} {abs(dx) // grid} step(s)"] if dx else []) + \
                       ([f"{'down' if dy > 0 else 'up'} {abs(dy) // grid} step(s)"] if dy else [])
                line = f"nudged {', '.join(nm(s) for s in ss)} {' and '.join(dirs) or 'nowhere'}"
        elif kind == "set":
            ss = targets(kind, _op_ids(op))
            descs: List[str] = []
            sprops: Dict[str, Any] = {}
            floor_wh = {str(x.get("id")): ed.floor(x) for x in ss}
            for s in ss:
                if display_pipes.is_pipe(s):           # D3.6: a pipe's label / line only
                    done_k, bad_k = display_pipes.set_pipe_props(s, normalize_props(op["props"]))
                    if done_k:
                        descs.append(f"{' and '.join(done_k)} of {nm(s)}")
                    notes.extend(f"{nm(s)}: {b}" for b in bad_k)
                    continue
                origin = ed.origin(s)
                sprops = props_of(op["props"], s)
                pw, ph = request_px(request)
                if pw is not None and "w" in sprops:
                    sprops["w"] = _Num(str(pw))
                if ph is not None and "h" in sprops:
                    sprops["h"] = _Num(str(ph))
                if "w" in sprops or "h" in sprops:
                    ww = _geom_value(sprops["w"], W, grid) if "w" in sprops else None
                    hh = _geom_value(sprops["h"], H, grid) if "h" in sprops else None
                    ov = size_override(s, ww, hh)
                    if ov is not None:
                        # §6.6 item 2: the size the request gives, not the model's numbers
                        sprops.pop("w", None)
                        sprops.pop("h", None)
                        if (ov[0], ov[1]) != (_int0(s.get("w")), _int0(s.get("h"))):
                            sprops["w"], sprops["h"] = _Num(str(ov[0])), _Num(str(ov[1]))
                            if ov[2] == "smaller" and (ov[0], ov[1]) == ed.floor(s):
                                settle_notes.append(f"{nm(s)} stops at its smallest size, {ov[0]} x {ov[1]} px")
                        else:
                            size_stays(kind, s, ov[2])
                for k_id in ("label", "text"):
                    if isinstance(sprops.get(k_id), str) and _ID_LIKE_RE.match(sprops[k_id]):
                        # an id where words belong (corpus C098): the request's quoted words, else nothing
                        said_q = _quoted_words(request) if s.get("type") == "text" else ""
                        if said_q:
                            sprops[k_id] = said_q
                        else:
                            sprops.pop(k_id)
                if s.get("type") == "text" and isinstance(sprops.get("label"), str) and "text" not in sprops:
                    # a text shows its text: "change the text to X" sets its words (battery E19)
                    sprops["text"] = sprops["label"]
                ign: List[str] = []
                done, bad = apply_props(s, sprops, request=request, W=W, H=H, grid=grid,
                                        origin=origin, g=g, images=images, ignored=ign)
                for b in bad:
                    refuse(kind, str(s.get("id")), b, f"could not set {nm(s)}: {b}")
                for k in ign:
                    notes.append(f"ignored '{k}' — Describe cannot set that on {_a_kind(s)}")
                if done:
                    descs.append(f"{nm(s)}: {', '.join(done)}")
                    sid_t = str(s.get("id"))
                    ed.reshaped.add(sid_t)
                    flat = dict(sprops, **(sprops.get("style") if isinstance(sprops.get("style"), dict) else {}))
                    if any(k in flat for k in ("x", "y")):
                        pinned.add(sid_t)
                        for k in ("x", "y"):
                            if k in flat:
                                cons.get(sid_t, {}).pop(k, None)
                    if any(k in flat for k in ("w", "h")):
                        resized.add(sid_t)
                        pinned.add(sid_t)
                        fitted.discard(sid_t)
                        fw, fh = floor_wh.get(sid_t) or floor_size(s, W, H, grid)
                        if _int0(s.get("w")) < fw or _int0(s.get("h")) < fh:   # §6.5 item 4
                            if explicit_size:
                                settle_notes.append(f"{nm(s)} is now smaller than is easy to read")
                            else:
                                s["w"], s["h"] = max(_int0(s.get("w")), fw), max(_int0(s.get("h")), fh)
                                settle_notes.append(f"{nm(s)} stops at its smallest size, {s['w']} x {s['h']} px")
                    elif sid_t in fitted and any(k in flat for k in ("font", "text")):
                        # §6.5 item 3: an added text stays fitted to its words at its font (its
                        # center kept; a relation it has is re-applied at the end of the batch)
                        ax, ay, w, h = ed.abs_box(s)
                        nw, nh = text_box(s, grid)
                        s["w"], s["h"] = min(nw, W // grid * grid), min(nh, H // grid * grid)
                        ed.set_abs(s, _snap(max(0, ax + (w - s["w"]) // 2), grid), ay)
                        apply_cons(sid_t)
                    if any(k in sprops for k in ("x", "y", "w", "h", "label", "text", "font", "style",
                                                 "decimals", "fill", "stroke", "align")):
                        moved.append(s)
            own = not any(k in sprops for k in ("w", "h"))
            if descs:
                line = "set " + "; ".join(descs)
        elif kind == "connect":
            # D3.6 (§5.7.10 item 9): an auto-routed pipe; exempt from the footprint rule (nothing to
            # settle); the request's own phrase for the same two symbols decides line / flow / label
            pipe, pnote = conn.from_op(op)
            if pipe is not None:
                put_pipe(pipe)
                line = pnote
            elif pnote:
                refuse(kind, "canvas", pnote.split(": ", 1)[-1], pnote)
        elif kind == "readable":
            # §6.6 item 2 (engine op): an existing symbol a readability phrase is about, smaller
            # than its kind's default, grows to the default (its center kept)
            for s in targets(kind, _op_ids(op)):
                t0 = existing_target(s)
                ax, ay, w, h = ed.abs_box(s)
                if t0 is None or (t0[0], t0[1]) == (w, h):
                    continue
                s["w"], s["h"] = t0[0], t0[1]
                ed.set_abs(s, _snap(max(0, ax + (w - t0[0]) // 2), grid), _snap(max(0, ay + (h - t0[1]) // 2), grid))
                ed.reshaped.add(str(s.get("id")))
                resized.add(str(s.get("id")))
                fitted.discard(str(s.get("id")))
                moved.append(s)
                line = readable_line(s)
            own = False
        elif kind == "restyle":
            ss = targets(kind, _op_ids(op))
            if ss:
                s = ss[0]
                done, bad = apply_props(s, props_of({"style": op.get("style") or {}}, s, targeted=False), request=request,
                                        W=W, H=H, grid=grid, g=g, images=images)
                for b in bad:
                    notes.append(f"could not restyle {nm(s)}: {b}")
                if done:
                    line = f"restyled {nm(s)}: {', '.join(done)}"
                    ed.reshaped.add(str(s.get("id")))
                else:
                    refuse(kind, str(s.get("id")), "no allowed style")
        elif kind == "rule":
            ss = targets(kind, _op_ids(op))
            if ss:
                ss[0]["rules"] = _map_rules(op.get("rules"))
                line = f"set the rules of {nm(ss[0])}"
        elif kind == "rebind":
            ss = targets(kind, _op_ids(op))
            if ss:
                s = ss[0]
                binds: List[Dict[str, Any]] = []
                missing: List[str] = []
                for n in op["bind"]:
                    b = res.resolve(n)
                    if b is None:
                        missing.append(n.strip())
                    elif b not in binds:
                        binds.append(b)
                capn = _DOC_BIND_MAX.get(str(s.get("type")))
                misfit: List[str] = []
                if s.get("type") == "equipment":
                    capn = glyph_bind_max(s.get("glyph"))
                    if not missing and len(binds) <= capn:
                        fit, misfit = equipment_binds(str(s.get("glyph")), binds, res)
                        if len(fit) == len(binds):
                            misfit = []
                if missing:
                    refuse(kind, str(s.get("id")), "not found: " + ", ".join(missing),
                           f"could not bind {nm(s)}: nothing here is called {', '.join(missing)}")
                elif capn is not None and len(binds) > capn:
                    refuse(kind, str(s.get("id")), f"{_a_kind(s)} takes at most {capn} binding(s)",
                           f"could not bind {nm(s)}: {_a_kind(s)} takes at most {capn} binding(s)")
                elif misfit:
                    refuse(kind, str(s.get("id")), "does not fit", f"could not bind {nm(s)}: {misfit[0]}")
                else:
                    was_unbound = not s.get("bind")
                    s["bind"] = binds
                    prefill_letters(s, res)            # D3.5 R10: an instrument bubble's letters
                    if was_unbound:
                        default_decimals(s, pts, ctx_el, g)
                    lbl = str(s.get("label") or "")
                    if lbl.startswith(NOT_FOUND):
                        s.pop("label", None)
                    elif " — " + NOT_FOUND in lbl:
                        s["label"] = lbl.split(" — " + NOT_FOUND, 1)[0]
                    line = f"bound {nm(s)} to {', '.join(n.strip() for n in op['bind'])}"

        fail_name = ""
        if moved and fail is None:
            why = settle(moved, op, own)
            if why:
                fail = (str(moved[0].get("id")), why)
                fail_name = nm(moved[0])
            elif kind == "add" and placed_later(idx, str(moved[0].get("id"))):
                # the add is placed by a later op of this batch: where it was drafted is not news
                settle_notes[:] = [n for n in settle_notes if not n.startswith("kept ")]
        if fail is not None:
            top[:] = before
            ed.reshaped, ed.ghosts = reshaped_before, ghosts_before
            placed_at.clear()
            placed_at.update(placed_before)
            cons.clear()
            cons.update(cons_before)
            pinned.clear()
            pinned.update(pinned_before)
            resized.clear()
            resized.update(resized_before)
            fitted.clear()
            fitted.update(fitted_before)
            made_way.clear()
            made_way.update(made_way_before)
            if kind == "add":
                placeholders[:] = [p for p in placeholders if p["symbol"] != fail[0]]
                added[:] = [a for a in added if str(a.get("id")) != fail[0]]
            verb = {"add": "add", "place": "move", "move": "move", "align": "align", "center": "center",
                    "make_room": "move",
                    "swap": "swap", "nudge": "nudge", "distribute": "space out", "scale": "resize",
                    "match": "resize", "resize": "resize", "set": "change", "readable": "resize"}.get(kind, kind)
            what = f"{verb} {fail_name or fail[0]}"
            if kind == "place" and op.get("where") != "at":
                what = would_do(op)                    # the goal, not "move": it may move for another reason
            refuse(kind, fail[0], fail[1], f"did not {what}: {fail[1]}")
            continue
        if line:
            notes.append(line)
            applied += 1
        notes.extend(settle_notes)

    # Settled at the D5.4 live battery (E47: the model dropped the `set`): the font size and color
    # the request says for "the font" / "the text", when this batch added exactly one text and the
    # model gave it neither, are applied to that text (its box refitted; relations re-applied below).
    new_texts = [x for x in (_find(i) for i in added_order) if x is not None and x.get("type") == "text"]
    if len(new_texts) != 1:
        new_texts = [x for x in (_find(i) for i in from_request) if x is not None]
    for tx in new_texts:
        st = dict(tx.get("style") or {})
        said_bits: List[str] = []
        f_said = request_font(request)
        if f_said and not st.get("font"):
            st["font"] = f_said
            said_bits.append(f"font {f_said}")
        if not st.get("color"):
            for sent in _SENTENCE_RE.split(request or ""):
                if not re.search(r"\b(?:font|text|words|letters|lettering)\b", sent, re.I):
                    continue
                m = re.search(r"\b(" + "|".join(re.escape(n) for n in sorted(COLOR_NAMES, key=len, reverse=True))
                              + r")\b", sent, re.I)
                if m:
                    c, _why = color_value(m.group(1), request)
                    if c:
                        st["color"] = c
                        said_bits.append(f"color {m.group(1).lower()}")
                    break
        if said_bits:
            tx["style"] = st
            ed.reshaped.add(str(tx.get("id")))
            if str(tx.get("id")) in fitted:
                ax, ay, w, h = ed.abs_box(tx)
                nw, nh = text_box(tx, grid)
                tx["w"], tx["h"] = min(nw, W // grid * grid), min(nh, H // grid * grid)
                ed.set_abs(tx, _snap(max(0, ax + (w - tx["w"]) // 2), grid), ay)
                ed.clamp(tx)
                if ed.conflicts(tx) and str(tx.get("id")) not in cons:
                    ed.settle(tx, own=True)
            notes.append(f"set {nm(tx)}: {', '.join(said_bits)}")
    for sid in deferred:                               # an add whose place did not happen
        x = _find(sid)
        if x is not None and ed.conflicts(x):
            ed.clamp(x)
            why, more = ed.settle(x, may_shrink=[], own=True)
            if why:
                # §6.4: a refused step never leaves an overlap. The add's place was refused and it
                # does not fit anywhere: it is not added (live battery E28, found at the D3.5 gate:
                # this path used to keep the symbol overlapping with an "overlaps" note).
                for lst in [top] + [s.get("children") for s in ed._all() if isinstance(s.get("children"), list)]:
                    if x in lst:
                        lst.remove(x)
                        break
                placeholders[:] = [p for p in placeholders if p["symbol"] != sid]
                added[:] = [a for a in added if str(a.get("id")) != str(sid)]
                notes[:] = [n for n in notes if not (n.startswith("added ") and f"'{x.get('label')}'" in n)]
                applied -= 1
                refuse("add", str(sid), why, f"did not add {nm(x)}: {why}")
                continue
            notes.extend(more)
    # Settled after Fable 5.1's review: added symbols of one kind asked bigger scale together (all
    # at the smallest size any of them had to take to fit, centers kept — shrinking in place adds
    # no overlap); each one smaller than asked says "made <name> as large as fits".
    big_by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for sid in asked_big:
        x = _find(sid)
        if x is not None:
            big_by_kind.setdefault(str(x.get("type")), []).append(x)
    for xs in big_by_kind.values():
        if len(xs) < 2:
            continue
        mw, mh = min(_int0(x.get("w")) for x in xs), min(_int0(x.get("h")) for x in xs)
        for x in xs:
            ax, ay, w, h = ed.abs_box(x)
            if (w, h) != (mw, mh):
                x["w"], x["h"] = mw, mh
                ed.set_abs(x, _snap(ax + (w - mw) // 2, grid), _snap(ay + (h - mh) // 2, grid))
                ed.reshaped.add(str(x.get("id")))
    for sid, (aw, ah) in asked_big.items():
        x = _find(sid)
        if x is not None and (_int0(x.get("w")), _int0(x.get("h"))) != (aw, ah):
            notes.append(f"made {nm(x)} as large as fits")
    # §6.5 item 2: the batch's constraints are KEPT, not just applied once — re-applied in order
    # (a reference may have moved or been resized after the relation was made, a text refitted),
    # whatever that disturbs is settled again under the same rules, and every constraint is
    # re-verified exactly at the end; one that could not be kept says so, never a silent drift.
    if cons:
        start_boxes = {sid: ed.abs_box(x) for sid in cons for x in [_find(sid)] if x is not None}
        touched: List[str] = []
        for _round in range(4):
            changed = False
            for sid in list(cons):
                x = _find(sid)
                if x is None or is_locked(x):
                    continue
                b0 = ed.abs_box(x)
                apply_cons(sid)
                if ed.abs_box(x) != b0:
                    changed = True
                    if sid not in touched:
                        touched.append(sid)
            if not changed:
                break
        for sid in touched:                            # a permitted pair is no conflict (§6.6 item 1)
            x = _find(sid)
            if x is None:
                continue
            moved_ref = shift_reference(x)
            if moved_ref:
                notes.append(moved_ref)
            if not ed.conflicts(x):
                continue
            ed.last_moved = []
            why, more = ed.settle(x, own=False, fixed=fixed_of(sid), movable=movable)
            if why is None:
                note_made_way(more)
                notes.extend(more)
                continue
            b = start_boxes.get(sid)
            if b is not None:
                ed.set_abs(x, b[0], b[1])              # back where its own op left it
            if ed.conflicts(x):
                why, more = ed.settle(x, own=False)
                notes.extend(more)
        for sid, axes in list(cons.items()):
            x = _find(sid)
            if x is None:
                continue
            bad = [a for a in axes if not cons_kept(sid, a)]
            if not bad:
                continue
            # Why: where the constraint wanted it, what is there (or the canvas edge).
            b0 = ed.abs_box(x)
            tx, ty = b0[0], b0[1]
            for a in bad:
                p = cons_want(axes[a], x)
                if p is not None:
                    if a == "x":
                        tx = snapf(p)
                    else:
                        ty = snapf(p)
            outside = tx < 0 or ty < 0 or tx + b0[2] > W or ty + b0[3] > H
            ed.set_abs(x, tx, ty)
            hit = [] if outside else ed.conflicts(x)
            ed.set_abs(x, b0[0], b0[1])
            if outside:
                why_k = "it would not fit inside the canvas there"
            elif hit:
                why_k = f"{', '.join(nm(o) for o in hit[:3])} {'is' if len(hit) == 1 else 'are'} in the way"
            else:
                why_k = "another relation in this request moved it"
            notes.append(f"could not keep {nm(x)} {axes[bad[0]].get('desc') or 'where it was asked'}: {why_k}")
    for sid, fname in room_asked:
        x = _find(sid)
        if x is None or sid in made_way:
            continue
        # one note, and only when it moved (live battery E54: "move whatever is in the way")
        if ed.abs_box(x)[:2] != ed.orig.get(sid, ed.abs_box(x))[:2] and \
                not any(n.startswith(f"moved {nm(x)} ") for n in notes):
            notes.append(f"moved {nm(x)} out of the way" + (f" of {fname}" if fname else ""))
    # D3.6: the request's connect phrases no op covered ("connect A to B" is recognized
    # deterministically); then removed ends (item 3) and the hulls of Describe's pipes
    for pipe, pnote in conn.rest():
        if pipe is not None:
            put_pipe(pipe)
            notes.append(pnote)
            applied += 1
        elif pnote:
            refuse("connect", "canvas", pnote.split(": ", 1)[-1], pnote)
    gone_ids = {str(x.get("id")) for x in _walk(before_all) if isinstance(x, dict)} - \
        {str(x.get("id")) for x in _walk(top) if isinstance(x, dict)}
    if gone_ids:
        notes.extend(display_pipes.detach_removed(
            top, {i: ed.orig[i] for i in gone_ids if i in ed.orig},
            name_of=lambda i: str(ed.ghosts.get(i, {}).get("name") or "its equipment")))
    display_pipes.refresh_boxes(
        top, lambda i: ed.abs_box(_find(i)) if _find(i) is not None else None, ed.origin,
        moved=lambda i: _find(i) is not None and ed.abs_box(_find(i)) != ed.orig.get(i))
    _final_notes(notes, n0, refused, refusal_of, ed, nm)

    if added:
        grouped = _group([a for a in added if any(a is x for x in top)])
        for gsym in grouped:
            if gsym.get("type") == "group" and gsym.get("children"):
                first = gsym["children"][0]
                pos = next(i for i, s in enumerate(top) if s is first)
                for c in gsym["children"]:
                    top.remove(c)
                gsym["id"] = f"s{nxt}"
                nxt += 1
                top.insert(min(pos, len(top)), gsym)
    for s in _walk(top):
        s.pop("_group", None)
    for nm, dss in res.ambiguous.items():
        notes.append(f"{nm} is a tag in several datasets ({', '.join(dss)}); name it as "
                     f"<dataset>.{nm} to bind it")
    if range_missing:
        notes.append("set the range (min / max) of: " + ", ".join(range_missing))
    if not applied:
        for phrase in absent_references(request, before_all):
            notes.append(f"there is no {phrase} on this display — it has: {_holdings(before_all, names=ed.names)}")
        # Settled at the D5.3 live battery: a request about a LOCKED symbol that changed nothing
        # (the model returned no op for it) says so, naming it — not the generic line.
        told = False
        for s in request_references(request, top, ed, ()):
            if is_locked(s):
                if not any(r.get("symbol") == str(s.get("id")) and r.get("reason") == "locked"
                           for r in refused):
                    refuse("edit", str(s.get("id")), "locked", lock_note(s))
                told = True
        # The closing line never repeats a reason the notes above already explain.
        if unexplained:
            why = "; ".join(unexplained[:3])
        elif refused or told:
            why = "see the notes above"
        else:
            why = "the request did not ask for a change I can make"
        notes.append(f"no change was made — {why}")
    doc["next_symbol_id"] = max(nxt, _as_int(doc.get("next_symbol_id")) or 1)
    return {"display": doc, "placeholders": placeholders, "refused_ops": refused, "changed": applied > 0}


_PROP_KEYS = frozenset(("props", "style", "color", "fill", "stroke", "decimals", "prefix", "suffix",
                        "font", "align", "stroke_width", "orientation", "window", "y_axis", "colors",
                        "show", "rows", "element", "text", "shape", "flow", "image", "nav"))


def _symbol_props(s: Dict[str, Any]) -> Dict[str, Any]:
    """A drafted symbol's properties-panel fields: its `props`, plus any panel field written on the
    symbol itself (a model may put `"color": "green"` there)."""
    out: Dict[str, Any] = {}
    for k in _PROP_KEYS - {"props"}:
        if k in s and s[k] is not None:
            out[k] = s[k]
    if isinstance(s.get("props"), dict):
        out.update(s["props"])
    return out


__all__ = [
    "draft_display", "preview_grounding", "AUTHORING_SCHEMA", "OPS_SCHEMA", "check_authoring", "check_ops",
    "nouns_of", "tidy", "fallback_layout", "name_key", "CANVAS_W", "CANVAS_H", "GRID",
    "user_names", "apply_user_names", "caps_lifted", "SIZE_CAPS", "LIFT_WORDS", "EXAMPLE_NAMES",
    "GLYPHS", "GLYPH_CLASS", "EQUIPMENT_SIZES", "EQUIPMENT_FLOORS", "glyph_of", "request_glyphs",
    "decide_glyphs", "normalize_equipment", "equipment_binds",
]
