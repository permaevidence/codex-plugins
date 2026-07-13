#!/usr/bin/env python3
"""Shared macOS/Linux paths and platform labels for the plugin runtime."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping


LAUNCHD_LABEL = "com.permaevidence.codex-telegram-bridge"
SYSTEMD_SERVICE_NAME = "permaevidence-codex-telegram-bridge.service"


def platform_family(value: str | None = None) -> str:
    current = value or sys.platform
    if current == "darwin":
        return "macos"
    if current.startswith("linux"):
        return "linux"
    return "unsupported"


def platform_display_name(value: str | None = None) -> str:
    family = platform_family(value)
    if family == "macos":
        return "Mac"
    if family == "linux":
        return "Linux computer"
    return "computer"


def runtime_data_root(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path:
    resolved_home = (home or Path.home()).expanduser()
    family = platform_family(platform)
    if family == "macos":
        return resolved_home / "Library" / "Application Support" / "PermaEvidenceCodex"
    if family == "linux":
        values = os.environ if environ is None else environ
        xdg_data = str(values.get("XDG_DATA_HOME") or "").strip()
        candidate = Path(xdg_data).expanduser() if xdg_data else None
        base = candidate if candidate and candidate.is_absolute() else resolved_home / ".local" / "share"
        return base / "permaevidence-codex"
    raise RuntimeError(f"Unsupported operating system: {sys.platform}")


def systemd_user_dir(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    resolved_home = (home or Path.home()).expanduser()
    values = os.environ if environ is None else environ
    xdg_config = str(values.get("XDG_CONFIG_HOME") or "").strip()
    candidate = Path(xdg_config).expanduser() if xdg_config else None
    base = candidate if candidate and candidate.is_absolute() else resolved_home / ".config"
    return base / "systemd" / "user"


def service_definition_path(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path:
    family = platform_family(platform)
    resolved_home = (home or Path.home()).expanduser()
    if family == "macos":
        return resolved_home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    if family == "linux":
        return systemd_user_dir(home=resolved_home, environ=environ) / SYSTEMD_SERVICE_NAME
    raise RuntimeError(f"Unsupported operating system: {sys.platform}")
