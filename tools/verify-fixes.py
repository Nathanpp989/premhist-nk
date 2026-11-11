"""verify-fixes.py - check that the patched PremHist QA files are the ones actually installed.

Run it with the bundled Python, from the install folder:

    cd %LOCALAPPDATA%\\PremHist-QA
    python-describe\\python.exe tools\\verify-fixes.py

or point it somewhere else:

    python-describe\\python.exe tools\\verify-fixes.py "C:\\path\\to\\PremHist-QA"

It imports the INSTALLED orchestrator modules and exercises each fix, and text-checks the
PowerShell and seed changes. Read-only: it starts nothing, changes nothing, needs no Ollama and
no running PremHist. Exit code 0 = everything landed, 1 = something did not.
"""
from __future__ import annotations

import os
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "PremHist-QA")
ROOT = os.path.abspath(ROOT)

PASS, FAIL = [], []


def check(name, thunk, want):
    """`thunk` is evaluated here, not at the call site, so a half-finished transfer reports every
    missing piece instead of dying on the first AttributeError."""
    try:
        got = thunk() if callable(thunk) else thunk
    except Exception as e:                               # noqa: BLE001
        FAIL.append(name)
        print(f"  [FAIL] {name}\n           raised {type(e).__name__}: {e}")
        return
    if got == want:
        PASS.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}\n           got  {got!r}\n           want {want!r}")


def check_text(name, path, needle, present=True):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            body = f.read()
    except OSError as e:
        FAIL.append(name)
        print(f"  [FAIL] {name}: cannot read {path} ({e})")
        return
    hit = needle in body
    if hit == present:
        PASS.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}: {'missing' if present else 'still present'} in {os.path.basename(path)}")


print("=" * 72)
print("PremHist QA - patched-file verification")
print(f"install root : {ROOT}")
describe = os.path.join(ROOT, "describe")
orch = os.path.join(describe, "orchestrator")
print(f"describe dir : {describe}  {'(found)' if os.path.isdir(describe) else '(NOT FOUND)'}")
print(f"orchestrator : {orch}  {'(found)' if os.path.isdir(orch) else '(NOT FOUND)'}")
if not os.path.isdir(orch):
    print()
    print("  The orchestrator package is not where this script expected it.")
    print("  Find it with, in PowerShell:")
    print(f'      Get-ChildItem -Recurse -Filter calc_scope.py "{ROOT}" | Select FullName')
    print("  then pass the folder that CONTAINS 'orchestrator' as the argument.")
    sys.exit(1)
sys.path.insert(0, describe)

try:
    from orchestrator import calc_scope as cs
    from orchestrator import calc_authoring as ca
    from orchestrator import display_pipes as dp
    from orchestrator import display_authoring as da
    from orchestrator import llm_runtime as lr
except Exception as e:                                   # noqa: BLE001
    print(f"\n  cannot import the orchestrator modules: {type(e).__name__}: {e}")
    sys.exit(1)
print()

print("Round 1 - the three Describe fixes")
check("stem('status') is 'status', not 'statu'", lambda: cs.stem("status"), "status")
check("a 'status' request matches a Run State attribute",
      lambda: cs._attr_strength("Run State", "the pump status yesterday") > 0, True)
check("a quoted label no longer picks the line type",
      lambda: dp.parse_connects("connect Pump P-501 to Tank T-301 labeled 'Main steam'")[0]["line"],
      "process")
check("an explicit steam line still works",
      lambda: dp.parse_connects("run a steam line from the boiler to the deaerator")[0]["line"], "steam")
check("3-sigma / 15 minutes no longer license chart limits",
      lambda: sorted(ca._numbers_in("control chart on Bearing Temp, 3-sigma, every 15 minutes, center 50")),
      [50.0])
check("limits the request really states are kept",
      lambda: sorted(ca._numbers_in("center 50, limits 44 to 56")), [44.0, 50.0, 56.0])

print()
print("Round 2 - budgets, truncation, retry cost")
check("llm_runtime exports the retry cost", lambda: hasattr(lr, "max_call_seconds"), True)
check("one round's worst case at a 120 s timeout", lambda: lr.max_call_seconds(120), 162)
check("display Describe has a time budget", lambda: hasattr(da, "TOTAL_BUDGET_S"), True)
check("calc Describe subtracts the round ceiling", lambda: hasattr(ca, "_round_ceiling_s"), True)

idx = cs.PlantIndex()
idx.truncated = True
check("a truncated index makes no absence claim",
      lambda: cs.plant_refusal(idx, "the bearing temperature of the compressors"), None)
