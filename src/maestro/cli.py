"""The ``maestro`` command: set up, start, stop and inspect maestro-server.

Plain text out, a meaningful exit code back, stdlib only. The server itself is
``maestro-server``; this is the thin layer a person types.
"""

import argparse
import json
import os
import platform
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from importlib import resources
from pathlib import Path

from maestro import __version__, config

# Where Claude Code keeps its state. Module attributes so the tests can point
# them at a temporary directory.
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
CLAUDE_JSON = Path.home() / ".claude.json"
SKILL_DIR = CLAUDE_DIR / "skills" / "maestro"
AGENTS_DIR = CLAUDE_DIR / "agents"

UP_TIMEOUT = 20.0
DOWN_TIMEOUT = 10.0

# ---------------------------------------------------------------- output

_COLOURS = {"ok": "32", "warn": "33", "fail": "31"}


def _tag(level: str) -> str:
    text = f"{level:<4}"
    if sys.stdout.isatty() and sys.platform != "win32":
        return f"\033[{_COLOURS[level]}m{text}\033[0m"
    return text


def _say(*lines: str) -> None:
    for line in lines:
        print(line)


# ---------------------------------------------------------------- server helpers


def health(timeout: float = 1.0) -> dict | None:
    """The server's /health payload, or None when nothing maestro answers."""
    try:
        with urllib.request.urlopen(config.URL + "/health", timeout=timeout) as resp:
            data = json.loads(resp.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("maestro") else None


def _get(path: str, timeout: float = 5.0):
    with urllib.request.urlopen(config.URL + path, timeout=timeout) as resp:
        return json.loads(resp.read() or b"null")


def port_free(host: str | None = None, port: int | None = None) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host or config.HOST, port or config.PORT))
        except OSError:
            return False
    return True


def _read_pid() -> int | None:
    try:
        pid = int(config.PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    # A reused pid belongs to something else; /proc tells us where it exists.
    cmdline = Path(f"/proc/{pid}/cmdline")
    if cmdline.exists():
        try:
            if b"maestro" not in cmdline.read_bytes():
                return None
        except OSError:
            pass
    return pid


def _panel_url() -> str:
    return config.URL + "/"


# ---------------------------------------------------------------- commands


def cmd_up(args) -> int:
    if health():
        _say(f"maestro-server is already running at {_panel_url()}")
        if not args.no_open:
            webbrowser.open(_panel_url())
        return 0
    if not port_free():
        _say(
            f"port {config.PORT} is taken by something that is not maestro-server.",
            "Set another one with `port = ...` in config.toml or MAESTRO_PORT.",
        )
        return 1
    if args.foreground:
        from maestro import server

        _say(f"maestro-server on {_panel_url()} (Ctrl-C stops it)")
        server.main()
        return 0

    config.ensure_dirs()
    log_path = config.LOGS / "server.log"
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "maestro.server"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    config.PID_FILE.write_text(f"{proc.pid}\n")
    deadline = time.monotonic() + UP_TIMEOUT
    while time.monotonic() < deadline:
        if health():
            _say(f"maestro-server is running at {_panel_url()} (pid {proc.pid}, log {log_path})")
            if not args.no_open:
                webbrowser.open(_panel_url())
            return 0
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    if proc.poll() is None:
        _say(f"maestro-server started (pid {proc.pid}) but /health did not answer within {UP_TIMEOUT:.0f}s.")
    else:
        config.PID_FILE.unlink(missing_ok=True)
        _say(f"maestro-server exited with code {proc.returncode}.")
    _say(f"See {log_path}")
    return 1


def cmd_down(args) -> int:
    pid = _read_pid()
    if pid is None:
        config.PID_FILE.unlink(missing_ok=True)
        if health():
            _say(
                f"A maestro-server answers at {config.URL} but was not started by `maestro up`;",
                "stop it where it was started.",
            )
            return 1
        _say("maestro-server was not running.")
        return 0
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + DOWN_TIMEOUT
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.2)
    forced = False
    if _alive(pid):
        os.kill(pid, signal.SIGKILL)
        forced = True
    config.PID_FILE.unlink(missing_ok=True)
    _say(
        f"maestro-server stopped (pid {pid}{', killed' if forced else ''}).",
        "Sessions running in tmux were left alone; `maestro up` picks them up again.",
    )
    return 0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A child we spawned in this process would linger as a zombie; reap it.
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


