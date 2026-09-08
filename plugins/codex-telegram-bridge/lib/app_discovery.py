"""Bounded, complete app/list traversal shared by doctor and the live client."""

from __future__ import annotations

import math
import time


class AppDiscoveryError(RuntimeError):
    def __init__(self, category: str, detail: str, partial: dict | None = None):
        self.category = category
        self.partial = dict(partial or {})
        super().__init__(f"App discovery incomplete ({category}): {detail}; {len(self.partial)} apps collected")


def discovery_options(config: dict) -> dict:
    """Reject invalid budgets; never turn a configuration error into 'disconnected'."""
    options = {}
    for key, default in (("initial_timeout", 60), ("page_timeout", 15), ("total_timeout", 120)):
        value = config.get(f"app_discovery_{key}_seconds", default)
        try:
            number = float(value)
        except (ValueError, TypeError):
            number = 0
        if isinstance(value, bool) or not math.isfinite(number) or not 0 < number <= 600:
            raise AppDiscoveryError("configuration", f"invalid app_discovery_{key}_seconds")
        options[key] = number
    return options


def discover_apps(request, *, initial_timeout=60, page_timeout=15, total_timeout=120,
                  max_pages=100, max_entries=20000, clock=time.monotonic) -> dict:
    found = {}
    cursor = None
    seen = set()
    entries = 0
    deadline = clock() + total_timeout
    for page in range(max_pages):
        remaining = deadline - clock()
        if remaining <= 0:
            raise AppDiscoveryError("budget", "overall deadline reached", found)
        params = {"limit": 200, "forceRefetch": page == 0}
        if cursor is not None:
            params["cursor"] = cursor
        try:
            result = request("app/list", params, timeout=min(remaining, initial_timeout if page == 0 else page_timeout))
        except AppDiscoveryError as exc:
            raise AppDiscoveryError(exc.category, f"page {page + 1} failed", found) from exc
        except Exception as exc:
            category = "timeout" if "tim" in str(exc).lower() or isinstance(exc, TimeoutError) else "request"
            # Transport errors can contain account/server details. Keep diagnostics bounded.
            raise AppDiscoveryError(category, f"page {page + 1} failed ({type(exc).__name__})", found) from exc
        if clock() > deadline:
            raise AppDiscoveryError("budget", "overall deadline reached", found)
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise AppDiscoveryError("protocol", "expected a data array", found)
        entries += len(result["data"])
        if entries > max_entries:
            raise AppDiscoveryError("budget", "entry limit reached", found)
        for app in result["data"]:
            if not isinstance(app, dict) or not isinstance(app.get("id"), str) or not app["id"].strip():
                raise AppDiscoveryError("protocol", "invalid app entry", found)
            found[app["id"].strip()] = app
        cursor = result.get("nextCursor")
        if cursor is None or cursor == "":
            return found
        if not isinstance(cursor, str) or not cursor.strip():
            raise AppDiscoveryError("protocol", "invalid next cursor", found)
        if cursor in seen:
            raise AppDiscoveryError("cursor", "repeated next cursor", found)
        seen.add(cursor)
    raise AppDiscoveryError("budget", "page limit reached", found)
