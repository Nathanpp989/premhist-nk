"""
routes_displays.py — the PremAlytics authoring endpoints for PremHist's Describe box (D5).

design_displays.md §6 step 3 / §6.1 (R-33). ph-server's `POST /api/v1/displays/draft` forwards
`{request, draft?, locked?}` here with `premhist_url` (its own base URL) and `user` (the caller's
X-PH-User) added. This route is a THIN adapter (the one-stop-shop rule): it refuses a
`premhist_url` that is not loopback / allow-listed, binds the grounding client to it, and calls
the engine `orchestrator.display_authoring.draft_display`. Nothing is saved or published here;
the editor saves the result as the user's draft.

Everything both authoring routes do the same way lives in routes_common.py. Two things that used
to differ here are now the same as calcs: the error envelope (this route answered a shorter one,
so a client had to know which endpoint it had called), and the unknown-field check (a typo in the
body used to be accepted and silently ignored).

    POST /api/displays/draft      200 ok · 400 bad_request · 422 no_draft / too_broad ·
                                  503 llm_unavailable
    POST /api/displays/grounding  what the model WOULD be shown, with no model call
"""

from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import routes_common as rc

router = APIRouter()

_FIELDS = frozenset({"request", "premhist_url", "user", "draft", "locked", "footprints"})


def _common(body: Dict[str, Any], allowed, rid: str):
    err = rc.unknown_fields(body, _FIELDS, request_id=rid)
    if err:
        return None, err
    trio, err = rc.check_caller(body, allowed, request_id=rid, what="display")
    if err:
        return None, err
    request, premhist_url, user = trio
    draft = body.get("draft")
    locked = body.get("locked") or []
    footprints = body.get("footprints")      # D5.2: {"<id>": [x, y, w, h]} canvas px, or null
    if draft is not None and not isinstance(draft, dict):
        return None, rc.bad("draft must be a display document", request_id=rid)
    if not isinstance(locked, list) or not all(isinstance(x, str) for x in locked):
        return None, rc.bad("locked must be a list of symbol ids", request_id=rid)
    if footprints is not None and not isinstance(footprints, dict):
        return None, rc.bad("footprints must be an object of symbol id -> [x, y, w, h]",
                            request_id=rid)
    return (request, premhist_url, user, draft, locked, footprints), None


@router.post("/api/displays/draft")
async def displays_draft(req: Request) -> JSONResponse:
    rid, started = rc.new_request_id(), time.monotonic()
    body, err = await rc.read_body(req, request_id=rid)
    if err:
        return err
    try:
        draft_display, PremhistRest, allowed, _preview = rc.load_engine("displays")
    except Exception as e:                               # noqa: BLE001
        return rc.engine_unavailable("display authoring", e, request_id=rid)
    args, err = _common(body, allowed, rid)
    if err:
        return err
    request, premhist_url, user, draft, locked, footprints = args
    result = await draft_display(request, ground=PremhistRest(premhist_url, user),
                                 current=draft, locked_ids=locked, footprints=footprints)
    return rc.answer(result, request_id=rid, started=started)


@router.post("/api/displays/grounding")
async def displays_grounding(req: Request) -> JSONResponse:
    """What the model would be shown for this display request, without asking it: the system
    prompt, the grounding (tags, elements, templates found for the request's nouns), the compact
    view of the current display on a follow-up, and the size against the prompt budget."""
    rid, started = rc.new_request_id(), time.monotonic()
    body, err = await rc.read_body(req, request_id=rid)
    if err:
        return err
    try:
        _draft, PremhistRest, allowed, preview = rc.load_engine("displays")
    except Exception as e:                               # noqa: BLE001
        return rc.engine_unavailable("display authoring", e, request_id=rid)
    args, err = _common(body, allowed, rid)
    if err:
        return err
    request, premhist_url, user, draft, locked, footprints = args
    result = await preview(request, ground=PremhistRest(premhist_url, user),
                           current=draft, locked_ids=locked, footprints=footprints)
    return rc.answer(result, request_id=rid, started=started)
