# PremHist QA - patched files

Fixes and tooling for the PremHist QA package (historian + viewer, with the AI "Describe"
feature that drafts calcs and displays from plain English).

Everything here is verified by `tools/verify-fixes.py`, which imports the **installed**
orchestrator modules and exercises each fix rather than trusting that a file was copied.
Run it against an install:

    python-describe\python.exe tools\verify-fixes.py        # 100 checks

## Applying

    powershell -ExecutionPolicy Bypass -File .\apply-patch.ps1 -Yes
    ... -AlsoPackage "<the unzipped PremHist-QA-... folder>"   # so Setup.cmd keeps them

`apply-patch.ps1` enumerates every folder in this tree rather than copying a fixed list of
names, finds the orchestrator package instead of assuming where it is, backs up what it
replaces, and runs the verifier afterwards.

## What is fixed

| Area | Fix |
|---|---|
| Describe grounding | `stem("status")`, medium words (`water level` on a plant with levels), truncated-index absence claims, bare numbers as element identities, rollup path prefixes |
| Limits | `3-sigma, every 15 minutes, center 50` no longer licenses chart limits |
| Pipes | a quoted label no longer decides the line type |
| Budgets | display Describe has a time budget; the repair round accounts for the retry cost |
| Robustness | `draft_display` never raises; `/health` reports whether each engine imports |
| Performance | the plant index is cached (TTL) and warmed at startup |
| Routes | one error envelope and a request id across both engines; `/grounding` previews |
| Result shape | `reason` / `missing` / `suggestions` on both engines, so one renderer serves both |
| Logging | an outcome log per engine, capped and rotated, with `bad_request` distinct from an engine error |
| Seeding | `--late` binds the digital state set, not just the tag attribute |
| Lifecycle | a part that will not stop keeps its record; a model switch onto a broken engine is not success; a locked file does not abandon an uninstall halfway |

## Tools

| Tool | What it does |
|---|---|
| `tools/verify-fixes.py` | 100 checks against an install |
| `tools/describe-report.py` (`.cmd`) | what the Describe logs say: outcomes, timings, refusals, unmatched words; `--csv`, `--json` |
| `tools/diagnose-describe.ps1` | end-to-end diagnosis, including `POST /api/describe/selftest` |
| `tools/why-describe-wont-start.ps1` | when the service will not start at all |
| `tools/gen_glyph_catalog.py` (`glyphs.cmd`) | generate, audit, `--find` other copies of the glyph table, `--check` for drift |
| `tools/ci-glyph-catalog.*` | the same audit as a CI job |
| `tools/ctrlc-broadcast-test.ps1` | settles whether Ctrl+C reaches one part or all of them |

## Still open

- The Ctrl+C broadcast verdict: run `tools/ctrlc-broadcast-test.ps1` and the fix follows from it.
- Whether the viewer ships its own copy of the glyph table: `tools/glyphs.cmd --find` answers it.
- Authoring timeouts: `35` would let all three repair rounds fit the 150 s budget. At the shipped
  120 s one round costs 162 s, so the repair loop is unreachable. A configuration decision.

`VERIFY.txt` lists every file with its sha256 and line count.
