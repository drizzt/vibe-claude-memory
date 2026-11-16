# SPDX-FileCopyrightText: 2026 GodCC6
# SPDX-FileCopyrightText: 2026 Timothy Redaelli
#
# SPDX-License-Identifier: MIT

"""Access to Claude's memory store.

Every read is confined to the resolved Claude home: a path is only opened after
its real (symlink-resolved) location is proven to live inside that home. Writes
(``save_memory``, ``delete_memory``) are opt-in through ``enable_write`` and
touch only one project memory directory.
"""

import json
import os
import re
import time

from .paths import MEMORY_INDEX_FILENAME, confined_file, confined_real_path, count_index_entries, expand_home
from .redact import redact_text

MAX_IMPORT_DEPTH = 3
MAX_FILE_BYTES = 4 * 1024 * 1024

MEMORY_TYPES = ["user", "feedback", "project", "reference"]

# A topic file name: one plain `.md` basename, never the index.
_TOPIC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.md$")
# Index pointer separator: U+2014 between spaces, as the store's own entries use.
INDEX_SEPARATOR = " " + chr(0x2014) + " "
_IMPORT_LINE = re.compile(r"^[ \t]*@([^\s@]+)[ \t]*$", re.MULTILINE)
_POINTER_START = re.compile(r"^\s*[-*]\s+\[")
_YAML_PLAIN = re.compile(r"^[A-Za-z_][^:#]*$")
_YAML_KEYWORDS = ("true", "false", "yes", "no", "on", "off", "null")


def read_confined(root, path):
    """``{"text", "mtime", "size"}`` for a regular file inside ``root``, else None."""
    confined = confined_file(root, path)
    if confined is None:
        return None
    real, st = confined
    if st.st_size > MAX_FILE_BYTES:
        return None
    try:
        with open(real, encoding="utf-8", errors="replace") as f:
            return {"text": f.read(), "mtime": st.st_mtime, "size": st.st_size}
    except OSError:
        return None


def read_project_index(claude_home, project):
    """A project's MEMORY.md as ``{"text", "mtime", "size", "entries"}``, or None."""
    read = read_confined(claude_home, project["index_file"])
    if read is None:
        return None
    read["entries"] = count_index_entries(read["text"])
    return read


def list_topic_files(claude_home, project):
    """Topic files beside a project's index, sorted by name."""
    try:
        names = os.listdir(project["memory_dir"])
    except OSError:
        return []
    out = []
    for name in names:
        if not name.endswith(".md") or name == MEMORY_INDEX_FILENAME:
            continue
        path = os.path.join(project["memory_dir"], name)
        confined = confined_file(claude_home, path)
        if confined is None:
            continue
        st = confined[1]
        out.append({"name": name, "file": path, "mtime": st.st_mtime, "size": st.st_size})
    out.sort(key=lambda t: t["name"])
    return out


def _resolve_import(from_file, reference):
    """Resolve an ``@relative``, ``@~/x`` or ``@/abs`` import; a bare name gets ``.md``."""
    expanded = expand_home(reference)
    if os.path.isabs(expanded):
        return expanded
    if "." not in expanded:
        expanded += ".md"
    return os.path.normpath(os.path.join(os.path.dirname(from_file), expanded))


def read_instructions_with_imports(claude_home, path):
    """Read a global instructions file with its ``@import`` lines inlined.

    Returns ``{"text", "files"}`` or None when nothing was readable.
    Imports must resolve inside the Claude home; a missing or out-of-tree
    import leaves its ``@line`` in place.
    """
    seen = set()
    files = []

    def expand(current, depth):
        real = confined_real_path(claude_home, current)
        if real is None or real in seen or depth > MAX_IMPORT_DEPTH:
            return ""
        seen.add(real)
        read = read_confined(claude_home, real)
        if read is None:
            return ""
        files.append(real)

        def inline(m):
            imported = expand(_resolve_import(real, m.group(1)), depth + 1)
            return imported if imported else m.group(0)

        return _IMPORT_LINE.sub(inline, read["text"])

    text = expand(path, 0)
    if not files:
        return None
    return {"text": text, "files": files}


def read_topic(claude_home, project, name):
    """``{"text", "name"}`` for one topic file by plain name, or None."""
    if "/" in name or "\\" in name or "\0" in name:
        return None
    read = read_confined(claude_home, os.path.join(project["memory_dir"], name))
    if read is None:
        return None
    return {"text": read["text"], "name": name}


def search_topics(claude_home, project, query, limit=40, redact_mode="off"):
    """Case-insensitive line search across a project's topic files.

    Each file is redacted whole before it is split into lines: a multi-line
    secret (a PEM key) only matches its rule as one block, and a query hitting
    its body lines would otherwise return them unmasked.
    """
    needle = query.lower()
    out = []
    for topic in list_topic_files(claude_home, project):
        read = read_confined(claude_home, topic["file"])
        if read is None:
            continue
        text, _ = redact_text(read["text"], redact_mode)
        for i, line in enumerate(text.split("\n")):
            if needle in line.lower():
                out.append({"file": topic["name"], "line": i + 1, "text": line.strip()})
                if len(out) >= limit:
                    return out
    return out


def find_project(projects, selector):
    """Project by exact key, key without its leading dash, or unique substring."""
    if not isinstance(selector, str) or not selector:
        return None
    needle = selector.lower()
    for p in projects:
        if p["key"].lower() == needle:
            return p
    for p in projects:
        if p["key"].lower() == "-" + needle:
            return p
    matches = [p for p in projects if needle in p["key"].lower()]
    return matches[0] if len(matches) == 1 else None


