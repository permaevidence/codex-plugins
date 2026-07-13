#!/usr/bin/env python3
"""Install this repository into a stable, versioned per-user runtime directory."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from platform_support import platform_family, runtime_data_root, systemd_user_dir

APP_SUPPORT_ROOT = runtime_data_root()
VERSIONS_DIR = APP_SUPPORT_ROOT / "versions"
CURRENT_LINK = APP_SUPPORT_ROOT / "current"
KEEP_VERSIONS = 3


def runtime_root() -> Path:
    """Return the stable path used by docs, user services, and operator commands."""
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

    # Commit-based installs are immutable. Reusing an already validated
    # destination makes updates idempotent and prevents repeated invocations
    # from creating commit-...-1, commit-...-2, ... runtime churn.
    if destination.exists():
        if token.startswith("commit-"):
            _validate_runtime(destination)
            _activate(destination)
            return CURRENT_LINK
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

    _activate(destination)
    return CURRENT_LINK


def _activate(destination: Path) -> None:
    APP_SUPPORT_ROOT.mkdir(parents=True, exist_ok=True)
    next_link = APP_SUPPORT_ROOT / ".current.next"
    next_link.unlink(missing_ok=True)
    next_link.symlink_to(destination)
    os.replace(next_link, CURRENT_LINK)


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
        plugin_name = manifest.parent.name
        staged_plugin_dir = manifest.parent
        plugin_dir = installed_root / "plugins" / plugin_name
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
                    _rebase_plugin_mcp_path(
                        arg,
                        plugin_name=plugin_name,
                        staged_plugin_dir=staged_plugin_dir,
                        installed_plugin_dir=plugin_dir,
                    )
                    for arg in args
                ]
            command = server.get("command")
            if isinstance(command, str):
                server["command"] = _rebase_plugin_mcp_path(
                    command,
                    plugin_name=plugin_name,
                    staged_plugin_dir=staged_plugin_dir,
                    installed_plugin_dir=plugin_dir,
                )
        manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _rebase_plugin_mcp_path(
    value: object,
    *,
    plugin_name: str,
    staged_plugin_dir: Path,
    installed_plugin_dir: Path,
) -> object:
    """Map plugin-local MCP paths into the runtime being installed.

    A source checkout stores these paths as ``./scripts/...``. An installed
    runtime stores absolute paths because Codex starts MCP servers from the
    session cwd. When the setup wizard is rerun from that installed runtime,
    copying the already-absolute manifest must not leave the new runtime
    pointing at the previous version.

    Only absolute paths with a ``plugins/<plugin-name>/...`` suffix whose
    target exists in the staged plugin are rebased. Interpreter paths and
    other external absolute arguments remain untouched.
    """
    if not isinstance(value, str):
        return value

    relative: Path | None = None
    if value.startswith("./"):
        relative = Path(value[2:])
    else:
        path = Path(value)
        if not path.is_absolute():
            return value
        parts = path.parts
        for index in range(len(parts) - 1):
            if parts[index] != "plugins" or parts[index + 1] != plugin_name:
                continue
            candidate = Path(*parts[index + 2 :])
            if candidate.parts and (staged_plugin_dir / candidate).exists():
                relative = candidate
                break

    if relative is None:
        return value
    return str(installed_plugin_dir / relative)


def _validate_runtime(root: Path) -> None:
    marketplace = json.loads((root / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
    if marketplace.get("name") != "permaevidence-local":
        raise RuntimeError("Unexpected marketplace name in installed runtime")
    for name in ("codex-long-term-memory", "codex-telegram-bridge"):
        manifest = root / "plugins" / name / ".codex-plugin" / "plugin.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if data.get("name") != name:
            raise RuntimeError(f"Invalid installed plugin manifest: {manifest}")


def prune_old_versions(*, active: Path) -> None:
    """Prune only after the replacement runtime is configured and healthy.

    The previous runtime may still be executing a Telegram turn or a Codex
    hook while a new version is staged. Deleting it during installation makes
    those in-flight absolute script paths disappear before handoff completes.
    """
    active = active.resolve()
    protected = referenced_versions() | {active}
    versions = sorted(
        (path for path in VERSIONS_DIR.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    retained = set(protected)
    for path in versions:
        if len(retained) >= KEEP_VERSIONS:
            break
        retained.add(path.resolve())
    for path in versions:
        if path.resolve() in retained:
            continue
        shutil.rmtree(path, ignore_errors=True)


def referenced_versions() -> set[Path]:
    """Return installed versions still referenced by config or live processes.

    Absolute runtime paths are embedded in Codex hooks, cached MCP manifests,
    the bridge user service, and process command lines. A version remains
    protected for as long as any of those consumers still points at it.
    """
    if not VERSIONS_DIR.exists():
        return set()
    versions = [path.resolve() for path in VERSIONS_DIR.iterdir() if path.is_dir()]
    haystacks: list[str] = []

    codex_dir = Path.home() / ".codex"
    candidates = [codex_dir / "hooks.json", codex_dir / "config.toml"]
    plugin_dir = codex_dir / "plugins"
    if plugin_dir.exists():
        candidates.extend(
            path
            for path in plugin_dir.rglob("*")
            if path.is_file() and path.suffix in {".json", ".toml"}
        )
    if platform_family() == "macos":
        service_dir = Path.home() / "Library" / "LaunchAgents"
        pattern = "*.plist"
    elif platform_family() == "linux":
        service_dir = systemd_user_dir()
        pattern = "*.service"
    else:
        service_dir = None
        pattern = ""
    if service_dir and service_dir.exists():
        candidates.extend(path for path in service_dir.glob(pattern) if path.is_file())
    for path in candidates:
        try:
            if path.stat().st_size <= 2_000_000:
                haystacks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue

    try:
        processes = subprocess.run(
            ["ps", "-axo", "command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if processes.returncode == 0:
            haystacks.append(processes.stdout)
    except (OSError, subprocess.TimeoutExpired):
        # Failure to inspect processes must make pruning conservative.
        return set(versions)

    references = "\n".join(haystacks)
    return {path for path in versions if str(path) in references}


if __name__ == "__main__":
    installed = install_runtime(Path(__file__).resolve().parents[1])
    print(installed)
