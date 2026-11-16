# SPDX-FileCopyrightText: 2026 GodCC6
# SPDX-FileCopyrightText: 2026 Timothy Redaelli
#
# SPDX-License-Identifier: MIT

"""Build the memory view for one working directory.

Both entry points are short-lived (the hook runs once per session start, the
server rebuilds per tool call), so there is no cache: a rebuild is a directory
scan plus a few small reads.
"""

import os

from .paths import (
    MEMORY_DIRNAME,
    encode_project_key,
    find_git_root,
    format_index_date,
    list_memory_projects,
    select_projects,
)
from .redact import redact_text
from .render import render_global_block, render_memory_block, render_write_instructions
from .store import read_instructions_with_imports, read_project_index


def write_target(claude_home, cwd, git_root):
    """Where saves for ``cwd`` land: ``{"key", "memory_dir"}``.

    The same rule the store's other writer uses: the git repository root (the
    main worktree for a linked one, as ``find_git_root`` returns it), else the
    directory itself. Never a guessed neighbour, so a save cannot end up in
    another project's memory.
    """
    key = encode_project_key(git_root or cwd)
    return {"key": key, "memory_dir": os.path.join(claude_home, "projects", key, MEMORY_DIRNAME)}


def index_entries(claude_home, project):
    """Pointer count in a project's MEMORY.md, 0 when unreadable."""
    read = read_project_index(claude_home, project)
    return read["entries"] if read is not None else 0


def resolve(config, cwd):
    """Projects annotated with dates, plus the selection for ``cwd``.

    Only ``related`` projects get an ``entries`` count: reading every index
    just to count it would cost one file read per project on every call.

    Returns ``{"projects", "current", "related", "match", "git_root"}``.
    """
    home = config["claude_home"]
    projects = list_memory_projects(home)
    for p in projects:
        p["date"] = format_index_date(p["mtime"])
    # Claude files memory under the enclosing git repository root, so a
    # subdirectory of a repo must resolve to the same project Claude used.
    git_root = find_git_root(cwd)
    selection = select_projects(
        projects,
        cwd,
        git_root=git_root,
        include_descendants=config["include_descendants"],
        descendant_limit=config["descendant_limit"],
    )
    for p in selection["related"]:
        p["entries"] = index_entries(home, p)
    return {"projects": projects, "git_root": git_root, **selection}


def build_context(config, cwd):
    """``(text, view)``: injected text (global, memory, write instructions) and the ``resolve`` view."""
    home = config["claude_home"]
    mode = config["redact_mode"]
    view = resolve(config, cwd)
    blocks = []

    if config["enable_global_instructions"]:
        instructions = read_instructions_with_imports(home, os.path.join(home, "CLAUDE.md"))
        hits = {}
        if instructions is not None:
            text, hits = redact_text(instructions["text"], mode)
            instructions = {**instructions, "text": text}
        blocks.append(render_global_block(instructions, config["max_global_bytes"], hits))

    if config["enable_memory"]:
        current = view["current"]
        index = read_project_index(home, current) if current is not None else None
        hits = {}
        if index is not None:
            text, hits = redact_text(index["text"], mode)
            index = {**index, "text": text}
        blocks.append(
            render_memory_block(
                project=current,
                index=index,
                related=view["related"],
                all_projects=view["projects"],
                max_bytes=config["max_index_bytes"],
                match=view["match"],
                git_root=view["git_root"],
                hits=hits,
                writable=config["enable_write"],
            )
        )

    if config["enable_write"]:
        target = write_target(home, cwd, view["git_root"])
        exists = any(p["key"] == target["key"] for p in view["projects"])
        blocks.append(render_write_instructions(target["key"], exists))

    return "\n\n".join(b for b in blocks if b), view
