"""
display_pipes.py — Describe's side of connected piping (design_displays.md §5.7.10 item 9, read
with the block "As adopted — the build contract in one place", which wins where they differ).

What lives here (pure: no I/O, no LLM, no import of display_authoring's private internals):

- The vocabulary: the five line types (LINES), the source family that makes a new pipe's flow
  default to `network` (SOURCE_GLYPHS), the glyphs that have no connection points
  (PORTLESS_GLYPHS — mirrors `PH.DisplayGlyphs.ports(g)` being empty in the viewer).
- Recognizing a request's connect phrases deterministically (`parse_connects`): "connect A to B",
  "connect A and B", "run a steam line from A to B", "pipe A to B", "a duct from A to B" — with
  the line from the request's own words (`line_of_words`), a named flow tag ("with flow from
  FT-301") and a quoted label ("labeled 'Main steam'"). An end said as a pronoun ("it", "them")
  is left to the model's `connect` op.
- Resolving an end to ONE symbol (`Connector.resolve_end`): the caller's `resolve` callback first
  (display_authoring passes its own id / reference machinery), then a phrase resolver over the
  display's symbols (id, label words, glyph words — `display_glyphs` is the one public table).
- Building the pipe Describe writes (`build_pipe`): auto-routed (no `bends`, no ports — the viewer
  owns the geometry, item 4), `from` / `to` = `{"symbol": id}`, `line`, `flow` (`network` when
  either end is a source-family glyph, else `never`; a named flow tag → `tag` + binding 1), the
  label, and a box = the hull of both ends' boxes.
- The model's side: the `connect` op and the draft `kind: "pipe"` symbol (schema fragments to merge
  into display_authoring's OPS / AUTHORING schemas, `normalize_connect_ops`, `check_connect_op`,
  `check_draft_pipe`) and the prompt lines (PROMPT_FIRST, PROMPT_OPS).
- The notes: "connected Pump P-501 to Tank T-301 (steam line)" and "did not connect …: <why>".
- After an edit: `detach_removed` (a removed end → an open end, or the pipe goes with it when it
  was auto-routed) and `refresh_boxes` (a Describe pipe's hull follows its moved equipment).

Pipes are exempt from the no-overlap footprint rule and are never "overlapping" (`is_pipe`; the
footprint / overlap code in display_authoring skips them).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from orchestrator.display_glyphs import (GLYPH_PHRASE_RE, GLYPH_REF_WORDS, GLYPH_WORDS,
                                         PORTLESS_GLYPHS as _PORTLESS_GLYPHS, request_glyphs)

# ── Vocabulary ──────────────────────────────────────────────────────────

LINES: Tuple[str, ...] = ("process", "steam", "gas", "bus", "signal")
FLOW_SOURCES: Tuple[str, ...] = ("tag", "network", "symbol", "always", "never")
# "As adopted", network flow: the pump / fan / compressor family.
SOURCE_GLYPHS: frozenset = frozenset((
    "pump", "pd_pump", "vertical_pump", "compressor", "fan", "bfp", "condensate_pump", "cw_pump",
    "fd_fan", "id_fan", "pa_fan"))
# The glyphs with no connection points now live in the catalog itself (display_glyphs), beside
# every other per-glyph fact, instead of being a SECOND hand-maintained list here that nothing
# ever checked against it: the two could name different glyphs, and neither would have said so.
# Re-exported, so anything importing display_pipes.PORTLESS_GLYPHS still works.
PORTLESS_GLYPHS: frozenset = _PORTLESS_GLYPHS
MAX_LABEL = 200

# How a note names a line type: "connected … (steam line)".
LINE_WORDS: Dict[str, str] = {
    "process": "process line", "steam": "steam line", "gas": "duct", "bus": "bus", "signal": "signal line"}

# Request words → line (item 9: "steam line" → steam; "duct" / "flue" / "air" → gas; "bus" → bus;
# "signal" → signal; else process). The type names themselves count too ("a gas line", "process").
_LINE_WORD_RE = re.compile(
    r"\b(?P<steam>steam)\b|\b(?P<gas>ducts?|ducting|ductwork|flues?|air|gas)\b|\b(?P<bus>bus(?:es|bar|bars)?)\b|"
    r"\b(?P<signal>signals?)\b|\b(?P<process>process)\b", re.I)
# A model's `line` value → one of LINES (a near-miss, never a guess: unknown → None = the default).
_LINE_ALIASES: Dict[str, str] = {
    "process": "process", "pipe": "process", "piping": "process", "water": "process", "liquid": "process",
    "steam": "steam", "gas": "gas", "air": "gas", "duct": "gas", "flue": "gas", "flue_gas": "gas",
    "bus": "bus", "busbar": "bus", "electrical": "bus", "power": "bus",
    "signal": "signal", "instrument": "signal", "control": "signal",
}


def is_pipe(sym: Any) -> bool:
    """Whether `sym` is a pipe symbol (exempt from the footprint rule, never "overlapping")."""
    return isinstance(sym, dict) and sym.get("type") == "pipe"


def line_of_words(text: str) -> str:
    """The line type a piece of request text says (the first line word in it), else process."""
    m = _LINE_WORD_RE.search(text or "")
    if not m:
        return "process"
    return next(k for k in ("steam", "gas", "bus", "signal", "process") if m.group(k))


def line_value(v: Any) -> Optional[str]:
    """A model's `line` → one of LINES ("steam line" → steam, "duct" → gas), else None."""
    if not isinstance(v, str):
        return None
    t = re.sub(r"[\s\-]+", "_", v.strip().lower())
    t = re.sub(r"_(?:line|pipe|lines|pipes)$", "", t)
    return _LINE_ALIASES.get(t)


