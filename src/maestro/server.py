"""HTTP API of maestro-server. Loopback only, JSON in and out, SSE on /events.

The routes the panel and the two MCP servers use mirror CAO's names so the
existing panel works unchanged:

    GET    /health
    GET    /sessions                      POST /sessions?agent_profile=&session_name=&working_directory=&model=&wait=&use_worktree=
    GET    /sessions/{name}               DELETE /sessions/{name}
    GET    /sessions/{name}/terminals     POST /sessions/{name}/terminals   (json body)
    GET    /terminals/{id}                DELETE /terminals/{id}
    POST   /terminals/{id}/input          POST /terminals/{id}/inbox/messages
    POST   /terminals/{id}/progress {percent, note}
    GET    /terminals/{id}/output?mode=full|last
    POST   /terminals/{id}/answer  {answer}  POST /terminals/{id}/interrupt
    POST   /terminals/{id}/restart
    GET    /events   (SSE)                GET /events/history?limit=
    GET    /agents/profiles               GET /agents/profiles/{name}
    GET    /worktrees                     DELETE /worktrees/{terminal_id}   (orphaned checkouts)
    GET    /usage                         (subscription usage, percentages only)
    GET    /  and  GET /{file}            (the panel, from the installed package)
"""

import json
import logging
import os
import queue
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request
from importlib import resources
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from maestro import __version__, config, events, profiles
from maestro.fleet import InitError, NotReady, fleet

log = logging.getLogger("maestro.server")

# No CORS headers, on purpose. The panel is served by this same server, so it
# is same-origin and needs none; the MCP servers are ordinary local clients and
# CORS does not apply to them. Sending `Access-Control-Allow-Origin: *` on an
# API with no auth that starts processes would let ANY page the operator has
# open in their browser drive the fleet -- the classic localhost drive-by.
# `_same_origin_only` below closes the other half of that hole.
ALLOWED_CONTENT_TYPES = ("application/json", "")


def _truthy(value) -> bool:
    return str(value).lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- panel files

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".txt": "text/plain; charset=utf-8",
}
LONG_CACHE = {".woff2", ".woff", ".ttf", ".otf", ".svg", ".png", ".ico", ".jpg", ".webp"}


def panel_root():
    """The panel directory inside the installed package (works from a wheel)."""
    return resources.files("maestro") / "panel"


def static_file(rel: str) -> tuple[bytes, str, str] | None:
    """``(body, content type, cache control)`` for a panel file, or None.

    Only files under the panel directory are served: ``..``, absolute paths,
    backslashes and symlinks that resolve outside it are refused.
    """
    rel = urllib.parse.unquote(rel).strip("/") or "index.html"
    parts = rel.split("/")
    if any(p in ("", ".", "..") or "\\" in p or ":" in p or "\0" in p for p in parts):
        return None
    root = panel_root()
    node = root.joinpath(*parts)
    if isinstance(node, Path):
        try:
            node.resolve(strict=True).relative_to(Path(root).resolve(strict=True))
        except (OSError, ValueError):
            return None
    if not node.is_file():
        return None
    ext = os.path.splitext(parts[-1])[1].lower()
    if ext == ".html":
        cache = "no-store"
    elif ext in LONG_CACHE:
        cache = "public, max-age=604800"
    else:
        cache = "no-cache"
    return node.read_bytes(), CONTENT_TYPES.get(ext, "application/octet-stream"), cache


# ---------------------------------------------------------------- usage

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
USAGE_TTL = 60.0

_usage_cache = {"at": 0.0, "data": None}
_usage_lock = threading.Lock()


