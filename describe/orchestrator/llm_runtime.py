# orchestrator/llm_runtime.py

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests  # type: ignore
except Exception:
    requests = None  # type: ignore

from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from .settings import get_settings


# ── Debug prompt-dump logging (env-gated) ──────────────────────────────
# Activated by EITHER of the following env vars (any truthy value):
#   PREMALYTICS_DEBUG_PROMPTS  — granular flag, just for these dumps
#   PREMALYTICS_DEBUG          — existing master debug flag (read by
#                                 prompts.py and used elsewhere). Setting
#                                 this to "True" / "1" in start-ui3.ps1
#                                 activates everything debug-related,
#                                 including these prompt dumps.
#
# Either path writes to premalytics_conversation.log via orchestrator._clog
# so all diagnostics stay in one file. Zero overhead when both are unset
# (or set to a falsy value like "False").
#
# This is the chokepoint where we can finally see exactly what the model
# is being shown vs what it produces — invaluable for diagnosing schema-
# grounded SQL failures (is the schema at position 0? Is it intact? Did
# the model read it?).
_TRUTHY_VALUES = ("1", "true", "yes", "on")


def _debug_dump_enabled() -> bool:
    granular = os.environ.get("PREMALYTICS_DEBUG_PROMPTS", "").strip().lower()
    if granular in _TRUTHY_VALUES:
        return True
    master = os.environ.get("PREMALYTICS_DEBUG", "").strip().lower()
    return master in _TRUTHY_VALUES


