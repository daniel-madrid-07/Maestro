"""Paths, ports and environment shared by the three maestro processes."""

import os
import sys
from pathlib import Path

HOME = Path(os.environ.get("MAESTRO_HOME", str(Path.home() / ".maestro")))
TMP = HOME / "tmp"
LOGS = HOME / "logs"
USER_PROFILES = HOME / "profiles"
STATE_FILE = HOME / "state.json"

# Packaged profiles ship with the code; a same-named file in USER_PROFILES wins.
PACKAGED_PROFILES = Path(__file__).parent / "profiles"

HOST = "127.0.0.1"
PORT = int(os.environ.get("MAESTRO_PORT", "9889"))
URL = os.environ.get("MAESTRO_URL", f"http://{HOST}:{PORT}")

# tmux session names get this prefix so maestro never touches a session it did
# not create. The API talks in bare names.
SESSION_PREFIX = "mx-"

# How long a Claude Code session may take to show its input box.
INIT_TIMEOUT = float(os.environ.get("MAESTRO_INIT_TIMEOUT", "90"))

# The Ink renderer swallows an Enter sent right after a paste; CAO measured 2 s.
PASTE_SUBMIT_DELAY = 2.0

# Status is re-read from every pane this often.
TICK = 1.0

# Linux tool directories go first so `claude` never resolves to the Windows
# binary through WSL interop (that build cannot run inside tmux here).
LINUX_FIRST = ":".join(
    [
        str(Path.home() / ".npm-global" / "bin"),
        str(Path.home() / ".local" / "bin"),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
)


def pinned_path() -> str:
    return LINUX_FIRST + ":" + os.environ.get("PATH", "")


def console_script(name: str) -> str:
    """Absolute path of a sibling console script (``maestro-agent``) when installed.

    Inside a ``uv tool`` / venv install the scripts live next to the interpreter;
    an absolute path keeps the MCP server launchable from a pane whose PATH the
    profile cannot control.
    """
    candidate = Path(sys.executable).parent / name
    return str(candidate) if candidate.exists() else name


def ensure_dirs() -> None:
    for d in (HOME, TMP, LOGS, USER_PROFILES):
        d.mkdir(parents=True, exist_ok=True)
