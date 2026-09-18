"""Claude Code as a maestro provider: how to launch it, read its state, and
pull its last answer out of the rendered screen.

Everything here works on the text tmux renders for the pane -- what a person
would see -- never on the raw escape stream. The status patterns are ported
from CAO's Claude Code provider (Apache-2.0, see NOTICE), which measured them
against real TUI builds; they are kept deliberately narrow.
"""

import json
import os
import re
import stat
import threading
from pathlib import Path

from maestro import config, tmux
from maestro.profiles import Profile

# ---------------------------------------------------------------- patterns

# A live spinner line: glyph, a gerund, an ellipsis ("✻ Cultivating… (3s · ↑ 12 tokens)").
SPINNER = re.compile(r"^[ \t]*[✶✢✽✻✳·*][ \t]+\w*ing\b.*…")
# Older footers say this while a turn runs.
INTERRUPT = "esc to interrupt"
# The Ink selection footer of AskUserQuestion / permission prompts.
WAITING = re.compile(r"↑/↓ to navigate|Enter to confirm[ \t]*·")
PLAN_APPROVAL = "Would you like to proceed?"
OPTION_LINE = re.compile(r"^\s*(?:[❯>]\s*)?\d+\.", re.MULTILINE)
# Dialogs maestro answers itself, so they must never read as "waiting".
TRUST = "Yes, I trust this folder"
BYPASS = "Yes, I accept"
UPGRADE = re.compile(r"Newer \S+ model available")
# Finished-turn summary ("✻ Baked for 12s") and a response marker at line start.
COMPLETION = re.compile(r"[✶✢✽✻✳][^\n…]*\bfor\s+\d+(?:\.\d+)?\s*s\b")
RESPONSE = re.compile(
    r"^[ \t]*[⏺●](?![ \t]*(?:low|medium|high|xhigh|max)[ \t]*·)[ \t]+", re.MULTILINE
)
RAIL = re.compile(r"─{8,}")
PROMPT = re.compile(r"^[ \t]*[>❯](?:[\s\xa0]|$)")
FOOTER = re.compile(r"bypass permissions on|shift\+tab to cycle|\bfor agents\b|/effort")
BANNER = re.compile(r"Claude Code v\d+")

STATUSES = ("unknown", "idle", "processing", "completed", "waiting_user_answer", "error")


def rows_of(screen: str) -> list[str]:
    return [ln.rstrip() for ln in screen.split("\n") if ln.strip()]


def boxed_prompt(rows: list[str]) -> bool:
    """The real input box: a ``❯`` line with a ``────`` rail within two rows above and below."""
    rails = [i for i, ln in enumerate(rows) if RAIL.search(ln)]
    prompts = [i for i, ln in enumerate(rows) if PROMPT.search(ln)]
    return any(
        any(0 < p - r <= 2 for r in rails) and any(0 < r - p <= 2 for r in rails)
        for p in prompts
    )


def detect_status(screen: str) -> str:
    """Classify a rendered screen. ``completed`` here only means "a finished
    answer is on screen"; the fleet decides whether it is new (see Fleet.tick).
    """
    rows = rows_of(screen)
    if not rows:
        return "unknown"
    bottom = rows[-25:]
    bottom_text = "\n".join(bottom)

    if any(SPINNER.search(ln) for ln in bottom) or INTERRUPT in bottom_text:
        return "processing"

    dialog_of_our_own = TRUST in bottom_text or BYPASS in bottom_text or UPGRADE.search(bottom_text)
    if not dialog_of_our_own:
        if WAITING.search(bottom_text):
            return "waiting_user_answer"
        plan = bottom_text.find(PLAN_APPROVAL)
        if plan != -1:
            after = bottom_text[plan + len(PLAN_APPROVAL) :]
            options = list(OPTION_LINE.finditer(after))
            if options and not RAIL.search(after[options[-1].end():]) \
                    and not RESPONSE.search(after[options[-1].end():]):
                return "waiting_user_answer"

    if boxed_prompt(rows):
        everything = "\n".join(rows)
        if COMPLETION.search(everything) or RESPONSE.search(everything):
            return "completed"
        return "idle"
    return "unknown"


def response_count(screen: str) -> int:
    return len(RESPONSE.findall(screen)) + len(COMPLETION.findall(screen))


def last_response(screen: str) -> str | None:
    """Text of the last response marker, up to the input box or the next rail."""
    matches = list(RESPONSE.finditer(screen))
    if not matches:
        return None
    tail = screen[matches[-1].end() :]
    out: list[str] = []
    for line in tail.split("\n"):
        stripped = line.strip()
        if PROMPT.match(line) or RAIL.search(line) or FOOTER.search(line):
            break
        if COMPLETION.search(stripped) or stripped.startswith("⎿"):
            continue
        out.append(stripped)
    text = "\n".join(out).strip()
    return text or None


# ---------------------------------------------------------------- launch

_SETTINGS_LOCK = threading.Lock()


def _claude_config_file() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(config_dir) / ".claude.json" if config_dir else Path.home() / ".claude.json"


def _claude_settings_file() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config_dir) if config_dir else Path.home() / ".claude"
    return base / "settings.json"


def _rewrite_json(path: Path, mutate) -> bool:
    """Read-mutate-write a JSON file atomically, keeping its mode. Returns whether it changed."""
    with _SETTINGS_LOCK:
        data: dict = {}
        mode = 0o600
        if path.exists():
            mode = stat.S_IMODE(os.stat(path).st_mode)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        if not mutate(data):
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return True


