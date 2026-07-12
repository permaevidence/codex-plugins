#!/usr/bin/env python3
"""Install this repository into a stable, versioned per-user runtime directory."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path


APP_SUPPORT_ROOT = Path.home() / "Library" / "Application Support" / "PermaEvidenceCodex"
VERSIONS_DIR = APP_SUPPORT_ROOT / "versions"
CURRENT_LINK = APP_SUPPORT_ROOT / "current"
KEEP_VERSIONS = 3


def runtime_root() -> Path:
    """Return the stable path used by docs, LaunchAgents, and operator commands."""
    return CURRENT_LINK


def install_runtime(source_root: Path, *, cachebuster: str | None = None) -> Path:
    source_root = source_root.expanduser().resolve()
    marketplace = source_root / ".agents" / "plugins" / "marketplace.json"
    if not marketplace.is_file():
        raise RuntimeError(f"Not a plugin repository: missing {marketplace}")

    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    token = cachebuster or datetime.now(timezone.utc).strftime("local-%Y%m%d-%H%M%S")
    version_name = token
    destination = VERSIONS_DIR / version_name
    suffix = 1
    while destination.exists():
        destination = VERSIONS_DIR / f"{version_name}-{suffix}"
        suffix += 1

    staging_parent = Path(tempfile.mkdtemp(prefix=".install-", dir=str(VERSIONS_DIR)))
    staging = staging_parent / "runtime"
    try:
        shutil.copytree(
            source_root,
            staging,
            symlinks=True,
            ignore=shutil.ignore_patterns(
                ".git",
                ".DS_Store",
                "__pycache__",
                "*.pyc",
                ".pytest_cache",
                "tmp",
            ),
        )
        _apply_cachebusters(staging, token)
        _rewrite_mcp_paths(staging, installed_root=destination)
        _validate_runtime(staging)
        staging.replace(destination)
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)

    APP_SUPPORT_ROOT.mkdir(parents=True, exist_ok=True)
    next_link = APP_SUPPORT_ROOT / ".current.next"
    next_link.unlink(missing_ok=True)
    next_link.symlink_to(destination)
    os.replace(next_link, CURRENT_LINK)
    _prune_old_versions(active=destination)
    return CURRENT_LINK


def _apply_cachebusters(root: Path, token: str) -> None:
    for manifest in root.glob("plugins/*/.codex-plugin/plugin.json"):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        base = str(data.get("version") or "0.0.0").split("+", 1)[0]
        data["version"] = f"{base}+codex.{token}"
        manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _rewrite_mcp_paths(root: Path, *, installed_root: Path) -> None:
    """Make plugin MCP script paths absolute in the installed runtime.

    Codex spawns plugin MCP servers with the *session* cwd, not the plugin
    directory, so a relative "./scripts/server.py" in .mcp.json resolves
    against whatever directory the user happens to be in and the server dies
    before the initialize handshake. Rewrite relative entries to absolute
    paths inside the installed version directory (args are an array, so the
    space in "Application Support" is harmless here).
    """
    for manifest in root.glob("plugins/*/.mcp.json"):
        plugin_dir = installed_root / "plugins" / manifest.parent.name
        data = json.loads(manifest.read_text(encoding="utf-8"))
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            continue
        for server in servers.values():
            if not isinstance(server, dict):
                continue
            args = server.get("args")
            if isinstance(args, list):
                server["args"] = [
                    str(plugin_dir / arg[2:]) if isinstance(arg, str) and arg.startswith("./") else arg
                    for arg in args
                ]
            command = server.get("command")
            if isinstance(command, str) and command.startswith("./"):
                server["command"] = str(plugin_dir / command[2:])
        manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _validate_runtime(root: Path) -> None:
    marketplace = json.loads((root / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
    if marketplace.get("name") != "permaevidence-local":
        raise RuntimeError("Unexpected marketplace name in installed runtime")
    for name in ("codex-long-term-memory", "codex-telegram-bridge"):
        manifest = root / "plugins" / name / ".codex-plugin" / "plugin.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if data.get("name") != name:
            raise RuntimeError(f"Invalid installed plugin manifest: {manifest}")


def _prune_old_versions(*, active: Path) -> None:
    versions = sorted(
        (path for path in VERSIONS_DIR.iterdir() if path.is_dir() and path != active),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in versions[KEEP_VERSIONS - 1 :]:
        shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    installed = install_runtime(Path(__file__).resolve().parents[1])
    print(installed)
