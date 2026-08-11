#!/usr/bin/env python3
"""Maintain an OpenAI-signed Codex package at stable macOS paths.

The official standalone installer intentionally keeps releases in versioned
directories.  macOS Full Disk Access records for command-line executables are
path-sensitive, so the Telegram bridge uses a separately managed package whose
*actual* executable paths do not change between Codex releases.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from platform_support import platform_family, runtime_data_root


OPENAI_TEAM_IDENTIFIER = "2DC432GLL2"
EXPECTED_IDENTIFIERS = {
    "codex": "codex",
    "codex-code-mode-host": "codex-code-mode-host",
}
EXPECTED_REQUIREMENTS = {
    "codex": (
        "identifier codex and anchor apple generic and "
        "certificate 1[field.1.2.840.113635.100.6.2.6] /* exists */ and "
        "certificate leaf[field.1.2.840.113635.100.6.1.13] /* exists */ and "
        'certificate leaf[subject.OU] = "2DC432GLL2"'
    ),
    "codex-code-mode-host": (
        'identifier "codex-code-mode-host" and anchor apple generic and '
        "certificate 1[field.1.2.840.113635.100.6.2.6] /* exists */ and "
        "certificate leaf[field.1.2.840.113635.100.6.1.13] /* exists */ and "
        'certificate leaf[subject.OU] = "2DC432GLL2"'
    ),
}
STABLE_ROOT = runtime_data_root() / "codex-standalone"
PREVIOUS_ROOT = runtime_data_root() / "codex-standalone.previous"
RUNTIME_METADATA = ".permaevidence-runtime.json"
AT_FDCWD = -2
RENAME_SWAP = 0x00000002


class CodexRuntimeError(RuntimeError):
    """Raised when a stable Codex runtime cannot be prepared safely."""


def stable_codex_command(root: Path = STABLE_ROOT) -> Path:
    return root / "bin" / "codex"


def stable_codex_targets(root: Path = STABLE_ROOT) -> list[Path]:
    return [root / "bin" / "codex", root / "bin" / "codex-code-mode-host"]


def package_root_for_command(command: str | Path) -> Path:
    executable = Path(command).expanduser().resolve(strict=True)
    if executable.parent.name != "bin":
        raise CodexRuntimeError(f"Codex is not in a standalone package layout: {executable}")
    root = executable.parent.parent
    metadata = _package_metadata(root)
    if metadata.get("entrypoint") != "bin/codex":
        raise CodexRuntimeError(f"Unexpected Codex package metadata at {root}")
    for relative in ("bin/codex", "bin/codex-code-mode-host", "codex-path", "codex-resources"):
        if not (root / relative).exists():
            raise CodexRuntimeError(f"Incomplete Codex package: missing {root / relative}")
    return root


def locate_official_package(command: str | Path | None = None) -> Path:
    """Find the package managed by OpenAI's standalone installer."""

    candidates: list[str | Path] = []
    if command:
        candidates.append(command)
    candidates.append(Path.home() / ".codex/packages/standalone/current/bin/codex")
    discovered = shutil.which("codex")
    if discovered:
        candidates.append(discovered)

    errors: list[str] = []
    stable_paths = {STABLE_ROOT.resolve(), PREVIOUS_ROOT.resolve()}
    for candidate in candidates:
        try:
            root = package_root_for_command(candidate)
        except (OSError, CodexRuntimeError) as exc:
            errors.append(str(exc))
            continue
        if root.resolve() in stable_paths:
            continue
        return root
    detail = "; ".join(errors[-2:]) or "no standalone Codex executable was found"
    raise CodexRuntimeError(
        "OpenAI's standalone Codex package could not be located. "
        f"Run the official installer first ({detail})."
    )


