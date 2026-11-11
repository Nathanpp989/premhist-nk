"""
calc_authoring.py — "describe a calc": the LLM drafts, PremHist judges, a human approves.

design_calc.md §6 (the authoring flow and its defenses), §10.22 (the C4 build contract), §10.30
Q-D; rulings R26 / R119 (the read-back comes from the calc, never the LLM), R50 (unit aliases),
R58, R68 (`empty_window_at_trigger` goes back to the model like an error), R75 (automatic unit
conversion), R170 (the wire: PremHist/API.md §11). The pattern is `task_authoring.py` /
`display_authoring.py`:

- **Ground** in what exists and nothing else: the scope's template(s) — or the element — with
  each attribute's kind, units and category (and the bound tag's eng units when they differ),
  the templates of the scope's children (for rollups), the site's zone and shift pattern, and
  PremHist's closed vocabulary `GET /api/v1/calcs/catalog`. The model is never shown an
  attribute that does not exist, so it cannot invent a tag.
- **Draft**: the model fills a small JSON schema (`DRAFT_SCHEMA`: intent, name, trigger, body,
  outputs) — Ollama `format`-enforced. The scope is never the model's: it comes from the caller.
- **What it runs on (R239–R244, `calc_scope.py`, Jim 2026-10-23)**: the whole plant is indexed once
  per request (templates, elements, bound tags — GETs only). On specific tags the prompt carries
  the compact asset map (`SIM-TI-401.PV = Pump P-501 | Bearing Temp`). A deterministic planner —
  never the model — decides whether the request fits the scope it came with; when another scope
  fits better the answer is a PROPOSAL (`code: "scope_proposal"`, a draft for that scope) or a
  QUESTION (`code: "choose_scope"`, two readings), applied only on the person's click
  (`context.scope_try`; `context.keep_scope` = draft where it is). A refusal for want of a source
  in this scope looks in the whole plant first (a source elsewhere is a proposal); requests the
  whole plant cannot serve are refused before any model call.
- **Validate** through PremHist (`validate`, injected; default `ground.validate_calc` = the
  write-free `POST /api/v1/calcs/validate`). **PremHist is the only judge**: nothing here decides
  that a body is right. Problems go back to the model with their exact text (line, col, code,
  message), at most two repair rounds; R68's warning goes back like an error with its hint.
- **Never shows a draft with errors as a success**: `ok: true` only when PremHist answered with
  no error, a hash, a read-back and at least one resolvable instance.
- **Never saves**: no draft PUT, no create, no approve. The viewer puts the result into its editor;
  the user dry-runs and approves as usual (C3's gating).
- **SQC (C6, design_calc.md §10.36.20)**: the model may add a `chart` (a control chart). Limit
  numbers are written ONLY when the request states them (`_limit_guard` drops any other number
  before PremHist sees the draft), limit attributes only when they exist in the grounding;
  otherwise the limits are left out, and a draft whose only error is `sqc_limits_missing` is the
  outcome `needs_limits` (`{ok: false, code: "needs_limits", document, readback}`) — never a
  success and never "repaired" by inventing numbers.

The one public entry point is the coroutine `draft_calc(...)`. It never raises.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import calc_describe_log as _dlog
from . import calc_scope as _cs

MAX_REQUEST_CHARS = 4000
MAX_TRIES = 3                      # one draft + at most two repairs (design_calc §10.22 step 3)
NUM_PREDICT = 2000
MAX_PROMPT_TOKENS = 9000           # estimated as characters / 4; above it → too_broad
MAX_ATTRIBUTES = 150               # per template / element shown to the model
MAX_CHILD_TEMPLATES = 6            # templates of the scope's children (rollups)
MAX_SAMPLE_ELEMENTS = 10           # elements whose site / bound tags are read
MAX_POINTS = 40                    # points scope: tags found by the request's words
MAX_FAMILIES = 40                  # points scope: the plant's tag families shown
MAX_SUGGESTIONS = 3                # a refusal's "you could compute instead"
MAX_PROBLEMS_SENT = 12             # problems quoted back to the model per repair
# No repair round STARTS after this many seconds: ph-server's Describe forward gives up at 180 s
# (PremHist/API.md §11.3.1), and a late answer is worse than an honest "couldn't draft this".
TOTAL_BUDGET_S = 150.0

_OLLAMA_TIMEOUT_SENTINEL = "My local LLM backend (Ollama) didn't respond"

# R68 (design_calc.md): the repair round sends this warning like an error, with this hint.
EMPTY_WINDOW_HINT = ("the window is empty at the trigger time — for the previous day use 'y' and "
                     "'t' (yesterday's midnight to today's midnight); for the shift that just ended "
                     "use shift_start and shift_end")
_HINTS = {
    "empty_window_at_trigger": EMPTY_WINDOW_HINT,
    "unknown_reference": "use only attribute names listed under ATTRIBUTES, spelled exactly as listed",
    "unknown_function": "use only functions listed under FUNCTIONS",
    "no_shift_calendar": "this site has no shift pattern: use a daily or every trigger, not shift times",
    "unit_mismatch": "values of different dimensions cannot be added or compared; Convert only "
                     "changes units within one dimension (a plain ratio as a percentage is x * 100[%])",
    "cond_needs_every": "a condition that contains a window function needs every: (e.g. every: 1m)",
    "window_out_of_bounds": "a window reaches further back than max_lookback; set max_lookback "
                            "(at most the catalog's max_lookback_max) or shorten the window",
    "total_needs_rate": "this input is not a rate: write TagTot(x, s, e, per: 1h) — the total is then "
                        "in its unit times hours (e.g. %·h); do not convert it to something else",
    "output_has_history": "that attribute already holds plant data: name a NEW output attribute (on "
                          "specific tags: a NEW tag name) — a clear name for the result — in outputs "
                          "and in the output statement",
    "output_owned": "that attribute is another calc's output: name a NEW output attribute instead",
    "cycle": "the calc writes an attribute it reads: name a NEW output attribute instead",
    "unassigned_output": "every outputs entry needs its own `output 'Name' = ...;` statement with the "
                         "same name",
    "undefined_name": "an output statement must name an attribute listed in outputs",
    "not_a_tag": "that attribute is a static value: name a NEW output attribute instead",
    "no_active_instance": "no element has a tag for an input this calc reads; if the request cannot "
                          "be done without it, set \"cannot\"",
    "sqc": "fix the chart object exactly as the message says (presets N1-N8 / WE1-WE4 take only id "
           "and severity; never two rules with the same code)",
    "sqc_function": "the SQC functions belong only in the body of a calc with a chart; prefer role "
                    "outputs (\"role\": \"status\") over SqcStatus() in the body",
    "sqc_needs_history": "give the chart a history, e.g. \"history\": \"24h\"",
}
# Exact fixes for the recurring syntax slips of small models (the C4 battery), by message text.
_MESSAGE_HINTS = [
    (re.compile(r"unexpected character `&`"), "write the word `and`, never `&` or `&&`"),
    (re.compile(r"after `\|`, found `\|`|unexpected character `\|`|found `\|`"),
     "write the word `or`, never `|` or `||` (`|` only appears in a rollup path c|'Attr')"),
    (re.compile(r"unexpected character `!`"), "write `not` / `<>`, never `!` / `!=`"),
    (re.compile(r"found name `NoOutput`|`NoOutput` .*statement", re.I),
     "NoOutput() is an expression, not a statement: write `output 'X' = if cond then value else NoOutput();`"),
    (re.compile(r"unit is written as a string literal"),
     "write a quantity with the unit in brackets: 85[°C], 10[m3/h] (a string is only for Convert)"),
    (re.compile(r'State\("name"\) takes a string literal'),
     'write State("Running") with the state name as a quoted string, or compare with = "Running"'),
    (re.compile(r"`dir:` takes one of the strings"), 'write dir: "up" (or "down", "both") with quotes'),
    (re.compile(r"`(t|y|bod|bow|bom|boy|now|today|yesterday)` is not defined"),
     "a time word is a quoted time expression: write 'y' and 't' in single quotes, e.g. "
     "TimeGT('Level', 'y', 't', 80[%])"),
]
# Warnings the model is asked to fix (R68); every other warning is passed through to the user.
_REPAIRABLE_WARNINGS = frozenset({"empty_window_at_trigger"})

_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_STR = {"type": "string"}
TRIGGER_SCHEMA: Dict[str, Any] = {"anyOf": [
    {"type": "object", "properties": {"every": _STR, "offset": _STR},
     "required": ["every"], "additionalProperties": False},
    {"type": "object", "properties": {"daily": {"type": "array", "items": _STR, "minItems": 1}},
     "required": ["daily"], "additionalProperties": False},
    {"type": "object", "properties": {"weekly": {
        "type": "object", "properties": {"days": {"type": "array", "minItems": 1,
                                                  "items": {"type": "string", "enum": _WEEKDAYS}},
                                         "at": _STR},
        "required": ["days", "at"], "additionalProperties": False}},
     "required": ["weekly"], "additionalProperties": False},
    {"type": "object", "properties": {"monthly": {
        "type": "object", "properties": {"day": {"type": "integer"}, "at": _STR},
        "required": ["day", "at"], "additionalProperties": False}},
     "required": ["monthly"], "additionalProperties": False},
    {"type": "object", "properties": {"shift": {"type": "string", "enum": ["start", "end"]}},
     "required": ["shift"], "additionalProperties": False},
    {"type": "object", "properties": {"on_input": {"anyOf": [
        {"type": "array", "items": _STR, "minItems": 1}, {"type": "string", "enum": ["all"]}]}},
     "required": ["on_input"], "additionalProperties": False},
]}
OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "attribute": _STR,
        "point": {"type": "integer"},
        "role": {"type": "string", "description": "only on a chart calc: value, cl, ucl, lcl, zone, "
                                                  "status, flags, severity or rule:<id>"},
        "write": {"type": "string", "enum": ["always", "on_change"]},
        "on_bad": {"type": "string", "enum": ["write", "skip"]},
    },
    "additionalProperties": False,
}
# C6 (§10.36.2): a control chart. Limits are left OUT unless the request states them (or names
# attributes that exist): the engine refuses invented numbers (`_limit_guard`).
CHART_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "only for a control chart (SQC): the plotted subgroup, the limits, the rules",
    "properties": {
        "subgroup": {"type": "object"},
        "limits": {"type": "object", "properties": {"cl": _STR, "ucl": _STR, "lcl": _STR},
                   "additionalProperties": False},
        "limit_change": {"type": "string", "enum": ["reset", "continue"]},
        "history": _STR,
        "rules": {"type": "array", "items": {"type": "object"}, "minItems": 1},
    },
    "required": ["subgroup", "rules"],
    "additionalProperties": False,
}
NEED_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "quantity": {"type": "string", "description": "a physical quantity the request needs, e.g. volume flow"},
        "unit_kind": {"type": "string", "description": "its kind of unit, e.g. volume/time, power, %"},
        "attribute": {"type": ["string", "null"],
                      "description": "the listed attribute or tag name that holds it, or null"},
    },
    "required": ["quantity", "unit_kind", "attribute"],
    "additionalProperties": False,
}
DRAFT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "description": "one line: what the calc computes, in plain words"},
        "topic": {"type": "string", "description": "2-5 words naming what is computed, e.g. pump efficiency"},
        "needs": {"type": "array", "items": NEED_SCHEMA,
                  "description": "FIRST: every quantity the request needs, each mapped to a listed name or null"},
        "name": {"type": "string", "description": "a short name for the calc"},
        "trigger": TRIGGER_SCHEMA,
        "body": {"type": "string", "description": "the calc text: let / output statements"},
        "outputs": {"type": "array", "items": OUTPUT_SCHEMA, "minItems": 1},
        "max_lookback": {"type": "string", "description": "only when a window reaches back more than 31d"},
        "chart": CHART_SCHEMA,
        "cannot": {"type": "string", "description": "only when the request cannot be done here: why"},
    },
    "required": ["intent", "topic", "needs", "name", "trigger", "body", "outputs"],
    "additionalProperties": False,
}

_STOP = frozenset("""
a an the of for to and or in on at by per each every all from with into over under than then
is are be was were it its this that these those as if when while which what how much many
calc calculate calculation compute total totals count number value values give me i want need
please make create write show average avg sum min max hour hours day days daily shift shifts
week weekly month monthly minute minutes second seconds above below more less over under
""".split())
_WORD_RE = re.compile(r"[\w\-./]+", re.UNICODE)


def name_key(s: str) -> str:
    # R252: Unicode dashes / hyphens and no-break / narrow spaces fold to ASCII first
    # ("SIM‑TI‑401.PV" is SIM-TI-401.PV).
    return _cs.name_key(s)


# ── small helpers ─────────────────────────────────────────────────────

async def _g(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call one `ground` / `validate` method off the event loop (PremhistRest blocks); await it
    when it returns an awaitable."""
    res = await asyncio.to_thread(fn, *args, **kwargs)
    if inspect.isawaitable(res):
        res = await res
    return res


