# Changelog

## 0.1.3 — 2026-09-20

### Added

- **`fleet_status`**: every session and terminal in one call, with
  `needs_attention` naming whoever is waiting on an answer, stuck or failed.
  It replaces `list_sessions` plus a `get_session_info` per session.
- **`wait_for`**: blocks until a worker finishes, asks a question or dies (or
  the timeout runs out), so an orchestrator waits instead of polling
  `get_terminal_status` every few seconds -- a call and a turn each time.
- **`launch_sessions`**: a whole fleet in one call, started in parallel. Ten
  workers took ten calls and several minutes to exist; now it takes one call
  and about as long as the slowest one. A bad entry fails on its own.
- **`broadcast_message`**: one message to the whole fleet, or to the terminals
  or sessions you name.
- **`get_usage`**: how much of the subscription's five-hour and weekly windows
  is left, for deciding how large a fleet to open.
- **A fleet that survives other people's tidying.** Sessions now live on a
  tmux server of Maestro's own (`tmux -L maestro`), so a `tmux kill-server`
  from the owner, an agent or another orchestrator on the default server
  cannot take the whole fleet with it. A fleet started by an older Maestro on
  the default server is kept there for that run; the move happens once the
  fleet is empty. A terminal whose window disappears anyway is kept as an
  `error` with the reason, instead of being deleted with its inbox and its
  worktree record: `restart_terminal` brings it back with all three.
- **Staggered starts, no ceiling.** At most `max_starting` (4) sessions boot
  Claude at the same time; the rest queue. Two orchestrators launching twenty
  workers at once is what stalled every screen read for everyone.
- **Agents run below normal CPU priority** (`nice`, 10 by default), and so
  does everything they run: the model is elsewhere, but a worker's greps,
  builds, tests and the app it launches are on this machine, and a fleet of
  them must not take the owner's editor along.
- The profiles forbid an agent to stop, restart or reconfigure Maestro, tmux
  or WSL, or to kill processes it did not start, and ask it to close what it
  launched to test.
- **Shared MCP servers.** Agents run with `--strict-mcp-config` and used to
  get only Maestro's own bridge, so a worker could edit files and nothing
  else. `maestro mcp import` copies the servers from your own Claude Code into
  `~/.maestro/mcp.json` (Maestro's own control server is skipped: a worker
  holding it could drive the fleet), and every new agent gets them on top of
  whatever its profile names. `maestro mcp list` shows what they have, and
  `maestro doctor` counts them.

### Changed

- **The panel shows one project at a time.** Two projects at once used to put
  every agent on the same ring, which read as one fleet -- and a fleet is
  normally many sessions on one repository, so the split that matters is the
  working directory, not the session name. Each project is its own view now:
  drag the field sideways with the left button, like a slider, or scroll
  sideways, or press the arrow keys, or click the arrows at the edge (a
  focusable pair of buttons does the same). The whole page moves -- ring,
  wires and mark -- and the next project's page slides in behind it, already
  laid out. The projects you are not watching wait at the edges as one arrowhead each, drawn from the same grains the
  wires carry, each grain wandering on its own period so the head swells and
  sags instead of pulsing as a rigid shape. An arrow warms to the signal
  colour when a worker there is waiting on an answer and goes red when one has
  failed, so a project off screen can still ask for you. The corner names the
  project and its place in the row, and a ticker line from elsewhere says
  which project it came from, for as long as you are somewhere else.
- **Resting on an agent no longer shoves the field around.** What a node says
  about itself -- its profile and model, its reported percentage -- hangs
  below its name out of the flow and cross-fades into place, instead of
  opening a height that moved every label under it with no easing at all.
- **Resting on the mark says where you are**: the project on screen, and how
  far its agents report they have got.

## 0.1.2 — 2026-09-18

### Added

- **Native Windows.** No WSL, no tmux: each session runs in a ConPTY (the
  pseudo-console Windows Terminal is built on) and its screen is rendered by a
  terminal emulator (pyte), so status detection and answer extraction work
  unchanged. `config.backend` picks `tmux` or `conpty` (`auto` by default).
  On Windows the sessions are the server's children: they end with it instead
  of surviving a restart.
- **One-click Windows installer** (`MaestroSetup.exe`, attached to every
  release, about 2 MB). Installs Git for Windows (checksum-verified), uv,
  Claude Code and Maestro per user, signs the user in to Claude in the
  browser, registers the MCP server and the skill, and opens the panel in a
  window of its own. No administrator rights, no restart. Uninstalls from
  Settings → Apps.
- First-run notices from new Claude Code builds (for example "Claude in Chrome
  extension detected") no longer stall a session's start: an unknown option
  dialog before the first prompt is answered with its default, which these
  dialogs make the conservative choice.
- `maestro up` and `maestro open` open the panel in a window of its own on
  Windows (Chrome or Edge `--app`).
- **One-line installer for macOS and Linux** (`install.sh`): tmux through the
  system package manager or Homebrew, uv, Claude Code, Maestro, sign-in, start.
  Safe to run again.

### Fixed

- `maestro doctor` reported "claude login: ok" whenever `~/.claude.json`
  existed, which `maestro init` itself creates. It now asks Claude Code
  (`claude auth status`), so a fresh install that is not signed in fails the
  check instead of passing it.

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