def cmd_status(args) -> int:
    try:
        sessions = _get("/sessions")
        terminals = []
        for s in sessions:
            terminals += _get(f"/sessions/{s['name']}/terminals")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _say(f"maestro-server is not reachable at {config.URL} ({getattr(exc, 'reason', exc)}); try `maestro up`.")
        return 1
    if args.json:
        print(json.dumps({"sessions": sessions, "terminals": terminals}, indent=2))
        return 0
    if not terminals:
        _say(f"maestro-server at {config.URL}: no sessions.")
        return 0
    rows = [("SESSION", "TERMINAL", "PROFILE", "MODEL", "STATUS", "PROGRESS", "BRANCH")]
    for t in terminals:
        prog = t.get("progress") or {}
        percent = prog.get("percent") if isinstance(prog, dict) else None
        status = t.get("status") or "?"
        if t.get("stuck"):
            status += " (stuck)"
        rows.append(
            (
                t.get("session_name") or "",
                t.get("id") or "",
                t.get("agent_profile") or "",
                t.get("model") or "-",
                status,
                f"{percent}%" if percent is not None else "-",
                (t.get("worktree") or {}).get("branch") or "-",
            )
        )
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip())
    return 0


def cmd_open(args) -> int:
    if not health():
        _say(f"maestro-server is not running at {config.URL}; start it with `maestro up`.")
        return 1
    webbrowser.open(_panel_url())
    _say(_panel_url())
    return 0


# ---------------------------------------------------------------- init


def _packaged(*parts: str):
    return resources.files("maestro").joinpath(*parts)


def _write(dest: Path, text: str, force: bool) -> str:
    """Write unless an existing file would be clobbered; returns what happened."""
    if dest.exists() and not force:
        return "kept"
    existed = dest.exists()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return "replaced" if existed else "created"


def claude_bin() -> str | None:
    """Absolute path of the configured ``claude`` on the PATH panes get."""
    cmd = os.path.expanduser(config.CLAUDE)
    return shutil.which(cmd, path=config.pinned_path())


def mcp_json() -> str:
    return json.dumps({"command": config.console_script("maestro-ops"), "args": []})


