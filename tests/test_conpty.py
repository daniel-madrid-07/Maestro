"""The Windows backend against real pseudo-consoles (skipped elsewhere)."""

import sys
import time
import unittest

try:
    from maestro import conpty
except ImportError:  # not Windows, or pywinpty/pyte not installed
    conpty = None


def wait_for(predicate, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


@unittest.skipUnless(sys.platform == "win32" and conpty is not None, "needs Windows with pywinpty and pyte")
class ConptyTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(conpty.kill_session, "mx-test")

    def open(self, script: str) -> str:
        handle = conpty.new_session("mx-test", "w-1", ".", {"MAESTRO_TERMINAL_ID": "t1"})
        conpty.launch(handle, [sys.executable, "-u", "-c", script])
        return handle

    def test_screen_input_and_exit(self):
        h = self.open("import os; print('ID', os.environ['MAESTRO_TERMINAL_ID']); print('GOT', input('> '))")
        self.assertTrue(wait_for(lambda: "ID t1" in conpty.capture(h)), conpty.capture(h))
        self.assertFalse(conpty.agent_exited(h))
        conpty.send_line(h, "hello")
        self.assertTrue(wait_for(lambda: "GOT hello" in conpty.capture(h)), conpty.capture(h))
        self.assertTrue(wait_for(lambda: conpty.agent_exited(h)))
        self.assertTrue(conpty.pane_alive(h))  # the terminal outlives its process, like a tmux pane

    def test_claude_variables_are_not_inherited(self):
        import os

        os.environ["CLAUDECODE"] = "1"
        self.addCleanup(os.environ.pop, "CLAUDECODE", None)
        h = self.open("import os; print('NESTED', os.environ.get('CLAUDECODE'))")
        self.assertTrue(wait_for(lambda: "NESTED None" in conpty.capture(h)), conpty.capture(h))

    def test_kill_and_bookkeeping(self):
        h = self.open("import time; time.sleep(60)")
        self.assertEqual(conpty.list_sessions(), ["mx-test"])
        self.assertEqual(conpty.pane_window_name(h), "w-1")
        conpty.kill_window(h)
        self.assertFalse(conpty.pane_alive(h))
        self.assertFalse(conpty.has_session("mx-test"))
        with self.assertRaises(conpty.Error):
            conpty.capture(h)

    def test_scrollback_keeps_lines_above_the_screen(self):
        h = self.open("for i in range(200): print('line', i)")
        self.assertTrue(wait_for(lambda: "line 199" in conpty.capture(h)))
        self.assertIn("line 100", conpty.capture(h, 200))
        self.assertNotIn("line 100", conpty.capture(h, 0))


if __name__ == "__main__":
    unittest.main()