def _msg_content_for_log(content: Any) -> str:
    """Normalize a message's content field to a string for logging.
    Multimodal content can be a list; flatten via JSON dump."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    try:
        return json.dumps(content, default=str, ensure_ascii=False)
    except Exception:
        return str(content)


def _truncate_for_log(text: str, head: int = 3000, tail: int = 500) -> str:
    """Return text untouched if short, else head + ellipsis + tail.
    Generous limits — debug dump is supposed to capture everything that
    matters, including full schema blocks (~4-8KB) intact."""
    if len(text) <= head + tail + 80:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n  ...[{omitted} chars omitted]...\n{text[-tail:]}"


def _dump_prompt_to_log(messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]], model: str) -> None:
    """Dump the full messages array (with each message's role and content)
    plus a tool-list summary to the conversation log. Gated by
    PREMALYTICS_DEBUG_PROMPTS env var."""
    if not _debug_dump_enabled():
        return
    try:
        from .orchestrator import _clog
    except Exception:
        return

    try:
        msg_count = len(messages)
        # Approximate prompt size — char count of all roles+contents+tool_calls.
        total_chars = 0
        for m in messages:
            total_chars += len(_msg_content_for_log(m.get("content")))
            tc = m.get("tool_calls") or []
            if tc:
                try:
                    total_chars += len(json.dumps(tc, default=str))
                except Exception:
                    pass
        approx_tokens = total_chars // 4  # rough heuristic; small models often use ~4 chars/token

        tool_count = len(tools) if tools else 0
        tool_names = [(t.get("function") or {}).get("name") or "?" for t in (tools or [])]

        _clog("=" * 70)
        _clog(f"=== PROMPT DUMP ===  model={model}  messages={msg_count}  "
              f"~{total_chars} chars  ~{approx_tokens} tokens  tools={tool_count}")
        if tool_count:
            _clog(f"    tool_names: {tool_names}")
        _clog("-" * 70)

        for i, m in enumerate(messages):
            role = m.get("role", "?")
            content_raw = _msg_content_for_log(m.get("content"))
            tool_calls = m.get("tool_calls") or []
            name_attr = m.get("name") or ""
            tcid = m.get("tool_call_id") or ""

            header = f"  [{i}] role={role}"
            if name_attr:
                header += f" name={name_attr}"
            if tcid:
                header += f" tool_call_id={tcid}"
            header += f" content_chars={len(content_raw)}"
            _clog(header)

            if content_raw:
                # Indent each line by 6 spaces for readability in the log
                preview = _truncate_for_log(content_raw)
                for line in preview.splitlines() or [""]:
                    _clog(f"      {line}")

            if tool_calls:
                _clog(f"      tool_calls: {len(tool_calls)}")
                for j, tc in enumerate(tool_calls[:5]):
                    fn = (tc.get("function") or {})
                    args = fn.get("arguments", {})
                    try:
                        args_str = json.dumps(args, default=str) if not isinstance(args, str) else args
                    except Exception:
                        args_str = str(args)
                    if len(args_str) > 600:
                        args_str = args_str[:600] + f"... [{len(args_str)-600} more chars]"
                    _clog(f"        [{j}] name={fn.get('name','?')}  args={args_str}")

        _clog("=" * 70)
    except Exception as _e:
        # Logging must never break the LLM call. Swallow and move on.
        try:
            from .orchestrator import _clog
            _clog(f"  [prompt-dump] failed: {_e!r}")
        except Exception:
            pass


def _dump_response_to_log(resp_msg: Dict[str, Any]) -> None:
    """Dump the raw model response (content + tool_calls) BEFORE any
    post-processing (tool-call extraction, content cleanup). Lets us see
    exactly what the model produced, which the post-processed result
    can hide. Gated by PREMALYTICS_DEBUG_PROMPTS env var."""
    if not _debug_dump_enabled():
        return
    try:
        from .orchestrator import _clog
    except Exception:
        return

    try:
        role = resp_msg.get("role", "?")
        content = _msg_content_for_log(resp_msg.get("content"))
        tool_calls = resp_msg.get("tool_calls") or []

        _clog("=" * 70)
        _clog(f"=== LLM RAW RESPONSE ===  role={role}  "
              f"content_chars={len(content)}  tool_calls={len(tool_calls)}")
        _clog("-" * 70)

        if content:
            preview = _truncate_for_log(content, head=2000, tail=300)
            for line in preview.splitlines() or [""]:
                _clog(f"  {line}")

        for j, tc in enumerate(tool_calls[:10]):
            fn = (tc.get("function") or {})
            args = fn.get("arguments", {})
            try:
                args_str = json.dumps(args, default=str) if not isinstance(args, str) else args
            except Exception:
                args_str = str(args)
            if len(args_str) > 600:
                args_str = args_str[:600] + f"... [{len(args_str)-600} more chars]"
            _clog(f"  tool_call[{j}] name={fn.get('name','?')}")
            _clog(f"               args={args_str}")

        _clog("=" * 70)
    except Exception as _e:
        try:
            from .orchestrator import _clog
            _clog(f"  [response-dump] failed: {_e!r}")
        except Exception:
            pass


# ── What one call to this module can actually cost ─────────────────────
# call_ollama_chat retries ONCE on any failure, with a shorter timeout, and on double failure
# returns a normal-looking assistant message instead of raising. Callers that budget their time
# against a deadline (calc_authoring / display_authoring, both of which must answer inside
# ph-server's 180 s Describe forward) were computing with timeout_s alone and so under-counting
# every round by up to 35%. These are exported so the budget can be computed from one place.
RETRY_TIMEOUT_FRACTION = 0.35
RETRY_TIMEOUT_MIN_S = 15
# The message returned when both attempts fail. Both authoring modules recognise a reply that
# starts with this; it lived as a copied string literal in three files.
OLLAMA_TIMEOUT_MESSAGE = (
    "My local LLM backend (Ollama) didn't respond in time.\n"
    "Please try again. If it keeps happening, restart Ollama or switch to a smaller model.\n"
    "Tip: you can still use deterministic commands like `run <tool> ...` while Ollama is busy."
)
# The prefix callers match on (the first line, without its newline).
OLLAMA_TIMEOUT_SENTINEL = OLLAMA_TIMEOUT_MESSAGE.split("\n", 1)[0].rstrip(".")


def retry_seconds(timeout_s: int) -> int:
    """The timeout of the single retry call_ollama_chat makes after a failure."""
    return max(RETRY_TIMEOUT_MIN_S, int(int(timeout_s) * RETRY_TIMEOUT_FRACTION))


def max_call_seconds(timeout_s: int) -> int:
    """The worst case wall time of ONE call_ollama_chat: the attempt plus its retry. A caller
    with a deadline must have this much left before it starts another round, not just a
    non-negative remainder."""
    return int(timeout_s) + retry_seconds(timeout_s)


def _post_json(url: str, payload: Dict[str, Any], timeout_s: int = 60) -> Dict[str, Any]:
    if requests is not None:
        resp = requests.post(url, json=payload, timeout=timeout_s)
        resp.raise_for_status()
        return resp.json()

    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=timeout_s) as r:
            return json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        raise RuntimeError(f"Ollama HTTPError {e.code}: {body}") from e
    except URLError as e:
        raise RuntimeError(f"Ollama URLError: {e}") from e


# ── Tool-name sanitization ──────────────────────────────────────────
# Some local models emit control/format tokens *inside* the tool name,
# which then fails to match any registered tool ("Tool 'X' not found").
# Observed with gpt-oss (harmony format): the model returns tool names
# like  SQL.query<|channel|>commentary  or  SQL_query<|channel|>commentary
# — the harmony "channel" control sequence leaks into the function name.
# This strips that pollution so the name resolves normally. Two steps:
#   1. Cut the name at the first control-token marker ('<|', '<｜', or a
#      stray channel/analysis/final tag), leaving the real tool name.
#   2. If the result has no namespace dot but begins with a known
#      namespace followed by '_' (the model wrote SQL_query for SQL.query),
#      repair that single separator. Scoped to known namespaces so we
#      never rewrite a legitimately underscored raw tool name.
# Conservative: a name that's already clean passes through untouched.
_KNOWN_TOOL_NAMESPACES: Tuple[str, ...] = (
    "SQL", "core", "TimeBase", "RAG", "mcp2",
)
_CONTROL_TOKEN_RE = re.compile(r"<[\|｜].*$|<\|.*$|<channel>.*$", re.IGNORECASE)


def _sanitize_tool_name(name: Any) -> Any:
    """Strip leaked control/format tokens from a model-emitted tool name and
    repair a known-namespace underscore separator. Returns the cleaned name,
    or the original value unchanged if it isn't a non-empty string."""
    if not isinstance(name, str) or not name:
        return name
    cleaned = _CONTROL_TOKEN_RE.sub("", name).strip()
    # Trim any trailing junk left after the marker removal.
    cleaned = cleaned.rstrip("<|｜ \t")
    if not cleaned:
        return name  # nothing salvageable — leave original for error visibility
    if "." not in cleaned:
        for ns in _KNOWN_TOOL_NAMESPACES:
            prefix = ns + "_"
            if cleaned.startswith(prefix):
                cleaned = ns + "." + cleaned[len(prefix):]
                break
    return cleaned


def _normalize_tool_calls(tool_calls: Any) -> List[Dict[str, Any]]:
    """
    Normalize tool call shapes returned by Ollama/models into an OpenAI-like list format.

    Handles:
      - tool_calls as dict (single) or list
      - tool call objects with {"name","arguments"} at top-level (no "function" wrapper)
      - arguments as dict or JSON string
      - tool names polluted with control/format tokens (see _sanitize_tool_name)
    """
    if not tool_calls:
        return []

    # allow single tool call as dict
    if isinstance(tool_calls, dict):
        tool_calls = [tool_calls]

    if not isinstance(tool_calls, list):
        return []

    out: List[Dict[str, Any]] = []
    for i, tc in enumerate(tool_calls):
        if not isinstance(tc, dict):
            continue

        fn = tc.get("function")
        if not isinstance(fn, dict):
            fn = {}

        # fallback: {"name":..., "arguments":...} at top-level
        if not fn and ("name" in tc or "arguments" in tc):
            fn = tc

        name = fn.get("name")
        name = _sanitize_tool_name(name)
        args = fn.get("arguments", {})

        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                # preserve evidence
                args = {"__raw_arguments__": fn.get("arguments")}

        if not isinstance(args, dict):
            args = {}

        if not name:
            continue

        out.append(
            {
                "id": tc.get("id") or f"call_{i}",
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        )

    return out


# Cleanup: strip tool-call-shaped JSON syntax from LLM content so it
# doesn't leak into the user-facing chat bubble. The local Ollama model
# (and some others) sometimes emit tool-call JSON as prose — bare,
# fenced, or wrapped in <tool_call> tags — even when the same call has
# already been routed via the structured tool_calls field, or when the
# model is just echoing a prior turn's call from history. Without this
# strip, the user sees a raw {"name":"X","arguments":{...}} block
# inside the assistant's reply, which is confusing and unprofessional.
#
# Conservative by design: each candidate is JSON-parsed and required to
# have both "name" and "arguments" keys before being removed, so prose
# that merely discusses these words in English is unaffected.
_TOOL_CALL_TAG_STRIP_RE = re.compile(
    r"<tool_call>\s*.*?\s*</tool_call>",
    flags=re.DOTALL | re.IGNORECASE,
)
_FENCED_BLOCK_RE = re.compile(
    r"```(?:[a-zA-Z_]*\s*)?\n?(.*?)```",
    flags=re.DOTALL,
)
_BARE_JSON_LINE_RE = re.compile(
    r'^[ \t]*(\{[^\n]*?"name"[^\n]*?"arguments"[^\n]*?\})[ \t]*$',
    flags=re.MULTILINE,
)


def _looks_like_tool_call_json(blob: str) -> bool:
    """True iff blob parses as a JSON object with both 'name' and 'arguments' keys."""
    try:
        obj = json.loads(blob)
    except Exception:
        return False
    return isinstance(obj, dict) and "name" in obj and "arguments" in obj


def _strip_tool_call_markers(content: str) -> str:
    """
    Remove tool-call-shaped JSON syntax from LLM content so it doesn't
    leak into the user-facing chat. See module-level comment for rationale.
    """
    if not content:
        return content

    # 1. Strip <tool_call>...</tool_call> tag blocks (whatever they wrap).
    content = _TOOL_CALL_TAG_STRIP_RE.sub("", content)

    # 2. Strip fenced code blocks whose body parses as a tool-call object.
    def _maybe_strip_fenced(m: "re.Match[str]") -> str:
        body = (m.group(1) or "").strip()
        # Some models prefix the body with a redundant 'json'/'tool' tag.
        body = re.sub(r"^\s*(?:json|tool|tool_call)\s*", "", body, flags=re.IGNORECASE).strip()
        return "" if _looks_like_tool_call_json(body) else m.group(0)

    content = _FENCED_BLOCK_RE.sub(_maybe_strip_fenced, content)

    # 3. Strip bare single-line JSON tool-call objects.
    def _maybe_strip_bare(m: "re.Match[str]") -> str:
        return "" if _looks_like_tool_call_json(m.group(1).strip()) else m.group(0)

    content = _BARE_JSON_LINE_RE.sub(_maybe_strip_bare, content)

    # Collapse runs of blank lines created by removals (3+ newlines -> 2).
    content = re.sub(r"\n{3,}", "\n\n", content)

    return content.strip()


# Fallback: extract tool calls embedded in content, e.g.
# <tool_call>{"name":"core.Tool","arguments":{...}}</tool_call>
_TOOL_CALL_BLOCK_RE = re.compile(
    r"<tool_call>\s*(.*?)\s*</tool_call>",
    flags=re.DOTALL | re.IGNORECASE,
)


def _extract_tool_calls_from_content(content: str) -> List[Dict[str, Any]]:
    if not content:
        return []

    out: List[Dict[str, Any]] = []
    for i, m in enumerate(_TOOL_CALL_BLOCK_RE.finditer(content)):
        blob = (m.group(1) or "").strip()

        # If the model wrapped JSON in a fenced block, unwrap it
        if blob.startswith("```"):
            blob = blob.strip()
            blob = blob.strip("`")
            # Remove a leading 'json' or 'tool' language tag if present
            blob = re.sub(r"^\s*(json|tool)\s*", "", blob, flags=re.IGNORECASE).strip()

        try:
            obj = json.loads(blob)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue

        name = obj.get("name")
        args = obj.get("arguments", {})

        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {"__raw_arguments__": obj.get("arguments")}

        if not isinstance(args, dict):
            args = {}

        if not name:
            continue

        out.append(
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        )
    return out


async def call_ollama_chat(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    model: Optional[str] = None,
    host: Optional[str] = None,
    temperature: Optional[float] = None,
    timeout_s: Optional[int] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Ollama /api/chat wrapper.

    If timeout_s is not provided, we use orchestrator settings.timeout_s
    so the Admin tool can control it.
    """
    s = get_settings()
    used_host = (host or s.ollama_host).rstrip("/")
    used_model = model or s.ollama_model
    used_temperature = s.temperature if temperature is None else float(temperature)
    used_timeout = int(timeout_s) if timeout_s is not None else int(s.timeout_s)

    payload: Dict[str, Any] = {
        "model": used_model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": used_temperature},
    }
    # Set the context window explicitly — Ollama's default (~4096) truncates the
    # agentic prompt and yields empty replies. 0 means defer to Ollama's default.
    if getattr(s, "num_ctx", 0):
        payload["options"]["num_ctx"] = int(s.num_ctx)
    if tools:
        payload["tools"] = tools

    if "options" in kwargs and isinstance(kwargs["options"], dict):
        payload["options"].update(kwargs["options"])
    for k, v in kwargs.items():
        if k != "options":
            payload[k] = v

    # Debug — dump the full messages array + tool list before sending to Ollama.
    # No-op when PREMALYTICS_DEBUG_PROMPTS env var is unset. Single source of
    # truth: every LLM call goes through this chokepoint, so this captures
    # node_plan AND every node_reflect/node_tools follow-up call.
    _dump_prompt_to_log(messages, tools, used_model)

    # Run the blocking HTTP call in a thread so the asyncio event loop stays free.
    # This is critical: without it, asyncio.wait_for() timeouts can never fire because
    # requests.post() blocks the loop for the full duration of the Ollama inference.
    import asyncio as _asyncio
    import functools as _functools

    _call = _functools.partial(_post_json, f"{used_host}/api/chat", payload, timeout_s=used_timeout)
    try:
        resp = await _asyncio.get_event_loop().run_in_executor(None, _call)
    except Exception:
        # Retry once with a shorter timeout. retry_seconds() is the one definition of that
        # timeout, so a caller budgeting against a deadline can account for this round trip
        # (see max_call_seconds).
        retry_timeout = retry_seconds(used_timeout)
        _call_retry = _functools.partial(_post_json, f"{used_host}/api/chat", payload, timeout_s=retry_timeout)
        try:
            resp = await _asyncio.get_event_loop().run_in_executor(None, _call_retry)
        except Exception:
            return {"role": "assistant", "content": OLLAMA_TIMEOUT_MESSAGE, "tool_calls": []}

    msg = resp.get("message") or {}

    # Perf telemetry: Ollama reports model-LOAD time vs prompt-eval vs generation time —
    # exactly the "cold-load thrash vs slow generation" distinction needed to triage local
    # LLM timeouts (investigation pass-1 failures). Attached under a private key; consumers
    # read msg.get("_perf"). All *_duration values are NANOSECONDS (Ollama convention).
    try:
        msg["_perf"] = {k: resp[k] for k in (
            "total_duration", "load_duration",
            "prompt_eval_count", "prompt_eval_duration",
            "eval_count", "eval_duration",
        ) if k in resp}
    except Exception:
        pass

    # Debug — dump the raw model response BEFORE post-processing so we can see
    # exactly what the model produced (including any embedded tool-call markers
    # in content that would otherwise be stripped by _strip_tool_call_markers).
    _dump_response_to_log(msg)

    content = msg.get("content", "") or ""

    tool_calls = _normalize_tool_calls(msg.get("tool_calls"))
    if not tool_calls:
        # Model/backend may embed tool calls in content
        tool_calls = _extract_tool_calls_from_content(content)

    # Final cleanup: strip tool-call-shaped JSON from content so the UI
    # doesn't render raw {"name":"X","arguments":{...}} blocks inside
    # the assistant's reply. Tool execution itself uses `tool_calls`
    # (built above) — this only sanitizes the prose channel.
    content = _strip_tool_call_markers(content)

    out = {
        "role": msg.get("role", "assistant"),
        "content": content,
        "tool_calls": tool_calls,
    }
    # Carry the perf telemetry through — this return REBUILDS the message dict, which
    # silently dropped _perf on first ship (llm-perf lines logged all zeros).
    if msg.get("_perf"):
        out["_perf"] = msg["_perf"]
    return out

async def call_ollama_embeddings(
    text: str,
    model: Optional[str] = None,
    host: Optional[str] = None,
    timeout_s: Optional[int] = None,
) -> List[float]:
    """
    Call Ollama embeddings endpoint.

    Uses the dedicated embedding model (ORCH_OLLAMA_EMBED_MODEL) rather than
    the chat model, for better similarity-search quality. Falls back to the
    chat model if no embed model is configured.

    Returns a vector (list[float]). Raises RuntimeError on failure.
    """
    s = get_settings()
    used_host = (host or s.ollama_host).rstrip("/")
    used_model = model or s.ollama_embed_model  # dedicated embed model (falls back to chat model)
    used_timeout = int(timeout_s) if timeout_s is not None else int(s.timeout_s)

    payload = {"model": used_model, "prompt": text}

    # Run the blocking HTTP call in a thread so the asyncio event loop stays free.
    # Same pattern as call_ollama_chat — without this, the sync call blocks the
    # event loop and causes silent failures in the command center.
    import asyncio as _asyncio
    import functools as _functools
    _call = _functools.partial(_post_json, f"{used_host}/api/embeddings", payload, timeout_s=used_timeout)
    resp = await _asyncio.get_event_loop().run_in_executor(None, _call)

    emb = resp.get("embedding")
    if not isinstance(emb, list) or not emb:
        raise RuntimeError(f"Ollama embeddings returned no embedding: keys={list(resp.keys())}")

    # ensure floats
    out: List[float] = []
    for v in emb:
        try:
            out.append(float(v))
        except Exception:
            out.append(0.0)
    return out


call_chat = call_ollama_chat
ollama_chat = call_ollama_chat