def _sha(messages: List[Dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _dedupe(items: Iterable[str]) -> List[str]:
    seen: set = set()
    return [x for x in items if not (x in seen or seen.add(x))]


def _clean(s: Any, limit: int = 200) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(s if s is not None else "")).strip()[:limit]


def _words(request: str) -> List[str]:
    out: List[str] = []
    for w in _WORD_RE.findall(_cs.fold_text(request or "")):
        w = w.strip(".-/")
        if len(w) < 3 or w.casefold() in _STOP or w.replace(".", "").isdigit():
            continue
        if w.casefold() not in (x.casefold() for x in out):
            out.append(w)
    return out[:12]


def _ts_ns(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class _LlmUnavailable(Exception):
    pass


class _PremhistDown(Exception):
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
        if "body" in res and "trigger" in res:
            return json.dumps(res)
    return ""


def _parse_reply(content: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """The model's reply → (object, None) or (None, error). A fenced or prefixed reply is read by
    taking the outermost {...} — deterministic, never a guess at content."""
    t = (content or "").strip()
    if not t:
        return None, "the reply was empty"
    if t.startswith("```"):
        t = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    obj: Any = None
    try:
        obj = json.loads(t)
    except ValueError as e:
        first, last = t.find("{"), t.rfind("}")
        if 0 <= first < last:
            try:
                obj = json.loads(t[first:last + 1])
            except ValueError:
                obj = None
        if obj is None:
            return None, f"the reply is not valid JSON ({e})"
    if not isinstance(obj, dict):
        return None, "the reply must be one JSON object"
    return obj, None


def _round_ceiling_s() -> float:
    """The worst case wall time of ONE model round. llm_runtime retries once on a failure, so a
    round costs up to 1.35x the configured per-call timeout: a deadline check that compares only
    the elapsed time still lets a round start that cannot finish in what is left (a round begun
    at 149.9 s of a 150 s budget ran on to ~312 s, well past the 180 s Describe forward)."""
    try:
        from .settings import get_settings
        t = int(getattr(get_settings(), "calc_authoring_timeout_s", 120))
    except Exception:
        t = 120
    try:
        from .llm_runtime import max_call_seconds
        return float(max_call_seconds(t))
    except Exception:
        return float(t + max(15, int(t * 0.35)))


def _default_llm_call(call_ollama_chat: Callable[..., Any]) -> Callable[..., Any]:
    """`call_ollama_chat` with the per-call timeout `settings.calc_authoring_timeout_s` (default
    120) and, for gpt-oss models, `think="low"` (as display authoring)."""
    try:
        from .settings import get_settings
        timeout_s = int(getattr(get_settings(), "calc_authoring_timeout_s", 120))
    except Exception:
        timeout_s = 120

    def call(messages: List[Dict[str, Any]], *, format: Any, model: str, temperature: float,
             options: Dict[str, Any]) -> Any:
        extra: Dict[str, Any] = {"think": "low"} if "gpt-oss" in name_key(model or "") else {}
        return call_ollama_chat(messages, model=model, temperature=temperature, timeout_s=timeout_s,
                                format=format, options=options, **extra)

    return call


# ── grounding ─────────────────────────────────────────────────────────

class _Grounding:
    def __init__(self) -> None:
        self.scope: Dict[str, Any] = {}
        self.scope_text = ""
        self.template_name: Optional[str] = None       # template scope: for "runs on 4 of 10 pumps"
        self.attributes: List[Dict[str, Any]] = []        # the scope's own attributes (prompt rows)
        self.child_templates: List[Dict[str, Any]] = []   # templates of the scope's children
        self.points: List[Dict[str, Any]] = []            # points scope: tags found by request words
        self.all_points: List[Dict[str, Any]] = []        # points scope: every tag (families, refusals)
        self.plant_templates: List[Dict[str, Any]] = []   # points scope: the plant's templates
        self.sites: List[Dict[str, Any]] = []             # the site(s) of the scope's instances
        self.catalog: Dict[str, Any] = {}
        self.element_count: Optional[int] = None
        self.notes: List[str] = []
        self.index: Optional[_cs.PlantIndex] = None       # the whole plant (R239), or None
        self.asset_lines: List[str] = []                  # points scope: tag = element | attribute
        self.asset_shown = 0
        self.asset_total = 0
        # R251: how much of a large plant the points-scope prompt shows (None = all), so the
        # prompt fits MAX_PROMPT_TOKENS on a larger store; matching still uses every tag.
        self.points_cap: Optional[int] = None
        self.families_cap = MAX_FAMILIES
        self.templates_cap: Tuple[int, int] = (12, 12)

    def attribute_names(self) -> List[str]:
        names = [a["name"] for a in self.attributes]
        for t in self.child_templates:
            names.extend(a["name"] for a in t["attributes"])
        return names

    def _rows(self) -> List[Dict[str, Any]]:
        return list(self.attributes) + [a for t in self.child_templates for a in t["attributes"]]

    def input_names(self) -> List[str]:
        """Every name a need may map to: the scope's attributes (+ children's) or, on specific
        tags, the tags (found and all)."""
        names = self.attribute_names()
        for p in self.points + self.all_points:
            if p.get("name"):
                names.append(str(p["name"]))
        return _dedupe(names)

    def units_of(self, name: str) -> Tuple[Optional[str], Optional[str]]:
        """(units, point type) of a listed attribute or tag, by name."""
        k = name_key(name)
        for a in self._rows():
            if name_key(a.get("name")) == k:
                return (a.get("tag_units") or a.get("units")), a.get("point_type")
        for p in self.points + self.all_points:
            if name_key(p.get("name")) == k:
                return p.get("eng_units"), p.get("point_type")
        return None, None

    def universe(self) -> str:
        """Everything the grounding says exists, as one lower-case text (absence checks)."""
        bits = [self.scope_text, self.template_name or ""]
        bits += [str(a.get("name")) for a in self._rows()] + [str(t.get("name")) for t in self.child_templates]
        for p in self.points + self.all_points:
            bits += [str(p.get("name") or ""), str(p.get("description") or "")]
        for t in self.plant_templates:
            bits.append(str(t.get("name") or ""))
            bits += [str(a.get("name")) for a in t.get("attributes") or [] if isinstance(a, dict)]
        if self.index is not None:
            bits.append(self.index.universe())
        return " ".join(bits).casefold()


def _attr_row(d: Dict[str, Any], bound: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One attribute for the prompt: name, kind, units, category; for a tag attribute whether a
    tag is bound on the sample element (an input) or not (a calc output creates its tag), and the
    bound tag's eng units when they differ from the attribute's units."""
    row: Dict[str, Any] = {"name": str(d.get("name")), "kind": str(d.get("kind") or ""),
                           "units": d.get("units"), "category": d.get("category")}
    if bound is not None:
        v = bound.get("value") if isinstance(bound.get("value"), dict) else {}
        kind = v.get("kind")
        if kind == "tag":
            row["bound"] = not v.get("missing")
            row["point"] = v.get("point")
            tag = v.get("tag") if isinstance(v.get("tag"), dict) else {}
            if tag.get("eng_units") and tag.get("eng_units") != d.get("units"):
                row["tag_units"] = tag.get("eng_units")
            if tag.get("point_type"):
                row["point_type"] = tag.get("point_type")
            if tag.get("name") and not v.get("missing"):
                row["tag_name"] = str(tag.get("name"))
        elif kind == "unbound":
            row["bound"] = False
        elif kind == "static":
            row["kind"] = "static"
    return row


def _rank_attrs(rows: List[Dict[str, Any]], words: Sequence[str]) -> List[Dict[str, Any]]:
    """Every row when they fit; else the rows a request word names first (then by name)."""
    if len(rows) <= MAX_ATTRIBUTES:
        return rows
    ws = [w.casefold() for w in words]
    scored = sorted(rows, key=lambda r: (-sum(1 for w in ws if w in r["name"].casefold()),
                                         name_key(r["name"])))
    return scored[:MAX_ATTRIBUTES]


def _site_of(element_id: Optional[int], parents: Dict[int, Optional[int]],
             assigned: Dict[int, Optional[int]]) -> int:
    """design_calc §10.6: the nearest ancestor-or-self with an assignment, else site 1."""
    seen: set = set()
    e = element_id
    while e is not None and e not in seen:
        seen.add(e)
        if e in assigned:
            return assigned[e] if assigned[e] is not None else 1
        e = parents.get(e)
    return 1


def _assignments_now(sites_doc: Dict[str, Any], now_ns: int) -> Dict[int, Optional[int]]:
    """element → the site assigned in force now (R30: the greatest effective_from ≤ now)."""
    out: Dict[int, Optional[int]] = {}
    for a in sites_doc.get("assignments") or []:
        if not isinstance(a, dict) or not isinstance(a.get("element"), int):
            continue
        best: Optional[Tuple[int, Any]] = None
        for h in a.get("history") or []:
            if not isinstance(h, dict):
                continue
            t = _ts_ns(h.get("effective_from_ns"))
            if t is None or t > now_ns:
                continue
            if best is None or t >= best[0]:
                best = (t, h.get("site"))
        if best is not None:
            out[a["element"]] = best[1] if isinstance(best[1], int) else None
    return out


async def _mark_owned(ground: Any, rows: List[Dict[str, Any]]) -> None:
    """A bound tag that is a calc's output (the point's `calc` field, API.md §11.12) is marked so
    the model reads it but never writes it (it would be `output_owned`). Best effort."""
    fn = getattr(ground, "point", None)
    if fn is None:
        return
    for r in [r for r in rows if r.get("bound") and isinstance(r.get("point"), int)][:60]:
        try:
            p = await _g(fn, r["point"])
        except Exception:
            continue
        c = p.get("calc") if isinstance(p, dict) else None
        if isinstance(c, dict):
            r["owner"] = _clean(c.get("name"), 80) or f"#{c.get('id')}"


async def _ground(ground: Any, scope: Dict[str, Any], request: str,
                  index: Optional[_cs.PlantIndex] = None) -> _Grounding:
    g = _Grounding()
    g.scope = scope
    g.index = index
    words = _words(request)
    g.catalog = dict(await _g(ground.calc_catalog) or {})
    sample_ids: List[int] = []
    if "template" in scope:
        t = await _g(ground.template, int(scope["template"]))
        if not isinstance(t, dict):
            raise ValueError(f"template {scope['template']} does not exist")
        ids = list(await _g(ground.template_elements, int(scope["template"])) or [])
        g.element_count = len(ids)
        sample_ids = ids[:MAX_SAMPLE_ELEMENTS]
        sample = await _g(ground.element, sample_ids[0]) if sample_ids else None
        by_name = {name_key(a.get("name")): a for a in (sample or {}).get("attributes") or []
                   if isinstance(a, dict)}
        g.template_name = str(t.get("name") or "") or None
        g.scope_text = (f"template \"{t.get('name')}\" — the calc runs once for each of its "
                        f"{len(ids)} element(s)")
        if isinstance(sample, dict):
            g.scope_text += f"; bound tags shown as on {sample.get('path')}"
        rows = [_attr_row(d, by_name.get(name_key(d.get("name"))) if sample else None)
                for d in t.get("attributes") or [] if isinstance(d, dict) and d.get("name")]
        g.attributes = _rank_attrs(rows, words)
        for r in g.attributes:            # a template calc names attributes, never the sample's tags
            r.pop("tag_name", None)
        await _mark_owned(ground, g.attributes)
        parent_for_children = sample
    elif "element" in scope:
        e = await _g(ground.element, int(scope["element"]))
        if not isinstance(e, dict):
            raise ValueError(f"element {scope['element']} does not exist")
        sample_ids = [int(scope["element"])]
        g.scope_text = f"element {e.get('path')}" + (f" (template \"{e.get('template_name')}\")"
                                                      if e.get("template_name") else "")
        rows = [_attr_row({"name": a.get("name"), "kind": (a.get("value") or {}).get("kind"),
                           "units": a.get("units"), "category": a.get("category")}, a)
                for a in e.get("attributes") or [] if isinstance(a, dict) and a.get("name")]
        for r in rows:
            if r["kind"] in ("unbound",):
                r["kind"] = "tag"
        g.attributes = _rank_attrs(rows, words)
        await _mark_owned(ground, g.attributes)
        parent_for_children = e
    else:
        pts = scope.get("points") if isinstance(scope.get("points"), dict) else {}
        g.scope_text = "points (no element): read tags with tag(\"dataset\", \"name\") or tag(\"name\")"
        seen: set = set()
        for w in words:
            try:
                found = await _g(ground.search_points, w)
            except Exception:
                found = []
            for p in found or []:
                if isinstance(p, dict) and p.get("id") is not None and p["id"] not in seen:
                    seen.add(p["id"])
                    g.points.append(p)
        # R239: tags whose ASSET the request names ("pump bearing temperature" -> SIM-TI-401.PV =
        # Pump P-501 | Bearing Temp), although their own name and description do not say so.
        for p in _cs.extra_points(index, request, [x.get("name") for x in g.points]):
            if p.get("id") not in seen:
                seen.add(p.get("id"))
                g.points.append(p)
        g.points = _rank_points(g.points, request, index)[:MAX_POINTS + _cs.MAX_EXTRA_POINTS]
        g.asset_lines, g.asset_shown, g.asset_total = _cs.asset_map_lines(index, request)
        # The whole plant at a glance (tag families, templates): the model can then tell "no
        # such data here" from "not found by these words" (R231, Jim's turbine request).
        try:
            allp = await _g(ground.search_points, "", 1000)
        except Exception:
            allp = []
        g.all_points = [p for p in allp or [] if isinstance(p, dict) and p.get("name")]
        tf = getattr(ground, "templates", None)
        if callable(tf):
            try:
                g.plant_templates = [t for t in await _g(tf) or [] if isinstance(t, dict)]
            except Exception:
                g.plant_templates = []
        parent_for_children = None
        site_id = pts.get("site", 1) if isinstance(pts.get("site", 1), int) else 1
        sites_doc = await _g(ground.sites) or {}
        g.sites = [s for s in sites_doc.get("sites") or [] if isinstance(s, dict) and s.get("id") == site_id]
        return g

    # Templates of the scope's children (rollups `for c in children(template: ...)`).
    if isinstance(parent_for_children, dict) and parent_for_children.get("id") is not None:
        try:
            kids = await _g(ground.children, int(parent_for_children["id"])) or []
        except Exception:
            kids = []
        tids: List[int] = []
        for k in kids:
            tid = k.get("template") if isinstance(k, dict) else None
            if isinstance(tid, int) and tid not in tids and tid != scope.get("template"):
                tids.append(tid)
        for tid in tids[:MAX_CHILD_TEMPLATES]:
            ct = await _g(ground.template, tid)
            if isinstance(ct, dict):
                n = sum(1 for k in kids if isinstance(k, dict) and k.get("template") == tid)
                g.child_templates.append({
                    "name": ct.get("name"), "children": n,
                    "attributes": [_attr_row(d) for d in ct.get("attributes") or []
                                   if isinstance(d, dict) and d.get("name")][:MAX_ATTRIBUTES]})

    # The site(s) of the instances: zone and shift pattern (shift triggers, shift_start/_end).
    sites_doc = await _g(ground.sites) or {}
    assigned = _assignments_now(sites_doc, time.time_ns())
    parents: Dict[int, Optional[int]] = {}
    site_ids: List[int] = []
    for eid in sample_ids:
        cur: Optional[int] = eid
        hops = 0
        while cur is not None and cur not in parents and hops < 64:
            el = await _g(ground.element, cur)
            parents[cur] = el.get("parent") if isinstance(el, dict) else None
            if cur in assigned:
                break
            cur = parents[cur]
            hops += 1
        sid = _site_of(eid, parents, assigned)
        if sid not in site_ids:
            site_ids.append(sid)
    if not site_ids:
        site_ids = [1]
    by_id = {s.get("id"): s for s in sites_doc.get("sites") or [] if isinstance(s, dict)}
    g.sites = [by_id[i] for i in site_ids if i in by_id]
    return g


# ── prompts ───────────────────────────────────────────────────────────

# Worked examples from a FICTIONAL plant ("EX ..." names) so a name cannot be copied into a real
# draft by accident; the grounding lists the real names.
_EXAMPLES = """\
Worked examples (a fictional plant; its names are NOT in this historian — use the ATTRIBUTES below):
1. "total the flow per shift" (EX Flow in m3/h; there is no attribute for the result yet, so the
   calc names a NEW one, "EX Shift Volume" — approval creates it in m3):
   {"intent": "the total flow of each shift", "topic": "shift volume",
    "needs": [{"quantity": "volume flow", "unit_kind": "volume/time", "attribute": "EX Flow"}],
    "name": "EX Shift Volume", "trigger": {"shift": "end"},
    "body": "output 'EX Shift Volume' = TagTot('EX Flow', shift_start, shift_end);",
    "outputs": [{"attribute": "EX Shift Volume"}]}
2. "minutes the bearing runs above 85 degC each hour":
   {"intent": "minutes per hour with EX Bearing Temp above 85 degC", "name": "EX Hot Minutes",
    "trigger": {"every": "1h"},
    "body": "output 'EX Hot Minutes' = Minutes(TimeGT('EX Bearing Temp', '*-1h', '*', 85[°C]));",
    "outputs": [{"attribute": "EX Hot Minutes"}]}
3. "yesterday's energy from the power (kW), every morning at 6" (EX Daily Energy in kWh):
   {"intent": "the energy of the previous day from EX Power", "topic": "daily energy",
    "needs": [{"quantity": "electrical power", "unit_kind": "power", "attribute": "EX Power"}],
    "name": "EX Daily Energy",
    "trigger": {"daily": ["06:00"]},
    "body": "output 'EX Daily Energy' = TagTot('EX Power', 'y', 't', per: 1h);",
    "outputs": [{"attribute": "EX Daily Energy"}]}
4. "run hours yesterday from the run state" (EX Run State is a digital tag with state Running):
   {"intent": "hours EX Run State was Running over the previous day", "name": "EX Run Hours",
    "trigger": {"daily": ["00:00"]},
    "body": "output 'EX Run Hours' = Hours(TimeEq('EX Run State', 'y', 't', \\"Running\\"));",
    "outputs": [{"attribute": "EX Run Hours"}]}
5. "sum the hourly average flow of every EX Pump under this unit":
   {"intent": "the sum over child EX Pumps of their hourly average flow", "name": "EX Unit Flow",
    "trigger": {"every": "1h"},
    "body": "output 'EX Unit Flow' = Sum(for c in children(template: \\"EX Pump\\") : TagAvg(c|'EX Flow', '*-1h', '*'));",
    "outputs": [{"attribute": "EX Unit Flow"}]}
6. "1 when EX Vibration is above 7 and EX Flow below 10 or the pump is tripped, else 0, every minute":
   {"intent": "a 0/1 flag of high vibration at low flow, or tripped", "name": "EX Vib Flag",
    "trigger": {"every": "1m"},
    "body": "output 'EX Vib Flag' = if ('EX Vibration' > 7[mm/s] and 'EX Flow' < 10[m3/h]) or 'EX Run State' = \\"Tripped\\" then 1 else 0;",
    "outputs": [{"attribute": "EX Vib Flag"}]}
7. "the hourly total of EX Level (in %, NOT a rate)": a total of a non-rate needs per: and is in %·h:
   {"intent": "the hourly integral of EX Level, in %·h", "name": "EX Level Integral",
    "trigger": {"every": "1h"},
    "body": "output 'EX Level Integral' = TagTot('EX Level', '*-1h', '*', per: 1h);",
    "outputs": [{"attribute": "EX Level Integral"}]}
8. "write the flow only while running" (skip a trigger: NoOutput() is an EXPRESSION, never a statement):
   {"intent": "the hourly average flow, written only while running", "name": "EX Running Flow",
    "trigger": {"every": "1h"},
    "body": "output 'EX Running Flow' = if 'EX Run State' = \\"Running\\" then TagAvg('EX Flow', '*-1h', '*') else NoOutput();",
    "outputs": [{"attribute": "EX Running Flow"}]}
9. "the energy of EX Feeder" when only EX Current (A) is listed (a current is not a power):
   {"intent": "feeder energy", "topic": "EX feeder energy", "needs": [{"quantity": "electrical power",
    "unit_kind": "power", "attribute": null}], "name": "EX Energy", "trigger": {"daily": ["00:00"]},
    "body": "", "outputs": [{"attribute": "EX Energy"}],
    "cannot": "EX Current is a current in A; without a voltage and power factor it gives no power"}
10. "how many EX Pumps under this unit are running, every minute" (a count over children: Count()
   counts GOOD results, not true ones — sum a 0/1 instead):
   {"intent": "the number of child EX Pumps whose run state is Running", "name": "EX Pumps Running",
    "trigger": {"every": "1m"},
    "body": "output 'EX Pumps Running' = Sum(for c in children(template: \\"EX Pump\\") : if TagVal(c|'EX Run State') = \\"Running\\" then 1 else 0);",
    "outputs": [{"attribute": "EX Pumps Running"}]}
"""

_SYSTEM = """\
You write PremHist calcs. Reply with ONE JSON object only:
{"intent": one plain line saying what the calc computes,
 "topic": 2-5 words naming what is computed ("pump efficiency", "shift volume"),
 "needs": FILL THIS FIRST — the quantities THIS request reads, each mapped to the listed
          attribute (or tag) that holds it: [{"quantity": "flow", "unit_kind": "%", "attribute":
          "<a listed name, exactly as listed>" | null}, ...]. A listed attribute holds its quantity
          in ITS OWN units — use it (a flow in % is fine for "total the flow": the total is in %·h).
          null ONLY when nothing listed holds that quantity at all, or the request asks for a
          physical quantity the listed units cannot give (power from a current, a volume in m3
          from a flow in %). If a need is null: set "cannot" (what is missing, what the plant has
          instead), leave "body" "" and never substitute another attribute.
 "name": a short calc name,
 "trigger": when it evaluates — one of {"every": "1h", "offset"?: "0s"} | {"daily": ["06:00", ...]} |
            {"weekly": {"days": ["mon"], "at": "06:00"}} | {"monthly": {"day": 1, "at": "06:00"}} |
            {"shift": "end"} | {"shift": "start"} | {"on_input": ["Attr", ...]} | {"on_input": "all"},
 "body": the calc text,
 "outputs": [{"attribute": "<output attribute>"}, ...] (one entry per output statement),
 "max_lookback"?: a duration, only when a window reaches back further than 31d,
 "cannot"?: ONLY when the request cannot be done here — a need is null, or it is not a
            calculation (sending email, the internet, training a model): one sentence naming what
            is missing and what the plant has instead; body ""}

OUTPUTS — where the result goes:
- Prefer a NEW output attribute: name it in "outputs" with a clear name for the result (e.g.
  'Shift Volume', 'Hours Above 85', 'Daily Energy'). It need not exist: approval creates it on the
  template (or element) with the unit of the expression. Never reuse an input's name.
- An attribute marked [output candidate] may be used when it clearly means the result.
- NEVER write to an attribute marked [input ...] or [output of calc ...]: it holds plant data or
  another calc's results.

The calc language:
- statements: `let name = expr;` and `output 'Attribute' = expr;` — each output exactly once, and
  every "outputs" entry has its `output '...' = ...;` statement.
- 'Name' in quotes is an ATTRIBUTE of the element the calc runs on (or a time expression where a
  function expects a time). INPUTS: use ONLY attribute names listed below, exactly as written.
- logic is written with words: `and`, `or`, `not`. NEVER `&`, `&&`, `|`, `||`, `!`.
  `|` is only the attribute path inside a rollup (c|'Attr'). Equality is `=`, not-equal `<>`.
- if-expression: `if cond then a else b`. To write nothing at a trigger use the EXPRESSION
  NoOutput() inside an output: `output 'X' = if cond then v else NoOutput();` — never a bare
  `NoOutput` statement.
- time expressions (PI syntax) in quotes: '*' = the trigger time; 't' = today's local midnight;
  'y' = yesterday's local midnight; 'bod' 'bow' 'bom' 'boy'; offsets '*-1h', 't+6h', '*-1d+6h';
  the bare words shift_start / shift_end = the shift that just ended (only when the site has shifts).
- a window function reads [s, e): TagAvg('EX Flow', '*-1h', '*'). The window must not be empty AT THE
  TRIGGER: a daily trigger at 00:00 summing "yesterday" uses 'y' to 't', never 't' to '*'.
- quantities carry units in brackets: 85[°C], 3.2[m3/h], 7[mm/s]; durations are bare: 1h, 15m, 30s.
  Convert(x, "L/min") takes the unit as a string. State names are strings: "Running".
- durations convert with Seconds() Minutes() Hours() Days(); TimeGT/TimeEq/TimeTrue return durations.
- TagTot of a RATE (m3/h, kg/s, gal/min) gives the amount (m3, kg, gal). TagTot of anything that is
  not a rate (%, kW, °C, mm/s, no unit) needs per: 1h and gives unit·h (kW → kWh, % → %·h). Do not
  invent a conversion the data does not support: a flow in % stays in % (say so in "intent").
  TagAvg is the time-weighted average.
- units of the same dimension convert automatically; never add values of different dimensions.
- rollups over child elements: Sum(for c in children(template: "Name") : TagAvg(c|'Attr', '*-1h', '*'))
  (also Avg Min Max Count Median StdDev).
- a digital input compares with a state name string: TimeEq('EX Run State', 'y', 't', "Running").
- comments `// ...` are allowed.
If an INPUT the request needs is not listed, do not substitute another attribute: set "cannot".
The body reads only the names listed in "needs".

COMMON PLANT FORMULAS — only when the request asks for one of these; what each needs:
- pump hydraulic power = ρ·g·Q·H: needs a volume flow Q (volume/time) and a head H (length) or a
  differential pressure (pressure; then P = Q·Δp); ρ and g are constants.
- pump efficiency = hydraulic power / shaft or electrical power: needs the above AND a shaft or
  electrical power (power). A motor current alone is not a power. In %, with a volume flow Q, a
  pressure Δp and a power P: 100 * Convert(Q * Δp, "kW") / P — Convert makes Q·Δp a power, so
  the ratio has no unit (never Q * Δp / P alone: that is in m³/h·bar/kW).
- electrical power from current: needs current (A), voltage (V) and power factor; 3-phase
  P = √3·V·I·PF. A current tag alone gives neither power nor energy.
- energy = TagTot(power, s, e, per: 1h): needs a power (kW, MW).
- heat duty = m·cp·ΔT: needs a mass flow (mass/time), the specific heat cp and two temperatures.
- specific energy = energy / production: needs an energy (or power) and a production amount.
- availability = run hours / period hours: needs a run state (digital) or a running flag.

CALCS ON SPECIFIC TAGS (when THE CALC RUNS ON says "points"): no element, no attributes. Read
inputs with tag("dataset", "Tag Name") exactly as the TAGS are listed (the dataset matters; never
a quoted 'Name' for an input); "needs" maps to those tag names. Outputs are NEW tags: {"attribute": "<new tag name>"} in
"outputs" and `output '<new tag name>' = ...;` in the body — approval creates the tag with the
unit of the expression. Never name an existing plant tag as an output. The ASSET MAP says what each
tag IS (tag = element | attribute) even when its description does not: "the pumps' bearing
temperatures" are the tags mapped to "<pump> | Bearing Temp"; one value over several tags is e.g.
Avg(TagAvg(tag("ds", "A"), '*-1h', '*'), TagAvg(tag("ds", "B"), '*-1h', '*')).

CONTROL CHARTS (SQC) — only when the request asks for a control chart, SPC, Nelson or Western
Electric rules, or "flag points that break the rules":
- add "chart": {"subgroup": ..., "limits": {"cl", "ucl", "lcl"}, "history"?: "24h", "rules": [...]}
  and role outputs instead of output statements: "outputs": [{"attribute": "<X> Value", "role":
  "value"}, {"attribute": "<X> Status", "role": "status"}, {"attribute": "<X> Severity", "role":
  "severity"}]; the body is "" (a role output has NO output statement).
- subgroup: {"value": "'Attr'"} (one value per trigger), {"mean" | "range" | "stdev": "'Attr'",
  "n": 5, "within": "1h"} (the last n samples), {"moving_range": "'Attr'"}.
- rules: presets {"id": "N1"} … {"id": "N8"} (Nelson) or {"id": "WE1"} … {"id": "WE4"} (Western
  Electric); never both N1 and WE1 (same code), N5 and WE2, N6 and WE3.
- LIMITS ARE CHOSEN BY A PERSON from a period of stable operation. Write a limit number ONLY when
  the request states it ("center 50, limits 44 to 56" → {"cl": "50", "ucl": "56", "lcl": "44"});
  name a limit attribute ONLY when it is listed under ATTRIBUTES ({"ucl": "'UCL'"}). Otherwise
  LEAVE "limits" OUT ENTIRELY: never compute, guess or suggest limits.
- v1 charts are 3-sigma Shewhart charts with fixed limits: "4-sigma limits", "recompute the limits
  every day", EWMA or CUSUM charts are not possible: set "cannot" and say so.
- a history is needed when the trigger is not "every": give "history": "24h" (or longer).

""" + _EXAMPLES


def _fmt_attr(a: Dict[str, Any]) -> str:
    bits = [a.get("kind") or "?"]
    if a.get("point_type"):
        bits.append(a["point_type"])
    if a.get("units"):
        bits.append(f"units {a['units']}")
    if a.get("tag_units"):
        bits.append(f"tag eng units {a['tag_units']}")
    if a.get("category"):
        bits.append(f"category {a['category']}")
    role = ""
    if a.get("kind") == "tag" and a.get("owner"):
        role = f" [output of calc '{a['owner']}': read it, never write it]"
    elif a.get("kind") == "tag" and "bound" in a:
        role = ((f" [input: plant tag {a['tag_name']} — read it as '{a['name']}', never write it]"
                 if a.get("tag_name") else " [input: plant tag — read it, never write it]") if a["bound"]
                else " [output candidate: no tag yet]")
    elif a.get("kind") == "static":
        role = " [static value]"
    return f"- '{a['name']}' — " + ", ".join(bits) + role


def _fmt_site(s: Dict[str, Any]) -> str:
    zone = s.get("zone") or []
    rule = zone[-1].get("rule") if zone and isinstance(zone[-1], dict) else "UTC0"
    head = f"- site \"{s.get('name')}\" (zone {rule})"
    sh = s.get("shifts")
    if not isinstance(sh, dict) or not sh.get("days"):
        return head + ": NO shift pattern — do not use shift triggers, shift_start or shift_end"
    days = []
    for i, day in enumerate(sh.get("days") or []):
        parts = [f"{x.get('name')} {x.get('start')}" + (f" (crew {x['crew']})" if x.get("crew") else "")
                 for x in day if isinstance(x, dict)]
        days.append(f"day {i + 1}: " + (", ".join(parts) if parts else "no shifts"))
    return head + f": shifts in a {len(days)}-day cycle from {sh.get('anchor')} — " + "; ".join(days)


_SQC_EXAMPLE_LITERAL = {
    "trigger": {"on_input": ["Viscosity"]}, "body": "",
    "chart": {"subgroup": {"value": "'Viscosity'"}, "limits": {"cl": "50", "ucl": "56", "lcl": "44"},
              "history": "30d", "rules": [{"id": f"N{i}"} for i in range(1, 9)]},
    "outputs": [{"attribute": "Viscosity SQC Value", "role": "value"},
                {"attribute": "Viscosity SQC Status", "role": "status"},
                {"attribute": "Viscosity SQC Severity", "role": "severity"}]}
_SQC_EXAMPLE_ATTR = {
    "trigger": {"every": "1h"}, "body": "",
    "chart": {"subgroup": {"mean": "'Temp'", "n": 5, "within": "1h"},
              "limits": {"cl": "'Temp CL'", "ucl": "'Temp UCL'", "lcl": "'Temp LCL'"},
              "rules": [{"id": "WE1"}, {"id": "WE2"}, {"id": "WE3"}, {"id": "WE4"}]},
    "outputs": [{"attribute": "Temp SQC Value", "role": "value"},
                {"attribute": "Temp SQC Flags", "role": "flags"},
                {"attribute": "Temp SQC Severity", "role": "severity"}]}

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
# §10.36.20: a number is one the REQUEST STATES AS A LIMIT only where it sits in a limit clause.
# Every number anywhere used to qualify, so "a control chart on Bearing Temp, 3-sigma, every 15
# minutes, center 50" licensed ucl 3 and lcl 15: the chart then validated and was offered as a
# success with limits the model had invented — the one thing this guard exists to prevent.
# "sigma", durations and subgroup sizes are deliberately NOT cues, and neither is "mean" (an
# X-bar request says "the mean of 5 samples", where 5 is a subgroup size, not a limit).
_LIMIT_CUE_RE = re.compile(
    r"\b(?:ucl|lcl|usl|lsl|cl|cent(?:er|re)(?:\s*line)?|target|nominal|upper|lower|"
    r"(?:control\s+|spec(?:ification)?\s+)?limits?|spec(?:ification)?s?)\b", re.I)
# A clause ends at a comma, a semicolon, a line break, or a full stop that is not a decimal point.
_CLAUSE_SPLIT_RE = re.compile(r"[;,\n]|\.(?!\d)")
# Inside a clause that does name limits, a number written as a period, a sigma count, a subgroup
# size or a rule count is still not a limit: "limits 44 to 56 every 15 minutes" states 44 and 56.
# A spelled-out unit may be spaced or hyphenated ("3-sigma", "15 minutes"); a single-letter unit
# counts only when written compactly ("30d"), so a limit of "56 m" is left alone.
_NOT_A_LIMIT_NUM_RE = re.compile(
    r"-?\d+(?:\.\d+)?(?:"
    r"\s*-?\s*(?:sigma|sec|secs|second|seconds|min|mins|minute|minutes|hr|hrs|hour|hours|"
    r"day|days|week|weeks|month|months|year|years|sample|samples|point|points|"
    r"subgroup|subgroups|rule|rules)"
    r"|(?:s|m|h|d|w|mo|y)"
    r")\b", re.I)


def _numbers_in(text: str) -> set:
    """The numbers the request states AS LIMITS, normalized by magnitude (`56`, `56.0`, `+56` and
    `-56` are one): the numbers of a clause that names a limit, minus the ones written as a period
    or a count. A number that appears only elsewhere in the request — a sigma count, a trigger
    period, a history window, a subgroup size, a date — is not one, so it can never license a
    limit the person did not give.

    Deliberately strict: a limit phrasing this misses makes the draft `needs_limits`, which asks
    the person for the numbers — the outcome §10.36.20 prefers over an invented limit."""
    out: set = set()
    for clause in _CLAUSE_SPLIT_RE.split(text or ""):
        if not _LIMIT_CUE_RE.search(clause):
            continue
        for m in _NUM_RE.finditer(_NOT_A_LIMIT_NUM_RE.sub(lambda x: " " * len(x.group(0)), clause)):
            try:
                out.add(abs(float(m.group(0))))
            except ValueError:
                continue
    return out


def _limit_guard(doc: Dict[str, Any], request: str) -> Optional[str]:
    """§10.36.20: a limit NUMBER the request does not state is never sent to PremHist — the
    draft's limits are dropped (it becomes `needs_limits`). Returns a note when it dropped any."""
    chart = doc.get("chart")
    if not isinstance(chart, dict) or not isinstance(chart.get("limits"), dict):
        return None
    stated = _numbers_in(request)
    invented = []
    for k, v in list(chart["limits"].items()):
        if not isinstance(v, str):
            continue
        t = v.strip()
        if t.startswith("'"):
            continue                              # an attribute: PremHist checks it exists
        m = _NUM_RE.search(t)
        if m and abs(float(m.group(0))) not in stated:
            invented.append(f"{k} {t}")
    if invented:
        chart.pop("limits", None)
        return ("the assistant proposed limit numbers the request did not state ("
                + ", ".join(invented) + "); limits are chosen by a person, so they were left out")
    return None


def _fmt_catalog(cat: Dict[str, Any]) -> str:
    lines: List[str] = ["FUNCTIONS (the closed catalog; nothing else exists):"]
    by_cat: Dict[str, List[str]] = {}
    for f in cat.get("functions") or []:
        if not isinstance(f, dict) or not f.get("name"):
            continue
        sig = f.get("signature") or f["name"]
        meaning = _clean(f.get("meaning"), 140)
        by_cat.setdefault(str(f.get("category") or "other"), []).append(
            f"  {sig}" + (f" — {meaning}" if meaning else ""))
    for c, fs in by_cat.items():
        lines.append(f"{c}:")
        lines.extend(fs)
    t = cat.get("time") or {}
    if t:
        lines.append("TIME tokens: " + " ".join(str(x) for x in t.get("tokens") or [])
                     + "; offset units " + " ".join(str(x) for x in t.get("offset_units") or [])
                     + "; e.g. " + " ".join(str(x) for x in t.get("examples") or []))
    trig = [f"  {json.dumps(x.get('example'), ensure_ascii=False)} — {_clean(x.get('meaning'), 120)}"
            for x in cat.get("triggers") or [] if isinstance(x, dict) and x.get("example") is not None]
    if trig:
        lines.append("TRIGGERS:")
        lines.extend(trig)
    units = cat.get("units") or {}
    atoms = [str(a.get("symbol")) for a in units.get("atoms") or [] if isinstance(a, dict) and a.get("symbol")]
    if atoms:
        lines.append("UNIT symbols: " + " ".join(atoms))
    sqc = cat.get("sqc") or {}
    if isinstance(sqc, dict) and sqc.get("presets"):
        lines.append("SQC presets (chart.rules): " + "; ".join(
            f"{p.get('id')} = {p.get('phrase')}" for p in sqc.get("presets") or [] if isinstance(p, dict)))
        lines.append("SQC subgroups: " + " ".join(str(x) for x in sqc.get("subgroups") or [])
                     + "; roles: " + " ".join(str(r.get("role")) for r in sqc.get("roles") or []
                                              if isinstance(r, dict)))
        lines.append("SQC example (individuals, stated limits): " + json.dumps(_SQC_EXAMPLE_LITERAL))
        lines.append("SQC example (X-bar, attribute limits that exist): " + json.dumps(_SQC_EXAMPLE_ATTR))
    lim = cat.get("limits") or {}
    if lim:
        lines.append(f"LIMITS: max_lookback at most {lim.get('max_lookback_max')}, "
                     f"at most {lim.get('max_triggers_per_day')} triggers per day")
    return "\n".join(lines)


def _rank_points(points: List[Dict[str, Any]], request: str,
                 index: Optional[_cs.PlantIndex]) -> List[Dict[str, Any]]:
    """The found tags, most relevant first (R251), so a capped list keeps the ones the request is
    about: a tag the request names (whole, or by an identifying token such as TI-401) first, then
    a tag whose asset the request names (attribute, element), then by how many request words its
    name and description share. Stable: equal tags keep the search order."""
    words = {w.casefold() for w in _words(request)}
    stems = set(_cs.stems(request))

    def score(p: Dict[str, Any]) -> int:
        nm = str(p.get("name") or "")
        s = 0
        if _cs.names_it(request, nm):
            s += 100
        for e, an in (index.tag_owner(nm) if index is not None else [])[:3]:
            s += 10 * _cs.attr_strength(an, request)
            if _cs.names_it(request, e.name):
                s += 20
        toks = set(re.findall(r"[a-z0-9]+", name_key(nm + " " + str(p.get("description") or ""))))
        s += sum(1 for w in words if w in toks) + sum(1 for w in stems if w in toks)
        return s
    return [p for _i, p in sorted(enumerate(points), key=lambda x: (-score(x[1]), x[0]))]


# The points-scope prompt on a larger plant (R251): each level shows less of what the request is
# NOT about — found tags (relevance-ranked), asset-map lines and entries, tag families, templates —
# until the prompt fits; every cap is said in the text ("… N more"), never silent.
_POINTS_LEVELS: Tuple[Dict[str, Any], ...] = (
    {"points": 40, "lines": 24, "entries": 4, "relevant": False, "families": 25, "templates": (10, 10)},
    {"points": 30, "lines": 14, "entries": 3, "relevant": False, "families": 15, "templates": (8, 8)},
    {"points": 20, "lines": 10, "entries": 2, "relevant": True, "families": 10, "templates": (6, 6)},
    {"points": 12, "lines": 6, "entries": 2, "relevant": True, "families": 6, "templates": (4, 4)},
)


def _families(points: List[Dict[str, Any]], cap: int = MAX_FAMILIES) -> List[str]:
    """The plant's tags by family (digits → #): count, type, units, one example and its text."""
    fam: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for p in points:
        key = (str(p.get("dataset") or ""), re.sub(r"\d+", "#", str(p.get("name"))))
        f = fam.setdefault(key, {"n": 0, "units": [], "types": [], "ex": p})
        f["n"] += 1
        if p.get("eng_units") and p["eng_units"] not in f["units"]:
            f["units"].append(p["eng_units"])
        if p.get("point_type") and p["point_type"] not in f["types"]:
            f["types"].append(p["point_type"])
    out = [f"ALL TAGS IN THIS PLANT, by family (# = a number; {len(points)} tags):"]
    for (_ds, key), f in list(fam.items())[:cap]:
        ex = f["ex"]
        out.append(f"- {key} — {f['n']} tag(s), {'/'.join(f['types']) or '?'}"
                   + (f", units {'/'.join(str(u) for u in f['units'])}" if f["units"] else ", no units")
                   + f"; e.g. tag(\"{ex.get('dataset')}\", \"{ex.get('name')}\")"
                   + (f" = {_clean(ex.get('description'), 60)}" if ex.get("description") else ""))
    if len(fam) > cap:
        out.append(f"- … and {len(fam) - cap} more families")
    return out


def _grounding_text(g: _Grounding) -> str:
    out = [f"THE CALC RUNS ON: {g.scope_text}"]
    if g.attributes:
        out.append("ATTRIBUTES (the only names a quoted 'Name' may use):")
        out.extend(_fmt_attr(a) for a in g.attributes)
    elif "points" not in g.scope:
        out.append("ATTRIBUTES: none — this scope has no attributes")
    for t in g.child_templates:
        out.append(f"CHILD ELEMENTS of template \"{t['name']}\" ({t['children']} here) — read them in a "
                   f"rollup as c|'Attr':")
        out.extend(_fmt_attr(a) for a in t["attributes"])
    if "points" in g.scope and not g.points:
        out.append("TAGS found for the request's words: NONE — no tag name or description matches them")
    if g.points:
        out.append("TAGS found for the request's words (read with tag(\"dataset\", \"name\")):")
        shown = g.points if g.points_cap is None else g.points[:g.points_cap]
        for p in shown:
            asset = _cs.tag_asset(g.index, p.get("name"))
            out.append(f"- tag(\"{p.get('dataset')}\", \"{p.get('name')}\") — {p.get('point_type') or '?'}"
                       + (f", units {p['eng_units']}" if p.get("eng_units") else "")
                       # a tag whose asset is known: that says what it is (its family keeps the text)
                       + (f" = {asset}" if asset else (f", {_clean(p.get('description'), 80)}"
                                                        if p.get("description") else "")))
        if len(shown) < len(g.points):
            out.append(f"- … and {len(g.points) - len(shown)} more tags found (less related to the "
                       "request; not shown)")
    if g.asset_lines:
        out.append("ASSET MAP — what each tag IS (tag = element | attribute; a tag's description may not "
                   f"say it; {g.asset_shown} of {g.asset_total} bindings shown):")
        out.extend(g.asset_lines)
    if g.all_points:
        out.extend(_families(g.all_points, g.families_cap))
    if g.plant_templates:
        nt, na = g.templates_cap
        out.append("TEMPLATES in this plant (for orientation; a calc on specific tags reads TAGS): " + "; ".join(
            f"{t.get('name')} (" + ", ".join(
                f"{a.get('name')}" + (f" {a['units']}" if a.get("units") else "")
                for a in (t.get("attributes") or [])[:na] if isinstance(a, dict)) + ")"
            for t in g.plant_templates[:nt])
            + (f"; … and {len(g.plant_templates) - nt} more" if len(g.plant_templates) > nt else ""))
    if g.sites:
        out.append("SITE:")
        out.extend(_fmt_site(s) for s in g.sites)
    out.append(_fmt_catalog(g.catalog))
    return "\n".join(out)


def _current_text(current: Dict[str, Any]) -> str:
    keep = {k: current.get(k) for k in ("name", "trigger", "body", "outputs", "max_lookback")
            if current.get(k) is not None}
    return ("THE CALC AS IT STANDS (change it as the request says; reply with the WHOLE calc):\n"
            + json.dumps(keep, ensure_ascii=False, indent=1))


# ── the document and PremHist's verdict ───────────────────────────────

_LITERAL_RE = re.compile(r"""(['"])((?:(?!\1)[^\n])*)\1""")


def _fold_literals(body: Any, g: "_Grounding", outputs: Sequence[str] = ()) -> Any:
    """R252: a quoted name in the body written with Unicode dashes or odd spaces (U+2011 in
    'SIM-TI-401.PV') is written in ASCII when that names a listed attribute, tag or dataset, or is
    one of the calc's own output names (a NEW tag or attribute is never named with a no-break
    hyphen); any other text is left as the model wrote it (PremHist judges it)."""
    if not isinstance(body, str) or body == _cs.fold_text(body):
        return body
    names = {name_key(n) for n in g.input_names()}
    names |= {name_key(p.get("dataset")) for p in g.points + g.all_points if p.get("dataset")}
    outs = {str(o) for o in outputs}

    def sub(m: "re.Match[str]") -> str:
        f = " ".join(_cs.fold_text(m.group(2)).split())
        if f != m.group(2) and (name_key(f) in names or m.group(2) in outs):
            return m.group(1) + f + m.group(1)
        return m.group(0)
    return _LITERAL_RE.sub(sub, body)


def _fold_document(doc: Dict[str, Any], g: "_Grounding") -> None:
    """R252 on the whole draft: the body's quoted names and the outputs' new names, in ASCII."""
    raw_outs = [o["attribute"] for o in doc.get("outputs") or []
                if isinstance(o, dict) and isinstance(o.get("attribute"), str)]
    doc["body"] = _fold_literals(doc.get("body"), g, raw_outs)
    for o in doc.get("outputs") or []:
        if isinstance(o, dict) and isinstance(o.get("attribute"), str):
            o["attribute"] = " ".join(_cs.fold_text(o["attribute"]).split())


def _build_document(obj: Dict[str, Any], scope: Dict[str, Any], current: Optional[Dict[str, Any]],
                    fixed_name: Optional[str]) -> Dict[str, Any]:
    """The calc document (§10.2) from the model's reply. The scope is the caller's, never the
    model's; the name is the calc's when it has one. Everything else is passed as the model wrote
    it — PremHist judges it (unknown or invalid fields are its `parse` problems)."""
    cur = current if isinstance(current, dict) else {}
    doc: Dict[str, Any] = {
        "name": fixed_name or _clean(obj.get("name"), 200) or _clean(cur.get("name"), 200) or "Drafted calc",
        "kind": "expression",
        "scope": scope,
        "trigger": obj.get("trigger"),
        "body": obj.get("body") if isinstance(obj.get("body"), str) else "",
        "outputs": obj.get("outputs") if isinstance(obj.get("outputs"), list) else [],
    }
    if isinstance(obj.get("chart"), dict):
        doc["chart"] = obj["chart"]
    for k in ("wait", "max_lookahead", "options"):              # kept from the editor's document
        if cur.get(k) is not None:
            doc[k] = cur[k]
    lb = obj.get("max_lookback") if obj.get("max_lookback") not in (None, "") else cur.get("max_lookback")
    if lb is not None:
        doc["max_lookback"] = lb
    intent = _clean(obj.get("intent"), 2000)
    if intent:
        doc["description"] = intent
    elif isinstance(cur.get("description"), str):
        doc["description"] = cur["description"]
    return doc


def _problem_line(p: Dict[str, Any]) -> str:
    where = []
    if p.get("element") is not None:
        where.append(f"element {p['element']}")
    if p.get("line") is not None:
        where.append(f"line {p['line']}" + (f", col {p['col']}" if p.get("col") is not None else ""))
    head = (", ".join(where) + ": ") if where else ""
    line = f"{head}{p.get('code')} ({p.get('severity')}): {_clean(p.get('message'), 400)}"
    hints = [h for rx, h in _MESSAGE_HINTS if rx.search(str(p.get("message") or ""))]
    if not hints and _HINTS.get(str(p.get("code"))):
        hints = [_HINTS[str(p.get("code"))]]
    return line + (f" — fix: {hints[0]}" if hints else "")


def _dedupe_problems(probs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The same problem on several elements once (the first element kept): by code, position
    and message."""
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for p in probs:
        k = (p.get("code"), p.get("severity"), p.get("line"), p.get("col"), p.get("message"))
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def _verdict(v: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """PremHist's validate answer → (blocking problems, repairable warnings, other warnings).

    Blocking (R174(3): a document is valid only with no error at the document level AND in every
    instance): every document error and every instance error (the same problem on several elements
    once), an instance that does not resolve with no stated error, no instance at all, and a
    missing hash or read-back. Repairable: R68's `empty_window_at_trigger`."""
    probs = [p for p in v.get("problems") or [] if isinstance(p, dict)]
    errors = [p for p in probs if p.get("severity") == "error"]
    warnings = [p for p in probs if p.get("severity") != "error"]
    inst = [i for i in v.get("instances") or [] if isinstance(i, dict)]
    # Phases 3-7 are per instance (R16): unknown references, units, R68's warning live there.
    for i in inst:
        for p in i.get("problems") or []:
            if isinstance(p, dict) and p.get("severity") != "error":
                q = dict(p)
                q.setdefault("element", i.get("element"))
                warnings.append(q)
    for i in inst:
        own = [p for p in i.get("problems") or [] if isinstance(p, dict) and p.get("severity") == "error"]
        for p in own:
            q = dict(p)
            q.setdefault("element", i.get("element"))
            errors.append(q)
        if not own and not i.get("ok") and not i.get("inactive"):
            errors.append({"code": "no_instance", "severity": "error", "message":
                           f"element {i.get('element')} does not resolve this calc", "line": None,
                           "col": None, "element": i.get("element")})
    errors = _dedupe_problems(errors)
    if not errors and not inst:
        errors.append({"code": "no_instance", "severity": "error", "message":
                       "the scope has no element to run this calc on", "line": None, "col": None,
                       "element": None})
    if not errors and (not v.get("hash") or not isinstance(v.get("readback"), str) or not v.get("readback")):
        errors.append({"code": "not_checked", "severity": "error", "message":
                       "PremHist gave no hash or read-back for this calc", "line": None, "col": None,
                       "element": None})
    warnings = _dedupe_problems(warnings)
    repairable = [p for p in warnings if p.get("code") in _REPAIRABLE_WARNINGS]
    other = [p for p in warnings if p.get("code") not in _REPAIRABLE_WARNINGS]
    return errors, repairable, other


def _verdict_sqc(v: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """[`_verdict`] that does not add `not_checked` when the only error is the missing limits
    (R216: PremHist still answers its read-back, but it is the `needs_limits` outcome's)."""
    errors, repairable, other = _verdict(v)
    real = [p for p in errors if p.get("code") not in ("not_checked",)]
    if real and all(p.get("code") == "sqc_limits_missing" for p in real):
        return real, repairable, other
    return errors, repairable, other


def _plural(name: Optional[str]) -> str:
    """'Centrifugal Pump' → 'pumps' (the last word, lower case); 'elements' without a name."""
    w = (name or "").split()
    if not w:
        return "elements"
    last = w[-1].lower()
    return last if last.endswith("s") else last + "s"


def _coverage_note(v: Dict[str, Any], template_name: Optional[str]) -> Optional[str]:
    """R200: where the calc will run — "runs on 4 of 10 pumps; not on 6 (Vibration has no tag)"
    — when some instances are inactive (an input of theirs has no tag). None when all run."""
    inst = [i for i in v.get("instances") or [] if isinstance(i, dict)]
    off = [i for i in inst if i.get("inactive")]
    if not off:
        return None
    reasons = _dedupe(str(i.get("inactive")) for i in off)
    on = sum(1 for i in inst if i.get("ok"))
    return (f"runs on {on} of {len(inst)} {_plural(template_name)}; not on {len(off)} "
            f"({'; '.join(reasons)}) — they start on their own once the tag is bound")


def _understood(obj: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(obj, dict):
        return None
    parts = []
    if isinstance(obj.get("intent"), str) and obj["intent"].strip():
        parts.append(_clean(obj["intent"], 300))
    if obj.get("trigger") is not None:
        parts.append("trigger " + json.dumps(obj.get("trigger"), ensure_ascii=False))
    return "; ".join(parts) or None


def _fail(code: str, error: str, *, problems: Sequence[Dict[str, Any]] = (), understood: Optional[str] = None,
          notes: Sequence[str] = (), authoring: Optional[Dict[str, Any]] = None,
          reason: Optional[str] = None, missing: Sequence[str] = (),
          suggestions: Sequence[str] = ()) -> Dict[str, Any]:
    """A failure. `reason` = the headline the viewer shows (one plain, specific sentence; `error`
    when not given), `missing` = the quantities the plant has no data for, `suggestions` = up to
    three requests the grounding shows CAN be done (never invented), `understood` = the model's
    reading of the request (never the reason)."""
    out: Dict[str, Any] = {"ok": False, "code": code, "error": error, "problems": list(problems),
                           "understood": understood, "notes": _dedupe(notes),
                           "reason": reason or error, "missing": list(missing),
                           "suggestions": list(suggestions)}
    if authoring is not None:
        out["authoring"] = authoring
    return out


def _scope_of(context: Optional[Dict[str, Any]], current: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for src in (context, current):
        sc = src.get("scope") if isinstance(src, dict) else None
        if isinstance(sc, dict) and len(sc) == 1:
            k = next(iter(sc))
            if k in ("template", "element") and isinstance(sc[k], int) and not isinstance(sc[k], bool):
                return {k: sc[k]}
            if k == "points" and isinstance(sc[k], dict):
                return {"points": dict(sc[k])}
    return None


# ── plausibility: needs, units, refusals in plain words ──────────────
#
# design_calc.md R231 / §10.22 (Jim's live tests 2026-10-22: a points-scope turbine request that
# surfaced a syntax error; a pump "efficiency" in %²/kW offered as a success). PremHist stays the
# only judge of VALIDITY; these checks only decide that a valid-looking draft is not what was
# asked (a refusal, never a success) and write the refusal in plain words.

_UNIT_CLASSES: List[Tuple[str, "re.Pattern[str]"]] = [(k, re.compile(rx)) for k, rx in [
    ("percent", r"^(%|percent|pct)$"),
    ("energy", r"^(k|m|g)?wh$|^(k|m|g)?j$|^(mm)?btu$|^therms?$"),
    ("power", r"^(k|m|g)?w$|^hp$|^(k|m)?va$"),
    ("current", r"^(m|k)?a$|^amps?$"),
    ("voltage", r"^(m|k)?v$|^volts?$"),
    ("temperature", r"^°?[cfk]$|^deg[cf]$|^celsius$|^fahrenheit$"),
    ("pressure", r"^(k|m|g)?pa[ag]?$|^m?bar[ag]?$|^psi[ag]?$|^mmhg$|^(mm|in)h2o$|^atm$"),
    ("volflow", r"^(m3|l|gal|ft3|bbl|ml|cm3|scf|nm3)/(s|min|h|d)$|^(gpm|lpm|cfm|scfm)$"),
    ("massflow", r"^(kg|g|t|lb|klb|ton|tonne)/(s|min|h|d)$|^tph$"),
    ("volume", r"^(m3|l|gal|ft3|bbl|ml|cm3)$"),
    ("mass", r"^(kg|g|t|lb|ton|tonne)$"),
    ("speed", r"^(mm|in|m)/s$|^ips$"),
    ("length", r"^(m|mm|cm|km|ft|in)$"),
]]
_CLASS_WORDS: List[Tuple[str, "re.Pattern[str]"]] = [(k, re.compile(rx, re.I)) for k, rx in [
    ("energy", r"\benergy\b|\b[km]wh\b"),
    ("power", r"\bpower\b(?!\s*factor)|\b[km]w\b|\bwatts?\b"),
    ("current", r"\bcurrent\b|\bamps?\b|\bamperage\b"),
    ("voltage", r"\bvoltage\b|\bvolts?\b"),
    ("temperature", r"\btemperature\b|°|\bdeg\b"),
    ("pressure", r"\bpressure\b"),
    ("length", r"\bhead\b|\blength\b"),
    ("volflow", r"volume\s*/\s*time|\bvolume flow|\bvolumetric\b|\bm3/h\b|\bl/min\b|\bgal/min\b|\bgpm\b"),
    ("massflow", r"mass\s*/\s*time|\bmass flow\b|\bkg/h\b|\bt/h\b"),
]]
# Request phrases that name a result unit ("in kWh", "in liters per minute", "in DEG F").
_REQ_UNITS: List[Tuple[str, "re.Pattern[str]"]] = [(k, re.compile(rx, re.I)) for k, rx in [
    ("energy", r"\b(in|as)\s+(k|m)?wh\b"),
    ("power", r"\b(in|as|to)\s+(k|m)w\b(?!h)"),
    ("volflow", r"\b(in|to)\s+(liters?|litres?|gallons?|cubic meters?|m3)\s+per\s+(minute|hour|second|day)\b"
                r"|\b(in|to)\s+(l/min|gal/min|m3/h|gpm|lpm)\b"),
    ("volume", r"\b(in|as)\s+(liters?|litres?|gallons?|cubic meters?|m3)\b(?!\s*(per|/))"),
    ("temperature", r"\b(in|to)\s+(deg\s*[cf]|°\s*[cf]|celsius|fahrenheit)\b"),
    ("pressure", r"\b(in|to)\s+(bar|psi|kpa|pa)\b"),
]]
_DIMENSIONLESS_RE = re.compile(r"\befficienc|\bratio\b|\bfraction\b|\byield\b|\bavailability\b|"
                               r"\butili[sz]ation\b|\bcop\b|\bcoefficient of performance\b|"
                               r"\b(as an?|in) (percent(age)?|%)", re.I)
_PCT_NAME_RE = re.compile(r"%|\bpercent|\bpct\b", re.I)
_SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


def unit_display(u: Optional[str]) -> str:
    """A canonical unit text for people: exponents as superscripts ("%2/kW" → "%²/kW",
    "m^10" → "m¹⁰"); H2O stays H2O."""
    t = str(u or "")
    t = re.sub(r"\^(\d+)", lambda m: m.group(1).translate(_SUPERSCRIPT), t)
    return re.sub(r"(?<=[A-Za-z%°µ])(?<!H)(\d+)(?=$|[/·*\s)])", lambda m: m.group(1).translate(_SUPERSCRIPT), t)


def _unit_class(u: Optional[str]) -> Optional[str]:
    t = str(u or "").strip().lower().replace(" ", "").replace("³", "3").replace("hr", "h")
    t = t.replace("°", "°").replace("deg", "deg")
    if not t:
        return None
    for k, rx in _UNIT_CLASSES:
        if rx.search(t):
            return k
    return None


def _kind_classes(text: str) -> set:
    out = set()
    for k, rx in _CLASS_WORDS:
        if rx.search(text or ""):
            out.add(k)
    c = _unit_class(text)
    if c:
        out.add(c)
    return out


def _an(q: str) -> str:
    q = str(q or "").strip()
    if re.match(r"^(a|an|the|no)\s", q, re.I):
        return q
    return ("an " if q[:1].lower() in "aeiou" else "a ") + q


_CLASS_PLAIN = {"volflow": "a volume flow (like L/min)", "volume": "a volume (like m³ or gallons)",
                "energy": "an energy (like kWh)", "power": "a power (like kW)", "temperature": "a temperature",
                "pressure": "a pressure (like bar)", "massflow": "a mass flow (like kg/h)"}


def _join(items: Sequence[str], word: str = "and") -> str:
    items = [x for x in items if x]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" {word} " + items[-1]


def _sentence(s: str) -> str:
    s = " ".join(str(s or "").split()).strip()
    if not s:
        return s
    s = s[0].upper() + s[1:]
    return s if s.endswith((".", "!", "?")) else s + "."


def _input_rows(g: "_Grounding") -> List[Dict[str, Any]]:
    """What the calc can READ: bound / unknown tag attributes (not calc outputs), and on specific
    tags every tag — as {name, units, point_type, static}."""
    out: List[Dict[str, Any]] = []
    for a in g._rows():
        kind = a.get("kind")
        if kind == "static":
            out.append({"name": a["name"], "units": a.get("units"), "point_type": None, "static": True})
        elif kind == "tag" and a.get("bound") is not False and not a.get("owner"):
            out.append({"name": a["name"], "units": a.get("tag_units") or a.get("units"),
                        "point_type": a.get("point_type"), "static": False})
    seen = {name_key(r["name"]) for r in out}
    for p in g.points + g.all_points:
        if p.get("name") and name_key(p["name"]) not in seen:
            seen.add(name_key(p["name"]))
            out.append({"name": str(p["name"]), "units": p.get("eng_units"), "point_type": p.get("point_type"),
                        "static": False, "description": p.get("description")})
    return out


# Formulas whose inputs the plant must measure (CLAUDE: the "common plant formulas" block). Each:
# (request pattern, topic, [(what is needed, unit classes that satisfy it)]).
_FORMULAS: List[Tuple["re.Pattern[str]", str, List[Tuple[str, set]]]] = [
    (re.compile(r"\befficienc", re.I), "efficiency",
     [("a measured shaft or electrical power", {"power", "energy"})]),
    (re.compile(r"\bheat rate\b", re.I), "heat rate",
     [("a power or energy output", {"power", "energy"}), ("a fuel flow", {"massflow", "volflow"})]),
    (re.compile(r"\bspecific energy\b|\benergy (per|intensity)\b", re.I), "specific energy",
     [("an energy or power", {"power", "energy"}), ("a production amount", {"massflow", "mass", "volume", "volflow"})]),
    (re.compile(r"\bheat duty\b", re.I), "heat duty",
     [("a mass flow", {"massflow"}), ("temperatures", {"temperature"})]),
    (re.compile(r"\bhydraulic power\b", re.I), "hydraulic power",
     [("a volume flow", {"volflow"}), ("a head or pressure", {"length", "pressure"})]),
]
_PUMP_EXTRA = [("a volume flow", {"volflow"}), ("a head or pressure", {"length", "pressure"})]
# "current" as the electrical quantity — not the adjective ("the current electricity price").
_FROM_CURRENT_RE = (re.compile(r"\b(energy|power|[km]wh?|watts?)\b", re.I),
                    re.compile(r"\bamps?\b|\bamperage\b|\bcurrents?\b(?=\s*(?:$|[.,;:)])|\s+(?:of|to|from|in|on|"
                               r"into|and|or|tags?|signals?|draw|measurements?|readings?)\b)", re.I))


def _plant_rows(g: "_Grounding") -> List[Dict[str, Any]]:
    """What the WHOLE plant could feed a calc (R242): the scope's inputs plus, when the plant
    index is known, every bound template attribute and static value elsewhere."""
    rows = _input_rows(g)
    if g.index is not None:
        seen = {name_key(r["name"]) for r in rows}
        for r in g.index.input_rows():
            if name_key(r["name"]) not in seen:
                seen.add(name_key(r["name"]))
                rows.append(r)
    return rows


# R243: a measured power or energy the request asks for ("energy used by each feeder in kWh") when
# nothing in the plant measures one; a pumped volume ("gallons pumped") when no flow is a volume flow.
_ENERGY_ASK_RE = re.compile(r"\b(energy|[kmg]wh|kilowatt[- ]?hours?|power(?!\s*factor)|[kmg]w(?!h)|watts?)\b",
                            re.I)
_VOLUME_ASK_RE = (re.compile(r"\b(gallons?|liters?|litres?|cubic met(?:er|re)s?|m3|m³|barrels?|bbl)\b", re.I),
                  re.compile(r"\b(pump\w*|flow\w*|deliver\w*|transfer\w*|throughput|moved)\b", re.I))


def _physics_gap(request: str, g: "_Grounding") -> Optional[Dict[str, Any]]:
    """A formula the request names whose inputs the plant does not measure (grounding only,
    model-independent): {topic, needs: [labels], missing: [labels], facts: [plain clauses]}.
    Judged on the WHOLE plant when its index is known (R242): a source in another scope is not
    a gap — it is a scope proposal."""
    rows = _plant_rows(g)
    live = [r for r in rows if not r["static"]]
    have = {_unit_class(r["units"]) for r in live} - {None}
    reqs: List[Tuple[str, set]] = []
    topic = None
    for rx, name, needs in _FORMULAS:
        if rx.search(request):
            topic = name
            reqs = list(needs)
            if name == "efficiency" and re.search(r"\bpumps?\b", request, re.I):
                topic = "pump efficiency"
                reqs = list(_PUMP_EXTRA) + reqs
            break
    if topic is None and _FROM_CURRENT_RE[0].search(request) and _FROM_CURRENT_RE[1].search(request) \
            and not ({"power", "energy"} & have):
        topic = "power from current"
        reqs = [("a voltage", {"voltage"}), ("a power factor", set())]
    if topic is None and g.index is not None and _ENERGY_ASK_RE.search(request) \
            and not ({"power", "energy"} & have):
        m = _ENERGY_ASK_RE.search(request)
        topic = "energy" if re.match(r"(?i)energy|kilowatt|.*wh$", m.group(1)) else "power"
        reqs = [("a measured power or energy", {"power", "energy"})]
    if topic is None and g.index is not None and _VOLUME_ASK_RE[0].search(request) \
            and _VOLUME_ASK_RE[1].search(request) and not ({"volflow", "volume"} & have):
        topic = "a pumped volume"
        reqs = [("a volume flow", {"volflow"})]
    if topic is None:
        return None
    missing: List[str] = []
    facts: List[str] = []
    for label, classes in reqs:
        if classes and classes & have:
            continue
        if not classes:                                   # power factor: by name
            if any(re.search(r"power factor|\bpf\b|cos\s*phi", r["name"], re.I) for r in rows):
                continue
        missing.append(label)
        static = [r for r in rows if r["static"] and _unit_class(r["units"]) in classes]
        for r in static[:1]:
            facts.append(f"{r['name']} is a fixed value, not a measurement")
    if not missing:
        return None
    # What the plant measures instead, near the missing quantities (flow in %, motor current …).
    words = {"volume flow": r"flow", "a measured shaft or electrical power": r"power|current|amp|kw",
             "a power or energy output": r"power|energy|current", "a voltage": r"volt",
             "an energy or power": r"power|energy|current",
             "a measured power or energy": r"power|current|amp|kw"}
    for label in missing:
        rx = next((v for k, v in words.items() if k in label), None)
        if not rx:
            continue
        for r in live:
            if re.search(rx, r["name"] + " " + str(r.get("description") or ""), re.I) and r.get("units"):
                c = _unit_class(r["units"])
                if c not in {"power", "energy", "volflow", "voltage"}:
                    facts.append(f"{r['name']} is only in {unit_display(r['units'])}")
                    break
    absent = [re.sub(r"^(a measured |an |a )", "", m) for m in missing]
    facts.append("there is " + _join(["no " + x for x in _dedupe(absent)]) + " in this plant")
    return {"topic": topic, "needs": [lbl for lbl, _ in reqs], "missing": missing, "facts": _dedupe(facts)}


# R255: the formulas whose measured inputs are judged per scope (the plant-wide check is _physics_gap).
_SCOPE_FORMULA_TOPICS = frozenset({"efficiency", "pump efficiency", "heat rate", "specific energy", "heat duty",
                                   "hydraulic power", "power from current"})


def _index_rows(index: _cs.PlantIndex, scope: Dict[str, Any],
                request: str = "") -> Optional[List[Dict[str, Any]]]:
    """A template / element scope's inputs from the plant index, as grounding attribute rows (the
    shape `_input_rows` reads); an element that is the home of a template's members also gets that
    template's rows (a rollup reads them). Specific tags: the tags the request names (R255
    amendment); None when it names none (they see the whole plant)."""
    k = _cs.scope_key(scope)
    if k is None:
        return None
    rows: List[Dict[str, Any]] = []
    if k[0] == "points":
        names = {name_key(n) for n in _cs.named_tags(index, request)}
        for pt in index.points:
            if name_key(pt.get("name")) in names:
                rows.append({"name": str(pt["name"]), "kind": "tag", "units": pt.get("eng_units"),
                             "tag_units": pt.get("eng_units"), "point_type": pt.get("point_type")})
        return rows or None

    def tpl_rows(t: "_cs.Template") -> None:
        for a in t.attrs:
            an = str(a["name"])
            if a.get("kind") == "static":
                rows.append({"name": an, "kind": "static", "units": a.get("units")})
                continue
            bd = index.bound(t, an)
            if bd:
                rows.append({"name": an, "kind": "tag", "units": a.get("units"),
                             "tag_units": bd[0][1].get("eng_units"), "point_type": bd[0][1].get("point_type")})
    if k[0] == "template":
        t = index.template(k[1])
        if t is None:
            return None
        tpl_rows(t)
        return rows
    e = index.elements.get(k[1])
    if e is None:
        return None
    for an, a in e.attrs.items():
        if a.get("kind") == "static":
            rows.append({"name": an, "kind": "static", "units": a.get("units")})
        elif a.get("tag"):
            rows.append({"name": an, "kind": "tag", "units": a.get("units"), "tag_units": a["tag"].get("eng_units"),
                         "point_type": a["tag"].get("point_type")})
    for t in index.templates:
        if t.elements and index.common_parent(t) is e:
            tpl_rows(t)
    return rows


def _formula_gap_in(request: str, index: _cs.PlantIndex, scope: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    rows = _index_rows(index, scope, request)
    if rows is None:
        return None
    g = _Grounding()
    g.scope = scope
    g.attributes = rows
    gap = _physics_gap(request, g)                     # g.index is None: judged on this scope only
    return gap if gap is not None and gap["topic"] in _SCOPE_FORMULA_TOPICS else None


def _formula_scope(request: str, scope: Optional[Dict[str, Any]],
                   index: Optional[_cs.PlantIndex]) -> Optional[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]:
    """R255: a formula the request names (pump efficiency, heat rate …) whose measured inputs the
    scope at hand lacks → (its gap, a proposal {scope, label, why} for the template that measures
    them all, or None). None when the scope can feed it (or is specific tags, or no formula)."""
    if index is None or scope is None:
        return None
    gap = _formula_gap_in(request, index, scope)
    if gap is None:
        return None
    k = _cs.scope_key(scope)
    here = index.template(k[1]) if k[0] == "template" else None
    if k[0] == "element" and k[1] in index.elements:
        here = index.template(index.elements[k[1]].template)
    if k[0] == "points":
        tags = _cs.named_tags(index, request)
        owners = {e.template for t in tags for e, _a in index.tag_owner(t)}
        here = index.template(next(iter(owners))) if len(owners) == 1 else None
        here_name = _join(tags) or "these tags"
    else:
        here_name = (index.elements[k[1]].name if k[0] == "element" and k[1] in index.elements
                     else here.name if here is not None else "this scope")
    rank = {s.template.id: i for i, s in enumerate(_cs.subjects(index, request))}
    cands = []
    for t in index.templates:
        if not t.elements or (here is not None and t.id == here.id):
            continue
        if gap["topic"] == "pump efficiency" and "pump" not in _cs.stems(t.name):
            continue
        if _formula_gap_in(request, index, {"template": t.id}) is None:
            cands.append(t)
    if not cands:
        return gap, None
    cands.sort(key=lambda t: (rank.get(t.id, 999), -len(t.elements), t.name))
    t = cands[0]
    rows = _index_rows(index, {"template": t.id}) or []
    measured = []
    for label in gap["needs"]:
        classes = next((c for lb, c in list(_PUMP_EXTRA) + [n for _rx, _nm, ns in _FORMULAS for n in ns]
                        if lb == label), set())
        r = next((r for r in rows if r.get("kind") == "tag" and _unit_class(r.get("tag_units") or r.get("units"))
                  in classes), None)
        if r is not None:
            u = r.get("tag_units") or r.get("units")
            item = f"{r['name']} ({unit_display(u)})" if u else str(r["name"])
            if item not in measured:
                measured.append(item)
    why = (f"{t.name} measures {_join(measured) or 'every input'}, which {gap['topic']} needs; "
           f"{here_name} {'do' if k[0] == 'points' and ' and ' in here_name else 'does'} not")
    return gap, {"scope": {"template": t.id}, "label": _cs.label_of(index, {"template": t.id}), "why": why}


def _formula_choices(request: str, scope: Dict[str, Any], gap: Dict[str, Any], alt: Dict[str, Any],
                     index: _cs.PlantIndex, notes: List[str]) -> Dict[str, Any]:
    """R255 amendment: an element / specific-tags scope that cannot feed the formula, while a template
    measures every input → the choices, with NO model call: "Pump P-501 can't compute pump
    efficiency — it has no volume flow, … The Feed Pumps can." [each Feed Pump] [one Feed Pump]
    [Keep]. (A model drafting on a template the person did not pick is avoidable guesswork here.)"""
    k = _cs.scope_key(scope)
    tags = _cs.named_tags(index, request) if k[0] == "points" else []
    who = index.elements[k[1]].name if k[0] == "element" and k[1] in index.elements else (_join(tags) or "These tags")
    has = "they have" if len(tags) > 1 else "it has"
    t = index.template(alt["scope"]["template"])
    absent = _dedupe([re.sub(r"^(a measured |an |a )", "", m) for m in gap["missing"]])
    facts = [f for f in gap.get("facts") or [] if not f.endswith(" in this plant")]
    reason = (f"{who} can't compute {gap['topic']} — {has} " + _join(["no " + x for x in absent])
              + (f" ({'; '.join(facts)})" if facts else "") + f". The {_cs._plural(t.name)} can.")
    choices = [{"scope": alt["scope"], "label": alt["label"], "why": alt["why"]}]
    one = next((e for e in sorted(index.members(t), key=lambda e: (e.path, e.id))
                if _formula_gap_in(request, index, {"element": e.id}) is None), None)
    if one is not None:
        choices.append({"scope": {"element": one.id}, "label": _cs.label_of(index, {"element": one.id}),
                        "why": f"{one.name} alone ({alt['why']})"})
    return {"ok": False, "code": "choose_scope", "kind": "cannot_compute", "error": reason, "reason": reason,
            "choices": choices, "keep": {"scope": scope, "label": _cs.label_of(index, scope)}, "problems": [], "understood": None,
            "notes": _dedupe(notes), "missing": gap["missing"], "suggestions": []}


def _suggestions(g: "_Grounding", request: str, avoid: Sequence[str] = ()) -> List[str]:
    """Up to three requests the grounding shows CAN be done (existing inputs only; never an
    invented name): run hours from a digital state, an hourly average, a daily maximum of a
    vibration — the inputs the request's words point at first."""
    rows = [r for r in (_input_rows(g) or _plant_rows(g)) if not r["static"]]
    if not rows:
        return []
    words = [w.casefold() for w in _words(request)]
    avoid_k = {name_key(a) for a in avoid}

    def score(r: Dict[str, Any]) -> int:
        text = (r["name"] + " " + str(r.get("description") or "")).casefold()
        return -sum(1 for w in words if w in text)

    rows.sort(key=lambda r: (score(r), name_key(r["name"])))
    cands: List[Tuple[int, str, str]] = []                   # (priority, kind, text)
    for r in rows:
        if name_key(r["name"]) in avoid_k:
            continue
        pt = str(r.get("point_type") or "").lower()
        u = str(r.get("units") or "")
        if pt == "digital" or (re.search(r"\b(run|state|status)\b", r["name"], re.I) and not u):
            cands.append((0, "digital", f"Run hours per day from {r['name']}"))
        elif _unit_class(u) == "speed" or re.search(r"vib", r["name"], re.I):
            cands.append((2, "vib", f"Daily maximum of {r['name']}"))
        else:
            cands.append((1, "avg", f"Hourly average of {r['name']}" + (f" (in {unit_display(u)})" if u else "")))
    out: List[str] = []
    for want in ("digital", "avg", "vib"):
        hit = next((t for _p, k, t in cands if k == want), None)
        if hit:
            out.append(hit)
    for _p, _k, t in cands:
        if len(out) >= MAX_SUGGESTIONS:
            break
        if t not in out:
            out.append(t)
    return out


def _needs_of(obj: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    n = obj.get("needs")
    if not isinstance(n, list):
        return None
    return [x for x in n if isinstance(x, dict) and isinstance(x.get("quantity"), str)]


_NOT_DATA_RE = re.compile(r"\b(time|duration|period|interval|constant|threshold|limit|setpoint value|date|"
                          r"calendar|shift|window|count of|number of)\b", re.I)


def _norm_ref(s: Any) -> str:
    """A model's reference to one input, reduced to the NAME the grounding lists: tag("name"),
    tag("dataset", "name"), either quote style, and a rollup path c|'Attr' (the form the system
    prompt's example 5 teaches) → Attr.

    The quotes used to be stripped BEFORE the split on "|", which left an orphan quote behind:
    c|'Bearing Temp' became c|'Bearing Temp and p|'Bearing Temp' became 'Bearing Temp, so a
    listed attribute matched nothing. _missing_needs then refused the request with
    "c|'Bearing Temp is not in this plant" - about an attribute the plant does have, with no model
    call - and _reads_outside_needs raised a bogus "the body reads 'Bearing Temp', which is not in
    needs" that cost a repair round. The c| prefix was also kept deliberately, which defeated the
    split for exactly the rollup case it exists for; no caller inspects the prefix, and all three
    (_missing_needs, _listed_name, _reads_outside_needs) want the bare attribute name, which is
    also what _body_reads yields for the same path."""
    t = " ".join(_cs.fold_text(s or "").split())
    # tag("name"), tag("dataset", "name"), single or double quotes
    m = re.match(r"""^tag\(\s*(["'])((?:(?!\1).)*)\1(?:\s*,\s*(["'])((?:(?!\3).)*)\3)?\s*\)$""", t)
    if m:
        t = m.group(4) if m.group(4) is not None else m.group(2)
    t = t.strip("'\" ")
    if "|" in t:
        t = t.split("|")[-1]
    return t.strip("'\" ")


# Separators a model may put between a tag's dataset and its name when it echoes a listed tag
# ("Demo L1-PA.BRG-TEMP", "Demo.L1-PA.BRG-TEMP", "Demo/…", "Demo: …", "Demo, …").
_DS_SEPS = (" ", ".", "/", ":", ",", ", ", ": ", " / ", " - ", " — ")


def _listed_name(ref: Any, g: "_Grounding") -> Optional[str]:
    """The listed attribute / tag a need's reference names, or None. A tag's name is matched
    WHOLE — dots and hyphens are part of it (L1-PA.BRG-TEMP), never word separators — and a
    reference that carries the tag's dataset in front ("Demo L1-PA.BRG-TEMP", tag("Demo", …))
    is that tag (finding #4: the dataset was joined to the name and the tag "was not in this
    plant")."""
    a = _norm_ref(ref)
    if not a:
        return None
    names = {name_key(n): n for n in g.input_names()}
    k = name_key(a)
    if k in names:
        return names[k]
    for p in g.points + g.all_points:
        ds, nm = str(p.get("dataset") or ""), str(p.get("name") or "")
        if not ds or not nm:
            continue
        if any(k == name_key(ds + sep + nm) for sep in _DS_SEPS):
            return nm
    return None


def _missing_needs(needs: List[Dict[str, Any]], g: "_Grounding") -> List[Dict[str, Any]]:
    """The needs nothing listed holds: null, a name that is not listed, or a listed name whose
    units are of another kind than the need's (a current mapped to a power). Each:
    {quantity, attribute, why}."""
    out = []
    for n in needs:
        q = _clean(n.get("quantity"), 80)
        a = n.get("attribute")
        a = _norm_ref(a) if isinstance(a, str) and a.strip() and a.strip().lower() != "null" else None
        if a is None:
            if _NOT_DATA_RE.search(q):
                continue                                  # a time, a limit: not plant data
            out.append({"quantity": q, "attribute": None, "why": None})
            continue
        listed = _listed_name(a, g)
        if listed is None:
            out.append({"quantity": q, "attribute": a, "why": f"{a} is not in this plant"})
            continue
        units, ptype = g.units_of(listed)
        want = _kind_classes(str(n.get("unit_kind") or "")) or _kind_classes(q)
        got = _unit_class(units)
        # volflow is not strict: a flow in % is the plant's flow (totals in %·h); a volume the
        # request names in m3 is the dimension guard's (R232(3)).
        strict = {"power", "energy", "voltage", "pressure", "massflow"}
        if want & strict and got and got not in want and not (got == "energy" and "power" in want):
            out.append({"quantity": q, "attribute": a,
                        "why": f"{a} is in {unit_display(units)}, not {_an(q)}"})
    return out


def _body_reads(body: str, outputs: Sequence[Any]) -> List[str]:
    """The names a body reads: quoted names that are not time expressions or output targets,
    the attribute of a rollup path c|'Attr', and tag("…") names."""
    stripped = re.sub(r"//[^\n]*", "", body or "")
    stripped = re.sub(r"\boutput\s+'(?:[^']|'')*'", "output", stripped)
    reads: List[str] = []
    for m in re.finditer(r"'((?:[^']|'')*)'", stripped):
        t = m.group(1).replace("''", "'").strip()
        if not t or _TIME_RE.match(t):
            continue
        reads.append(t)
    for m in re.finditer(r'tag\(\s*"([^"]*)"(?:\s*,\s*"([^"]*)")?\s*\)', stripped):
        reads.append(m.group(2) if m.group(2) is not None else m.group(1))
    outs = {name_key(o.get("attribute")) for o in outputs or [] if isinstance(o, dict)
            and isinstance(o.get("attribute"), str)}
    return _dedupe(r for r in reads if name_key(r) not in outs)


_TIME_RE = re.compile(r"^(?:\*|t|y|today|yesterday|now|bod|bow|bom|boy|shift_start|shift_end)"
                      r"(?:\s*[+-]\s*\d+(?:\.\d+)?\s*(?:ns|us|ms|s|m|h|d|w|mo|y))*$", re.I)


def _refusal_reason(topic: str, needs_labels: Sequence[str], facts: Sequence[str]) -> str:
    head = f"Can't draft {topic}"
    if needs_labels:
        head += f": it needs {_join(needs_labels)}"
    if facts:
        head += " — " + "; ".join(facts)
    return _sentence(head)


def _absent_words(text: str, g: "_Grounding") -> List[str]:
    uni = g.universe()
    out = []
    for w in _words(text):
        k = w.casefold()
        if k in _GENERIC_WORDS or len(k) < 4:
            continue
        if k not in uni and k.rstrip("s") not in uni:
            out.append(w.lower())
    return _dedupe(out)


_GENERIC_WORDS = frozenset("""output outputs result results data value plant combined overall current
calculate compute show give find get make track measure report monitor plot trend need want much many
well each every total sum yesterday today hourly nightly""".split())


def _no_data_reason(topic: str, request: str, g: "_Grounding", extra: Sequence[str] = ()) -> Optional[str]:
    """"This plant has no data about a combustion turbine — none of its tags or templates mentions
    combustion, turbine or power." — only when the request's own subject words appear nowhere in
    the grounding (an absent subject, not a hard formula)."""
    req_absent = _absent_words(request, g)
    uni = g.universe()
    present = [w for w in _words(request) if len(w) >= 4 and w.casefold() not in _GENERIC_WORDS
               and (w.casefold() in uni or w.casefold().rstrip("s") in uni)]
    if not req_absent or present:
        return None                       # the subject exists here: not "no data about it"
    words = _dedupe(req_absent + [w for w in _absent_words(" ".join(extra), g) if w not in req_absent])
    what = "tags or templates" if "points" in g.scope else "attributes or templates"
    # Past MAX_INDEX_ELEMENTS the plant index stopped reading elements, so "none of its ..." would
    # claim more than was looked at. The sentence says what was actually seen instead.
    if g.index is not None and g.index.truncated:
        return _sentence(f"Couldn't find data for {topic} — none of the {what} this draft could see "
                         f"mentions {_join(words[:6], 'or')} (this plant is larger than one draft "
                         f"reads, so there may be more elsewhere)")
    return _sentence(f"This plant has no data about {topic} — none of its {what} mentions "
                     f"{_join(words[:6], 'or')}")


def _dimension_issue(request: str, obj: Dict[str, Any], v: Dict[str, Any]) -> Optional[str]:
    """The result's unit (PremHist's validate `outputs[].units`, never parsed from text) against
    what the request implies: a dimensionless quantity (efficiency, ratio, %, …) must come out
    without a unit (or in %); a unit the request names must be of that kind. Returns the plain
    sentence, or None."""
    outs = [o for o in v.get("outputs") or [] if isinstance(o, dict)]
    if not outs:
        return None
    names = " ".join(str(o.get("name") or "") for o in outs)
    if _DIMENSIONLESS_RE.search(request) or _DIMENSIONLESS_RE.search(names) or _PCT_NAME_RE.search(names):
        for o in outs:
            u = o.get("units")
            if u and _unit_class(u) != "percent":
                pct = re.search(r"efficienc|percent|%", request + " " + names, re.I)
                return (f"The draft's result would be in {unit_display(u)}, which isn't "
                        f"{'a percentage' if pct else 'a plain ratio'} — the formula doesn't make "
                        f"physical sense, so it wasn't offered.")
    wanted = {k for k, rx in _REQ_UNITS if rx.search(request)}
    if wanted:
        for o in outs:
            got = _unit_class(o.get("units"))
            if got and got not in wanted:
                return (f"The draft's result would be in {unit_display(o.get('units'))}, but the request "
                        f"asks for {_join([_CLASS_PLAIN.get(k, k) for k in sorted(wanted)], 'or')} — the "
                        f"plant's data cannot give that, so it wasn't offered.")
    return None


_NO_DATA_CODES = frozenset({"unknown_reference", "not_a_tag", "undefined_name", "no_active_instance"})


def _REQ_UNITS_HIT(request: str) -> bool:
    """The request names a result unit (then a unit mismatch is repaired first)."""
    return any(rx.search(request) for _k, rx in _REQ_UNITS)


def _refuses(obj: Dict[str, Any], g: "_Grounding") -> bool:
    needs = _needs_of(obj)
    if needs and _missing_needs(needs, g):
        return True
    cannot = obj.get("cannot")
    return bool(isinstance(cannot, str) and cannot.strip()
                and not (isinstance(obj.get("body"), str) and obj["body"].strip()))


def _model_refusal(obj: Dict[str, Any], g: "_Grounding", request: str, notes: List[str],
                   authoring: Dict[str, Any]) -> Dict[str, Any]:
    """The refusal the model's own reading leads to, in plain words: the headline names the
    missing quantities (from "needs") and what the plant has instead; suggestions come from the
    grounding, never from the model."""
    needs = _needs_of(obj) or []
    miss = _missing_needs(needs, g)
    topic = _clean(obj.get("topic"), 80) or _clean(obj.get("intent"), 80) or "this calc"
    cannot = _clean(obj.get("cannot"), 240).rstrip(".")
    labels = [m["quantity"] for m in miss]
    facts = [m["why"] for m in miss if m["why"]]
    if cannot and labels:
        # The headline already says what it needs: keep only what the model adds after that.
        cannot = re.sub(r"^\s*(it\s+)?(needs|requires)\b[^;:.—]*[;:.—]\s*", "", cannot, flags=re.I).strip(" ;,.")
    if cannot:
        facts.append(cannot[0].lower() + cannot[1:] if cannot[:2] != cannot[:2].upper() else cannot)
    reason = None
    miss_q = {m["quantity"] for m in miss}
    held = [n for n in needs if isinstance(n.get("attribute"), str) and n["attribute"].strip()
            and n["attribute"].strip().lower() != "null" and _clean(n.get("quantity"), 80) not in miss_q]
    # "No data about X" when every need is unheld — or, without needs, when the model's reason
    # is about missing data (never for "sending email is not a calculation").
    about_data = bool(re.search(r"\b(tags?|data|attributes?|listed|available|measur\w*|signals?|sensors?)\b",
                                cannot, re.I))
    if (labels and not held) or (not needs and about_data):
        reason = _no_data_reason(topic, request, g, labels)
    reason = reason or _refusal_reason(topic, labels, facts[:3])
    intent = _clean(obj.get("intent"), 300)
    understood = intent if intent and name_key(intent) not in name_key(reason) else None
    return _fail("no_draft", reason, reason=reason, missing=labels, understood=understood,
                 suggestions=_suggestions(g, request + " " + " ".join(labels),
                                          avoid=[m["attribute"] for m in miss if m["attribute"]]),
                 notes=notes, authoring=authoring)


def _reads_outside_needs(obj: Dict[str, Any], g: Optional["_Grounding"] = None) -> List[str]:
    needs = _needs_of(obj)
    if not needs:
        return []
    mapped = {name_key(_norm_ref(n.get("attribute"))) for n in needs
              if isinstance(n.get("attribute"), str) and n["attribute"].strip()}
    if g is not None:                       # a dataset-qualified need maps to the tag's name
        mapped |= {name_key(x) for x in (_listed_name(n.get("attribute"), g) for n in needs
                                         if isinstance(n.get("attribute"), str)) if x}
    if not mapped:
        return []
    reads = _body_reads(obj.get("body") if isinstance(obj.get("body"), str) else "", obj.get("outputs") or [])
    return [r for r in reads if name_key(r) not in mapped]


# ── the engine ────────────────────────────────────────────────────────

async def draft_calc(request: str, *, ground: Any, validate: Optional[Callable[..., Any]] = None,
                     llm_call: Optional[Callable[..., Any]] = None, current: Optional[Dict[str, Any]] = None,
                     context: Optional[Dict[str, Any]] = None, model: Optional[str] = None) -> Dict[str, Any]:
    """Draft (or revise) a PremHist calc from a plain-English request. **A coroutine.** Never raises.

    - `ground`: duck-typed (sync or async) `calc_catalog()`, `template(id)`, `template_elements(id)`,
      `element(id)`, `children(id)`, `sites()`, `search_points(q)` (points scope), `calc(id)` (when
      `context.calc` is given) — e.g. `premhist_rest.PremhistRest`. Only reads.
    - `validate(document, calc)`: PremHist's `POST /api/v1/calcs/validate` answer (API.md §11.4);
      default `ground.validate_calc`. **The only judge** of a draft.
    - `llm_call(messages, *, format, model, temperature, options)` → a message dict or text;
      default `llm_runtime.call_ollama_chat` with `settings.calc_authoring_timeout_s`. `model`
      defaults to `settings.ollama_model`.
    - `current`: the editor's document (a revision), or None.
    - `context`: `{"scope": Scope?, "calc": id?, "name": text?, "scope_try": Scope?, "keep_scope":
      bool?}` — the scope comes from here (or `current`, or the calc `calc`), never from the model;
      `scope_try` = a scope the person clicked (R240 / R241), `keep_scope` = no proposal.

    Returns `{ok: True, document, canonical, hash, readback (PremHist's), problems (warnings),
    intent, instances, cost, authoring: {kind: "llm_proposed", model, prompt_sha256, run_id,
    request, repairs, model_calls, elapsed_s}, notes, seconds}` (+ `scope_change` after a
    `scope_try`), `{ok: False, code: "scope_proposal", scope_proposal: {scope, label, why, from,
    from_label}, document, …}`, `{ok: False, code: "choose_scope", choices: [{scope, label, why}],
    keep}` or `{ok: False, code: bad_request | too_broad | no_draft | llm_unavailable, error,
    problems, understood, notes}`. Nothing is saved."""
    t0 = time.monotonic()
    try:
        res = await _draft_calc(request, ground=ground, validate=validate, llm_call=llm_call,
                                current=current, context=context, model=model)
    except Exception as e:  # never raises (task_authoring's rule); a bug is a visible failure
        res = _fail("no_draft", f"drafting failed: {type(e).__name__}: {e}")
    # R246: one local JSON line per request (never the network; never raises).
    _dlog.record(request, context, res, time.monotonic() - t0)
    return res


async def _draft_calc(request: str, *, ground: Any, validate: Optional[Callable[..., Any]],
                      llm_call: Optional[Callable[..., Any]], current: Optional[Dict[str, Any]],
                      context: Optional[Dict[str, Any]], model: Optional[str]) -> Dict[str, Any]:
    t0 = time.monotonic()
    notes: List[str] = []
    if not isinstance(request, str) or not request.strip():
        return _fail("bad_request", "describe the calc you want")
    if len(request) > MAX_REQUEST_CHARS:
        return _fail("bad_request", f"the description is over {MAX_REQUEST_CHARS} characters")
    if current is not None and not isinstance(current, dict):
        return _fail("bad_request", "the current calc is not a calc document")
    if context is not None and not isinstance(context, dict):
        return _fail("bad_request", "context must be an object")
    ctx = dict(context or {})
    calc_id = ctx.get("calc")
    if calc_id is not None and (not isinstance(calc_id, int) or isinstance(calc_id, bool)):
        return _fail("bad_request", "context.calc must be a calc id")

    fixed_name: Optional[str] = _clean(ctx.get("name"), 200) or None
    scope = _scope_of(ctx, current)
    if calc_id is not None:
        try:
            c = await _g(ground.calc, calc_id)
        except Exception as e:
            return _fail("bad_request", f"could not read PremHist to ground the request: {e}")
        if not isinstance(c, dict):
            return _fail("bad_request", f"calc {calc_id} does not exist")
        cs = _scope_of({"scope": c.get("scope")}, None)
        if cs is not None:
            scope = cs                                  # the calc's scope is its scope for life
        fixed_name = fixed_name or (_clean(c.get("name"), 200) or None)
    scope_try = ctx.get("scope_try")
    keep = ctx.get("keep_scope") is True
    if scope_try is not None and _scope_of({"scope": scope_try}, None) is None:
        return _fail("bad_request", "context.scope_try must be {\"template\": id}, {\"element\": id} or "
                                    "{\"points\": {...}}")

    # R239-R242: the whole plant, read once (best effort: None turns every plant-wide step off).
    try:
        index = await _cs.build_index(ground)
    except Exception:
        index = None
    if index is not None and index.truncated:
        # Said out loud, never silent (the rule asset_map_lines already follows for its own caps):
        # the plant-wide steps below - the scope plan, the formula checks, the plant-wide source
        # search - ran on the part of the plant that was read, so an asset past the cap is not
        # offered and is not refused either.
        notes.append(f"this plant is larger than one draft reads ({_cs.MAX_INDEX_ELEMENTS} elements): "
                     "the scope and source suggestions cover the part that was read")
    if scope is None and index is None and scope_try is None:
        return _fail("bad_request", "choose what the calc runs on first (a template, an element or "
                                    "a site's points)")
    common: Dict[str, Any] = dict(ground=ground, validate=validate, llm_call=llm_call, fixed_name=fixed_name,
                                  model=model, t0=t0, index=index)

    # The person clicked a proposed scope (or a choice): draft for it; the answer says the scope
    # changed (the viewer asked for exactly this, so it is not a silent change).
    if scope_try is not None:
        st = _scope_of({"scope": scope_try}, None)
        if not await _scope_exists(ground, st):
            return _fail("bad_request", f"the scope {json.dumps(st)} does not exist")
        same = _cs.scope_key(st) == _cs.scope_key(scope)
        res = await _attempt(request, scope=st, current=current if same else None,
                             calc_id=calc_id if same else None, notes=list(notes), **common)
        if res.get("ok") and not same:
            res["scope_change"] = {"from": scope, "to": st, "label": _cs.label_of(index, st),
                                   "from_label": _cs.label_of(index, scope)}
        return _public(res)

    # Plant-wide plausibility first (R242 / R243): a formula or a request the WHOLE plant cannot
    # serve is refused before any scope is proposed or any model is asked.
    g0 = _Grounding()
    g0.index = index
    g0.scope = scope or {}
    gap = _physics_gap(request, g0) if index is not None else None
    if gap is not None:
        reason = _refusal_reason(gap["topic"], gap["needs"], gap["facts"])
        return _fail("no_draft", reason, reason=reason, missing=gap["missing"],
                     suggestions=_suggestions(g0, request + " " + " ".join(gap["missing"])), notes=notes)
    plan = _cs.plan_scope(request, scope, index)
    if keep and plan.kind in ("propose", "choose"):
        plan = _cs.Plan()                               # "Keep <scope>": draft where it is
    if plan.kind == "refuse":
        reason = _sentence(plan.refusal["reason"])
        return _fail("no_draft", reason, reason=reason, suggestions=_suggestions(g0, request), notes=notes)
    if plan.kind == "choose":
        return _choose_result(plan, scope, index, notes)
    target = plan.proposal["scope"] if plan.kind == "propose" else scope
    if target is None:
        return _fail("bad_request", "choose what the calc runs on first (a template, an element or "
                                    "a site's points)")
    # R255 amendment: from an element or specific tags (the person's own scope) the same check is
    # judged on that element's attributes / the tags the request names; a template that measures
    # every input is offered as choices with no model call at all.
    if not keep and _cs.scope_key(scope) is not None and _cs.scope_key(scope)[0] in ("element", "points"):
        fs0 = _formula_scope(request, scope, index)
        if fs0 is not None and fs0[1] is not None:
            return _formula_choices(request, scope, fs0[0], fs0[1], index, notes)
    # R255: a formula whose measured inputs this scope lacks but another template has (pump
    # efficiency on a pump measuring flow only in %, while Feed Pump measures flow, pressure and
    # power) is proposed there before any model call; kept where it is, it is refused in place.
    fs = _formula_scope(request, target, index)
    if fs is not None:
        fgap, alt = fs
        if alt is not None and not keep:
            plan = _cs.Plan("propose", proposal=alt)
            target = alt["scope"]
        elif keep:
            reason = _refusal_reason(fgap["topic"], fgap["needs"], fgap["facts"])
            return _fail("no_draft", reason, reason=reason, missing=fgap["missing"],
                         suggestions=_suggestions(g0, request + " " + " ".join(fgap["missing"])), notes=notes)
    same = _cs.scope_key(target) == _cs.scope_key(scope)
    res = await _attempt(request, scope=target, current=current if same else None,
                         calc_id=calc_id if same else None, notes=list(notes), **common)
    if plan.kind == "propose":
        return _public(_as_proposal(res, plan.proposal, scope, index))
    # R242: refused for want of a source in THIS scope — look in the whole plant; a source
    # elsewhere is a proposal (drafted there), never a refusal.
    if (not res.get("ok") and res.get("_missing") and index is not None and not keep
            and time.monotonic() - t0 < TOTAL_BUDGET_S / 2):
        alt = _cs.source_scope(res["_missing"], request, scope, index)
        if alt is not None:
            res2 = await _attempt(request, scope=alt["scope"], current=None, calc_id=None,
                                  notes=list(notes), **common)
            if res2.get("ok"):
                return _public(_as_proposal(res2, alt, scope, index))
    return _public(res)


async def preview_grounding(request: str, *, ground: Any, current: Optional[Dict[str, Any]] = None,
                            context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What the model WOULD be shown for this request, without asking it. **A coroutine.** Never
    raises.

    Most complaints about Describe are complaints about its grounding - "it says my plant has no
    X", "it used the wrong tag", "it refused and I do not know what it looked at". Until now the
    only way to see the grounding was to infer it from a model call that had already happened: it
    is built fresh per request, never stored, and the outcome log keeps the request and the result
    but not what the model was told. This runs everything up to the model call and stops:
    the plant index, the scope planner, the grounding, the prompt. No Ollama, no draft, nothing
    saved - so it is cheap enough to run on every puzzling request.

    Returns `{ok: True, kind: "calc", scope, scope_label, plan, messages: [{role, content}],
    chars, est_tokens, token_limit, too_broad, index: {...}, attributes: [...], tags_found: [...],
    seconds}`, or the same `{ok: False, code, error}` shapes draft_calc uses."""
    t0 = time.monotonic()
    try:
        return await _preview_grounding(request, ground=ground, current=current, context=context, t0=t0)
    except Exception as e:  # noqa: BLE001 - as draft_calc: a bug is a visible failure
        return _fail("bad_request", f"could not build the grounding: {type(e).__name__}: {e}")


async def _preview_grounding(request: str, *, ground: Any, current: Optional[Dict[str, Any]],
                             context: Optional[Dict[str, Any]], t0: float) -> Dict[str, Any]:
    if not isinstance(request, str) or not request.strip():
        return _fail("bad_request", "describe the calc you want")
    if len(request) > MAX_REQUEST_CHARS:
        return _fail("bad_request", f"the description is over {MAX_REQUEST_CHARS} characters")
    ctx = dict(context or {})
    scope = _scope_of(ctx, current)
    try:
        index = await _cs.build_index(ground)
    except Exception:
        index = None
    # The scope the planner would use, by the same rules draft_calc follows.
    plan = _cs.plan_scope(request, scope, index)
    if ctx.get("keep_scope") is True and plan.kind in ("propose", "choose"):
        plan = _cs.Plan()
    target = scope
    if ctx.get("scope_try") is not None:
        target = _scope_of({"scope": ctx["scope_try"]}, None) or scope
    elif plan.kind == "propose":
        target = plan.proposal["scope"]

    # What the planner decided, reported whether or not there is a scope to ground against.
    idx_info = ({"elements": len(index.elements), "templates": len(index.templates),
                 "points": len(index.points), "truncated": index.truncated,
                 "element_reads": index.reads, "cap": _cs.MAX_INDEX_ELEMENTS,
                 # How old the plant picture is. A grounding preview exists to answer "what did
                 # it look at", and "a 54-second-old copy" is part of that answer: an asset added
                 # since is not in it.
                 "from_cache": index.from_cache, "age_s": index.cache_age_s,
                 "ttl_s": _cs.index_ttl_s()}
                if index is not None else None)
    plan_out: Dict[str, Any] = {"ok": True, "kind": "calc", "plan": plan.kind or "fits",
                                "scope_from": scope, "index": idx_info, "notes": []}
    if plan.kind == "propose" and plan.proposal:
        plan_out["proposal"] = {"label": plan.proposal.get("label"), "why": plan.proposal.get("why")}
    if plan.kind == "choose":
        plan_out["choices"] = [{"label": c.get("label"), "why": c.get("why")} for c in plan.choices]
    if plan.kind == "refuse" and plan.refusal:
        plan_out["would_refuse"] = plan.refusal

    # A refusal happens BEFORE any grounding, and a question has no scope to ground against yet:
    # in both cases there is no prompt to show, and saying so is the answer. Returning the
    # "choose what the calc runs on first" error here would have been doubly misleading - the
    # planner had in fact reached a verdict, and it is the verdict the tester came to see.
    if plan.kind == "refuse":
        plan_out["messages"] = None
        plan_out["notes"] = ["this request is refused before any model call; no prompt is built"]
        plan_out["seconds"] = round(time.monotonic() - t0, 3)
        return plan_out
    if target is None:
        plan_out["messages"] = None
        plan_out["notes"] = ["no scope yet: the planner would ask which one first, so no prompt "
                             "is built. Send context.scope (or context.scope_try) to see one."]
        plan_out["seconds"] = round(time.monotonic() - t0, 3)
        return plan_out

    g = await _ground(ground, target, request, index)
    user = "REQUEST: " + request.strip() + "\n\n" + _grounding_text(g)
    if isinstance(current, dict) and isinstance(current.get("body"), str) and current["body"].strip():
        user = _current_text(current) + "\n\n" + user
    messages = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]
    chars = sum(len(m["content"]) for m in messages)
    out: Dict[str, Any] = {
        **plan_out,
        "scope": target, "scope_label": _cs.label_of(index, target),
        "messages": messages,
        "chars": chars, "est_tokens": chars // 4, "token_limit": MAX_PROMPT_TOKENS,
        "too_broad": chars // 4 > MAX_PROMPT_TOKENS,
        "attributes": [str(a.get("name")) for a in g.attributes],
        "tags_found": [str(p.get("name")) for p in g.points],
        "asset_map_lines": len(g.asset_lines),
        "asset_bindings_shown": g.asset_shown, "asset_bindings_total": g.asset_total,
        "sites": [str(s.get("name")) for s in g.sites],
        "catalog_functions": len(g.catalog.get("functions") or []),
        "notes": list(g.notes),
        "seconds": round(time.monotonic() - t0, 3),
    }
    if index is not None and index.truncated:
        out["notes"] = _dedupe(list(out["notes"]) + [
            f"this plant is larger than one draft reads ({_cs.MAX_INDEX_ELEMENTS} elements): the "
            "scope and source suggestions cover the part that was read"])
    return out


async def _scope_exists(ground: Any, scope: Dict[str, Any]) -> bool:
    try:
        if "template" in scope:
            return isinstance(await _g(ground.template, int(scope["template"])), dict)
        if "element" in scope:
            return isinstance(await _g(ground.element, int(scope["element"])), dict)
    except Exception:
        return False
    return "points" in scope


def _public(res: Dict[str, Any]) -> Dict[str, Any]:
    """The engine's answer without its private bookkeeping."""
    return {k: v for k, v in res.items() if not k.startswith("_")}


def _as_proposal(res: Dict[str, Any], prop: Dict[str, Any], from_scope: Optional[Dict[str, Any]],
                 index: Optional[_cs.PlantIndex]) -> Dict[str, Any]:
    """R240: a valid draft for a scope the person did not pick is a PROPOSAL, never applied
    without their click: `{ok: false, code: "scope_proposal", reason, scope_proposal: {scope, label,
    why, from, from_label}, document, readback, …}`. A failed draft stays the failure it is (with a
    note saying which scope it was drafted for)."""
    label = prop.get("label") or _cs.label_of(index, prop.get("scope"))
    if not res.get("ok"):
        out = dict(res)
        out["notes"] = _dedupe(list(res.get("notes") or []) + [f"drafted for {label} — the scope that "
                                                               f"fits the request"])
        return out
    out = dict(res)
    out["ok"] = False
    out["code"] = "scope_proposal"
    reason = _sentence(f"This fits better as {label}")
    out["reason"] = reason
    out["error"] = reason
    out["understood"] = res.get("intent") or None
    out["missing"] = []
    out["suggestions"] = []
    out["scope_proposal"] = {"scope": prop.get("scope"), "label": label, "why": prop.get("why"),
                             "from": from_scope, "from_label": _cs.label_of(index, from_scope)}
    return out


def _choose_result(plan: "_cs.Plan", scope: Optional[Dict[str, Any]], index: Optional[_cs.PlantIndex],
                   notes: List[str]) -> Dict[str, Any]:
    """R241: a request that reads two ways is a question with clickable choices; nothing drafted."""
    labels = [c["label"] for c in plan.choices]
    if len(labels) == 2:
        q = f"Which do you mean: {labels[0]}, or {labels[1]}?"
    else:
        q = "What should it run on: " + "; ".join(labels) + "?"
    keep = {"scope": scope, "label": _cs.label_of(index, scope)} if scope is not None else None
    return {"ok": False, "code": "choose_scope", "kind": "ambiguous", "error": q, "reason": q, "choices": plan.choices,
            "keep": keep, "problems": [], "understood": None, "notes": _dedupe(notes), "missing": [],
            "suggestions": []}


async def _attempt(request: str, *, ground: Any, validate: Optional[Callable[..., Any]],
                   llm_call: Optional[Callable[..., Any]], current: Optional[Dict[str, Any]],
                   scope: Dict[str, Any], calc_id: Optional[int], fixed_name: Optional[str],
                   model: Optional[str], t0: float, index: Optional[_cs.PlantIndex],
                   notes: List[str]) -> Dict[str, Any]:
    """One draft for one scope: ground, plausibility, the model + PremHist repair loop, the result
    (the C4 flow as it was; `_missing` = the needs nothing in the scope holds, for R242)."""
    try:
        g = await _ground(ground, scope, request, index)
    except Exception as e:
        return _fail("bad_request", f"could not read PremHist to ground the request: {e}")
    if not g.catalog.get("functions"):
        return _fail("bad_request", "PremHist did not return its calc catalog")
    if "template" in scope and g.element_count == 0:
        notes.append("this template has no elements yet: the draft can only be checked once one exists")

    # A formula whose inputs this plant does not measure (efficiency without a measured power,
    # power from a current without a voltage …): refused in plain words before any model call —
    # model-independent, so a dimensionally legal but meaningless formula can never be offered.
    gap = _physics_gap(request, g)
    if gap is not None:
        reason = _refusal_reason(gap["topic"], gap["needs"], gap["facts"])
        return _fail("no_draft", reason, reason=reason, missing=gap["missing"],
                     suggestions=_suggestions(g, request + " " + " ".join(gap["missing"])), notes=notes)

    def _messages() -> List[Dict[str, Any]]:
        user = "REQUEST: " + request.strip() + "\n\n" + _grounding_text(g)
        if isinstance(current, dict) and isinstance(current.get("body"), str) and current["body"].strip():
            user = _current_text(current) + "\n\n" + user
        return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]

    def _too_big(ms: List[Dict[str, Any]]) -> bool:
        return sum(len(m["content"]) for m in ms) // 4 > MAX_PROMPT_TOKENS

    messages = _messages()
    if _too_big(messages) and "points" in scope:
        # R251: a larger store is summarized, most relevant first, until the prompt fits.
        for lv in _POINTS_LEVELS:
            g.points_cap = lv["points"]
            g.families_cap = lv["families"]
            g.templates_cap = lv["templates"]
            g.asset_lines, g.asset_shown, g.asset_total = _cs.asset_map_lines(
                index, request, max_lines=lv["lines"], max_entries=lv["entries"], relevant_only=lv["relevant"])
            messages = _messages()
            if not _too_big(messages):
                break
    if _too_big(messages):
        if "points" in scope:
            return _fail("too_broad", "that request matches too many tags to draft against at once — "
                                      "name the tags you mean (e.g. SIM-TI-401.PV), or choose a template "
                                      "or element instead", notes=notes)
        return _fail("too_broad", "that scope is too large to draft against at once — choose a "
                                  "template or element with fewer attributes", notes=notes)

    if model is None:
        try:
            from .settings import get_settings
            model = get_settings().ollama_model
        except Exception:
            model = "default"
    if llm_call is None:
        try:
            from .llm_runtime import call_ollama_chat
        except Exception as e:
            return _fail("llm_unavailable", f"the local model runtime is not available: {e}", notes=notes)
        llm_call = _default_llm_call(call_ollama_chat)
    if validate is None:
        validate = getattr(ground, "validate_calc", None)
        if validate is None:
            return _fail("bad_request", "no PremHist validation is available to check a draft", notes=notes)

    run_id = uuid.uuid4().hex
    last_dim: Optional[str] = None
    convo = list(messages)
    last_obj: Optional[Dict[str, Any]] = None
    last_problems: List[Dict[str, Any]] = []
    calls = 0
    best: Optional[Dict[str, Any]] = None            # a valid draft that still had a repairable warning
    out_of_time = False
    ceiling = _round_ceiling_s()
    for attempt in range(1, MAX_TRIES + 1):
        # A repair round must be able to FINISH inside the budget, not merely start inside it.
        if attempt > 1 and time.monotonic() + ceiling > t0 + TOTAL_BUDGET_S:
            out_of_time = True
            break
        sent = [dict(m) for m in convo]
        try:
            calls += 1
            res = llm_call(sent, format=DRAFT_SCHEMA, model=str(model), temperature=0,
                           options={"num_predict": NUM_PREDICT})
            if inspect.isawaitable(res):
                res = await res
        except Exception as e:
            if best is not None:
                break
            return _fail("llm_unavailable", f"the local model did not answer: {type(e).__name__}: {e}",
                         notes=notes)
        content = _content_of(res)
        if content.strip().startswith(_OLLAMA_TIMEOUT_SENTINEL):
            if best is not None:
                break
            return _fail("llm_unavailable", "the local model did not answer: "
                         + content.strip().splitlines()[0], notes=notes)
        obj, perr = _parse_reply(content)
        if obj is None:
            last_problems = [{"code": "reply", "severity": "error", "message": perr, "line": None,
                              "col": None, "element": None}]
            feedback = f"That reply could not be used: {perr}. Reply again with the JSON object only."
        elif _refuses(obj, g):
            # The model's needs name a quantity nothing listed holds (or it says "cannot" and
            # drafts nothing): an honest refusal, its headline built from the needs, and no
            # PremHist call (nothing to judge).
            elapsed = time.monotonic() - t0
            refusal = _model_refusal(obj, g, request, notes,
                                     {"kind": "llm_proposed", "model": str(model), "run_id": run_id,
                                      "request": request, "model_calls": calls,
                                      "elapsed_s": round(elapsed, 3), "prompt_sha256": _sha(sent),
                                      "repairs": calls - 1})
            refusal["_missing"] = (_missing_needs(_needs_of(obj) or [], g)
                                   or [{"quantity": _clean(obj.get("topic") or obj.get("intent"), 80),
                                        "attribute": None}])
            return refusal
        else:
            last_obj = obj
            if isinstance(obj.get("cannot"), str) and obj["cannot"].strip():
                notes.append("the assistant noted: " + _clean(obj["cannot"], 300))
            doc = _build_document(obj, scope, current, fixed_name)
            _fold_document(doc, g)
            guard = _limit_guard(doc, request)
            if guard:
                notes.append(guard)
            try:
                v = await _g(validate, doc, calc_id)
            except Exception as e:
                return _fail("bad_request", f"PremHist could not check the draft: {e}",
                             understood=_understood(obj), notes=notes)
            if not isinstance(v, dict):
                return _fail("bad_request", "PremHist's check of the draft was not understood",
                             understood=_understood(obj), notes=notes)
            errors, repairable, other = _verdict_sqc(v)
            # §10.36.20: the only error is the missing limits → `needs_limits` (never repaired by
            # inventing numbers, never a success).
            if errors and all(p.get("code") == "sqc_limits_missing" for p in errors):
                elapsed = time.monotonic() - t0
                return {
                    "ok": False, "code": "needs_limits",
                    "error": "this chart needs limits: type the center line and the two control "
                             "limits, or name the attributes that hold them",
                    "document": doc, "canonical": v.get("canonical"), "hash": v.get("hash"),
                    "readback": v.get("readback"), "problems": errors + repairable + other,
                    "understood": _understood(obj), "intent": _clean(obj.get("intent"), 2000),
                    "notes": _dedupe(notes), "seconds": round(elapsed, 3),
                    "authoring": {"kind": "llm_proposed", "model": str(model), "run_id": run_id,
                                  "request": request, "model_calls": calls,
                                  "elapsed_s": round(elapsed, 3), "prompt_sha256": _sha(sent),
                                  "repairs": calls - 1},
                }
            candidate = {"doc": doc, "v": v, "obj": obj, "warnings": repairable + other,
                         "sent": sent, "attempt": attempt}
            if not errors:
                # Valid is not enough: the result's unit (PremHist's, from validate's outputs)
                # must fit what was asked — an "efficiency" in %²/kW is never offered.
                dim = _dimension_issue(request, obj, v)
                if dim and not _REQ_UNITS_HIT(request):
                    elapsed = time.monotonic() - t0
                    return _fail("no_draft", dim, reason=dim, understood=_clean(obj.get("intent"), 300) or None,
                                 suggestions=_suggestions(g, request), notes=notes,
                                 authoring={"kind": "llm_proposed", "model": str(model), "run_id": run_id,
                                            "request": request, "model_calls": calls,
                                            "elapsed_s": round(elapsed, 3), "prompt_sha256": _sha(sent),
                                            "repairs": calls - 1})
                if dim:
                    last_dim = dim                       # a unit the request named: repair, then refuse
                    repairable = repairable + [{"code": "result_unit", "severity": "warning", "message": dim,
                                                "line": None, "col": None, "element": None}]
                    candidate = None
                else:
                    extra = _reads_outside_needs(obj, g)
                    if extra:
                        repairable = repairable + [{
                            "code": "needs", "severity": "warning", "line": None, "col": None, "element": None,
                            "message": "the body reads " + ", ".join(f"'{x}'" for x in extra)
                                       + ", which is not in \"needs\": list it in needs with the quantity it "
                                         "stands for, or do not read it (set \"cannot\" if a need has no listed name)"}]
            if candidate is not None and not errors and not repairable:
                best = candidate
                break
            if candidate is not None and not errors:
                best = candidate                     # valid; one more round may clear the warning
            last_problems = errors or repairable
            feedback = ("PremHist checked that calc and reported:\n"
                        + "\n".join("- " + _problem_line(p) for p in last_problems[:MAX_PROBLEMS_SENT])
                        + "\nFix exactly these problems. Reply with the WHOLE corrected JSON object only.")
        if attempt < MAX_TRIES:
            convo = sent + [{"role": "assistant", "content": content},
                            {"role": "user", "content": feedback}]

    elapsed = time.monotonic() - t0
    authoring_base = {"kind": "llm_proposed", "model": str(model), "run_id": run_id, "request": request,
                      "model_calls": calls, "elapsed_s": round(elapsed, 3)}
    if out_of_time:
        notes.append(f"stopped after {calls} model call(s): drafting has a {TOTAL_BUDGET_S:.0f} s budget")
    if best is None:
        topic = (_clean((last_obj or {}).get("topic"), 80) or "this calc")
        suggestions: List[str] = []
        reason = last_dim
        if reason is None and (any(p.get("code") in _NO_DATA_CODES for p in last_problems)
                               or any("specific tags" in str(p.get("message")) for p in last_problems)):
            reason = _no_data_reason(topic, request, g)
            if reason:
                suggestions = _suggestions(g, request)
        if reason is None:
            n = len(last_problems)
            reason = _sentence(f"Couldn't draft {topic}: PremHist still found {n} problem{'' if n == 1 else 's'} "
                               f"in it after {calls} tr{'y' if calls == 1 else 'ies'} — they are listed below")
        elif last_dim:
            suggestions = _suggestions(g, request)
        failed = _fail("no_draft", reason, reason=reason, problems=last_problems,
                       understood=_understood(last_obj), notes=notes, suggestions=suggestions,
                       authoring=dict(authoring_base, prompt_sha256=_sha(convo), repairs=calls - 1))
        if any(p.get("code") in _NO_DATA_CODES for p in last_problems):
            failed["_missing"] = (_missing_needs(_needs_of(last_obj or {}) or [], g)
                                  or [{"quantity": topic, "attribute": None}])
        return failed
    v = best["v"]
    if any(p.get("code") == "empty_window_at_trigger" for p in best["warnings"]):
        notes.append("PremHist warns that a window is empty at the trigger time — check the read-back "
                     "and the dry run before approving")
    coverage = _coverage_note(v, g.template_name)
    if coverage:
        notes.append(coverage)
    for p in best["warnings"]:
        if p.get("code") == "opaque_unit":
            notes.append(f"a unit PremHist does not know: {_clean(p.get('message'), 200)}")
    authoring = dict(authoring_base, prompt_sha256=_sha(best["sent"]), repairs=best["attempt"] - 1)
    if calls > 1:
        notes.insert(0, f"drafted in {elapsed:.1f} s ({calls} model calls)")
    return {
        "ok": True,
        "document": best["doc"],
        "canonical": v.get("canonical"),
        "hash": v.get("hash"),
        "readback": v.get("readback"),                  # PremHist's (R26 / R119), never the model's
        "problems": best["warnings"],
        "intent": _clean(best["obj"].get("intent"), 2000),
        "instances": v.get("instances") or [],
        "cost": v.get("cost"),
        "authoring": authoring,
        "notes": _dedupe(notes),
        "seconds": round(elapsed, 3),
    }


__all__ = ["draft_calc", "preview_grounding", "DRAFT_SCHEMA", "EMPTY_WINDOW_HINT", "MAX_TRIES"]
