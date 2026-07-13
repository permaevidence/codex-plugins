from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.ical_calendar import (
    CalendarFeedError,
    fetch_calendar_events,
    parse_ical_events,
    validate_calendar_url,
)


RECURRING_ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:weekly@example.com
DTSTART;TZID=America/New_York:20260701T090000
DTEND;TZID=America/New_York:20260701T100000
RRULE:FREQ=WEEKLY;COUNT=5;BYDAY=MO,WE
EXDATE;TZID=America/New_York:20260708T090000
SUMMARY:Weekly meeting
LOCATION:Room 1
ATTENDEE;CN=Alice:mailto:alice@example.com
END:VEVENT
BEGIN:VEVENT
UID:weekly@example.com
RECURRENCE-ID;TZID=America/New_York:20260713T090000
DTSTART;TZID=America/New_York:20260713T110000
DTEND;TZID=America/New_York:20260713T120000
SUMMARY:Moved meeting
END:VEVENT
BEGIN:VEVENT
UID:holiday@example.com
DTSTART;VALUE=DATE:20260720
DTEND;VALUE=DATE:20260721
SUMMARY:All-day event
END:VEVENT
END:VCALENDAR
"""


class FakeResponse:
    def __init__(self, body: str):
        self.body = body.encode()
        self.headers = {"ETag": '"abc"', "Last-Modified": "Mon, 13 Jul 2026 00:00:00 GMT"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit: int):
        return self.body[:limit]


class IcalCalendarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tz = ZoneInfo("America/New_York")
        self.start = datetime(2026, 7, 1, tzinfo=self.tz)
        self.end = datetime(2026, 8, 1, tzinfo=self.tz)

    def test_recurring_events_exdates_and_overrides_are_expanded(self) -> None:
        events = parse_ical_events(RECURRING_ICS, self.start, self.end)
        timed = [event for event in events if event["uid"] == "weekly@example.com"]
        starts = [event["start"]["dateTime"] for event in timed]
        self.assertEqual(
            starts,
            [
                "2026-07-01T09:00:00-04:00",
                "2026-07-06T09:00:00-04:00",
                "2026-07-13T11:00:00-04:00",
                "2026-07-15T09:00:00-04:00",
            ],
        )
        self.assertEqual(timed[2]["summary"], "Moved meeting")
        self.assertEqual(timed[0]["attendees"][0]["email"], "alice@example.com")

    def test_all_day_events_remain_all_day(self) -> None:
        events = parse_ical_events(RECURRING_ICS, self.start, self.end)
        holiday = next(event for event in events if event["uid"] == "holiday@example.com")
        self.assertTrue(holiday["all_day"])
        self.assertEqual(holiday["start"], {"date": "2026-07-20"})

    def test_floating_event_uses_requested_calendar_timezone(self) -> None:
        text = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:floating@example.com
DTSTART:20260720T090000
DTEND:20260720T100000
SUMMARY:Floating appointment
END:VEVENT
END:VCALENDAR
"""
        events = parse_ical_events(text, self.start, self.end)
        self.assertEqual(events[0]["start"]["dateTime"], "2026-07-20T09:00:00-04:00")

    def test_monthly_last_weekday_bysetpos(self) -> None:
        text = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:last-weekday
