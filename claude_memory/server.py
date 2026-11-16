# SPDX-FileCopyrightText: 2026 Timothy Redaelli
#
# SPDX-License-Identifier: MIT

"""Minimal MCP stdio server exposing the ``claude_memory`` tool.

Speaks newline-delimited JSON-RPC 2.0 on stdin/stdout with only the methods a
tool server needs: ``initialize``, ``ping``, ``tools/list`` and ``tools/call``.
Stdlib only, so the plugin installs by copying a directory.

Config and the session folder are re-read on every call: the hook may record
the folder after the server started, and edits to the config file apply
without restarting the session.
"""

import json
import os
import sys

if __package__ in (None, ""):
    # Run as a script under `python3 -I`, which keeps the script directory off
    # sys.path: add the plugin root so the package imports resolve.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_memory.config import load_config  # noqa: E402
from claude_memory.session import session_cwd  # noqa: E402
from claude_memory.tool import TOOL_NAME, run, tool_definition  # noqa: E402

DEFAULT_PROTOCOL = "2025-06-18"
SERVER_INFO = {"name": "claude-memory", "version": "1.0.0"}


def _result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle(message):
    """Response dict for one request, or None for a notification."""
    if not isinstance(message, dict):
        return _error(None, -32600, "invalid request")
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}
    if "id" not in message:
        return None  # notifications (initialized, cancelled) need no answer

    if method == "initialize":
        version = params.get("protocolVersion") if isinstance(params, dict) else None
        return _result(
            msg_id,
            {
                "protocolVersion": version if isinstance(version, str) else DEFAULT_PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        config = load_config()
        tools = [tool_definition(config["enable_write"])] if config["enable_tool"] else []
        return _result(msg_id, {"tools": tools})
    if method == "tools/call":
        config = load_config()
        if not isinstance(params, dict) or params.get("name") != TOOL_NAME or not config["enable_tool"]:
            return _error(msg_id, -32602, f"unknown tool {params.get('name') if isinstance(params, dict) else None!r}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        try:
            text = run(args, config, session_cwd(config))
            is_error = False
        except Exception as e:  # report to the model instead of killing the server
            text = f"claude_memory failed: {e}"
            is_error = True
        return _result(msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error})
    return _error(msg_id, -32601, f"method not found: {method}")


def _handle_safely(message):
    """``handle`` that answers an internal error instead of ending the server."""
    try:
        return handle(message)
    except Exception as e:  # config or cwd lookup failing outside the tool call
        msg_id = message.get("id") if isinstance(message, dict) else None
        return _error(msg_id, -32603, f"internal error: {e}")


def main():
    # Bytes in (json detects UTF-8), ASCII-only JSON out: the client speaks
    # UTF-8 whatever the locale encoding of this process is.
    for line in sys.stdin.buffer:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = _error(None, -32700, "parse error")
        else:
            if isinstance(message, list):
                responses = [r for r in map(_handle_safely, message) if r is not None]
                response = responses or None
            else:
                response = _handle_safely(message)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
