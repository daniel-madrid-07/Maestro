"""maestro-ops: the MCP server an outside orchestrator (Claude in VS Code) uses.

Tool names and result shapes follow awslabs' ``cao-ops-mcp``, so an orchestrator
written against that one works here unchanged. ``maestro init`` registers this
server with Claude Code under the name ``maestro``, which is what namespaces the
tools the orchestrator sees.
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
    use_worktree: bool = False,
) -> dict[str, Any]:
    """Start a new session: one full Claude Code process in its own tmux window.

    Blocks until Claude shows its input box (up to ~90 s) so a failed start is
    reported here rather than discovered later. ``working_directory`` must be a
    Linux path (``/mnt/c/...`` for a Windows folder). ``model`` overrides the
    profile's model (opus / sonnet). ``initial_message`` is delivered as the
    first task once the session is ready. ``provider`` is accepted for
    compatibility; only claude_code exists.

    ``use_worktree`` runs the session in its own git worktree (under
    MAESTRO_HOME) on a new branch ``mx/<terminal_id>`` made from the repo's
    HEAD, so parallel workers on one repository never trample each other; the
    result's ``worktree`` gives its path and branch. The worker is told to
    commit there. Merge it yourself with plain git afterwards
    (``git -C <repo> merge mx/<terminal_id>``). Deleting the terminal removes
    the checkout after committing anything left uncommitted on the branch; the
    branch is kept when it has commits the repo's HEAD lacks.
    """
    try:
        term = request(
            "POST", "/sessions",
            body={
                "agent_profile": agent_profile, "session_name": session_name,
                "working_directory": working_directory, "model": model,
                "initial_message": initial_message, "sender_id": "maestro-ops",
                "orchestration_type": "launch", "wait": True, "use_worktree": use_worktree,
            },
            timeout=config.INIT_TIMEOUT * 4 + 45,
        )
    except ApiError as exc:
        return {"success": False, "message": f"Launch session failed: {exc.detail}", "session_name": session_name, "terminal_id": None}
    return {
        "success": True,
        "message": f"Session '{term['session_name']}' launched" + ("; initial message delivered" if initial_message else ""),
        "session_name": term["session_name"],
        "terminal_id": term["id"],
        "provider": "claude_code",
        "worktree": term.get("worktree"),
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
def list_worktrees() -> dict[str, Any]:
    """Every live terminal that runs in its own git worktree: what there is to merge.

    Each entry has terminal_id, status, repo, path and branch. Merge a finished
    one with ``git -C <repo> merge <branch>``; deleting the terminal commits any
    leftovers on the branch, removes the checkout and keeps the branch when it
    has commits the repo's HEAD lacks.
    ``orphaned`` lists checkouts whose terminal died with an earlier server;
    they are never removed on their own — merge what you want, then
    ``remove_worktree``.
    """
    try:
        data = request("GET", "/worktrees")
        return {"success": True, "worktrees": data["live"], "orphaned": data["orphaned"]}
    except ApiError as exc:
        return _fail("List worktrees", exc)


@mcp.tool()
def remove_worktree(terminal_id: str) -> dict[str, Any]:
    """Remove an orphaned checkout listed by list_worktrees (leftovers are committed first; the branch stays if unmerged)."""
    try:
        return request("DELETE", f"/worktrees/{terminal_id}", timeout=60)
    except ApiError as exc:
        return _fail("Remove worktree", exc)


@mcp.tool()
def shutdown_session(session_name: str) -> dict[str, Any]:
    """Close every terminal of a session and the session itself."""
    try:
        return request("DELETE", f"/sessions/{session_name}", timeout=60)
    except ApiError as exc:
        return _fail("Shutdown session", exc)


@mcp.tool()
def answer_prompt(terminal_id: str, answer: str) -> dict[str, Any]:
    """Answer the question a terminal is blocked on. Only valid while its status is waiting_user_answer.

    ``answer`` is a key (Enter, Escape, Up, Down, Tab, Space), a single digit
    (in an option dialog it selects and submits that option: ``"2"`` = the
    second one), or one line of text, typed and followed by Enter — only where
    a text field has focus (an option list treats letters as hotkeys). Escape
    cancels the question and ends the turn with no answer.
    """
    try:
        return request("POST", f"/terminals/{terminal_id}/answer", body={"answer": answer})
    except ApiError as exc:
        return _fail("Answer prompt", exc)


@mcp.tool()
def interrupt(terminal_id: str) -> dict[str, Any]:
    """Stop a terminal's current turn (Escape), e.g. when a worker is going in circles.

    Only while its status is processing or waiting_user_answer (an idle prompt
    has nothing to interrupt and is refused). Returns the status observed ~2 s
    later (normally idle or completed); queued messages are then delivered as
    usual.
    """
    try:
        return request("POST", f"/terminals/{terminal_id}/interrupt")
    except ApiError as exc:
        return _fail("Interrupt", exc)


@mcp.tool()
def restart_terminal(terminal_id: str) -> dict[str, Any]:
    """Replace a terminal's Claude with a fresh one: same id, profile, model, directory and inbox.

    Use when its status is ``error`` (Claude exited) or it stays unresponsive
    after ``interrupt``. The conversation is lost. Blocks until the new Claude
    is ready (up to ~90 s).
    """
    try:
        return request("POST", f"/terminals/{terminal_id}/restart", timeout=config.INIT_TIMEOUT + 45)
    except ApiError as exc:
        return _fail("Restart terminal", exc)


@mcp.tool()
def fleet_status() -> dict[str, Any]:
    """The whole fleet in one call: every session, every terminal, and who needs you.

    Prefer this over list_sessions plus a get_session_info per session: it is
    one call and one block of context instead of one per session. Each terminal
    carries status, progress, stuck, model, profile, working directory,
    worktree and pending_messages. ``needs_attention`` is the part to act on --
    terminals that are waiting on an answer, stuck, or failed. ``totals`` counts
    them by status.
    """
    try:
        return {"success": True, **request("GET", "/fleet")}
    except ApiError as exc:
        return _fail("Fleet status", exc)


@mcp.tool()
def wait_for(
    terminal_ids: list[str] | None = None,
    states: list[str] | None = None,
    timeout_seconds: float = 120,
    require_all: bool = False,
) -> dict[str, Any]:
    """Block until a terminal finishes, asks something or dies -- instead of polling.

    This holds your conversation until it returns. From Claude Code, prefer a
    background watch on ``GET /events`` (see the maestro skill) so the user can
    keep talking to you; use this for scripts, or with a short timeout.

    Returns as soon as one watched terminal reaches one of ``states``
    (default: completed, waiting_user_answer, error -- ``stuck`` is also
    available), or when ``timeout_seconds`` runs out (max 900). With
    ``require_all`` it waits for every watched terminal instead of the first.
    ``terminal_ids`` defaults to the whole fleet. A terminal with a message
    still queued counts as busy, not finished.

    This is the loop to use while a fleet runs: wait, act on what came back
    (read its output, answer its question, restart it), wait again. Polling
    get_terminal_status in a loop costs a call and a turn every few seconds
    and tells you nothing in between.
    """
    try:
        res = request(
            "POST", "/wait",
            body={"terminal_ids": terminal_ids, "states": states,
                  "timeout": timeout_seconds, "require_all": require_all},
            timeout=min(float(timeout_seconds), 900) + 30,
        )
    except ApiError as exc:
        return _fail("Wait", exc)
    return {"success": True, **res}


@mcp.tool()
def launch_sessions(sessions: list[dict[str, Any]], max_parallel: int = 4) -> dict[str, Any]:
    """Launch several sessions at once, in parallel. One call for a whole fleet.

    Each entry takes the same fields as launch_session: ``agent_profile``,
    ``session_name``, ``working_directory``, ``model``, ``initial_message``,
    ``use_worktree``. Every launch still blocks until its Claude is ready, so a
    failure is reported against its own entry and the rest still start;
    ``max_parallel`` (1-8) caps how many start at the same time.

    Ten sessions launched one by one cost ten calls and several minutes of
    waiting; this is one call and about as long as the slowest one.
    """
    if not sessions:
        return {"success": False, "message": "sessions must not be empty"}
    try:
        res = request("POST", "/sessions/batch",
                      body={"sessions": sessions, "max_parallel": max_parallel},
                      timeout=config.INIT_TIMEOUT * 6 + 120)
    except ApiError as exc:
        return _fail("Launch sessions", exc)
    results = res["results"]
    started = [r for r in results if r.get("success")]
    return {
        "success": bool(started),
        "message": f"{len(started)} of {len(results)} sessions launched",
        "results": results,
    }


@mcp.tool()
def broadcast_message(
    message: str,
    terminal_ids: list[str] | None = None,
    session_names: list[str] | None = None,
) -> dict[str, Any]:
    """Queue one message for many terminals at once (the whole fleet when none is named).

    For the things you say to everyone: a change of plan, "commit what you have
    and report", a constraint you forgot. Each terminal receives it when it is
    next idle, exactly as send_session_message does.
    """
    try:
        res = request("POST", "/broadcast",
                      body={"message": message, "terminal_ids": terminal_ids,
                            "session_names": session_names, "sender_id": "maestro-ops"})
    except ApiError as exc:
        return _fail("Broadcast", exc)
    return {"success": True, "message": f"Queued for {res['delivered']} terminal(s)", **res}


@mcp.tool()
def get_usage() -> dict[str, Any]:
    """How much of the Claude subscription is left: the 5-hour and weekly windows.

    Worth a look before opening a large fleet, and when deciding whether to run
    the stronger model everywhere. Percentages are utilisation, so 100 means
    spent; ``resets_at`` says when the window rolls over. Unavailable (no local
    credentials, or the endpoint refused) comes back as ok: false rather than a
    made-up number.
    """
    try:
        return {"success": True, **request("GET", "/usage")}
    except ApiError as exc:
        return _fail("Get usage", exc)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
