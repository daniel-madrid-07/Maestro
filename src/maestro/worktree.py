"""Isolated git worktrees: one checkout and one branch per worker.

Two workers editing the same repository trample each other; a worktree gives
each its own directory on branch ``mx/<terminal_id>``. The orchestrator merges
the branch with plain git; ``remove`` never throws work away, so a branch with
unmerged commits survives the terminal.
"""

import os
import subprocess
from dataclasses import asdict, dataclass

from maestro import config

BRANCH_PREFIX = "mx/"


@dataclass
class Worktree:
    repo: str
    path: str
    branch: str

    def public(self) -> dict:
        return asdict(self)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=60)


def create(working_directory: str, terminal_id: str) -> Worktree:
    """Add a worktree of the repo holding ``working_directory`` on a new branch from HEAD."""
    top = _git("-C", working_directory, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise ValueError(f"use_worktree: {working_directory} is not inside a git repository")
    repo = top.stdout.strip()
    branch = BRANCH_PREFIX + terminal_id
    # Under MAESTRO_HOME: on ext4 (fast) and never inside the repo's own tree.
    path = str(config.HOME / "worktrees" / f"{os.path.basename(repo)}-{terminal_id}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    added = _git("-C", repo, "worktree", "add", "-b", branch, path, "HEAD")
    if added.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {added.stderr.strip() or added.stdout.strip()}")
    return Worktree(repo=repo, path=path, branch=branch)


def remove(worktree: Worktree) -> dict:
    """Drop the checkout; delete the branch only when it is merged into HEAD. Never raises."""
    result = {"removed": False, "branch_kept": True, "reason": None}
    try:
        gone = _git("-C", worktree.repo, "worktree", "remove", "--force", worktree.path)
        _git("-C", worktree.repo, "worktree", "prune")
        result["removed"] = not os.path.exists(worktree.path)
        if not result["removed"]:
            result["reason"] = f"worktree remove failed: {gone.stderr.strip()}"
            return result
        # -d (never -D) refuses a branch with commits HEAD does not have.
        deleted = _git("-C", worktree.repo, "branch", "-d", worktree.branch)
        if deleted.returncode == 0:
            result["branch_kept"] = False
        else:
            # First line only: git's hint suggests -D, which would discard the work.
            why = (deleted.stderr.strip().splitlines() or ["git branch -d failed"])[0]
            result["reason"] = f"branch {worktree.branch} kept: {why}"
    except Exception as exc:  # report, never raise: the terminal is already gone
        result["reason"] = str(exc)
    return result
