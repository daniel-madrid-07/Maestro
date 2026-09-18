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
"""

import json
import logging
import os
import queue
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from maestro import __version__, config, events, profiles
from maestro.fleet import InitError, NotReady, fleet

log = logging.getLogger("maestro.server")

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def _truthy(value) -> bool:
    return str(value).lower() in ("1", "true", "yes", "on")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"maestro/{__version__}"

    # ------------------------------------------------------------ plumbing

    def log_message(self, fmt, *args):  # the access log is noise
        pass

    def handle_error(self, request, client_address):  # noqa: D401 - stdlib hook
        """Silence the traceback the stdlib prints when a keep-alive client
        drops the socket between requests; anything else stays loud."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in CORS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

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

    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in CORS.items():
            self.send_header(k, v)
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
        return None

    def _sse(self):
        q = events.log.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        for k, v in CORS.items():
            self.send_header(k, v)
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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(config.LOGS / "server.log", encoding="utf-8")],
    )
    fleet.restore()

    stop = threading.Event()
    threading.Thread(target=fleet.watch, args=(stop,), name="watch", daemon=True).start()

    server = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    server.daemon_threads = True

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
