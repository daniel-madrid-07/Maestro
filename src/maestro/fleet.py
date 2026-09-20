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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from maestro import claude, config, events, profiles
from maestro.term import backend as console
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


# What a terminal is marked with when its window disappeared under it.
HOST_DIED = "terminal window disappeared (tmux server killed, or the window closed by hand); restart_terminal brings it back"


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
    # Self-reported by the agent through report_progress: {"percent", "note", "at"}.
    progress: dict | None = None
    restarting: bool = False  # a failed start then keeps the terminal (status error)
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
            "progress": self.progress,
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
        self._starting = threading.BoundedSemaphore(config.MAX_STARTING)
        self.sessions: dict[str, Session] = {}
        self.terminals: dict[str, Terminal] = {}
        # Worktrees whose terminal died with a previous server; never removed
        # on their own (they may hold unmerged work), listed under /worktrees.
        self.orphans: list[dict] = []

    # ------------------------------------------------------------ persistence

    def save(self) -> None:
        with self._lock:
            data = {
                "sessions": [s.__dict__ for s in self.sessions.values()],
                "terminals": [t.persisted() for t in self.terminals.values()],
                "orphans": list(self.orphans),
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
        live = set(console.list_sessions())
        # A fleet started by a Maestro that predates the dedicated tmux server
        # lives on the default one. Rather than lose it, this run stays on the
        # default server; the move happens the next time the fleet is empty.
        wanted = {Session(**s).tmux for s in data.get("sessions", [])}
        if wanted and not (wanted & live) and config.TMUX_SOCKET:
            stray = set(console.stray_sessions(config.SESSION_PREFIX))
            if wanted & stray:
                log.warning(
                    "the running fleet is on the default tmux server; staying there for this run "
                    "(Maestro moves to its own server, -L %s, once the fleet is empty)", config.TMUX_SOCKET,
                )
                config.TMUX_SOCKET = ""
                live = set(console.list_sessions())
        self.orphans = [o for o in data.get("orphans", []) if os.path.isdir(o.get("path", ""))]
        for s in data.get("sessions", []):
            session = Session(**s)
            if session.tmux in live:
                self.sessions[session.name] = session
        for t in data.get("terminals", []):
            wt = t.pop("worktree", None)
            term = Terminal(**t, worktree=worktrees.Worktree(**wt) if wt else None)
            if term.session in self.sessions and console.pane_alive(term.pane):
                term.ready = True
                term.dispatched = True  # its history is unknown; trust the screen
                self.terminals[term.id] = term
            elif wt and os.path.isdir(wt["path"]):
                self.orphans.append({"terminal_id": term.id, "session_name": term.session, "agent_profile": term.agent_profile, **wt})
                log.warning("terminal %s is gone; its worktree %s is kept as orphaned", term.id, wt["path"])
        log.info("restored %d session(s), %d terminal(s), %d orphaned worktree(s)", len(self.sessions), len(self.terminals), len(self.orphans))
        stray = console.stray_sessions(config.SESSION_PREFIX)
        if stray:
            log.warning(
                "%d session(s) on the default tmux server predate Maestro's own (%s) and are not managed: %s. "
                "Attach with `tmux attach -t <name>`, or end them with `tmux kill-session -t <name>`.",
                len(stray), config.TMUX_SOCKET, ", ".join(stray),
            )
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

        # Outside the lock: a checkout of a /mnt/c repository can take many
        # seconds, and the watch loop must keep running meanwhile.
        wt = None
        if use_worktree:
            wt = worktrees.create(cwd, terminal_id)
            # Keep the caller's subdirectory, now inside the new checkout.
            cwd = os.path.normpath(os.path.join(wt.path, os.path.relpath(cwd, wt.repo)))
            if initial_message:
                initial_message += (
                    f"\n\n[You are working in an isolated git worktree on branch {wt.branch} at {wt.path}. "
                    "Commit your work on this branch before you report; the orchestrator merges it into the main branch.]"
                )

        with self._lock:
            if new_session and session_name in self.sessions:  # created meanwhile
                session, new_session = self.sessions[session_name], False
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
        if fresh or not console.has_session(session.tmux):
            pane = console.new_session(session.tmux, window, cwd, env)
        else:
            pane = console.new_window(session.tmux, window, cwd, env)
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
            # Behind a queue of other starts, this one is not late until its own
            # turn has come and gone.
            with self._lock:
                ahead = sum(1 for t in self.terminals.values() if not t.ready and not t.failed and t is not term)
            worker.join(config.INIT_TIMEOUT * (1 + ahead // config.MAX_STARTING) + 30)
            if term.failed:
                raise InitError(term.failed)
            if not term.ready:
                raise InitError("initialisation is still running")

    def _initialize(self, term: Terminal, profile, initial_message, orchestration_type, sender_id):
        # Starts are staggered, never capped: with two orchestrators launching
        # at once, twenty Claudes booting together is what stalls tmux and every
        # screen read for everyone. Queued starts wait their turn here.
        with self._starting:
            self._initialize_now(term, profile, initial_message, orchestration_type, sender_id)

    def _initialize_now(self, term: Terminal, profile, initial_message, orchestration_type, sender_id):
        try:
            claude.ensure_no_bypass_dialog()
            claude.ensure_trusted(term.cwd)
            prompt_file, mcp_file = claude.write_launch_files(term.id, profile)
            console.launch(term.pane, claude.launch_argv(profile, term.model, prompt_file, mcp_file))
            started = time.time()
            answered: set[str] = set()
            previous = None
            while time.time() - started < config.INIT_TIMEOUT:
                time.sleep(0.5)
                screen = console.capture(term.pane, 40)
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
                term.restarting = False
                term.status = "idle"
                term.last_active = now_iso()
            log.info("terminal %s (%s) ready in %s", term.id, term.agent_profile, term.session)
            if initial_message:
                self.send_input(term.id, initial_message, sender_id, orchestration_type)
        except Exception as exc:
            log.error("terminal %s failed to start: %s", term.id, exc)
            term.failed = str(exc)
            events.log.emit("terminal_error", term.id, term.session, agent_name=term.agent_profile, reason=str(exc))
            if term.restarting:
                # A restart that fails keeps the terminal (inbox, worktree, id)
                # so the orchestrator can try again or read what happened.
                with self._lock:
                    term.restarting = False
                    term.status = "error"
                return
            try:
                self.delete_terminal(term.id)
            except KeyError:
                pass

    # ------------------------------------------------------------ messaging

    def send_input(self, terminal_id: str, text: str, sender_id=None, orchestration_type=None) -> None:
        """Paste ``text`` into the terminal now and mark a turn as dispatched."""
        term = self.get_terminal(terminal_id)
        with self._lock:
            if not term.ready:
                raise NotReady(f"terminal '{terminal_id}' is still starting")
            # Pinned here: if a restart swaps the pane meanwhile, the paste goes
            # to the dead one and fails, never into the new pane's bare shell.
            pane = term.pane
        self._mark_dispatched(term, pane)
        console.send_text(pane, text, config.PASTE_SUBMIT_DELAY)
        with self._lock:
            # The paste itself took ~2 s; the grace period counts from the Enter.
            term.dispatch_at = term.last_delivery = time.time()
        events.log.emit(
            "post_send_message", terminal_id, term.session,
            sender=sender_id, receiver=terminal_id,
            orchestration_type=orchestration_type or "send_message",
        )

    def _mark_dispatched(self, term: Terminal, pane: str | None = None) -> None:
        """Remember the answer on screen now, so the loop can tell the next one apart.

        Same capture depth as ``_observe``: the counts are compared against it.
        """
        screen = console.capture(pane or term.pane, 60)
        with self._lock:
            term.snapshot_response = claude.last_response(screen)
            term.snapshot_count = claude.response_count(screen)
            term.dispatched = True
            term.dispatch_at = time.time()
            term.last_delivery = term.dispatch_at
            term.status = "processing"
            term.waiting_since, term.stuck, term.screen_hash = None, False, None
            term.last_active = now_iso()

    def queue_message(self, terminal_id: str, text: str, sender_id=None, orchestration_type=None) -> int:
        """Queue ``text``; the watch loop pastes it when the terminal is free."""
        term = self.get_terminal(terminal_id)
        with self._lock:
            term.inbox.append({"message": text, "sender_id": sender_id, "orchestration_type": orchestration_type})
            return len(term.inbox)

    # ------------------------------------------------------------ the whole fleet

    # An orchestrator asks the same three questions all day: what is running,
    # who needs me, and is anyone done yet. Answering them one session at a
    # time costs a call and a chunk of context each; these answer them whole.

    SETTLED = ("completed", "waiting_user_answer", "error")

    def snapshot(self) -> dict:
        """Every session and terminal, plus who is waiting on the orchestrator."""
        with self._lock:
            terms = sorted((t.public() for t in self.terminals.values()), key=lambda t: t["created"])
            sessions = [
                {
                    "name": s.name,
                    "working_directory": s.cwd,
                    "created": s.created,
                    "terminals": [t["id"] for t in terms if t["session_name"] == s.name],
                }
                for s in self.sessions.values()
            ]
        by_status: dict[str, int] = {}
        for t in terms:
            state = "stuck" if t.get("stuck") else (t.get("status") or "unknown")
            by_status[state] = by_status.get(state, 0) + 1
        return {
            "sessions": sessions,
            "terminals": terms,
            "totals": {"sessions": len(sessions), "terminals": len(terms), "by_status": by_status},
            # The point of the whole call: a worker cannot ask for you, so this
            # is where an unanswered question or a dead session surfaces.
            "needs_attention": [
                {
                    "terminal_id": t["id"],
                    "session_name": t["session_name"],
                    "why": "stuck" if t.get("stuck") else t["status"],
                    "waiting_since": t.get("waiting_since"),
                }
                for t in terms
                if t.get("stuck") or t.get("status") in ("waiting_user_answer", "error")
            ],
        }

    def wait_for(self, terminal_ids=None, states=None, timeout: float = 300.0,
                 require_all: bool = False) -> dict:
        """Block until terminals reach one of ``states``, or the timeout runs out.

        This is what replaces polling: the caller asks once and is answered the
        moment a worker finishes, asks a question or dies, instead of spending a
        call and a turn every few seconds to find out.
        """
        wanted = tuple(states or self.SETTLED)
        started = time.monotonic()
        deadline = started + max(1.0, min(float(timeout), 900.0))
        while True:
            with self._lock:
                unknown = [i for i in (terminal_ids or []) if i not in self.terminals]
                watched = [t for t in self.terminals.values()
                           if not terminal_ids or t.id in terminal_ids]
                ready, pending = [], []
                for t in watched:
                    state = "stuck" if (t.stuck and t.status == "processing") else t.status
                    row = {
                        "terminal_id": t.id, "session_name": t.session, "status": state,
                        "stuck": bool(t.stuck), "pending_messages": len(t.inbox),
                    }
                    # A queued message it has not read yet is work it has not
                    # started: "completed" there means the previous turn.
                    (ready if (state in wanted and not t.inbox) else pending).append(row)
            hit = (bool(watched) and not pending) if require_all else bool(ready)
            if hit or not watched or time.monotonic() >= deadline:
                return {
                    "matched": ready, "pending": pending, "unknown": unknown,
                    "timed_out": not hit,
                    "waited_seconds": round(time.monotonic() - started, 1),
                }
            time.sleep(0.4)

    def create_many(self, specs: list[dict], max_parallel: int = 4) -> list[dict]:
        """Launch several sessions in parallel; one result per spec, in order.

        Sequential launches cost the caller a call and ~10-30 s each, so ten
        workers took minutes to even exist. Each still blocks until its Claude
        is ready, so a failure is still reported against its own spec.
        """
        def one(spec: dict) -> dict:
            name = spec.get("session_name") or f"s-{os.urandom(3).hex()}"
            try:
                term = self.create_terminal(
                    name,
                    spec.get("agent_profile") or "worker",
                    spec.get("working_directory"),
                    model=spec.get("model"),
                    initial_message=spec.get("initial_message"),
                    orchestration_type="launch",
                    sender_id=spec.get("sender_id") or "maestro-ops",
                    wait=True,
                    use_worktree=bool(spec.get("use_worktree")),
                )
            except Exception as exc:  # noqa: BLE001 - one bad spec must not sink the batch
                log.warning("batch launch of %s failed: %s", name, exc)
                return {"success": False, "session_name": name, "terminal_id": None,
                        "message": f"{type(exc).__name__}: {exc}"}
            return {"success": True, "session_name": term.session, "terminal_id": term.id,
                    "worktree": term.worktree.public() if term.worktree else None}

        if not specs:
            return []
        width = max(1, min(int(max_parallel or 4), 8))
        with ThreadPoolExecutor(max_workers=width, thread_name_prefix="batch") as pool:
            return list(pool.map(one, specs))

    def broadcast(self, message: str, terminal_ids=None, session_names=None,
                  sender_id=None) -> list[dict]:
        """Queue one message for many terminals (all of them, when nothing is named)."""
        with self._lock:
            targets = [
                t for t in self.terminals.values()
                if (not terminal_ids and not session_names)
                or (terminal_ids and t.id in terminal_ids)
                or (session_names and t.session in session_names)
            ]
        out = []
        for t in targets:
            out.append({"terminal_id": t.id, "session_name": t.session,
                        "queued": self.queue_message(t.id, message, sender_id, "broadcast")})
        return out

    def output(self, terminal_id: str, mode: str = "full") -> dict:
        term = self.get_terminal(terminal_id)
        screen = console.capture(term.pane, 2000)
        if mode == "last":
            return {"terminal_id": terminal_id, "mode": mode, "output": claude.last_response(screen) or ""}
        return {"terminal_id": terminal_id, "mode": "full", "output": screen}

    # ------------------------------------------------------------ progress

    def report_progress(self, terminal_id: str, percent, note: str = "") -> Terminal:
        """Record what an agent says about its own task: 0-100 and a few words."""
        term = self.get_terminal(terminal_id)
        try:
            value = int(round(float(percent)))
        except (TypeError, ValueError):
            raise ValueError("percent must be a number from 0 to 100")
        if not 0 <= value <= 100:
            raise ValueError("percent must be from 0 to 100")
        note = " ".join(str(note or "").split())[:80]
        with self._lock:
            term.progress = {"percent": value, "note": note, "at": now_iso()}
            term.last_active = term.progress["at"]
        events.log.emit("terminal_progress", terminal_id, term.session, agent_name=term.agent_profile, percent=value, note=note)
        return term

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
        if "\n" in answer or "\r" in answer:
            raise ValueError("answer must be a single line (a newline would submit halfway)")
        key = ANSWER_KEYS.get(answer.strip().lower())
        if key == "Escape":
            # Cancelling the question ends the turn without an answer; marking a
            # dispatch would leave the loop waiting for one forever.
            self._clear_dispatch(term)
            console.send_key(term.pane, key)
            return term
        self._mark_dispatched(term)
        if key:
            console.send_key(term.pane, key)
        elif len(answer) == 1 and answer.isdigit():
            console.send_literal(term.pane, answer)
        else:
            console.send_literal(term.pane, answer)
            time.sleep(0.3)
            console.send_key(term.pane, "Enter")
        return term

    def _clear_dispatch(self, term: Terminal) -> None:
        with self._lock:
            term.dispatched = False  # the interrupted turn will never produce an answer
            term.dispatch_at = 0.0
            term.last_active = now_iso()

    def interrupt(self, terminal_id: str) -> Terminal:
        """Stop the current turn with Escape; return once the screen had ~2 s to settle.

        Only while ``processing`` or ``waiting_user_answer``: on an idle prompt
        Escape does nothing and a second one opens Claude's Rewind menu, which
        would leave the terminal blocked (observed on Claude Code 2.1.276).
        """
        term = self.get_terminal(terminal_id)
        if not term.ready:
            raise NotReady(f"terminal '{terminal_id}' is still starting")
        if term.status not in ("processing", "waiting_user_answer"):
            raise ValueError(f"terminal '{terminal_id}' has nothing to interrupt (status {term.status})")
        self._clear_dispatch(term)
        console.send_key(term.pane, "Escape")
        time.sleep(1.5)
        if claude.detect_status(console.capture(term.pane, 40)) == "processing":
            console.send_key(term.pane, "Escape")  # the first was swallowed mid-render
            time.sleep(INTERRUPT_SETTLE)
        return term

    def restart_terminal(self, terminal_id: str, wait: bool = True) -> Terminal:
        """Replace a terminal's window with a fresh Claude; same id, profile, model, cwd, caller, inbox."""
        term = self.get_terminal(terminal_id)
        profile = profiles.load(term.agent_profile)
        with self._lock:
            if not term.ready and not term.failed:
                raise ValueError(f"terminal '{terminal_id}' is still starting; wait for it")
            session = self.sessions.get(term.session)
            if session is None:
                raise KeyError(f"session '{term.session}' of terminal '{terminal_id}' no longer exists")
            term.ready = False  # keeps the watch loop off the dying pane
            term.restarting = True
            old_pane = term.pane
        console.kill_window(old_pane)
        with self._lock:
            try:
                term.pane, term.name = self._open_window(session, term.id, profile.name, term.cwd, False)
            except Exception as exc:
                term.failed, term.status, term.restarting = f"restart failed: {exc}", "error", False
                raise
            term.failed = None
            term.status = "unknown"
            term.dispatched = False
            term.dispatch_at = 0.0
            term.snapshot_response = None
            term.snapshot_count = 0
            term.last_delivery = 0.0
            term.waiting_since, term.stuck, term.screen_hash = None, False, None
            term.last_active = now_iso()
            # The new Claude knows nothing of the old conversation: re-tell it
            # who to report to and where it works, ahead of anything queued.
            reminders = []
            if term.caller_id:
                reminders.append(f"Terminal {term.caller_id} assigned your task; report to it with send_message (no receiver_id needed).")
            if term.worktree:
                reminders.append(f"You work in an isolated git worktree on branch {term.worktree.branch} at {term.worktree.path}; commit there before you report.")
            if reminders:
                term.inbox.appendleft({
                    "message": "[Maestro: this terminal was restarted and its previous conversation is gone. " + " ".join(reminders) + "]",
                    "sender_id": None, "orchestration_type": "restart",
                })
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
        console.kill_window(term.pane)
        claude.remove_launch_files(terminal_id)
        events.log.emit("post_kill_terminal", terminal_id, term.session, agent_name=term.agent_profile)
        if not remaining:
            self._kill_session(term.session)
        self.save()
        result = {"success": True, "terminal_id": terminal_id}
        if term.worktree:
            result["worktree"] = {**term.worktree.public(), **worktrees.remove(term.worktree)}
            claude.forget_trusted(term.cwd)  # one-off checkout; no trust entry left behind
        return result

    # ------------------------------------------------------------ worktrees

    def worktrees_report(self) -> dict:
        """Live worktree terminals plus checkouts orphaned by a server restart."""
        with self._lock:
            live = [
                {"terminal_id": t.id, "session_name": t.session, "status": t.status, **t.worktree.public()}
                for t in self.terminals.values() if t.worktree
            ]
            return {"live": live, "orphaned": list(self.orphans)}

    def remove_orphan(self, terminal_id: str) -> dict:
        with self._lock:
            orphan = next((o for o in self.orphans if o["terminal_id"] == terminal_id), None)
            if orphan is None:
                raise KeyError(f"no orphaned worktree for terminal '{terminal_id}'")
        wt = worktrees.Worktree(repo=orphan["repo"], path=orphan["path"], branch=orphan["branch"])
        res = worktrees.remove(wt)
        claude.forget_trusted(wt.path)
        with self._lock:
            self.orphans = [o for o in self.orphans if o["terminal_id"] != terminal_id]
        self.save()
        return {"success": res["removed"], "terminal_id": terminal_id, "worktree": {**wt.public(), **res}}

    def _kill_session(self, name: str) -> None:
        with self._lock:
            session = self.sessions.pop(name, None)
        if session is None:
            return
        console.kill_session(session.tmux)
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
            except console.Error as exc:
                log.debug("observe %s: %s", term.id, exc)
            except Exception:
                log.exception("observe %s", term.id)

    def _observe(self, term: Terminal) -> None:
        if not console.pane_alive(term.pane):
            if not term.ready:  # restart_terminal is swapping the pane
                return
            if term.failed == HOST_DIED:
                return  # already reported; nothing to read until it is restarted
            # The window went away under us: the tmux server was killed, or
            # someone closed it by hand. Deleting the terminal here used to
            # throw away its id, its inbox and its worktree record along with
            # it; kept as an error, `restart_terminal` brings it back with all
            # three, and `needs_attention` says what happened.
            log.warning("terminal %s: pane vanished (%s)", term.id, HOST_DIED)
            with self._lock:
                term.failed = HOST_DIED
                term.status = "error"
                term.last_active = now_iso()
                term.waiting_since, term.stuck = None, False
            events.log.emit("terminal_error", term.id, term.session, agent_name=term.agent_profile, reason=HOST_DIED)
            self.save()
            return
        screen = console.capture(term.pane, 60)
        if claude.REWIND in "\n".join(claude.rows_of(screen)[-20:]):
            log.info("terminal %s: dismissing the Rewind menu", term.id)
            console.send_key(term.pane, "Escape")
            return
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
                term.inbox and term.ready and status in READY_STATES
                and now - term.last_delivery > DELIVERY_GAP
            )
            msg = term.inbox.popleft() if deliver else None
        if msg:
            try:
                self.send_input(term.id, msg["message"], msg["sender_id"], msg["orchestration_type"])
            except (NotReady, console.Error) as exc:
                with self._lock:
                    term.inbox.appendleft(msg)  # the pane went away under us; next tick retries
                log.warning("terminal %s: delivery deferred: %s", term.id, exc)

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
