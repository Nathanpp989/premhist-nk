"""
routes_selftest.py - one call that proves whether Describe can actually work right now.

    POST /api/describe/selftest   {"premhist_url": "http://127.0.0.1:4050", "user": "qa"}

Diagnosing a broken Describe used to mean the `POST {}` probe: a 400 meant the engine imported, a
503 meant it did not. That is a trick, not a diagnostic - it answers one bit, and only about the
import. Everything else (can this process reach PremHist? did the plant index build? is the
configured model actually pulled?) was unanswerable without typing a real request into the viewer
and waiting out a model call.

This runs the whole path up to, but not including, the model:

    engines      both authoring modules import (what /health reports, repeated here so one call
                 answers everything)
    premhist     this process can reach that PremHist, and how long it took
    calcs        the calc engine's own grounding for a canned request - scope, plan, prompt size
    displays     the display engine's own grounding - tags, elements, templates found
    index        the plant index cache: entries, hits, misses, ages, TTL
    ollama       the configured host answers, and whether the configured model is among its
                 pulled models - the one way to catch "Switch Describe model" pointing at a model
                 nobody pulled

**No model call is made**, so it costs seconds rather than minutes, and **nothing is written**:
no calc, no display, no outcome-log line. Every step is independent - PremHist being down does not
stop the Ollama check, so one call still tells you everything that is wrong rather than the first
thing.

`ok` is true only when every step that must work does. `ollama.model_pulled` being false does not
clear `ok` on its own: the model list is advisory (a custom or aliased tag may not appear), so it
is reported as a warning rather than treated as a verdict.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import routes_common as rc

router = APIRouter()

_FIELDS = frozenset({"premhist_url", "user"})

# Canned requests. Deliberately ordinary: they must ground on any plant without naming anything
# that only the QA example hierarchy has, so the result reflects the install and not the fixture.
CALC_PROBE = "the average level over the last day"
DISPLAY_PROBE = "a display of the plant with a trend"

OLLAMA_TIMEOUT_S = 3.0


def _ms(t0: float) -> float:
    return round((time.monotonic() - t0) * 1000, 1)


async def _step(fn) -> Dict[str, Any]:
    """Run one check, timing it, turning any failure into a reported step rather than a 500."""
    t0 = time.monotonic()
    try:
        out = await fn()
        out.setdefault("ok", True)
    except Exception as e:                                   # noqa: BLE001 - reported, never raised
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    out["ms"] = _ms(t0)
    return out


def _ollama_tags(host: str, model: str) -> Dict[str, Any]:
    """`GET {host}/api/tags` - Ollama's model list. A plain GET: no inference, no tokens.

    The host comes from the service's own settings file, not from the request body, so it is
    configuration rather than caller input; it is still read with a short timeout and a capped
    read so a wedged Ollama cannot hold this call open."""
    url = host.rstrip("/") + "/api/tags"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=OLLAMA_TIMEOUT_S) as r:    # noqa: S310 - configured host
        raw = r.read(1024 * 1024)
    data = json.loads(raw.decode("utf-8", "replace") or "{}")
    names = [str(m.get("name")) for m in (data.get("models") or []) if isinstance(m, dict)]
    base = model.split(":")[0]
    return {"host": host, "models": len(names),
            "model": model,
            # An exact tag match, else the same base name under another tag, else not found.
            "model_pulled": (model in names) or any(n.split(":")[0] == base for n in names),
            "exact_tag": model in names,
            "sample": sorted(names)[:10]}


def _index_state() -> Dict[str, Any]:
    """The plant index cache, when this install has it (it arrived with the cache patch)."""
    try:
        from orchestrator import calc_scope as cs
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "error": f"calc_scope did not import: {type(e).__name__}: {e}"}
    if not hasattr(cs, "index_cache_stats"):
        return {"ok": True, "cached": False,
                "note": "this install predates the plant index cache"}
    try:
        stats = cs.index_cache_stats()
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "cached": True, **stats}


@router.post("/api/describe/selftest")
async def describe_selftest(req: Request) -> JSONResponse:
    rid, started = rc.new_request_id(), time.monotonic()
    body, err = await rc.read_body(req, request_id=rid)
    if err:
        return err
    err = rc.unknown_fields(body, _FIELDS, request_id=rid)
    if err:
        return err

    steps: Dict[str, Any] = {}

    # 1. The engines, by the same import the route handlers do.
    engines: Dict[str, Any] = {}
    for name in ("calcs", "displays"):
        try:
            rc.load_engine(name)
            engines[name] = True
        except Exception as e:                               # noqa: BLE001
            engines[name] = f"{type(e).__name__}: {e}"
    steps["engines"] = {"ok": all(v is True for v in engines.values()), **engines}

    # The URL is checked with the engine's own allow-list, so selftest can never be used to make
    # this process talk to somewhere a draft could not.
    premhist_url = body.get("premhist_url")
    user = body.get("user") if body.get("user") not in (None, "") else "anonymous"
    Rest = Allowed = preview_calcs = preview_displays = None
    if steps["engines"]["ok"]:
        _dc, Rest, Allowed, preview_calcs = rc.load_engine("calcs")
        _dd, _r2, _a2, preview_displays = rc.load_engine("displays")
    if Allowed is None:
        return rc.answer({"ok": False, "code": "bad_request",
                          "error": "the authoring engines do not import here, so nothing else "
                                   "could be checked",
                          "steps": steps}, request_id=rid, started=started)
    if not isinstance(premhist_url, str) or not Allowed(premhist_url):
        return rc.bad("premhist_url is not allowed (loopback or PREM_DISPLAYS_PREMHIST_ALLOW only)",
                      request_id=rid)
    if not isinstance(user, str) or not rc.USER_RE.match(user):
        return rc.bad("user must be 1-64 visible ASCII characters", request_id=rid)

    ground = Rest(premhist_url, user)

    # 2. Can this process reach that PremHist at all? The cheapest read that proves it.
    async def premhist_check() -> Dict[str, Any]:
        import asyncio
        templates = await asyncio.to_thread(ground.templates)
        out: Dict[str, Any] = {"url": premhist_url, "user": user,
                               "templates": len(list(templates or []))}
        try:
            # `sites()` returns {sites: [...], assignments: [...]} - NOT a site object. Reading it
            # as one gave a `site` of null against every real PremHist, which looked like a
            # historian with no sites configured rather than a bug in this check.
            sites = await asyncio.to_thread(ground.sites)
            rows = list((sites or {}).get("sites") or []) if isinstance(sites, dict) else []
            out["sites"] = len(rows)
            first = rows[0] if rows and isinstance(rows[0], dict) else None
            if first:
                out["site"] = first.get("name") or first.get("id")
        except Exception as e:                               # noqa: BLE001
            out["sites_warning"] = f"{type(e).__name__}: {e}"
        return out

    steps["premhist"] = await _step(premhist_check)

    # 3 and 4. Each engine's own grounding, which stops before the model.
    if steps["premhist"]["ok"]:
        async def calcs_check() -> Dict[str, Any]:
            r = await preview_calcs(CALC_PROBE, ground=ground)
            idx = r.get("index") if isinstance(r.get("index"), dict) else {}
            return {"request": CALC_PROBE, "grounded": bool(r.get("ok")),
                    "scope": r.get("scope_label") or r.get("scope"),
                    "plan": (r.get("plan") or {}).get("kind") if isinstance(r.get("plan"), dict) else r.get("plan"),
                    "elements": idx.get("elements"), "templates": idx.get("templates"),
                    "truncated": idx.get("truncated"),
                    "from_cache": idx.get("from_cache"), "index_age_s": idx.get("age_s"),
                    "prompt_chars": r.get("chars"), "est_tokens": r.get("est_tokens"),
                    "too_broad": r.get("too_broad"),
                    "would_refuse": bool(r.get("would_refuse")),
                    "reason": r.get("error") or r.get("reason")}

        async def displays_check() -> Dict[str, Any]:
            r = await preview_displays(DISPLAY_PROBE, ground=ground)
            def n(k):
                v = r.get(k)
                return len(v) if isinstance(v, (list, tuple)) else None
            return {"request": DISPLAY_PROBE, "grounded": bool(r.get("ok")),
                    "tags": n("tags"), "elements": n("elements"), "templates": n("templates"),
                    "unmatched": r.get("unmatched") or [],
                    "prompt_chars": r.get("chars"), "est_tokens": r.get("est_tokens"),
                    "too_broad": r.get("too_broad"),
                    "reason": r.get("error") or r.get("reason")}

        steps["calcs"] = await _step(calcs_check)
        steps["displays"] = await _step(displays_check)
    else:
        skipped = {"ok": False, "skipped": "PremHist could not be reached", "ms": 0.0}
        steps["calcs"] = dict(skipped)
        steps["displays"] = dict(skipped)

    # 5. The index cache, read after the grounding above so the numbers mean something.
    steps["index"] = _index_state()

    # 6. Ollama: reachable, and is the configured model actually pulled?
    async def ollama_check() -> Dict[str, Any]:
        import asyncio
        from orchestrator.settings import get_settings
        s = get_settings()
        return await asyncio.to_thread(_ollama_tags, s.ollama_host, s.ollama_model)

    steps["ollama"] = await _step(ollama_check)

    must_pass = ("engines", "premhist", "calcs", "displays", "ollama")
    ok = all(bool(steps.get(k, {}).get("ok")) for k in must_pass)
    warnings = []
    if steps["ollama"].get("ok") and steps["ollama"].get("model_pulled") is False:
        warnings.append(f"the configured model {steps['ollama'].get('model')!r} is not among "
                        f"Ollama's pulled models - use Switch Describe model, or pull it")
    if steps["ollama"].get("ok") and steps["ollama"].get("model_pulled") \
            and not steps["ollama"].get("exact_tag"):
        warnings.append("the configured model matches only by base name, not by exact tag")
    if steps["calcs"].get("truncated"):
        warnings.append("the plant index was truncated: this plant is larger than the index cap, "
                        "so Describe reasons about part of it")

    return rc.answer({"ok": ok, "kind": "selftest", "steps": steps, "warnings": warnings,
                      "model_call_made": False, "wrote_anything": False},
                     request_id=rid, started=started)
