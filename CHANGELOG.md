# Changelog

## 0.1.1 — 2026-09-18

Found by a fleet of nine agents auditing the repository through Maestro itself.

### Fixed

- **A page in your browser could drive the API** (security). The server sent
  `Access-Control-Allow-Origin: *` on an API with no authentication that starts
  processes, so any site open in the same browser could launch sessions in any
  directory, feed them prompts and read their output. No CORS headers are sent
  any more, a request carrying a foreign `Origin` is refused, and a body must
  be declared `application/json` so a form post cannot slip past without a
  preflight.
- **A worker's uncommitted work could be deleted.** When `git status` failed
  for any reason — a slow or flaky filesystem is enough — the worktree was
  treated as clean and force-removed. An unreadable checkout is now kept, with
  the reason reported.
- **`maestro down` could signal an unrelated process on macOS.** The "is this
  pid really the server?" check read `/proc`, which does not exist there, so it
  was silently skipped. It uses `ps` now.
- The panel labelled every agent with its profile, so a fleet of eight read as
  eight identical `worker`s. The label is the task now; the profile and model
  are one click away. Progress events name the agent and carry their note.
- README: `maestro open` was missing from the command table and both MCP tool
  lists were incomplete.

## 0.1.0 — 2026-09-18

First public release.

### Added

- **Fleet** — full Claude Code sessions in tmux windows, one per agent, with
  per-terminal inboxes and a watch loop that classifies every pane's state from
  the rendered screen (idle, processing, completed, waiting, error).
- **`maestro-ops` MCP server** — launch, message, poll, read, answer, interrupt,
  restart and shut down sessions from an outside orchestrator.
- **`maestro-agent` MCP server** — `assign`, `handoff`, `send_message`,
  `report_progress`, `list_terminals`, `delete_terminal` inside every session.
- **Control tools** — `answer_prompt` (a key, a digit that picks an option, or
  a line of text), `interrupt`, and `restart_terminal` keeping the id, profile,
  model, directory and queue.
- **Isolated git worktrees** — `use_worktree` on launch/assign/handoff, branch
  `mx/<id>` in its own checkout; removing an agent commits whatever it left
  uncommitted and keeps an unmerged branch. `list_worktrees` shows what is left
  to merge, including checkouts orphaned by a server restart.
- **Notifications** — `waiting`, `stuck` (nothing on screen changed for
  `stuck_after`, default 600 s) and `error` events, raised by the panel as
  system notifications.
- **Self-reported progress** — `report_progress(percent, note)`, shown per agent
  and as a fleet mean on the panel.
- **Panel** — served by the server at `/`, installable as a desktop app,
  works offline.
- **CLI** — `maestro up | down | status | open | init | doctor`.
- **Profiles** — `worker`, `reviewer`, `code_supervisor`, overridable from
  `~/.maestro/profiles/`.

### Known limitations

- tmux is required, so Windows means WSL.
- State detection is tuned against Claude Code 2.1.x; a TUI redesign may need
  the patterns in `claude.py` adjusting (the test fixtures are real captures).
- Progress is what an agent reports about itself, not a measurement.
- The subscription usage read-out uses an undocumented endpoint and fails soft.
