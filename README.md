<div align="center">

<img src="src/maestro/panel/icon.svg" alt="Maestro logo" width="132">

# Maestro

**Run a dozen Claude Code sessions at once, and let one of them conduct the rest.**

Each agent is a full Claude Code process in its own tmux window — not a subagent, not an API call. Maestro launches them, reads their screens to know what they are doing, carries messages between them, and draws the whole fleet on a live panel.

[![Latest release](https://img.shields.io/github/v/release/daniel-madrid-07/Maestro?include_prereleases&label=release&color=d97757)](https://github.com/daniel-madrid-07/Maestro/releases)
[![Python](https://img.shields.io/badge/python-3.11%2B-4b8bbe)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20WSL-1f6feb)](#requirements)
[![License](https://img.shields.io/badge/license-MIT-8bc34a)](LICENSE)

</div>

---

## What it does

Claude Code can spawn subagents, but a subagent shares your context window and dies with your turn. Maestro runs **real sessions** instead: separate processes, separate context, separate conversation, all alive at the same time. You keep working in your editor while eight of them build eight parts of your project.

The orchestrator can be Claude itself. Point Claude Code at the `maestro` MCP server and it can launch workers, hand them briefs, poll their status, read their answers, answer their questions and close them — from the session you are already talking to. Or drive it by hand with `maestro status` and the CLI.

Knowing what an agent is doing is the hard part, and Maestro does it the way a person would: it reads the rendered terminal. A spinner means working, an option list means it is waiting for an answer, a finished response means the turn is done. No log parsing, no hooks, no cooperation needed from the agent.

## Features

- **Full sessions, not subagents** — one Claude Code process per worker, in its own tmux window, with its own context and its own conversation
- **Agents that delegate** — every session gets an MCP bridge: `assign` a worker without waiting, `handoff` and block for the answer, `send_message` to reply to whoever called you
- **Unstick a worker without touching the keyboard** — `answer_prompt` answers the question it is blocked on, `interrupt` stops a turn that is going in circles, `restart_terminal` gives it a fresh Claude with the same id, directory and queue
- **Isolated git worktrees** — `use_worktree` puts a worker on its own branch in its own checkout, so ten agents on one repository never trample each other; merge with plain git when they are done, and nothing uncommitted is ever thrown away
- **Live panel** — the fleet as a ring of agents around the conductor, wires that light up when a message crosses, a ticker of what just happened, and the state of every agent in its glyph. Installs as a desktop app (PWA); works offline
- **It tells you when it needs you** — system notifications when an agent asks a question, dies, or freezes for ten minutes with nothing changing on screen
- **Progress you can trust** — agents report their own percentage (`report_progress`); hover a worker to read it, hover the mark for the fleet's mean. No report, no invented number
- **Survives a restart** — the server re-adopts the tmux sessions that are still alive, and keeps a note of worktrees whose agent is gone so their work can still be merged
- **Small enough to read** — about 2,000 lines of Python, two dependencies, no database, no web framework

## Installation / Usage

```bash
uv tool install git+https://github.com/daniel-madrid-07/Maestro
maestro init      # config, profiles, the skill, and the MCP server in Claude Code
maestro doctor    # checks tmux, claude, the port and the registration
maestro up        # starts the server and opens the panel
```

`pipx install git+https://github.com/daniel-madrid-07/Maestro` works the same way.

Then, in any project, tell Claude Code what you want built:

> Orchestrate this with Maestro: split it into as many workers as it genuinely divides into, review at the end, and close the sessions when you are done.

Claude launches the fleet, and you watch it on <http://127.0.0.1:9889>.

### Commands

| | |
|---|---|
| `maestro up` | start the server, open the panel (`--foreground`, `--no-open`) |
| `maestro status` | every session and terminal, with status, progress and branch (`--json`) |
| `maestro doctor` | what is missing and how to fix it |
| `maestro down` | stop the server; running sessions stay up in tmux |
| `maestro init` | set up `~/.maestro`, the profiles, the skill and the MCP registration (`--force`) |

### Requirements

| | |
|---|---|
| **Python** | 3.11 or newer |
| **tmux** | 3.0 or newer — every agent lives in a tmux window |
| **Claude Code** | installed and signed in (`claude` on `PATH`) |
| **OS** | Linux, macOS, or Windows through WSL. There is no native Windows build: tmux is not optional |

### Profiles

An agent profile is a Markdown file with YAML front matter: model, effort, MCP servers and the system prompt appended to Claude's own. Three ship with Maestro — `worker` (delegates lookups to a cheap scout subagent), `reviewer`, and `code_supervisor` (runs its own workers). Copies land in `~/.maestro/profiles/`, and a file you edit there wins over the packaged one.

## Architecture

- **`maestro-server`** — the HTTP API. Owns the tmux sessions, starts Claude in each pane, reads every screen once a second to classify its state, delivers queued messages when an agent is free, streams events over SSE, and serves the panel.
- **`maestro-ops`** — the MCP server the outside orchestrator (Claude Code in your editor) talks to: `launch_session`, `send_session_message`, `get_terminal_status`, `get_terminal_output`, `answer_prompt`, `interrupt`, `restart_terminal`, `list_worktrees`, `shutdown_session`.
- **`maestro-agent`** — the MCP server every launched session gets, so agents can build their own sub-fleets: `assign`, `handoff`, `send_message`, `report_progress`, `list_terminals`, `delete_terminal`.
- **The panel** — a single HTML file served by the server: canvas for the field and the wires, DOM for the labels, SSE for the traffic.

State lives in `~/.maestro/` (config, profiles, logs, worktrees, `state.json`). Nothing leaves your machine except Claude Code's own traffic.

## Tech stack

Python 3.11 (standard library — `http.server`, `subprocess`, `threading`), [tmux](https://github.com/tmux/tmux), [Claude Code](https://claude.com/claude-code), the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk), PyYAML, and plain HTML/Canvas for the panel.

## License

MIT — see [LICENSE](LICENSE). The terminal-state heuristics are adapted from [awslabs/cli-agent-orchestrator](https://github.com/awslabs/cli-agent-orchestrator) (Apache-2.0); see [NOTICE](NOTICE).
