"""
routes_calcs.py — the PremAlytics authoring endpoints for PremHist's calc Describe box (PremCalc C4).

design_calc.md §6, §10.22. ph-server's `POST /api/v1/calcs/draft` forwards exactly
`{request, current, context, premhist_url, user}` here (PremHist/API.md §11.3.1: `premhist_url` =
its own base URL, `user` = the caller's X-PH-User, `current` / `context` null when absent). This
route is a THIN adapter (the one-stop-shop rule): it refuses a `premhist_url` that is not loopback
/ allow-listed, binds the grounding client to it, and calls the engine
`orchestrator.calc_authoring.draft_calc`, whose judge is PremHist's own
`POST /api/v1/calcs/validate`. Nothing is saved, approved or certified here; the viewer puts the
draft into its editor and the user dry-runs and approves as usual.

Everything both authoring routes do the same way - body parsing, the engine import, the
`premhist_url` / `user` checks, the error envelope, request ids, timing, the status map - lives in
routes_common.py, so the two cannot drift apart again.

Body: `request` (text), `premhist_url`, `user` (absent = anonymous), `current` (the editor's calc
document, or null), `context` (null, or `{"calc": id | null, "name": text | null, "kind":
"expression" | "frame" | null, "scope": Scope | null, "scope_try": Scope?, "keep_scope": bool?}` —
the open calc; its own scope wins. R240 / R241: `scope_try` = the scope the person clicked in a
proposal or a choice, `keep_scope` = "Keep <scope>" (no proposal; draft where it is).

    POST /api/calcs/draft      200 ok, scope_proposal, choose_scope · 400 bad_request ·
                               422 no_draft / too_broad / needs_limits · 503 llm_unavailable
    POST /api/calcs/grounding  what the model WOULD be shown for this request, with no model call
                               (same body; see calc_authoring.preview_grounding)
"""

from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import routes_common as rc

router = APIRouter()

_FIELDS = frozenset({"request", "premhist_url", "user", "current", "context"})


def _is_id(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _common(body: Dict[str, Any], allowed, rid: str):
    """The checks shared by draft and grounding. Returns (args, None) or (None, refusal)."""
    err = rc.unknown_fields(body, _FIELDS, request_id=rid)
    if err:
        return None, err
    trio, err = rc.check_caller(body, allowed, request_id=rid, what="calc")
    if err:
        return None, err
    request, premhist_url, user = trio
    current = body.get("current")
    context = body.get("context")
    if current is not None and not isinstance(current, dict):
        return None, rc.bad("current must be a calc document (an object)", request_id=rid)
    if context is not None and not isinstance(context, dict):
        return None, rc.bad("context must be an object", request_id=rid)
    ctx: Dict[str, Any] = dict(context or {})
    if ctx.get("calc") is not None and not _is_id(ctx["calc"]):
        return None, rc.bad("context.calc must be a calc id", request_id=rid)
    if ctx.get("scope") is not None and not isinstance(ctx["scope"], dict):
        return None, rc.bad("context.scope must be {\"template\": id}, {\"element\": id} or "
                            "{\"points\": {...}}", request_id=rid)
    if ctx.get("name") is not None and not isinstance(ctx["name"], str):
        return None, rc.bad("context.name must be text", request_id=rid)
    if ctx.get("kind") == "frame":
        return None, rc.bad("Describe drafts expression calcs; event-frame calcs are written in "
                            "the editor", request_id=rid)
    if ctx.get("scope_try") is not None and not isinstance(ctx["scope_try"], dict):
        return None, rc.bad("context.scope_try must be {\"template\": id}, {\"element\": id} or "
                            "{\"points\": {...}}", request_id=rid)
    if ctx.get("keep_scope") is not None and not isinstance(ctx["keep_scope"], bool):
        return None, rc.bad("context.keep_scope must be true or false", request_id=rid)
    ctx = {k: ctx[k] for k in ("calc", "scope", "name", "scope_try", "keep_scope")
           if ctx.get(k) is not None}
    return (request, premhist_url, user, current, ctx), None


@router.post("/api/calcs/draft")
async def calcs_draft(req: Request) -> JSONResponse:
    rid, started = rc.new_request_id(), time.monotonic()
    body, err = await rc.read_body(req, request_id=rid)
    if err:
        return err
    try:
        draft_calc, PremhistRest, allowed, _preview = rc.load_engine("calcs")
    except Exception as e:                               # noqa: BLE001
        return rc.engine_unavailable("calc authoring", e, request_id=rid)
    args, err = _common(body, allowed, rid)
    if err:
        return err
    request, premhist_url, user, current, ctx = args
    result = await draft_calc(request, ground=PremhistRest(premhist_url, user),
                              current=current, context=ctx)
    return rc.answer(result, request_id=rid, started=started)


@router.post("/api/calcs/grounding")
async def calcs_grounding(req: Request) -> JSONResponse:
    """What the model would be shown, without asking it: the exact messages, their size against
    the prompt budget, the scope the planner picked and why, and what the plant index saw.

    Most Describe complaints are grounding complaints - "it says my plant has no X" - and until
    now the only way to see the grounding was to read it out of a model call that had already
    happened. This costs no model time, so it can be run on every puzzling request."""
    rid, started = rc.new_request_id(), time.monotonic()
    body, err = await rc.read_body(req, request_id=rid)
    if err:
        return err
    try:
        _draft, PremhistRest, allowed, preview = rc.load_engine("calcs")
    except Exception as e:                               # noqa: BLE001
        return rc.engine_unavailable("calc authoring", e, request_id=rid)
    args, err = _common(body, allowed, rid)
    if err:
        return err
    request, premhist_url, user, current, ctx = args
    result = await preview(request, ground=PremhistRest(premhist_url, user),
                           current=current, context=ctx)
    return rc.answer(result, request_id=rid, started=started)
