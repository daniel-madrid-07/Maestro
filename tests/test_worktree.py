"""maestro.worktree against a real temporary git repository."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from maestro import config, worktree


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


class WorktreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "README.md").write_text("hi\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-q", "-m", "init")
        self.home = base / "home"
        patcher = mock.patch.object(config, "HOME", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def branches(self) -> list[str]:
        return git(self.repo, "branch", "--format=%(refname:short)").split()

    def test_create_makes_clean_checkout_on_own_branch(self):
        wt = worktree.create(str(self.repo), "abc123")
        self.assertEqual(wt.branch, "mx/abc123")
        self.assertEqual(wt.path, str(self.home / "worktrees" / "repo-abc123"))
        self.assertEqual(os.path.realpath(wt.repo), os.path.realpath(self.repo))
        self.assertTrue((Path(wt.path) / "README.md").exists())
        self.assertEqual(git(wt.path, "rev-parse", "--abbrev-ref", "HEAD").strip(), "mx/abc123")
        self.assertEqual(git(wt.path, "status", "--porcelain"), "")

    def test_create_from_subdirectory_uses_repo_root(self):
        sub = self.repo / "sub"
        sub.mkdir()
        wt = worktree.create(str(sub), "sub1")
        self.assertEqual(os.path.realpath(wt.repo), os.path.realpath(self.repo))

    def test_remove_keeps_branch_with_unmerged_commit(self):
        wt = worktree.create(str(self.repo), "w1")
        (Path(wt.path) / "README.md").write_text("hi\nmore\n")
        git(wt.path, "commit", "-q", "-am", "worker edit")
        res = worktree.remove(wt)
        self.assertTrue(res["removed"])
        self.assertTrue(res["branch_kept"])
        self.assertIsNotNone(res["reason"])
        self.assertFalse(os.path.exists(wt.path))
        self.assertIn("mx/w1", self.branches())
        git(self.repo, "merge", "-q", "mx/w1")
        self.assertIn("more", (self.repo / "README.md").read_text())

    def test_remove_without_commits_deletes_branch(self):
        wt = worktree.create(str(self.repo), "w2")
        res = worktree.remove(wt)
        self.assertEqual(res, {"removed": True, "branch_kept": False, "reason": None})
        self.assertFalse(os.path.exists(wt.path))
        self.assertNotIn("mx/w2", self.branches())

    def test_create_outside_repo_raises_value_error(self):
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        with self.assertRaises(ValueError):
            worktree.create(str(plain), "x")

    def test_remove_never_raises(self):
        res = worktree.remove(worktree.Worktree(repo="/nonexistent", path="/nonexistent/wt", branch="mx/none"))
        self.assertTrue(res["branch_kept"])


class TerminalFieldTest(unittest.TestCase):
    def test_worktree_survives_persist_and_shows_in_public(self):
        from maestro.fleet import Terminal
        wt = worktree.Worktree(repo="/r", path="/h/worktrees/r-t1", branch="mx/t1")
        term = Terminal(id="t1", name="w", session="s", pane="%1", agent_profile="worker", cwd=wt.path, worktree=wt)
        self.assertEqual(term.public()["worktree"], {"repo": "/r", "path": wt.path, "branch": "mx/t1"})
        data = term.persisted()
        wt_data = data.pop("worktree")
        again = Terminal(**data, worktree=worktree.Worktree(**wt_data))
        self.assertEqual(again.worktree, wt)
        plain = Terminal(id="t2", name="w", session="s", pane="%2", agent_profile="worker", cwd="/r")
        self.assertIsNone(plain.public()["worktree"])


if __name__ == "__main__":
    unittest.main()
