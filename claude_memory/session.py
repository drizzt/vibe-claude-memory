# SPDX-FileCopyrightText: 2026 Timothy Redaelli
#
# SPDX-License-Identifier: MIT

"""Hand the session working directory from the hook to the MCP server.

Vibe starts a plugin's MCP server with its cwd inside the plugin root, so the
server cannot see the session folder. The ``session_start`` hook can (it is
the ``cwd`` field of its stdin payload), and both processes share the
session's ``PLUGIN_DATA`` directory, so the hook records the folder there.
"""

import json
import os

from .store import write_atomic

SESSION_FILE = "session.json"


def _session_path():
    data = os.environ.get("PLUGIN_DATA")
    return os.path.join(data, SESSION_FILE) if data else None


def record_cwd(cwd):
    """Store ``cwd`` for the server; silently skipped without ``PLUGIN_DATA``."""
    path = _session_path()
    if path is None:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_atomic(path, json.dumps({"cwd": cwd}))


def session_cwd(config):
    """Session folder: recorded by the hook, else config ``cwd``, else the process cwd."""
    path = _session_path()
    if path is not None:
        try:
            with open(path, encoding="utf-8") as f:
                cwd = json.load(f).get("cwd")
            if isinstance(cwd, str) and cwd:
                return os.path.abspath(cwd)
        except (OSError, ValueError, AttributeError):
            pass
    return config["cwd"] or os.getcwd()
