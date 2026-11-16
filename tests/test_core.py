# SPDX-FileCopyrightText: 2026 GodCC6
# SPDX-FileCopyrightText: 2026 Timothy Redaelli
#
# SPDX-License-Identifier: MIT

"""Unit tests for paths, redaction, store, rendering and the tool.

Everything runs against a synthetic fixture in a temp directory, so the suite
passes on any machine and never reads a real ``~/.claude``.
"""

import os
import re
import shutil
import tempfile
import unittest

from claude_memory.paths import (
    confined_real_path,
    count_index_entries,
    encode_project_key,
    find_git_root,
    format_index_date,
    list_memory_projects,
    project_label,
    resolve_claude_home,
    resolve_key_to_path,
    select_projects,
)
from claude_memory.redact import REDACTION_MARK, redact_text
from claude_memory.render import byte_length, render_global_block, render_memory_block, truncate_to_bytes
from claude_memory.store import (
    write_atomic,
    delete_memory,
    find_project,
    read_confined,
    read_instructions_with_imports,
    read_project_index,
    save_memory,
    search_topics,
)
from claude_memory.tool import TOOL_MAX_BYTES, run, tool_definition

from fixtures import build_fixture, dispose_fixture, make_config

fx = None


def setUpModule():
    global fx
    fx = build_fixture()


def tearDownModule():
    dispose_fixture(fx)


def _project(key):
    return next(p for p in list_memory_projects(fx.home) if p["key"] == key)


class PathsTest(unittest.TestCase):
    def test_encode_matches_slug_format(self):
        self.assertEqual(encode_project_key("/home/user/my-project"), "-home-user-my-project")
        self.assertEqual(encode_project_key(fx.ws), fx.ws_project.key)

    def test_encode_windows_drive(self):
        self.assertEqual(encode_project_key("C:\\Users\\me"), "C--Users-me")

    def test_encode_every_non_alnum(self):
        self.assertEqual(encode_project_key("/home/me/.config/my_app (2)"), "-home-me--config-my-app--2-")

    def test_count_index_entries(self):
        text = "\n".join(["# Memory Index", "", "- [a](a.md) - one", "- [b](b.md) - two", "plain text", "* [c](c.md) - three"])
        self.assertEqual(count_index_entries(text), 3)

    def test_confined_rejects_outside(self):
        self.assertIsNone(confined_real_path(fx.home, "/etc/passwd"))

    def test_confined_rejects_escaping_symlink(self):
        outside = tempfile.mkdtemp(prefix="cm-out-")
        link = os.path.join(fx.home, "escape-link.md")
        try:
            with open(os.path.join(outside, "secret.md"), "w") as f:
                f.write("top secret")
            os.symlink(os.path.join(outside, "secret.md"), link)
            self.assertIsNone(confined_real_path(fx.home, link))
        finally:
            shutil.rmtree(outside, ignore_errors=True)
            if os.path.lexists(link):
                os.unlink(link)

    def test_list_finds_indexed_projects(self):
        projects = list_memory_projects(fx.home)
        keys = [p["key"] for p in projects]
        for expected in (fx.ws_project.key, fx.repo_project.key, fx.hub_b_project.key):
            self.assertIn(expected, keys)
        for p in projects:
            self.assertIsNotNone(confined_real_path(fx.home, p["index_file"]))

    def test_list_missing_home(self):
        self.assertEqual(list_memory_projects(os.path.join(fx.root, "no-such-home")), [])

    def test_git_root_walks_up(self):
        self.assertEqual(find_git_root(fx.repo_ws), fx.repo)
        self.assertEqual(find_git_root(fx.repo), fx.repo)
        self.assertIsNone(find_git_root(fx.hub))

    def test_git_root_linked_worktree(self):
        admin = os.path.join(fx.repo, ".git", "worktrees", "wt")
        worktree = os.path.join(fx.root, "repo-wt")
        os.makedirs(admin, exist_ok=True)
        os.makedirs(os.path.join(worktree, "sub"), exist_ok=True)
        with open(os.path.join(admin, "commondir"), "w") as f:
            f.write("../..\n")
        with open(os.path.join(worktree, ".git"), "w") as f:
            f.write(f"gitdir: {admin}\n")
        self.assertEqual(find_git_root(os.path.join(worktree, "sub")), fx.repo)

    def test_git_root_submodule(self):
        os.makedirs(os.path.join(fx.repo, ".git", "modules", "sm"), exist_ok=True)
        sub = os.path.join(fx.repo, "sm")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, ".git"), "w") as f:
            f.write("gitdir: ../.git/modules/sm\n")
        self.assertEqual(find_git_root(sub), sub)

    def test_select_exact(self):
        sel = select_projects(list_memory_projects(fx.home), fx.ws, include_descendants=False)
        self.assertEqual(sel["match"], "exact")
        self.assertEqual(sel["current"]["key"], fx.ws_project.key)

    def test_select_git_root(self):
        sel = select_projects(
            list_memory_projects(fx.home), fx.repo_ws, git_root=find_git_root(fx.repo_ws), include_descendants=False
        )
        self.assertEqual(sel["match"], "git-root")
        self.assertEqual(sel["current"]["key"], fx.repo_project.key)

    def test_select_git_root_beats_own_memory(self):
        # Claude keys by the git root even when the subdirectory has a memory dir.
        projects = list_memory_projects(fx.home) + [
            {"key": encode_project_key(fx.repo_ws), "mtime": 1.0, "memory_dir": "", "index_file": ""}
        ]
        sel = select_projects(projects, fx.repo_ws, git_root=fx.repo)
        self.assertEqual(sel["match"], "git-root")
        self.assertEqual(sel["current"]["key"], fx.repo_project.key)
        self.assertIn(encode_project_key(fx.repo_ws), [p["key"] for p in sel["related"]])

    def test_select_no_fallback_to_neighbours(self):
        sel = select_projects(list_memory_projects(fx.home), fx.hub, descendant_limit=10)
        self.assertEqual(sel["match"], "none")
        self.assertIsNone(sel["current"])
        related = sel["related"]
        self.assertEqual([p["key"] for p in related], [fx.hub_b_project.key, fx.hub_a_project.key])
        for a, b in zip(related, related[1:]):
            self.assertGreaterEqual(a["mtime"], b["mtime"])

    def test_select_none(self):
        sel = select_projects(list_memory_projects(fx.home), os.path.join(fx.root, "unrelated", "deep"))
        self.assertEqual(sel, {"current": None, "related": [], "match": "none"})

    def test_resolve_key_to_path(self):
        self.assertEqual(resolve_key_to_path(fx.ws_project.key), fx.ws)
        self.assertIsNone(resolve_key_to_path("-no-such-root-xyz"))

    def test_project_label(self):
        self.assertEqual(project_label(fx.ws_project.key), "ws")
        self.assertEqual(project_label("-no-such-root-xyz-my-project"), "my-project")

    def test_format_index_date(self):
        self.assertEqual(format_index_date(0), "?")
        self.assertRegex(format_index_date(1788912000), r"^\d{4}-\d{2}-\d{2}$")

    def test_resolve_claude_home(self):
        self.assertTrue(resolve_claude_home("~/.claude").endswith("/.claude"))
        self.assertEqual(resolve_claude_home(fx.home), fx.home)