def default_flow(a: Dict[str, Any], b: Dict[str, Any], line: str = "process") -> Dict[str, Any]:
    """Item 9 / P7: `network` when either end is a source-family glyph, else `never`. A signal
    line never flows (§5.7.10 item 6: flow is ignored there), so it is written `never`."""
    if line != "signal" and any(s.get("type") == "equipment" and s.get("glyph") in SOURCE_GLYPHS for s in (a, b)):
        return {"source": "network"}
    return {"source": "never"}


# ── Schema fragments (merged into display_authoring's schemas) ──────────

# The op's own fields (`from` / `to` are already `_REF` on the op schema; a connect takes one id each).
OP_NAME = "connect"
OP_PROPERTIES: Dict[str, Any] = {
    "line": {"type": "string", "enum": list(LINES)},
    "label": {"type": "string"},
    "flow": {"type": "string", "description": "the flow tag's name, only when the user names one"},
}
# The draft symbol's own fields (kind "pipe" in the AUTHORING schema's symbols).
DRAFT_KIND = "pipe"
DRAFT_PROPERTIES: Dict[str, Any] = {
    "from": {"type": "string"},
    "to": {"type": "string"},
    "line": {"type": "string", "enum": list(LINES)},
    "flow": {"type": "string"},
}

PROMPT_FIRST = (
    "Pipes: a pipe between two equipment symbols of your reply is {\"kind\": \"pipe\", \"from\": <that "
    "symbol's label or its equipment words>, \"to\": <the same>, \"line\"?: process|steam|gas|bus|signal, "
    "\"label\"?} — no bind, no x / y / w / h (the engine routes it).\n"
)
PROMPT_OPS = (
    "{\"op\": \"connect\", \"from\": id, \"to\": id, \"line\"?: process|steam|gas|bus|signal, \"label\"?, "
    "\"flow\"?: <a flow tag the user names>}   (\"connect A to B\", \"run a steam line from A to B\", \"pipe A "
    "to B\": a pipe between two equipment symbols; the engine routes it and keeps it on them)\n"
)


def normalize_connect_ops(ops: Any) -> Any:
    """A model's near-misses folded into `connect` (never a guess): an `add` of a `kind: "pipe"`
    symbol → a connect with its from / to / line / label / flow; `a` / `b` written for from / to;
    a one-element list ref → its id; `line` words folded (line_value). Mutates and returns `ops`."""
    if not isinstance(ops, list):
        return ops
    for i, o in enumerate(ops):
        if not isinstance(o, dict):
            continue
        if o.get("op") == "add" and isinstance(o.get("symbol"), dict) and o["symbol"].get("kind") == DRAFT_KIND:
            sy = o["symbol"]
            new = {"op": OP_NAME}
            for k in ("from", "to", "line", "label", "flow"):
                if sy.get(k) is not None:
                    new[k] = sy[k]
            ops[i] = o = new
        if o.get("op") != OP_NAME:
            continue
        for k, alt in (("from", "a"), ("to", "b")):
            if o.get(k) in (None, "", []) and isinstance(o.get(alt), str):
                o[k] = o[alt]
            if isinstance(o.get(k), list) and len(o[k]) == 1 and isinstance(o[k][0], str):
                o[k] = o[k][0]
        if "line" in o:
            lv = line_value(o.get("line"))
            if lv is None:
                o.pop("line", None)                    # tolerated: the request's words decide
            else:
                o["line"] = lv
        if "flow" in o and not isinstance(o.get("flow"), str):
            fl = o.get("flow")
            name = fl.get("tag") or fl.get("name") if isinstance(fl, dict) else None
            if isinstance(name, str) and name.strip():
                o["flow"] = name.strip()
            else:
                o.pop("flow", None)
    return ops


def check_connect_op(op: Dict[str, Any], at: str) -> List[str]:
    """Every way a model's connect op misses the schema (empty = valid; run normalize first)."""
    errs: List[str] = []
    for k in ("from", "to"):
        v = op.get(k)
        if not isinstance(v, str) or not v.strip():
            errs.append(f"{at}.{k}: the id of one equipment symbol (e.g. \"s3\")")
    if op.get("line") is not None and op["line"] not in LINES:
        errs.append(f"{at}.line: one of {', '.join(LINES)}")
    if op.get("label") is not None and not isinstance(op["label"], str):
        op.pop("label", None)                          # tolerated: no label
    if op.get("flow") is not None and not isinstance(op["flow"], str):
        op.pop("flow", None)                           # tolerated: the default flow
    return errs