check("the not-a-calc refusal still fires on a truncated index",
      lambda: (cs.plant_refusal(idx, "email me the daily total") or {}).get("topic"), "a message")

print()
print("Round 4 - rollup paths and bare numbers")
check("a rollup path reduces to the attribute", lambda: ca._norm_ref("c|'Bearing Temp'"), "Bearing Temp")
check("...and so does any other prefix", lambda: ca._norm_ref("p|'Bearing Temp'"), "Bearing Temp")
check("a bare number is not an element identity", lambda: cs._distinct_tokens("Unit 1 Boiler"), [])
check("a real tag still is", lambda: cs._distinct_tokens("Pump P-503"), ["P-503"])
check("draft_display has the never-raises wrapper", lambda: hasattr(da, "_draft_display"), True)

print()
print("Round 5 - the medium words")
t = cs.Template({"id": 1, "name": "Storage Tank", "attributes": [{"name": "Level", "kind": "tag"}]})
e = cs.Element({"id": 10, "name": "Tank T-301", "path": "/Tank Farm/Tank T-301", "parent": None,
                "template": 1, "template_name": "Storage Tank",
                "attributes": [{"name": "Level", "units": "%",
                                "value": {"kind": "tag",
                                          "tag": {"dataset": "PremPlantUA",
                                                  "name": "SIM-LI-301.PV", "eng_units": "%",
                                                  "point_type": "numeric"}}}]})
t.elements = [10]
idx2 = cs.PlantIndex()
idx2.templates = [t]
idx2.elements = {10: e}
idx2.points = [{"name": "SIM-LI-301.PV", "description": "tank level"}]
check("'water level' is not refused on a plant with levels",
      lambda: cs.plant_refusal(idx2, "the average water level in each tank"), None)
check("'oil temperature' is not refused either",
      lambda: cs.plant_refusal(idx2, "the oil temperature in each tank"), None)
check("a genuinely absent subject is still refused",
      lambda: (cs.plant_refusal(idx2, "the steam flow") or {}).get("topic"), "steam")

print()
print("Round 6 - the Describe service reports its own engines")
_app = None
for cand in (os.path.join(describe, "describe_app.py"),
             os.path.join(os.path.dirname(orch), "describe_app.py")):
    if os.path.isfile(cand):
        _app = cand
        break
if _app:
    check_text("describe_app reports engine imports", _app, "_engine_state")
    check_text("...and ok reflects them", _app, "all(v is True for v in engines.values())")
else:
    FAIL.append("describe_app.py found")
    print("  [FAIL] describe_app.py found: not beside the orchestrator package")

print()
print("Round 7 - the plant index cache and the one glyph catalog")
check("build_index has a TTL cache", lambda: hasattr(cs, "index_cache_stats"), True)
check("...with a documented default", lambda: cs.INDEX_TTL_S, 60.0)
check("...that can be turned off", lambda: callable(cs.invalidate_index_cache), True)
check("PORTLESS_GLYPHS lives in the catalog",
      lambda: __import__("orchestrator.display_glyphs", fromlist=["x"]).PORTLESS_GLYPHS
      is dp.PORTLESS_GLYPHS, True)
check("the catalog still holds 90 glyphs",
      lambda: len(__import__("orchestrator.display_glyphs", fromlist=["x"]).GLYPHS), 90)
check("grounding preview on both engines",
      lambda: hasattr(ca, "preview_grounding") and hasattr(da, "preview_grounding"), True)
_rc = os.path.join(describe, "routes_common.py")
check_text("routes share one error envelope", _rc, "def envelope")
check_text("...and carry a request id", _rc, "new_request_id")

print()
print("Round 8 - the display outcome log, one failure shape, the catalog audit")
check("display Describe has an outcome log",
      lambda: __import__("orchestrator.display_describe_log", fromlist=["x"]).DEFAULT_PATH.endswith(
          "display_describe_log.jsonl"), True)
check("...with the same outcome vocabulary as calcs",
      lambda: sorted({__import__("orchestrator.display_describe_log", fromlist=["x"]).outcome_of(r, f)
                      for r, f in (({"ok": True, "authoring": {"model_calls": 1}}, False),
                                   ({"ok": True, "authoring": {"model_calls": 1}}, True),
                                   ({"ok": True, "authoring": {"model_calls": 0}}, False),
                                   ({"ok": False, "code": "no_draft"}, False),
                                   ({"ok": False, "code": "bad_request"}, False),
                                   ({"ok": False, "code": "llm_unavailable"}, False))}),
      ["bad_request", "draft", "edit", "error", "no_change", "refusal"])