def read_usage() -> dict:
    """Claude subscription utilisation (5-hour and weekly windows), cached 60 s.

    The endpoint is the one Claude Code's own ``/usage`` reads. It is NOT a
    documented API, so every failure is reported as ``{"ok": false}`` (or the
    last good reading flagged ``stale``) and never raised. The OAuth token stays
    in this process: callers only ever get percentages and reset times. On macOS
    the credentials may live in the Keychain; we do not shell out for them.
    """
    with _usage_lock:
        if time.monotonic() - _usage_cache["at"] < USAGE_TTL and _usage_cache["data"]:
            return _usage_cache["data"]
        if not CREDENTIALS.is_file():
            return {"ok": False, "message": "no local credentials"}
        try:
            token = json.loads(CREDENTIALS.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"]
            req = urllib.request.Request(
                USAGE_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "anthropic-beta": "oauth-2025-04-20",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = json.load(resp)

            def window(key):
                block = raw.get(key) or {}
                return {"percent": round(block.get("utilization") or 0), "resets_at": block.get("resets_at")}

            data = {"ok": True, "session": window("five_hour"), "week": window("seven_day")}
        except Exception as exc:  # noqa: BLE001 - undocumented endpoint, see docstring
            # A transient miss (429, token refresh, network) must not blank the
            # panel: hand back the last reading, flagged, while one exists.
            if _usage_cache["data"]:
                return {**_usage_cache["data"], "stale": True, "message": type(exc).__name__}
            return {"ok": False, "message": type(exc).__name__}
        _usage_cache.update(at=time.monotonic(), data=data)
        return data


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # noqa: D401 - stdlib hook
        """Silence the traceback the stdlib prints when a keep-alive client
        drops the socket between requests; anything else stays loud."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"maestro/{__version__}"

    # ------------------------------------------------------------ plumbing

    def log_message(self, fmt, *args):  # the access log is noise
        pass

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, body: bytes, content_type: str, cache: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)
        return True

    def _params(self) -> dict:
        """Query string and JSON body merged; the body wins."""
        parts = urlsplit(self.path)
        params = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                raise ValueError("body is not valid JSON")
            if isinstance(body, dict):
                params.update(body)
        return params

    def _same_origin_only(self) -> bool:
        """Refuse anything a web page on another origin sent us.

        A browser attaches ``Origin`` to every cross-origin request and to every
        POST; a page that is not our own panel therefore cannot get past this,
        even for a "simple" request that needs no preflight. Local clients
        (the MCP servers, curl, the CLI) send no ``Origin`` at all and are
        unaffected. Belt to that: a body must be declared JSON, which a form
        post from a hostile page cannot claim without a preflight.
        """
        origin = self.headers.get("Origin")
        if origin:
            host = self.headers.get("Host") or ""
            if origin not in (f"http://{host}", f"https://{host}"):
                self._json(403, {"detail": "cross-origin requests are refused"})
                return False
        if self.command in ("POST", "PUT", "PATCH"):
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype not in ALLOWED_CONTENT_TYPES:
                self._json(415, {"detail": "send application/json"})
                return False
        return True

    def do_OPTIONS(self):
        # Nothing to negotiate: there is no cross-origin access to grant.
        self.send_response(405)
        self.send_header("Allow", "GET, POST, DELETE")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method: str) -> None:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        segs = path.split("/")[1:]
        try:
            if not self._same_origin_only():
                return
            handled = self._dispatch(method, segs)
            if handled is None:
                self._json(404, {"detail": f"no route for {method} {path}"})
        except KeyError as exc:
            self._json(404, {"detail": str(exc).strip("'")})
        except FileNotFoundError as exc:
            self._json(404, {"detail": str(exc)})
        except (ValueError, NotReady) as exc:
            self._json(400, {"detail": str(exc)})
        except InitError as exc:
            self._json(500, {"detail": f"launch failed: {exc}"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            log.exception("%s %s", method, path)
            self._json(500, {"detail": f"{type(exc).__name__}: {exc}"})

    # ------------------------------------------------------------ routes

    def _dispatch(self, method: str, segs: list[str]):
        head = segs[0] if segs else ""
        if head == "health" and method == "GET":
            return self._json(200, {"ok": True, "maestro": True, "version": __version__, "sessions": len(fleet.sessions), "terminals": len(fleet.terminals)})

        if segs == ["usage"] and method == "GET":
            return self._json(200, read_usage())

        if head == "events" and method == "GET":
            if len(segs) == 1:
                return self._sse()
            if segs[1] == "history":
                limit = int(self._params().get("limit", 100))
                return self._json(200, {"events": events.log.history(limit)})

        if head == "agents" and len(segs) >= 2 and segs[1] == "profiles" and method == "GET":
            if len(segs) == 2:
                return self._json(200, profiles.list_all())
            p = profiles.load(segs[2])
            return self._json(200, {**p.summary(), "system_prompt": p.system_prompt, "mcpServers": p.mcp_servers})

        if head == "sessions":
            if len(segs) == 1:
                if method == "GET":
                    return self._json(200, fleet.list_sessions())
                if method == "POST":
                    p = self._params()
                    name = p.get("session_name") or f"s-{os.urandom(3).hex()}"
                    term = fleet.create_terminal(
                        name, p.get("agent_profile", "worker"), p.get("working_directory"),
                        model=p.get("model"), initial_message=p.get("initial_message"),
                        orchestration_type=p.get("orchestration_type"), sender_id=p.get("sender_id"),
                        wait=_truthy(p.get("wait", "false")),
                        use_worktree=_truthy(p.get("use_worktree", "false")),
                    )
                    return self._json(201, term.public())
            name = segs[1]
            if len(segs) == 2:
                if method == "GET":
                    return self._json(200, fleet.get_session(name))
                if method == "DELETE":
                    return self._json(200, fleet.delete_session(name))
            if len(segs) == 3 and segs[2] == "terminals":
                if method == "GET":
                    return self._json(200, fleet.session_terminals(name))
                if method == "POST":
                    p = self._params()
                    term = fleet.create_terminal(
                        name, p.get("agent_profile", "worker"), p.get("working_directory"),
                        model=p.get("model"), caller_id=p.get("caller_id"),
                        initial_message=p.get("initial_message"),
                        orchestration_type=p.get("orchestration_type"), sender_id=p.get("sender_id"),
                        wait=_truthy(p.get("wait", "false")),
                        use_worktree=_truthy(p.get("use_worktree", "false")),
                    )
                    return self._json(201, term.public())

        if head == "worktrees":
            if len(segs) == 1 and method == "GET":
                return self._json(200, fleet.worktrees_report())
            if len(segs) == 2 and method == "DELETE":
                return self._json(200, fleet.remove_orphan(segs[1]))

        if head == "terminals" and len(segs) >= 2:
            tid = segs[1]
            if len(segs) == 2:
                if method == "GET":
                    return self._json(200, fleet.get_terminal(tid).public())
                if method == "DELETE":
                    return self._json(200, fleet.delete_terminal(tid))
                return None
            sub = segs[2]
            if sub == "output" and method == "GET":
                mode = self._params().get("mode", "full")
                if mode not in ("full", "last"):
                    raise ValueError("mode must be 'full' or 'last'")
                return self._json(200, fleet.output(tid, mode))
            if sub == "input" and method == "POST":
                p = self._params()
                if not p.get("message"):
                    raise ValueError("message is required")
                fleet.send_input(tid, p["message"], p.get("sender_id"), p.get("orchestration_type"))
                return self._json(200, {"success": True, "terminal_id": tid, "delivered": True})
            if sub == "inbox" and method == "POST":
                p = self._params()
                if not p.get("message"):
                    raise ValueError("message is required")
                n = fleet.queue_message(tid, p["message"], p.get("sender_id"), p.get("orchestration_type"))
                return self._json(200, {"success": True, "terminal_id": tid, "queued": n})
            if sub == "answer" and method == "POST":
                p = self._params()
                if not isinstance(p.get("answer"), str) or not p["answer"]:
                    raise ValueError("answer is required")
                return self._json(200, {**fleet.answer_prompt(tid, p["answer"]).public(), "success": True})
            if sub == "progress" and method == "POST":
                p = self._params()
                if "percent" not in p:
                    raise ValueError("percent is required")
                return self._json(200, {**fleet.report_progress(tid, p["percent"], p.get("note", "")).public(), "success": True})
            if sub == "interrupt" and method == "POST":
                return self._json(200, {**fleet.interrupt(tid).public(), "success": True})
            if sub == "restart" and method == "POST":
                wait = _truthy(self._params().get("wait", "true"))
                return self._json(200, {**fleet.restart_terminal(tid, wait=wait).public(), "success": True})
        if method == "GET":
            found = static_file("/".join(segs))
            if found:
                return self._file(*found)
        return None

    def _sse(self):
        q = events.log.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            events.log.unsubscribe(q)
        return True


def main() -> None:
    config.ensure_dirs()
    os.environ["PATH"] = config.pinned_path()
    # `maestro up` points stderr at server.log itself; echoing there would
    # write every line twice.
    handlers = [logging.FileHandler(config.LOGS / "server.log", encoding="utf-8")]
    if sys.stderr.isatty():
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    fleet.restore()

    stop = threading.Event()
    threading.Thread(target=fleet.watch, args=(stop,), name="watch", daemon=True).start()

    server = Server((config.HOST, config.PORT), Handler)

    def shutdown(signum, _frame):
        log.info("signal %s: shutting down", signum)
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log.info("maestro-server %s listening on http://%s:%d", __version__, config.HOST, config.PORT)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
