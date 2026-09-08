"""Structured updater validation; ordinary doctor remains strict."""

from __future__ import annotations

import json
import fcntl
import stat
from contextlib import contextmanager
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class DoctorFailure(RuntimeError):
    def __init__(self, message: str, report: dict | None = None):
        self.report = report or {}
        super().__init__(message)


@contextmanager
def maintenance_lock(state_dir: Path):
    """Serialize plugin updates, CLI updates and historical revalidation."""
    state_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(state_dir / ".update.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError("Invalid updater lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another update or revalidation is already running.") from exc
        yield
    finally:
        os.close(fd)


def write_report(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def validate_runtime(command: list[str], *, allow_degraded: bool = True) -> dict:
    with tempfile.TemporaryDirectory(prefix="codex-doctor-") as tmp:
        report_path = Path(tmp) / "report.json"
        completed = subprocess.run([*command, "--report", str(report_path)], check=False)
        try:
            report = json.loads(report_path.read_text())
        except (OSError, ValueError) as exc:
            raise DoctorFailure("Doctor did not produce a valid report") from exc
    checks = report.get("checks") if isinstance(report, dict) else None
    if not isinstance(report, dict) or report.get("schema_version") != 1 or not isinstance(checks, list) or not checks or any(
        not isinstance(item, dict) or type(item.get("ok")) is not bool
        or not isinstance(item.get("label"), str) or not item["label"]
        or item.get("scope") not in {"runtime", "integration"} for item in checks
    ):
        raise DoctorFailure("Doctor report has an invalid schema")
    failed = [item for item in checks if not item["ok"]]
    if completed.returncode not in {0, 1} or bool(failed) != (completed.returncode == 1):
        raise DoctorFailure("Doctor exit status contradicts its report", report)
    if any(item["scope"] == "runtime" for item in failed) or (failed and not allow_degraded):
        raise DoctorFailure("Doctor failed: " + "; ".join(item["label"] for item in failed), report)
    report["integration_status"] = "degraded" if failed else "ok"
    report["runtime_validated"] = True
    return report


def resolve_update_failure(path: Path, command: list[str]) -> dict:
    """Preserve the original failure; annotate only the exact attempt validated."""
    original = path.read_bytes()
    state = json.loads(original)
    if not isinstance(state, dict) or state.get("status") != "failed":
        raise RuntimeError("The last attempt is not a failed update; no resolution needed.")
    report = validate_runtime(command, allow_degraded=False)
    # Updaters/revalidation serialize via the same maintenance_lock.
    if path.read_bytes() != original:
        raise RuntimeError("Update state changed during validation; retry revalidation.")
    state["resolution"] = {"status": "recovered", "resolved_at": datetime.now(timezone.utc).isoformat(),
                           "detail": "Full standard doctor passed after repair.", "doctor": report}
    write_report(path, state)
    return state


def mark_announced(path: Path, observed: dict) -> bool:
    """A delayed notification receipt must not overwrite recovery or a newer attempt."""
    with maintenance_lock(path.parent):
        current = json.loads(path.read_bytes())
        identity = ("status", "started_at", "completed_at", "commit", "version", "stage")
        if any(current.get(key) != observed.get(key) for key in identity):
            return False
        current["announced"] = True
        write_report(path, current)
        return True
