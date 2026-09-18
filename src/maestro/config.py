"""Paths, ports and environment shared by the maestro processes.

Every setting resolves as: environment variable > ``$MAESTRO_HOME/config.toml``
> default. The result is exposed as plain module attributes (``config.PORT``)
so the rest of the code, and the tests that patch them, stay simple.
"""

import os
import sys
import tomllib
from pathlib import Path

DEFAULTS = {
    "port": 9889,
    "host": "127.0.0.1",
    "claude": "claude",
    "init_timeout": 90.0,
    "stuck_after": 600.0,
    "tick": 1.0,
    "session_prefix": "mx-",
    "extra_path": [],
}

# key -> (environment variable, type). extra_path takes os.pathsep-separated dirs.
ENV = {
    "port": ("MAESTRO_PORT", int),
    "host": ("MAESTRO_HOST", str),
    "claude": ("MAESTRO_CLAUDE", str),
    "init_timeout": ("MAESTRO_INIT_TIMEOUT", float),
    "stuck_after": ("MAESTRO_STUCK_AFTER", float),
    "tick": ("MAESTRO_TICK", float),
    "session_prefix": ("MAESTRO_SESSION_PREFIX", str),
    "extra_path": ("MAESTRO_EXTRA_PATH", list),
}

CONFIG_TEMPLATE = """\
# Maestro configuration. Every key is optional; the value shown is the default.
# An environment variable wins over this file (MAESTRO_PORT, MAESTRO_CLAUDE, ...).

# Where maestro-server listens. Keep it on loopback: the API has no auth.
port = 9889
host = "127.0.0.1"

# The Claude Code command each session runs: a name on PATH or an absolute path.
claude = "claude"

# Seconds a new session may take to show its input box.
init_timeout = 90

# Seconds a working session may keep an unchanged screen before it is flagged stuck.
stuck_after = 600

# Seconds between two reads of every pane.
tick = 1.0

# tmux sessions maestro creates get this prefix; it never touches any other.
session_prefix = "mx-"

# Directories put in front of PATH for the panes maestro launches, e.g.
# ["~/.npm-global/bin"] when `claude` would otherwise resolve to the wrong build.
extra_path = []
"""


def home_dir(environ=None) -> Path:
    environ = os.environ if environ is None else environ
    return Path(environ.get("MAESTRO_HOME") or str(Path.home() / ".maestro")).expanduser()


def _coerce(key: str, value, kind):
    if kind is list:
        if isinstance(value, str):
            value = [p for p in value.split(os.pathsep) if p]
        if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
            raise ValueError(f"{key} must be a list of directories")
        return [str(Path(p).expanduser()) for p in value]
    if kind is str and not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    if kind in (int, float) and isinstance(value, bool):
        raise ValueError(f"{key} must be a number")
    return kind(value)


def load(environ=None) -> tuple[dict, str | None]:
    """Resolve every setting; returns ``(values, error)``.

    A broken config file does not stop the processes: its values are ignored and
    the error is handed back so ``maestro doctor`` can show it.
    """
    environ = os.environ if environ is None else environ
    values = dict(DEFAULTS)
    error = None
    path = home_dir(environ) / "config.toml"
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            for key, (_var, kind) in ENV.items():
                if key in data:
                    values[key] = _coerce(key, data[key], kind)
        except (tomllib.TOMLDecodeError, ValueError, TypeError, OSError) as exc:
            values = dict(DEFAULTS)
            error = f"{path}: {exc}"
    for key, (var, kind) in ENV.items():
        raw = environ.get(var)
        if raw not in (None, ""):
            try:
                values[key] = _coerce(key, raw, kind)
            except (ValueError, TypeError) as exc:
                error = f"{var}: {exc}"
    return values, error


_values, LOAD_ERROR = load()

HOME = home_dir()
CONFIG_FILE = HOME / "config.toml"
TMP = HOME / "tmp"
LOGS = HOME / "logs"
USER_PROFILES = HOME / "profiles"
STATE_FILE = HOME / "state.json"
PID_FILE = HOME / "server.pid"

# Packaged profiles ship with the code; a same-named file in USER_PROFILES wins.
PACKAGED_PROFILES = Path(__file__).parent / "profiles"

HOST = _values["host"]
PORT = _values["port"]
URL = os.environ.get("MAESTRO_URL", f"http://{HOST}:{PORT}")

# The Claude Code command every pane runs.
CLAUDE = _values["claude"]

# tmux session names get this prefix so maestro never touches a session it did
# not create. The API talks in bare names.
SESSION_PREFIX = _values["session_prefix"]

# How long a Claude Code session may take to show its input box.
INIT_TIMEOUT = float(_values["init_timeout"])

# The Ink renderer swallows an Enter sent right after a paste. 2 s is what it
# took to stop losing submissions; shorter values dropped messages silently.
PASTE_SUBMIT_DELAY = 2.0

# Status is re-read from every pane this often.
TICK = float(_values["tick"])

# A processing pane whose screen has not changed for this long is reported stuck.
STUCK_AFTER = float(_values["stuck_after"])

EXTRA_PATH = list(_values["extra_path"])


def pinned_path() -> str:
    """PATH for the server and the panes it launches: ``extra_path`` first.

    tmux panes inherit whatever PATH the server had, and a login shell may put
    the wrong ``claude`` first: on WSL, for instance, Windows directories come
    through interop and ``claude`` can resolve to the Windows build, which
    cannot run inside tmux. Listing the right directory in ``extra_path`` pins it.
    """
    inherited = os.environ.get("PATH", "")
    return os.pathsep.join([*EXTRA_PATH, inherited] if inherited else EXTRA_PATH)


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
