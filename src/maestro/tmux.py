"""The few tmux operations maestro needs, as plain subprocess calls.

Every pane is addressed by its tmux pane id (``%12``): unique for the life of
the tmux server, unaffected by window renames or index shifts, and accepted by
every command below as ``-t``.
"""

import shlex
import subprocess
import time
import uuid

SHELLS = {"bash", "zsh", "sh", "fish", "dash"}


class TmuxError(RuntimeError):
    pass


# The name every terminal backend uses for its failures (see maestro.term).
Error = TmuxError

# Sessions survive a server restart: tmux keeps them, `restore()` re-adopts them.
PERSISTENT = True

# A Claude Code launched from inside another one inherits CLAUDE* variables and
# believes it is nested, which disables parts of it. The shell drops them first.
UNSET_CLAUDE_ENV = (
    "unset $(env | sed -n 's/^\\(CLAUDE[A-Z_]*\\)=.*/\\1/p' "
    "| grep -v CLAUDE_CONFIG_DIR) 2>/dev/null; "
)


def _run(*args: str, input: bytes | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        ["tmux", *args], input=input, capture_output=True, timeout=20
    )
    if check and proc.returncode != 0:
        raise TmuxError(
            f"tmux {' '.join(args[:2])} failed: {proc.stderr.decode(errors='replace').strip()}"
        )
    return proc.stdout.decode(errors="replace")


def has_session(name: str) -> bool:
    return subprocess.run(
        ["tmux", "has-session", "-t", f"={name}"], capture_output=True
    ).returncode == 0


def list_sessions() -> list[str]:
    out = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"], capture_output=True, text=True
    )
    if out.returncode != 0:
        return []  # no server running
    return [ln for ln in out.stdout.splitlines() if ln]


def _env_flags(env: dict[str, str]) -> list[str]:
    flags: list[str] = []
    for k, v in env.items():
        flags += ["-e", f"{k}={v}"]
    return flags


def new_session(name: str, window: str, cwd: str, env: dict[str, str]) -> str:
    """Create a detached session with one window; return that window's pane id."""
    # A detached session defaults to 80x24, which cramps Claude's TUI; give it
    # a real terminal's worth of columns and rows.
    out = _run(
        "new-session", "-d", "-s", name, "-n", window, "-c", cwd,
        "-x", "220", "-y", "50", *_env_flags(env), "-P", "-F", "#{pane_id}",
    )
    # The server must not exit when a mass teardown leaves it momentarily empty.
    _run("set-option", "-g", "exit-empty", "off", check=False)
    return out.strip()


def new_window(session: str, window: str, cwd: str, env: dict[str, str]) -> str:
    out = _run(
        "new-window", "-d", "-t", f"={session}:", "-n", window, "-c", cwd,
        *_env_flags(env), "-P", "-F", "#{pane_id}",
    )
    return out.strip()


def pane_alive(pane: str) -> bool:
    return subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane, "#{pane_id}"], capture_output=True
    ).returncode == 0


def pane_command(pane: str) -> str:
    return _run("display-message", "-p", "-t", pane, "#{pane_current_command}").strip()


def pane_cwd(pane: str) -> str:
    return _run("display-message", "-p", "-t", pane, "#{pane_current_path}").strip()


def pane_window_name(pane: str) -> str:
    return _run("display-message", "-p", "-t", pane, "#{window_name}").strip()


def capture(pane: str, scrollback: int = 60) -> str:
    """Rendered text of the pane: the viewport plus ``scrollback`` lines above it."""
    return _run("capture-pane", "-p", "-t", pane, "-S", f"-{scrollback}")


def kill_window(pane: str) -> None:
    _run("kill-window", "-t", pane, check=False)


def kill_session(name: str) -> None:
    _run("kill-session", "-t", f"={name}", check=False)


def send_key(pane: str, key: str) -> None:
    # A pane the user wheel-scrolled sits in copy mode and would eat the key.
    _run("send-keys", "-t", pane, "-X", "cancel", check=False)
    _run("send-keys", "-t", pane, key)


def send_line(pane: str, line: str) -> None:
    """Type a shell command literally and press Enter (used only for launching)."""
    _run("send-keys", "-t", pane, "-X", "cancel", check=False)
    _run("send-keys", "-t", pane, "-l", line)
    _run("send-keys", "-t", pane, "Enter")


def send_literal(pane: str, text: str) -> None:
    """Type ``text`` as keystrokes, without Enter."""
    _run("send-keys", "-t", pane, "-X", "cancel", check=False)
    _run("send-keys", "-t", pane, "-l", text)


def send_text(pane: str, text: str, submit_delay: float) -> None:
    """Paste ``text`` into a TUI as one bracketed paste, then submit with Enter.

    ``paste-buffer -p`` lets tmux emit the bracketed-paste markers only when the
    pane asked for them (Claude's TUI does), so multi-line text arrives as one
    message instead of one submission per line.
    """
    buf = f"mx_{uuid.uuid4().hex[:8]}"
    try:
        _run("send-keys", "-t", pane, "-X", "cancel", check=False)
        _run("load-buffer", "-b", buf, "-", input=text.encode())
        _run("paste-buffer", "-p", "-b", buf, "-t", pane)
        time.sleep(submit_delay)
        _run("send-keys", "-t", pane, "-X", "cancel", check=False)
        _run("send-keys", "-t", pane, "Enter")
    finally:
        _run("delete-buffer", "-b", buf, check=False)


def quote(parts: list[str]) -> str:
    return shlex.join(parts)


def launch(pane: str, argv: list[str], shell_wait: float = 15.0) -> None:
    """Start ``argv`` in the pane: wait for its shell, then type the command.

    Typed into a shell rather than run as the window's command on purpose: when
    Claude exits, the shell is left behind, which is how ``agent_exited`` tells
    an exit from a hang.
    """
    deadline = time.time() + shell_wait
    while time.time() < deadline and pane_command(pane) not in SHELLS:
        time.sleep(0.3)
    send_line(pane, UNSET_CLAUDE_ENV + quote(argv))


def agent_exited(pane: str) -> bool:
    """True when the pane's foreground process is a shell again (Claude exited)."""
    try:
        return pane_command(pane) in SHELLS
    except TmuxError:
        return False
