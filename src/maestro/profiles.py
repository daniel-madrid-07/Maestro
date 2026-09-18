"""Agent profiles: a Markdown file with YAML front matter, same shape as CAO's.

Front matter keys maestro reads: ``name``, ``description``, ``role``, ``model``,
``claudeConfig.effort``, ``permissionMode``, ``mcpServers``, ``tags``,
``capabilities``. The body is the system prompt appended to Claude's own.

Lookup order: ``~/.maestro/profiles/<name>.md`` (the owner's copies) first, then
the profiles packaged with maestro.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from maestro import config


@dataclass
class Profile:
    name: str
    description: str = ""
    role: str = ""
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    system_prompt: str = ""
    tags: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    source: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "description": self.description,
            "role": self.role,
            "model": self.model,
            "effort": self.effort,
            "tags": self.tags,
            "capabilities": self.capabilities,
            "loadable": True,
        }


def _split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta = yaml.safe_load(text[3:end]) or {}
    body = text[end + 4 :]
    return (meta if isinstance(meta, dict) else {}), body.strip()


def parse(text: str, name: str, source: str) -> Profile:
    meta, body = _split_front_matter(text)
    claude_cfg = meta.get("claudeConfig") or {}
    return Profile(
        name=str(meta.get("name") or name),
        description=str(meta.get("description") or ""),
        role=str(meta.get("role") or ""),
        model=meta.get("model"),
        effort=claude_cfg.get("effort") if isinstance(claude_cfg, dict) else None,
        permission_mode=meta.get("permissionMode"),
        mcp_servers=meta.get("mcpServers") or {},
        system_prompt=body,
        tags=[str(t) for t in (meta.get("tags") or [])],
        capabilities=[str(c) for c in (meta.get("capabilities") or [])],
        source=source,
    )


def _stores() -> list[tuple[Path, str]]:
    return [(config.USER_PROFILES, "user"), (config.PACKAGED_PROFILES, "packaged")]


def _validate_name(name: str) -> None:
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"invalid profile name: {name!r}")


def load(name: str) -> Profile:
    _validate_name(name)
    for directory, source in _stores():
        path = directory / f"{name}.md"
        if path.is_file():
            return parse(path.read_text(encoding="utf-8"), name, source)
    raise FileNotFoundError(f"profile not found: {name}")


def list_all() -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for directory, source in _stores():
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            if path.stem in seen:
                seen[path.stem]["duplicated_in"].append(source)
                continue
            try:
                entry = parse(path.read_text(encoding="utf-8"), path.stem, source).summary()
            except Exception as exc:  # a broken file is listed, not hidden
                entry = {"name": path.stem, "source": source, "loadable": False, "error": str(exc)}
            entry["duplicated_in"] = []
            seen[path.stem] = entry
    return sorted(seen.values(), key=lambda p: p["name"])
