"""Screen classification and answer extraction against real captured panes.

The fixtures under tests/fixtures are verbatim `tmux capture-pane` output of
Claude Code 2.1.276 sessions launched by maestro. Synthetic variants are built
from them for states that are awkward to catch live (spinner, question).
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

from maestro import claude, profiles

FIX = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIX / f"{name}.txt").read_text(encoding="utf-8")


def with_line_above_box(screen: str, line: str) -> str:
    """Insert ``line`` just above the input box's top rail (where the spinner renders)."""
    lines = screen.split("\n")
    rails = [i for i, ln in enumerate(lines) if claude.RAIL.search(ln)]
    top = rails[-2]  # the box has two rails; the last two on screen
    return "\n".join(lines[:top] + [line] + lines[top:])


class DetectStatus(unittest.TestCase):
    def test_pasted_prompt_without_answer_is_idle(self):
        # Captured right after the first paste: box holds the text, no spinner yet.
        # The fleet reports "processing" here through its dispatch grace, not
        # through the classifier, so the raw class must stay idle.
        self.assertEqual(claude.detect_status(fixture("worker_after_launch")), "idle")

    def test_finished_turn_is_completed(self):
        self.assertEqual(claude.detect_status(fixture("worker2_completed")), "completed")

    def test_spinner_above_box_is_processing(self):
        screen = with_line_above_box(fixture("worker2_completed"), "✻ Cooking… (3s · ↑ 12 tokens)")
        self.assertEqual(claude.detect_status(screen), "processing")

    def test_bare_spinner_line_is_processing(self):
        screen = fixture("worker_after_launch") + "\n✢ Misting… (33s · ↑ 332 tokens · esc to interrupt)\n"
        self.assertEqual(claude.detect_status(screen), "processing")

    def test_question_footer_is_waiting(self):
        screen = fixture("worker2_completed").replace(
            "⏵⏵ bypass permissions on", "↑/↓ to navigate · Enter to confirm · Esc to cancel"
        )
        self.assertEqual(claude.detect_status(screen), "waiting_user_answer")

    def test_rewind_menu_reads_as_waiting(self):
        # Captured after two Escapes on an idle prompt: "Enter to continue · Esc to cancel".
        self.assertEqual(claude.detect_status(fixture("rewind_dialog")), "waiting_user_answer")
        self.assertIn(claude.REWIND, fixture("rewind_dialog"))

    def test_trust_dialog_is_not_waiting(self):
        screen = (
            "Accessing workspace:\n /mnt/c/x\n Yes, I trust this folder\n ❯ No, exit\n"
            " Enter to confirm · Esc to cancel\n"
        )
        self.assertNotEqual(claude.detect_status(screen), "waiting_user_answer")

    def test_shell_prompt_is_unknown(self):
        self.assertEqual(claude.detect_status("user@host:~$ \n"), "unknown")

    def test_empty_is_unknown(self):
        self.assertEqual(claude.detect_status("\n\n"), "unknown")


class LastResponse(unittest.TestCase):
    def test_takes_the_newest_answer(self):
        self.assertEqual(claude.last_response(fixture("worker2_completed")), "PING")

    def test_counts_turns(self):
        # two ● answers + two "✻ <verb> for Ns" summaries
        self.assertEqual(claude.response_count(fixture("worker2_completed")), 4)

    def test_nothing_before_first_answer(self):
        self.assertIsNone(claude.last_response(fixture("worker_after_launch")))

    def test_effort_footer_is_not_an_answer(self):
        screen = "❯ hi\n\n● high · /effort\n"
        self.assertIsNone(claude.last_response(screen))


class Launch(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "the tmux launch line is POSIX shell")
    def test_command_carries_model_effort_and_files(self):
        p = profiles.load("worker")
        cmd = claude.build_command("abcd1234", p, "sonnet", Path("/tmp/x.prompt"), Path("/tmp/x.mcp.json"))
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertIn("--model sonnet", cmd)
        self.assertIn("--effort high", cmd)
        self.assertIn("--append-system-prompt-file /tmp/x.prompt", cmd)
        self.assertIn("--mcp-config /tmp/x.mcp.json --strict-mcp-config", cmd)
        self.assertTrue(cmd.startswith("unset $(env"))

    def test_profile_model_is_the_default(self):
        p = profiles.load("code_supervisor")
        cmd = claude.build_command("abcd1234", p, None, Path("/tmp/x.prompt"), None)
        self.assertIn("--model opus", cmd)
        self.assertIn("--effort xhigh", cmd)
        self.assertNotIn("--mcp-config", cmd)

    def test_argv_is_the_command_without_the_shell(self):
        p = profiles.load("worker")
        argv = claude.launch_argv(p, "sonnet", Path("/tmp/x.prompt"), None)
        self.assertEqual(argv[1:3], ["--dangerously-skip-permissions", "--model"])
        self.assertNotIn("unset", " ".join(argv))


class StartupDialogs(unittest.TestCase):
    CHROME = (
        "Claude in Chrome extension detected\n\n"
        " > 1. Keep browser tools off\n"
        "   2. Turn on browser tools\n\n"
        "Enter to confirm · Esc to cancel\n"
    )

    def answer(self, screen, answered):
        with mock.patch("maestro.term.backend.send_key") as key, \
             mock.patch("maestro.term.backend.capture", return_value=screen), \
             mock.patch("time.sleep"):
            sent = claude.answer_startup_dialogs("%1", screen, answered)
        return sent, [c.args[1] for c in key.call_args_list]

    def test_unknown_dialog_takes_its_default_once(self):
        answered: set[str] = set()
        self.assertEqual(self.answer(self.CHROME, answered), (True, ["Enter"]))
        self.assertEqual(self.answer(self.CHROME, answered), (False, []))

    def test_ready_prompt_is_not_a_dialog(self):
        self.assertEqual(self.answer(fixture("worker_after_launch"), set()), (False, []))


class ProjectKeys(unittest.TestCase):
    def test_windows_spellings(self):
        with mock.patch.object(claude.sys, "platform", "win32"), \
             mock.patch.object(claude.os.path, "realpath", side_effect=lambda p: p):
            keys = claude._project_keys(r"C:\Users\x\repo")
        self.assertTrue({r"C:\Users\x\repo", "C:/Users/x/repo", "c:/Users/x/repo"} <= keys)


class Profiles(unittest.TestCase):
    def test_packaged_profiles_load(self):
        names = {p["name"] for p in profiles.list_all()}
        self.assertTrue({"worker", "code_supervisor", "reviewer"} <= names)
        for n in ("worker", "code_supervisor", "reviewer"):
            p = profiles.load(n)
            self.assertIn("maestro-agent", p.mcp_servers)
            self.assertTrue(p.system_prompt)

    def test_front_matter_parsing(self):
        p = profiles.parse("---\nname: x\nmodel: sonnet\nclaudeConfig:\n  effort: low\n---\n\nBody", "x", "test")
        self.assertEqual((p.model, p.effort, p.system_prompt), ("sonnet", "low", "Body"))

    def test_rejects_traversal(self):
        with self.assertRaises(ValueError):
            profiles.load("../etc/passwd")


if __name__ == "__main__":
    unittest.main()