def _package_metadata(root: Path) -> dict[str, Any]:
    try:
        payload = json.loads((root / "codex-package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexRuntimeError(f"Invalid Codex package metadata at {root}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CodexRuntimeError(f"Invalid Codex package metadata at {root}")
    return payload


def package_version(root: Path) -> str:
    return str(_package_metadata(root).get("version") or "").strip()


def _codesign_details(path: Path) -> dict[str, str]:
    try:
        completed = subprocess.run(
            ["codesign", "-dv", "--verbose=4", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodexRuntimeError(f"Could not inspect the signature on {path}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "codesign failed").strip()
        raise CodexRuntimeError(f"Could not inspect the signature on {path}: {detail}")
    output = "\n".join((completed.stdout or "", completed.stderr or ""))
    values: dict[str, str] = {}
    for line in output.splitlines():
        if line.startswith("Identifier="):
            values["identifier"] = line.split("=", 1)[1].strip()
        elif line.startswith("TeamIdentifier="):
            values["team_identifier"] = line.split("=", 1)[1].strip()
    return values


def _designated_requirement(path: Path) -> str:
    try:
        completed = subprocess.run(
            ["codesign", "-d", "-r-", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodexRuntimeError(f"Could not read the designated requirement for {path}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "codesign failed").strip()
        raise CodexRuntimeError(f"Could not read the designated requirement for {path}: {detail}")
    output = "\n".join((completed.stdout or "", completed.stderr or ""))
    for line in output.splitlines():
        if "designated =>" in line:
            return line.split("designated =>", 1)[1].strip()
    raise CodexRuntimeError(f"No designated requirement was reported for {path}")


def verify_signed_package(
    root: Path,
    *,
    expected_requirements: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Verify both native executables and return their signing requirements."""

    if platform_family() != "macos":
        raise CodexRuntimeError("The stable Full Disk Access runtime is only used on macOS.")
    metadata = _package_metadata(root)
    if str(metadata.get("target") or "") not in {"aarch64-apple-darwin", "x86_64-apple-darwin"}:
        raise CodexRuntimeError(f"Unexpected Codex package target: {metadata.get('target')}")
    machine = platform.machine().lower()
    if machine == "arm64" and metadata.get("target") != "aarch64-apple-darwin":
        raise CodexRuntimeError("The Codex package architecture does not match this Apple Silicon Mac.")
    if machine == "x86_64" and metadata.get("target") != "x86_64-apple-darwin":
        raise CodexRuntimeError("The Codex package architecture does not match this Intel Mac.")

    requirements: dict[str, str] = {}
    signatures: dict[str, dict[str, str]] = {}
    for filename, expected_identifier in EXPECTED_IDENTIFIERS.items():
        executable = root / "bin" / filename
        if not executable.is_file():
            raise CodexRuntimeError(f"Codex package is missing {executable}")
        try:
            verified = subprocess.run(
                [
                    "codesign",
                    "--verify",
                    "--strict",
                    "--verbose=2",
                    f"-R={EXPECTED_REQUIREMENTS[filename]}",
                    str(executable),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CodexRuntimeError(f"Could not verify {executable}: {exc}") from exc
        if verified.returncode != 0:
            detail = (verified.stderr or verified.stdout or "codesign verification failed").strip()
            raise CodexRuntimeError(f"Code-signature verification failed for {executable}: {detail}")
        details = _codesign_details(executable)
        if details.get("team_identifier") != OPENAI_TEAM_IDENTIFIER:
            raise CodexRuntimeError(f"{executable} is not signed by the expected OpenAI team.")
        if details.get("identifier") != expected_identifier:
            raise CodexRuntimeError(
                f"{executable} has identifier {details.get('identifier')!r}, expected {expected_identifier!r}."
            )
        requirement = _designated_requirement(executable)
        if requirement != EXPECTED_REQUIREMENTS[filename]:
            raise CodexRuntimeError(
                f"The designated requirement for {filename} does not match the expected OpenAI requirement."
            )
        if expected_requirements is not None and requirement != expected_requirements.get(filename):
            raise CodexRuntimeError(
                f"The designated requirement for {filename} changed. "
                "The existing Full Disk Access grant cannot be assumed to carry forward."
            )
        signatures[filename] = details
        requirements[filename] = requirement
    return {
        "version": str(metadata.get("version") or ""),
        "target": str(metadata.get("target") or ""),
        "requirements": requirements,
        "signatures": signatures,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _same_package(left: Path, right: Path) -> bool:
    if package_version(left) != package_version(right):
        return False
    return all(
        _sha256(left / "bin" / filename) == _sha256(right / "bin" / filename)
        for filename in EXPECTED_IDENTIFIERS
    )


def _atomic_exchange(left: Path, right: Path) -> None:
    """Atomically swap two existing directory entries on macOS."""

    if platform_family() != "macos":
        raise CodexRuntimeError("Atomic Codex package exchange is only implemented on macOS.")
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx_np = getattr(libc, "renameatx_np", None)
    if renameatx_np is None:
        raise CodexRuntimeError("This macOS version does not provide atomic directory exchange.")
    renameatx_np.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameatx_np.restype = ctypes.c_int
    result = renameatx_np(
        AT_FDCWD,
        os.fsencode(left),
        AT_FDCWD,
        os.fsencode(right),
        RENAME_SWAP,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise CodexRuntimeError(
            f"Atomic Codex runtime exchange failed: {os.strerror(error or errno.EIO)}"
        )


def _write_runtime_metadata(root: Path, verification: dict[str, Any], source: Path) -> None:
    payload = {
        "version": verification.get("version"),
        "target": verification.get("target"),
        "source": str(source),
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "requirements": verification.get("requirements"),
    }
    (root / RUNTIME_METADATA).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def sync_stable_codex_runtime(
    command: str | Path | None = None,
    *,
    stable_root: Path = STABLE_ROOT,
    previous_root: Path = PREVIOUS_ROOT,
) -> dict[str, Any]:
    """Copy the official package to stable paths, retaining one rollback copy."""

    source = locate_official_package(command)
    source_verification = verify_signed_package(source)
    expected_requirements: dict[str, str] | None = None
    current_verification: dict[str, Any] | None = None
    if stable_root.exists():
        current_verification = verify_signed_package(stable_root)
        expected_requirements = dict(current_verification["requirements"])
        verify_signed_package(source, expected_requirements=expected_requirements)
        if _same_package(source, stable_root):
            return {
                "changed": False,
                "version": source_verification["version"],
                "source": source,
                "root": stable_root,
                "codex": stable_codex_command(stable_root),
                "targets": stable_codex_targets(stable_root),
                "requirements": source_verification["requirements"],
            }

    stable_root.parent.mkdir(parents=True, exist_ok=True)
    staging_parent = Path(tempfile.mkdtemp(prefix=".codex-standalone-", dir=str(stable_root.parent)))
    staged = staging_parent / "package"
    try:
        shutil.copytree(source, staged, symlinks=True)
        staged_verification = verify_signed_package(staged, expected_requirements=expected_requirements)
        _write_runtime_metadata(staged, staged_verification, source)

        if stable_root.exists():
            if previous_root.exists():
                shutil.rmtree(previous_root)
            _atomic_exchange(stable_root, staged)
            staged.replace(previous_root)
        else:
            staged.replace(stable_root)

        try:
            verify_signed_package(stable_root, expected_requirements=expected_requirements)
        except Exception:
            if previous_root.exists() and stable_root.exists():
                _atomic_exchange(stable_root, previous_root)
            raise
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)

    return {
        "changed": True,
        "previous_version": (current_verification or {}).get("version"),
        "version": source_verification["version"],
        "source": source,
        "root": stable_root,
        "codex": stable_codex_command(stable_root),
        "targets": stable_codex_targets(stable_root),
        "requirements": source_verification["requirements"],
    }


def rollback_stable_codex_runtime(
    *,
    stable_root: Path = STABLE_ROOT,
    previous_root: Path = PREVIOUS_ROOT,
) -> dict[str, Any]:
    if not stable_root.exists() or not previous_root.exists():
        raise CodexRuntimeError("No previous stable Codex runtime is available for rollback.")
    current = verify_signed_package(stable_root)
    previous = verify_signed_package(previous_root)
    _atomic_exchange(stable_root, previous_root)
    restored = verify_signed_package(stable_root)
    return {
        "replaced_version": current["version"],
        "restored_version": restored["version"],
        "requirements": previous["requirements"],
    }


def stable_runtime_status(root: Path = STABLE_ROOT) -> dict[str, Any]:
    if platform_family() != "macos":
        return {"state": "not_applicable", "detail": "Stable Codex permission paths are macOS-specific."}
    if not root.exists():
        return {"state": "missing", "detail": f"Stable Codex runtime is missing at {root}."}
    try:
        verification = verify_signed_package(root)
    except CodexRuntimeError as exc:
        return {"state": "invalid", "detail": str(exc)}
    return {
        "state": "ready",
        "detail": f"OpenAI-signed Codex {verification['version']} at stable paths.",
        "version": verification["version"],
        "root": root,
        "codex": stable_codex_command(root),
        "targets": stable_codex_targets(root),
        "requirements": verification["requirements"],
    }
