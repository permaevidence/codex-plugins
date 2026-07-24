#!/usr/bin/env python3
"""Resolve a working FFmpeg executable for Telegram voice-note conversion."""

from __future__ import annotations

import functools
import importlib
import shutil
import subprocess
import sys
from pathlib import Path


STATE_DIR = Path.home() / ".codex" / "telegram-bridge"
DEPENDENCY_DIR = STATE_DIR / "python"


def _ffmpeg_works(executable: str) -> bool:
    try:
        completed = subprocess.run(
            [executable, "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


@functools.lru_cache(maxsize=1)
def resolve_ffmpeg_executable() -> str | None:
    """Prefer a working system FFmpeg, then the setup-managed private copy."""

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg and _ffmpeg_works(system_ffmpeg):
        return system_ffmpeg

    if not DEPENDENCY_DIR.is_dir():
        return None
    if str(DEPENDENCY_DIR) not in sys.path:
        sys.path.insert(0, str(DEPENDENCY_DIR))
    try:
        imageio_ffmpeg = importlib.import_module("imageio_ffmpeg")
        bundled_ffmpeg = str(imageio_ffmpeg.get_ffmpeg_exe())
    except (AttributeError, ImportError, OSError, RuntimeError):
        return None
    return bundled_ffmpeg if _ffmpeg_works(bundled_ffmpeg) else None


def ffmpeg_status() -> tuple[bool, str]:
    executable = resolve_ffmpeg_executable()
    if not executable:
        return (
            False,
            "missing; rerun setup to install the private Telegram voice-note converter",
        )
    try:
        private = Path(executable).resolve().is_relative_to(DEPENDENCY_DIR.resolve())
    except (OSError, ValueError):
        private = False
    if private:
        return True, f"private setup-managed FFmpeg: {executable}"
    return True, f"system FFmpeg: {executable}"