class RedactTest(unittest.TestCase):
    def test_masks_each_rule(self):
        cases = [
            ("sk-abcdefghijklmnopqrstuvwx", "openai-key"),
            ("ghp_abcdefghijklmnopqrstuvwxyz01", "github-token"),
            ("glpat-abcdefghijklmnop", "gitlab-token"),
            ("xoxb-1234567890-abcdef", "slack-token"),
            ("AKIAIOSFODNN7EXAMPLE", "aws-key-id"),
            ("eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4", "jwt"),
            ("Authorization: Bearer abcdefghijklmnopqrstuvwx", "bearer"),
            ("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----", "private-key"),
            ("password: 10qMdWxy12", "password"),
            ("pwd=`oP3nS3same12`", "password"),
            ("Token: 4a000abcdefghij", "api-secret"),
            ("api_key = sk_live_abcdefghijkl", "api-secret"),
            ("mysql://root:hunter2xyz@10.0.0.1:3306/db", "dsn"),
            ("jdbc:mysql://root:hunter2xyz@10.0.0.1:3306/db?x=1", "dsn"),
            ("deadbeefdeadbeefdeadbeefdeadbeef", "long-hex"),
            ("DEADBEEFDEADBEEFDEADBEEFDEADBEEF", "long-hex"),
            ("sk-ant-api03-abcdefghijklmnopqrstu", "anthropic-key"),
            ("DB_PASSWORD=hunter2xyz99", "password"),
            ('{"password": "hunter2xyz99"}', "password"),
            ("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "api-secret"),
            ('"api_key": "abcdefghijklmnop"', "api-secret"),
            ("access_token: ya29abcdefghijklmnopq", "api-secret"),
            ('SECRET_KEY = "django-insecure-abcdefghijk"', "api-secret"),
            ("Authorization: bearer abcdefghijklmnopqrstuvwx", "bearer"),
            ("redis://:s3cretpassw0rd@host:6379", "dsn"),
            ("-----BEGIN PGP PRIVATE KEY BLOCK-----\nabc\n-----END PGP PRIVATE KEY BLOCK-----", "private-key"),
            ("sk_live_abcdefghijklmnop1234", "stripe-key"),
            ("rk_test_abcdefghijklmnop1234", "stripe-key"),
            ("AIzaSyA1234567890abcdefghijklmnopqrstuv", "google-api-key"),
            ("SG.abcdefghijklmnop1234.abcdefghijklmnop5678", "sendgrid-key"),
            ("npm_abcdefghijklmnopqrstuvwxyz0123456789", "npm-token"),
            ("https://hooks.slack.com/services/T000/B000/XXXXXXXX", "slack-webhook"),
        ]
        for value, kind in cases:
            with self.subTest(kind=kind, value=value):
                text, hits = redact_text(value)
                self.assertIn(REDACTION_MARK, text)
                self.assertGreaterEqual(hits.get(kind, 0), 1, hits)

    def test_assignment_keeps_keyword_and_quote(self):
        text, _ = redact_text("pwd=`oP3nS3same12`")
        self.assertEqual(text, f"pwd=`{REDACTION_MARK}`")

    def test_prose_not_masked(self):
        prose = "password: stored in 1password, see the docs"
        self.assertEqual(redact_text(prose), (prose, {}))

    def test_lookalikes_not_masked(self):
        for prose in ["run npm_install first", "the AIzaShort key", "sk_live_short", "SG.short.value"]:
            with self.subTest(prose=prose):
                self.assertEqual(redact_text(prose), (prose, {}))

    def test_report_mode(self):
        value = "token: abcdefghijklmnop"
        text, hits = redact_text(value, "report")
        self.assertEqual(text, value)
        self.assertGreaterEqual(sum(hits.values()), 1)

    def test_off_mode(self):
        value = "token: abcdefghijklmnop"
        self.assertEqual(redact_text(value, "off"), (value, {}))


class StoreTest(unittest.TestCase):
    def test_read_confined_outside(self):
        self.assertIsNone(read_confined(fx.home, "/etc/passwd"))

    def test_read_project_index(self):
        index = read_project_index(fx.home, _project(fx.ws_project.key))
        self.assertIsNotNone(index)
        self.assertEqual(index["entries"], 2)

    def test_imports_inlined(self):
        inst = read_instructions_with_imports(fx.home, os.path.join(fx.home, "CLAUDE.md"))
        self.assertEqual(len(inst["files"]), 2)
        self.assertNotRegex(inst["text"], re.compile(r"^@imported\.md$", re.M))
        self.assertIn("IMPORTED_MARKER_OK", inst["text"])

    def test_imports_nothing_readable(self):
        self.assertIsNone(read_instructions_with_imports(fx.home, os.path.join(fx.home, "nope.md")))

    def test_find_project(self):
        projects = list_memory_projects(fx.home)
        self.assertEqual(find_project(projects, fx.ws_project.key)["key"], fx.ws_project.key)
        self.assertEqual(find_project(projects, "hub-b")["key"], fx.hub_b_project.key)
        self.assertIsNone(find_project(projects, "no-such-project-xyz"))

    def test_search_topics(self):
        matches = search_topics(fx.home, _project(fx.ws_project.key), "second body", 5)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["file"], "beta.md")