def check_draft_pipe(s: Any, at: str, errs: List[str]) -> None:
    """A first draft's `kind: "pipe"` symbol (in place of display_authoring's _check_symbol, which
    requires a box a pipe does not have)."""
    if not isinstance(s, dict):
        errs.append(f"{at}: must be an object")
        return
    for k in ("from", "to"):
        v = s.get(k)
        if isinstance(v, list) and len(v) == 1 and isinstance(v[0], str):
            s[k] = v = v[0]
        if not isinstance(v, str) or not v.strip():
            errs.append(f"{at}.{k}: the label (or equipment words) of an equipment symbol in your reply")
    if "line" in s:
        lv = line_value(s.get("line"))
        if lv is None:
            s.pop("line", None)
        else:
            s["line"] = lv
    for k in ("bind", "x", "y", "w", "h", "rules", "glyph", "rotate", "flip"):
        s.pop(k, None)                                 # a pipe has none of these (the engine routes it)


# ── Recognizing a request's connect phrases ────────────────────────────

_LINE_NOUNS = r"(?:line|lines|pipe|pipes|piping|pipeline|duct|ducting|flue|bus|busbar|cable|wire|connection|link)"
# An end stops at a clause end or a qualifier.
_END = (r"(?=\s*(?:[,;:!?()\n]|\.(?:\s|$)|$)|\s+(?:with|using|via|labell?ed|called|named|and|then|please|"
        r"so|carrying|for)\b|\s+as\s+(?:an?|the)\b)")
# 1. "connect / link / join / tie A to|with|and|into B"
_CONNECT_RE = re.compile(
    r"\b(?P<verb>connect|link|join|tie)\s+(?:up\s+)?(?:from\s+)?(?P<a>.+?)\s+(?:to|with|and|into|onto)\s+"
    r"(?P<b>.+?)" + _END, re.I)
# 2. "run / draw / add … a (steam) line from A to B", "a duct between A and B" (not "the line from …":
#    that is an existing thing being moved)
_LINE_FROM_RE = re.compile(
    r"(?P<pre>\b(?:(?:run|draw|add|route|lay|create|make|insert|put)\s+(?:(?:a|an|another|one|new|some)\s+)?|"
    r"(?:a|an|another|one|new|some)\s+)(?:new\s+)?(?:[\w\-]+\s+){0,2}?)(?P<noun>" + _LINE_NOUNS + r")\s+"
    r"(?:from|between)\s+(?P<a>.+?)\s+(?:to|into|and|onto)\s+(?P<b>.+?)" + _END, re.I)
# 3. "pipe / duct A to B" (the verb, at a clause start)
_PIPE_VERB_RE = re.compile(
    r"(?:^|[,;:!?\n]\s*|\.\s+|\b(?:and|then|please|also|now)\s+)(?P<verb>pipe|duct|wire|cable)\s+(?:from\s+)?"
    r"(?P<a>.+?)\s+(?:to|into|onto)\s+(?P<b>.+?)" + _END, re.I)
_NEG_BEFORE_RE = re.compile(r"\b(?:don['’]?t|do\s+not|never|not|no|without|stop)\s+(?:\w+\s+){0,2}$", re.I)
_CLAUSE_TAIL_RE = re.compile(r"[^.;!?\n]*")
# A phrase's qualifiers stop at the next clause ("…, then run a steam line …" is not this pipe's line).
_TAIL_STOP_RE = re.compile(r"\b(?:then|also|but|and)\b", re.I)
_FLOW_TAG_RE = re.compile(
    r"\bflow\s+(?:from|by|of|on|via|per|at|tag|measured\s+(?:by|at|on)|read\s+(?:by|from)|shown\s+by)\s+"
    r"(?:the\s+)?(?:tag\s+|meter\s+|transmitter\s+|point\s+)?(?P<tag>[\"“'‘][^\"”'’]+[\"”'’]|[\w][\w\-.:/|]*)", re.I)
_LABEL_RE = re.compile(
    r"\b(?:labell?ed|called|named|label(?:\s+it)?)\s+(?P<q>[\"“'‘])(?P<lab>[^\"”'’]{1,200})[\"”'’]|"
    r"\b(?:labell?ed|called|named)\s+(?P<bare>[\w][\w\-.]*(?:\s+[\w][\w\-.]*){0,3}?)(?=\s*(?:[,;!?()\n]|\.(?:\s|$)|$|"
    r"\s+(?:with|and|then|using|as)\b))", re.I)
# Verbs that also mean navigation / association ("link the button to …"): both ends must name equipment.
_WEAK_VERBS = frozenset(("link", "join", "tie"))
_PRONOUN_ENDS = frozenset("it them this that these those one ones itself each other".split())
_ARTICLES_RE = re.compile(r"^(?:(?:the|a|an|our|my|this|that|its|their|existing|new)\s+)+", re.I)


class ConnectAsk(dict):
    """One connect phrase of a request: {"from", "to" (the user's words), "line", "line_said"
    (whether the words named a line type), "flow_tag" (a name or None), "label" (or None), "verb"
    (the verb — connect / link / join / tie / pipe / duct / wire / cable — or "line" for "a … line
    from A to B"), "start", "end" (the phrase's span, qualifiers included)}."""


def _clean_end(t: str) -> str:
    t = (t or "").strip().strip("\"“”'‘’").strip()
    t = re.sub(r"[\s,.:;]+$", "", t)
    return t


