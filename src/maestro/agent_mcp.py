"""maestro-agent: the MCP server every launched session gets.

It lets a session create workers in its own session (``assign`` without
waiting, ``handoff`` waiting for the answer), message other terminals, and
clean up. The session's own id arrives in ``MAESTRO_TERMINAL_ID``.
"""

import os
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from maestro import config
from maestro.client import ApiError, request

mcp = FastMCP("maestro-agent")


def _me() -> str | None:
    return os.environ.get("MAESTRO_TERMINAL_ID") or None


def _my_session() -> str:
    me = _me()
    if not me:
        raise ApiError(0, "MAESTRO_TERMINAL_ID is not set: this tool only works inside a maestro session")
    return request("GET", f"/terminals/{me}")["session_name"]


@mcp.tool()
def assign(
    agent_profile: str,
    message: str,
    working_directory: str | None = None,
    model: str | None = None,
    use_worktree: bool = False,
) -> dict[str, Any]:
    """Start a worker in this session and give it a task, without waiting.

    The worker is told who assigned it; it reports back with ``send_message``
    (which routes to you when no receiver is given). Returns the worker's
    terminal_id: poll it with get_terminal_status, read its result with
    get_terminal_output, and call delete_terminal when you are done with it.
    ``model`` overrides the profile's model (opus / sonnet). There is no limit
    on how many workers you may run at once.

    ``use_worktree`` runs the worker in its own git worktree (under
    MAESTRO_HOME) on a new branch ``mx/<terminal_id>`` made from the repo's
    HEAD, so parallel workers on one repository never trample each other; the
    result's ``worktree`` gives its path and branch. The worker is told to
    commit there. Merge it yourself with plain git afterwards
    (``git -C <repo> merge mx/<terminal_id>``). Deleting the terminal removes
    the checkout; the branch is kept when it has unmerged commits.
    """
    try:
        me = _me()
        session = _my_session()
        worker_message = (
            f"{message}\n\n[Assigned by terminal {me}. When done, send your results back "
            f"to terminal {me} with the send_message tool.]"
        )
        term = request(
            "POST", f"/sessions/{session}/terminals",
            body={
                "agent_profile": agent_profile, "working_directory": working_directory,
                "model": model, "caller_id": me, "initial_message": worker_message,
                "sender_id": me, "orchestration_type": "assign", "wait": False,
                "use_worktree": use_worktree,
            },
        )
    except ApiError as exc:
        return {"success": False, "terminal_id": None, "message": f"Assignment failed: {exc.detail}"}
    return {
        "success": True,
        "terminal_id": term["id"],
        "worktree": term.get("worktree"),
        "message": (
            f"Task assigned to {agent_profile} (terminal {term['id']}); it is starting and will "
            f"receive the task once ready. delete_terminal('{term['id']}') when finished."
        ),
    }


