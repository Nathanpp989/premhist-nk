"""
calc_describe_log.py — a local outcome log of every PremCalc Describe request (design_calc.md R246).

So we can learn from real use (Jim, 2026-10-23): one JSON line per `calc_authoring.draft_calc`
call, appended to a LOCAL file — never the network (air-gapped rule), never raising (a log must not
break drafting). The engine writes it, so every caller (the viewer through UI3, a headless caller)
is logged the same way.

Path: env `PREMALYTICS_CALC_DESCRIBE_LOG` (a file path; `off` / `0` / empty-after-strip disables
it), else `<product root>/logs/calc_describe_log.jsonl` (the convention of the other PremAlytics
logs: an env override, else a file under the product; `logs/` is git-ignored).

A line: `{ts, scope, calc, scope_try, keep_scope, request, outcome, code, headline, scope_proposal,
choices, scope_change, body, model, run_id, seconds, repairs, notes}` — `outcome` is one of `draft`,
`scope_proposal`, `choose_scope`, `needs_limits`, `refusal`, `error`. The request is the person's own
text (capped at 2,000 characters); nothing else from the plant is written (no data values).
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Optional

ENV = "PREMALYTICS_CALC_DESCRIBE_LOG"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(_ROOT, "logs", "calc_describe_log.jsonl")
_LOCK = threading.Lock()

# `bad_request` was absent here, so it fell through to "error" - a tester pressing Describe with an
# empty box was recorded identically to an engine crash. That inflates the one number a QA pass is
# trying to establish, so it is now its own outcome, as it already is in display_describe_log.
# `llm_unavailable` is deliberately still "error": the model not answering IS a failure.
_OUTCOME = {"scope_proposal": "scope_proposal", "choose_scope": "choose_scope", "needs_limits": "needs_limits",
            "no_draft": "refusal", "too_broad": "refusal", "bad_request": "bad_request"}

# The shared writer: per-field caps, a line backstop, and rotation at a size cap. Guarded, because
# calc_authoring imports THIS module unguarded - a missing describe_log_io must cost the caps, not
# calc Describe itself. The fallback is the behaviour this file had before: a plain uncapped append.
try:
    from . import describe_log_io as _io
except Exception:                                                    # noqa: BLE001
    _io = None                                                       # type: ignore[assignment]


def _text(v, chars=2000):
    if _io is not None:
        return _io.text(v, chars)
    return (str(v)[:chars] if v else None)


def _items(v, limit=20, chars=1000):
    if _io is not None:
        return _io.items(v, limit, chars)
    return list(v) if isinstance(v, (list, tuple)) and v else None


def _obj(v, chars=2000):
    if _io is not None:
        return _io.obj(v, chars)
    return v


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


def outcome_of(result: Dict[str, Any]) -> str:
    if result.get("ok") is True:
        return "draft"
    return _OUTCOME.get(str(result.get("code")), "error")


def entry(request: Any, context: Optional[Dict[str, Any]], result: Dict[str, Any],
          seconds: Optional[float] = None) -> Dict[str, Any]:
    """The JSON line for one request (pure)."""
    ctx = context if isinstance(context, dict) else {}
    auth = result.get("authoring") if isinstance(result.get("authoring"), dict) else {}
    doc = result.get("document") if isinstance(result.get("document"), dict) else {}
    out = outcome_of(result)
    headline = result.get("reason") or result.get("error")
    if out == "draft":
        headline = result.get("intent") or headline
    secs = result.get("seconds") if isinstance(result.get("seconds"), (int, float)) else seconds
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scope": ctx.get("scope"),
        "calc": ctx.get("calc"),
        "scope_try": ctx.get("scope_try"),
        "keep_scope": bool(ctx.get("keep_scope")) or None,
        "request": str(request if request is not None else "")[:2000],
        "outcome": out,
        "code": result.get("code") if out != "draft" else None,
        "headline": _text(headline, 600),
        # Capped: a model reply can make any of these three arbitrarily long, and one line of this
        # file used to be able to reach a megabyte. Capped WITHOUT re-typing: an object stays an
        # object while it fits, so anything reading these fields still finds the shape it expects.
        "scope_proposal": _obj(result.get("scope_proposal")),
        "choices": _items(result.get("choices")),
        "scope_change": result.get("scope_change"),
        "body": (_text(doc.get("body"), (_io.BODY_CHARS if _io is not None else 20000))
                 if out in ("draft", "scope_proposal", "needs_limits") else None),
        "model": auth.get("model"),
        "run_id": auth.get("run_id"),
        "seconds": round(float(secs), 3) if isinstance(secs, (int, float)) else None,
        "repairs": auth.get("repairs"),
        "notes": _items(result.get("notes")),
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
