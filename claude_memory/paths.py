# SPDX-FileCopyrightText: 2026 GodCC6
# SPDX-FileCopyrightText: 2026 Timothy Redaelli
#
# SPDX-License-Identifier: MIT

"""Claude project-path encoding and memory-directory discovery.

Claude stores per-project state under ``~/.claude/projects/<key>/``, where
``<key>`` is the absolute project path with every character other than an ASCII
letter or digit replaced by ``-``. The encoding is lossy (a real ``-`` in a path
is indistinguishable from a separator), so this module never decodes a key to
make security decisions; it only decodes for human-readable display.
"""

import os
import re
import stat
import time

MEMORY_DIRNAME = "memory"
MEMORY_INDEX_FILENAME = "MEMORY.md"

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")
_INDEX_ENTRY = re.compile(r"^\s*[-*]\s+\[[^\]]+\]\([^)]+\)")
_GITDIR = re.compile(r"^gitdir:\s*(.+)$", re.MULTILINE)


def encode_project_key(absolute_path):
    """``/home/me/.config`` becomes ``-home-me--config``."""
    return _NON_ALNUM.sub("-", absolute_path)


def expand_home(p):
    """Expand a leading ``~`` to the current user's home directory."""
    if p == "~" or p.startswith("~/") or p.startswith("~\\"):
        return os.path.expanduser("~") + p[1:]
    return p


def resolve_claude_home(claude_home=None):
    """Absolute Claude home, default ``~/.claude``."""
    raw = claude_home if claude_home else os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.abspath(expand_home(raw))


def confined_real_path(root, target):
    """Real path of ``target`` when it stays inside ``root``, else None.

    Compares symlink-resolved paths so a symlink cannot smuggle a target out of
    the tree. A missing root or target is None too.
    """
    try:
        real_root = os.path.realpath(root, strict=True)
        real_target = os.path.realpath(target, strict=True)
    except OSError:
        return None
    if real_target == real_root:
        return real_target
    prefix = real_root if real_root.endswith(os.sep) else real_root + os.sep
    return real_target if real_target.startswith(prefix) else None


def confined_file(root, target):
    """``(real_path, stat)`` for a regular file inside ``root``, else None."""
    real = confined_real_path(root, target)
    if real is None:
        return None
    try:
        st = os.stat(real)
    except OSError:
        return None
    return (real, st) if stat.S_ISREG(st.st_mode) else None


def count_index_entries(text):
    """Count ``- [title](file.md) - summary`` pointer lines in a MEMORY.md body."""
    return sum(1 for line in text.split("\n") if _INDEX_ENTRY.match(line))


def list_memory_projects(claude_home):
    """Every project with a ``<home>/projects/<key>/memory/MEMORY.md`` inside the home.

    Returns dicts with ``key``, ``memory_dir``, ``index_file`` and ``mtime``
    (seconds), sorted by key. A memory directory symlinked out of the home is
    skipped.
    """
    projects_root = os.path.join(claude_home, "projects")
    try:
        entries = list(os.scandir(projects_root))
    except OSError:
        return []

    found = []
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        key = entry.name
        memory_dir = os.path.join(projects_root, key, MEMORY_DIRNAME)
        index_file = os.path.join(memory_dir, MEMORY_INDEX_FILENAME)
        confined = confined_file(claude_home, index_file)
        if confined is None:
            continue
        found.append(
            {"key": key, "memory_dir": memory_dir, "index_file": index_file, "mtime": confined[1].st_mtime}
        )
    found.sort(key=lambda p: p["key"])
    return found


