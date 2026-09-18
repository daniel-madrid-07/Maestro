"""maestro init / doctor and the panel's static route, with no server, tmux or claude."""

import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from maestro import cli, config, server


class Sandbox(unittest.TestCase):
    """MAESTRO_HOME and ~/.claude in a temporary directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.home = base / "maestro"
        claude_dir = base / "claude"
        patches = [
            mock.patch.object(config, "HOME", self.home),
            mock.patch.object(config, "CONFIG_FILE", self.home / "config.toml"),
            mock.patch.object(config, "TMP", self.home / "tmp"),
            mock.patch.object(config, "LOGS", self.home / "logs"),
            mock.patch.object(config, "USER_PROFILES", self.home / "profiles"),
            mock.patch.object(config, "LOAD_ERROR", None),
            mock.patch.object(cli, "CLAUDE_DIR", claude_dir),
            mock.patch.object(cli, "SKILL_DIR", claude_dir / "skills" / "maestro"),
            mock.patch.object(cli, "CLAUDE_JSON", base / ".claude.json"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.skill = claude_dir / "skills" / "maestro" / "SKILL.md"
        self.claude_json = base / ".claude.json"

    def run_cli(self, *argv) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class InitTest(Sandbox):
    def test_creates_everything_once(self):
        with mock.patch.object(cli, "claude_bin", return_value=None):
            code, out = self.run_cli("init")
        self.assertEqual(code, 0)
        for d in ("profiles", "logs", "tmp"):
            self.assertTrue((self.home / d).is_dir())
        self.assertTrue((self.home / "config.toml").exists())
        self.assertTrue((self.home / "profiles" / "worker.md").exists())
        self.assertIn("name: maestro", self.skill.read_text())
        # No claude: the exact command is printed instead.
        self.assertIn("claude mcp add-json --scope user maestro", out)

    def test_idempotent_and_keeps_edited_profile(self):
        with mock.patch.object(cli, "claude_bin", return_value=None):
            self.run_cli("init")
            edited = self.home / "profiles" / "worker.md"
            edited.write_text("mine\n")
            (self.home / "config.toml").write_text("port = 1234\n")
            code, out = self.run_cli("init")
        self.assertEqual(code, 0)
        self.assertEqual(edited.read_text(), "mine\n")
        self.assertEqual((self.home / "config.toml").read_text(), "port = 1234\n")
        self.assertIn("worker.md: kept", out)

    def test_force_overwrites(self):
        with mock.patch.object(cli, "claude_bin", return_value=None):
            self.run_cli("init")
            edited = self.home / "profiles" / "worker.md"
            edited.write_text("mine\n")
            self.run_cli("init", "--force")
        self.assertNotEqual(edited.read_text(), "mine\n")

    def test_registers_mcp_when_missing(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return completed(1 if cmd[1:3] == ["mcp", "get"] else 0)

        with mock.patch.object(cli, "claude_bin", return_value="/bin/claude"), mock.patch.object(cli.subprocess, "run", fake_run):
            code, out = self.run_cli("init")
        self.assertEqual(code, 0)
        add = [c for c in calls if c[1:3] == ["mcp", "add-json"]]
        self.assertEqual(len(add), 1)
        self.assertEqual(add[0][:6], ["/bin/claude", "mcp", "add-json", "--scope", "user", "maestro"])
        self.assertIn('"command"', add[0][6])

    def test_skips_mcp_when_registered(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return completed(0)

        with mock.patch.object(cli, "claude_bin", return_value="/bin/claude"), mock.patch.object(cli.subprocess, "run", fake_run):
            code, out = self.run_cli("init")
        self.assertEqual(code, 0)
        self.assertFalse([c for c in calls if "add-json" in c])
        self.assertIn("already registered", out)


class DoctorTest(Sandbox):
    def healthy(self):
        """Patches under which every check passes."""
        self.claude_json.write_text("{}")
        return [
            mock.patch.object(cli.shutil, "which", return_value="/usr/bin/tmux"),
            mock.patch.object(cli, "claude_bin", return_value="/usr/bin/claude"),
            mock.patch.object(cli.subprocess, "run", return_value=completed(0, "2.0.0 (Claude Code)\n")),
            mock.patch.object(cli, "health", return_value=None),
            mock.patch.object(cli, "port_free", return_value=True),
            mock.patch.object(cli, "is_wsl", return_value=False),
        ]

    def doctor(self, *extra) -> tuple[int, str]:
        patches = self.healthy() + list(extra)
        for p in patches:
            p.start()
        try:
            return self.run_cli("doctor")
        finally:
            for p in reversed(patches):
                p.stop()

    def test_all_ok_exits_zero(self):
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text("x")
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertNotIn("fail", out)

    def test_missing_tmux_fails(self):
        code, out = self.doctor(mock.patch.object(cli.shutil, "which", return_value=None))
        self.assertEqual(code, 1)
        self.assertIn("tmux", out)

    def test_port_taken_fails(self):
        code, out = self.doctor(mock.patch.object(cli, "port_free", return_value=False))
        self.assertEqual(code, 1)
        self.assertIn("taken", out)

    def test_windows_build_under_wsl_warns(self):
        code, out = self.doctor(
            mock.patch.object(cli, "is_wsl", return_value=True),
            mock.patch.object(cli, "claude_bin", return_value="/mnt/c/Users/x/claude"),
        )
        self.assertIn("Windows build", out)

    def test_native_windows_fails_with_wsl_hint(self):
        code, out = self.doctor(mock.patch.object(sys, "platform", "win32"))
        self.assertEqual(code, 1)
        self.assertIn("run it inside WSL", out)

    def test_never_logged_in_fails(self):
        patches = self.healthy()
        self.claude_json.unlink()
        for p in patches:
            p.start()
        try:
            code, out = self.run_cli("doctor")
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertEqual(code, 1)
        self.assertIn("claude login", out)


class StaticRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.panel = base / "panel"
        (self.panel / "fonts").mkdir(parents=True)
        (self.panel / "index.html").write_text("<h1>panel</h1>")
        (self.panel / "fonts" / "a.woff2").write_bytes(b"font")
        (base / "secret.txt").write_text("no")
        (self.panel / "escape.txt").symlink_to(base / "secret.txt")
        p = mock.patch.object(server, "panel_root", return_value=self.panel)
        p.start()
        self.addCleanup(p.stop)

    def test_root_serves_index(self):
        body, ctype, cache = server.static_file("")
        self.assertEqual(body, b"<h1>panel</h1>")
        self.assertTrue(ctype.startswith("text/html"))
        self.assertEqual(cache, "no-store")

    def test_fonts_are_cached(self):
        body, ctype, cache = server.static_file("fonts/a.woff2")
        self.assertEqual(ctype, "font/woff2")
        self.assertIn("max-age", cache)

    def test_refuses_traversal(self):
        for rel in ("../secret.txt", "fonts/../../secret.txt", "%2e%2e/secret.txt", "..%2fsecret.txt", "fonts\\..\\..\\secret.txt"):
            self.assertIsNone(server.static_file(rel), rel)

    def test_refuses_escaping_symlink(self):
        self.assertIsNone(server.static_file("escape.txt"))

    def test_missing_file(self):
        self.assertIsNone(server.static_file("nope.js"))


class PackagedPanelTest(unittest.TestCase):
    def test_real_panel_ships_index(self):
        self.assertIsNotNone(server.static_file("index.html"))


class UsageTest(unittest.TestCase):
    def setUp(self):
        server._usage_cache.update(at=0.0, data=None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.creds = Path(self.tmp.name) / ".credentials.json"

    def test_no_credentials_file(self):
        with mock.patch.object(server, "CREDENTIALS", self.creds):
            self.assertEqual(server.read_usage(), {"ok": False, "message": "no local credentials"})

    def test_network_error_is_not_raised_and_token_not_returned(self):
        self.creds.write_text('{"claudeAiOauth": {"accessToken": "sekret"}}')
        with mock.patch.object(server, "CREDENTIALS", self.creds), mock.patch.object(
            server.urllib.request, "urlopen", side_effect=OSError("down")
        ):
            result = server.read_usage()
        self.assertFalse(result["ok"])
        self.assertNotIn("sekret", str(result))

    def test_percentages_only(self):
        self.creds.write_text('{"claudeAiOauth": {"accessToken": "sekret"}}')
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.BytesIO(b'{"five_hour": {"utilization": 12.4, "resets_at": "t1"}, "seven_day": {"utilization": 50}}')
        with mock.patch.object(server, "CREDENTIALS", self.creds), mock.patch.object(server.urllib.request, "urlopen", return_value=resp) as urlopen:
            result = server.read_usage()
        self.assertEqual(result, {"ok": True, "session": {"percent": 12, "resets_at": "t1"}, "week": {"percent": 50, "resets_at": None}})
        self.assertEqual(urlopen.call_args[0][0].get_header("Anthropic-beta"), "oauth-2025-04-20")


if __name__ == "__main__":
    unittest.main()
