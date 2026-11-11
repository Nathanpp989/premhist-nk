"""describe-report.py - what the two Describe outcome logs actually say.

The logs are the only durable record of a QA campaign: the viewer forgets a Describe as soon as the
tab closes. Until now nothing read them except `diagnose-describe.ps1`, which prints the last three
lines. This answers the questions a QA pass actually asks.

Run it with the bundled Python, from the install folder:

    cd %LOCALAPPDATA%\\PremHist-QA
    python-describe\\python.exe tools\\describe-report.py

or, from anywhere:

    tools\\describe-report.cmd --since 2026-11-01 --csv report

Options:
  --logs DIR       the logs folder          (default: <install>\\logs, found from this file)
  --since DATE     only requests on or after this UTC date (YYYY-MM-DD, or a full timestamp)
  --until DATE     only requests on or before it
  --engine WHICH   calcs | displays | both (default both)
  --top N          how many entries in each ranked list      (default 10)
  --budget S       the drafting budget, for the "over budget" count  (default: read from the
                   engines when they are importable, else 150)
  --csv DIR        also write one CSV per engine into DIR, for a spreadsheet
  --json           print the whole report as JSON instead of text
  --full           do not truncate the refusal and error lists

It reads only. It starts nothing, needs no Ollama, no running PremHist and no network, and it
never writes to the logs. Rotated generations (`*.prev.jsonl`) are included, oldest first.

A line it cannot parse is counted and skipped, not fatal: a log can be cut mid-write if the
service was killed, and one bad line should not cost you the report.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BUDGET_S = 150.0

ENGINES = (
    ("calcs", "calc_describe_log.jsonl"),
    ("displays", "display_describe_log.jsonl"),
)

# Outcomes that mean "Describe produced something", per engine. Everything else is a non-draft.
PRODUCED = {"draft", "edit"}
# Outcomes that are the tester's input, not the engine's fault - counted apart from errors.
NOT_ENGINE_FAULT = {"bad_request"}


# ── reading ───────────────────────────────────────────────────────────

def generations(logs_dir: str, name: str) -> List[str]:
    """The log and its rotated previous generation, oldest first, only those that exist."""
    stem = name[:-6] if name.endswith(".jsonl") else name
    out = []
    for cand in (stem + ".prev.jsonl", name):
        p = os.path.join(logs_dir, cand)
        if os.path.isfile(p):
            out.append(p)
    return out


def read_lines(paths: List[str]) -> Tuple[List[Dict[str, Any]], int]:
    """Every entry across the generations, plus how many lines could not be parsed."""
    rows: List[Dict[str, Any]] = []
    bad = 0
    for p in paths:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        bad += 1
                        continue
                    if isinstance(obj, dict):
                        rows.append(obj)
                    else:
                        bad += 1
        except OSError:
            continue
    return rows, bad


def in_window(row: Dict[str, Any], since: Optional[str], until: Optional[str]) -> bool:
    ts = str(row.get("ts") or "")
    if since and ts < since:
        return False
    if until and ts[:len(until)] > until:
        return False
    return True


def norm_date(v: Optional[str], end: bool = False) -> Optional[str]:
    """`2026-11-01` becomes a timestamp bound; a full timestamp is used as given."""
    if not v:
        return None
    v = v.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v + ("T99" if end else "T00:00:00Z")
    return v


# ── summarising ───────────────────────────────────────────────────────

def pct(values: List[float], q: float) -> Optional[float]:
    """The q-th percentile by nearest rank. No numpy: this ships on an embeddable Python."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[k]


