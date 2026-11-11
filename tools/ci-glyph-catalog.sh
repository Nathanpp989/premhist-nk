#!/usr/bin/env bash
# ci-glyph-catalog.sh - fail the build when the glyph catalog has drifted.
#
# The glyph table is duplicated by design: Python owns it (orchestrator/display_glyphs.py), and
# the historian types and the viewer each carry a copy (ph-types/glyphs.rs, display_glyphs_*.js).
# Nothing checked that the three agreed, so the first sign of a drift was a runtime
# AttributeError or a symbol the viewer could not draw - found by a tester, not by a build.
#
# This runs the two checks gen_glyph_catalog.py already knows how to do:
#
#   --audit        the catalog is internally consistent (every name round-trips, every glyph has
#                  an area and a class, portless glyphs have no ports). Always runnable.
#   --check DIR    the generated files in DIR match the catalog. Only run when those files are
#                  present, so a checkout that does not carry them is not failed for it.
#
# Usage:  tools/ci-glyph-catalog.sh [GENERATED_DIR]
#
# GENERATED_DIR defaults to the first of these that exists: ph-types, viewer/src/generated,
# build/generated. Exit 0 = clean, 1 = drift (and the output names what differs).

set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
GEN="${HERE}/gen_glyph_catalog.py"
PY="${PYTHON:-python3}"

if [ ! -f "$GEN" ]; then
  echo "ci-glyph-catalog: ${GEN} not found" >&2
  exit 1
fi

rc=0

echo "== glyph catalog: internal audit"
# -I so the catalog cannot be influenced by anything in the current directory.
"$PY" -I "$GEN" --audit || rc=1

DIR="${1:-}"
if [ -z "$DIR" ]; then
  for cand in ph-types viewer/src/generated build/generated; do
    if [ -d "$cand" ]; then DIR="$cand"; break; fi
  done
fi

if [ -n "$DIR" ] && [ -d "$DIR" ]; then
  echo "== glyph catalog: generated files in ${DIR}"
  "$PY" -I "$GEN" --check "$DIR" || rc=1
else
  echo "== glyph catalog: no generated files to compare (skipped)"
  echo "   Pass the directory holding glyphs.rs / display_glyphs_*.js to check them too."
fi

if [ "$rc" -ne 0 ]; then
  echo
  echo "The glyph catalog has drifted. Regenerate with:"
  echo "    python tools/gen_glyph_catalog.py --out ${DIR:-<dir>}"
  echo "and commit the result - do not hand-edit the generated files."
fi
exit "$rc"
