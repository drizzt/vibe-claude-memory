<!--
SPDX-FileCopyrightText: 2026 GodCC6
SPDX-FileCopyrightText: 2026 Timothy Redaelli

SPDX-License-Identifier: MIT
-->

# vibe-claude-memory

A [Mistral Vibe](https://github.com/mistralai/mistral-vibe) plugin that surfaces **Claude's
existing memory** inside Vibe, so a task can continue in Vibe after a Claude quota runs out.
Read-only by default.

Python port of [dsh-claude-memory](https://github.com/GodCC6/dsh-claude-memory) (a DeepSeek
Harness plugin), adapted to Vibe's plugin model. Standard library only.

```
~/.claude/CLAUDE.md          ─┐
  @imports inlined            │
~/.claude/projects/<key>/     ├─► confine ─► redact ─► byte-budget ─► session_start context
  memory/MEMORY.md  (index)   │                                       + claude_memory tool
  memory/<topic>.md (bodies) ─┘
```

## What it does

- At session start, adds the user-global `~/.claude/CLAUDE.md` (with `@path` imports inlined)
  and the resolved project's `MEMORY.md` index to the session context.
- Provides one `claude_memory` tool so the model can open any memory, list projects, or search
  a project's notes. Full bodies never enter the context unless the model asks.
- Masks credential-shaped text **before** anything reaches the model provider.
- Never writes, moves, or deletes anything under the Claude home, unless `enable_write` is on.

## Install

Requires Python 3.11 or newer as `python3` on `PATH`.

```bash
git clone https://github.com/drizzt/vibe-claude-memory.git ~/dev/vibe-claude-memory
mkdir -p ~/.vibe/plugins
ln -s ~/dev/vibe-claude-memory ~/.vibe/plugins/claude-memory   # or copy the directory
```

Then restart Vibe or run `/reload-plugins`. Vibe pins a copy of the plugin per session, so
later edits to the checkout reach the next session, not the running one.

Vibe asks before every plugin tool call by default. To let the read actions run without a
prompt, add to `~/.vibe/config.toml`:

```toml
[tools.claude-memory_claude_memory]
permission = "always"
```

The key is `<MCP server name>_<tool name>`. Leave it at the default `ask` if you turn on
`enable_write`, so every save and delete is approved by hand.

## How it plugs into Vibe

Vibe has no Python plugin API and no hook that edits the system prompt. The plugin uses the
two extension points that exist:

| Piece | File | Role |
|---|---|---|
| `session_start` hook | `ai.mistral.vibe/hooks.toml`, `claude_memory/hook.py` | Prints the memory text as `additional_context`, which Vibe adds to the next user message. Runs on startup, resume, `/clear` and fork; never for subagents. The status line reads `[claude-memory:load] <project key> (<match>)`. |
| MCP stdio server | `mcp.json`, `claude_memory/server.py` | Serves the `claude_memory` tool. Minimal JSON-RPC: `initialize`, `ping`, `tools/list`, `tools/call`. |

`plugin.json` exposes the tool directly (`toolOverrides`, `direct_and_programmatic`), so the
model calls it as `claude_memory`; code mode reaches it as `tools.mcp_claude_memory.claude_memory`.

The MCP server runs inside the plugin's directory and cannot see the session folder. The
hook can (`cwd` in its stdin payload), so it records the folder in
`$PLUGIN_DATA/session.json`, a per-session directory both processes share. The server reads
it on every call and falls back to the `cwd` setting, then its own working directory.

## Configuration

Optional. Settings come from `$VIBE_HOME/claude-memory.toml` (`VIBE_HOME` defaults to
`~/.vibe`), then from `CLAUDE_MEMORY_<FIELD>` environment variables, for example
`CLAUDE_MEMORY_ENABLE_WRITE=1`. An invalid value falls back to its default. The file is
re-read on every tool call and every session start.

```toml
# ~/.vibe/claude-memory.toml
enable_write = true
max_index_bytes = 16000
```

| Field | Default | Meaning |
|---|---|---|
| `claude_home` | `~/.claude` | Claude home. Every read is confined to this root. |
| `cwd` | unset | **Fallback only.** The session folder recorded by the hook always wins. |
| `include_descendants` | `true` | List sibling/child projects that have memory. |
| `descendant_limit` | `12` | How many other projects to list. |
| `max_index_bytes` | `24000` | Byte budget for the memory block. |
| `max_global_bytes` | `6000` | Byte budget for `CLAUDE.md` plus inlined imports. |
| `enable_memory` | `true` | Inject the memory block. |
| `enable_global_instructions` | `true` | Inject the user-global instructions. |
| `enable_tool` | `true` | Offer the `claude_memory` tool (`false`: the server lists no tools). |
| `enable_write` | `false` | Let the model save and delete memories in the current project (needs `enable_tool`). |
| `redact_mode` | `on` | `on` masks secrets, `report` counts only, `off` disables. |

## What reaches the model

One `additional_context` text at session start, made of up to three blocks:

1. **Claude global instructions**: `~/.claude/CLAUDE.md` with `@path` lines inlined.
2. **Claude memory**: the resolved project's `MEMORY.md` index, plus a newest-first list of
   the other projects that have memory.
3. **Persistent memory** (only with `enable_write`): what is worth remembering and when to save.

Full memory bodies arrive only through the tool, one file at a time, under a 20 KB cap.

## Project resolution

The directory that decides which memory is relevant is the **session's** folder. The plugin
picks the same single store Claude itself loads, with no fallback:

- **git-root**: the session folder is inside a git repository (a linked worktree counts as
  its main repository), so the key is the repository root, even when the subdirectory has a
  memory directory of its own;
- **exact**: no git repository, so the key is the session folder itself;
- **none**: that key has no memory. No index is injected; ancestor and child projects that
  have memory are listed as pointers for the tool.

Upstream also fell back to the most recently updated ancestor or child project. Claude does
not (checked against the Claude CLI 2.1.325 source: it resolves one path from the git root,
else the project directory), so this port dropped that rule: it would inject another
project's notes as if they were this one's.

## The `claude_memory` tool

| Action | Arguments | Returns |
|---|---|---|
| `projects` | `limit?` | Every project with memory, entry counts, current marker |
| `index` | `project?` | One project's `MEMORY.md` |
| `read` | `project?`, `file?` | One topic file; without `file`, the topic listing |
| `search` | `query`, `project?`, `limit?` | Matching lines as `file:line: text` |

`project` accepts a full key, a key without its leading dash, or a unique substring. Every
response passes the same redactor and a 20 KB cap.

## Write support

Off by default. With `enable_write = true`, notes taken in Vibe show up in the next Claude
session and the other way round. The tool gains two actions, and the session context gains
the "Persistent memory" block so the model keeps the memory up to date without being asked.

| Action | Arguments | Effect |
|---|---|---|
| `save` | `file`, `name`, `description`, `type`, `body` | Writes `<file>` with `name`/`description`/`metadata.type` frontmatter and upserts its `MEMORY.md` pointer line |
| `delete` | `file` | Removes `<file>` and its pointer line |

- **Current project only.** The target is the git repository root (the main worktree for a
  linked one), else the session folder: the same directory Claude itself uses. There is no
  `project` argument.
- **Index lines are upserted, not rebuilt.** Only the line linking to `<file>` changes;
  headings, ordering and every other line stay byte for byte.
- **Confined.** `file` must be a plain `.md` basename other than `MEMORY.md`, and the memory
  directory must resolve inside `claude_home`. Topic file and index are written through a
  temporary file and `os.replace`, so a concurrent reader never sees half a file.
- **Not redacted on write.** Notes land on disk as the model wrote them; redaction still
  applies to everything read back toward the provider.

## Security model

Notes written for one model provider are about to be sent to a different one.

- **Read-only by default.** With `enable_write` off no code path writes, moves, or deletes
  anything under `claude_home`. The only file the plugin writes is `session.json` in Vibe's
  per-session plugin data directory.
- **Confined.** Every path is `realpath`-resolved and must stay inside `claude_home`; a
  symlinked file pointing outside is rejected. Topic names containing a path separator are
  rejected.
- **Redacted before injection.** 18 rules cover provider key shapes (including Stripe,
  Google, SendGrid, npm and Slack webhooks), bearer tokens, PEM blocks, JWTs,
  `password`/`token`/`secret` assignments (password values need 8+ chars with a letter and
  a digit, or 12+ chars; token and secret values 12+ chars), connection strings with inline
  credentials, and long hex blobs. A false positive costs one masked line; a false negative
  ships a credential, so the policy leans conservative.
- **Untrusted framing.** The injected text tells the model the notes are background, never
  instructions.
- **No dependencies.** Standard library only, run with `python3 -I` so neither the user's
  site-packages nor the current directory can inject a module.

Redaction is pattern-based, not a classifier. It cannot catch a secret that does not look
like one. Use `redact_mode = "report"` to audit what *would* be masked, and
`enable_memory = false` as the kill switch.

## Tests

```bash
python3 -m unittest discover -s tests
```

| File | What it covers |
|---|---|
| `tests/test_core.py` | Paths, redaction, store, budgeting, tool, writes: against a synthetic fixture in a temp dir |
| `tests/test_entrypoints.py` | Config layering; the hook and the MCP server run as subprocesses exactly as Vibe starts them; an optional check against a real `~/.claude` (skipped when absent) that asserts no credential-shaped text reaches the context |

No network, no model calls.

## Case study: Claude files memory under the git repository root

Memory lives at `~/.claude/projects/<encoded-project-path>/memory/`, where the path is the
project directory with every character other than an ASCII letter or digit replaced by `-`
(`/home/me/.config` becomes `-home-me--config`). All linked worktrees of a repository share
the main worktree's key. The question is *which* directory counts as the project. Measured
upstream on a docs hub that sits inside a larger repo and has no `.git` of its own:

| Observation | Value |
|---|---|
| The hub session's recorded `cwd` | the hub directory |
| Its `Write`/`Edit` calls into a `memory/` path | 12, **all** under the enclosing repo's key |
| References to the enclosing repo's `memory/MEMORY.md` in that session | 235 |
| References to its own directory's `memory/` path | **0** |
| `.git` at hub / parent / grandparent | no / no / **yes** |

Session *transcripts* are stored under the cwd slug; **memory is stored under the git
repository root**. A bridge that keys by the raw cwd silently reads the wrong project's
notes for any subdirectory of a repository. Memory directories are also created on demand, so "no memory
directory" is normal and does not mean the directory was unused.

## Known limits

- **Context, not system prompt.** Vibe adds hook context to the next user message. A long
  session that compacts its history may summarize it away; `/clear` or a resume runs the
  hook again.
- **No refresh mid-session.** The injected index is read once at session start. The tool
  always reads the store live.
- **Subagents get no injected context.** Vibe does not run `session_start` for them. They can
  still call the tool, which resolves against the main session's folder.
- **Not a session-history reader.** It reads memory files, not transcripts.
- **Claude's own index cap is not enforced.** Claude loads the first 200 lines or 25 KB of
  `MEMORY.md`; this plugin injects up to `max_index_bytes` and does not warn about the cap.
- **Lossy project keys.** `-` is both the separator and a legal character in a directory
  name, so display labels are recovered by walking the filesystem and fall back to the raw
  key tail.

## License

MIT