def summarise(name: str, rows: List[Dict[str, Any]], bad: int, budget: float,
              top: int) -> Dict[str, Any]:
    secs = [float(r["seconds"]) for r in rows
            if isinstance(r.get("seconds"), (int, float)) and not isinstance(r.get("seconds"), bool)]
    # An entry with no `outcome` is a malformed line, not an engine state: counted and reported
    # apart, so it can never be read as something Describe did. (A log cut mid-write, or a line
    # from a future version, both land here.)
    malformed = sum(1 for r in rows if not r.get("outcome"))
    outcomes = Counter(str(r["outcome"]) for r in rows if r.get("outcome"))
    produced = sum(n for o, n in outcomes.items() if o in PRODUCED)
    errors = [r for r in rows if str(r.get("outcome")) == "error"]
    refusals = [r for r in rows if str(r.get("outcome")) == "refusal"]
    unmatched: Counter = Counter()
    for r in rows:
        for n in (r.get("unmatched") or r.get("missing") or []):
            if isinstance(n, str) and n.strip() and not n.startswith("...["):
                unmatched[n.strip()] += 1
    glyphs: Counter = Counter()
    for r in rows:
        g = r.get("glyphs")
        if isinstance(g, dict):
            for k, v in g.items():
                if isinstance(v, int):
                    glyphs[str(k)] += v
    symbols = [int(r["symbols"]) for r in rows if isinstance(r.get("symbols"), int)]
    repairs = Counter(int(r["repairs"]) for r in rows if isinstance(r.get("repairs"), int))
    models = Counter(str(r["model"]) for r in rows if r.get("model"))
    # Only real timestamps bound the window: an entry missing `ts` would otherwise sort to the
    # front as the empty string and report the campaign as starting at nothing.
    stamps = sorted(s for s in (str(r.get("ts") or "").strip() for r in rows) if s)
    counted = len(rows) - malformed
    return {
        "engine": name,
        "requests": len(rows),
        "counted": counted,
        "malformed_entries": malformed,
        "unparsable_lines": bad,
        "first": (stamps[0] if stamps else None),
        "last": (stamps[-1] if stamps else None),
        "outcomes": dict(outcomes.most_common()),
        "produced": produced,
        "produced_rate": (round(produced / counted, 3) if counted else None),
        "seconds": {
            "n": len(secs),
            "median": (round(pct(secs, 0.5), 1) if secs else None),
            "p95": (round(pct(secs, 0.95), 1) if secs else None),
            "max": (round(max(secs), 1) if secs else None),
            "over_budget": sum(1 for s in secs if s > budget),
            "budget_s": budget,
        },
        "models": dict(models.most_common()),
        "repairs": {str(k): v for k, v in sorted(repairs.items())},
        "symbols_median": (int(pct(symbols, 0.5)) if symbols else None),
        "top_glyphs": dict(glyphs.most_common(top)),
        "top_unmatched": dict(unmatched.most_common(top)),
        "refusals": [_brief(r) for r in sorted(refusals, key=_ts, reverse=True)],
        "errors": [_brief(r) for r in sorted(errors, key=_ts, reverse=True)],
        "bad_requests": outcomes.get("bad_request", 0),
    }


def _ts(r: Dict[str, Any]) -> str:
    return str(r.get("ts") or "")


def _brief(r: Dict[str, Any]) -> Dict[str, Any]:
    return {"ts": r.get("ts"), "code": r.get("code"), "request": r.get("request"),
            "headline": r.get("headline"), "seconds": r.get("seconds"),
            "model": r.get("model"), "run_id": r.get("run_id")}


# ── printing ──────────────────────────────────────────────────────────

def cut(s: Any, n: int) -> str:
    s = "" if s is None else str(s).replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "~"


def mix(outcomes: Dict[str, int]) -> str:
    return "  ".join(f"{k} {v}" for k, v in outcomes.items()) or "none"


def ranked(d: Dict[str, int]) -> str:
    return ", ".join(f"{k} ({v})" for k, v in d.items()) or "none"


def print_text(report: Dict[str, Any], top: int, full: bool) -> None:
    W = 78
    print("=" * W)
    print("PremHist Describe report")
    print(f"logs   : {report['logs_dir']}")
    win = report.get("window") or {}
    if win.get("since") or win.get("until"):
        print(f"window : {win.get('since') or 'start'} .. {win.get('until') or 'now'}")
    print()

    any_rows = False
    for s in report["engines"]:
        print(f"{s['engine']}")
        if not s["requests"]:
            why = "no log file yet" if not s["files"] else "no requests in this window"
            print(f"  {why}")
            print()
            continue
        any_rows = True
        span = (f"{cut(s['first'], 20)} .. {cut(s['last'], 20)}" if s["first"]
                else "no timestamps")
        print(f"  requests   {s['counted']}   ({span})")
        print(f"  outcomes   {mix(s['outcomes'])}")
        if s["produced_rate"] is not None:
            print(f"  produced   {s['produced']} of {s['counted']}  "
                  f"({s['produced_rate'] * 100:.0f}% drew or drafted something)")
        sec = s["seconds"]
        if sec["n"]:
            line = (f"  seconds    median {sec['median']}   p95 {sec['p95']}   max {sec['max']}")
            if sec["over_budget"]:
                line += f"   OVER the {sec['budget_s']:.0f}s budget: {sec['over_budget']}"
            print(line)
        if s["models"]:
            print(f"  model      {ranked(s['models'])}")
        if s["repairs"]:
            print(f"  repairs    {', '.join(f'{k}: {v}' for k, v in s['repairs'].items())}")
        if s["symbols_median"] is not None:
            print(f"  drawn      median {s['symbols_median']} symbol(s) per display")
        if s["top_glyphs"]:
            print(f"  glyphs     {ranked(s['top_glyphs'])}")
        if s["top_unmatched"]:
            print(f"  unmatched  {ranked(s['top_unmatched'])}")
        if s["unparsable_lines"] or s["malformed_entries"]:
            bits = []
            if s["unparsable_lines"]:
                bits.append(f"{s['unparsable_lines']} line(s) were not valid JSON")
            if s["malformed_entries"]:
                bits.append(f"{s['malformed_entries']} entr(y/ies) had no outcome")
            print(f"  NOTE       {'; '.join(bits)} - skipped, not counted above")
        print()

    for key, title in (("refusals", "Refusals"), ("errors", "Errors")):
        rows = [(s["engine"], r) for s in report["engines"] for r in s[key]]
        rows.sort(key=lambda er: str(er[1].get("ts") or ""), reverse=True)
        if not rows:
            continue
        shown = rows if full else rows[:top]
        print(f"{title} ({len(rows)}){'' if full or len(rows) <= top else f', newest {len(shown)}'}")
        for eng, r in shown:
            print(f"  {cut(r.get('ts'), 20):20s} {eng:8s} {cut(r.get('headline'), 44)}")
            print(f"  {'':20s} {'':8s} asked: {cut(r.get('request'), 44)}")
        if not full and len(rows) > top:
            print(f"  ... {len(rows) - len(shown)} more (--full to see them all)")
        print()

    if not any_rows:
        print("Nothing to report yet: run a Describe from the Calcs or Displays tab, then re-run this.")
        print("If you have run one, check that the logs folder above is the right one (--logs DIR).")


