#!/usr/bin/env python3
"""Stdio MCP server: Claude plan usage (shared with the menu bar app, refreshed on
demand) and the calling Claude Code session's context size. Standard library only.

    claude mcp add usagebar -s user -- python3 /path/to/mcp_server.py
"""

import fcntl
import glob
import json
import os
import re
import sys
import time
from datetime import datetime

import monitor
from monitor import __version__

TOOLS = [
    {
        "name": "get_usage",
        "description": "Claude plan quota and this session's context in one call, for planning long work. "
                       "plan: percent used and reset time for the 5-hour session window and each weekly "
                       "quota; refetched (about 2s) when older than max_age_minutes, fetched_at says when. "
                       "context: tokens in the calling Claude Code session's context, the model's max "
                       "window, and percent used.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_age_minutes": {"type": "number", "minimum": 0, "default": 2,
                                    "description": "Reuse plan data up to this old; 0 forces a fresh fetch."},
                "session_id": {"type": "string", "description": "Defaults to the calling session."},
            },
        },
    },
]


class ToolError(Exception):
    """Reported to the agent as a failed tool call, not a protocol error."""


def read_state():
    try:
        with open(monitor.STATE_PATH) as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return None


def get_usage(args):
    max_age = args.get("max_age_minutes", 2)
    if not isinstance(max_age, (int, float)) or max_age < 0:
        raise ToolError("max_age_minutes must be a number >= 0")
    os.makedirs(os.path.dirname(monitor.STATE_PATH), exist_ok=True)
    with open(monitor.STATE_PATH + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # many agents asking at once share one claude run
        state = read_state()
        fetched = isinstance(state, dict) and state.get("fetched_at")
        if not fetched or time.time() - datetime.fromisoformat(fetched).timestamp() > max_age * 60:
            monitor.save_state(*monitor.fetch())
            state = read_state()
    if not state:
        raise ToolError("Could not fetch usage; see %s" % monitor.LOG_PATH)
    return state


def find_transcript(session_id):
    session_id = session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if session_id:
        if not re.fullmatch(r"[\w-]+", session_id):  # keeps it inside PROJECTS
            raise ToolError("Invalid session_id")
        return monitor.transcript_path(session_id)
    # shortcut: newest transcript for this cwd; wrong if two sessions share a project dir and the env var is missing
    files = glob.glob(os.path.join(monitor.PROJECTS, re.sub(r"[^A-Za-z0-9]", "-", os.getcwd()), "*.jsonl"))
    return max(files, key=os.path.getmtime, default=None)


def get_context_size(args):
    path = find_transcript(args.get("session_id"))
    if not path:
        raise ToolError("Transcript not found")
    found = monitor.last_usage(path)
    if not found:
        raise ToolError("No replies in this transcript yet")
    usage, model = found
    tokens = monitor.context_tokens(usage)
    window = monitor.WINDOWS.get(model)  # None for a model not in the table
    return {
        "session_id": os.path.basename(path)[:-len(".jsonl")],
        "model": model,
        "context_tokens": tokens,
        "window_tokens": window,
        "percent_used": round(tokens * 100 / window, 1) if window else None,
    }


def get_all(args):
    """Each part fails on its own, so a missing state file still returns the context."""
    out = {}
    for key, fn in (("plan", get_usage), ("context", get_context_size)):
        try:
            out[key] = fn(args)
        except (ToolError, OSError) as exc:
            out[key] = {"error": str(exc)}
    return out


HANDLERS = {"get_usage": get_all}


def handle(req):
    """Returns (result, error) for one JSON-RPC request."""
    method, params = req.get("method"), req.get("params") or {}
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "usagebar", "version": __version__},
        }, None
    if method == "ping":
        return {}, None
    if method == "tools/list":
        return {"tools": TOOLS}, None
    if method == "tools/call":
        fn = HANDLERS.get(params.get("name"))
        if not fn:
            return None, {"code": -32602, "message": "Unknown tool: %s" % params.get("name")}
        try:
            text, is_error = json.dumps(fn(params.get("arguments") or {}), indent=1), False
        except (ToolError, OSError) as exc:
            text, is_error = str(exc), True
        return {"content": [{"type": "text", "text": text}], "isError": is_error}, None
    return None, {"code": -32601, "message": "Method not found: %s" % method}


def serve():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except ValueError:
            req, (result, error) = {"id": None}, (None, {"code": -32700, "message": "Parse error"})
        else:
            if not isinstance(req, dict) or "id" not in req:  # notification: no reply
                continue
            result, error = handle(req)
        reply = {"jsonrpc": "2.0", "id": req["id"]}
        reply.update({"error": error} if error else {"result": result})
        print(json.dumps(reply), flush=True)


def test():
    import tempfile
    PROJECTS = monitor.PROJECTS = tempfile.mkdtemp()
    monitor.STATE_PATH = os.path.join(PROJECTS, "state", "last.json")
    calls = []
    monitor.fetch = lambda: calls.append(1) or ("Current session: 5% used", None)
    assert get_usage({})["quotas"]["Current session"]["percent_used"] == 5 and len(calls) == 1
    get_usage({})  # fresh enough: no second fetch
    assert len(calls) == 1
    get_usage({"max_age_minutes": 0})
    assert len(calls) == 2
    for bad in (-1, "5"):
        try:
            get_usage({"max_age_minutes": bad})
            raise AssertionError(bad)
        except ToolError:
            pass
    os.makedirs(os.path.join(PROJECTS, "-proj"))
    rows = [
        {"type": "assistant", "isSidechain": False, "message": {"model": "m1", "usage": {
            "input_tokens": 2, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 100, "output_tokens": 5}}},
        {"type": "assistant", "isSidechain": True, "message": {"model": "sub", "usage": {"input_tokens": 999}}},
        {"type": "assistant", "message": {"model": "<synthetic>", "usage": {"input_tokens": 0}}},
        {"type": "user", "message": {"content": "hi"}},
    ]
    with open(os.path.join(PROJECTS, "-proj", "abc-1.jsonl"), "w") as fh:
        fh.write("\n".join(json.dumps(r) for r in rows) + '\n{"type":"assistant", "trunc')
    assert get_context_size({"session_id": "abc-1"}) == {
        "session_id": "abc-1", "model": "m1", "context_tokens": 117, "window_tokens": None, "percent_used": None}
    rows[0]["message"]["model"] = "claude-opus-5-5"
    rows[0]["message"]["usage"]["cache_read_input_tokens"] = 69_993
    with open(os.path.join(PROJECTS, "-proj", "abc-2.jsonl"), "w") as fh:
        fh.write(json.dumps(rows[0]))
    got = get_context_size({"session_id": "abc-2"})
    assert (got["window_tokens"], got["percent_used"]) == (1_000_000, 7.0), got
    for bad in ("../x", "nope"):
        try:
            get_context_size({"session_id": bad})
            raise AssertionError(bad)
        except ToolError:
            pass

    assert handle({"method": "tools/list"})[0]["tools"] == TOOLS
    assert handle({"method": "bogus"})[1]["code"] == -32601
    result, _ = handle({"method": "tools/call", "params": {"name": "get_usage", "arguments": {"session_id": "abc-2"}}})
    both = json.loads(result["content"][0]["text"])
    assert both["context"]["percent_used"] == 7.0 and "plan" in both, both
    both = get_all({"session_id": "x/y"})
    assert both["context"] == {"error": "Invalid session_id"}, both
    print("ok")


if __name__ == "__main__":
    test() if "--test" in sys.argv else serve()
