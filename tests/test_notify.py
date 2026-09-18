"""Owner-facing notifications: a worker asking a question, a worker gone quiet.

Screens are the real fixtures with a spinner or a question footer spliced in;
tmux and the clock are mocked so a ten-minute stall takes no time.
"""

import unittest
from unittest import mock

from maestro import config, events, fleet

from test_status import fixture, with_line_above_box

BASE = fixture("worker2_completed")


def processing(spinner="✻ Cooking… (3s · ↑ 12 tokens)", progress=None):
    screen = BASE
    if progress:
        screen = with_line_above_box(screen, progress)
    return with_line_above_box(screen, spinner)


WAITING = with_line_above_box(BASE, "Enter to confirm · Esc to cancel")


class Harness(unittest.TestCase):
    def setUp(self):
        self.log = events.EventLog()
        self.now = 1000.0
        self.screen = BASE
        patches = [
            mock.patch.object(events, "log", self.log),
            mock.patch("maestro.tmux.capture", side_effect=lambda pane, lines=0: self.screen),
            mock.patch("maestro.tmux.pane_alive", return_value=True),
            mock.patch("maestro.claude.shell_is_back", return_value=False),
            mock.patch("time.time", side_effect=lambda: self.now),
            mock.patch.object(config, "STUCK_AFTER", 600.0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.fleet = fleet.Fleet()
        self.term = fleet.Terminal(
            id="t1", name="worker-t1", session="s", pane="%1", agent_profile="worker", cwd="/tmp", ready=True,
        )
        self.fleet.terminals["t1"] = self.term

    def observe(self, screen, advance=1.0):
        self.now += advance
        self.screen = screen
        self.fleet.tick()

    def kinds(self, kind):
        return [e for e in self.log.history() if e["kind"] == kind]


class Waiting(Harness):
    def test_emits_once_per_episode(self):
        self.observe(processing())
        for _ in range(5):
            self.observe(WAITING)
        self.assertEqual(self.term.status, "waiting_user_answer")
        waits = self.kinds("waiting")
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0]["detail"]["event_type"], "terminal_waiting")
        self.assertEqual(waits[0]["detail"]["agent_name"], "worker")
        self.assertIsNotNone(self.term.public()["waiting_since"])

        self.observe(processing())
        self.assertIsNone(self.term.public()["waiting_since"])
        self.observe(WAITING)
        self.assertEqual(len(self.kinds("waiting")), 2)  # a new question is a new episode


class Stuck(Harness):
    def test_flags_after_stuck_after_and_only_once(self):
        self.observe(processing())
        self.observe(processing(), advance=599)
        self.assertEqual(self.kinds("stuck"), [])
        self.assertFalse(self.term.public()["stuck"])

        self.observe(processing(), advance=1)
        stuck = self.kinds("stuck")
        self.assertEqual(len(stuck), 1)
        self.assertEqual(stuck[0]["detail"]["event_type"], "terminal_stuck")
        self.assertEqual(stuck[0]["detail"]["seconds"], 600)
        self.assertTrue(self.term.public()["stuck"])

        for _ in range(5):
            self.observe(processing(), advance=600)
        self.assertEqual(len(self.kinds("stuck")), 1)

    def test_spinner_alone_is_not_progress(self):
        # The spinner counts seconds and tokens on a frozen pane; that must not
        # keep resetting the clock.
        self.observe(processing("✻ Cooking… (3s · ↑ 12 tokens)"))
        self.observe(processing("✶ Cooking… (10m 3s · ↓ 4.1k tokens · esc to interrupt)"), advance=600)
        self.assertEqual(len(self.kinds("stuck")), 1)

    def test_changed_screen_resets_the_clock(self):
        self.observe(processing(progress="⏺ 1"))
        self.observe(processing(progress="⏺ 2"), advance=400)
        self.observe(processing(progress="⏺ 2"), advance=400)
        self.assertEqual(self.kinds("stuck"), [])
        self.observe(processing(progress="⏺ 2"), advance=200)
        self.assertEqual(len(self.kinds("stuck")), 1)

    def test_progress_after_stuck_clears_and_can_fire_again(self):
        self.observe(processing(progress="⏺ 1"))
        self.observe(processing(progress="⏺ 1"), advance=600)
        self.observe(processing(progress="⏺ 2"))
        self.assertFalse(self.term.stuck)
        self.observe(processing(progress="⏺ 2"), advance=600)
        self.assertEqual(len(self.kinds("stuck")), 2)

    def test_leaving_processing_clears(self):
        self.observe(processing())
        self.observe(processing(), advance=600)
        self.assertTrue(self.term.stuck)
        self.observe(WAITING)
        self.assertFalse(self.term.stuck)
        self.observe(processing(), advance=599)
        self.assertEqual(len(self.kinds("stuck")), 1)


if __name__ == "__main__":
    unittest.main()
