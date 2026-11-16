# SPDX-FileCopyrightText: 2026 GodCC6
# SPDX-FileCopyrightText: 2026 Timothy Redaelli
#
# SPDX-License-Identifier: MIT

"""Synthetic Claude home for the test suite.

The tests never touch a real ``~/.claude``. ``build_fixture`` creates a
disposable tree reproducing every layout the plugin cares about::

    <root>/home/                  claude_home
      CLAUDE.md                   global instructions with an @import
      imported.md                 the imported file
      projects/<key>/memory/      one memory set per synthetic project
    <root>/ws                     cwd that has its own memory (exact)
    <root>/ws-child               child of <root>/ws
    <root>/repo/.git              synthetic git root
    <root>/repo/ws                cwd whose memory lives at the repo root
    <root>/hub, hub-a, hub-b      no-memory hub with two children
"""

import datetime
import os
import secrets
import shutil
import tempfile
from types import SimpleNamespace

from claude_memory.paths import MEMORY_DIRNAME, MEMORY_INDEX_FILENAME, encode_project_key
from claude_memory.store import INDEX_SEPARATOR as SEP


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _ts(iso):
    return datetime.datetime.fromisoformat(iso).replace(tzinfo=datetime.timezone.utc).timestamp()


def _create_project(projects_root, project_path, entries, topics=None, mtime=None):
    key = encode_project_key(project_path)
    memory_dir = os.path.join(projects_root, key, MEMORY_DIRNAME)
    os.makedirs(memory_dir)
    body = "\n".join(f"- [{t}]({f}){SEP}{s}" for t, f, s in entries)
    index_file = os.path.join(memory_dir, MEMORY_INDEX_FILENAME)
    _write(index_file, body + "\n")
    for name, text in (topics or {}).items():
        _write(os.path.join(memory_dir, name), text)
    if mtime is not None:
        os.utime(index_file, (mtime, mtime))
    return SimpleNamespace(key=key, dir=memory_dir, index_file=index_file)


def build_fixture():
    # Alphanumeric only: key-to-path recovery cannot round-trip a "_" or "-".
    root = os.path.join(os.path.realpath(tempfile.gettempdir()), "vibeclaudememory" + secrets.token_hex(6))
    os.makedirs(root)
    fx = SimpleNamespace(root=root)
    fx.home = os.path.join(root, "home")
    fx.projects_root = os.path.join(fx.home, "projects")
    fx.ws = os.path.join(root, "ws")
    fx.ws_child = os.path.join(root, "ws-child")
    fx.repo = os.path.join(root, "repo")
    fx.repo_ws = os.path.join(fx.repo, "ws")
    fx.hub = os.path.join(root, "hub")
    fx.hub_a = os.path.join(root, "hub-a")
    fx.hub_b = os.path.join(root, "hub-b")
    for d in [fx.projects_root, fx.ws, fx.ws_child, os.path.join(fx.repo, ".git"), fx.repo_ws, fx.hub, fx.hub_a, fx.hub_b]:
        os.makedirs(d, exist_ok=True)

    _write(os.path.join(fx.home, "CLAUDE.md"), "# Personal preferences\n\nAlways answer in Chinese.\n\n@imported.md\n")
    _write(os.path.join(fx.home, "imported.md"), "# Imported guidance\n\nMarker: IMPORTED_MARKER_OK\n")

    old = _ts("2026-01-01T00:00:00")
    mid = _ts("2026-06-01T00:00:00")
    fresh = _ts("2026-09-01T00:00:00")

    alpha = "\n".join(
        [
            "---",
            "name: alpha",
            "description: alpha topic",
            "metadata:",
            "  type: feedback",
            "---",
            "",
            "# Alpha",
            "",
            "Plain body line.",
            "openai_key = sk-abcdefghijklmnopqrstuvwx",
            "password: hunter2secret",
            "",
        ]
    )
    fx.ws_project = _create_project(
        fx.projects_root,
        fx.ws,
        [("Alpha", "alpha.md", "first topic"), ("Beta", "beta.md", "second topic")],
        {
            "alpha.md": alpha,
            "beta.md": "---\nname: beta\n---\n\n# Beta\n\nsecond body\n",
            # Not in the index: only search and read reach it.
            "key.md": "# Key\n-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
            "QyNTUxOQAAACBnh5Yxq3wNYbJm0aLcm0lOcCAQXJl6ZgWm8iN1xgZ5ZgAAAJjc0Gzf3NBs\n"
            "-----END OPENSSH PRIVATE KEY-----\n\nafter the key\n",
        },
        fresh,
    )
    fx.ws_child_project = _create_project(
        fx.projects_root, fx.ws_child, [("Child", "child.md", "child topic")], {"child.md": "# Child\n\nchild body\n"}, mid
    )
    fx.repo_project = _create_project(
        fx.projects_root, fx.repo, [("Repo", "repo.md", "repo-level topic")], {"repo.md": "# Repo\n\nrepo body\n"}, mid
    )
    fx.hub_a_project = _create_project(fx.projects_root, fx.hub_a, [("Hub A", "a.md", "older sibling")], {}, old)
    fx.hub_b_project = _create_project(fx.projects_root, fx.hub_b, [("Hub B", "b.md", "newer sibling")], {}, fresh)
    return fx


def dispose_fixture(fx):
    shutil.rmtree(fx.root, ignore_errors=True)


def make_config(fx, **overrides):
    """A config dict like ``load_config`` returns, pointed at the fixture."""
    from claude_memory.config import DEFAULTS

    config = dict(DEFAULTS, claude_home=fx.home)
    config.update(overrides)
    return config