def ensure_no_bypass_dialog() -> None:
    """``--dangerously-skip-permissions`` shows a confirmation unless this setting is on."""

    def mutate(settings: dict) -> bool:
        if settings.get("skipDangerousModePermissionPrompt") is True:
            return False
        settings["skipDangerousModePermissionPrompt"] = True
        return True

    _rewrite_json(_claude_settings_file(), mutate)


def ensure_trusted(cwd: str) -> None:
    """Pre-accept the "Do you trust this folder?" dialog for ``cwd``.

    Claude keys projects by ``process.cwd()``; both the given spelling and the
    symlink-resolved one are recorded. Answering the dialog live races the
    renderer (keys sent right after it paints are dropped), so it is avoided
    entirely; ``answer_startup_dialogs`` remains as the fallback.
    """
    candidates = {cwd, os.path.realpath(cwd)}

    def mutate(cfg: dict) -> bool:
        projects = cfg.setdefault("projects", {})
        if not isinstance(projects, dict):
            projects = cfg["projects"] = {}
        changed = False
        for path in candidates:
            entry = projects.get(path)
            if not isinstance(entry, dict):
                entry = projects[path] = {"allowedTools": []}
            if entry.get("hasTrustDialogAccepted") is not True:
                entry["hasTrustDialogAccepted"] = True
                changed = True
        return changed

    path = _claude_config_file()
    if path.exists():  # never invent the file: onboarding must have run once
        _rewrite_json(path, mutate)


def build_command(
    terminal_id: str, profile: Profile, model: str | None, prompt_file: Path, mcp_file: Path | None
) -> str:
    """The shell line that starts Claude Code for one terminal.

    Any ``CLAUDE*`` variable inherited from a parent Claude session is unset first,
    otherwise the child believes it is nested and disables parts of itself.
    """
    parts = ["claude"]
    if profile.permission_mode:
        parts += ["--permission-mode", profile.permission_mode]
    else:
        parts.append("--dangerously-skip-permissions")
    resolved_model = model or profile.model
    if resolved_model:
        parts += ["--model", resolved_model]
    if profile.effort:
        parts += ["--effort", str(profile.effort)]
    parts += ["--append-system-prompt-file", str(prompt_file)]
    if mcp_file is not None:
        parts += ["--mcp-config", str(mcp_file), "--strict-mcp-config"]
    unset = (
        "unset $(env | sed -n 's/^\\(CLAUDE[A-Z_]*\\)=.*/\\1/p' "
        "| grep -v CLAUDE_CONFIG_DIR) 2>/dev/null; "
    )
    return unset + tmux.quote(parts)


def write_launch_files(terminal_id: str, profile: Profile) -> tuple[Path, Path | None]:
    """Persist the system prompt and the MCP config Claude reads at start."""
    config.ensure_dirs()
    prompt_file = config.TMP / f"{terminal_id}.prompt"
    prompt_file.write_text(profile.system_prompt or "", encoding="utf-8")
    prompt_file.chmod(0o600)

    if not profile.mcp_servers:
        return prompt_file, None
    servers: dict = {}
    for name, spec in profile.mcp_servers.items():
        spec = dict(spec) if isinstance(spec, dict) else {"command": str(spec)}
        command = spec.get("command", "")
        # The agent bridge is resolved to its absolute script so the pane's PATH
        # does not matter.
        if command in ("maestro-agent", "cao-mcp-server"):
            spec["command"] = config.console_script("maestro-agent")
        spec.setdefault("args", [])
        env = dict(spec.get("env") or {})
        env.setdefault("MAESTRO_TERMINAL_ID", terminal_id)
        env.setdefault("MAESTRO_URL", config.URL)
        spec["env"] = env
        servers[name] = spec
    mcp_file = config.TMP / f"{terminal_id}.mcp.json"
    mcp_file.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    mcp_file.chmod(0o600)
    return prompt_file, mcp_file


def remove_launch_files(terminal_id: str) -> None:
    for suffix in (".prompt", ".mcp.json"):
        (config.TMP / f"{terminal_id}{suffix}").unlink(missing_ok=True)


def answer_startup_dialogs(pane: str, screen: str, answered: set[str]) -> bool:
    """Dismiss a startup dialog if one is on screen. Returns True when a key was sent.

    Each dialog is answered once (their text stays in scrollback afterwards).
    """
    import time

    tail = "\n".join(rows_of(screen)[-20:])
    if TRUST in tail and "trust" not in answered:
        answered.add("trust")
        # Let the renderer settle before it can accept keys, then re-read to see
        # which option is highlighted (newer builds preselect "No, exit").
        time.sleep(1.0)
        fresh = tmux.capture(pane, 20)
        yes_line = next((ln for ln in fresh.split("\n") if TRUST in ln), "")
        if not re.search(r"[>❯]", yes_line.split(TRUST)[0]):
            tmux.send_key(pane, "Down")
            time.sleep(0.4)
        tmux.send_key(pane, "Enter")
        return True
    if BYPASS in tail and "bypass" not in answered:
        answered.add("bypass")
        time.sleep(1.0)
        tmux.send_key(pane, "Down")
        time.sleep(0.4)
        tmux.send_key(pane, "Enter")
        return True
    if UPGRADE.search(tail) and "upgrade" not in answered:
        answered.add("upgrade")
        time.sleep(0.5)
        tmux.send_key(pane, "2")
        return True
    return False


def looks_ready(screen: str) -> bool:
    return bool(BANNER.search(screen)) and boxed_prompt(rows_of(screen))


def shell_is_back(pane: str) -> bool:
    """True when the pane's foreground process is a shell again (Claude exited)."""
    try:
        return tmux.pane_command(pane) in tmux.SHELLS
    except tmux.TmuxError:
        return False
