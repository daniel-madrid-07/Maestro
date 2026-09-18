"""answer_prompt / interrupt / restart_terminal against a mocked tmux."""

import unittest
from unittest import mock

from maestro import fleet as fleet_mod
from maestro import tmux
from maestro.fleet import Fleet, Session, Terminal


def make_fleet(status: str = "idle") -> tuple[Fleet, Terminal]:
    f = Fleet()
    f.save = mock.Mock()
    f.sessions["s1"] = Session(name="s1", cwd="/tmp")
    term = Terminal(
        id="abcd1234", name="worker-abcd", session="s1", pane="%1",
        agent_profile="worker", cwd="/tmp", model="sonnet", caller_id="caller01",
    )
    term.ready = True
    term.status = status
    f.terminals[term.id] = term
    return f, term


class ControlTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.multiple(
            tmux,
            capture=mock.Mock(return_value=""),
            send_key=mock.DEFAULT,
            send_literal=mock.DEFAULT,
            send_text=mock.DEFAULT,
            kill_window=mock.DEFAULT,
            has_session=mock.Mock(return_value=True),
            new_window=mock.Mock(return_value="%9"),
            new_session=mock.Mock(return_value="%9"),
        )
        self.tmux = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(fleet_mod.time, "sleep").start()

    def test_answer_refused_unless_waiting(self):
        f, _ = make_fleet("processing")
        with self.assertRaisesRegex(ValueError, "processing"):
            f.answer_prompt("abcd1234", "2")
        self.tmux["send_key"].assert_not_called()
        self.tmux["send_literal"].assert_not_called()

    def test_answer_key_token_is_sent_as_key(self):
        for token, key in (("enter", "Enter"), ("ESCAPE", "Escape"), ("Down", "Down"), ("space", "Space")):
            f, term = make_fleet("waiting_user_answer")
            self.tmux["send_key"].reset_mock()
            f.answer_prompt("abcd1234", token)
            self.tmux["send_key"].assert_called_once_with("%1", key)
            self.tmux["send_literal"].assert_not_called()
            self.assertTrue(term.dispatched)
            self.assertEqual(term.status, "processing")

    def test_answer_digit_is_sent_alone(self):
        # Claude's option dialogs submit on the digit itself; no Enter after it.
        f, term = make_fleet("waiting_user_answer")
        f.answer_prompt("abcd1234", "2")
        self.tmux["send_literal"].assert_called_once_with("%1", "2")
        self.tmux["send_key"].assert_not_called()
        self.assertEqual(term.status, "processing")

    def test_answer_text_is_typed_then_entered(self):
        f, term = make_fleet("waiting_user_answer")
        f.answer_prompt("abcd1234", "blue, please")
        self.tmux["send_literal"].assert_called_once_with("%1", "blue, please")
        self.tmux["send_key"].assert_called_once_with("%1", "Enter")
        self.assertTrue(term.dispatched)
        self.assertGreater(term.dispatch_at, 0)
        self.assertEqual(term.status, "processing")

    def test_interrupt_sends_escape_and_clears_dispatch(self):
        f, term = make_fleet("processing")
        term.dispatched, term.dispatch_at = True, 123.0
        f.interrupt("abcd1234")
        self.assertEqual(self.tmux["send_key"].call_args_list[0], mock.call("%1", "Escape"))
        self.assertTrue(all(c.args[1] == "Escape" for c in self.tmux["send_key"].call_args_list))
        self.assertFalse(term.dispatched)

    def test_restart_keeps_id_and_restarts(self):
        f, term = make_fleet("error")
        term.inbox.append({"message": "later", "sender_id": None, "orchestration_type": None})
        term.dispatched = True
        with mock.patch.object(Fleet, "_start") as start, \
             mock.patch.object(fleet_mod.profiles, "load") as load, \
             mock.patch.object(fleet_mod.events.log, "emit") as emit:
            load.return_value.name = "worker"
            same = f.restart_terminal("abcd1234")
        self.assertIs(same, term)
        self.assertEqual(list(f.terminals), ["abcd1234"])
        self.tmux["kill_window"].assert_called_once_with("%1")
        self.assertEqual(term.pane, "%9")
        self.assertEqual(term.name, "worker-abcd")
        self.assertEqual((term.model, term.cwd, term.caller_id), ("sonnet", "/tmp", "caller01"))
        self.assertEqual(len(term.inbox), 1)
        self.assertFalse(term.ready)
        self.assertFalse(term.dispatched)
        self.assertEqual(term.status, "unknown")
        start.assert_called_once_with(term, load.return_value, True)
        types = [c.args[0] for c in emit.call_args_list]
        self.assertIn("terminal_restarted", types)
        self.assertNotIn("post_create_terminal", types)


if __name__ == "__main__":
    unittest.main()