check("...and it is written by the engine, for every answer",
      lambda: "_dlog.record" in __import__("inspect").getsource(da.draft_display), True)

# One failure shape across both engines: the viewer needs one renderer, not two.
_SHAPE = ("ok", "code", "error", "notes", "reason", "missing", "suggestions")
_cf = lambda: ca._fail("no_draft", "x")                       # noqa: E731
_df = lambda: da._fail("no_draft", "x", [])                   # noqa: E731
check("a display failure carries reason / missing / suggestions",
      lambda: [k for k in _SHAPE if k not in _df()], [])
check("...the same keys a calc failure carries",
      lambda: [k for k in _SHAPE if k not in _cf()], [])
check("...and missing / suggestions are lists on both",
      lambda: all(isinstance(f()[k], list) for f in (_cf, _df) for k in ("missing", "suggestions")),
      True)
check("display suggestions invent nothing when there is no grounding",
      lambda: da._suggest(None), [])

# The glyph catalog's own audit, run here so catalog drift fails verification rather than
# surfacing later as an AttributeError in the middle of a Describe.
def _audit():
    import importlib.util
    p = os.path.join(ROOT, "tools", "gen_glyph_catalog.py")
    if not os.path.isfile(p):
        return ["tools/gen_glyph_catalog.py is not installed"]
    spec = importlib.util.spec_from_file_location("_gen_glyph_catalog", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                              # type: ignore[union-attr]
    return mod.audit()


check("the glyph catalog audits clean", _audit, [])

print()
print("Round 9 - the outcome logs are bounded, and a bad request is not an error")
_cdl = __import__("orchestrator.calc_describe_log", fromlist=["x"])
_ddl = __import__("orchestrator.display_describe_log", fromlist=["x"])
check("an empty request is not logged as an engine error",
      lambda: _cdl.outcome_of({"ok": False, "code": "bad_request"}), "bad_request")
check("...and a dead model still is",
      lambda: _cdl.outcome_of({"ok": False, "code": "llm_unavailable"}), "error")
check("...and both engines agree on it",
      lambda: _cdl.outcome_of({"ok": False, "code": "bad_request"})
      == _ddl.outcome_of({"ok": False, "code": "bad_request"}), True)
check("the shared log writer is installed",
      lambda: __import__("orchestrator.describe_log_io", fromlist=["x"]).max_bytes() > 0, True)
check("a 200k-char calc body is capped on the line",
      lambda: len(_cdl.entry("q", None, {"ok": True, "intent": "i",
                                         "document": {"body": "x" * 200_000}}, 1.0)["body"]) < 21_000, True)
check("notes are capped on both logs",
      lambda: all(len(e["notes"]) <= 21 for e in (
          _cdl.entry("q", None, {"ok": True, "notes": ["n" * 9_000] * 50}, 1.0),
          _ddl.entry("q", {}, {"ok": True, "display": {"symbols": []}, "notes": ["n" * 9_000] * 50,
                               "authoring": {"model_calls": 1}}, 1.0))), True)
# A cap must bound a field, never re-type it: `choices` and `scope_proposal` are read as objects
# by anything that reads the calc log, and an earlier version of the cap turned them into JSON
# strings. Caught by running the original and the patched log side by side.
check("capping keeps a small object an object",
      lambda: type(_cdl.entry("q", None, {"ok": False, "code": "scope_proposal",
                                          "scope_proposal": {"why": "w"}}, 1.0)["scope_proposal"]).__name__,
      "dict")
check("...and a small choice list a list of objects",
      lambda: _cdl.entry("q", None, {"ok": False, "code": "choose_scope",
                                     "choices": [{"a": 1}]}, 1.0)["choices"], [{"a": 1}])
check("...while an oversized one is still capped",
      lambda: isinstance(_cdl.entry("q", None, {"ok": False, "code": "scope_proposal",
                                                "scope_proposal": {"why": "w" * 90000}},
                                    1.0)["scope_proposal"], str), True)
check("no line can exceed the backstop",
      lambda: len(__import__("orchestrator.describe_log_io", fromlist=["x"]).line_of(
          {"ts": "t", "outcome": "draft", "body": "y" * 300_000, "mystery": "z" * 300_000}).encode())
      <= __import__("orchestrator.describe_log_io", fromlist=["x"]).MAX_LINE_BYTES, True)

print()
print("Round 10 - the Describe outcome report")


def _report_mod():
    import importlib.util
    p = os.path.join(ROOT, "tools", "describe-report.py")
    spec = importlib.util.spec_from_file_location("_describe_report", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                              # type: ignore[union-attr]
    return mod


check("tools/describe-report.py ships",
      lambda: os.path.isfile(os.path.join(ROOT, "tools", "describe-report.py")), True)
check("...and its .cmd wrapper",
      lambda: os.path.isfile(os.path.join(ROOT, "tools", "describe-report.cmd")), True)
check("it reads rotated generations oldest first",
      lambda: [os.path.basename(p) for p in _report_mod().generations(
          os.path.join(ROOT, "logs"), "calc_describe_log.jsonl")]
      in ([], ["calc_describe_log.jsonl"], ["calc_describe_log.prev.jsonl"],
          ["calc_describe_log.prev.jsonl", "calc_describe_log.jsonl"]), True)
check("a malformed entry is never counted as an outcome",
      lambda: _report_mod().summarise("t", [{"ts": "2026-01-01T00:00:00Z"}, {"outcome": "draft"}],
                                      0, 150.0, 5)["outcomes"], {"draft": 1})
check("...and is reported instead",
      lambda: _report_mod().summarise("t", [{"ts": "2026-01-01T00:00:00Z"}], 0, 150.0, 5
                                      )["malformed_entries"], 1)
check("a missing timestamp cannot become the window start",
      lambda: _report_mod().summarise("t", [{"outcome": "draft"},
                                            {"ts": "2026-05-05T00:00:00Z", "outcome": "draft"}],
                                      0, 150.0, 5)["first"], "2026-05-05T00:00:00Z")
check("over-budget requests are counted",
      lambda: _report_mod().summarise("t", [{"ts": "t", "outcome": "draft", "seconds": 200.0},
                                            {"ts": "t", "outcome": "draft", "seconds": 10.0}],
                                      0, 150.0, 5)["seconds"]["over_budget"], 1)
check("the budget comes from the engine, not a constant",
      lambda: _report_mod().engine_budget(), da.TOTAL_BUDGET_S)

print()
print("Round 11 - the selftest route, and what /health now reports")
_st = os.path.join(describe, "routes_selftest.py")
_ap_py = os.path.join(describe, "describe_app.py")
check_text("routes_selftest.py ships", _st, "api/describe/selftest")
check_text("...and makes no model call", _st, '"model_call_made": False')
check_text("...and checks the model is actually pulled", _st, "model_pulled")
check_text("...and reads sites() as {sites: [...]}, not as a site", _st, '(sites or {}).get("sites")')
check_text("the app mounts selftest defensively", _ap_py, "import routes_selftest")
check_text("...so a missing selftest cannot stop the service", _ap_py, '_SELFTEST = f"')
check_text("/health reports the version and uptime", _ap_py, '"uptime_s"')
check_text("/health reports the plant index cache", _ap_py, "def _index_state")
check_text("the index is warmed at startup", _ap_py, "async def _warm")
check_text("...only where it is told to look", _ap_py, 'WARM_URL_ENV = "PREMALYTICS_PREMHIST_URL"')
check_text("...and an unreachable PremHist is not reported as an empty plant",
           _ap_py, '"state": "unreachable"')
check_text("qa-common configures the warm", os.path.join(ROOT, "qa-common.ps1"),
           "PREMALYTICS_PREMHIST_URL")
check_text("diagnose calls the selftest route",
           os.path.join(ROOT, "tools", "diagnose-describe.ps1"), "/api/describe/selftest")
check_text("...and no longer claims /health skips the engines",
           os.path.join(ROOT, "tools", "diagnose-describe.ps1"),
           "It does NOT check", present=False)
check_text("README documents the selftest", os.path.join(ROOT, "README-QA.md"),
           "/api/describe/selftest")

print()
print("The package files (text checks)")
check_text("seed: calcs query is paginated", os.path.join(ROOT, "seed_examples.py"),
           "/api/v1/calcs?")
check_text("seed: --late honours seed_plant's result", os.path.join(ROOT, "seed_examples.py"),
           "rc = 1 if seed_assets.seed_plant")
check_text("seed: reports what it actually did", os.path.join(ROOT, "seed_examples.py"),
           'print("EXAMPLES ')
check_text("seed: qa_approve compares with name_key", os.path.join(ROOT, "seed_examples.py"),
           "approve = {name_key(n) for n in")
# The late pass must bind the digital state set too, not just the tag attribute: seed_states only
# binds SIM-PMP-5xx.STATE points that existed when it ran, so a pump whose state tag reported late
# kept its raw 0..3 codes instead of Stopped / Starting / Running / Tripped.
check_text("seed: --late binds the digital state set as well",
           os.path.join(ROOT, "seed_examples.py"), "seed_assets.seed_states(api, DATASET)")
check_text("seed: --late says so plainly when there is no hierarchy yet",
           os.path.join(ROOT, "seed_examples.py"), "there is no hierarchy to bind")
check_text("start: the hardcoded examples line is gone",
           os.path.join(ROOT, "start-premhist-qa.ps1"),
           "4 displays (Displays tab)", present=False)
check_text("start: seedlate no longer blocks a restart",
           os.path.join(ROOT, "start-premhist-qa.ps1"), '-ne "seedlate"')
check_text("start: Add-Rec returns the record", os.path.join(ROOT, "start-premhist-qa.ps1"),
           "return $rec")
check_text("qa-common: the viewer URL is probed",
           os.path.join(ROOT, "qa-common.ps1"), "function Get-QaViewerUrl")
check_text("qa-common: Wait-QaReady takes a record",
           os.path.join(ROOT, "qa-common.ps1"), "procOrRec")
# Round 12: the three scripts that had never been patched.
check_text("stop: a part that did not stop keeps its record",
           os.path.join(ROOT, "stop-premhist-qa.ps1"), "did NOT stop and are still running")
check_text("stop: and the pid file is only removed when all are gone",
           os.path.join(ROOT, "stop-premhist-qa.ps1"), "if ($alive.Count -eq 0)")
check_text("switch: does not claim success with a broken engine",
           os.path.join(ROOT, "switch-describe-model.ps1"), "but an engine does not import")
check_text("switch: notices it is reading another settings file",
           os.path.join(ROOT, "switch-describe-model.ps1"), "not the $chosen just chosen")
check_text("uninstall: a locked file does not abort it halfway",
           os.path.join(ROOT, "uninstall-premhist-qa.ps1"), "could not be removed")
check_text("uninstall: and it says the install is only partly gone",
           os.path.join(ROOT, "uninstall-premhist-qa.ps1"), "PARTLY uninstalled")
check_text("setup: no false 'Nothing was changed'",
           os.path.join(ROOT, "setup-premhist-qa.ps1"), "stoppedIt")
check_text("qa-common: the display log is pointed at logs\\",
           os.path.join(ROOT, "qa-common.ps1"), "PREMALYTICS_DISPLAY_DESCRIBE_LOG")
check_text("diagnose: both Describe logs are read",
           os.path.join(ROOT, "tools", "diagnose-describe.ps1"), "display_describe_log.jsonl")
check_text("README: the display log is documented",
           os.path.join(ROOT, "README-QA.md"), "logs\\display_describe_log.jsonl")
for _ci in ("ci-glyph-catalog.sh", "ci-glyph-catalog.cmd", "ci-glyph-catalog.yml"):
    check(f"tools/{_ci} ships", (lambda p=_ci: os.path.isfile(os.path.join(ROOT, "tools", p))), True)

# The patch applier itself. It used to copy a FIXED LIST of orchestrator file names, which left
# display_glyphs.py and the three routes_*.py behind while reporting success - so a verifier run
# could fail on files that were sitting in the folder the tester had just unzipped. These two
# checks exist so that regression cannot come back unnoticed.
_ap = os.path.join(ROOT, "tools", "apply-patch.ps1")
if not os.path.isfile(_ap):
    _ap = os.path.join(ROOT, "apply-patch.ps1")
if os.path.isfile(_ap):
    check_text("apply-patch enumerates describe\\ instead of listing it", _ap,
               'Get-ChildItem $srcDescribe -File -Filter *.py')
    check_text("apply-patch names any file it cannot place", _ap, "$unplanned")
    check_text("apply-patch does not patch a backup copy by mistake", _ap, "patch-backup")
    check_text("apply-patch has a -Yes switch (no prompt needed)", _ap, '[switch]$Yes')
    check_text("...and its confirmation is not case-sensitive", _ap, "-cne \"YES\"", present=False)
else:
    print("  [skip] apply-patch.ps1 is not kept in the install (it lives in the bundle)")

print()
print("=" * 72)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("\nNot landed:")
    for n in FAIL:
        print(f"  - {n}")
    print("\nA failure here usually means that file was not copied, or was copied to the")
    print("package folder but Setup.cmd has not been run since.")
sys.exit(1 if FAIL else 0)