def find_git_root(start_dir):
    """Enclosing git repository root, or None.

    Claude files memory under the git repository root, not the raw working
    directory. Both a ``.git`` directory and a ``.git`` file (worktrees and
    submodules) mark a root. A linked worktree resolves to its main repository,
    because the memory store is shared across all worktrees of a repo.
    """
    current = os.path.abspath(expand_home(start_dir))
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return _main_worktree_root(current)
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _main_worktree_root(root):
    """Map a linked worktree to its main repository root; anything else to itself.

    A worktree's ``.git`` file points at ``<main>/.git/worktrees/<name>``, which
    holds a ``commondir`` file pointing back at ``<main>/.git``. A submodule's
    ``.git`` file has no ``commondir``, so a submodule stays its own root.
    """
    try:
        with open(os.path.join(root, ".git"), encoding="utf-8") as f:
            pointer = _GITDIR.search(f.read())
        if pointer is None:
            return root
        git_dir = os.path.normpath(os.path.join(root, pointer.group(1).strip()))
        with open(os.path.join(git_dir, "commondir"), encoding="utf-8") as f:
            common_dir = os.path.normpath(os.path.join(git_dir, f.read().strip()))
        if os.path.basename(common_dir) == ".git":
            return os.path.dirname(common_dir)
        return root
    except OSError:
        # `.git` is a directory, or not a worktree: the directory is the root.
        return root


def _by_recency(p):
    return (-p["mtime"], p["key"])


def select_projects(projects, cwd, git_root=None, include_descendants=True, descendant_limit=12):
    """Pick the project whose memory Claude itself would load for ``cwd``.

    Claude keys memory by the enclosing git repository root when there is one,
    else by the working directory, and never falls back to another project. So
    there is one candidate key: ``git-root`` when it comes from a git root other
    than ``cwd``, ``exact`` when it is ``cwd`` itself, ``none`` when that key has
    no memory.

    ``related`` lists the cwd, ancestor and child projects with memory, newest first, as
    pointers for the tool; their indexes are never injected.

    Returns ``{"current", "related", "match"}``.
    """
    cwd_abs = os.path.abspath(expand_home(cwd))
    cwd_key = encode_project_key(cwd_abs)
    root_abs = os.path.abspath(expand_home(git_root)) if git_root is not None else cwd_abs
    by_key = {p["key"]: p for p in projects}
    limit = max(0, int(descendant_limit))

    current = by_key.get(encode_project_key(root_abs))
    if current is None:
        match = "none"
    else:
        match = "exact" if root_abs == cwd_abs else "git-root"

    related = []
    if include_descendants:
        nearby = [
            p
            for p in projects
            if p["key"] == cwd_key
            or p["key"].startswith(cwd_key + "-")
            or cwd_key.startswith(p["key"] + "-")
        ]
        if current is not None:
            nearby = [p for p in nearby if p["key"] != current["key"]]
        related = sorted(nearby, key=_by_recency)[:limit]
    return {"current": current, "related": related, "match": match}


def format_index_date(mtime):
    """``YYYY-MM-DD`` local date for an mtime in seconds, or ``?``."""
    if not isinstance(mtime, (int, float)) or mtime <= 0:
        return "?"
    return time.strftime("%Y-%m-%d", time.localtime(mtime))


def resolve_key_to_path(key):
    """Recover the real directory for a project key by walking the filesystem.

    The encoding is lossy, so a naive decode turns ``...-quality-pilot`` into
    ``.../quality/pilot``. Walking the real tree and preferring the longest
    matching join recovers the true path when it still exists.
    """
    if not key.startswith("-"):
        return None  # Windows drive keys have no leading dash
    segments = key[1:].split("-")
    current = os.sep
    index = 0
    while index < len(segments):
        for take in range(len(segments) - index, 0, -1):
            candidate = os.path.join(current, "-".join(segments[index : index + take]))
            if os.path.isdir(candidate):
                current = candidate
                index += take
                break
        else:
            return None
    return current


def project_label(key):
    """Short label: the real directory name when recoverable, else the key's last two segments."""
    real = resolve_key_to_path(key)
    if real is not None:
        return os.path.basename(real) or key
    segments = key.removeprefix("-").split("-")
    return "-".join(segments[-2:])
