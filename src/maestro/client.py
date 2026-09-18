"""Minimal HTTP client for maestro-server, shared by both MCP servers."""

import json
import urllib.error
import urllib.parse
import urllib.request

from maestro import config


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


def request(method: str, path: str, params: dict | None = None, body: dict | None = None, timeout: float = 30):
    url = config.URL + path
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    data = None
    headers = {}
    if body is not None:
        data = json.dumps({k: v for k, v in body.items() if v is not None}).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            detail = json.loads(raw).get("detail", raw.decode(errors="replace"))
        except (json.JSONDecodeError, AttributeError):
            detail = raw.decode(errors="replace") or exc.reason
        raise ApiError(exc.code, str(detail)) from None
    except urllib.error.URLError as exc:
        raise ApiError(0, f"maestro-server is not reachable at {config.URL} ({exc.reason})") from None