DTSTART;TZID=America/New_York:20260130T090000
DTEND;TZID=America/New_York:20260130T093000
RRULE:FREQ=MONTHLY;COUNT=3;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1
SUMMARY:Month end
END:VEVENT
END:VCALENDAR
"""
        events = parse_ical_events(
            text,
            datetime(2026, 1, 1, tzinfo=self.tz),
            datetime(2026, 4, 1, tzinfo=self.tz),
        )
        self.assertEqual(
            [event["start"]["dateTime"][:10] for event in events],
            ["2026-01-30", "2026-02-27", "2026-03-31"],
        )

    def test_bad_events_are_skipped_without_dropping_valid_events(self) -> None:
        text = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:good
DTSTART;TZID=America/New_York:20260720T090000
DTEND;TZID=America/New_York:20260720T100000
SUMMARY:Valid meeting
END:VEVENT
BEGIN:VEVENT
UID:unsupported
DTSTART;TZID=America/New_York:20260720T110000
RRULE:FREQ=HOURLY;COUNT=2
SUMMARY:Unsupported recurrence
END:VEVENT
BEGIN:VEVENT
UID:impossible
DTSTART;TZID=America/New_York:20260201T120000
RRULE:FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30
SUMMARY:Impossible recurrence
END:VEVENT
END:VCALENDAR
"""
        warnings: list[str] = []
        events = parse_ical_events(text, self.start, self.end, warnings=warnings)
        self.assertEqual([event["uid"] for event in events], ["good"])
        self.assertTrue(any("UID unsupported" in warning for warning in warnings))
        self.assertTrue(any("UID impossible" in warning for warning in warnings))

    def test_only_private_google_https_urls_are_accepted(self) -> None:
        validate_calendar_url("https://calendar.google.com/calendar/ical/example/private-token/basic.ics")
        with self.assertRaises(CalendarFeedError):
            validate_calendar_url("http://calendar.google.com/calendar/ical/example/basic.ics")
        with self.assertRaises(CalendarFeedError):
            validate_calendar_url("https://example.com/private.ics")

    def test_recent_cache_is_used_during_temporary_fetch_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = root / "sources.json"
            cache = root / "cache.json"
            sources.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Primary",
                                "url": "https://calendar.google.com/calendar/ical/example/private-token/basic.ics",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            now = datetime(2026, 7, 13, tzinfo=timezone.utc)
            events, report = fetch_calendar_events(
                sources,
                cache,
                self.start,
                self.end,
                opener=lambda request, timeout: FakeResponse(RECURRING_ICS),
                now=now,
            )
            self.assertTrue(events)
            self.assertEqual(report["status"], "ok")

            events, report = fetch_calendar_events(
                sources,
                cache,
                self.start,
                self.end,
                opener=lambda request, timeout: (_ for _ in ()).throw(URLError("offline")),
                now=now,
            )
            self.assertTrue(events)
            self.assertEqual(report["status"], "warning")
            self.assertEqual(report["stale_sources"], 1)
            self.assertEqual(cache.stat().st_mode & 0o777, 0o600)

    def test_not_modified_revalidation_refreshes_cache_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = root / "sources.json"
            cache = root / "cache.json"
            sources.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Primary",
                                "url": "https://calendar.google.com/calendar/ical/example/private-token/basic.ics",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            initial = datetime(2026, 7, 1, tzinfo=timezone.utc)
            fetch_calendar_events(
                sources,
                cache,
                self.start,
                self.end,
                opener=lambda request, timeout: FakeResponse(RECURRING_ICS),
                now=initial,
            )

            revalidated = initial.replace(day=7)
            events, report = fetch_calendar_events(
                sources,
                cache,
                self.start,
                self.end,
                opener=lambda request, timeout: (_ for _ in ()).throw(
                    HTTPError(request.full_url, 304, "Not Modified", {}, None)
                ),
                now=revalidated,
            )
            self.assertTrue(events)
            self.assertEqual(report["status"], "ok")
            cached = json.loads(cache.read_text(encoding="utf-8"))
            record = next(iter(cached["sources"].values()))
            self.assertEqual(record["fetched_at"], revalidated.isoformat())

            events, report = fetch_calendar_events(
                sources,
                cache,
                self.start,
                self.end,
                opener=lambda request, timeout: (_ for _ in ()).throw(URLError("offline")),
                now=initial.replace(day=9),
            )
            self.assertTrue(events)
            self.assertEqual(report["status"], "warning")


if __name__ == "__main__":
    unittest.main()
