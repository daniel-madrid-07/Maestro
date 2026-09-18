"""Maestro: many full Claude Code sessions in tmux, directed by one of them or by VS Code.

Three processes share this package:

- ``maestro-server``  -- the HTTP API that owns the tmux sessions and watches them.
- ``maestro-ops``     -- the MCP server an outside orchestrator (Claude in VS Code) talks to.
- ``maestro-agent``   -- the MCP server every launched session gets, so it can spawn
                         and message other sessions.

The wire format is kept compatible with the parts of CAO the panel and the
``/cao`` skill already use: the same tool names, the same ``/sessions`` and
``/events`` routes, the same event ``kind`` vocabulary.
"""

__version__ = "0.1.1"
