"""Central configuration for the orchestrator.

This module is intended to be the *single* source of truth for LLM model
selection and related runtime settings.

If you need to change the model, do it here (or via the env vars documented
below) rather than scattering defaults across multiple files.

Precedence is NOT uniform, and the difference matters: for `provider`, `ollama_host` and
`ollama_model` the SETTINGS FILE wins over the environment variable, while for
`ollama_investigation_model`, `ollama_sql_model`, `ollama_embed_model`, `reasoning_effort` and
`reasoning_investigation` the ENVIRONMENT VARIABLE wins over the file. The numeric knobs
(temperature, timeout_s, num_ctx, the two authoring timeouts) take the file over the env too.
That is what makes the PremHist QA scripts work: they own `orchestrator_settings.json` through
ORCH_SETTINGS_FILE so the tester's model choice cannot be overridden by a stray
ORCH_OLLAMA_MODEL in the environment.

Env vars (optional):
  - ORCH_LLM_PROVIDER                (default: "ollama")
  - ORCH_OLLAMA_HOST                 (default: "http://127.0.0.1:11434")
  - ORCH_OLLAMA_MODEL                (default: "gpt-oss:20b")  — conversation/chat + Ask orchestration (fast)
  - ORCH_OLLAMA_INVESTIGATION_MODEL  (default: falls back to ORCH_OLLAMA_MODEL) — Explore investigation (powerful)
  - ORCH_OLLAMA_SQL_MODEL            (default: falls back to ORCH_OLLAMA_INVESTIGATION_MODEL) — SQL-writing specialist
  - ORCH_OLLAMA_EMBED_MODEL          (default: falls back to ORCH_OLLAMA_MODEL)  — embeddings only
  - ORCH_LLM_TEMPERATURE             (default: 0.0)
  - ORCH_LLM_TIMEOUT_S               (default: 60)
  - ORCH_OLLAMA_NUM_CTX              (default: 16384)  — Ollama context window; 0 = Ollama default
  - ORCH_DISPLAY_AUTHORING_TIMEOUT_S (default: 120)    — "describe a display" model call (design_displays §6.1)

The embed model is intentionally separate from the chat model so that:
  - Operators can swap chat LLMs via the Admin tool without affecting embeddings
  - A purpose-built embedding model (e.g. nomic-embed-text) can be used for
    semantic routing while a larger model handles chat/reasoning
"""

from __future__ import annotations

import os
import json
from dataclasses import dataclass


DEFAULT_SETTINGS_FILE = os.environ.get(
    "ORCH_SETTINGS_FILE", os.path.expanduser("~/.orchestrator_settings.json")
)


