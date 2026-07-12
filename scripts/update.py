#!/usr/bin/env python3
"""Safely update an installed Perma Evidence Codex runtime from an exact Git commit."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path


REPOSITORY = "permaevidence/codex-plugins"
APP_SUPPORT_ROOT = Path.home() / "Library" / "Application Support" / "PermaEvidenceCodex"
CURRENT_LINK = APP_SUPPORT_ROOT / "current"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download, validate, and atomically activate the latest plugin runtime.")
    parser.add_argument("--ref", default="main", help="Git ref to resolve. Defaults to main.")
    return parser.parse_args()


def run(command: list[str]) -> None:
    print("+", " ".join(command))
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {completed.returncode}: {' '.join(command)}")


def resolve_commit(ref: str) -> str:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/commits/{ref}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "permaevidence-codex-updater"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    commit = str(payload.get("sha") or "")
    if len(commit) != 40:
        raise RuntimeError("GitHub did not return an immutable commit SHA")
    return commit


def import_module(path: Path, name: str):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def activate_runtime(source: Path, commit: str) -> Path:
    runtime_module = import_module(source / "scripts/runtime_install.py", "downloaded_runtime_install")
    installed = runtime_module.install_runtime(source, cachebuster=f"commit-{commit[:12]}")
    metadata = {"repository": REPOSITORY, "commit": commit}
    (installed / "INSTALL-METADATA.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return installed


def configure_runtime(root: Path) -> None:
    run([sys.executable, str(root / "scripts/install_plugins.py"), "--replace-marketplace"])
    run([sys.executable, str(root / "plugins/codex-long-term-memory/scripts/install.py")])
    setup = import_module(root / "scripts/setup.py", "installed_setup")
    config = setup.load_json(setup.TELEGRAM_CONFIG)
    cwd = Path(str(config.get("default_cwd") or Path.home())).expanduser()
    setup.trust_memory_hooks(cwd)
    run([sys.executable, str(root / "plugins/codex-long-term-memory/scripts/update_agents_injection.py"), "--cwd", str(cwd)])
    bridge = root / "plugins/codex-telegram-bridge/scripts/bridge.py"
    run([sys.executable, str(bridge), "install-service"])
    run([sys.executable, str(bridge), "doctor"])


def restore(previous: Path | None) -> None:
    if not previous or not previous.exists():
        return
    temp_link = APP_SUPPORT_ROOT / ".current.rollback"
    temp_link.unlink(missing_ok=True)
    temp_link.symlink_to(previous)
    os.replace(temp_link, CURRENT_LINK)
    try:
        configure_runtime(CURRENT_LINK)
    except Exception as exc:
        print(f"Automatic rollback activation also needs attention: {exc}", file=sys.stderr)


def main() -> int:
    args = parse_args()
    previous = CURRENT_LINK.resolve() if CURRENT_LINK.exists() else None
    commit = resolve_commit(args.ref)
    print(f"Resolved {args.ref} to {commit}")
    with tempfile.TemporaryDirectory(prefix="permaevidence-update-") as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / "source.zip"
        url = f"https://github.com/{REPOSITORY}/archive/{commit}.zip"
        with urllib.request.urlopen(url, timeout=60) as response, archive.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(tmp_path / "source")
        roots = [path for path in (tmp_path / "source").iterdir() if path.is_dir()]
        if len(roots) != 1:
            raise RuntimeError("Downloaded archive had an unexpected layout")
        try:
            installed = activate_runtime(roots[0], commit)
            configure_runtime(installed)
        except Exception:
            restore(previous)
            raise
    print(f"Update complete at commit {commit[:12]}.")
    print("Send /newsession in Telegram before testing updated MCP tools or skills.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"Update failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
