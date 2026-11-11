"""
describe_log_io.py - the shared writer behind the two Describe outcome logs.

`calc_describe_log` and `display_describe_log` are deliberate parallels, and both had the same two
problems, measured rather than supposed:

- **No size cap, and no rotation.** The calc log keeps the drafted `body`; a 200,000-character body
  produced a single 200 KB line, and three of them a 601 KB file. Nothing ever trimmed it. The
  package's own log rotation does not reach these files either: `qa-common.ps1` rotates with
  `-replace '\\.log$', '.prev.log'`, which does not match `.jsonl`. So the two files a bug report
  is asked to zip (README section 7) were the only ones that grew without bound, across every run.
- **No cap on individual fields.** `notes`, `choices` and `scope_proposal` were written whole. One
  pathological reply could put a megabyte on one line, and `record()` is called from async code, so
  that write happens on the event loop.

Both logs now go through here. Nothing in this module raises: a log must never be the reason a
draft fails, so every function degrades to doing less rather than throwing.

Rotation keeps ONE previous generation, `<name>.prev.jsonl`, matching the `.prev.log` convention
the package already uses for part logs. Cap: env `PREMALYTICS_DESCRIBE_LOG_MAX_MB` (default 8;
`0` or `off` means no cap, for a run where you want the whole history).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence

MAX_MB_ENV = "PREMALYTICS_DESCRIBE_LOG_MAX_MB"
DEFAULT_MAX_MB = 8.0

# Per-field caps. Generous enough that a real entry is never truncated, small enough that no single
# line can dominate the file.
BODY_CHARS = 20_000
TEXT_CHARS = 2_000
LIST_ITEMS = 20
LIST_ITEM_CHARS = 1_000
# A backstop for fields nobody enumerated: if a line still exceeds this, the heaviest fields are
# dropped and the entry says so, rather than the cap being silently exceeded.
MAX_LINE_BYTES = 64 * 1024
HEAVY_FIELDS = ("body", "notes", "choices", "scope_proposal", "glyphs", "placeholders",
                "refused_ops")


def max_bytes() -> Optional[int]:
    """The size at which the log rotates, or None when the cap is turned off."""
    raw = os.environ.get(MAX_MB_ENV)
    if raw is None:
        mb = DEFAULT_MAX_MB
    else:
        raw = raw.strip().lower()
        if raw in ("", "0", "off", "false", "no", "none"):
            return None
        try:
            mb = float(raw)
        except ValueError:
            mb = DEFAULT_MAX_MB
    if mb <= 0:
        return None
    return int(mb * 1024 * 1024)


def resolve(env: str, default: str) -> Optional[str]:
    """The file an outcome log goes to, or None when that log is turned off."""
    raw = os.environ.get(env)
    if raw is None:
        return default
    raw = raw.strip()
    if not raw or raw.lower() in ("off", "0", "false", "no", "none"):
        return None
    return raw


def text(value: Any, chars: int = TEXT_CHARS) -> Optional[str]:
    """A string field, capped. None and empty both become None, so the key stays absent-ish."""
    if value is None:
        return None
    s = value if isinstance(value, str) else str(value)
    if not s:
        return None
    return s if len(s) <= chars else s[:chars] + f"...[+{len(s) - chars} chars]"


def items(value: Any, limit: int = LIST_ITEMS, chars: int = LIST_ITEM_CHARS) -> Optional[List[Any]]:
    """A list field, capped in both directions: how many entries, and how long each one is.

    A dict or list entry keeps its SHAPE while it fits. An earlier version stringified every
    non-scalar entry, which quietly changed `choices` in the calc log from a list of objects to a
    list of JSON strings - a cap is supposed to bound a field, not re-type it, and anything parsing
    that field would have broken. Only an entry that is genuinely too big is replaced by its
    truncated JSON, which is visibly a string and says how much was cut."""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    out: List[Any] = []
    for it in list(value)[:limit]:
        if isinstance(it, str):
            out.append(text(it, chars))
        elif isinstance(it, (int, float, bool)) or it is None:
            out.append(it)
        else:
            try:
                encoded = json.dumps(it, ensure_ascii=False, default=str)
            except Exception:                        # noqa: BLE001
                encoded = str(it)
            out.append(it if len(encoded) <= chars else text(encoded, chars))
    extra = len(value) - limit
    if extra > 0:
        out.append(f"...[+{extra} more]")
    return out


def obj(value: Any, chars: int = TEXT_CHARS) -> Any:
    """An object field, capped WITHOUT changing its type while it fits.

    Same rule as `items`: a cap bounds a field, it does not re-type it. `scope_proposal` is read
    as an object by anything that reads the calc log, so it stays an object unless it is genuinely
    too large, in which case it becomes its truncated JSON - visibly a string, and self-describing.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return text(value, chars) if isinstance(value, str) else value
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:                                # noqa: BLE001
        return text(str(value), chars)
    return value if len(encoded) <= chars else text(encoded, chars)


def line_of(entry: Dict[str, Any]) -> str:
    """One JSON line for an entry, guaranteed under MAX_LINE_BYTES.

    The per-field caps above do the real work; this only catches an entry that is still too big
    because of a field no cap knows about. It drops the heaviest fields rather than cutting the
    JSON in half, because half a line is not a line a reader can parse."""
    try:
        s = json.dumps(entry, ensure_ascii=False, default=str)
    except Exception:
        s = json.dumps({"ts": entry.get("ts"), "outcome": entry.get("outcome"),
                        "log_error": "entry could not be serialised"}, default=str)
    if len(s.encode("utf-8", "replace")) <= MAX_LINE_BYTES:
        return s
    trimmed = dict(entry)
    dropped: List[str] = []
    for key in HEAVY_FIELDS:
        if trimmed.get(key) is not None:
            trimmed[key] = None
            dropped.append(key)
            trimmed["log_trimmed"] = dropped
            try:
                s2 = json.dumps(trimmed, ensure_ascii=False, default=str)
            except Exception:
                continue
            if len(s2.encode("utf-8", "replace")) <= MAX_LINE_BYTES:
                return s2
    return json.dumps({"ts": entry.get("ts"), "outcome": entry.get("outcome"),
                       "log_trimmed": dropped or list(HEAVY_FIELDS),
                       "log_error": "entry over the line limit"}, default=str)


def append(path: str, line: str, lock: Any) -> Optional[str]:
    """Append one line, rotating first when the file has reached the cap. Never raises.

    Rotation happens BEFORE the write, so the line that crosses the threshold is not itself split
    across two files. The previous generation is replaced, so at most two files exist per log."""
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        cap = max_bytes()
        with lock:
            if cap is not None:
                try:
                    if os.path.getsize(path) >= cap:
                        prev = path[:-6] + ".prev.jsonl" if path.endswith(".jsonl") else path + ".prev"
                        os.replace(path, prev)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass          # cannot rotate (locked, read-only): keep logging to the same file
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return path
    except Exception:
        return None


__all__ = ["resolve", "text", "items", "obj", "line_of", "append", "max_bytes",
           "MAX_MB_ENV", "DEFAULT_MAX_MB", "BODY_CHARS", "TEXT_CHARS", "LIST_ITEMS",
           "LIST_ITEM_CHARS", "MAX_LINE_BYTES", "HEAVY_FIELDS"]
