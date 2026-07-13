#!/usr/bin/env python3
"""Read private iCalendar feeds without Google API credentials.

The implementation intentionally supports the recurrence features emitted by
Google Calendar (RRULE, RDATE, EXDATE, and RECURRENCE-ID overrides) while
remaining dependency-free for the plugin's macOS and Linux installers.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import tempfile
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MAX_ICS_BYTES = 12 * 1024 * 1024
WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


class CalendarFeedError(RuntimeError):
    """A private calendar feed could not be validated, fetched, or parsed."""


def load_calendar_sources(path: Path) -> list[dict[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as exc:
        raise CalendarFeedError(f"Calendar source configuration is invalid: {exc}") from exc
    raw_sources = payload.get("sources", []) if isinstance(payload, dict) else payload
    if not isinstance(raw_sources, list):
        raise CalendarFeedError("Calendar source configuration must contain a sources array.")
    sources: list[dict[str, str]] = []
    for index, item in enumerate(raw_sources, start=1):
        if isinstance(item, str):
            name, url = f"Calendar {index}", item
        elif isinstance(item, dict):
            name = str(item.get("name") or f"Calendar {index}").strip()
            url = str(item.get("url") or "").strip()
        else:
            raise CalendarFeedError(f"Calendar source {index} is not valid.")
        validate_calendar_url(url)
        sources.append({"name": name, "url": url})
    return sources


def validate_calendar_url(url: str) -> None:
    parsed = urlparse(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host != "calendar.google.com":
        raise CalendarFeedError(
            "Calendar feeds must use a private https://calendar.google.com/... iCal URL."
        )


def fetch_calendar_events(
    sources_path: Path,
    cache_path: Path,
    window_start: datetime,
    window_end: datetime,
    *,
    timeout: int = 15,
    max_stale_seconds: int = 7 * 24 * 60 * 60,
    opener: Callable[..., Any] = urlopen,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sources = load_calendar_sources(sources_path)
    if not sources:
        return [], {"status": "disabled", "sources": 0, "stale_sources": 0}
    now = now or datetime.now(timezone.utc)
    cache = _load_cache(cache_path)
    cache_sources = cache.setdefault("sources", {})
    all_events: list[dict[str, Any]] = []
    failures: list[str] = []
    stale_sources = 0

    for source in sources:
        key = hashlib.sha256(source["url"].encode("utf-8")).hexdigest()
        cached = cache_sources.get(key, {}) if isinstance(cache_sources, dict) else {}
        text = ""
        stale = False
        request = Request(
            source["url"],
            headers={
                "User-Agent": "PermaEvidenceCodexCalendar/1",
                **({"If-None-Match": str(cached.get("etag"))} if cached.get("etag") else {}),
                **(
                    {"If-Modified-Since": str(cached.get("last_modified"))}
                    if cached.get("last_modified")
                    else {}
                ),
            },
        )
        try:
            with opener(request, timeout=timeout) as response:
                raw = response.read(MAX_ICS_BYTES + 1)
                if len(raw) > MAX_ICS_BYTES:
                    raise CalendarFeedError("A calendar feed exceeded the 12 MB safety limit.")
                text = raw.decode("utf-8-sig", errors="replace")
                if "BEGIN:VCALENDAR" not in text:
                    raise CalendarFeedError("Google returned data that is not an iCalendar feed.")
                headers = getattr(response, "headers", {})
                cache_sources[key] = {
                    "fetched_at": now.isoformat(),
                    "etag": headers.get("ETag", "") if hasattr(headers, "get") else "",
                    "last_modified": headers.get("Last-Modified", "") if hasattr(headers, "get") else "",
                    "ics": text,
                }
        except HTTPError as exc:
            if exc.code == 304 and cached.get("ics"):
                text = str(cached["ics"])
            else:
                text, stale = _cached_feed_or_raise(cached, now, max_stale_seconds, exc)
        except Exception as exc:
            try:
                text, stale = _cached_feed_or_raise(cached, now, max_stale_seconds, exc)
            except CalendarFeedError as cache_exc:
                failures.append(f"{source['name']}: {cache_exc}")
                continue

        try:
            events = parse_ical_events(text, window_start, window_end)
            for event in events:
                event["calendar"] = source["name"]
            all_events.extend(events)
            if stale:
                stale_sources += 1
        except Exception as exc:
            failures.append(f"{source['name']}: {type(exc).__name__}: {exc}")

    _save_cache(cache_path, cache)
    all_events.sort(key=lambda item: _event_sort_key(item))
    if not all_events and failures:
        raise CalendarFeedError("; ".join(failures)[:1000])
    return all_events, {
        "status": "warning" if failures or stale_sources else "ok",
        "sources": len(sources),
        "stale_sources": stale_sources,
        "failures": failures,
    }


def parse_ical_events(
    text: str,
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    if window_start.tzinfo is None or window_end.tzinfo is None:
        raise CalendarFeedError("Calendar expansion requires timezone-aware window bounds.")
    components = _event_components(text)
    grouped: dict[str, list[dict[str, list[tuple[dict[str, str], str]]]]] = {}
    for index, component in enumerate(components):
        uid = _first_text(component, "UID") or f"missing-uid-{index}"
        grouped.setdefault(uid, []).append(component)

    expanded: list[dict[str, Any]] = []
    for uid, group in grouped.items():
        masters = [item for item in group if not _has(item, "RECURRENCE-ID")]
        overrides = [item for item in group if _has(item, "RECURRENCE-ID")]
        override_map: dict[str, dict[str, list[tuple[dict[str, str], str]]]] = {}
        for override in overrides:
            recurrence_id, _ = _date_property(override, "RECURRENCE-ID")
            override_map[_instance_key(recurrence_id)] = override

        matched_overrides: set[str] = set()
        for master in masters:
            if _first_text(master, "STATUS").upper() == "CANCELLED":
                continue
            start, all_day = _date_property(master, "DTSTART")
            end = _event_end(master, start, all_day)
            duration = end - start
            occurrences = [start]
            if _has(master, "RRULE"):
                occurrences = _rrule_occurrences(start, _first_text(master, "RRULE"), window_end)
            occurrences.extend(_date_list_properties(master, "RDATE"))
            exclusions = {_instance_key(value) for value in _date_list_properties(master, "EXDATE")}
            unique_occurrences = sorted({_instance_key(value): value for value in occurrences}.values())
            for occurrence in unique_occurrences:
                key = _instance_key(occurrence)
                if key in exclusions:
                    continue
                override = override_map.get(key)
                if override is not None:
                    matched_overrides.add(key)
                    if _first_text(override, "STATUS").upper() == "CANCELLED":
                        continue
                    instance_start, instance_all_day = _date_property(override, "DTSTART")
                    instance_end = _event_end(override, instance_start, instance_all_day, default_duration=duration)
                    event = _render_event(uid, override, instance_start, instance_end, instance_all_day)
                else:
                    event = _render_event(uid, master, occurrence, occurrence + duration, all_day)
                if _overlaps(event, window_start, window_end):
                    expanded.append(event)

        for key, override in override_map.items():
            if key in matched_overrides or _first_text(override, "STATUS").upper() == "CANCELLED":
                continue
            start, all_day = _date_property(override, "DTSTART")
            end = _event_end(override, start, all_day)
            event = _render_event(uid, override, start, end, all_day)
            if _overlaps(event, window_start, window_end):
                expanded.append(event)

    expanded.sort(key=_event_sort_key)
    return expanded


def _event_components(text: str) -> list[dict[str, list[tuple[dict[str, str], str]]]]:
    lines = _unfold_lines(text)
    components: list[dict[str, list[tuple[dict[str, str], str]]]] = []
    current: dict[str, list[tuple[dict[str, str], str]]] | None = None
    for line in lines:
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            current = {}
            continue
        if upper == "END:VEVENT":
            if current is not None:
                components.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        bits = head.split(";")
        name = bits[0].upper()
        params: dict[str, str] = {}
        for bit in bits[1:]:
            if "=" in bit:
                key, raw = bit.split("=", 1)
                params[key.upper()] = raw.strip('"')
        current.setdefault(name, []).append((params, value))
    return components


def _unfold_lines(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    output: list[str] = []
    for line in normalized.split("\n"):
        if line.startswith((" ", "\t")) and output:
            output[-1] += line[1:]
        else:
            output.append(line)
    return output


def _has(component: dict[str, list[Any]], name: str) -> bool:
    return bool(component.get(name))


def _first_text(component: dict[str, list[tuple[dict[str, str], str]]], name: str) -> str:
    values = component.get(name) or []
    return _unescape_text(values[0][1]) if values else ""


def _date_property(
    component: dict[str, list[tuple[dict[str, str], str]]],
    name: str,
) -> tuple[datetime, bool]:
    values = component.get(name) or []
    if not values:
        raise CalendarFeedError(f"VEVENT is missing {name}.")
    return _parse_ical_datetime(values[0][1], values[0][0])


def _date_list_properties(
    component: dict[str, list[tuple[dict[str, str], str]]],
    name: str,
) -> list[datetime]:
    results: list[datetime] = []
    for params, raw in component.get(name) or []:
        for value in raw.split(","):
            first = value.split("/", 1)[0]
            parsed, _ = _parse_ical_datetime(first, params)
            results.append(parsed)
    return results


def _parse_ical_datetime(value: str, params: dict[str, str] | None = None) -> tuple[datetime, bool]:
    params = params or {}
    raw = value.strip()
    is_date = params.get("VALUE", "").upper() == "DATE" or bool(re.fullmatch(r"\d{8}", raw))
    if is_date:
        parsed_date = datetime.strptime(raw[:8], "%Y%m%d").date()
        return datetime.combine(parsed_date, dt_time.min, tzinfo=_local_timezone()), True
    tzid = params.get("TZID", "")
    tz = _timezone_for(tzid) if tzid else _local_timezone()
    if raw.endswith("Z"):
        tz = timezone.utc
        raw = raw[:-1]
    formats = ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M")
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=tz), False
        except ValueError:
            continue
    raise CalendarFeedError(f"Unsupported iCalendar date value: {value[:80]}")


def _timezone_for(tzid: str) -> Any:
    cleaned = tzid.strip().lstrip("/")
    aliases = {
        "US/Eastern": "America/New_York",
        "US/Central": "America/Chicago",
        "US/Mountain": "America/Denver",
        "US/Pacific": "America/Los_Angeles",
    }
    cleaned = aliases.get(cleaned, cleaned)
    try:
        return ZoneInfo(cleaned)
    except ZoneInfoNotFoundError:
        return _local_timezone()


def _local_timezone() -> Any:
    tz_name = os.environ.get("TZ", "").strip()
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def _event_end(
    component: dict[str, list[tuple[dict[str, str], str]]],
    start: datetime,
    all_day: bool,
    *,
    default_duration: timedelta | None = None,
) -> datetime:
    if _has(component, "DTEND"):
        return _date_property(component, "DTEND")[0]
    duration_text = _first_text(component, "DURATION")
    if duration_text:
        return start + _parse_duration(duration_text)
    return start + (default_duration or (timedelta(days=1) if all_day else timedelta(0)))


def _parse_duration(value: str) -> timedelta:
    match = re.fullmatch(
        r"(?P<sign>-)?P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
        value.strip().upper(),
    )
    if not match:
        raise CalendarFeedError(f"Unsupported iCalendar duration: {value[:80]}")
    delta = timedelta(
        weeks=int(match.group("weeks") or 0),
        days=int(match.group("days") or 0),
        hours=int(match.group("hours") or 0),
        minutes=int(match.group("minutes") or 0),
        seconds=int(match.group("seconds") or 0),
    )
    return -delta if match.group("sign") else delta


def _rrule_occurrences(start: datetime, raw_rule: str, window_end: datetime) -> list[datetime]:
    rule = _parse_rrule(raw_rule)
    frequency = rule.get("FREQ", "").upper()
    interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = None
    if rule.get("UNTIL"):
        until, _ = _parse_ical_datetime(rule["UNTIL"], {})
        if until.tzinfo is timezone.utc:
            until = until.astimezone(start.tzinfo)
    hard_end = min(window_end.astimezone(start.tzinfo) + timedelta(days=2), until or datetime.max.replace(tzinfo=start.tzinfo))
    if frequency not in {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}:
        raise CalendarFeedError(f"Unsupported recurrence frequency: {frequency or '(missing)'}")

    results: list[datetime] = []
    generated = 0
    period = 0
    while period < 100000:
        candidates = _period_candidates(start, frequency, interval, period, rule)
        if not candidates:
            period += 1
            continue
        candidates = _apply_bysetpos(candidates, rule.get("BYSETPOS", ""))
        stop = False
        for candidate in candidates:
            if candidate < start:
                continue
            if until is not None and candidate > until:
                stop = True
                break
            if candidate > hard_end:
                stop = True
                break
            generated += 1
            if count is not None and generated > count:
                stop = True
                break
            results.append(candidate)
        if stop or (count is not None and generated >= count):
            break
        period += 1
        if candidates and min(candidates) > hard_end:
            break
    if period >= 100000:
        raise CalendarFeedError("A recurrence rule exceeded the expansion safety limit.")
    return results


def _parse_rrule(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in raw.split(";"):
        if "=" in item:
            key, value = item.split("=", 1)
            result[key.upper()] = value.upper()
    return result


def _period_candidates(
    start: datetime,
    frequency: str,
    interval: int,
    period: int,
    rule: dict[str, str],
) -> list[datetime]:
    if frequency == "DAILY":
        candidate = start + timedelta(days=period * interval)
        return [candidate] if _matches_common_filters(candidate, start, rule) else []
    if frequency == "WEEKLY":
        week_start = (start - timedelta(days=start.weekday())) + timedelta(weeks=period * interval)
        weekdays = _weekday_numbers(rule.get("BYDAY", "")) or [start.weekday()]
        candidates = [week_start + timedelta(days=weekday) for weekday in weekdays]
        return [item for item in candidates if _matches_common_filters(item, start, rule)]
    if frequency == "MONTHLY":
        year, month = _add_months(start.year, start.month, period * interval)
        return _month_candidates(start, year, month, rule)
    year = start.year + period * interval
    months = _int_list(rule.get("BYMONTH", "")) or [start.month]
    candidates: list[datetime] = []
    for month in months:
        if 1 <= month <= 12:
            candidates.extend(_month_candidates(start, year, month, rule, ignore_bymonth=True))
    return candidates


def _month_candidates(
    start: datetime,
    year: int,
    month: int,
    rule: dict[str, str],
    *,
    ignore_bymonth: bool = False,
) -> list[datetime]:
    if not ignore_bymonth:
        allowed_months = _int_list(rule.get("BYMONTH", ""))
        if allowed_months and month not in allowed_months:
            return []
    days_in_month = calendar.monthrange(year, month)[1]
    month_days = _int_list(rule.get("BYMONTHDAY", ""))
    byday_tokens = [item for item in rule.get("BYDAY", "").split(",") if item]
    days: set[int] = set()
    if month_days:
        for value in month_days:
            day = value if value > 0 else days_in_month + value + 1
            if 1 <= day <= days_in_month:
                days.add(day)
    elif byday_tokens:
        for token in byday_tokens:
            match = re.fullmatch(r"([+-]?\d+)?([A-Z]{2})", token)
            if not match or match.group(2) not in WEEKDAYS:
                continue
            ordinal = int(match.group(1)) if match.group(1) else None
            weekday = WEEKDAYS[match.group(2)]
            matching = [
                day for day in range(1, days_in_month + 1)
                if date(year, month, day).weekday() == weekday
            ]
            if ordinal is None:
                days.update(matching)
            elif matching and -len(matching) <= ordinal <= len(matching) and ordinal != 0:
                days.add(matching[ordinal - 1] if ordinal > 0 else matching[ordinal])
    elif start.day <= days_in_month:
        days.add(start.day)
    return [
        start.replace(year=year, month=month, day=day)
        for day in sorted(days)
        if _matches_common_filters(start.replace(year=year, month=month, day=day), start, rule)
    ]


def _matches_common_filters(candidate: datetime, start: datetime, rule: dict[str, str]) -> bool:
    months = _int_list(rule.get("BYMONTH", ""))
    if months and candidate.month not in months:
        return False
    month_days = _int_list(rule.get("BYMONTHDAY", ""))
    if month_days:
        last = calendar.monthrange(candidate.year, candidate.month)[1]
        normalized = {value if value > 0 else last + value + 1 for value in month_days}
        if candidate.day not in normalized:
            return False
    bydays = _weekday_numbers(rule.get("BYDAY", ""))
    if bydays and candidate.weekday() not in bydays:
        return False
    return True


def _weekday_numbers(raw: str) -> list[int]:
    values: list[int] = []
    for token in raw.split(","):
        match = re.search(r"([A-Z]{2})$", token)
        if match and match.group(1) in WEEKDAYS:
            values.append(WEEKDAYS[match.group(1)])
    return sorted(set(values))


def _int_list(raw: str) -> list[int]:
    output: list[int] = []
    for item in raw.split(","):
        try:
            output.append(int(item))
        except ValueError:
            continue
    return output


def _apply_bysetpos(candidates: list[datetime], raw: str) -> list[datetime]:
    positions = _int_list(raw)
    if not positions:
        return sorted(set(candidates))
    ordered = sorted(set(candidates))
    selected: list[datetime] = []
    for position in positions:
        if position == 0 or abs(position) > len(ordered):
            continue
        selected.append(ordered[position - 1] if position > 0 else ordered[position])
    return sorted(set(selected))


def _add_months(year: int, month: int, amount: int) -> tuple[int, int]:
    zero_based = year * 12 + (month - 1) + amount
    return zero_based // 12, zero_based % 12 + 1


def _render_event(
    uid: str,
    component: dict[str, list[tuple[dict[str, str], str]]],
    start: datetime,
    end: datetime,
    all_day: bool,
) -> dict[str, Any]:
    attendees: list[dict[str, str]] = []
    for params, raw in component.get("ATTENDEE") or []:
        email = _unescape_text(raw)
        if email.lower().startswith("mailto:"):
            email = email[7:]
        item: dict[str, str] = {"email": email}
        if params.get("CN"):
            item["displayName"] = _unescape_text(params["CN"])
        attendees.append(item)
    return {
        "uid": uid,
        "summary": _first_text(component, "SUMMARY") or "Untitled",
        "location": _first_text(component, "LOCATION"),
        "description": _first_text(component, "DESCRIPTION"),
        "attendees": attendees,
        "start": {"date": start.date().isoformat()} if all_day else {"dateTime": start.isoformat()},
        "end": {"date": end.date().isoformat()} if all_day else {"dateTime": end.isoformat()},
        "all_day": all_day,
    }


def _overlaps(event: dict[str, Any], window_start: datetime, window_end: datetime) -> bool:
    start = _event_datetime(event["start"], window_start.tzinfo)
    end = _event_datetime(event["end"], window_start.tzinfo)
    return start < window_end and end > window_start


def _event_datetime(value: dict[str, str], fallback_tz: Any) -> datetime:
    if value.get("dateTime"):
        return datetime.fromisoformat(value["dateTime"])
    return datetime.combine(date.fromisoformat(value["date"]), dt_time.min, tzinfo=fallback_tz)


def _event_sort_key(event: dict[str, Any]) -> str:
    start = event.get("start", {})
    return str(start.get("dateTime") or start.get("date") or "")


def _instance_key(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=_local_timezone())
    return value.astimezone(timezone.utc).isoformat()


def _unescape_text(value: str) -> str:
    return (
        str(value or "")
        .replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _cached_feed_or_raise(
    cached: dict[str, Any],
    now: datetime,
    max_stale_seconds: int,
    exc: Exception,
) -> tuple[str, bool]:
    text = str(cached.get("ics") or "")
    fetched_at = str(cached.get("fetched_at") or "")
    try:
        age = (now - datetime.fromisoformat(fetched_at)).total_seconds()
    except Exception:
        age = float("inf")
    if text and age <= max(0, int(max_stale_seconds)):
        return text, True
    raise CalendarFeedError(
        f"Could not fetch the private calendar feed and no recent cache was available ({type(exc).__name__})."
    ) from exc


def _load_cache(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {"sources": {}}
    except Exception:
        return {"sources": {}}


def _save_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp = Path(raw_path)
    try:
        with open(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        temp.chmod(0o600)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
