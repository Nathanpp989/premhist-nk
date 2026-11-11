"""Seed the QA examples into a running PremHist QA install, through the REST API only.

Run by start-premhist-qa.ps1 on every start, once the OPC UA collector is healthy. Each step
adds only what is missing, so a tester's own changes are never touched:

1. the asset hierarchy: seed_assets.py (mcp\\) over dataset PremPlantUA, names `PremPlant.SIM-...`
   (Pump Station with ten pumps, Tank Farm with 30 tanks, Electrical; the "Pump Run State" names);
2. the example displays (examples\\displays.json): each created when no display has its name,
   archived ones included (a display a tester archived stays archived);
3. the example calcs (examples\\calcs.json, the calc export format, scopes by name): imported as
   drafts when no calc has the name; the ones in `qa_approve` that THIS run created are then
   validated, dry-run and approved (the normal approval path), so they produce values from the
   start. The rest stay drafts for testers to approve themselves.

    python seed_examples.py --url http://127.0.0.1:4050             exit 2 = seeded, some tags not yet there
    python seed_examples.py --url http://127.0.0.1:4050 --late 900  wait up to 900 s for those, then bind them

On a first start some simulator tags (report-on-change temperatures) send their first value only
minutes later, so their attributes cannot be bound yet. Start PremHist then runs a hidden `--late`
pass that binds each one once it exists; the approved calcs activate those instances by
themselves (an instance whose input tag gets bound is activated and back-computed, API.md §11).

Standard library only. Exit 0 = seeded or already there; 1 = something failed (said on stderr).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "mcp"))
import seed_assets  # noqa: E402  (mcp\seed_assets.py, shipped beside the MCP adapter)

USER = "qa-examples"
DATASET = "PremPlantUA"
PREFIX = "PremPlant."

# What this run actually did. start-premhist-qa.ps1 printed a HARDCODED "4 displays ... 3 approved,
# 1 draft" whenever seeding exited 0 or 2, so a run that created nothing, or created a display
# with problems, or failed to approve a calc, still told the tester the full set was there. The
# counts live here and go out as the one EXAMPLES line the start script echoes verbatim.
COUNTS: Dict[str, int] = {}


class Api(seed_assets.Api):
    def call(self, method: str, path: str, body: Any = None, ok: tuple = (200, 201)) -> Any:
        import urllib.error
        import urllib.request
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "X-PH-User": USER})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:   # noqa: S310 - a local address
                status, text = r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode("utf-8", "replace")
        out = json.loads(text) if text else {}
        if status not in ok:
            raise RuntimeError(f"{method} {path} -> {status}: {text[:400]}")
        return out


def load(name: str) -> Dict[str, Any]:
    with open(os.path.join(HERE, "examples", name), encoding="utf-8") as f:
        return json.load(f)


def plant_names() -> List[str]:
    """Every ph-sim name the hierarchy binds (as seed_assets.seed_plant lists them)."""
    pumps = range(1, 11)
    return sorted({f"SIM-FIC-3{n:02d}.PV" for n in pumps} | {f"SIM-FIC-3{n:02d}.SP" for n in pumps}
                  | {f"SIM-TI-4{n:02d}.PV" for n in range(1, 21)} | {f"SIM-PMP-5{n:02d}.STATE" for n in pumps}
                  | {f"SIM-VIB-10{n}.PV" for n in range(1, 5)} | {f"SIM-LI-3{n:02d}.PV" for n in range(1, 31)}
                  | {f"SIM-{f}.I" for f in ("MCC-201", "MCC-202", "PMU-201", "PMU-202")})


def missing_names(api: Api) -> List[str]:
    names = plant_names()
    return [n for n, p in zip(names, seed_assets.resolve(api, DATASET, names)) if not p]


def wait_for_points(api: Api, timeout_s: float = 90.0) -> bool:
    """The collector creates the points on its first writes; wait for the ones the tree's first
    pump binds and the last pump and tank (the rest may come later: see --late)."""
    first = [n for _, n, _ in seed_assets.BINDINGS] + ["SIM-PMP-510.STATE", "SIM-LI-330.PV"]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if all(seed_assets.resolve(api, DATASET, first)):
                return True
        except RuntimeError as e:   # unknown_dataset: on a first start the collector has not written yet
            if "unknown_dataset" not in str(e):
                raise
        time.sleep(2)
    return False


def name_key(name: str) -> str:
    """Names compare as PremHist compares them, near enough for a QA seed: case and spacing."""
    return " ".join(name.split()).casefold()


def seed_displays(api: Api) -> int:
    made = 0
    for doc in load("displays.json")["displays"]:
        both = api.call("GET", "/api/v1/displays?" + urllib.parse.urlencode({"q": doc["name"], "limit": 1000}))
        both["displays"] += api.call("GET", "/api/v1/displays?" + urllib.parse.urlencode(
            {"q": doc["name"], "archived": "true", "limit": 1000}))["displays"]
        key = name_key(doc["name"])
        if any(name_key(d["name"]) == key for d in both["displays"]):
            continue
        r = api.call("POST", "/api/v1/displays", doc)
        made += 1
        if r.get("problems"):
            print(f"display {doc['name']!r}: problems {r['problems']}", file=sys.stderr)
    print(f"displays: {made} created")
    return made


def seed_calcs(api: Api) -> int:
    doc = load("calcs.json")
    # name_key, as everywhere else: compared raw, a stray double space in calcs.json silently
    # skipped a calc's approval and nothing said so.
    approve = {name_key(n) for n in doc.pop("qa_approve", [])}
    # The limit matters: without it this takes the server's default page, and on an install where
    # a tester has authored enough calcs to page, the names past the first page are not in `have`
    # and the examples are imported again on the next start (the opposite of "only what is
    # missing"). seed_displays already passes a limit for the same reason.
    have = {name_key(c["name"]) for c in api.call(
        "GET", "/api/v1/calcs?" + urllib.parse.urlencode({"limit": 1000}))["calcs"]}
    doc["calcs"] = [c for c in doc["calcs"] if name_key(c["name"]) not in have]
    if not doc["calcs"]:
        print("calcs: 0 created")
        COUNTS["calcs_created"] = 0
        COUNTS["calcs_approved"] = 0
        return 0
    res = api.call("POST", "/api/v1/calcs/import?mode=drafts", doc)["calcs"]
    created = [r for r in res if r.get("created")]
    for r in res:
        if not r.get("created"):
            print(f"calc {r['name']!r} not imported: {r.get('problems')}", file=sys.stderr)
    approved = 0
    COUNTS["calcs_created"] = len(created)
    COUNTS["calcs_approved"] = 0
    for r in created:
        if name_key(r["name"]) not in approve:
            continue
        cid = r["id"]
        calc = api.call("GET", f"/api/v1/calcs/{cid}")
        document = (calc.get("draft") or {}).get("document")
        if not document:
            print(f"calc {r['name']!r}: no draft to approve", file=sys.stderr)
            return 1
        v = api.call("POST", "/api/v1/calcs/validate", {"document": document, "calc": cid})
        errors = [p for p in v["problems"] if p.get("severity") == "error"]
        if errors or not v.get("hash"):
            print(f"calc {r['name']!r} does not validate: {errors}", file=sys.stderr)
            return 1
        dry = api.call("POST", "/api/v1/calcs/dry_run",
                       {"document": document, "calc": cid, "from": "-15m", "to": "now"})
        if not dry.get("dry_run_id"):
            print(f"calc {r['name']!r}: the dry run gave no id: {dry.get('problems')}", file=sys.stderr)
            return 1
        api.call("POST", f"/api/v1/calcs/{cid}/approve", {"hash": dry["hash"], "dry_run_id": dry["dry_run_id"]})
        approved += 1
        COUNTS["calcs_approved"] = approved
    print(f"calcs: {len(created)} created as drafts, {approved} approved")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:4050")
    ap.add_argument("--late", type=float, default=0.0,
                    help="wait up to this many seconds for tags not there yet, then bind them")
    a = ap.parse_args()
    api = Api(a.url)
    seed_assets.PREFIX = PREFIX
    try:
        if a.late > 0:
            deadline = time.monotonic() + a.late
            while missing_names(api) and time.monotonic() < deadline:
                time.sleep(20)
            left = missing_names(api)
            # seed_assets.seed_plant reads /Pump Station with no 404 tolerance, so a missing tree
            # is a bare RuntimeError with a percent-encoded URL in it - unreadable in
            # seedlate.err.log, and it means something a tester can act on: the FIRST seed never
            # got as far as creating the tree, so there is nothing for this pass to add to.
            root = api.call("GET", "/api/v1/assets/path?"
                            + urllib.parse.urlencode({"path": "/" + seed_assets.ROOT_NAME}),
                            ok=(200, 404))
            if root.get("ok") is False:
                print(f"/{seed_assets.ROOT_NAME} does not exist, so there is no hierarchy to bind "
                      f"late tags into - the first seed did not get that far. Run a full seed "
                      f"(start PremHist again, or python seed_examples.py).", file=sys.stderr)
                return 1
            # seed_plant's result was discarded here, so a late bind that FAILED still exited 0,
            # and a deadline that expired with tags still missing was never reported at all - the
            # hierarchy stayed permanently incomplete and the last thing the tester saw was the
            # start script's "they are added as they appear (usually within a few minutes)".
            rc = 1 if seed_assets.seed_plant(api, DATASET) else 0
            # seed_states as well, not seed_plant alone. seed_assets.seed_states binds the "Pump
            # Run State" set only to the SIM-PMP-5xx.STATE points that EXIST when it runs, so a
            # pump whose state tag reported late was skipped on the first seed and never revisited:
            # this pass bound its Run State attribute (the value worked) while the digital state
            # set stayed unbound, so the viewer showed the raw code 0..3 instead of
            # Stopped / Starting / Running / Tripped. Reproduced against the real seed_assets.py:
            # 9 of 10 pumps bound before, 10 of 10 after. Safe to repeat - it creates the set only
            # when absent and binds only points not already bound, so a second --late pass adds
            # nothing.
            if seed_assets.seed_states(api, DATASET):
                rc = 1
            if left:
                shown = ", ".join(left[:8]) + (" ..." if len(left) > 8 else "")
                print(f"gave up after {a.late:.0f} s with {len(left)} tag(s) that never sent a value: "
                      f"{shown} - their attributes are NOT bound", file=sys.stderr)
            return rc
        if not wait_for_points(api):
            print("the PremPlantUA tags did not appear within 90 s (is the collector writing?)", file=sys.stderr)
            return 1
        for step in (seed_assets.seed_tree, seed_assets.seed_plant, seed_assets.seed_states):
            if step(api, DATASET):
                return 1
        COUNTS["displays"] = seed_displays(api)
        rc = seed_calcs(api)
        late = missing_names(api)
        COUNTS["tags_not_yet_reporting"] = len(late)
        # The one line the start script shows the tester: what this run did, not what a full run
        # would have done. Printed even when a step failed, so the table can never overstate it.
        print("EXAMPLES " + ", ".join([
            f"{COUNTS.get('displays', 0)} display(s) added",
            f"{COUNTS.get('calcs_created', 0)} calc(s) added",
            f"{COUNTS.get('calcs_approved', 0)} approved",
        ]) + (f"; {len(late)} tag(s) not reporting yet" if late else ""))
        if rc == 0 and late:
            return 2   # seeded; some tags have not sent a first value yet (start runs --late)
        return rc
    except Exception as e:  # noqa: BLE001 - one plain line for the start script's log
        print(f"seeding failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