class RenderTest(unittest.TestCase):
    def test_truncate_cjk(self):
        cut, truncated = truncate_to_bytes("\u4e2d\u6587\u5b57\u7b26\u6d4b\u8bd5" * 100, 90)
        self.assertTrue(truncated)
        self.assertLessEqual(byte_length(cut), 90)
        self.assertNotIn("\ufffd", cut)

    def test_memory_block_budget(self):
        block = render_memory_block(
            project={"key": "-a-b-c"},
            index={"text": "- [x](x.md) - \u4e2d\u6587\u5185\u5bb9" * 200, "entries": 200},
            related=[],
            all_projects=[{"key": "-a-b-c"}],
            max_bytes=500,
        )
        self.assertLessEqual(byte_length(block), 500)
        self.assertIn("truncated", block)

    def test_global_block_budget_with_hits(self):
        hits = {"openai-key": 1, "api-secret": 2, "password": 3, "private-key": 1}
        block = render_global_block({"text": "x" * 5000, "files": ["/h/CLAUDE.md"]}, 300, hits)
        self.assertLessEqual(byte_length(block), 300)
        self.assertIn("password", block)

    def test_memory_block_empty(self):
        self.assertEqual(render_memory_block(None, None, [], [], 1000), "")

    def test_memory_block_git_root(self):
        block = render_memory_block(
            project={"key": "-a-b"},
            index={"text": "- [x](x.md) - one", "entries": 1},
            related=[],
            all_projects=[{"key": "-a-b"}],
            max_bytes=4000,
            match="git-root",
            git_root="/a/b",
        )
        self.assertIn("enclosing git repository root", block)
        self.assertIn("/a/b", block)


