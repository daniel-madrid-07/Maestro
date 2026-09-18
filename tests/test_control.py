"""answer_prompt / interrupt / restart_terminal against a mocked tmux."""

import unittest
from pathlib import Path
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
        # Escape is the exception (it ends the turn), covered separately below.
        for token, key in (("enter", "Enter"), ("Down", "Down"), ("space", "Space")):
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
        # One Escape: the (mocked) screen no longer shows a spinner afterwards.
        self.assertEqual(self.tmux["send_key"].call_args_list, [mock.call("%1", "Escape")])
        self.assertFalse(term.dispatched)

    def test_interrupt_repeats_escape_while_still_processing(self):
        f, _ = make_fleet("processing")
        tmux.capture.return_value = "✻ Cooking… (3s · ↑ 12 tokens)\n"
        f.interrupt("abcd1234")
        self.assertEqual(self.tmux["send_key"].call_args_list, [mock.call("%1", "Escape")] * 2)

    def test_interrupt_refused_on_idle_prompt(self):
        # A second Escape on an idle prompt opens the Rewind menu; refuse instead.
        for status in ("idle", "completed", "unknown", "error"):
            f, _ = make_fleet(status)
            with self.assertRaisesRegex(ValueError, status):
                f.interrupt("abcd1234")
        self.tmux["send_key"].assert_not_called()

    def test_answer_escape_clears_dispatch(self):
        f, term = make_fleet("waiting_user_answer")
        term.dispatched, term.dispatch_at = True, 123.0
        f.answer_prompt("abcd1234", "escape")
        self.tmux["send_key"].assert_called_once_with("%1", "Escape")
        self.assertFalse(term.dispatched)

    def test_rewind_menu_is_dismissed_by_the_watch_loop(self):
        f, term = make_fleet("idle")
        tmux.capture.return_value = (Path(__file__).parent / "fixtures" / "rewind_dialog.txt").read_text(encoding="utf-8")
        with mock.patch.object(tmux, "pane_alive", return_value=True):
            f._observe(term)
        self.tmux["send_key"].assert_called_once_with("%1", "Escape")
        self.assertEqual(term.status, "idle")  # untouched: the tick is skipped

    def test_restart_failure_keeps_the_terminal(self):
        f, term = make_fleet("error")
        term.failed = "claude exited"
        with mock.patch.object(Fleet, "_open_window", side_effect=tmux.TmuxError("no tmux")), \
             mock.patch.object(fleet_mod.profiles, "load") as load:
            load.return_value.name = "worker"
            with self.assertRaises(tmux.TmuxError):
                f.restart_terminal("abcd1234")
        self.assertIn("abcd1234", f.terminals)
        self.assertEqual(term.status, "error")
        self.assertFalse(term.restarting)

    def test_restart_reminds_a_worker_of_its_caller(self):
        f, term = make_fleet("error")
        term.failed = "claude exited"
        term.inbox.append({"message": "queued earlier", "sender_id": None, "orchestration_type": None})
        with mock.patch.object(Fleet, "_start"), \
             mock.patch.object(fleet_mod.profiles, "load") as load, \
             mock.patch.object(fleet_mod.events.log, "emit"):
            load.return_value.name = "worker"
            f.restart_terminal("abcd1234")
        self.assertIn("caller01", term.inbox[0]["message"])
        self.assertEqual(term.inbox[1]["message"], "queued earlier")

    def test_restart_refused_while_starting(self):
        f, term = make_fleet("unknown")
        term.ready = False
        with self.assertRaisesRegex(ValueError, "still starting"):
            f.restart_terminal("abcd1234")
        self.tmux["kill_window"].assert_not_called()

    def test_queued_message_survives_a_failed_paste(self):
        f, term = make_fleet("idle")
        term.inbox.append({"message": "later", "sender_id": None, "orchestration_type": None})
        tmux.capture.return_value = (Path(__file__).parent / "fixtures" / "worker2_completed.txt").read_text(encoding="utf-8")
        self.tmux["send_text"].side_effect = tmux.TmuxError("pane gone")
        with mock.patch.object(tmux, "pane_alive", return_value=True), \
             mock.patch.object(fleet_mod.events.log, "emit"):
            f._observe(term)
        self.assertEqual(len(term.inbox), 1)

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
        # The queued message survives, behind the "you were restarted" reminder.
        self.assertEqual([m["message"] for m in term.inbox][-1], "later")
        self.assertEqual(len(term.inbox), 2)
        self.assertFalse(term.ready)
        self.assertFalse(term.dispatched)
        self.assertEqual(term.status, "unknown")
        start.assert_called_once_with(term, load.return_value, True)
        types = [c.args[0] for c in emit.call_args_list]
        self.assertIn("terminal_restarted", types)
        self.assertNotIn("post_create_terminal", types)


if __name__ == "__main__":
    unittest.main()
