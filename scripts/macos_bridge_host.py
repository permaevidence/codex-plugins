#!/usr/bin/env python3
"""Create and verify the immutable macOS app that owns bridge TCC access."""

from __future__ import annotations

import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from platform_support import platform_family, runtime_data_root


BRIDGE_HOST_BUNDLE_ID = "com.permaevidence.codex-bridge-host"
BRIDGE_HOST_NAME = "PermaEvidence Codex Bridge"
BRIDGE_HOST_BUNDLE = runtime_data_root() / f"{BRIDGE_HOST_NAME}.app"
BRIDGE_HOST_EXECUTABLE = BRIDGE_HOST_BUNDLE / "Contents" / "MacOS" / "PermaEvidenceCodexBridge"
BRIDGE_HOST_SOURCE = Path(__file__).with_name("macos_bridge_host.c")


class BridgeHostError(RuntimeError):
    """Raised when the permanent bridge host cannot be prepared safely."""


def bridge_host_status(bundle: Path = BRIDGE_HOST_BUNDLE) -> dict[str, Any]:
    executable = bundle / "Contents" / "MacOS" / "PermaEvidenceCodexBridge"
    info_path = bundle / "Contents" / "Info.plist"
    if not executable.is_file() or not info_path.is_file():
        return {
            "state": "missing",
            "detail": f"Permanent bridge host is missing at {bundle}.",
            "bundle": bundle,
            "executable": executable,
        }
    try:
        info = plistlib.loads(info_path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        return {
            "state": "invalid",
            "detail": f"Bridge host Info.plist is invalid: {exc}",
            "bundle": bundle,
            "executable": executable,
        }
    if info.get("CFBundleIdentifier") != BRIDGE_HOST_BUNDLE_ID:
        return {
            "state": "invalid",
            "detail": "Bridge host bundle identifier does not match the permanent identity.",
            "bundle": bundle,
            "executable": executable,
        }
    try:
        verified = subprocess.run(
            ["codesign", "--verify", "--strict", "--deep", str(bundle)],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        version = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "state": "invalid",
            "detail": f"Bridge host verification could not run: {exc}",
            "bundle": bundle,
            "executable": executable,
        }
    if verified.returncode != 0 or version.returncode != 0:
        detail = (verified.stderr or version.stderr or "signature or executable check failed").strip()
        return {
            "state": "invalid",
            "detail": f"Permanent bridge host failed verification: {detail}",
            "bundle": bundle,
            "executable": executable,
        }
    return {
        "state": "ready",
        "detail": f"Permanent macOS bridge identity is ready at {bundle}.",
        "bundle": bundle,
        "executable": executable,
        "bundle_id": BRIDGE_HOST_BUNDLE_ID,
        "version": (version.stdout or "").strip(),
    }


def ensure_macos_bridge_host(
    *,
    bundle: Path = BRIDGE_HOST_BUNDLE,
    source: Path = BRIDGE_HOST_SOURCE,
) -> dict[str, Any]:
    """Create the host once; never replace a valid host and invalidate TCC."""

    if platform_family() != "macos":
        raise BridgeHostError("The permanent bridge host is only used on macOS.")
    current = bridge_host_status(bundle)
    if current.get("state") == "ready":
        return {**current, "changed": False}
    if bundle.exists():
        raise BridgeHostError(
            f"An invalid bridge host already exists at {bundle}. Refusing to replace its privacy identity: "
            f"{current.get('detail') or 'verification failed'}"
        )
    if not source.is_file():
        raise BridgeHostError(f"Bridge host source is missing: {source}")
    compiler = shutil.which("clang")
    signer = shutil.which("codesign")
    if not compiler or not signer:
        raise BridgeHostError("Xcode Command Line Tools (clang and codesign) are required on macOS.")

    bundle.parent.mkdir(parents=True, exist_ok=True)
    staging_parent = Path(tempfile.mkdtemp(prefix=".codex-bridge-host-", dir=str(bundle.parent)))
    staged_bundle = staging_parent / bundle.name
    executable = staged_bundle / "Contents" / "MacOS" / "PermaEvidenceCodexBridge"
    try:
        executable.parent.mkdir(parents=True)
        info = {
            "CFBundleDevelopmentRegion": "en",
            "CFBundleDisplayName": BRIDGE_HOST_NAME,
            "CFBundleExecutable": executable.name,
            "CFBundleIdentifier": BRIDGE_HOST_BUNDLE_ID,
            "CFBundleInfoDictionaryVersion": "6.0",
            "CFBundleName": BRIDGE_HOST_NAME,
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "1.0",
            "CFBundleVersion": "1",
            "LSBackgroundOnly": True,
            "LSMinimumSystemVersion": "11.0",
        }
        (staged_bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info, sort_keys=True))
        compiled = subprocess.run(
            [compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(executable)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if compiled.returncode != 0:
            raise BridgeHostError((compiled.stderr or compiled.stdout or "clang failed").strip())
        signed = subprocess.run(
            [signer, "--force", "--sign", "-", "--identifier", BRIDGE_HOST_BUNDLE_ID, str(staged_bundle)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if signed.returncode != 0:
            raise BridgeHostError((signed.stderr or signed.stdout or "codesign failed").strip())
        staged_status = bridge_host_status(staged_bundle)
        if staged_status.get("state") != "ready":
            raise BridgeHostError(str(staged_status.get("detail") or "Bridge host verification failed."))
        staged_bundle.replace(bundle)
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)

    installed = bridge_host_status(bundle)
    if installed.get("state") != "ready":
        raise BridgeHostError(str(installed.get("detail") or "Installed bridge host verification failed."))
    return {**installed, "changed": True}
