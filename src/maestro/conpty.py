"""Windows backend: each session is a Claude Code process in a ConPTY of its own.

Windows has no tmux, but it has pseudo-consoles (ConPTY, what Windows Terminal
is built on). Each terminal here owns one: the process runs in it, a reader
thread feeds everything it prints through a terminal emulator (pyte), and the
rest of Maestro reads that rendered screen exactly as it reads a tmux pane.

Same functions as ``maestro.tmux``, so the fleet does not care which one it
has. Two things differ, and callers must not assume otherwise:

- Sessions do not survive the server: the processes are its children.
  ``PERSISTENT`` is False and ``list_sessions`` is always empty after a start.
- There is no shell between Maestro and Claude: ``launch`` starts the process
  itself, and ``agent_exited`` is simply "the process is gone".
"""

import itertools
import os
import shutil
import threading
import time

import pyte
from winpty import PtyProcess

COLS, ROWS = 220, 50
HISTORY = 3000  # lines kept above the screen, for scrollback reads

PERSISTENT = False
SHELLS: set[str] = set()


class Error(RuntimeError):
    pass


TmuxError = Error  # the name older callers catch

# What a named key sends to a terminal application.
KEYS = {
    "Enter": "\r",
    "Escape": "\x1b",
    "Tab": "\t",
    "Space": " ",
    "Up": "\x1b[A",
    "Down": "\x1b[B",
    "Right": "\x1b[C",
    "Left": "\x1b[D",
    "C-c": "\x03",
    "BSpace": "\x7f",
}


class _Terminal:
    def __init__(self, session: str, window: str, cwd: str, env: dict[str, str]):
        self.session = session
        self.window = window
        self.cwd = cwd
        self.env = env
        self.screen = pyte.HistoryScreen(COLS, ROWS, history=HISTORY)
        self.stream = pyte.Stream(self.screen)
        self.lock = threading.Lock()
        self.proc: PtyProcess | None = None

    def start(self, argv: list[str]) -> None:
        # CreateProcess does not search PATHEXT, so `claude` must become the
        # real claude.exe / claude.cmd before it is spawned.
        exe = shutil.which(argv[0], path=self.env.get("PATH")) or argv[0]
        self.proc = PtyProcess.spawn([exe, *argv[1:]], cwd=self.cwd, env=self.env,
                                     dimensions=(ROWS, COLS))
        threading.Thread(target=self._pump, name=f"conpty-{self.window}", daemon=True).start()

    def _pump(self) -> None:
        while True:
            try:
                data = self.proc.read(65536)
            except EOFError:
                return
            except Exception:  # a torn-down console; the fleet sees agent_exited
                return
            if data:
                with self.lock:
                    self.stream.feed(data)

    def text(self, scrollback: int) -> str:
        with self.lock:
            cols = self.screen.columns
            history = list(self.screen.history.top)[-scrollback:] if scrollback > 0 else []
            lines = ["".join(line[x].data for x in range(cols)).rstrip() for line in history]
            lines += [row.rstrip() for row in self.screen.display]
        return "\n".join(lines)

    def write(self, data: str) -> None:
        if self.proc is None or not self.proc.isalive():
            raise Error(f"terminal {self.window} has no running process")
        self.proc.write(data)

    def close(self) -> None:
        if self.proc is not None:
            try:
                self.proc.terminate(force=True)
            except Exception:
                pass


_lock = threading.Lock()
_terminals: dict[str, _Terminal] = {}
_ids = itertools.count(1)


def _get(handle: str) -> _Terminal:
    with _lock:
        term = _terminals.get(handle)
    if term is None:
        raise Error(f"no such terminal: {handle}")
    return term


def _child_env(extra: dict[str, str]) -> dict[str, str]:
    """The server's environment for a child, minus what would confuse Claude.

    A Claude Code started from inside another one inherits CLAUDE* variables and
    believes it is nested; the tmux backend unsets them in the shell, this one
    simply does not pass them on.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("CLAUDE") or k.upper() == "CLAUDE_CONFIG_DIR"}
    env.update(extra)
    return env


def _open(session: str, window: str, cwd: str, env: dict[str, str]) -> str:
    handle = f"c{next(_ids)}"
    with _lock:
        _terminals[handle] = _Terminal(session, window, cwd, _child_env(env))
    return handle


# ---------------------------------------------------------------- tmux's API


def has_session(name: str) -> bool:
    with _lock:
        return any(t.session == name for t in _terminals.values())


def list_sessions() -> list[str]:
    with _lock:
        return sorted({t.session for t in _terminals.values()})


def new_session(name: str, window: str, cwd: str, env: dict[str, str]) -> str:
    return _open(name, window, cwd, env)


def new_window(session: str, window: str, cwd: str, env: dict[str, str]) -> str:
    return _open(session, window, cwd, env)


def launch(handle: str, argv: list[str], shell_wait: float = 0.0) -> None:
    _get(handle).start(argv)


def agent_exited(handle: str) -> bool:
    try:
        term = _get(handle)
    except Error:
        return False
    return term.proc is not None and not term.proc.isalive()


def pane_alive(handle: str) -> bool:
    with _lock:
        return handle in _terminals


def pane_command(handle: str) -> str:
    term = _get(handle)
    return "claude" if term.proc is not None and term.proc.isalive() else ""


def pane_cwd(handle: str) -> str:
    return _get(handle).cwd


def pane_window_name(handle: str) -> str:
    return _get(handle).window


def capture(handle: str, scrollback: int = 60) -> str:
    return _get(handle).text(scrollback)


def kill_window(handle: str) -> None:
    with _lock:
        term = _terminals.pop(handle, None)
    if term is not None:
        term.close()


def kill_session(name: str) -> None:
    with _lock:
        doomed = [h for h, t in _terminals.items() if t.session == name]
    for handle in doomed:
        kill_window(handle)


def kill_all() -> None:
    """Close every terminal; the server calls this on its way out."""
    with _lock:
        doomed = list(_terminals)
    for handle in doomed:
        kill_window(handle)


def send_key(handle: str, key: str) -> None:
    _get(handle).write(KEYS.get(key, key))


def send_line(handle: str, line: str) -> None:
    term = _get(handle)
    term.write(line)
    term.write("\r")


def send_literal(handle: str, text: str) -> None:
    _get(handle).write(text)


def send_text(handle: str, text: str, submit_delay: float) -> None:
    """Paste ``text`` as one bracketed paste, then submit with Enter.

    The markers keep a multi-line message a single submission; Claude's TUI
    turns bracketed paste on, and ConPTY passes the sequences through.
    """
    term = _get(handle)
    term.write("\x1b[200~" + text + "\x1b[201~")
    time.sleep(submit_delay)
    term.write("\r")


def quote(parts: list[str]) -> str:
    return " ".join(parts)