class ToolTest(unittest.TestCase):
    def setUp(self):
        self.config = make_config(fx)

    def call(self, **args):
        return run(args, self.config, fx.ws)

    def test_projects(self):
        self.assertIn(fx.ws_project.key, self.call(action="projects"))

    def test_projects_marks_current(self):
        line = next(l for l in self.call(action="projects").splitlines() if fx.ws_project.key + ":" in l)
        self.assertIn("(current)", line)

    def test_index(self):
        self.assertIn("MEMORY.md", self.call(action="index"))

    def test_read_masks_credentials(self):
        text = self.call(action="read", file="alpha.md")
        self.assertIn(REDACTION_MARK, text)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", text)
        self.assertEqual(redact_text(text)[1], {}, text[:400])

    def test_read_without_file_lists_topics(self):
        self.assertIn("alpha.md", self.call(action="read"))

    def test_search(self):
        self.assertIn("matches", self.call(action="search", query="body"))

    def test_search_masks_multiline_key_body(self):
        text = self.call(action="search", query="AAAA")
        self.assertNotIn("b3BlbnNzaC1rZXktdjEAAAAA", text)

    def test_search_line_numbers_survive_masking(self):
        self.assertIn("key.md:7: after the key", self.call(action="search", query="after the key"))

    def test_search_requires_query(self):
        self.assertIn("requires a non-empty", self.call(action="search"))

    def test_unknown_project(self):
        self.assertIn("No single project matches", self.call(action="index", project="no-such-project-xyz"))

    def test_unknown_action(self):
        self.assertIn("Unsupported action", self.call(action="nope"))

    def test_definition_shape(self):
        d = tool_definition(False)
        self.assertEqual(d["name"], "claude_memory")
        schema = d["inputSchema"]
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["required"], ["action"])
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("save", schema["properties"]["action"]["enum"])
        self.assertIn("save", tool_definition(True)["inputSchema"]["properties"]["action"]["enum"])
        # The save guidance rides in the tool description, which Vibe resends on every
        # request; the session-start text is seen only once.
        self.assertIn("Save without being asked", tool_definition(True)["description"])
        self.assertNotIn("Save without being asked", d["description"])

    def test_byte_cap(self):
        self.assertLessEqual(byte_length(self.call(action="index")), TOOL_MAX_BYTES + 200)

    def test_read_only_rejects_save(self):
        self.assertIn("Unsupported action", self.call(action="save", file="a.md"))


