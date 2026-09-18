"""Maestro: many full Claude Code sessions in tmux, directed by one of them or by VS Code.

Three processes share this package:

- ``maestro-server``  -- the HTTP API that owns the tmux sessions and watches them.
- ``maestro-ops``     -- the MCP server an outside orchestrator (Claude in VS Code) talks to.
- ``maestro-agent``   -- the MCP server every launched session gets, so it can spawn
                         and message other sessions.

Everything a session does reaches the others over one small HTTP API on
loopback: ``/sessions`` and ``/terminals`` to run the fleet, ``/events`` to
watch it. The panel is served by that same process, so it has no host to
configure and nothing to authenticate against.
"""

__version__ = "0.1.1"
