---
name: maestro
description: Orchestrate a task across many full Claude Code sessions with Maestro (each one a real Claude Code in its own tmux window, visible live in the Maestro panel). Use whenever the user asks to build, fix, refactor or research something "with Maestro", "with sessions", "with workers", "in parallel", "orchestrating", or types /maestro. Works from any project folder.
---

# Orchestrate with Maestro

You are the orchestrator. Do not do the work in this session: inspect, split,
launch workers, verify, report. The workers are full Claude Code sessions
running in tmux, driven through the `mcp__maestro__*` tools (registered at user
scope by `maestro init`, so they exist in every project). The user can watch
every session in the panel (`maestro open`, by default http://127.0.0.1:9889/).

## 0. Make sure the server is up

Call `list_sessions`. If it reports that maestro-server is not reachable, run
`maestro up --no-open` in a shell and call it again. If that fails, run
`maestro doctor` and show the user the lines that are not `ok`.

## 1. Understand, then split

Read the project yourself first (read-only). Split the goal into independent
units of work; one unit = one worker. If two units touch the same files, make
them one unit, sequence them, or give each its own worktree (below).

## 2. Launch

`launch_session` once per unit:

- `agent_profile`: `"worker"` for implementation, `"reviewer"` for review
  passes, `"code_supervisor"` when a sub-tree needs its own manager (it
  delegates to its own workers with `assign` / `handoff`). `list_profiles`
  shows what is installed; the user's copies live in `~/.maestro/profiles/`
  and win over the packaged ones.
- `working_directory`: an absolute path the server can see. The server runs
  where tmux runs, so on Windows (WSL) a Windows folder is spelled the Linux
  way: `C:\Code\Foo` becomes `/mnt/c/Code/Foo`. New folders are trusted
  automatically.
- `session_name`: short and meaningful (`auth-api`, `review-1`).
- `model`: optional; the profile's model otherwise (see the note below).
- `initial_message`: the full brief: goal, files, constraints, acceptance
  criteria, and "report back with exact paths when done".
- `use_worktree`: see "several workers on one repository".

Launching blocks until the session shows its input box, so a failed start is
reported right there.

**There is no maximum number of sessions.** Open as many as the work genuinely
splits into; never settle for fewer because it "seems enough". The only limits
are the machine and the user's plan; delete finished sessions as you go.

## 3. Drive to completion

- `get_terminal_status(terminal_id)`: `idle`, `processing`, `completed`,
  `waiting_user_answer` or `error`, plus `progress` (the percentage and note
  each worker reports with its own `report_progress` tool; the profiles ask
  for it, repeat the ask in long briefs) and `stuck` when a busy screen has not
  changed for a long time.
- `get_terminal_output(terminal_id, mode="last")`: the worker's last answer;
  `mode="full"` for the whole screen history. `read_session_output` pages
  through it.
- `send_session_message(terminal_id, message)`: follow-ups and corrections.
  Messages to a busy worker are queued and delivered when it is idle.
- `waiting_user_answer` → `answer_prompt(terminal_id, answer)`: a digit picks
  that option of the dialog, any other text is typed and submitted. A worker's
  question never has to wait for the user when you can answer it.
- a worker going in circles → `interrupt(terminal_id)` (Escape), then steer it
  with a message.
- status `error` or an unresponsive pane → `restart_terminal(terminal_id)`:
  same id, same working directory, a fresh Claude Code.

Launch a reviewer on anything non-trivial. Re-assign until the acceptance
criteria are actually met: do not accept "done" on a worker's word; check the
files and run the tests.

### Several workers on one repository

Pass `use_worktree=true` to `launch_session` (workers may pass it to `assign`
/ `handoff`). Each worker then gets its own checkout on branch `mx/<id>`, made
from the repository's HEAD. `list_worktrees()` shows every branch waiting to be
merged, plus orphaned checkouts left by an earlier server (removed with
`remove_worktree`). Once a worker has committed and been reviewed, merge with
`git -C <repo> merge mx/<id>`. Shutting a worker down commits whatever it left
uncommitted on its branch, so nothing is lost. Edits in a worktree do not show
up in the user's editor until the merge; say so when you choose it.

## 4. Close

`shutdown_session(session_name)` for every finished worker. Report to the user
which session ran which profile and model, and what each produced, with paths.

## Models

Each profile names a default model; `model` on `launch_session` overrides it
for one session. Prefer the stronger model for anything that implements,
decides or reviews, and a smaller one only for small, unambiguous tasks with a
clear success check. Follow the user's own policy when they have one.

## Notes

- The panel raises a notification when a session is waiting for an answer, is
  stuck, or failed; the user may act before you do.
- `maestro status` in a shell lists every session with its status, progress and
  branch; `maestro down` stops the server but leaves the sessions running, and
  `maestro up` picks them up again.
- On WSL, work under `/mnt/c` is slower than on the Linux filesystem; that is
  the price of seeing the edits live in a Windows editor. If git shows every
  file as modified there, run `git config core.filemode false` in that repo.