class WriteTest(unittest.TestCase):
    """Ordered steps on one memory directory, like the upstream suite."""

    memo = dict(file="feedback-x.md", name="X rule", description="first hook", type="feedback", body="Do X.\n\n**Why:** y")

    def setUp(self):
        self.dir = os.path.join(fx.home, "projects", "-write-test-" + self._testMethodName, "memory")

    def read(self, name):
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return f.read()

    def test_save_creates_topic_and_index(self):
        ok, text = save_memory(fx.home, self.dir, **self.memo)
        self.assertTrue(ok, text)
        self.assertTrue(
            self.read("feedback-x.md").startswith(
                "---\nname: X rule\ndescription: first hook\nmetadata:\n  type: feedback\n---\n\nDo X."
            )
        )
        index = self.read("MEMORY.md")
        self.assertTrue(index.startswith("# Memory Index\n\n- [X rule](feedback-x.md) "), index)
        self.assertEqual(count_index_entries(index), 1)

    def test_save_replaces_only_its_line(self):
        save_memory(fx.home, self.dir, **self.memo)
        index_file = os.path.join(self.dir, "MEMORY.md")
        with open(index_file, "a", encoding="utf-8") as f:
            f.write("- [Other](other.md) - hand written\nfree note line\n")
        ok, text = save_memory(fx.home, self.dir, **dict(self.memo, description="second hook"))
        self.assertTrue(ok and text.startswith("updated"), text)
        lines = self.read("MEMORY.md").split("\n")
        self.assertIn("second hook", lines[2])
        self.assertIn("- [Other](other.md) - hand written", lines)
        self.assertIn("free note line", lines)
        self.assertEqual(count_index_entries("\n".join(lines)), 2)

    def test_save_of_read_output_round_trips(self):
        # The update flow is read, then save: passing the read text back unchanged
        # must not nest the header and old frontmatter inside the new body.
        save_memory(fx.home, self.dir, **self.memo)
        first = self.read("feedback-x.md")
        echoed = f"# -some-key: feedback-x.md\n\n{first}"
        ok, text = save_memory(fx.home, self.dir, **dict(self.memo, body=echoed))
        self.assertTrue(ok, text)
        self.assertEqual(self.read("feedback-x.md"), first)

    def test_save_keeps_body_rules(self):
        body = "Intro.\n\n---\n\nAfter a rule."
        save_memory(fx.home, self.dir, **dict(self.memo, body=body))
        self.assertTrue(self.read("feedback-x.md").endswith(body + "\n"))

    def test_save_quotes_yaml_values(self):
        ok, text = save_memory(fx.home, self.dir, **dict(self.memo, description="Testing: run make check"))
        self.assertTrue(ok, text)
        self.assertIn('\ndescription: "Testing: run make check"\n', self.read("feedback-x.md"))

    def test_save_rejects_bad_input(self):
        for name in ["../escape.md", "a/b.md", "MEMORY.md", "note.txt", ".hidden.md", ""]:
            self.assertFalse(save_memory(fx.home, self.dir, **dict(self.memo, file=name))[0], name)
        self.assertFalse(save_memory(fx.home, self.dir, **dict(self.memo, type="secret"))[0])
        self.assertFalse(save_memory(fx.home, self.dir, **dict(self.memo, body="  "))[0])

    def test_save_refuses_symlinked_dir(self):
        outside = tempfile.mkdtemp(prefix="cm-out-")
        project_dir = os.path.join(fx.home, "projects", "-escape-test")
        os.makedirs(project_dir, exist_ok=True)
        os.symlink(outside, os.path.join(project_dir, "memory"))
        try:
            self.assertFalse(save_memory(fx.home, os.path.join(project_dir, "memory"), **self.memo)[0])
            self.assertFalse(os.path.exists(os.path.join(outside, "feedback-x.md")))
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_delete_keeps_rest(self):
        save_memory(fx.home, self.dir, **self.memo)
        with open(os.path.join(self.dir, "MEMORY.md"), "a", encoding="utf-8") as f:
            f.write("- [Other](other.md) - hand written\n")
        ok, text = delete_memory(fx.home, self.dir, "feedback-x.md")
        self.assertTrue(ok, text)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "feedback-x.md")))
        index = self.read("MEMORY.md")
        self.assertNotIn("feedback-x.md", index)
        self.assertIn("hand written", index)
        self.assertFalse(delete_memory(fx.home, self.dir, "feedback-x.md")[0])

    def test_delete_unreadable_index_keeps_topic(self):
        save_memory(fx.home, self.dir, **self.memo)
        index_file = os.path.join(self.dir, "MEMORY.md")
        os.unlink(index_file)
        os.mkdir(index_file)
        ok, text = delete_memory(fx.home, self.dir, "feedback-x.md")
        self.assertFalse(ok, text)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "feedback-x.md")))

    def test_write_atomic_failure_leaves_no_tmp(self):
        os.makedirs(self.dir, exist_ok=True)
        target = os.path.join(self.dir, "target")
        os.mkdir(target)
        with self.assertRaises(OSError):
            write_atomic(target, "x")
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")], [])

    def test_tool_saves_to_git_root(self):
        config = make_config(fx, enable_write=True)
        text = run(dict(self.memo, action="save"), config, fx.repo_ws)
        self.assertIn(fx.repo_project.key, text)
        saved = os.path.join(fx.repo_project.dir, "feedback-x.md")
        self.assertTrue(os.path.exists(saved))
        self.assertIn("deleted", run({"action": "delete", "file": "feedback-x.md"}, config, fx.repo_ws))
        self.assertFalse(os.path.exists(saved))


if __name__ == "__main__":
    unittest.main()
