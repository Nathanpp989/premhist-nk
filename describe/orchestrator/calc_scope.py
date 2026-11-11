"""
calc_scope.py — what a Describe request is ABOUT in the whole plant, and the scope that fits it.

design_calc.md R239–R244 (Jim, 2026-10-23: "PremCalc seems to be unusable … it can't seem to create
a calc for anything" — a calc on specific tags asked for "the average temperature for all pump
bearings" was refused because tag-only grounding could not know that SIM-TI-401.PV is Pump P-501's
Bearing Temp). The drafting engine (`calc_authoring.draft_calc`) uses this module for three things:

- **The plant index** (`build_index`): every template with its attributes, its elements (path,
  parent) and each element's bound tags, read once per request through the same read-only
  `ground` (GETs only; capped at `MAX_INDEX_ELEMENTS` element reads). Best effort: a `ground`
  without `templates()` (or any read failure) gives `None` and every feature below is off.
- **Asset context for specific tags** (`asset_map_lines`, `tag_asset`): the compact map
  "tag = element | attribute" (`SIM-TI-401.PV = Pump P-501 | Bearing Temp`) grouped by template
  attribute, capped at `MAX_ASSET_LINES` lines of at most `MAX_ASSET_ENTRIES` entries each (the
  entries the request's words name are always shown; the rest is "… N more" — the cap is said in
  the text, never silent), so a points-scope draft can find "pump bearing temperature".
- **Scope planning** (`plan_scope`): the scope that fits the request — each element of a
  template, one value across them (a rollup on their common parent), one named element, or
  specific tags — decided from the request's words and the plant, never by the model. The scope
  is still never changed silently: a different scope is a PROPOSAL the person confirms with one
  click (`propose`), and a request that reads both ways ("for all pump bearings": per pump, or one
  value across the pumps) is a QUESTION with two clickable choices (`choose`). Requests the whole
  plant cannot serve (a subject or medium that appears nowhere, a message instead of a calc, a
  shift total on a site without shifts) are refused here, before any model call (`refuse`).
- **Plant-wide sources** (`source_scope`): when a draft in the current scope is refused because a
  need has no listed source, the need is looked up in the WHOLE plant; a source elsewhere becomes
  a scope proposal, never a refusal (R242).

Pure functions over the index except `build_index` (reads). Nothing here saves anything.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import os
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

MAX_INDEX_ELEMENTS = 300           # element reads for the plant index (each one GET)
MAX_ASSET_LINES = 40               # points scope: "template | attribute" lines in the asset map
MAX_ASSET_ENTRIES = 6              # tag = element entries shown per line (+ every one the request names)
MAX_EXTRA_POINTS = 24              # points scope: tags added to TAGS found because their asset matches


# ── words ─────────────────────────────────────────────────────────────

_SYN = {
    "temperature": "temp", "temperatures": "temp", "temps": "temp", "temp": "temp",
    "ran": "run", "running": "run", "runs": "run", "runtime": "run", "run": "run",
    "trips": "trip", "tripped": "trip", "tripping": "trip", "trip": "trip",
    "starts": "start", "started": "start", "starting": "start", "startup": "start", "startups": "start",
    "stops": "stop", "stopped": "stop", "stopping": "stop",
    "setpoints": "setpoint", "sp": "setpoint",
    # "status" must be mapped explicitly: the plain -s rule below would stem it to "statu", and
    # then neither _ACTIVITY nor the ("state", "status") test in _attr_strength could ever match
    # it — a request saying "status" missed an attribute named "Status" or "Pump Run State".
    "status": "status", "statuses": "status",
    "vibrations": "vibration", "vib": "vibration", "vibs": "vibration",
    "levels": "level", "currents": "current", "amps": "current", "amp": "current", "amperage": "current",
    "flows": "flow", "flowrate": "flow", "bearings": "bearing",
}
# Words that mean "the run state" of something with a digital state attribute.
_ACTIVITY = frozenset({"run", "trip", "start", "stop", "state", "status", "online", "offline", "uptime",
                       "downtime"})
_GENERIC = frozenset("""
a an the of for to and or in on at by per each every all any from with into over under than then is are
be was were it its this that these those as if when while which what how much many calc calculate
calculation compute total totals count number value values give me i want need please make create write
show average avg mean sum min max minimum maximum highest lowest high low hour hours hourly day days daily
shift shifts week weeks weekly month months monthly year minute minutes second seconds above below more
less over under today yesterday tonight now last past previous current-day full empty alert alarm flag
warn warning when whenever time long spent changed change changes deviation difference between across
combined together altogether overall whole entire plant site area unit units deg degc degf
output outputs result results data track measure report monitor plot trend find get much well also just
only into out up down rate ratio percent percentage there their they them his her our my your liquid
""".split())
# Media a request may name as the THING measured ("steam flow"): when the plant mentions it nowhere,
# the request is about data this plant does not have (R243).
_MEDIA = frozenset("""steam gas fuel oil water air nitrogen oxygen hydrogen coolant glycol ammonia chlorine
diesel condensate slurry brine acid caustic propane methane""".split())
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_.']*")
_TIME_NOUNS = frozenset({"hour", "hours", "day", "days", "shift", "shifts", "week", "weeks", "month",
                         "months", "minute", "minutes", "second", "seconds", "year", "years", "morning",
                         "night", "evening", "midnight", "time", "run", "batch"})


# Unicode look-alikes a model (or a paste) puts into a name: the dashes and hyphens U+2010–U+2015,
# U+2212 minus, U+2043, the small / full-width hyphen-minus → "-"; no-break, narrow and other
# spaces → " "; soft hyphen and zero-width characters → nothing. "SIM‑TI‑401.PV" (U+2011) is
# SIM-TI-401.PV (the PremHist manual v1.4 capture: refused "… is not in this plant"; R252).
_FOLD_T = str.maketrans({
    **{chr(c): "-" for c in (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212, 0x2043, 0xFE58,
                             0xFE63, 0xFF0D)},
    **{chr(c): " " for c in (0x00A0, 0x1680, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006,
                             0x2007, 0x2008, 0x2009, 0x200A, 0x202F, 0x205F, 0x3000)},
    **{chr(c): "" for c in (0x00AD, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)},
})


def fold_text(s: Any) -> str:
    """The text with Unicode dashes / hyphens folded to "-", odd spaces to " ", and soft hyphens
    and zero-width characters removed (see _FOLD_T)."""
    return str(s if s is not None else "").translate(_FOLD_T)


def name_key(s: Any) -> str:
    return " ".join(fold_text(s).split()).casefold()


def stem(w: str) -> str:
    w = w.casefold().strip("'’.-_")
    w = re.sub(r"'s$|’s$", "", w)
    if w in _SYN:
        return _SYN[w]
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 4 and w.endswith("es") and w[:-2].endswith(("ss", "sh", "ch", "x")):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return _SYN.get(w, w)


def _norm_text(text: str) -> str:
    t = re.sub(r"\bset[\s-]*points?\b", "setpoint", fold_text(text or ""), flags=re.I)
    return t


def tokens(text: str) -> List[str]:
    """The request's words (raw, in order; punctuation trimmed)."""
    return [m.group(0).strip("'’.-_") for m in _WORD.finditer(_norm_text(text)) if m.group(0).strip("'’.-_")]


