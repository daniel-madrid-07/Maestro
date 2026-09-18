"""The fleet: sessions, their terminals, each terminal's inbox, and the loop
that watches every pane and delivers queued messages when a pane is free.

One session = one tmux session (``mx-<name>``); one terminal = one tmux
window running Claude Code. Terminals created by another terminal remember
it as ``caller_id`` so a worker can answer without knowing the address.
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from maestro import claude, config, events, profiles, tmux
from maestro import worktree as worktrees

log = logging.getLogger("maestro.fleet")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,59}$")
READY_STATES = ("idle", "completed")
# A freshly pasted message needs a moment before the screen reflects it.
DISPATCH_GRACE = 3.0
# Never paste into a pane that received something this recently.
DELIVERY_GAP = 3.0
# How long interrupt() lets the screen settle before reporting the status.
INTERRUPT_SETTLE = 2.0
# answer_prompt() tokens sent as a key press rather than typed.
ANSWER_KEYS = {k.lower(): k for k in ("Enter", "Escape", "Up", "Down", "Tab", "Space")}
# Elapsed-time counters ("12s", "1m 3s") tick on a pane that is otherwise frozen.
ELAPSED_RE = re.compile(r"\b\d+(?:\.\d+)?\s*[smh]\b")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def screen_digest(screen: str) -> str:
    """Hash of what a processing pane shows, minus what moves on its own.

    The spinner line animates and counts seconds and tokens even when nothing
    is happening, so it is dropped and elapsed times are blanked; anything
    else changing means real progress.
    """
    rows = [
        ELAPSED_RE.sub("#", ln)
        for ln in screen.split("\n")
        if not claude.SPINNER.search(ln) and claude.INTERRUPT not in ln
    ]
    return hashlib.sha1("\n".join(rows).encode("utf-8", "replace")).hexdigest()


class InitError(RuntimeError):
    pass


class NotReady(RuntimeError):
    pass


@dataclass
class Terminal:
    id: str
    name: str
    session: str
    pane: str
    agent_profile: str
    cwd: str
    model: str | None = None
    caller_id: str | None = None
    created: str = field(default_factory=now_iso)
    worktree: worktrees.Worktree | None = None
    # live state, never persisted
    ready: bool = False
    failed: str | None = None
    status: str = "unknown"
    dispatched: bool = False
    dispatch_at: float = 0.0
    snapshot_response: str | None = None
    snapshot_count: int = 0
    last_delivery: float = 0.0
    last_active: str = field(default_factory=now_iso)
    inbox: deque = field(default_factory=deque)
    waiting_since: str | None = None
    screen_hash: str | None = None  # of the last processing screen (see screen_digest)
    screen_changed: float = 0.0
    stuck: bool = False

    def public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "provider": "claude_code",
            "session_name": self.session,
            "agent_profile": self.agent_profile,
            "caller_id": self.caller_id,
            "model": self.model,
            "working_directory": self.cwd,
            "worktree": self.worktree.public() if self.worktree else None,
            "pane": self.pane,
            "status": self.status,
            "ready": self.ready,
            "error": self.failed,
            "pending_messages": len(self.inbox),
            "created": self.created,
            "last_active": self.last_active,
            "stuck": self.stuck,
            "waiting_since": self.waiting_since,
        }

    def persisted(self) -> dict:
        data = {
            k: getattr(self, k)
            for k in ("id", "name", "session", "pane", "agent_profile", "cwd", "model", "caller_id", "created")
        }
        data["worktree"] = self.worktree.public() if self.worktree else None
        return data


@dataclass
class Session:
    name: str
    cwd: str
    created: str = field(default_factory=now_iso)

    @property
    def tmux(self) -> str:
        return config.SESSION_PREFIX + self.name


class Fleet:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.sessions: dict[str, Session] = {}
        self.terminals: dict[str, Terminal] = {}

    # ------------------------------------------------------------ persistence

    def save(self) -> None:
        with self._lock:
            data = {
                "sessions": [s.__dict__ for s in self.sessions.values()],
                "terminals": [t.persisted() for t in self.terminals.values()],
            }
        config.ensure_dirs()
        tmp = config.STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, config.STATE_FILE)

    def restore(self) -> None:
        """Adopt the sessions a previous server left running; drop the rest."""
        if not config.STATE_FILE.exists():
            return
        try:
            data = json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        live = set(tmux.list_sessions())
        for s in data.get("sessions", []):
            session = Session(**s)
            if session.tmux in live:
                self.sessions[session.name] = session
        for t in data.get("terminals", []):
            wt = t.pop("worktree", None)
            term = Terminal(**t, worktree=worktrees.Worktree(**wt) if wt else None)
            if term.session in self.sessions and tmux.pane_alive(term.pane):
                term.ready = True
                term.dispatched = True  # its history is unknown; trust the screen
                self.terminals[term.id] = term
        log.info("restored %d session(s), %d terminal(s)", len(self.sessions), len(self.terminals))
        self.save()

    # ------------------------------------------------------------ queries

    def list_sessions(self) -> list[dict]:
        with self._lock:
            out = []
            for s in self.sessions.values():
                terms = [t for t in self.terminals.values() if t.session == s.name]
                out.append(
                    {
                        "id": s.name,
                        "name": s.name,
                        "status": "active",
                        "working_directory": s.cwd,
                        "agent_profile": terms[0].agent_profile if terms else None,
                        "terminals": len(terms),
                        "created": s.created,
                    }
                )
            return out

    def session_terminals(self, name: str) -> list[dict]:
        with self._lock:
            if name not in self.sessions:
                raise KeyError(f"session '{name}' not found")
            terms = sorted(
                (t for t in self.terminals.values() if t.session == name), key=lambda t: t.created
            )
            return [t.public() for t in terms]

    def get_session(self, name: str) -> dict:
        with self._lock:
            s = self.sessions.get(name)
            if s is None:
                raise KeyError(f"session '{name}' not found")
            return {
                "id": s.name,
                "name": s.name,
                "session": {"id": s.name, "name": s.name, "working_directory": s.cwd},
                "working_directory": s.cwd,
                "terminals": self.session_terminals(name),
            }

    def get_terminal(self, terminal_id: str) -> Terminal:
        with self._lock:
            t = self.terminals.get(terminal_id)
            if t is None:
                raise KeyError(f"terminal '{terminal_id}' not found")
            return t

    # ------------------------------------------------------------ creation

    def create_terminal(
        self,
        session_name: str,
        profile_name: str,
        cwd: str | None = None,
        model: str | None = None,
        caller_id: str | None = None,
        initial_message: str | None = None,
        orchestration_type: str | None = None,
        sender_id: str | None = None,
        wait: bool = False,
        use_worktree: bool = False,
    ) -> Terminal:
        """Open a window running Claude Code, in a new or existing session.

        Returns as soon as the window exists; Claude starts in the background
        and ``initial_message`` is delivered once it is ready. With ``wait``
        the call blocks until then and raises InitError on failure.
        With ``use_worktree`` the terminal runs in its own git worktree on
        branch ``mx/<terminal_id>`` (see maestro.worktree).
        """
        if not NAME_RE.match(session_name or ""):
            raise ValueError("session_name must match [A-Za-z0-9][A-Za-z0-9_.-]{0,59}")
        profile = profiles.load(profile_name)
        with self._lock:
            session = self.sessions.get(session_name)
            new_session = session is None
            if new_session:
                if not cwd:
                    raise ValueError("working_directory is required for a new session")
                session = Session(name=session_name, cwd=os.path.abspath(cwd))
            cwd = os.path.abspath(cwd or session.cwd)
            if not os.path.isdir(cwd):
                raise ValueError(f"working_directory does not exist: {cwd}")
            if caller_id and caller_id not in self.terminals:
                raise ValueError(f"caller terminal '{caller_id}' not found")

            terminal_id = uuid.uuid4().hex[:8]
            wt = None
            if use_worktree:
                wt = worktrees.create(cwd, terminal_id)
                cwd = wt.path
                if initial_message:
                    initial_message += (
                        f"\n\n[You are working in an isolated git worktree on branch {wt.branch} at {wt.path}. "
                        "Commit your work on this branch before you report; the orchestrator merges it into the main branch.]"
                    )
            try:
                pane, window = self._open_window(session, terminal_id, profile.name, cwd, new_session)
            except Exception:
                if wt:
                    worktrees.remove(wt)
                raise
            term = Terminal(
                id=terminal_id, name=window, session=session.name, pane=pane,
                agent_profile=profile.name, cwd=cwd, model=model, caller_id=caller_id,
                worktree=wt,
            )
            self.sessions[session.name] = session
            self.terminals[terminal_id] = term
            self.save()

        if new_session:
            events.log.emit("post_create_session", None, session.name, session_name=session.name)
        events.log.emit(
            "post_create_terminal", terminal_id, session.name,
            provider="claude_code", agent_name=profile.name, caller_id=caller_id,
        )
        self._start(term, profile, wait, initial_message, orchestration_type, sender_id)
        return term

    def _open_window(self, session: Session, terminal_id: str, profile_name: str, cwd: str, fresh: bool) -> tuple[str, str]:
        """Open the tmux window a terminal lives in; return (pane id, window name)."""
        window = f"{profile_name}-{terminal_id[:4]}"
        env = {
            "MAESTRO_TERMINAL_ID": terminal_id,
            "MAESTRO_URL": config.URL,
            "PATH": config.pinned_path(),
        }
        if fresh or not tmux.has_session(session.tmux):
            pane = tmux.new_session(session.tmux, window, cwd, env)
        else:
            pane = tmux.new_window(session.tmux, window, cwd, env)
        return pane, window

    def _start(self, term: Terminal, profile, wait: bool, initial_message=None, orchestration_type=None, sender_id=None) -> None:
        """Start Claude in the terminal's pane in the background; with ``wait``, block until ready."""
        worker = threading.Thread(
            target=self._initialize,
            args=(term, profile, initial_message, orchestration_type, sender_id),
            name=f"init-{term.id}", daemon=True,
        )
        worker.start()
        if wait:
            worker.join(config.INIT_TIMEOUT + 30)
            if term.failed:
                raise InitError(term.failed)
            if not term.ready:
                raise InitError("initialisation is still running")

    def _initialize(self, term: Terminal, profile, initial_message, orchestration_type, sender_id):
        try:
            deadline = time.time() + 15
            while time.time() < deadline and tmux.pane_command(term.pane) not in tmux.SHELLS:
                time.sleep(0.3)
            claude.ensure_no_bypass_dialog()
            claude.ensure_trusted(term.cwd)
            prompt_file, mcp_file = claude.write_launch_files(term.id, profile)
            tmux.send_line(term.pane, claude.build_command(term.id, profile, term.model, prompt_file, mcp_file))
            started = time.time()
            answered: set[str] = set()
            previous = None
            while time.time() - started < config.INIT_TIMEOUT:
                time.sleep(0.5)
                screen = tmux.capture(term.pane, 40)
                if claude.answer_startup_dialogs(term.pane, screen, answered):
                    previous = None
                    continue
                if claude.looks_ready(screen):
                    if previous == screen:  # two identical frames: the renderer settled
                        break
                    previous = screen
                    continue
                previous = None
                if time.time() - started > 6 and claude.shell_is_back(term.pane):
                    tail = "\n".join(claude.rows_of(screen)[-6:])
                    raise InitError(f"claude exited during startup:\n{tail}")
            else:
                raise InitError(f"Claude Code showed no input box within {config.INIT_TIMEOUT:.0f}s")
            with self._lock:
                term.ready = True
                term.status = "idle"
                term.last_active = now_iso()
            log.info("terminal %s (%s) ready in %s", term.id, term.agent_profile, term.session)
            if initial_message:
                self.send_input(term.id, initial_message, sender_id, orchestration_type)
        except Exception as exc:
            log.error("terminal %s failed to start: %s", term.id, exc)
            term.failed = str(exc)
            events.log.emit("terminal_error", term.id, term.session, agent_name=term.agent_profile, reason=str(exc))
            try:
                self.delete_terminal(term.id)
            except KeyError:
                pass

    # ------------------------------------------------------------ messaging

    def send_input(self, terminal_id: str, text: str, sender_id=None, orchestration_type=None) -> None:
        """Paste ``text`` into the terminal now and mark a turn as dispatched."""
        term = self.get_terminal(terminal_id)
        if not term.ready:
            raise NotReady(f"terminal '{terminal_id}' is still starting")
        self._mark_dispatched(term)
        tmux.send_text(term.pane, text, config.PASTE_SUBMIT_DELAY)
        events.log.emit(
            "post_send_message", terminal_id, term.session,
            sender=sender_id, receiver=terminal_id,
            orchestration_type=orchestration_type or "send_message",
        )

    def _mark_dispatched(self, term: Terminal) -> None:
        """Remember the answer on screen now, so the loop can tell the next one apart."""
        screen = tmux.capture(term.pane, 200)
        with self._lock:
            term.snapshot_response = claude.last_response(screen)
            term.snapshot_count = claude.response_count(screen)
            term.dispatched = True
            term.dispatch_at = time.time()
            term.last_delivery = term.dispatch_at
            term.status = "processing"
            term.last_active = now_iso()

    def queue_message(self, terminal_id: str, text: str, sender_id=None, orchestration_type=None) -> int:
        """Queue ``text``; the watch loop pastes it when the terminal is free."""
        term = self.get_terminal(terminal_id)
        with self._lock:
            term.inbox.append({"message": text, "sender_id": sender_id, "orchestration_type": orchestration_type})
            return len(term.inbox)

    def output(self, terminal_id: str, mode: str = "full") -> dict:
        term = self.get_terminal(terminal_id)
        screen = tmux.capture(term.pane, 2000)
        if mode == "last":
            return {"terminal_id": terminal_id, "mode": mode, "output": claude.last_response(screen) or ""}
        return {"terminal_id": terminal_id, "mode": "full", "output": screen}

    # ------------------------------------------------------------ control

    def answer_prompt(self, terminal_id: str, answer: str) -> Terminal:
        """Answer the question a terminal is blocked on (status waiting_user_answer).

        ``answer`` is one of ANSWER_KEYS (case-insensitive), sent as that key;
        a single digit, sent alone; or anything else, typed literally and
        followed by Enter. Observed on Claude Code 2.1.276: in an option dialog
        (AskUserQuestion) the digit key both selects and submits that option,
        so ``"2"`` answers with the second one and an extra Enter would land in
        whatever is shown next.
        """
        term = self.get_terminal(terminal_id)
        if not term.ready:
            raise NotReady(f"terminal '{terminal_id}' is still starting")
        if term.status != "waiting_user_answer":
            raise ValueError(f"terminal '{terminal_id}' is not waiting for an answer (status {term.status})")
        if not answer:
            raise ValueError("answer is required")
        self._mark_dispatched(term)
        key = ANSWER_KEYS.get(answer.strip().lower())
        if key:
            tmux.send_key(term.pane, key)
        elif len(answer) == 1 and answer.isdigit():
            tmux.send_literal(term.pane, answer)
        else:
            tmux.send_literal(term.pane, answer)
            time.sleep(0.3)
            tmux.send_key(term.pane, "Enter")
        return term

    def interrupt(self, terminal_id: str) -> Terminal:
        """Stop the current turn with Escape; return once the screen had ~2 s to settle."""
        term = self.get_terminal(terminal_id)
        if not term.ready:
            raise NotReady(f"terminal '{terminal_id}' is still starting")
        tmux.send_key(term.pane, "Escape")
        time.sleep(0.3)
        tmux.send_key(term.pane, "Escape")  # the first can be swallowed mid-render
        with self._lock:
            term.dispatched = False  # the interrupted turn will never produce an answer
            term.dispatch_at = 0.0
            term.last_active = now_iso()
        time.sleep(INTERRUPT_SETTLE)
        return term

    def restart_terminal(self, terminal_id: str, wait: bool = True) -> Terminal:
        """Replace a terminal's window with a fresh Claude; same id, profile, model, cwd, caller, inbox."""
        term = self.get_terminal(terminal_id)
        profile = profiles.load(term.agent_profile)
        with self._lock:
            session = self.sessions[term.session]
            term.ready = False  # keeps the watch loop off the dying pane
            old_pane = term.pane
        tmux.kill_window(old_pane)
        with self._lock:
            term.pane, term.name = self._open_window(session, term.id, profile.name, term.cwd, False)
            term.failed = None
            term.status = "unknown"
            term.dispatched = False
            term.dispatch_at = 0.0
            term.snapshot_response = None
            term.snapshot_count = 0
            term.last_delivery = 0.0
            term.last_active = now_iso()
        self.save()
        events.log.emit("terminal_restarted", term.id, term.session, agent_name=term.agent_profile, old_pane=old_pane, pane=term.pane)
        self._start(term, profile, wait)
        return term

    # ------------------------------------------------------------ teardown

    def delete_terminal(self, terminal_id: str) -> dict:
        with self._lock:
            term = self.terminals.pop(terminal_id, None)
            if term is None:
                raise KeyError(f"terminal '{terminal_id}' not found")
            remaining = [t for t in self.terminals.values() if t.session == term.session]
        tmux.kill_window(term.pane)
        claude.remove_launch_files(terminal_id)
        events.log.emit("post_kill_terminal", terminal_id, term.session, agent_name=term.agent_profile)
        if not remaining:
            self._kill_session(term.session)
        self.save()
        result = {"success": True, "terminal_id": terminal_id}
        if term.worktree:
            result["worktree"] = {**term.worktree.public(), **worktrees.remove(term.worktree)}
        return result

    def _kill_session(self, name: str) -> None:
        with self._lock:
            session = self.sessions.pop(name, None)
        if session is None:
            return
        tmux.kill_session(session.tmux)
        events.log.emit("post_kill_session", None, name, session_name=name)

    def delete_session(self, name: str) -> dict:
        with self._lock:
            if name not in self.sessions:
                raise KeyError(f"session '{name}' not found")
            ids = [t.id for t in self.terminals.values() if t.session == name]
        errors = []
        for tid in ids:
            try:
                self.delete_terminal(tid)
            except Exception as exc:  # keep going; the session kill below is the backstop
                errors.append(f"{tid}: {exc}")
        self._kill_session(name)
        self.save()
        return {"success": not errors, "deleted": [name], "errors": errors}

    # ------------------------------------------------------------ watching

    def tick(self) -> None:
        """Re-read every ready pane: refresh its status, deliver one queued message."""
        for term in list(self.terminals.values()):
            if not term.ready:
                continue
            try:
                self._observe(term)
            except tmux.TmuxError as exc:
                log.debug("observe %s: %s", term.id, exc)
            except Exception:
                log.exception("observe %s", term.id)

    def _observe(self, term: Terminal) -> None:
        if not tmux.pane_alive(term.pane):
            if not term.ready:  # restart_terminal is swapping the pane
                return
            log.warning("terminal %s: pane vanished", term.id)
            try:
                self.delete_terminal(term.id)
            except KeyError:
                pass
            return
        screen = tmux.capture(term.pane, 60)
        raw = claude.detect_status(screen)
        now = time.time()
        if term.dispatched and now - term.dispatch_at < DISPATCH_GRACE:
            status = "processing"
        elif raw == "completed":
            if not term.dispatched:
                status = "idle"
            elif (
                claude.response_count(screen) == term.snapshot_count
                and claude.last_response(screen) == term.snapshot_response
            ):
                status = "processing"  # the answer on screen is the previous one
            else:
                status = "completed"
        elif raw == "unknown" and claude.shell_is_back(term.pane):
            status = "error"
        else:
            status = raw
        with self._lock:
            if status != term.status:
                term.status = status
                term.last_active = now_iso()
                if status == "error":
                    events.log.emit("terminal_error", term.id, term.session, agent_name=term.agent_profile, reason="claude exited")
                if status == "waiting_user_answer":
                    term.waiting_since = term.last_active
                    events.log.emit("terminal_waiting", term.id, term.session, agent_name=term.agent_profile)
                else:
                    term.waiting_since = None
            self._check_stuck(term, status, screen, now)
            deliver = (
                term.inbox and status in READY_STATES and now - term.last_delivery > DELIVERY_GAP
            )
            msg = term.inbox.popleft() if deliver else None
        if msg:
            self.send_input(term.id, msg["message"], msg["sender_id"], msg["orchestration_type"])

    def _check_stuck(self, term: Terminal, status: str, screen: str, now: float) -> None:
        """Report a processing pane whose screen has not moved for STUCK_AFTER, once."""
        if status != "processing":
            term.screen_hash, term.stuck = None, False
            return
        digest = screen_digest(screen)
        if digest != term.screen_hash:
            term.screen_hash, term.screen_changed, term.stuck = digest, now, False
        elif not term.stuck and now - term.screen_changed >= config.STUCK_AFTER:
            term.stuck = True
            events.log.emit(
                "terminal_stuck", term.id, term.session,
                agent_name=term.agent_profile, seconds=int(now - term.screen_changed),
            )

    def watch(self, stop: threading.Event) -> None:
        while not stop.is_set():
            started = time.time()
            self.tick()
            stop.wait(max(0.2, config.TICK - (time.time() - started)))


fleet = Fleet()