def _load_file_overrides(path: str) -> dict:
    """Load user-editable settings overrides from disk.

    This file is meant to be written by the Admin tool. It should be a JSON
    object with any subset of:
      provider, ollama_host, ollama_model, ollama_embed_model, temperature, timeout_s
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        # Be conservative: ignore corrupt overrides rather than crash.
        return {}


def write_file_overrides(overrides: dict, path: str | None = None) -> None:
    """Persist overrides atomically. Intended for Admin tool integration."""
    path = path or DEFAULT_SETTINGS_FILE
    # A path with no directory part ("settings.json") gives dirname "" and makedirs then raises
    # FileNotFoundError: [Errno 2] ... ''. Guarded, as calc_describe_log.record already guards it.
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(overrides, f, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def clear_settings_cache() -> None:
    """No-op retained for backward compatibility: there is no cache to clear (see get_settings)."""
    return


def _env_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    try:
        return float(val)
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    try:
        return int(val)
    except Exception:
        return default


@dataclass(frozen=True)
class LLMSettings:
    """Runtime config for the LLM backend."""

    provider: str
    # Ollama-specific
    ollama_host: str
    ollama_model: str                    # conversation / chat + Ask orchestration (fast, small)
    ollama_investigation_model: str      # Explore investigation agent (falls back to ollama_model)
    ollama_sql_model: str                # SQL-generation specialist (falls back to investigation model)
    ollama_embed_model: str              # dedicated embedding model (falls back to ollama_model)

    # Common knobs
    temperature: float
    timeout_s: int
    num_ctx: int                   # Ollama context window in tokens (0 = let Ollama decide)

    # Reasoning effort (set via Admin tool, optional)
    reasoning_effort: str          # conversation reasoning (empty = off)
    reasoning_investigation: str   # investigation agent reasoning (empty = off)

    # "Describe a display" (D5) model-call timeout; file key display_authoring_timeout_s
    display_authoring_timeout_s: int = 120
    # "Describe a calc" (PremCalc C4, design_calc §10.22) per-model-call timeout;
    # file key calc_authoring_timeout_s, env ORCH_CALC_AUTHORING_TIMEOUT_S
    calc_authoring_timeout_s: int = 120


def get_settings() -> LLMSettings:
    """Read the settings from the env vars and the settings file, with sane defaults.

    NOT cached, despite what this said before: the settings file is re-read on every call, so
    every model call pays one small JSON read. That is also why 'Switch Describe model' takes
    effect without restarting anything that calls this - worth keeping in mind before adding a
    cache. See the module docstring for which source wins per field."""
    provider = os.environ.get("ORCH_LLM_PROVIDER", "ollama").strip() or "ollama"

    # Ollama defaults
    ollama_host = os.environ.get("ORCH_OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    ollama_model = os.environ.get("ORCH_OLLAMA_MODEL", "gpt-oss:20b").strip() or "gpt-oss:20b"

    # Dedicated embedding model — falls back to chat model if not set.
    # Set via env var (preferred for PremDispatch .ps1 scripts) or Admin tool file overrides.
    ollama_embed_model = os.environ.get("ORCH_OLLAMA_EMBED_MODEL", "").strip()

    temperature = _env_float("ORCH_LLM_TEMPERATURE", 0.0)
    timeout_s = _env_int("ORCH_LLM_TIMEOUT_S", 60)
    # Ollama context window. Ollama's own default is only ~4096 tokens, which the
    # agentic prompt (tool specs + schema + conversation history) easily overflows —
    # causing truncated input and EMPTY model replies. Set generously; gpt-oss:20b
    # supports far more. 0 means "don't set it" (defer to Ollama's default).
    num_ctx = _env_int("ORCH_OLLAMA_NUM_CTX", 16384)
    display_authoring_timeout_s = _env_int("ORCH_DISPLAY_AUTHORING_TIMEOUT_S", 120)
    calc_authoring_timeout_s = _env_int("ORCH_CALC_AUTHORING_TIMEOUT_S", 120)

    # Optional file-based overrides written by the Admin tool.
    file_overrides = _load_file_overrides(DEFAULT_SETTINGS_FILE)
    provider = str(file_overrides.get("provider", provider)).strip() or provider
    ollama_host = str(file_overrides.get("ollama_host", ollama_host)).strip() or ollama_host
    ollama_model = str(file_overrides.get("ollama_model", ollama_model)).strip() or ollama_model

    # Investigation model: env var wins, then file override, then fall back to chat model.
    ollama_investigation_model = os.environ.get("ORCH_OLLAMA_INVESTIGATION_MODEL", "").strip()
    if not ollama_investigation_model:
        ollama_investigation_model = str(file_overrides.get("ollama_investigation_model", "")).strip()
    if not ollama_investigation_model:
        ollama_investigation_model = ollama_model

    # SQL-generation specialist: the model that WRITES SQL (so a small fast tool-selector
    # can orchestrate while a big coder writes the SQL). env wins, then file, then falls back
    # to the powerful investigation model so SQL quality never depends on a small model.
    ollama_sql_model = os.environ.get("ORCH_OLLAMA_SQL_MODEL", "").strip()
    if not ollama_sql_model:
        ollama_sql_model = str(file_overrides.get("ollama_sql_model", "")).strip()
    if not ollama_sql_model:
        ollama_sql_model = ollama_investigation_model

    # Embed model: env var wins, then file override, then fall back to chat model.
    if not ollama_embed_model:
        ollama_embed_model = str(file_overrides.get("ollama_embed_model", "")).strip()
    if not ollama_embed_model:
        ollama_embed_model = ollama_model

    if "temperature" in file_overrides:
        try:
            temperature = float(file_overrides["temperature"])
        except Exception:
            pass
    if "timeout_s" in file_overrides:
        try:
            timeout_s = int(file_overrides["timeout_s"])
        except Exception:
            pass
    if "num_ctx" in file_overrides:
        try:
            num_ctx = int(file_overrides["num_ctx"])
        except Exception:
            pass
    if "display_authoring_timeout_s" in file_overrides:
        try:
            display_authoring_timeout_s = int(file_overrides["display_authoring_timeout_s"])
        except Exception:
            pass
    if "calc_authoring_timeout_s" in file_overrides:
        try:
            calc_authoring_timeout_s = int(file_overrides["calc_authoring_timeout_s"])
        except Exception:
            pass

    # Reasoning effort — env var wins, then file override, default empty (off).
    reasoning_effort = os.environ.get("ORCH_REASONING_EFFORT", "").strip()
    if not reasoning_effort:
        reasoning_effort = str(file_overrides.get("reasoning_effort", "")).strip()

    reasoning_investigation = os.environ.get("ORCH_REASONING_INVESTIGATION", "").strip()
    if not reasoning_investigation:
        reasoning_investigation = str(file_overrides.get("reasoning_investigation", "")).strip()

    return LLMSettings(
        provider=provider,
        ollama_host=ollama_host,
        ollama_model=ollama_model,
        ollama_investigation_model=ollama_investigation_model,
        ollama_sql_model=ollama_sql_model,
        ollama_embed_model=ollama_embed_model,
        temperature=temperature,
        timeout_s=timeout_s,
        num_ctx=num_ctx,
        reasoning_effort=reasoning_effort,
        reasoning_investigation=reasoning_investigation,
        display_authoring_timeout_s=display_authoring_timeout_s,
        calc_authoring_timeout_s=calc_authoring_timeout_s,
    )
