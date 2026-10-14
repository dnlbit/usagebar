#!/usr/bin/env python3
"""Stdio MCP server: Claude plan usage (as last polled by monitor.py) and the
calling Claude Code session's context size. Standard library only.

    claude mcp add usagebar -s user -- python3 /path/to/mcp_server.py
"""

import glob
import json
import os
import re
import sys

from monitor import STATE_PATH, __version__

PROJECTS = os.path.expanduser("~/.claude/projects")
# Model max context windows (Anthropic docs). Claude Code auto-compacts earlier (500k for Opus 5.5).
WINDOWS = {
    model: 1_000_000 for model in (
        "claude-fable-5-1", "claude-fable-5", "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8",
        "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6",
        "claude-haiku-5-5",
    )
}

TOOLS = [
    {
        "name": "get_usage",
        "description": "Claude plan quota and this session's context in one call. plan: percent used and "
                       "reset time for the 5-hour session window and the week, polled by the menu bar "
                       "monitor every 5-20 min (fetched_at says how fresh). context: tokens in the calling "
                       "Claude Code session's context, the model's max window, and percent used.",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string", "description": "Defaults to the calling session."}},
        },
    },
]


class ToolError(Exception):
    """Reported to the agent as a failed tool call, not a protocol error."""


def get_usage(_):
    try:
        with open(STATE_PATH) as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise ToolError("No usage recorded yet. Is the menu bar app (monitor.py) running?")


def find_transcript(session_id):
    session_id = session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if session_id:
        if not re.fullmatch(r"[\w-]+", session_id):  # keeps it inside PROJECTS
            raise ToolError("Invalid session_id")
        hits = glob.glob(os.path.join(PROJECTS, "*", session_id + ".jsonl"))
        return hits[0] if hits else None
    # shortcut: newest transcript for this cwd; wrong if two sessions share a project dir and the env var is missing
    files = glob.glob(os.path.join(PROJECTS, re.sub(r"[^A-Za-z0-9]", "-", os.getcwd()), "*.jsonl"))
    return max(files, key=os.path.getmtime, default=None)


def get_context_size(args):
    path = find_transcript(args.get("session_id"))
    if not path:
        raise ToolError("Transcript not found")
    usage = model = None
    # shortcut: reads the whole transcript per call; seek from the end if multi-MB files get slow
    with open(path) as fh:
        for line in fh:
            if '"assistant"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:  # last line may still be mid-write
                continue
            msg = entry.get("message") or {}
            if (entry.get("type") == "assistant" and not entry.get("isSidechain")
                    and msg.get("usage") and msg.get("model") != "<synthetic>"):
                usage, model = msg["usage"], msg.get("model")
    if not usage:
        raise ToolError("No replies in this transcript yet")
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    tokens = sum(usage.get(k) or 0 for k in keys)
    window = WINDOWS.get(model)  # None for a model not in the table
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
    global PROJECTS
    PROJECTS = tempfile.mkdtemp()
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