def mcp_registered(claude: str) -> bool:
    try:
        return subprocess.run([claude, "mcp", "get", "maestro"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def cmd_init(args) -> int:
    force = args.force
    config.ensure_dirs()
    _say(f"{config.HOME}: profiles/, logs/, tmp/ ready")
    _say(f"config.toml: {_write(config.CONFIG_FILE, config.CONFIG_TEMPLATE, force)}")

    for src in sorted(_packaged("profiles").iterdir(), key=lambda p: p.name):
        if src.name.endswith(".md"):
            what = _write(config.USER_PROFILES / src.name, src.read_text(encoding="utf-8"), force)
            _say(f"profile {src.name}: {what}")

    skill = _packaged("skill", "SKILL.md").read_text(encoding="utf-8")
    _say(f"skill {SKILL_DIR / 'SKILL.md'}: {_write(SKILL_DIR / 'SKILL.md', skill, force)}")

    # The `worker` profile tells its agent to send every lookup to a `scout`
    # subagent, which only exists if we install it.
    for src in sorted(_packaged("agents").iterdir(), key=lambda p: p.name):
        if src.name.endswith(".md"):
            what = _write(AGENTS_DIR / src.name, src.read_text(encoding="utf-8"), force)
            _say(f"subagent {src.name}: {what}")

    status = 0
    add = ["mcp", "add-json", "--scope", "user", "maestro", mcp_json()]
    claude = claude_bin()
    if claude is None:
        _say(
            f"MCP server: `{config.CLAUDE}` is not on PATH; once Claude Code is installed, run:",
            "  " + shlex.join(["claude", *add]),
        )
    elif mcp_registered(claude) and not force:
        _say("MCP server: already registered with Claude Code (maestro)")
    else:
        if force and mcp_registered(claude):
            subprocess.run([claude, "mcp", "remove", "--scope", "user", "maestro"], capture_output=True, timeout=30)
        try:
            done = subprocess.run([claude, *add], capture_output=True, text=True, timeout=30)
            ok = done.returncode == 0
            detail = (done.stderr or done.stdout).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            ok, detail = False, str(exc)
        if ok:
            _say("MCP server: registered with Claude Code as `maestro` (user scope)")
        else:
            status = 1
            _say(f"MCP server: registration failed ({detail}); run it yourself:", "  " + shlex.join([claude, *add]))

    _say("", "What now:", "  maestro doctor    check tmux, Claude Code and the port", "  maestro up        start the server and open the panel")
    return status


# ---------------------------------------------------------------- doctor


def _run(cmd: list[str]) -> str | None:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return (done.stdout or done.stderr).strip().splitlines()[0] if (done.stdout or done.stderr).strip() else ""


def is_wsl() -> bool:
    return sys.platform == "linux" and (
        bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in platform.uname().release.lower()
    )


def checks() -> list[tuple[str, str, str, str]]:
    """``(level, name, detail, fix)`` for every check, in order."""
    out = []

    def add(level, name, detail, fix=""):
        out.append((level, name, detail, fix))

    if sys.platform == "win32":
        add("fail", "platform", "native Windows", "Maestro needs tmux; on Windows run it inside WSL")
        return out

    v = sys.version_info
    if v >= (3, 11):
        add("ok", "python", f"{v.major}.{v.minor}.{v.micro}")
    else:
        add("fail", "python", f"{v.major}.{v.minor}.{v.micro}", "install Python 3.11 or newer")

    if config.LOAD_ERROR:
        add("fail", "config", config.LOAD_ERROR, f"fix or delete {config.CONFIG_FILE}")
    else:
        add("ok", "config", str(config.CONFIG_FILE) if config.CONFIG_FILE.exists() else "defaults (no config.toml)")

    if shutil.which("tmux", path=config.pinned_path()):
        add("ok", "tmux", _run(["tmux", "-V"]) or "found")
    else:
        fix = "brew install tmux" if sys.platform == "darwin" else "install tmux with your package manager (apt install tmux)"
        add("fail", "tmux", "not found on PATH", fix)

    claude = claude_bin()
    if claude is None:
        add("fail", "claude", f"`{config.CLAUDE}` not found", "install Claude Code, or set `claude = \"/abs/path\"` in config.toml")
    else:
        version = _run([claude, "--version"])
        if version is None:
            add("fail", "claude", f"{claude} does not run (`--version` failed)", "reinstall Claude Code for this platform")
        elif is_wsl() and os.path.realpath(claude).startswith("/mnt/"):
            add(
                "warn", "claude", f"{claude} is the Windows build",
                "install Claude Code inside WSL and put its directory in `extra_path` (or `claude = ...`) in config.toml",
            )
        else:
            add("ok", "claude", f"{claude} ({version})")

    if CLAUDE_JSON.exists():
        add("ok", "claude login", f"{CLAUDE_JSON} exists")
    else:
        add("fail", "claude login", "Claude Code has never been run", "run `claude` once and log in")

    if claude and mcp_registered(claude):
        add("ok", "mcp", "maestro registered with Claude Code")
    else:
        add("warn", "mcp", "maestro not registered with Claude Code", "run `maestro init`")

    if (SKILL_DIR / "SKILL.md").exists():
        add("ok", "skill", str(SKILL_DIR / "SKILL.md"))
    else:
        add("warn", "skill", "maestro skill not installed", "run `maestro init`")

    if health():
        add("ok", "port", f"maestro-server answering at {config.URL}")
    elif port_free():
        add("ok", "port", f"{config.HOST}:{config.PORT} free")
    else:
        add("fail", "port", f"{config.HOST}:{config.PORT} taken by another program", "set `port = ...` in config.toml or MAESTRO_PORT")

    try:
        config.HOME.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=config.HOME):
            pass
        add("ok", "home", f"{config.HOME} writable")
    except OSError as exc:
        add("fail", "home", f"{config.HOME} not writable ({exc.strerror or exc})", "fix its permissions or set MAESTRO_HOME")
    return out


def cmd_doctor(args) -> int:
    results = checks()
    width = max(len(name) for _, name, _, _ in results)
    for level, name, detail, fix in results:
        print(f"{_tag(level)}  {name:<{width}}  {detail}")
        if level != "ok" and fix:
            print(f"      {'':<{width}}  -> {fix}")
    return 1 if any(level == "fail" for level, *_ in results) else 0


# ---------------------------------------------------------------- entry point


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="maestro", description="Run many Claude Code sessions in tmux, directed by one of them.")
    p.add_argument("--version", action="version", version=f"maestro {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    up = sub.add_parser("up", help="start the server and open the panel")
    up.add_argument("--no-open", action="store_true", help="do not open the browser")
    up.add_argument("--foreground", action="store_true", help="run attached; Ctrl-C stops it")
    up.set_defaults(func=cmd_up)

    sub.add_parser("down", help="stop the server started by `up` (sessions keep running)").set_defaults(func=cmd_down)

    st = sub.add_parser("status", help="list sessions and terminals")
    st.add_argument("--json", action="store_true", help="print the raw payload")
    st.set_defaults(func=cmd_status)

    sub.add_parser("open", help="open the panel in the browser").set_defaults(func=cmd_open)

    init = sub.add_parser("init", help="set up ~/.maestro, profiles, the skill and the MCP server")
    init.add_argument("--force", action="store_true", help="overwrite existing files")
    init.set_defaults(func=cmd_init)

    sub.add_parser("doctor", help="check that everything maestro needs is in place").set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
