"""Settings resolve as environment > config.toml > default."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from maestro import config


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def env(self, **extra):
        return {"MAESTRO_HOME": str(self.home), **extra}

    def test_defaults_without_file_or_env(self):
        values, error = config.load(self.env())
        self.assertIsNone(error)
        self.assertEqual(values, config.DEFAULTS)

    def test_file_beats_default(self):
        (self.home / "config.toml").write_text('port = 9999\nclaude = "/opt/claude"\nextra_path = ["/opt/bin"]\n')
        values, error = config.load(self.env())
        self.assertIsNone(error)
        self.assertEqual(values["port"], 9999)
        self.assertEqual(values["claude"], "/opt/claude")
        self.assertEqual(values["extra_path"], ["/opt/bin"])
        self.assertEqual(values["host"], "127.0.0.1")

    def test_env_beats_file(self):
        (self.home / "config.toml").write_text("port = 9999\nstuck_after = 30\n")
        values, _ = config.load(self.env(MAESTRO_PORT="7000", MAESTRO_CLAUDE="claude-dev"))
        self.assertEqual(values["port"], 7000)
        self.assertEqual(values["claude"], "claude-dev")
        self.assertEqual(values["stuck_after"], 30.0)

    def test_env_extra_path_splits_on_pathsep(self):
        values, _ = config.load(self.env(MAESTRO_EXTRA_PATH=os.pathsep.join(["/a", "/b"])))
        self.assertEqual(values["extra_path"], ["/a", "/b"])

    def test_broken_file_falls_back_and_reports(self):
        (self.home / "config.toml").write_text("port = [\n")
        values, error = config.load(self.env())
        self.assertEqual(values, config.DEFAULTS)
        self.assertIn("config.toml", error)

    def test_wrong_type_is_reported(self):
        (self.home / "config.toml").write_text('port = "high"\n')
        values, error = config.load(self.env())
        self.assertEqual(values["port"], 9889)
        self.assertIsNotNone(error)

    def test_template_parses_to_the_defaults(self):
        (self.home / "config.toml").write_text(config.CONFIG_TEMPLATE)
        values, error = config.load(self.env())
        self.assertIsNone(error)
        self.assertEqual(values, config.DEFAULTS)


class PinnedPathTest(unittest.TestCase):
    def test_without_extra_path_is_the_inherited_path(self):
        with mock.patch.object(config, "EXTRA_PATH", []), mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            self.assertEqual(config.pinned_path(), "/usr/bin:/bin")

    def test_extra_path_goes_first(self):
        with mock.patch.object(config, "EXTRA_PATH", ["/x/bin", "/y/bin"]), mock.patch.dict(os.environ, {"PATH": "/usr/bin"}):
            self.assertEqual(config.pinned_path(), "/x/bin:/y/bin:/usr/bin")

    def test_nothing_hardcoded(self):
        with mock.patch.object(config, "EXTRA_PATH", []), mock.patch.dict(os.environ, {"PATH": "/only"}):
            self.assertNotIn(".npm-global", config.pinned_path())


if __name__ == "__main__":
    unittest.main()
