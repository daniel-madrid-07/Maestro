"""snapshot / wait_for / broadcast / create_many against a mocked terminal backend."""

import threading
import time
import unittest
from unittest import mock

from maestro.fleet import Fleet, Session, Terminal
from maestro.term import backend as console


def make_fleet(*states: str) -> Fleet:
    f = Fleet()
    f.save = mock.Mock()
    f.sessions["s1"] = Session(name="s1", cwd="/tmp")
    for i, status in enumerate(states):
        term = Terminal(
            id=f"t{i}", name=f"worker-{i}", session="s1", pane=f"%{i}",
            agent_profile="worker", cwd="/tmp", model="sonnet", caller_id=None,
        )
        term.ready = True
        term.status = status
        f.terminals[term.id] = term
    return f


class SnapshotTest(unittest.TestCase):
    def test_counts_and_names_who_needs_the_orchestrator(self):
        f = make_fleet("processing", "waiting_user_answer", "error")
        f.terminals["t0"].stuck = True
        snap = f.snapshot()
        self.assertEqual(snap["totals"], {"sessions": 1, "terminals": 3,
                                          "by_status": {"stuck": 1, "waiting_user_answer": 1, "error": 1}})
        self.assertEqual({n["terminal_id"] for n in snap["needs_attention"]}, {"t0", "t1", "t2"})
        self.assertEqual({n["why"] for n in snap["needs_attention"]}, {"stuck", "waiting_user_answer", "error"})
        self.assertEqual(snap["sessions"][0]["terminals"], ["t0", "t1", "t2"])

    def test_quiet_fleet_asks_for_nothing(self):
        self.assertEqual(make_fleet("processing", "idle").snapshot()["needs_attention"], [])


class WaitTest(unittest.TestCase):
    def test_returns_at_once_when_one_has_settled(self):
        f = make_fleet("processing", "completed")
        res = f.wait_for(timeout=5)
        self.assertFalse(res["timed_out"])
        self.assertEqual([m["terminal_id"] for m in res["matched"]], ["t1"])
        self.assertEqual([p["terminal_id"] for p in res["pending"]], ["t0"])

    def test_a_queued_message_means_it_has_not_finished(self):
        f = make_fleet("completed")
        f.terminals["t0"].inbox.append({"message": "next task"})
        res = f.wait_for(timeout=1)
        self.assertTrue(res["timed_out"])
        self.assertEqual(res["pending"][0]["pending_messages"], 1)

    def test_wakes_when_the_status_changes(self):
        f = make_fleet("processing")
        def finish():
            time.sleep(0.5)
            f.terminals["t0"].status = "completed"
        threading.Thread(target=finish, daemon=True).start()
        started = time.monotonic()
        res = f.wait_for(timeout=10)
        self.assertFalse(res["timed_out"])
        self.assertLess(time.monotonic() - started, 5)

    def test_require_all_waits_for_the_slowest(self):
        f = make_fleet("completed", "processing")
        res = f.wait_for(timeout=1, require_all=True)
        self.assertTrue(res["timed_out"])
        f.terminals["t1"].status = "error"
        self.assertFalse(f.wait_for(timeout=5, require_all=True)["timed_out"])

    def test_unknown_ids_are_reported_not_waited_for(self):
        f = make_fleet("processing")
        res = f.wait_for(["nope"], timeout=1)
        self.assertEqual(res["unknown"], ["nope"])
        self.assertFalse(res["matched"])

    def test_stuck_is_a_state_of_its_own(self):
        f = make_fleet("processing")
        f.terminals["t0"].stuck = True
        res = f.wait_for(states=["stuck"], timeout=2)
        self.assertEqual([m["terminal_id"] for m in res["matched"]], ["t0"])


class BroadcastTest(unittest.TestCase):
    def test_reaches_every_terminal_by_default(self):
        f = make_fleet("idle", "idle")
        sent = f.broadcast("commit what you have")
        self.assertEqual([s["terminal_id"] for s in sent], ["t0", "t1"])
        self.assertEqual(f.terminals["t1"].inbox[0]["message"], "commit what you have")

    def test_can_be_aimed_at_named_terminals(self):
        f = make_fleet("idle", "idle")
        f.broadcast("only you", terminal_ids=["t1"])
        self.assertEqual(len(f.terminals["t0"].inbox), 0)
        self.assertEqual(len(f.terminals["t1"].inbox), 1)


class BatchTest(unittest.TestCase):
    def test_one_bad_spec_does_not_sink_the_rest(self):
        f = make_fleet()
        def create(name, profile, cwd, **kw):
            if name == "bad":
                raise ValueError("working_directory does not exist: /nope")
            term = Terminal(id=name, name=name, session=name, pane="%9",
                            agent_profile=profile, cwd=cwd or "/tmp", model=None, caller_id=None)
            f.terminals[name] = term
            return term
        with mock.patch.object(f, "create_terminal", side_effect=create):
            results = f.create_many([
                {"session_name": "good", "working_directory": "/tmp"},
                {"session_name": "bad", "working_directory": "/nope"},
            ])
        self.assertEqual([r["success"] for r in results], [True, False])
        self.assertIn("does not exist", results[1]["message"])

    def test_empty_batch_is_not_an_error(self):
        self.assertEqual(make_fleet().create_many([]), [])


if __name__ == "__main__":
    unittest.main()