def stems(text: str) -> List[str]:
    return [stem(w) for w in tokens(text)]


def _name_tokens(name: str) -> List[str]:
    return [stem(w) for w in re.split(r"[^A-Za-z0-9]+", _norm_text(name or "")) if w]


def _distinct_tokens(name: str) -> List[str]:
    """The identifying tokens of an element name: those with a digit AND something that is not a
    digit ("P-503", "T-301", "MCC-201", "2A").

    A BARE number is not identifying. It used to count, so an element named the way real sites
    name things - "Unit 1 Boiler", "Line 2 Pump", "Boiler 3" - was "named by" any request that
    happened to contain that digit: "the average steam flow over the last 1 hour" matched
    Unit 1 Boiler, and step 1 of plan_scope then proposed that one element's scope, with the
    reason "the request names Unit 1 Boiler". Every request mentioning a period, a threshold or a
    count hit this. The cost of the fix is that such an element is no longer found by its number
    alone; it is still found by a tag, an attribute or its template."""
    return [w for w in re.findall(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*", fold_text(name or ""))
            if re.search(r"\d", w) and not w.isdigit()]


def _plural(name: Optional[str]) -> str:
    n = str(name or "elements").strip()
    return n if n.lower().endswith("s") else n + "s"


def _plural_last(name: Optional[str]) -> str:
    w = (name or "").split()
    if not w:
        return "elements"
    last = w[-1].lower()
    return last if last.endswith("s") else last + "s"


async def _g(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    res = await asyncio.to_thread(fn, *args, **kwargs)
    if inspect.isawaitable(res):
        res = await res
    return res


# ── the plant index ───────────────────────────────────────────────────

class Element:
    __slots__ = ("id", "name", "path", "parent", "template", "template_name", "attrs")

    def __init__(self, d: Dict[str, Any]) -> None:
        self.id: int = int(d["id"])
        self.name: str = str(d.get("name") or "")
        self.path: str = str(d.get("path") or "")
        self.parent: Optional[int] = d.get("parent") if isinstance(d.get("parent"), int) else None
        self.template: Optional[int] = d.get("template") if isinstance(d.get("template"), int) else None
        self.template_name: Optional[str] = d.get("template_name") or None
        # attr name -> {kind, units, tag: {dataset, name, eng_units, point_type} | None}
        self.attrs: Dict[str, Dict[str, Any]] = {}
        for a in d.get("attributes") or []:
            if not isinstance(a, dict) or not a.get("name"):
                continue
            v = a.get("value") if isinstance(a.get("value"), dict) else {}
            tag = v.get("tag") if v.get("kind") == "tag" and isinstance(v.get("tag"), dict) else None
            self.attrs[str(a["name"])] = {
                "kind": v.get("kind"), "units": a.get("units"),
                "tag": ({"dataset": tag.get("dataset"), "name": tag.get("name"),
                         "eng_units": tag.get("eng_units"), "point_type": tag.get("point_type")}
                        if tag and not v.get("missing") and tag.get("name") else None)}


class Template:
    __slots__ = ("id", "name", "attrs", "elements")

    def __init__(self, d: Dict[str, Any]) -> None:
        self.id: int = int(d["id"])
        self.name: str = str(d.get("name") or "")
        self.attrs: List[Dict[str, Any]] = [a for a in d.get("attributes") or []
                                            if isinstance(a, dict) and a.get("name")]
        self.elements: List[int] = []


class PlantIndex:
    def __init__(self) -> None:
        self.templates: List[Template] = []
        self.elements: Dict[int, Element] = {}
        self.points: List[Dict[str, Any]] = []
        self.sites: List[Dict[str, Any]] = []
        self.truncated = False
        self.reads = 0
        # Set by build_index: whether this index came from the cache and how old it was when it
        # was handed over. Never silent - a stale index would answer "this plant has no X" about
        # an asset added a minute ago, so the age travels with the index.
        self.from_cache = False
        self.cache_age_s = 0.0

    # what exists, as one lower-case text (absence checks)
    def universe(self) -> str:
        bits: List[str] = []
        for t in self.templates:
            bits.append(t.name)
            bits += [str(a.get("name")) for a in t.attrs]
        for e in self.elements.values():
            bits += [e.name, e.path] + list(e.attrs)
        for p in self.points:
            bits += [str(p.get("name") or ""), str(p.get("description") or "")]
        return " ".join(bits).casefold()

    def universe_stems(self) -> set:
        out: set = set()
        for w in re.findall(r"[A-Za-z][A-Za-z0-9]*", self.universe()):
            out.add(stem(w))
            out.add(w)
        return out

    def template(self, tid: Optional[int]) -> Optional[Template]:
        return next((t for t in self.templates if t.id == tid), None)

    def template_by_name(self, name: str) -> Optional[Template]:
        return next((t for t in self.templates if name_key(t.name) == name_key(name)), None)

    def members(self, t: Template) -> List[Element]:
        return [self.elements[i] for i in t.elements if i in self.elements]

    def common_parent(self, t: Template) -> Optional[Element]:
        """The one element every member of `t` hangs under directly (a rollup's home), or None."""
        ps = {e.parent for e in self.members(t)}
        if len(ps) == 1:
            p = next(iter(ps))
            if p is not None and p in self.elements:
                return self.elements[p]
        return None

    def bound(self, t: Template, attr: str) -> List[Tuple[Element, Dict[str, Any]]]:
        out = []
        for e in self.members(t):
            a = e.attrs.get(attr)
            if a and a.get("tag"):
                out.append((e, a["tag"]))
        return out

    def tag_owner(self, tag_name: str) -> List[Tuple[Element, str]]:
        k = name_key(tag_name)
        out = []
        for e in self.elements.values():
            for an, a in e.attrs.items():
                if a.get("tag") and name_key(a["tag"].get("name")) == k:
                    out.append((e, an))
        return out

    def input_rows(self) -> List[Dict[str, Any]]:
        """What any calc in the plant could read: every template attribute bound somewhere (its tag's
        units and type), every static value, and every tag of the plant (a tag on no asset is still
        a measurement) — {name, units, point_type, static, where, description?}."""
        out: List[Dict[str, Any]] = []
        for t in self.templates:
            for a in t.attrs:
                an = str(a["name"])
                if a.get("kind") == "static":
                    out.append({"name": an, "units": a.get("units"), "point_type": None, "static": True,
                                "where": t.name})
                    continue
                b = self.bound(t, an)
                if b:
                    tag = b[0][1]
                    out.append({"name": an, "units": tag.get("eng_units") or a.get("units"),
                                "point_type": tag.get("point_type"), "static": False, "where": t.name})
        for p in self.points:
            out.append({"name": str(p.get("name")), "units": p.get("eng_units"), "point_type": p.get("point_type"),
                        "static": False, "where": "tag", "description": p.get("description")})
        return out


# ── the index cache ───────────────────────────────────────────────────
#
# Building the index costs one GET per template, one per element up to MAX_INDEX_ELEMENTS, one
# for the tag list and one for the sites: ~52 round trips on a QA-sized plant, ~300 on a real
# one. That was paid again on EVERY Describe request and every grounding preview, before the
# model was even asked - and it is time taken out of the same budget the repair loop cannot
# currently afford (one round at the shipped 120 s timeout already exceeds the 150 s budget).
#
# The index is read-only by contract: plan_scope, subjects, asset_map_lines, extra_points,
# source_scope and _index_rows all only read it. That is what makes one instance safe to share
# between concurrent requests. Anything added later that MUTATES a PlantIndex must copy first.
INDEX_TTL_S = 60.0          # env ORCH_PLANT_INDEX_TTL_S; 0 or less disables the cache entirely
_INDEX_CACHE_MAX = 4        # distinct (historian, user, cap) combinations kept


class _IndexEntry:
    __slots__ = ("index", "built_at", "lock")

    def __init__(self) -> None:
        self.index: Optional[PlantIndex] = None
        self.built_at = 0.0
        self.lock = asyncio.Lock()


_INDEX_CACHE: Dict[Tuple[str, str, int], _IndexEntry] = {}
_INDEX_CACHE_LOCK = asyncio.Lock()
_INDEX_HITS = [0, 0]        # [hits, misses]


def index_ttl_s() -> float:
    """The cache lifetime, re-read per call so it can be changed without a restart."""
    raw = os.environ.get("ORCH_PLANT_INDEX_TTL_S")
    if raw is None or not str(raw).strip():
        return INDEX_TTL_S
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return INDEX_TTL_S


def _index_key(ground: Any, max_elements: int) -> Optional[Tuple[str, str, int]]:
    """A ground that does not say WHICH historian it reads is never cached: two such grounds
    could be different plants and would silently share one index."""
    base = getattr(ground, "base_url", None)
    if not isinstance(base, str) or not base:
        return None
    user = getattr(ground, "user", "")
    return (base, str(user or ""), int(max_elements))


def invalidate_index_cache(ground: Any = None) -> int:
    """Drop the cached index for one `ground`, or all of them. Returns how many were dropped.
    Call it after anything that changes the plant's assets."""
    if ground is None:
        n = len(_INDEX_CACHE)
        _INDEX_CACHE.clear()
        return n
    base = getattr(ground, "base_url", None)
    gone = [k for k in _INDEX_CACHE if base is not None and k[0] == base]
    for k in gone:
        del _INDEX_CACHE[k]
    return len(gone)


def index_cache_stats() -> Dict[str, Any]:
    """What the cache is holding, for /health and the diagnostics."""
    now = time.monotonic()
    return {
        "ttl_s": index_ttl_s(),
        "entries": sum(1 for e in _INDEX_CACHE.values() if e.index is not None),
        "hits": _INDEX_HITS[0], "misses": _INDEX_HITS[1],
        "ages_s": sorted(round(now - e.built_at, 1) for e in _INDEX_CACHE.values()
                         if e.index is not None),
    }


async def build_index(ground: Any, *, max_elements: int = MAX_INDEX_ELEMENTS,
                      use_cache: bool = True) -> Optional[PlantIndex]:
    """The plant index, or None when `ground` cannot list templates (or a read fails).

    Cached for `index_ttl_s()` seconds per (historian, user, cap). A concurrent miss on the same
    key waits for the one build rather than starting a second: two Describe requests arriving
    together used to cost twice the round trips. `use_cache=False` always rebuilds.

    A failed build is never cached, so one unreachable moment does not poison the next minute."""
    ttl = index_ttl_s()
    key = _index_key(ground, max_elements) if use_cache else None
    if ttl <= 0 or key is None:
        _INDEX_HITS[1] += 1
        return await _build_index_uncached(ground, max_elements=max_elements)

    async with _INDEX_CACHE_LOCK:
        entry = _INDEX_CACHE.get(key)
        if entry is None:
            if len(_INDEX_CACHE) >= _INDEX_CACHE_MAX:       # evict the oldest build
                oldest = min(_INDEX_CACHE, key=lambda k: _INDEX_CACHE[k].built_at)
                del _INDEX_CACHE[oldest]
            entry = _IndexEntry()
            _INDEX_CACHE[key] = entry

    async with entry.lock:
        now = time.monotonic()
        if entry.index is not None and (now - entry.built_at) < ttl:
            _INDEX_HITS[0] += 1
            return _handover(entry.index, True, round(now - entry.built_at, 1))
        _INDEX_HITS[1] += 1
        idx = await _build_index_uncached(ground, max_elements=max_elements)
        if idx is None:
            # A failed build caches nothing. Drop the entry we reserved, so an unreachable
            # moment leaves no empty shell behind holding a key.
            async with _INDEX_CACHE_LOCK:
                if _INDEX_CACHE.get(key) is entry and entry.index is None:
                    del _INDEX_CACHE[key]
            return None
        entry.index = idx
        entry.built_at = time.monotonic()
        return _handover(idx, False, 0.0)


def _handover(idx: PlantIndex, from_cache: bool, age_s: float) -> PlantIndex:
    """The caller gets its own PlantIndex object, sharing the (read-only) dicts underneath.

    `from_cache` and `cache_age_s` describe THIS hand-over, not the index, so writing them onto
    the shared instance was wrong: a later cache hit rewrote them under an earlier caller that
    still held the same object, and its `from_cache` flipped to True retroactively. The copy also
    means a caller that breaks the read-only contract cannot poison what is cached."""
    out = copy.copy(idx)
    out.from_cache = from_cache
    out.cache_age_s = age_s
    return out


async def _build_index_uncached(ground: Any, *, max_elements: int = MAX_INDEX_ELEMENTS) -> Optional[PlantIndex]:
    """The reads themselves. build_index is the cached door in front of this."""
    tf = getattr(ground, "templates", None)
    if not callable(tf):
        return None
    idx = PlantIndex()
    try:
        tpls = await _g(tf) or []
        for d in tpls:
            if isinstance(d, dict) and isinstance(d.get("id"), int):
                idx.templates.append(Template(d))
        budget = max_elements
        parents: set = set()
        for t in idx.templates:
            # Stop here too, not only in the inner loop: the inner `break` left this loop running,
            # so every remaining template still cost one template_elements GET for nothing.
            if budget <= 0:
                idx.truncated = True
                break
            ids = list(await _g(ground.template_elements, t.id) or [])
            for eid in ids:
                if budget <= 0:
                    idx.truncated = True
                    break
                el = await _g(ground.element, int(eid))
                idx.reads += 1
                budget -= 1
                if isinstance(el, dict) and isinstance(el.get("id"), int):
                    e = Element(el)
                    if e.template is None:
                        e.template = t.id
                        e.template_name = t.name
                    idx.elements[e.id] = e
                    t.elements.append(e.id)
                    if e.parent is not None:
                        parents.add(e.parent)
        for pid in sorted(parents):
            if pid in idx.elements or budget <= 0:
                continue
            el = await _g(ground.element, pid)
            idx.reads += 1
            budget -= 1
            if isinstance(el, dict) and isinstance(el.get("id"), int):
                idx.elements[pid] = Element(el)
        sp = getattr(ground, "search_points", None)
        if callable(sp):
            try:
                idx.points = [p for p in await _g(sp, "", 1000) or [] if isinstance(p, dict) and p.get("name")]
            except Exception:
                idx.points = []
        sf = getattr(ground, "sites", None)
        if callable(sf):
            try:
                idx.sites = [s for s in (await _g(sf) or {}).get("sites") or [] if isinstance(s, dict)]
            except Exception:
                idx.sites = []
    except Exception:
        return None
    return idx


# ── asset context for specific tags ───────────────────────────────────

def tag_asset(idx: Optional[PlantIndex], tag_name: Any) -> Optional[str]:
    """"Pump P-501 | Bearing Temp" for a tag bound to an element's attribute (several: joined)."""
    if idx is None or not tag_name:
        return None
    own = idx.tag_owner(str(tag_name))
    if not own:
        return None
    return "; ".join(f"{e.name} | {a}" for e, a in own[:3])


def _hits(text: str, name: str) -> bool:
    """A request word names this name (stemmed, or an identifying token like P-503)."""
    req = set(stems(text))
    raw = {w.casefold() for w in tokens(text)}
    if any(w.casefold() in raw for w in _distinct_tokens(name)):
        return True
    toks = [w for w in _name_tokens(name) if w not in _GENERIC]
    return bool(toks) and any(w in req for w in toks)


def _names(text: str, name: str) -> bool:
    """The request names this element / tag itself (an identifying token such as P-503 or
    TI-401, or the whole name) — not merely its kind ("pump")."""
    raw = {w.casefold() for w in tokens(text)}
    if name_key(name) in name_key(text):
        return True
    return any(w.casefold() in raw for w in _distinct_tokens(name))


def asset_map_lines(idx: Optional[PlantIndex], request: str, *, max_lines: int = MAX_ASSET_LINES,
                    max_entries: int = MAX_ASSET_ENTRIES,
                    relevant_only: bool = False) -> Tuple[List[str], int, int]:
    """The compact asset map for a points-scope prompt → (lines, entries shown, entries in all).
    Lines whose template / attribute the request names come first; inside a line, the entries
    the request names are always shown, then the first ones up to `max_entries`. A larger store
    is summarized (R251): at most `max_lines` lines, and with `relevant_only` only the lines the
    request's words name — the rest is one "… and N more" line, never silent."""
    if idx is None:
        return [], 0, 0
    groups: List[Tuple[int, str, List[str], int]] = []
    shown_n = total_n = 0
    for t in idx.templates:
        parent = idx.common_parent(t)
        for a in t.attrs:
            an = str(a["name"])
            b = idx.bound(t, an)
            if not b:
                continue
            total_n += len(b)
            named = [(e, tag) for e, tag in b if _names(request, e.name) or _names(request, str(tag.get("name")))]
            rest = [(e, tag) for e, tag in b if (e, tag) not in named]
            pick = named + rest[:max(0, max_entries - len(named))]
            more = len(b) - len(pick)
            units = b[0][1].get("eng_units") or a.get("units")
            head = (f"- {t.name} | {an} ({len(b)} of {len(idx.members(t))} {_plural_last(t.name)} have a tag"
                    + (f", under {parent.path}" if parent else "") + (f"; {units}" if units else "") + "): ")
            ents = ", ".join(f"{tag.get('name')} = {e.name}" for e, tag in pick)
            if more:
                ents += f", … {more} more in the same pattern"
            score = (2 if _hits(request, an) else 0) + (1 if _hits(request, t.name) else 0) + (1 if named else 0)
            groups.append((score, f"{t.name}|{an}", [head + ents], len(pick)))
    groups.sort(key=lambda g: -g[0])
    keep = [g for g in groups if g[0] > 0 or not relevant_only][:max(0, max_lines)]
    lines: List[str] = []
    for g in keep:
        lines += g[2]
        shown_n += g[3]
    if len(groups) > len(keep):
        lines.append(f"- … and {len(groups) - len(keep)} more template attributes with tags (not shown)")
    return lines, shown_n, total_n


def extra_points(idx: Optional[PlantIndex], request: str, have: Iterable[Any]) -> List[Dict[str, Any]]:
    """Tags whose ASSET the request names (attribute / element / template) although their own name
    and description do not: "bearing temperature" finds SIM-TI-401.PV (Pump P-501 | Bearing Temp)."""
    if idx is None:
        return []
    seen = {name_key(x) for x in have}
    by_name = {name_key(p.get("name")): p for p in idx.points}
    out: List[Dict[str, Any]] = []
    scored: List[Tuple[int, str]] = []
    for t in idx.templates:
        for a in t.attrs:
            an = str(a["name"])
            for e, tag in idx.bound(t, an):
                s = _attr_strength(an, request)
                if s == 0:
                    continue
                s += 1 if _hits(request, t.name) else 0
                s += 2 if _hits(request, e.name) else 0
                scored.append((-s, str(tag.get("name"))))
    for _s, n in sorted(scored):
        k = name_key(n)
        if k in seen or k not in by_name:
            continue
        seen.add(k)
        out.append(by_name[k])
        if len(out) >= MAX_EXTRA_POINTS:
            break
    return out


# ── what the request is about ─────────────────────────────────────────

def _attr_strength(attr: str, request: str, point_type: Optional[str] = None) -> int:
    """1 + the number of its words when every word of the attribute is in the request (stemmed:
    "Bearing Temp" = 3 beats "Temperature" = 2 for "bearing temperature"), 2 when (digital) an
    activity word names a state attribute, 1 when only its head word is; 0 = no."""
    req = set(stems(request))
    toks = [w for w in _name_tokens(attr) if w]
    if not toks:
        return 0
    if all(w in req for w in toks):
        return 1 + len(toks)
    is_state = (str(point_type or "").lower() == "digital" or any(w in ("state", "status") for w in toks))
    if is_state and req & _ACTIVITY:
        return 2
    if toks[-1] in req and toks[-1] not in _GENERIC:
        return 1
    return 0


class Subject:
    def __init__(self, template: Template, attrs: List[str], name_hit: bool, score: int) -> None:
        self.template = template
        self.attrs = attrs
        self.name_hit = name_hit
        self.score = score


def _template_point_type(idx: PlantIndex, t: Template, attr: str) -> Optional[str]:
    b = idx.bound(t, attr)
    return b[0][1].get("point_type") if b else None


def subjects(idx: PlantIndex, request: str) -> List[Subject]:
    """Templates the request is about, best first: +3 when a word names the template (its last
    word, "pump", or its whole name), +2 / +1 per attribute it names strongly / by its head word.
    An attribute counts only when some element of the template has a tag (or it is static)."""
    req = set(stems(request))
    out: List[Subject] = []
    for t in idx.templates:
        if not t.elements:
            continue                                  # a template with no element runs nowhere
        nt = _name_tokens(t.name)
        name_hit = bool(nt) and (nt[-1] in req or all(w in req for w in nt))
        attrs: List[str] = []
        score = 3 if name_hit else 0
        # R250: each further word of the template's name the request says ("feed" in "each feed
        # pump") makes it the better subject — two pump templates no longer tie on "pump".
        if name_hit:
            score += sum(1 for w in nt[:-1] if w in req and w not in _GENERIC)
        for a in t.attrs:
            an = str(a["name"])
            if a.get("kind") != "static" and not idx.bound(t, an):
                continue
            s = _attr_strength(an, request, _template_point_type(idx, t, an))
            if s:
                attrs.append(an)
                score += s
        if score > 0:
            out.append(Subject(t, attrs, name_hit, score))
    out.sort(key=lambda s: (-s.score, s.template.name))
    return out


def named_elements(idx: PlantIndex, request: str) -> List[Element]:
    """Elements the request names by an identifying token ("P-503", "T-301", "MCC-201")."""
    raw = {w.casefold() for w in tokens(request)}
    out = []
    for e in idx.elements.values():
        d = [w.casefold() for w in _distinct_tokens(e.name)]
        if d and any(w in raw for w in d):
            out.append(e)
    return out


def named_tags(idx: PlantIndex, request: str) -> List[str]:
    """Tags the request names, whole or by a dotted / hyphenated segment run ("TI-401")."""
    out = []
    for w in tokens(request):
        if not re.search(r"\d", w) or len(w) < 4:
            continue
        rx = re.compile(r"(^|[-_.])" + re.escape(w) + r"($|[-_.])", re.I)
        for p in idx.points:
            n = str(p.get("name") or "")
            if n.casefold() == w.casefold() or rx.search(n):
                out.append(n)
    return list(dict.fromkeys(out))


_ACROSS_RE = re.compile(r"\b(across|combined|altogether|overall|all together|in total|whole|entire|"
                        r"of any|any of the|of all|sum of all|total of all|how many|number of (?:the )?\w+s (?:that|which)? ?"
                        r"(?:are|is|were))\b", re.I)


def intent(request: str, subj: Subject) -> str:
    """'per' (a value for each element), 'across' (one value over all of them), 'ambiguous'
    ("for all pump bearings"), or 'plain' (no plural, no each / all: the element at hand)."""
    t = _norm_text(request)
    nt = _name_tokens(subj.template.name)
    noun = nt[-1] if nt else ""
    words = tokens(t)
    st = [stem(w) for w in words]
    low = [w.casefold() for w in words]
    across = bool(_ACROSS_RE.search(t))
    per = False
    for i, w in enumerate(low):
        if w in ("each", "every", "per", "by", "individual", "individually", "respective"):
            nxt = [st[j] for j in range(i + 1, min(i + 4, len(st))) if low[j] not in ("the", "a", "an")]
            if nxt and noun and nxt[0] == noun:
                per = True
            elif nxt and nxt[0] not in _TIME_NOUNS and low[i] in ("each", "every") and \
                    any(x == noun for x in nxt[:2]):
                per = True
    if re.search(r"\b(each|every)\s+(one|of them)\b", t, re.I):
        per = True
    if per and not across:
        return "per"
    if across and not per:
        return "across"
    if per and across:
        return "ambiguous"
    has_all = bool(re.search(r"\ball\b", t, re.I))
    plural = any(w.casefold().endswith("s") and not re.search(r"['’]s$", w) and stem(w) == noun
                 and w.casefold() != noun for w in words)
    if has_all:
        return "ambiguous"
    if plural:
        return "ambiguous"
    return "plain"


# ── refusals the whole plant decides ─────────────────────────────────

_NOT_A_CALC_RE = re.compile(r"\b(e-?mail|text me|sms|send (?:me |a |an )?(?:message|notification|email|text)|"
                            r"notify me|call me|page me|tweet|post (?:it|to)|slack)\b", re.I)
_SHIFT_RE = re.compile(r"\b(per|each|every|by|this|last|previous|the|of the|end of|start of)\s+shifts?\b|"
                       r"\bshift (?:total|average|avg|max|min|count|report)\b", re.I)
# Equipment a request may be about ("the compressors", "combustion turbine output"): when the plant
# mentions it nowhere, the request is about equipment this plant does not have (R243). A closed
# list on purpose — an unknown word (a synonym such as "fill", "throughput") is left to the model,
# which can map it to what exists; only a named piece of equipment that is absent is refused here.
_EQUIPMENT = frozenset("""compressor boiler turbine chiller blower conveyor kiln reactor exchanger generator
mixer agitator dryer furnace separator centrifuge extruder crusher evaporator condenser heater oven
transformer inverter scrubber cyclone thickener clarifier digester aerator silo hopper engine fan
gearbox spindle press robot""".split())


def _absent(word: str, uni: set) -> bool:
    w = word.casefold()
    return w not in uni and stem(w) not in uni and w.rstrip("s") not in uni


def plant_refusal(idx: PlantIndex, request: str) -> Optional[Dict[str, str]]:
    """{topic, reason} when the whole plant cannot serve the request (no model call), else None."""
    if _NOT_A_CALC_RE.search(request):
        return {"topic": "a message", "reason": (
            "A calc can't send an email or a message — it computes values and writes them to tags. "
            "You could compute a flag (1 when it happens, else 0) and let an alarm or the Command "
            "Center watch that tag.")}
    if _SHIFT_RE.search(request) and idx.sites and not any(
            isinstance(s.get("shifts"), dict) and s["shifts"].get("days") for s in idx.sites):
        return {"topic": "a shift value", "reason": (
            "This plant has no shift pattern, so a calc can't run per shift — set the shifts under "
            "Sites first, or ask per hour, per day, or every 8 or 12 hours instead.")}
    # The two checks below refuse the request outright, before any model call, on the grounds that
    # a word appears NOWHERE in the plant. That is only sound over a COMPLETE index. Past
    # MAX_INDEX_ELEMENTS the element reads stop (idx.truncated), so an element named
    # "Compressor C-401" beyond the cap was never seen and "This plant has no compressor" would be
    # a flat falsehood - stated to the user as settled fact. An absence check over partial data is
    # not an absence check, so on a truncated index these are skipped and the request goes to the
    # model, which can still answer "not in this plant" from the grounding it is shown.
    if idx.truncated:
        return None
    uni = idx.universe_stems()
    words = tokens(request)
    low = [w.casefold() for w in words]
    # Does the request name anything this plant DOES have? If so, an absent medium is a qualifier
    # on a quantity that exists, not the subject: a tank farm whose tanks carry a "Level" can
    # answer "the average water level in each tank" perfectly well even though nothing anywhere is
    # called water. Without this, that request - and "the oil temperature", and "the air
    # temperature in the tank farm" - was refused outright, with no model call, as "This plant has
    # no data about water". That is the shape of the complaint this module was written to fix.
    # calc_authoring._no_data_reason already applies exactly this test before it says the same
    # thing; plant_refusal did not.
    present = [w for w in low
               if len(w) >= 4 and w not in _GENERIC and (w in uni or stem(w) in uni)]
    # A medium the plant mentions nowhere, AND nothing else in the request it has either
    # ("the steam flow" at a plant with neither steam nor flow): it is about data it lacks.
    for w in low:
        if w in _MEDIA and _absent(w, uni) and not present:
            return {"topic": w, "reason": f"This plant has no data about {w} — none of its tags, templates "
                                          f"or elements mentions {w}."}
    # Equipment the plant mentions nowhere ("bearing temperature of the compressors").
    for w in words:
        st = stem(w)
        if st in _EQUIPMENT and _absent(w, uni):
            return {"topic": w.casefold(), "reason": f"This plant has no {w.casefold()} — none of its tags, "
                                                     f"templates or elements mentions {st}."}
    return None


# ── the scope plan ────────────────────────────────────────────────────

def scope_key(scope: Optional[Dict[str, Any]]) -> Optional[Tuple[str, Any]]:
    if not isinstance(scope, dict) or len(scope) != 1:
        return None
    k = next(iter(scope))
    if k == "points":
        return ("points", (scope["points"] or {}).get("site", 1) if isinstance(scope["points"], dict) else 1)
    return (k, scope[k])


def label_of(idx: Optional[PlantIndex], scope: Optional[Dict[str, Any]],
             template: Optional[Template] = None) -> str:
    """How a person reads a scope: "each Centrifugal Pump (10 pumps)", "one value across all
    Centrifugal Pumps (on /Pump Station)", "/Pump Station/Pump P-503 only", "specific tags"."""
    k = scope_key(scope)
    if k is None:
        return "no scope yet"
    if k[0] == "points":
        return "specific tags (site default)" if k[1] == 1 else f"specific tags (site {k[1]})"
    if idx is None:
        return f"{k[0]} {k[1]}"
    if k[0] == "template":
        t = idx.template(k[1])
        if t is None:
            return f"template {k[1]}"
        n = len(t.elements)
        return f"each {t.name} ({n} {_plural_last(t.name) if n != 1 else t.name.split()[-1].lower()})"
    e = idx.elements.get(k[1])
    if e is None:
        return f"element {k[1]}"
    if template is None:
        # An element that is the home of a template's members reads as "one value across" them.
        template = next((t for t in idx.templates if t.elements and idx.common_parent(t) is e), None)
    if template is not None:
        return f"one value across all {_plural(template.name)} (on {e.path})"
    return f"{e.path} only"


def _choice(idx: PlantIndex, scope: Dict[str, Any], why: str, template: Optional[Template] = None) -> Dict[str, Any]:
    return {"scope": scope, "label": label_of(idx, scope, template), "why": why}


def _why_subject(idx: PlantIndex, subj: Subject, request: str) -> str:
    t = subj.template
    members = idx.members(t)
    parent = idx.common_parent(t)
    attrs = [a for a in subj.attrs]
    bound = [len(idx.bound(t, a)) for a in attrs if idx.bound(t, a)]
    where = f" under {parent.path}" if parent else ""
    if attrs:
        a_txt = " and ".join(f"'{a}'" for a in attrs[:3])
        k = min(bound) if bound else len(members)
        return (f"{a_txt} {'is an attribute' if len(attrs) == 1 else 'are attributes'} of {t.name}, with a tag "
                f"on {k} of its {len(members)} {_plural_last(t.name)}{where}")
    return f"the request is about {_plural_last(t.name)}: {len(members)} {t.name} element(s){where}"


class Plan:
    """kind: None (draft in the scope given), 'propose' (one scope that fits better, drafted for the
    person to confirm), 'choose' (two readings: the person picks), 'refuse'."""

    def __init__(self, kind: Optional[str] = None, **kw: Any) -> None:
        self.kind = kind
        self.proposal: Optional[Dict[str, Any]] = kw.get("proposal")
        self.choices: List[Dict[str, Any]] = kw.get("choices") or []
        self.refusal: Optional[Dict[str, str]] = kw.get("refusal")
        self.note: Optional[str] = kw.get("note")


def _fits(idx: PlantIndex, scope: Dict[str, Any], targets: Sequence[Dict[str, Any]]) -> bool:
    k = scope_key(scope)
    return any(scope_key(t) == k for t in targets)


def plan_scope(request: str, scope: Optional[Dict[str, Any]], idx: Optional[PlantIndex]) -> Plan:
    """The scope that fits the request (R240). Never silent: a different scope is a proposal or a
    question, decided only from the request's words and what exists."""
    if idx is None:
        return Plan()
    ref = plant_refusal(idx, request)
    if ref is not None:
        return Plan("refuse", refusal=ref)
    cur = scope_key(scope)
    # 1. A named element ("pump P-503", "tank T-301", "MCC-201") with what the request reads.
    els = named_elements(idx, request)
    if len(els) == 1:
        e = els[0]
        target = {"element": e.id}
        if cur == ("element", e.id):
            return Plan()
        return Plan("propose", proposal=_choice(idx, target, f"the request names {e.name}"))
    # 2. A tag named literally ("TI-401"): specific tags fit, or the one element it is bound on.
    tags = named_tags(idx, request)
    if tags and not els:
        if cur is not None and cur[0] == "points":
            return Plan()
        owners = [e for t in tags for e, _a in idx.tag_owner(t)]
        if cur is not None and cur[0] == "element" and any(o.id == cur[1] for o in owners):
            return Plan()
        site = 1
        return Plan("propose", proposal=_choice(idx, {"points": {"site": site}},
                                                f"the request names the tag {tags[0]}"))
    # 3. A template subject: each of them, one value across them, or ask.
    subs = subjects(idx, request)
    if not subs:
        if scope is None:
            return Plan("choose", choices=[_choice(idx, {"template": t.id}, f"{len(t.elements)} {t.name} element(s)")
                                           for t in idx.templates[:5] if t.elements]
                        + [_choice(idx, {"points": {"site": 1}}, "advanced: you name each tag")])
        return Plan()
    # R250: among subjects that fit the words equally, the one the calc is on is the subject —
    # "bearing temperature of each pump" on a calc for each Feed Pump stays there although
    # Centrifugal Pump fits the words as well (R240: nothing is proposed when the scope fits).
    if cur is not None and cur[0] in ("template", "element"):
        here = cur[1] if cur[0] == "template" else (idx.elements[cur[1]].template if cur[1] in idx.elements else None)
        parent_of = {s.template.id: idx.common_parent(s.template) for s in subs}
        for i, s in enumerate(subs):
            if s.score < subs[0].score:
                break
            p = parent_of.get(s.template.id)
            if s.template.id == here or (cur[0] == "element" and p is not None and p.id == cur[1]):
                subs.insert(0, subs.pop(i))
                break
    best = subs[0]
    if len(subs) > 1 and subs[1].score == best.score and not best.name_hit:
        # Two templates fit the words equally (e.g. a temperature on pumps and on tanks).
        if cur is not None and cur[0] == "template" and cur[1] in (best.template.id, subs[1].template.id):
            return Plan()
        return Plan("choose", choices=[_choice(idx, {"template": s.template.id}, _why_subject(idx, s, request))
                                       for s in subs[:2]])
    t = best.template
    parent = idx.common_parent(t)
    per = {"template": t.id}
    across = {"element": parent.id} if parent else None
    why = _why_subject(idx, best, request)
    how = intent(request, best)
    if how == "per":
        targets = [per]
    elif how == "across":
        targets = [across] if across else [per]
    elif how == "ambiguous":
        targets = ([across, per] if across else [per])
    else:                                            # plain: the element at hand fits, else each
        if cur is not None and cur[0] == "element":
            e = idx.elements.get(cur[1])
            if e is not None and e.template == t.id and all(a in e.attrs for a in best.attrs):
                return Plan()
            if e is not None and parent is not None and e.id == parent.id:
                return Plan()
        targets = [per] + ([across] if across else [])
        if scope is not None and _fits(idx, scope, targets):
            return Plan()
        return Plan("propose", proposal=_choice(idx, per, why))
    if scope is not None and _fits(idx, scope, targets):
        return Plan()
    if len(targets) == 1:
        tgt = targets[0]
        return Plan("propose", proposal=_choice(idx, tgt, why, t if "element" in tgt else None))
    return Plan("choose", choices=[_choice(idx, x, why, t if "element" in x else None) for x in targets])


# ── plant-wide sources for a need the current scope lacks (R242) ─────

def source_scope(needs: Sequence[Dict[str, Any]], request: str, scope: Optional[Dict[str, Any]],
                 idx: Optional[PlantIndex]) -> Optional[Dict[str, Any]]:
    """A scope proposal ({scope, label, why}) where every missing need has a source, or None."""
    if idx is None or not needs:
        return None
    text = request + " " + " ".join(str(n.get("quantity") or "") + " " + str(n.get("attribute") or "")
                                    for n in needs)
    subs = subjects(idx, text)
    if not subs:
        return None
    best = subs[0]
    t = best.template
    if not best.attrs:
        return None
    how = intent(request, best)
    parent = idx.common_parent(t)
    target = {"element": parent.id} if (how == "across" and parent) else {"template": t.id}
    if scope_key(target) == scope_key(scope):
        return None
    return _choice(idx, target, _why_subject(idx, best, request), t if "element" in target else None)


# For calc_authoring's relevance ranking of found tags (R251).
names_it = _names
attr_strength = _attr_strength

__all__ = ["fold_text", "names_it", "attr_strength", "PlantIndex", "build_index",
           "invalidate_index_cache", "index_cache_stats", "index_ttl_s", "INDEX_TTL_S", "asset_map_lines", "extra_points", "tag_asset", "plan_scope",
           "source_scope", "label_of", "scope_key", "Plan", "plant_refusal", "subjects", "intent",
           "named_elements", "named_tags", "MAX_INDEX_ELEMENTS", "MAX_ASSET_LINES", "MAX_ASSET_ENTRIES"]
