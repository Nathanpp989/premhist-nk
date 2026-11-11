"""
display_describe_log.py - a local outcome log of every PremHist display Describe request.

The counterpart of `calc_describe_log` (design_calc.md R246), for the other engine. README-QA
sections 4 and 7 tell the tester to read "the Describe log" when a request comes back wrong, and
the status table offers a Describe column for both tabs - but only calcs were ever written down.
A display that drafted the wrong symbols, or came back with placeholders, left no trace at all
once the viewer tab was closed, so the one artefact a QA pass most needs - what was asked, what
came back, and how long it took - did not exist for half the feature.

Same rules as the calc log, deliberately: one JSON line per `display_authoring.draft_display`
call, appended to a LOCAL file (never the network - the air-gapped rule), never raising (a log
must not break drafting). The engine writes it, so every caller is logged the same way.

Path: env `PREMALYTICS_DISPLAY_DESCRIBE_LOG` (a file path; `off` / `0` / empty-after-strip
disables it), else `<product root>/logs/display_describe_log.jsonl`.

A line: `{ts, request, follow_up, outcome, code, headline, understood, symbols, glyphs,
placeholders, refused_ops, unmatched, model, run_id, seconds, repairs, model_calls, notes}` -
`outcome` is one of `draft`, `edit`, `no_change`, `refusal`, `bad_request`, `error`.

Two deliberate differences from the calc log:

- The calc log keeps the drafted `body`, because a calc body is a few lines of text and is the
  thing you want to read back. A display document is hundreds of lines of geometry, so this logs
  a SUMMARY of what was drawn - the symbol count and the glyph kinds - and not the document.
  Reading a jsonl in Notepad stays possible, and the log cannot grow without bound.
- `no_change` is its own outcome. Two display paths answer `ok: True` having deliberately drawn
  nothing (an undo/publish request, and "repeat this for every pump" on a follow-up). Counting
  those as drafts would overstate what Describe produced, which is exactly the number a QA pass
  is trying to establish.

As with the calc log, the request is the person's own text (capped at 2,000 characters) and
nothing else from the plant is written: no tag values, no element data.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

ENV = "PREMALYTICS_DISPLAY_DESCRIBE_LOG"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(_ROOT, "logs", "display_describe_log.jsonl")
_LOCK = threading.Lock()
# `bad_request` is kept distinct rather than folded into `error`: it means the request never
# reached the model (empty, too long, or a malformed current document), which is a different
# thing to diagnose than a failure during drafting.
_OUTCOME = {"no_draft": "refusal", "too_broad": "refusal", "bad_request": "bad_request"}
_MAX_GLYPHS = 40

# The shared writer (caps, line backstop, rotation at a size cap). Guarded for the same reason the
# calc log guards it: a missing describe_log_io should cost the caps, never the drafting.
try:
    from . import describe_log_io as _io
except Exception:                                                    # noqa: BLE001
    _io = None                                                       # type: ignore[assignment]


def log_path() -> Optional[str]:
    """The file the log goes to, or None when it is turned off."""
    if _io is not None:
        return _io.resolve(ENV, DEFAULT_PATH)
    raw = os.environ.get(ENV)
    if raw is None:
        return DEFAULT_PATH
    raw = raw.strip()
    if not raw or raw.lower() in ("off", "0", "false", "no", "none"):
        return None
    return raw


def outcome_of(result: Dict[str, Any], follow_up: bool = False) -> str:
    """`draft` / `edit` / `no_change` / `refusal` / `bad_request` / `error`."""
    if result.get("ok") is not True:
        return _OUTCOME.get(str(result.get("code")), "error")
    auth = result.get("authoring") if isinstance(result.get("authoring"), dict) else {}
    if auth.get("model_calls") == 0:
        return "no_change"
    return "edit" if follow_up else "draft"


def _walk(symbols: Any) -> List[Dict[str, Any]]:
    """Every symbol, groups flattened. Mirrors display_authoring._walk without importing it, so
    the log never depends on the engine it is logging (and cannot be broken by a change there)."""
    out: List[Dict[str, Any]] = []
    stack = list(symbols) if isinstance(symbols, list) else []
    while stack:
        s = stack.pop(0)
        if not isinstance(s, dict):
            continue
        out.append(s)
        kids = s.get("symbols") or s.get("children")
        if isinstance(kids, list):
            stack = list(kids) + stack
    return out


def _drawn(display: Any) -> Dict[str, Any]:
    """What was drawn, as counts and glyph kinds - never the document itself."""
    syms = _walk((display or {}).get("symbols") if isinstance(display, dict) else None)
    kinds: Dict[str, int] = {}
    for s in syms:
        k = s.get("kind") or s.get("glyph") or s.get("type")
        if isinstance(k, str) and k.strip():
            kinds[k.strip()] = kinds.get(k.strip(), 0) + 1
    top = sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))[:_MAX_GLYPHS]
    return {"symbols": len(syms) or None,
            "glyphs": {k: n for k, n in top} or None}


def _names(items: Any, limit: int = 20) -> Optional[List[str]]:
    """Placeholder / refused-op entries as short strings, whatever shape the engine used."""
    if not isinstance(items, list) or not items:
        return None
    out: List[str] = []
    for it in items[:limit]:
        if isinstance(it, str):
            out.append(it[:200])
        elif isinstance(it, dict):
            txt = (it.get("text") or it.get("label") or it.get("name") or it.get("reason")
                   or it.get("op") or it.get("id"))
            out.append(str(txt)[:200] if txt else json.dumps(it, default=str)[:200])
        else:
            out.append(str(it)[:200])
    return out


def entry(request: Any, context: Optional[Dict[str, Any]], result: Dict[str, Any],
          seconds: Optional[float] = None) -> Dict[str, Any]:
    """The JSON line for one request (pure)."""
    ctx = context if isinstance(context, dict) else {}
    auth = result.get("authoring") if isinstance(result.get("authoring"), dict) else {}
    follow_up = bool(ctx.get("follow_up"))
    out = outcome_of(result, follow_up)
    headline = result.get("reason") or result.get("error")
    secs = result.get("seconds") if isinstance(result.get("seconds"), (int, float)) else seconds
    drawn = _drawn(result.get("display")) if result.get("ok") is True else {"symbols": None, "glyphs": None}
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "request": str(request if request is not None else "")[:2000],
        "follow_up": follow_up or None,
        "outcome": out,
        "code": result.get("code") if result.get("ok") is not True else None,
        "headline": (str(headline)[:600] if headline else None),
        "understood": (str(result.get("understood"))[:600] if result.get("understood") else None),
        "symbols": drawn["symbols"],
        "glyphs": drawn["glyphs"],
        "placeholders": _names(result.get("placeholders")),
        "refused_ops": _names(result.get("refused_ops")),
        # The nouns nothing in this historian matched (D5.1) - the single most useful field when a
        # display came back missing something, and the reason `missing` is now on the result too.
        "unmatched": (list(result.get("missing"))[:20] or None) if result.get("missing") else None,
        "model": auth.get("model"),
        "run_id": auth.get("run_id"),
        "seconds": round(float(secs), 3) if isinstance(secs, (int, float)) else None,
        "repairs": auth.get("repairs"),
        "model_calls": auth.get("model_calls"),
        # Capped. This was the one field here with no limit on it - the same defect the calc log
        # had, which I reproduced there and then found in my own file.
        "notes": (_io.items(result.get("notes")) if _io is not None
                  else (list(result.get("notes")) if result.get("notes") else None)),
    }


def record(request: Any, context: Optional[Dict[str, Any]], result: Dict[str, Any],
           seconds: Optional[float] = None) -> Optional[str]:
    """Appends the line; returns the path written, or None (off, or the write failed). Never raises."""
    try:
        path = log_path()
        if path is None:
            return None
        e = entry(request, context, result, seconds)
        if _io is not None:
            return _io.append(path, _io.line_of(e), _LOCK)
        line = json.dumps(e, ensure_ascii=False, default=str)
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with _LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return path
    except Exception:
        return None


__all__ = ["record", "entry", "log_path", "outcome_of", "ENV", "DEFAULT_PATH"]