def _one_line(value):
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def _yaml_scalar(value):
    """A frontmatter value: plain when YAML reads it back as the same string.

    Anything else (``a: b``, a leading indicator or digit, ``#``, ``true``) is
    written as a JSON string, which is a valid YAML double-quoted scalar.
    """
    if _YAML_PLAIN.match(value) and value.lower() not in _YAML_KEYWORDS:
        return value
    return json.dumps(value, ensure_ascii=False)


def write_atomic(path, text):
    """A reader sees the old or the new body, never half a file."""
    tmp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        # Do not leave a stray temp file next to the memory files.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _prepare_memory_dir(claude_home, memory_dir):
    """Create the directory and prove it lives inside the home; error text or None.

    The nearest existing ancestor below the home is checked first, so a
    symlinked ``projects/<key>`` cannot get directories created outside it.
    """
    existing = memory_dir
    while not os.path.exists(existing) and os.path.dirname(existing) != existing:
        existing = os.path.dirname(existing)
    below_home = existing == claude_home or existing.startswith(claude_home.rstrip(os.sep) + os.sep)
    if below_home and confined_real_path(claude_home, existing) is None:
        return f"{memory_dir} resolves outside {claude_home}"
    try:
        os.makedirs(memory_dir, exist_ok=True)
    except OSError as e:
        return f"cannot create {memory_dir}: {e}"
    if confined_real_path(claude_home, memory_dir) is None:
        return f"{memory_dir} resolves outside {claude_home}"
    return None


def _check_topic_name(name):
    if not isinstance(name, str) or not _TOPIC_NAME.match(name) or name == MEMORY_INDEX_FILENAME:
        return f'invalid file {json.dumps(name)}: use a plain name like "feedback-testing.md"'
    return None


def _load_index(claude_home, index_file):
    if not os.path.exists(index_file):
        return "# Memory Index\n"
    read = read_confined(claude_home, index_file)
    if read is None:
        raise OSError(f"{index_file} is unreadable or outside the Claude home")
    return read["text"]


def _links_to(line, name):
    return _POINTER_START.match(line) is not None and f"]({name})" in line


def _strip_read_wrapper(body, file):
    """Drop what ``read`` adds around a body: its ``# <key>: <file>`` header and
    the stored frontmatter.

    The update flow is read, then save the whole body; a model that passes the
    read output back unchanged would otherwise nest the old header and
    frontmatter inside the new file.
    """
    text = body.strip()
    first, _, rest = text.partition("\n")
    if first.startswith("# ") and first.endswith(f": {file}"):
        text = rest.strip()
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end >= 0 and text[end + 4 : end + 5] in ("", "\n"):
            text = text[end + 4 :].strip()
    return text


def save_memory(claude_home, memory_dir, file, name, description, type, body):
    """Write one topic file and upsert its index line.

    Every other index line is kept byte for byte, so hand-written ordering and
    notes survive. Saving an existing file replaces it whole.
    Returns ``(ok, text)``.
    """
    error = _check_topic_name(file)
    if error is not None:
        return False, error
    if type not in MEMORY_TYPES:
        return False, f"type must be one of {', '.join(MEMORY_TYPES)}"
    title = _one_line(name)
    summary = _one_line(description)
    if isinstance(body, str):
        body = _strip_read_wrapper(body, file)
    if not title or not summary or not isinstance(body, str) or not body.strip():
        return False, 'save requires non-empty "name", "description" and "body"'
    error = _prepare_memory_dir(claude_home, memory_dir)
    if error is not None:
        return False, error

    topic = "\n".join(
        [
            "---",
            f"name: {_yaml_scalar(title)}",
            f"description: {_yaml_scalar(summary)}",
            "metadata:",
            f"  type: {type}",
            "---",
            "",
            body.strip(),
            "",
        ]
    )
    index_file = os.path.join(memory_dir, MEMORY_INDEX_FILENAME)
    pointer = f"- [{title}]({file}){INDEX_SEPARATOR}{summary}"
    try:
        lines = _load_index(claude_home, index_file).split("\n")
        at = next((i for i, line in enumerate(lines) if _links_to(line, file)), -1)
        if at >= 0:
            lines[at] = pointer
        else:
            while lines and lines[-1] == "":
                lines.pop()
            # Keep a blank line between a heading and the first pointer.
            if lines and lines[-1].startswith("#"):
                lines.append("")
            lines += [pointer, ""]
        write_atomic(os.path.join(memory_dir, file), topic)
        write_atomic(index_file, "\n".join(lines))
        return True, f"{'updated' if at >= 0 else 'saved'} {file}"
    except OSError as e:
        return False, f"save failed: {e}"


def delete_memory(claude_home, memory_dir, file):
    """Remove one topic file and its index line. Returns ``(ok, text)``."""
    error = _check_topic_name(file)
    if error is not None:
        return False, error
    topic_file = os.path.join(memory_dir, file)
    index_file = os.path.join(memory_dir, MEMORY_INDEX_FILENAME)
    try:
        # Read the index before touching anything, so an unreadable index fails
        # the call without leaving a pointer to a removed topic file.
        lines = kept = []
        if os.path.exists(index_file):
            lines = _load_index(claude_home, index_file).split("\n")
            kept = [line for line in lines if not _links_to(line, file)]
        found = False
        if confined_real_path(claude_home, topic_file) is not None:
            os.unlink(topic_file)
            found = True
        if len(kept) != len(lines):
            write_atomic(index_file, "\n".join(kept))
            found = True
        return (True, f"deleted {file}") if found else (False, f"no memory {json.dumps(file)}")
    except OSError as e:
        return False, f"delete failed: {e}"