def _bare(t: str) -> str:
    """An end's words without its article ("the condensate pump" → "condensate pump")."""
    return _ARTICLES_RE.sub("", _clean_end(t)).strip()


def parse_connects(request: str) -> List[ConnectAsk]:
    """The connect phrases of a request, in order, deterministically (item 9). A phrase whose
    sentence negates it right before its verb ("don't connect …"), or whose end is a pronoun ("it")
    is not an ask (the model's op handles a pronoun). Overlapping matches keep the first."""
    text = request or ""
    found: List[Tuple[int, int, re.Match, str]] = []
    for rx, how in ((_CONNECT_RE, "connect"), (_LINE_FROM_RE, "line"), (_PIPE_VERB_RE, "pipe")):
        for m in rx.finditer(text):
            found.append((m.start("a"), m.end("b"), m, how))
    found.sort(key=lambda x: (x[0], x[1]))
    out: List[ConnectAsk] = []
    last_end = -1
    for a0, b1, m, how in found:
        if a0 < last_end:
            continue
        if _NEG_BEFORE_RE.search(text[max(0, m.start() - 40):m.start()]):
            continue
        a, b = _clean_end(m.group("a")), _clean_end(m.group("b"))
        if not _bare(a) or not _bare(b):
            continue
        if _bare(a).lower() in _PRONOUN_ENDS or _bare(b).lower() in _PRONOUN_ENDS:
            continue
        tail = _CLAUSE_TAIL_RE.match(text, m.end("b")).group(0)
        stop = _TAIL_STOP_RE.search(tail)
        if stop:
            tail = tail[:stop.start()]
        ft = _FLOW_TAG_RE.search(tail)
        tag = None
        if ft:
            tv = ft.group("tag").strip("\"“”'‘’").rstrip(".,")
            # a tag name has a digit or was quoted ("with flow from the pump" names no tag)
            if re.search(r"\d", tv) or ft.group("tag")[:1] in "\"“'‘":
                tag = tv
        lb = _LABEL_RE.search(tail)
        label = None
        if lb:
            label = (lb.group("lab") or lb.group("bare") or "").strip() or None
        # the line words: what the phrase says outside its two ends (an end's own words — "air
        # preheater", "steam drum" — never decide the line). The user's OWN text does not decide
        # it either: a quoted label and a flow tag's name are blanked out first, so
        # "connect A to B labeled 'Main steam'" is a process line, not a steam line (and
        # "labeled 'signal path'" no longer forces flow never and drops the flow binding).
        line_text = tail
        for _m in (lb, ft):
            if _m is not None:
                line_text = (line_text[:_m.start()] + " " * (_m.end() - _m.start())
                             + line_text[_m.end():])
        said = (m.group("pre") + " " + m.group("noun")) if how == "line" else ""
        said += " " + line_text
        lm = _LINE_WORD_RE.search(said)
        line = line_of_words(said) if lm else "process"
        out.append(ConnectAsk({"from": a, "to": b, "line": line, "line_said": bool(lm), "flow_tag": tag,
                               "label": label, "verb": m.group("verb").lower() if how != "line" else "line", "start": m.start(), "end": m.end("b") + len(tail)}))
        last_end = b1
    return out


def connect_spans(request: str) -> List[Tuple[int, int]]:
    """The spans of the request's connect phrases (with their qualifiers): text other passes should
    not read as a symbol's name ("labeled …") or a glyph to draw ("connect the pump to the tank"
    adds no pump)."""
    return [(a["start"], a["end"]) for a in parse_connects(request)]


# ── Resolving an end ───────────────────────────────────────────────────

def _tok(text: str) -> List[str]:
    t = re.sub(r"['’]s\b", "", text or "")
    return [w.lower() for w in re.findall(r"[\w\-]+", t)]


def _box(sym: Dict[str, Any]) -> Tuple[int, int, int, int]:
    def i(v: Any) -> int:
        return v if isinstance(v, int) and not isinstance(v, bool) else 0
    return i(sym.get("x")), i(sym.get("y")), i(sym.get("w")), i(sym.get("h"))


def end_name(sym: Dict[str, Any], fallback: Optional[str] = None) -> str:
    """How a pipe note names an end: "Pump P-501" (the glyph's word + the label, unless the label
    already says it), the label alone for a non-equipment symbol, else `fallback` (the caller's
    distinct name, e.g. "the left pump"), else "the <glyph word>"."""
    lbl = str(sym.get("label") or "").strip()
    word = GLYPH_WORDS.get(str(sym.get("glyph"))) if sym.get("type") == "equipment" else None
    if lbl and not re.match(r"^s\d+$", lbl, re.I):
        if word and word.lower() not in lbl.lower():
            return f"{word[0].upper()}{word[1:]} {lbl}"
        return lbl
    if fallback:
        return fallback
    if word:
        return f"the {word}"
    return f"the {sym.get('type') or 'symbol'}"


