# SPDX-FileCopyrightText: 2026 GodCC6
# SPDX-FileCopyrightText: 2026 Timothy Redaelli
#
# SPDX-License-Identifier: MIT

"""Plugin configuration.

Sources, later wins: built-in defaults, ``$VIBE_HOME/claude-memory.toml``
(``VIBE_HOME`` defaults to ``~/.vibe``), then ``CLAUDE_MEMORY_<FIELD>``
environment variables (for example ``CLAUDE_MEMORY_ENABLE_WRITE=1``).
Invalid values fall back to the default instead of failing the session.
"""

import os
import tomllib

from .paths import resolve_claude_home

DEFAULTS = {
    "claude_home": None,
    "cwd": None,
    "include_descendants": True,
    "descendant_limit": 12,
    "max_index_bytes": 24000,
    "max_global_bytes": 6000,
    "enable_memory": True,
    "enable_global_instructions": True,
    "enable_tool": True,
    "enable_write": False,
    "redact_mode": "on",
}

REDACT_MODES = ("on", "report", "off")
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def config_file():
    vibe_home = os.environ.get("VIBE_HOME") or os.path.join(os.path.expanduser("~"), ".vibe")
    return os.path.join(os.path.expanduser(vibe_home), "claude-memory.toml")


def _from_env(field, default):
    """Parse an env string to the type of the field's default."""
    raw = os.environ.get("CLAUDE_MEMORY_" + field.upper())
    if raw is None:
        return None
    if isinstance(default, bool):
        low = raw.strip().lower()
        return True if low in _TRUE else False if low in _FALSE else None
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return None
    return raw


def _valid(field, value):
    default = DEFAULTS[field]
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if field == "redact_mode":
        return value in REDACT_MODES
    return isinstance(value, str) and value != ""


def load_config(path=None):
    """Fully defaulted config dict with ``claude_home`` resolved."""
    raw = {}
    try:
        with open(path or config_file(), "rb") as f:
            raw = tomllib.load(f)
    except (OSError, ValueError):  # TOMLDecodeError, or bytes that are not UTF-8
        raw = {}

    config = dict(DEFAULTS)
    for field, default in DEFAULTS.items():
        if field in raw and _valid(field, raw[field]):
            config[field] = raw[field]
        env = _from_env(field, default)
        if env is not None and _valid(field, env):
            config[field] = env

    config["claude_home"] = resolve_claude_home(config["claude_home"])
    if config["cwd"] is not None:
        config["cwd"] = os.path.abspath(os.path.expanduser(config["cwd"]))
    # Writing needs the tool; without it there is no way to save.
    config["enable_write"] = config["enable_tool"] and config["enable_write"]
    return config
