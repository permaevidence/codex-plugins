#!/usr/bin/env python3
"""Install the bridge's pinned private FFmpeg fallback when needed."""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from lib import audio_tools


PYTHON = sys.executable or shutil.which("python3") or "/usr/bin/python3"
REQUIREMENTS_FILE = PLUGIN_ROOT / "requirements.txt"


def ensure_voice_converter() -> str:
    """Return the usable converter, installing the private fallback if absent."""

    executable = audio_tools.resolve_ffmpeg_executable()
    if executable:
        return executable

    audio_tools.DEPENDENCY_DIR.mkdir(parents=True, exist_ok=True)
    for directory in (audio_tools.STATE_DIR, audio_tools.DEPENDENCY_DIR):
        try:
            directory.chmod(0o700)
        except OSError:
            pass
    command = [
        PYTHON,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--no-deps",
        "--only-binary=:all:",
        "--require-hashes",
        "--target",
        str(audio_tools.DEPENDENCY_DIR),
        "--upgrade",
        "--requirement",
        str(REQUIREMENTS_FILE),
    ]
    environment = dict(os.environ)
    environment.setdefault("PIP_ROOT_USER_ACTION", "ignore")
    try:
        subprocess.run(command, check=True, timeout=300, env=environment)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(
            "Could not install the pinned Telegram voice-note converter. "
            f"Check network access and Python pip, then rerun setup. Reason: {exc}"
        ) from exc

    importlib.invalidate_caches()
    audio_tools.resolve_ffmpeg_executable.cache_clear()
    executable = audio_tools.resolve_ffmpeg_executable()
    if not executable:
        raise SystemExit(
            "The private Telegram voice-note converter installed but did not run. "
            "Install ffmpeg through the operating system package manager, then rerun setup."
        )
    return executable


def main() -> None:
    executable = ensure_voice_converter()
    print(f"Voice-note converter ready: {executable}")


if __name__ == "__main__":
    main()