def phrase_matches(phrase: str, pool: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The symbols of `pool` a phrase names: an id ("s3"); else the symbols whose whole label occurs
    in it (the longest such labels win); else, and to narrow labels, the equipment its glyph words
    name — a multi-word glyph phrase ("condensate pump", "FD fan") names that glyph, a single
    reference word ("pump", "valve") the whole family (display_glyphs.GLYPH_REF_WORDS)."""
    p = _bare(phrase)
    if not p:
        return []
    if re.fullmatch(r"s[1-9]\d*", p.strip(), re.I):
        return [s for s in pool if str(s.get("id")) == p.strip().lower()]
    toks = _tok(p)
    tset = set(toks)
    by_label: List[Tuple[int, Dict[str, Any]]] = []
    for s in pool:
        lw = _tok(str(s.get("label") or ""))
        if lw and all(w in tset for w in lw):
            by_label.append((len(lw), s))
    if by_label:
        best = max(n for n, _s in by_label)
        by_label_syms = [s for n, s in by_label if n == best]
    else:
        by_label_syms = []
    glyphs: Optional[frozenset] = None
    m = GLYPH_PHRASE_RE.search(re.sub(r"[\"“][^\"”]*[\"”]", " ", p))
    if m:
        words = m.group(0).strip().lower().split()
        hit = request_glyphs(m.group(0))
        if len(words) == 1 and (words[0] in GLYPH_REF_WORDS):
            glyphs = GLYPH_REF_WORDS[words[0]]
        elif hit:
            glyphs = frozenset(hit[0][0])
    else:
        ref = next((t for t in toks if t in GLYPH_REF_WORDS), None)
        if ref is not None:
            glyphs = GLYPH_REF_WORDS[ref]
    by_glyph = [s for s in pool if s.get("type") == "equipment" and glyphs is not None and s.get("glyph") in glyphs]
    if by_label_syms:
        both = [s for s in by_label_syms if s in by_glyph] if glyphs is not None else []
        return both or by_label_syms
    return by_glyph


# ── Building a pipe ────────────────────────────────────────────────────

def hull(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
    """The smallest box holding both boxes (x, y, w, h)."""
    x0, y0 = min(a[0], b[0]), min(a[1], b[1])
    x1, y1 = max(a[0] + a[2], b[0] + b[2]), max(a[1] + a[3], b[1] + b[3])
    return x0, y0, max(1, x1 - x0), max(1, y1 - y0)


def build_pipe(sid: str, a: Dict[str, Any], b: Dict[str, Any], *, line: str = "process",
               label: Optional[str] = None, flow_binding: Optional[Dict[str, Any]] = None,
               box_of: Optional[Callable[[Dict[str, Any]], Tuple[int, int, int, int]]] = None,
               origin: Tuple[int, int] = (0, 0)) -> Dict[str, Any]:
    """The auto-routed pipe Describe writes between equipment symbols `a` → `b` (item 4: no
    `bends`, no ports — the viewer owns the geometry). Its box = the hull of both ends' boxes
    (`box_of` gives absolute boxes; `origin` = the parent's absolute top-left, (0, 0) at the top
    level). `flow_binding` = the named flow tag's binding → flow `tag` with it as binding 1."""
    bx = box_of or _box
    x, y, w, h = hull(bx(a), bx(b))
    if line not in LINES:
        line = "process"
    pipe: Dict[str, Any] = {
        "id": sid, "type": "pipe", "x": x - origin[0], "y": y - origin[1], "w": w, "h": h,
        "from": {"symbol": str(a.get("id"))}, "to": {"symbol": str(b.get("id"))},
        "line": line,
        "flow": {"source": "tag"} if flow_binding is not None and line != "signal" else default_flow(a, b, line),
        "bind": [flow_binding] if flow_binding is not None and line != "signal" else [],
        "style": {}, "rules": [],
    }
    lbl = (label or "").strip()[:MAX_LABEL]
    if lbl:
        pipe["label"] = lbl
    return pipe


def ends_of(p: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """A pipe's attached symbol ids (from, to); None for a tee, an open or a missing end."""
    def one(e: Any) -> Optional[str]:
        return str(e["symbol"]) if isinstance(e, dict) and isinstance(e.get("symbol"), str) else None
    return one(p.get("from")), one(p.get("to"))


def _flow_words(pipe: Dict[str, Any], tag: Optional[str]) -> str:
    return f", flow by {tag}" if (pipe.get("flow") or {}).get("source") == "tag" and tag else ""


class Connector:
    """One Describe batch's pipes. The caller (display_authoring) hands in what it owns:

    - `symbols()` → the display's symbols now (leaves, nested included; the pool ends resolve in);
    - `new_id()` → the next free symbol id ("s12"), consumed only when a pipe is made;
    - `resolve(ref)` → its own answer for a reference (a list of symbols; [] = names nothing), or
      None to let the phrase resolver decide (display_authoring maps ids it knows: `real()` /
      `find()`, the ids it guessed for this batch's adds);
    - `name_of(sym)` → a distinct name for an unlabeled symbol ("the left pump"), or None;
    - `box_of(sym)` → its absolute box (default: x, y, w, h as written — top-level symbols);
    - `bind_name(name)` → a binding for a flow tag's name, or None (display_authoring's
      `_Resolver.resolve`).

    `from_op` / `from_draft` handle the model's connect op / draft pipe; a request's own connect
    phrase that names the same two symbols wins over the model's words (line, flow, label: item 9
    takes them from the request); `rest()` makes the request's phrases the model gave no op for.
    Every call returns (pipe or None, note): the caller inserts the pipe at the bottom of the
    stacking order (index 0 of the top-level symbols) and appends the note (an empty note = say
    nothing: a model op whose ids fail while the request's own phrase is still to be made)."""

    def __init__(self, request: str, *, symbols: Callable[[], Iterable[Dict[str, Any]]],
                 new_id: Callable[[], str],
                 resolve: Optional[Callable[[str], Optional[Sequence[Dict[str, Any]]]]] = None,
                 name_of: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
                 box_of: Optional[Callable[[Dict[str, Any]], Tuple[int, int, int, int]]] = None,
                 bind_name: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None) -> None:
        self.request = request or ""
        self.symbols = symbols
        self.new_id = new_id
        self.resolve = resolve
        self.name_of = name_of
        self.box_of = box_of or _box
        self.bind_name = bind_name
        self.asks: List[ConnectAsk] = parse_connects(self.request)
        self.done: set = set()                         # indexes of asks already made (or refused)
        self.made: List[Dict[str, Any]] = []           # the pipes this batch made

    # names
    def nm(self, s: Dict[str, Any]) -> str:
        return end_name(s, self.name_of(s) if self.name_of else None)

    def said(self, ref: str) -> str:
        """An unresolved end as the user knows it: their words, never a bare id."""
        r = _clean_end(ref)
        if re.fullmatch(r"s\d+", r, re.I) or not r:
            return "that symbol"
        return r

    def resolve_end(self, ref: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """One end → (the symbol, None) or (None, why it cannot be an end)."""
        s, why = self._lookup(ref)
        return (None, why) if why else (s, None)

    def _lookup(self, ref: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """One end → (the symbol, None); (the ONE symbol it names, why that cannot be an end); or
        (None, why nothing / several are named)."""
        if not isinstance(ref, str) or not ref.strip():
            return None, "an end is missing"
        pool = [s for s in self.symbols() if isinstance(s, dict) and s.get("type") not in ("group", "repeat")]
        got = self.resolve(ref) if self.resolve else None
        cands = list(got) if got is not None else phrase_matches(ref, pool)
        if not cands and got is not None and not re.fullmatch(r"\s*s\d+\s*", ref, re.I):
            cands = phrase_matches(ref, pool)          # the caller knew no id: the words may name one
        # one symbol named twice (a label and a glyph) is one candidate
        uniq: List[Dict[str, Any]] = []
        for c in cands:
            if all(c is not u for u in uniq):
                uniq.append(c)
        if not uniq:
            b = _bare(ref)
            return None, (f"there is no {b} on the display" if b and not re.fullmatch(r"s\d+", b, re.I)
                          else "that symbol is not on the display")
        eq = [c for c in uniq if c.get("type") == "equipment"] if len(uniq) > 1 else uniq
        if len(eq) > 1:
            names = [self.nm(c) for c in eq[:4]]
            more = " or another" if len(eq) > 4 else ""
            return None, (f"'{_clean_end(ref)}' could be {', '.join(names[:-1])} or {names[-1]}{more} — "
                          "name the one you mean")
        s = (eq or uniq)[0]
        if is_pipe(s):
            return s, f"{self.nm(s)} is a pipe — Describe connects equipment; draw a tee in Edit"
        if s.get("type") != "equipment":
            return s, f"{self.nm(s)} is not equipment — a pipe connects equipment symbols"
        if s.get("glyph") in PORTLESS_GLYPHS:
            return s, f"{self.nm(s)} has no connection points"
        return s, None

    def _ask_for(self, a: Dict[str, Any], b: Dict[str, Any]) -> Optional[int]:
        """The request's connect phrase naming exactly these two symbols (either order), if any."""
        for i, ask in enumerate(self.asks):
            if i in self.done:
                continue
            ra, _ = self.resolve_end(ask["from"])
            rb, _ = self.resolve_end(ask["to"])
            if ra is not None and rb is not None and {id(ra), id(rb)} == {id(a), id(b)}:
                return i
        return None

    def _names_equipment(self, ref: str) -> bool:
        """Whether an end's words name equipment: a glyph phrase or reference word ("the deaerator",
        "the pump" — present or not), or a label of an equipment symbol on the display."""
        b = _bare(ref)
        if GLYPH_PHRASE_RE.search(b) or any(t in GLYPH_REF_WORDS for t in _tok(b)):
            return True
        pool = [s for s in self.symbols() if isinstance(s, dict)]
        return any(s.get("type") == "equipment" for s in phrase_matches(ref, pool))

    def _connected(self, a: Dict[str, Any], b: Dict[str, Any]) -> bool:
        pair = {str(a.get("id")), str(b.get("id"))}
        for p in list(self.symbols()) + self.made:
            if is_pipe(p) and set(x for x in ends_of(p) if x) == pair and None not in ends_of(p):
                return True
        return False

    def _said_in_request(self, text: Optional[str]) -> bool:
        return bool(text) and str(text).strip().lower() in self.request.lower()

    def _make(self, a_ref: Any, b_ref: Any, *, line: str, tag: Optional[str],
              label: Optional[str], words: Tuple[str, str]) -> Tuple[Optional[Dict[str, Any]], str]:
        a, why_a = self._lookup(a_ref)
        b, why_b = self._lookup(b_ref)
        what = f"{self.nm(a) if a is not None else words[0]} to {self.nm(b) if b is not None else words[1]}"
        if why_a or why_b:
            return None, f"did not connect {what}: {why_a or why_b}"
        assert a is not None and b is not None
        if a is b:
            return None, f"did not connect {what}: both ends are {self.nm(a)}"
        if self._connected(a, b):
            return None, f"did not connect {what}: they are already connected"
        binding = None
        tag_note = ""
        if tag and line != "signal":
            binding = self.bind_name(tag) if self.bind_name else None
            if binding is None:
                tag_note = f"; did not find the flow tag {tag}, so its flow follows the default"
        pipe = build_pipe(self.new_id(), a, b, line=line, label=label, flow_binding=binding, box_of=self.box_of)
        self.made.append(pipe)
        note = f"connected {what} ({LINE_WORDS[pipe['line']]}{_flow_words(pipe, tag)}){tag_note}"
        return pipe, note

    def _from_model(self, src: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
        a, _ = self.resolve_end(src.get("from"))
        b, _ = self.resolve_end(src.get("to"))
        words = (self.said(str(src.get("from") or "")), self.said(str(src.get("to") or "")))
        i = self._ask_for(a, b) if a is not None and b is not None else None
        if (a is None or b is None) and any(j not in self.done for j in range(len(self.asks))):
            # the model's ids name no usable pair, but the request's own phrase is still to come
            # (`rest()` makes it from the user's words): say nothing here, not a second refusal
            return None, ""
        if i is not None:
            # the request's own phrase for these two: its words decide (item 9)
            self.done.add(i)
            ask = self.asks[i]
            frm, to = (src.get("from"), src.get("to"))
            ra, _ = self.resolve_end(ask["from"])
            if ra is not None and ra is b:             # the user said B → A: their direction
                frm, to = to, frm
            mlabel = src.get("label") if isinstance(src.get("label"), str) \
                and self._said_in_request(src.get("label")) else None
            return self._make(frm, to, line=ask["line"], tag=ask["flow_tag"],
                              label=ask["label"] or mlabel, words=words)
        # no phrase of the request says this pair: the model's line only when the request says that
        # line's word, its flow tag / label only when the request says them (Describe does no harm)
        line = src.get("line") if src.get("line") in LINES else "process"
        if line != "process" and line_of_words(self.request) != line \
                and not re.search(r"\b" + re.escape(line) + r"\b", self.request, re.I):
            line = "process"
        tag = src.get("flow") if isinstance(src.get("flow"), str) and self._said_in_request(src.get("flow")) else None
        label = src.get("label") if isinstance(src.get("label"), str) and self._said_in_request(src.get("label")) else None
        return self._make(src.get("from"), src.get("to"), line=line, tag=tag, label=label,
                          words=words)

    def from_op(self, op: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
        """A model's (normalized, checked) `connect` op."""
        return self._from_model(op)

    def from_draft(self, s: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
        """A first draft's `kind: "pipe"` symbol (its ends name symbols of the same reply by label or
        equipment words; resolved after the engine gave them ids)."""
        return self._from_model(s)

    def rest(self) -> List[Tuple[Optional[Dict[str, Any]], str]]:
        """The request's connect phrases no model op covered, in order (made deterministically)."""
        out: List[Tuple[Optional[Dict[str, Any]], str]] = []
        for i, ask in enumerate(self.asks):
            if i in self.done:
                continue
            self.done.add(i)
            named = [self._names_equipment(ask["from"]), self._names_equipment(ask["to"])]
            if not (all(named) if ask["verb"] in _WEAK_VERBS else any(named)):
                # "connect the trend to FT-301", "link the button to Pump Detail": not piping — an
                # ask needs an end that names equipment (both, for link / join / tie); said nothing
                continue
            out.append(self._make(ask["from"], ask["to"], line=ask["line"],
                                  tag=ask["flow_tag"], label=ask["label"],
                                  words=(_clean_end(ask["from"]), _clean_end(ask["to"]))))
        return out


# ── After an edit: removed ends, moved equipment ───────────────────────

def _walk(symbols: Iterable[Any]) -> Iterable[Dict[str, Any]]:
    for s in symbols:
        if isinstance(s, dict):
            yield s
            if isinstance(s.get("children"), list):
                yield from _walk(s["children"])


def _prune(lst: List[Any], gone: set) -> None:
    lst[:] = [s for s in lst if not (isinstance(s, dict) and str(s.get("id")) in gone)]
    for s in lst:
        if isinstance(s, dict) and isinstance(s.get("children"), list):
            _prune(s["children"], gone)


def detach_removed(top: List[Any], removed: Dict[str, Tuple[int, int, int, int]],
                   name_of: Optional[Callable[[str], str]] = None,
                   box_of: Optional[Callable[[Dict[str, Any]], Tuple[int, int, int, int]]] = None) -> List[str]:
    """Item 3 for Describe's removals (`removed`: id → its absolute box before it went). A pipe
    keeps going: an end on a removed symbol becomes an open end — `{"at": [x, y]}` at the removed
    symbol's center (Describe has no port geometry; the viewer's end then sits where the symbol
    was); a tee on a removed pipe drops its end (an open end at the route's first / last bend).
    An AUTO-ROUTED pipe (no `bends`) cannot have an open end ("an auto-routed pipe needs both
    ends attached"), so it is removed with its equipment, and the note says so. Mutates `top`;
    returns the notes."""
    bx = box_of or _box
    notes: List[str] = []
    gone: set = set()
    changed = True
    while changed:                                     # a removed pipe can be a tee's host
        changed = False
        for p in _walk(top):
            if not is_pipe(p) or str(p.get("id")) in gone or str(p.get("id")) in removed:
                continue
            auto = not (isinstance(p.get("bends"), list) and p["bends"])
            for key in ("from", "to"):
                e = p.get(key)
                if not isinstance(e, dict):
                    continue
                sid = e.get("symbol") if isinstance(e.get("symbol"), str) else None
                host = e.get("pipe") if isinstance(e.get("pipe"), str) else None
                if sid is not None and sid in removed:
                    if auto:
                        gone.add(str(p.get("id")))
                        who = name_of(sid) if name_of else "its equipment"
                        notes.append(f"removed the pipe on {who} with it (a pipe Describe drew follows "
                                     "its equipment)")
                        changed = True
                        break
                    rx, ry, rw, rh = removed[sid]
                    px, py, _w, _h = bx(p)
                    p[key] = {"at": [rx + rw // 2 - px, ry + rh // 2 - py]}
                elif host is not None and (host in removed or host in gone):
                    if auto:
                        gone.add(str(p.get("id")))
                        notes.append("removed a pipe teed onto a removed pipe")
                        changed = True
                        break
                    p.pop(key, None)
    if gone:
        _prune(top, gone)
    return notes


def refresh_boxes(top: List[Any], box_of_id: Callable[[str], Optional[Tuple[int, int, int, int]]],
                  origin_of: Optional[Callable[[Dict[str, Any]], Tuple[int, int]]] = None,
                  moved: Optional[Callable[[str], bool]] = None) -> None:
    """An auto-routed pipe's box (ignored by the renderer, but read by the summary / footprints)
    follows its ends: the hull of both attached symbols' absolute boxes. A pipe with `bends` is the
    editor's (its box = route bounds + 8 px) and is left alone; with `moved` (id → whether that
    symbol moved or was resized in this edit), a pipe whose ends both stayed is left alone too."""
    for p in _walk(top):
        if not is_pipe(p) or (isinstance(p.get("bends"), list) and p["bends"]):
            continue
        a, b = ends_of(p)
        if moved is not None and not ((a and moved(a)) or (b and moved(b))):
            continue
        ba = box_of_id(a) if a else None
        bb = box_of_id(b) if b else None
        if ba is None or bb is None:
            continue
        x, y, w, h = hull(ba, bb)
        ox, oy = origin_of(p) if origin_of else (0, 0)
        p["x"], p["y"], p["w"], p["h"] = x - ox, y - oy, w, h


# ── Ops on a pipe (pipes follow their equipment) ───────────────────────

# The ops that move or size a symbol: on a pipe they are refused (its geometry is its ends').
GEOMETRY_OPS: frozenset = frozenset((
    "move", "resize", "place", "align", "match", "distribute", "center", "swap", "scale", "nudge",
    "make_room", "readable"))


def geometry_refusal(name: str) -> str:
    """The note for a geometry op on a pipe."""
    return f"did not move {name}: a pipe follows its equipment — move the equipment instead"


def set_pipe_props(pipe: Dict[str, Any], props: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """A `set` on a pipe: `label` and `line` (its words folded); x / y / w / h are refused (the
    pipe follows its equipment), other keys are not a pipe's. Returns (applied keys, bad notes)."""
    applied: List[str] = []
    bad: List[str] = []
    for k, v in props.items():
        if k == "label":
            t = str(v or "").strip()[:MAX_LABEL]
            if t:
                pipe["label"] = t
            else:
                pipe.pop("label", None)
            applied.append(k)
        elif k == "line":
            lv = line_value(v)
            if lv is None:
                bad.append(f"line: one of {', '.join(LINES)}")
            else:
                pipe["line"] = lv
                if lv == "signal":
                    pipe["flow"] = {"source": "never"}
                    pipe["bind"] = []
                applied.append(k)
        elif k in ("x", "y", "w", "h"):
            bad.append(f"{k}: a pipe follows its equipment")
        else:
            bad.append(f"{k}: not a property of a pipe")
    return applied, bad


__all__ = [
    "LINES", "FLOW_SOURCES", "SOURCE_GLYPHS", "PORTLESS_GLYPHS", "LINE_WORDS", "is_pipe", "line_of_words",
    "line_value", "default_flow", "OP_NAME", "OP_PROPERTIES", "DRAFT_KIND", "DRAFT_PROPERTIES",
    "PROMPT_FIRST", "PROMPT_OPS", "normalize_connect_ops", "check_connect_op", "check_draft_pipe",
    "ConnectAsk", "parse_connects", "connect_spans", "end_name", "phrase_matches", "hull", "build_pipe",
    "ends_of", "Connector", "detach_removed", "refresh_boxes", "GEOMETRY_OPS", "geometry_refusal",
    "set_pipe_props",
]
