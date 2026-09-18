"""maestro-ops: the MCP server an outside orchestrator (Claude in VS Code) uses.

Tool names and result shapes follow CAO's ``cao-ops-mcp`` so the ``/cao``
skill and existing habits carry over unchanged.
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

from maestro import config
from maestro.client import ApiError, request

mcp = FastMCP("maestro-ops")


def _fail(op: str, exc: ApiError) -> dict[str, Any]:
    return {"success": False, "message": f"{op} failed: {exc.detail}"}


@mcp.tool()
def list_profiles() -> dict[str, Any]:
    """List the agent profiles a session can be launched with (name, role, model, effort)."""
    try:
        return {"success": True, "profiles": request("GET", "/agents/profiles")}
    except ApiError as exc:
        return _fail("List profiles", exc)


@mcp.tool()
def get_profile_details(profile_name: str) -> dict[str, Any]:
    """Return a profile's full system prompt and settings."""
    try:
        return {"success": True, **request("GET", f"/agents/profiles/{profile_name}")}
    except ApiError as exc:
        return _fail("Get profile", exc)


@mcp.tool()
def launch_session(
    agent_profile: str,
    session_name: str | None = None,
    working_directory: str | None = None,
    model: str | None = None,
    initial_message: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Start a new session: one full Claude Code process in its own tmux window.

    Blocks until Claude shows its input box (up to ~90 s) so a failed start is
    reported here rather than discovered later. ``working_directory`` must be a
    Linux path (``/mnt/c/...`` for a Windows folder). ``model`` overrides the
    profile's model (opus / sonnet). ``initial_message`` is delivered as the
    first task once the session is ready. ``provider`` is accepted for
    compatibility; only claude_code exists.
    """
    try:
        term = request(
            "POST", "/sessions",
            body={
                "agent_profile": agent_profile, "session_name": session_name,
                "working_directory": working_directory, "model": model,
                "initial_message": initial_message, "sender_id": "maestro-ops",
                "orchestration_type": "launch", "wait": True,
            },
            timeout=config.INIT_TIMEOUT + 45,
        )
    except ApiError as exc:
        return {"success": False, "message": f"Launch session failed: {exc.detail}", "session_name": session_name, "terminal_id": None}
    return {
        "success": True,
        "message": f"Session '{term['session_name']}' launched" + ("; initial message delivered" if initial_message else ""),
        "session_name": term["session_name"],
        "terminal_id": term["id"],
        "provider": "claude_code",
    }


@mcp.tool()
def send_session_message(terminal_id: str, message: str) -> dict[str, Any]:
    """Queue a message for a terminal; it is pasted as soon as the terminal is idle."""
    try:
        res = request("POST", f"/terminals/{terminal_id}/inbox/messages",
                      body={"message": message, "sender_id": "maestro-ops", "orchestration_type": "send_message"})
    except ApiError as exc:
        return {"success": False, "message": f"Send message failed: {exc.detail}", "terminal_id": terminal_id}
    return {"success": True, "message": f"Message queued for terminal '{terminal_id}' ({res['queued']} pending)", "terminal_id": terminal_id}


@mcp.tool()
def get_terminal_status(terminal_id: str) -> dict[str, Any]:
    """Live status of one terminal: idle, processing, completed, waiting_user_answer, error."""
    try:
        return request("GET", f"/terminals/{terminal_id}")
    except ApiError as exc:
        return _fail("Get terminal status", exc)


@mcp.tool()
def get_terminal_output(terminal_id: str, mode: str = "last") -> dict[str, Any]:
    """Read a terminal's output. ``last`` = Claude's final answer; ``full`` = the rendered scrollback."""
    try:
        return {"success": True, **request("GET", f"/terminals/{terminal_id}/output", params={"mode": mode})}
    except ApiError as exc:
        return _fail("Get terminal output", exc)


@mcp.tool()
def read_session_output(
    terminal_id: str | None = None,
    session_name: str | None = None,
    mode: str = "full",
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Read output by terminal id, or by session name when the session has one terminal.

    ``max_chars`` keeps only the tail of the output.
    """
    try:
        if not terminal_id:
            if not session_name:
                return {"success": False, "message": "Provide terminal_id or session_name"}
            terms = request("GET", f"/sessions/{session_name}/terminals")
            if len(terms) != 1:
                return {"success": False, "message": f"Session '{session_name}' has {len(terms)} terminals; pass terminal_id", "terminals": terms}
            terminal_id = terms[0]["id"]
        data = request("GET", f"/terminals/{terminal_id}/output", params={"mode": mode})
    except ApiError as exc:
        return _fail("Read output", exc)
    output = data["output"]
    total = len(output)
    truncated = bool(max_chars and max_chars > 0 and total > max_chars)
    if truncated:
        output = output[-max_chars:]
    return {"success": True, "terminal_id": terminal_id, "mode": mode, "output": output, "truncated": truncated, "total_chars": total}


@mcp.tool()
def list_sessions() -> dict[str, Any]:
    """All running sessions with their terminal counts."""
    try:
        return {"success": True, "sessions": request("GET", "/sessions")}
    except ApiError as exc:
        return _fail("List sessions", exc)


@mcp.tool()
def get_session_info(session_name: str) -> dict[str, Any]:
    """A session and every terminal in it, with live statuses (oldest terminal first)."""
    try:
        return request("GET", f"/sessions/{session_name}")
    except ApiError as exc:
        return _fail("Get session info", exc)


@mcp.tool()
def shutdown_session(session_name: str) -> dict[str, Any]:
    """Close every terminal of a session and the session itself."""
    try:
        return request("DELETE", f"/sessions/{session_name}", timeout=60)
    except ApiError as exc:
        return _fail("Shutdown session", exc)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