@mcp.tool()
def handoff(
    agent_profile: str,
    message: str,
    timeout: int = 900,
    working_directory: str | None = None,
    model: str | None = None,
    use_worktree: bool = False,
) -> dict[str, Any]:
    """Start a worker, give it a task, wait for its answer, and close it.

    Blocking: returns the worker's final answer as ``output``. Use ``assign``
    when you want several workers running at the same time.

    ``use_worktree`` runs the worker in its own git worktree (under
    MAESTRO_HOME) on a new branch ``mx/<terminal_id>`` made from the repo's
    HEAD, so parallel workers on one repository never trample each other; the
    result's ``worktree`` gives its path and branch. The worker is told to
    commit there. Merge it yourself with plain git afterwards
    (``git -C <repo> merge mx/<terminal_id>``). Deleting the terminal removes
    the checkout; the branch is kept when it has unmerged commits.
    Here the terminal is closed for you, so the result's ``worktree`` also
    says whether the branch was kept (``branch_kept``).
    """
    terminal_id = None
    try:
        me = _me()
        session = _my_session()
        term = request(
            "POST", f"/sessions/{session}/terminals",
            body={
                "agent_profile": agent_profile, "working_directory": working_directory,
                "model": model, "caller_id": me,
                "initial_message": f"[Maestro Handoff from terminal {me}] {message}\n\n"
                                   "Complete the task, present the result, and stop; the result "
                                   "is collected automatically, do not call send_message.",
                "sender_id": me, "orchestration_type": "handoff", "wait": True,
                "use_worktree": use_worktree,
            },
            timeout=config.INIT_TIMEOUT + 45,
        )
        terminal_id = term["id"]
        deadline = time.time() + timeout
        status = "processing"
        while time.time() < deadline:
            time.sleep(3)
            status = request("GET", f"/terminals/{terminal_id}")["status"]
            if status in ("completed", "error", "waiting_user_answer"):
                break
        if status != "completed":
            return {
                "success": False, "terminal_id": terminal_id, "output": None, "worktree": term.get("worktree"),
                "message": f"Handoff did not complete (status {status}); the terminal is left running for inspection",
            }
        output = request("GET", f"/terminals/{terminal_id}/output", params={"mode": "last"})["output"]
        closed = request("DELETE", f"/terminals/{terminal_id}")
        return {
            "success": True, "terminal_id": terminal_id, "output": output,
            "worktree": closed.get("worktree"), "message": "Handoff completed",
        }
    except ApiError as exc:
        return {"success": False, "terminal_id": terminal_id, "output": None, "message": f"Handoff failed: {exc.detail}"}


@mcp.tool()
def send_message(message: str, receiver_id: str | None = None) -> dict[str, Any]:
    """Send a message to another terminal's inbox (delivered when it is idle).

    Omit ``receiver_id`` to reply to the terminal that assigned you.
    """
    me = _me()
    try:
        if not receiver_id:
            if not me:
                return {"success": False, "error": "receiver_id not given and MAESTRO_TERMINAL_ID is unset"}
            receiver_id = request("GET", f"/terminals/{me}").get("caller_id")
            if not receiver_id:
                return {"success": False, "error": "receiver_id not given and this terminal has no recorded caller"}
        if me and receiver_id == me:
            return {"success": False, "error": f"{receiver_id} is your own terminal; omit receiver_id to reply to your caller"}
        text = message + (f"\n\n[Message from terminal {me}. Reply with send_message if needed.]" if me else "")
        res = request("POST", f"/terminals/{receiver_id}/inbox/messages",
                      body={"message": text, "sender_id": me, "orchestration_type": "send_message"})
    except ApiError as exc:
        return {"success": False, "error": f"Delivery to {receiver_id} failed: {exc.detail}"}
    return {"success": True, "receiver_id": receiver_id, "queued": res["queued"]}


@mcp.tool()
def get_terminal_status(terminal_id: str) -> dict[str, Any]:
    """Live status of a terminal: idle, processing, completed, waiting_user_answer, error."""
    try:
        return request("GET", f"/terminals/{terminal_id}")
    except ApiError as exc:
        return {"success": False, "error": exc.detail}


@mcp.tool()
def get_terminal_output(terminal_id: str, mode: str = "last") -> dict[str, Any]:
    """Read a terminal's output: ``last`` = its final answer, ``full`` = rendered scrollback."""
    try:
        return {"success": True, **request("GET", f"/terminals/{terminal_id}/output", params={"mode": mode})}
    except ApiError as exc:
        return {"success": False, "error": exc.detail}


@mcp.tool()
def list_terminals() -> dict[str, Any]:
    """Every terminal in your session with its status, profile and caller."""
    try:
        return {"success": True, "terminals": request("GET", f"/sessions/{_my_session()}/terminals")}
    except ApiError as exc:
        return {"success": False, "error": exc.detail}


@mcp.tool()
def delete_terminal(terminal_id: str) -> dict[str, Any]:
    """Close a worker you no longer need."""
    try:
        return request("DELETE", f"/terminals/{terminal_id}")
    except ApiError as exc:
        return {"success": False, "error": exc.detail}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
