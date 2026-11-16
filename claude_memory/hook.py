# SPDX-FileCopyrightText: 2026 Timothy Redaelli
#
# SPDX-License-Identifier: MIT

"""``session_start`` hook: inject Claude memory into the session.

Reads the hook payload on stdin, records the session folder for the MCP
server, and prints the global instructions, the project's memory index and
(when writing is on) the save instructions as ``additional_context``, which
Vibe adds to the next user message for the model.

A failure prints nothing useful and exits non-zero: Vibe shows a warning and
the session goes on without the memory.
"""

import json
import os
import sys

if __package__ in (None, ""):
    # Run as a script under `python3 -I`, which keeps the script directory off
    # sys.path: add the plugin root so the package imports resolve.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_memory.config import load_config  # noqa: E402
from claude_memory.context import build_context  # noqa: E402
from claude_memory.session import record_cwd  # noqa: E402


def main():
    try:
        payload = json.loads(sys.stdin.buffer.read() or b"{}")
    except ValueError:
        payload = {}
    config = load_config()
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    if isinstance(cwd, str) and cwd:
        cwd = os.path.abspath(cwd)
        try:
            record_cwd(cwd)
        except OSError:
            pass  # the tool falls back to its own cwd; the context still goes out
    else:
        cwd = config["cwd"] or os.getcwd()

    context, view = build_context(config, cwd)
    if not context:
        return
    current = view["current"]
    status = f"{current['key']} ({view['match']})" if current is not None else "no project memory"
    out = {"system_message": status, "hook_specific_output": {"additional_context": context}}
    # ASCII-only JSON: Vibe reads stdout as UTF-8 whatever the locale encoding is.
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main()
