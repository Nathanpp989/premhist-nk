"""
routes_common.py — what both authoring routes do identically, in one place.

routes_calcs.py and routes_displays.py each parsed the body, imported the engine, checked
`premhist_url` and `user`, and shaped their errors. The checks were the same; the SHAPES were
not, and that difference reached clients:

    calcs    error -> {ok, code, error, problems, understood, notes}
    displays error -> {ok, code, error, notes}

so anything reading an error had to know which endpoint it had called. Both now answer with one
envelope. The extra keys a display error gains are additive (`problems: []`, `understood: null`),
so a reader that only looked at `error` and `notes` is unaffected.

What else this adds, in the one place both routes pass through:

- a **request id** on every answer (and in the outcome log), so a tester's bug report and the
  server's log line can be tied together - previously nothing connected them;
- **server-side timing** (`route_seconds`), separate from the engine's own `seconds`, which makes
  the gap between "the engine finished" and "the caller got an answer" visible;
- one **status map** covering every code either engine returns, so a new outcome cannot silently
  become a 400 because one route's table was not updated.

Nothing here talks to PremHist, Ollama or the model. It is validation, shaping and bookkeeping.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi.responses import JSONResponse

# PremHist API.md §10.1 / §11.1: 1-64 visible ASCII, no leading or trailing space.
USER_RE = re.compile(r"^[\x21-\x7e](?:[\x20-\x7e]{0,62}[\x21-\x7e])?$")

# Every code either engine can return. A proposal and a question are ANSWERS, not errors
# (R240 / R241), so they are 200 with ok:false - nothing is applied without a click.
STATUS: Dict[str, int] = {
    "bad_request": 400,
    "no_draft": 422,
    "too_broad": 422,
    "needs_limits": 422,
    "llm_unavailable": 503,
    "scope_proposal": 200,
    "choose_scope": 200,
}

# A body bigger than this is refused before it is parsed: the engines cap `request` at 4000
# characters, and `current` / `draft` are documents, not uploads.
MAX_BODY_BYTES = 4 * 1024 * 1024


class EngineUnavailable(Exception):
    """The orchestrator package did not import in this process."""


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def envelope(code: str, error: str, *, request_id: str, ok: bool = False,
             **extra: Any) -> Dict[str, Any]:
    """The one error shape both routes answer with."""
    out: Dict[str, Any] = {"ok": ok, "code": code, "error": error,
                           "problems": [], "understood": None, "notes": [],
                           "request_id": request_id}
    out.update(extra)
    return out


def bad(error: str, *, request_id: str, **extra: Any) -> JSONResponse:
    return JSONResponse(envelope("bad_request", error, request_id=request_id, **extra),
                        status_code=400)


def engine_unavailable(what: str, exc: BaseException, *, request_id: str) -> JSONResponse:
    return JSONResponse(
        envelope("llm_unavailable", f"{what} is not available here: {exc}", request_id=request_id),
        status_code=503)


async def read_body(req: Any, *, request_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[JSONResponse]]:
    """(body, None) or (None, the refusal). Reads the raw bytes first so an enormous body is
    refused before any JSON parser sees it."""
    try:
        raw = await req.body()
    except Exception:
        return None, bad("the body could not be read", request_id=request_id)
    if len(raw) > MAX_BODY_BYTES:
        return None, bad(f"the body is over {MAX_BODY_BYTES // (1024 * 1024)} MB", request_id=request_id)
    try:
        import json
        body = json.loads(raw.decode("utf-8")) if raw else None
    except Exception:
        return None, bad("the body must be JSON", request_id=request_id)
    if not isinstance(body, dict):
        return None, bad("the body must be a JSON object", request_id=request_id)
    return body, None


def load_engine(kind: str) -> Tuple[Callable[..., Any], Any, Callable[[str], bool], Callable[..., Any]]:
    """(draft fn, PremhistRest, allowed, preview fn) for "calcs" or "displays".

    The import is deliberately HERE and not at module scope, as it always was: describe_app must
    stay importable - and the service must keep starting - when the orchestrator package is
    broken, so that /health can report which engine failed instead of the whole service dying.
    """
    from orchestrator.premhist_rest import PremhistRest, allowed
    if kind == "calcs":
        from orchestrator.calc_authoring import draft_calc, preview_grounding
        return draft_calc, PremhistRest, allowed, preview_grounding
    from orchestrator.display_authoring import draft_display, preview_grounding
    return draft_display, PremhistRest, allowed, preview_grounding


def check_caller(body: Dict[str, Any], allowed: Callable[[str], bool], *, request_id: str,
                 what: str) -> Tuple[Optional[Tuple[str, str, str]], Optional[JSONResponse]]:
    """The three fields every authoring call carries: ((request, premhist_url, user), None) or
    (None, the refusal). `what` names the thing being drafted, for the message."""
    request = body.get("request")
    premhist_url = body.get("premhist_url")
    user = body.get("user") if body.get("user") not in (None, "") else "anonymous"
    if not isinstance(premhist_url, str) or not allowed(premhist_url):
        return None, bad("premhist_url is not allowed (loopback or PREM_DISPLAYS_PREMHIST_ALLOW only)",
                         request_id=request_id)
    if not isinstance(user, str) or not USER_RE.match(user):
        return None, bad("user must be 1-64 visible ASCII characters", request_id=request_id)
    if not isinstance(request, str) or not request.strip():
        return None, bad(f"request: describe the {what} you want", request_id=request_id)
    return (request, premhist_url, user), None


def unknown_fields(body: Dict[str, Any], fields: frozenset, *, request_id: str) -> Optional[JSONResponse]:
    """Refuse a field neither this route nor the engine reads. routes_calcs always did this;
    routes_displays did not, so a typo there was accepted and silently ignored."""
    unknown = sorted(k for k in body if k not in fields)
    if unknown:
        return bad("unknown field(s): " + ", ".join(unknown), request_id=request_id)
    return None


def answer(result: Dict[str, Any], *, request_id: str, started: float) -> JSONResponse:
    """The engine's own result, with the bookkeeping both routes should carry, at the status its
    code maps to. An unknown code is a 400, as before."""
    if not isinstance(result, dict):
        return JSONResponse(
            envelope("bad_request", "the engine returned something that is not a result",
                     request_id=request_id), status_code=400)
    out = dict(result)
    out.setdefault("request_id", request_id)
    out["route_seconds"] = round(time.monotonic() - started, 3)
    status = 200 if out.get("ok") else STATUS.get(str(out.get("code")), 400)
    return JSONResponse(out, status_code=status)


__all__ = ["USER_RE", "STATUS", "MAX_BODY_BYTES", "EngineUnavailable", "new_request_id",
           "envelope", "bad", "engine_unavailable", "read_body", "load_engine", "check_caller",
           "unknown_fields", "answer"]
