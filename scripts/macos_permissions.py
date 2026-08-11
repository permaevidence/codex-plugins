#!/usr/bin/env python3
"""Best-effort discovery and Full Disk Access checks for macOS Codex binaries."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any


FULL_DISK_ACCESS_SERVICE = "kTCCServiceSystemPolicyAllFiles"
SYSTEM_TCC_DATABASE = Path("/Library/Application Support/com.apple.TCC/TCC.db")
OPENAI_TEAM_IDENTIFIER = "2DC432GLL2"


def macos_code_signature(path: Path) -> dict[str, str]:
    """Return the designated identifier and Apple Developer Team identifier."""

    try:
        completed = subprocess.run(
            ["codesign", "-dv", "--verbose=4", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if completed.returncode != 0:
        return {}
    output = "\n".join((completed.stdout or "", completed.stderr or ""))
    values: dict[str, str] = {}
    for line in output.splitlines():
        if line.startswith("Identifier="):
            values["identifier"] = line.split("=", 1)[1].strip()
        elif line.startswith("TeamIdentifier="):
            values["team_identifier"] = line.split("=", 1)[1].strip()
    return values


def codex_permission_installation(codex_cmd: str | None = None) -> dict[str, Any]:
    """Resolve the active Codex launcher and binaries that macOS authorizes.

    Native/standalone Codex keeps ``codex-code-mode-host`` beside ``codex``.
    npm Codex runs through a shared Node process, so granting it Full Disk
    Access would broaden access for unrelated Node programs and is deliberately
    not offered as an automatic target.
    """

    launcher_raw = str(codex_cmd or shutil.which("codex") or "").strip()
    if not launcher_raw:
        return {
            "kind": "missing",
            "launcher": None,
            "codex": None,
            "helper": None,
            "targets": [],
            "detail": "Codex was not found on PATH.",
        }

    launcher = Path(launcher_raw).expanduser()
    try:
        resolved = launcher.resolve(strict=True)
    except OSError:
        resolved = launcher.resolve()

    normalized = str(resolved).lower()
    if (
        "node_modules/@openai/codex" in normalized
        or resolved.suffix.lower() in {".js", ".mjs", ".cjs"}
    ):
        return {
            "kind": "npm",
            "launcher": launcher,
            "codex": resolved,
            "helper": None,
            "targets": [],
            "detail": (
                "npm Codex runs through the shared Node executable. Granting Node "
                "Full Disk Access would also authorize unrelated Node programs."
            ),
        }

    helper = resolved.parent / "codex-code-mode-host"
    if not helper.is_file():
        return {
            "kind": "other",
            "launcher": launcher,
            "codex": resolved,
            "helper": None,
            "targets": [],
            "signatures": {},
            "detail": (
            "The Codex executable was found, but codex-code-mode-host was not "
            f"present beside it at {helper}."
            ),
        }

    helper = helper.resolve()
    targets = [resolved, helper]
    signatures = {str(path): macos_code_signature(path) for path in targets}
    expected_identifiers = {
        str(resolved): "codex",
        str(helper): "codex-code-mode-host",
    }
    trusted = all(
        signatures[str(path)].get("team_identifier") == OPENAI_TEAM_IDENTIFIER
        and signatures[str(path)].get("identifier") == expected_identifiers[str(path)]
        for path in targets
    )
    if not trusted:
        return {
            "kind": "untrusted",
            "launcher": launcher,
            "codex": resolved,
            "helper": helper,
            "targets": [],
            "signatures": signatures,
            "detail": (
                "Codex and its helper were found, but their Apple code signatures "
                "could not be verified as OpenAI binaries. The wizard will not "
                "request Full Disk Access for unverified executables."
            ),
        }

    return {
        "kind": "native",
        "launcher": launcher,
        "codex": resolved,
        "helper": helper,
        "targets": targets,
        "signatures": signatures,
        "detail": "OpenAI-signed native Codex and its code-mode helper were found.",
    }


def codex_full_disk_access_status(
    targets: list[Path],
    *,
    database: Path = SYSTEM_TCC_DATABASE,
) -> dict[str, Any]:
    """Return whether the requested exact binary paths have Full Disk Access.

    TCC deliberately protects its database. A setup process without permission
    to read it receives ``unknown`` rather than a false failure. The user still
    confirms the setting in System Settings in that case.
    """

    normalized_targets = [Path(os.path.realpath(path)).expanduser() for path in targets]
    if not normalized_targets:
        return {
            "state": "not_applicable",
            "authorized": [],
            "missing": [],
            "detail": "No native Codex permission targets were found.",
        }

    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT client, auth_value FROM access WHERE service = ?",
                (FULL_DISK_ACCESS_SERVICE,),
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        return {
            "state": "unknown",
            "authorized": [],
            "missing": normalized_targets,
            "detail": (
                "macOS would not let this process inspect the protected privacy "
                f"database ({type(exc).__name__}). Confirm the entries visually."
            ),
        }

    decisions = {
        os.path.realpath(str(client)): int(auth_value)
        for client, auth_value in rows
        if client
    }
    authorized = [
        path
        for path in normalized_targets
        if decisions.get(os.path.realpath(str(path))) == 2
    ]
    missing = [path for path in normalized_targets if path not in authorized]
    if not missing:
        return {
            "state": "granted",
            "authorized": authorized,
            "missing": [],
            "detail": f"Full Disk Access is enabled for all {len(authorized)} Codex executables.",
        }
    return {
        "state": "missing",
        "authorized": authorized,
        "missing": missing,
        "detail": (
            f"{len(missing)} of {len(normalized_targets)} current Codex "
            "executables are not authorized."
        ),
    }
