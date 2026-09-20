"""The MCP servers shared with every agent: the file, the merge, the command."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from maestro import claude, cli, config, profiles


class SharedMcpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.shared = self.home / "mcp.json"
        for target, value in (("HOME", self.home), ("TMP", self.home / "tmp"),
                              ("SHARED_MCP", self.shared)):
            p = mock.patch.object(config, target, value)
            p.start()
            self.addCleanup(p.stop)
        (self.home / "tmp").mkdir(parents=True, exist_ok=True)

    def write_shared(self, servers):
        self.shared.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")

    def launch_config(self, profile_name="worker"):
        _prompt, mcp_file = claude.write_launch_files("t1", profiles.load(profile_name))
        if mcp_file is None:
            return None
        return json.loads(mcp_file.read_text(encoding="utf-8"))["mcpServers"]

    def test_shared_servers_reach_every_agent(self):
        self.write_shared({"playwright": {"command": "npx", "args": ["@playwright/mcp"]}})
        servers = self.launch_config()
        self.assertIn("playwright", servers)
        self.assertIn("maestro-agent", servers)   # the profile's own is still there

    def test_a_profile_wins_over_the_shared_file(self):
        self.write_shared({"maestro-agent": {"command": "impostor"}})
        servers = self.launch_config()
        self.assertNotEqual(servers["maestro-agent"]["command"], "impostor")

    def test_a_broken_file_is_ignored_not_fatal(self):
        self.shared.write_text("{ not json", encoding="utf-8")
        self.assertEqual(claude.shared_mcp_servers(), {})
        self.assertIn("maestro-agent", self.launch_config())

    def test_no_file_means_no_extra_servers(self):
        self.assertEqual(claude.shared_mcp_servers(), {})


class McpCommandTest(SharedMcpTest):
    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def source(self, servers):
        path = self.home / "claude.json"
        path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        return str(path)

    def test_import_copies_servers_and_skips_the_control_one(self):
        src = self.source({
            "playwright": {"command": "npx"},
            "maestro": {"command": "maestro-ops"},
        })
        code, out = self.run_cli("mcp", "import", "--source", src)
        self.assertEqual(code, 0)
        self.assertIn("playwright", out)
        self.assertIn("Skipped", out)
        self.assertEqual(list(claude.shared_mcp_servers()), ["playwright"])

    def test_import_adds_to_what_is_already_shared(self):
        self.write_shared({"github": {"command": "gh-mcp"}})
        self.run_cli("mcp", "import", "--source", self.source({"fetch": {"command": "uvx"}}))
        self.assertEqual(sorted(claude.shared_mcp_servers()), ["fetch", "github"])

    def test_replace_starts_from_scratch(self):
        self.write_shared({"github": {"command": "gh-mcp"}})
        self.run_cli("mcp", "import", "--replace", "--source", self.source({"fetch": {"command": "uvx"}}))
        self.assertEqual(list(claude.shared_mcp_servers()), ["fetch"])

    def test_only_picks_the_named_ones(self):
        src = self.source({"a": {"command": "x"}, "b": {"command": "y"}})
        self.run_cli("mcp", "import", "--only", "b", "--source", src)
        self.assertEqual(list(claude.shared_mcp_servers()), ["b"])

    def test_list_says_what_agents_get(self):
        self.write_shared({"playwright": {"command": "npx"}})
        code, out = self.run_cli("mcp", "list")
        self.assertEqual(code, 0)
        self.assertIn("playwright", out)


if __name__ == "__main__":
    unittest.main()
