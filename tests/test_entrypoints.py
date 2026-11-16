# SPDX-FileCopyrightText: 2026 GodCC6
# SPDX-FileCopyrightText: 2026 Timothy Redaelli
#
# SPDX-License-Identifier: MIT

"""End-to-end tests for config loading, the MCP server and the session hook.

The server and hook run as subprocesses exactly as Vibe starts them
(``python3 -I <script>``), with ``HOME``, ``VIBE_HOME`` and ``PLUGIN_DATA``
pointed at temp directories.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from claude_memory.config import load_config
from claude_memory.redact import redact_text

from fixtures import build_fixture, dispose_fixture

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "claude_memory", "server.py")
HOOK = os.path.join(ROOT, "claude_memory", "hook.py")


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = build_fixture()
        self.addCleanup(dispose_fixture, self.fx)
        self.vibe_home = os.path.join(self.fx.root, "vibe")
        self.plugin_data = os.path.join(self.fx.root, "data", "session-1")
        os.makedirs(self.vibe_home)
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": self.fx.root,
            "VIBE_HOME": self.vibe_home,
            "PLUGIN_DATA": self.plugin_data,
            "CLAUDE_MEMORY_CLAUDE_HOME": self.fx.home,
        }

    def write_config(self, text):
        with open(os.path.join(self.vibe_home, "claude-memory.toml"), "w") as f:
            f.write(text)

    def hook(self, payload):
        proc = subprocess.run(
            [sys.executable, "-I", HOOK],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=self.env,
            cwd=ROOT,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def server(self, *messages, raw=None):
        proc = subprocess.run(
            [sys.executable, "-I", SERVER],
            input=raw if raw is not None else "".join(json.dumps(m) + "\n" for m in messages),
            capture_output=True,
            text=True,
            env=self.env,
            cwd=ROOT,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [json.loads(line) for line in proc.stdout.splitlines()]


class ConfigTest(Base):
    def setUp(self):
        super().setUp()
        self._saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_defaults(self):
        config = load_config()
        self.assertEqual(config["claude_home"], self.fx.home)
        self.assertFalse(config["enable_write"])
        self.assertEqual(config["redact_mode"], "on")

    def test_file_then_env(self):
        self.write_config('max_index_bytes = 1000\nenable_write = true\nredact_mode = "report"\n')
        os.environ["CLAUDE_MEMORY_MAX_INDEX_BYTES"] = "2000"
        self.addCleanup(os.environ.pop, "CLAUDE_MEMORY_MAX_INDEX_BYTES")
        config = load_config()
        self.assertEqual(config["max_index_bytes"], 2000)
        self.assertTrue(config["enable_write"])
        self.assertEqual(config["redact_mode"], "report")

    def test_invalid_values_fall_back(self):
        self.write_config('max_index_bytes = "big"\nredact_mode = "maybe"\nenable_tool = 1\n')
        config = load_config()
        self.assertEqual(config["max_index_bytes"], 24000)
        self.assertEqual(config["redact_mode"], "on")
        self.assertTrue(config["enable_tool"])

    def test_broken_toml_ignored(self):
        self.write_config("this is = = not toml")
        self.assertEqual(load_config()["max_global_bytes"], 6000)

    def test_non_utf8_file_ignored(self):
        with open(os.path.join(self.vibe_home, "claude-memory.toml"), "wb") as f:
            f.write(b"max_index_bytes = 1000 # \xff\n")
        self.assertEqual(load_config()["max_index_bytes"], 24000)

    def test_write_needs_tool(self):
        self.write_config("enable_tool = false\nenable_write = true\n")
        self.assertFalse(load_config()["enable_write"])


class HookTest(Base):
    def test_injects_context_and_records_cwd(self):
        out = json.loads(self.hook({"hook_event_name": "session_start", "cwd": self.fx.repo_ws, "session_id": "x"}))
        context = out["hook_specific_output"]["additional_context"]
        self.assertIn("IMPORTED_MARKER_OK", context)
        self.assertIn(self.fx.repo_project.key, context)
        self.assertIn("enclosing git repository root", context)
        self.assertNotIn("# Persistent memory", context)
        self.assertIn("git-root", out["system_message"])
        with open(os.path.join(self.plugin_data, "session.json")) as f:
            self.assertEqual(json.load(f), {"cwd": self.fx.repo_ws})

    def test_no_credentials_leak(self):
        out = json.loads(self.hook({"cwd": self.fx.ws}))
        context = out["hook_specific_output"]["additional_context"]
        self.assertEqual(redact_text(context)[1], {})

    def test_write_instructions(self):
        self.write_config("enable_write = true\n")
        out = json.loads(self.hook({"cwd": self.fx.hub}))
        context = out["hook_specific_output"]["additional_context"]
        self.assertIn("# Persistent memory", context)
        self.assertIn("which has no memory yet", context)

    def test_everything_disabled_prints_nothing(self):
        self.write_config("enable_memory = false\nenable_global_instructions = false\n")
        self.assertEqual(self.hook({"cwd": self.fx.ws}), "")


class ServerTest(Base):
    INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}
    INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}

    @staticmethod
    def call(msg_id, **arguments):
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": "tools/call",
            "params": {"name": "claude_memory", "arguments": arguments},
        }

    def test_round_trip(self):
        responses = self.server(self.INIT, self.INITIALIZED, self.LIST, self.call(3, action="projects"))
        self.assertEqual([r["id"] for r in responses], [1, 2, 3])
        init, listing, called = responses
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("tools", init["result"]["capabilities"])
        self.assertEqual([t["name"] for t in listing["result"]["tools"]], ["claude_memory"])
        content = called["result"]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertIn(self.fx.ws_project.key, content[0]["text"])
        self.assertFalse(called["result"]["isError"])

    def test_uses_session_cwd_from_hook(self):
        self.hook({"cwd": self.fx.repo_ws})
        (response,) = self.server(self.call(1, action="index"))
        self.assertIn(self.fx.repo_project.key, response["result"]["content"][0]["text"])

    def test_tool_disabled(self):
        self.write_config("enable_tool = false\n")
        listing, called = self.server(self.LIST, self.call(3, action="projects"))
        self.assertEqual(listing["result"]["tools"], [])
        self.assertIn("error", called)

    def test_unknown_method_and_bad_json(self):
        parse_error, unknown = self.server(raw='not json\n{"jsonrpc":"2.0","id":9,"method":"resources/list"}\n')
        self.assertEqual(parse_error["error"]["code"], -32700)
        self.assertEqual(unknown["error"]["code"], -32601)


class LiveTest(unittest.TestCase):
    """Optional check against this machine's real store; skipped when absent."""

    def test_hook_against_real_store(self):
        home = os.path.join(os.path.expanduser("~"), ".claude")
        if not os.path.isdir(os.path.join(home, "projects")):
            self.skipTest(f"no Claude store at {home}")
        with tempfile.TemporaryDirectory() as data:
            env = {"PATH": os.environ.get("PATH", ""), "HOME": os.path.expanduser("~"), "PLUGIN_DATA": data,
                   "VIBE_HOME": data}
            proc = subprocess.run(
                [sys.executable, "-I", HOOK],
                input=json.dumps({"cwd": ROOT}),
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        if not proc.stdout:
            return
        context = json.loads(proc.stdout)["hook_specific_output"]["additional_context"]
        self.assertEqual(redact_text(context)[1], {})
        self.assertLessEqual(len(context.encode()), 6000 + 24000 + 8000)


if __name__ == "__main__":
    unittest.main()