def write_csv(report: Dict[str, Any], out_dir: str) -> List[str]:
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for s in report["engines"]:
        if not s["requests"]:
            continue
        p = os.path.join(out_dir, f"describe-{s['engine']}.csv")
        cols = ["ts", "outcome", "code", "seconds", "model", "repairs", "symbols",
                "request", "headline", "run_id"]
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in s["_rows"]:
                w.writerow([r.get(c) if not isinstance(r.get(c), (list, dict)) else
                            json.dumps(r.get(c), ensure_ascii=False) for c in cols])
        written.append(p)
    return written


# ── main ──────────────────────────────────────────────────────────────

def default_logs_dir() -> str:
    """<install>\\logs, derived from this file's own location (tools\\ sits beside logs\\)."""
    return os.path.join(os.path.dirname(HERE), "logs")


def engine_budget() -> float:
    """The engines' own budget when they can be imported, so the number is never invented."""
    for rel in ("describe", os.path.join("describe", "orchestrator")):
        p = os.path.join(os.path.dirname(HERE), rel)
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, os.path.dirname(HERE) + os.sep + "describe")
            break
    try:
        from orchestrator import display_authoring as da      # type: ignore
        return float(getattr(da, "TOTAL_BUDGET_S", DEFAULT_BUDGET_S))
    except Exception:                                          # noqa: BLE001
        return DEFAULT_BUDGET_S


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logs", metavar="DIR", default=None)
    ap.add_argument("--since", metavar="DATE", default=None)
    ap.add_argument("--until", metavar="DATE", default=None)
    ap.add_argument("--engine", choices=("calcs", "displays", "both"), default="both")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--budget", type=float, default=None)
    ap.add_argument("--csv", metavar="DIR", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--full", action="store_true")
    a = ap.parse_args(argv)

    logs_dir = os.path.abspath(a.logs or default_logs_dir())
    budget = a.budget if a.budget is not None else engine_budget()
    since, until = norm_date(a.since), norm_date(a.until, end=True)

    if not os.path.isdir(logs_dir):
        print(f"No logs folder at {logs_dir}")
        print("Pass the right one:  --logs \"C:\\path\\to\\PremHist-QA\\logs\"")
        return 1

    summaries = []
    for name, fname in ENGINES:
        if a.engine != "both" and a.engine != name:
            continue
        files = generations(logs_dir, fname)
        rows, bad = read_lines(files)
        rows = [r for r in rows if in_window(r, since, until)]
        s = summarise(name, rows, bad, budget, max(1, a.top))
        s["files"] = [os.path.basename(p) for p in files]
        s["_rows"] = rows
        summaries.append(s)

    report = {"logs_dir": logs_dir, "window": {"since": a.since, "until": a.until},
              "budget_s": budget, "engines": summaries}

    if a.json:
        print(json.dumps({**report, "engines": [{k: v for k, v in s.items() if k != "_rows"}
                                                for s in summaries]},
                         indent=2, ensure_ascii=False, default=str))
    else:
        print_text(report, max(1, a.top), a.full)

    if a.csv:
        written = write_csv(report, os.path.abspath(a.csv))
        if written:
            print("CSV written:")
            for p in written:
                print(f"  {p}")
        else:
            print("No CSV written: no requests in this window.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
