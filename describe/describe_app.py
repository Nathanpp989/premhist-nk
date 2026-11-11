"""describe_app.py - the PremHist QA Describe service (calc and display Describe), no Command Center.

The slim app proven by supporting/manuals/capture_env.js (ensureAdapter): it mounts ONLY the two
UI3 authoring routes, POST /api/calcs/draft and POST /api/displays/draft, over the orchestrator
authoring modules, which talk to local Ollama and back to PremHist with the standard library.
No Postgres, no Docker, no Admin, no engine. ph-server forwards Describe here through
--authoring-url / --calc-authoring-url (start-premhist-qa.ps1 sets both).

The model is the one in ORCH_SETTINGS_FILE (the QA-owned orchestrator_settings.json the QA scripts
write), so an Admin-written ~/.orchestrator_settings.json can never override the tester's choice.

GET /health answers the model and the Ollama URL this process actually uses, AND whether each
authoring engine imports. That last part matters: routes_calcs and routes_displays import the
orchestrator package inside the request handler, so a broken or half-copied module is not noticed
until someone types into a Describe box - the service starts cleanly, nothing appears in
describe.err.log, /health used to answer {"ok": true}, and Start PremHist printed "healthy". The
only sign was a 503 "calc authoring is not available here: ..." on the first real Describe. The
import is now reported here, where the start script can say so.

Run: python -m uvicorn describe_app:app --app-dir <this folder> --host 127.0.0.1 --port 4060
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fastapi import FastAPI  # noqa: E402

import routes_calcs  # noqa: E402
import routes_displays  # noqa: E402

app = FastAPI(title="PremHist QA Describe")
app.include_router(routes_calcs.router)
app.include_router(routes_displays.router)

# POST /api/describe/selftest. Mounted defensively: this process must still start and still draft
# if the selftest module is missing or broken, because a diagnostic that can stop the service it
# diagnoses is worse than no diagnostic.
_SELFTEST: object = "not installed"
try:
    import routes_selftest  # noqa: E402

    app.include_router(routes_selftest.router)
    _SELFTEST = True
except Exception as e:                                       # noqa: BLE001
    _SELFTEST = f"{type(e).__name__}: {e}"

_STARTED = time.monotonic()
_START_EPOCH = time.time()

# The startup index warm (see _warm below). The plant index cache made a repeat Describe fast and
# left the FIRST one after every restart paying the full ~52 round trips to PremHist. A warm fixes
# that, but it needs a PremHist URL, and this service is never configured with one: `premhist_url`
# arrives in each request body. So the warm runs only when told where to look.
WARM_URL_ENV = "PREMALYTICS_PREMHIST_URL"
WARM_USER_ENV = "PREMALYTICS_PREMHIST_USER"
_warm_state: dict = {"state": "off",
                     "why": f"set {WARM_URL_ENV} (and {WARM_USER_ENV}) to warm the index at startup"}

# The module each Describe box actually needs, by the name a person would use for it.
_ENGINES = {"calcs": "orchestrator.calc_authoring", "displays": "orchestrator.display_authoring"}


def _engine_state() -> dict:
    """{"calcs": True | "<error>", "displays": ...} - the same import the route handlers do, so
    this reports exactly what a real Describe would hit. A successful import is cached by Python,
    so repeated health checks cost nothing."""
    out: dict = {}
    for name, module in _ENGINES.items():
        try:
            importlib.import_module(module)
            out[name] = True
        except Exception as e:                           # noqa: BLE001 - reported, never raised
            out[name] = f"{type(e).__name__}: {e}"
    return out


def _version() -> str:
    """The first line of VERSION.txt beside the install, which is what a bug report quotes."""
    for rel in ("..", "../.."):
        p = os.path.normpath(os.path.join(_HERE, rel, "VERSION.txt"))
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                line = f.readline().strip()
            if line:
                return line
        except OSError:
            continue
    return "unknown"


def _index_state() -> dict:
    """The plant index cache, so a reader can tell whether the NEXT Describe will be fast.

    /health used to say nothing about it, which made the cache invisible: the difference between a
    2-second Describe and a 12-second one was unobservable from outside the process."""
    out: dict = {"warm": dict(_warm_state)}
    try:
        from orchestrator import calc_scope as cs
    except Exception as e:                                   # noqa: BLE001
        out["error"] = f"calc_scope did not import: {type(e).__name__}: {e}"
        return out
    if not hasattr(cs, "index_cache_stats"):
        out["cached"] = False
        out["note"] = "this install predates the plant index cache"
        return out
    try:
        out["cached"] = True
        out.update(cs.index_cache_stats())
    except Exception as e:                                   # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


async def _warm() -> None:
    """Build the plant index once at startup, in the background. Never raises, never blocks.

    Two things are deliberate. It is a task, not an awaited call, so uvicorn finishes starting and
    /health answers while this runs - a warm that delayed startup would trade one wait for a worse
    one. And the cache is keyed by (url, user, cap), so this only speeds up requests from the SAME
    user: `PREMALYTICS_PREMHIST_USER` should be the tester's own user, and /health reports which
    user was warmed so a mismatch is visible rather than mysterious."""
    url = (os.environ.get(WARM_URL_ENV) or "").strip()
    user = (os.environ.get(WARM_USER_ENV) or "qa").strip() or "qa"
    if not url:
        return
    _warm_state.clear()
    _warm_state.update({"state": "running", "url": url, "user": user})
    t0 = time.monotonic()
    try:
        from orchestrator import calc_scope as cs
        from orchestrator.premhist_rest import PremhistRest, allowed
        if not allowed(url):
            _warm_state.update({"state": "refused", "error": f"{WARM_URL_ENV} is not loopback or "
                                                             f"allow-listed: {url!r}"})
            return
        if not hasattr(cs, "build_index"):
            _warm_state.update({"state": "failed", "error": "calc_scope has no build_index"})
            return
        ground = PremhistRest(url, user)
        # Reachability is probed first, with one cheap read, because `build_index` is best-effort:
        # it answers None on failure and says nothing about why. Without this probe an unreachable
        # PremHist and a historian with no asset hierarchy were reported identically - which they
        # are not, and the first is the one a tester can fix in a second.
        try:
            await asyncio.to_thread(ground.templates)
        except Exception as e:                               # noqa: BLE001
            _warm_state.update({"state": "unreachable",
                                "seconds": round(time.monotonic() - t0, 2),
                                "error": f"{type(e).__name__}: {e}",
                                "why": f"nothing answered at {url} - check the port, and that "
                                       f"ph-server is running"})
            return
        idx = await cs.build_index(ground)
        if idx is None:
            _warm_state.update({"state": "failed", "seconds": round(time.monotonic() - t0, 2),
                                "why": "PremHist answered, but the plant index could not be built"})
            return
        els = len(getattr(idx, "elements", {}) or {})
        tmpl = len(getattr(idx, "templates", []) or [])
        _warm_state.update({
            # An index that built but holds nothing is not a healthy warm either, and reporting it
            # as plain "ok" hides the usual cause: this is a PremHist, but not the one with the
            # plant on it, or it has no asset hierarchy yet.
            "state": "ok" if (els or tmpl) else "empty",
            "seconds": round(time.monotonic() - t0, 2),
            "elements": els,
            "templates": tmpl,
            "truncated": bool(getattr(idx, "truncated", False)),
        })
        if not (els or tmpl):
            _warm_state["why"] = ("the index built but is empty: this PremHist answered and has "
                                  "no asset hierarchy to read")
    except Exception as e:                                   # noqa: BLE001 - reported, never raised
        _warm_state.update({"state": "failed", "seconds": round(time.monotonic() - t0, 2),
                            "error": f"{type(e).__name__}: {e}"})


@app.on_event("startup")
async def _start_warm() -> None:
    # Fire and forget. If creating the task fails for any reason, the service starts exactly as it
    # did before and the first Describe simply pays for its own index, as it always has.
    try:
        asyncio.get_event_loop().create_task(_warm())
    except Exception as e:                                   # noqa: BLE001
        _warm_state.update({"state": "failed", "error": f"could not start: {type(e).__name__}: {e}"})


@app.get("/health")
def health() -> dict:
    from orchestrator.settings import DEFAULT_SETTINGS_FILE, get_settings

    s = get_settings()
    engines = _engine_state()
    # `ok` now means what a reader assumes it means: this service can actually draft. The HTTP
    # status stays 200 either way, so Start PremHist's gate (Test-QaHttp, a 200 check) is
    # unchanged and the rest of PremHist still comes up when only Describe is broken.
    return {"ok": all(v is True for v in engines.values()),
            "engines": engines,
            "model": s.ollama_model, "ollama": s.ollama_host,
            "settings_file": DEFAULT_SETTINGS_FILE,
            # Added so one GET answers "what is running, for how long, and will the next Describe
            # be fast?" - the three things a tester and the status table both want.
            "version": _version(),
            "uptime_s": round(time.monotonic() - _STARTED, 1),
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_START_EPOCH)),
            "selftest": _SELFTEST,
            "index": _index_state()}
