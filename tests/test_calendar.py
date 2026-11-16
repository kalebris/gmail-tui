"""Calendar draft parsing and desk basics (no live Google calls)."""

from __future__ import annotations

import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile

from gmail_tui.accounts import Account
from gmail_tui.cal_ui import CalendarDesk, event_draft_text, parse_event_draft, render_calendar
from gmail_tui.http_calendar import CalendarError, CalEvent, CalendarInfo, EventPatch


class EventDraftTests(unittest.TestCase):
    def test_round_trip_timed_event(self) -> None:
        start = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
        end = start + timedelta(hours=1)
        text = event_draft_text(
            EventPatch(
                summary="Standup",
                start=start,
                end=end,
                all_day=False,
                location="Zoom",
                description="daily",
            ),
            account="me@example.com",
            calendar_id="primary",
        )
        account, calendar_id, patch = parse_event_draft(text)
        self.assertEqual(account, "me@example.com")
        self.assertEqual(calendar_id, "primary")
        self.assertEqual(patch.summary, "Standup")
        self.assertEqual(patch.location, "Zoom")
        self.assertEqual(patch.description.strip(), "daily")
        self.assertFalse(patch.all_day)
        self.assertEqual(patch.start.hour, 10)
        self.assertEqual(patch.end.hour, 11)

    def test_all_day_extends_end_date(self) -> None:
        text = (
            "Account: me@example.com\n"
            "Calendar: primary\n"
            "Title: Holiday\n"
            "Kind: all-day\n"
            "When: 2026-11-12\n"
            "Until: 2026-11-12\n"
            "Location: \n"
            "Description:\n"
            "\n"
        )
        _account, _cal, patch = parse_event_draft(text)
        self.assertTrue(patch.all_day)
        self.assertEqual(patch.start.date(), date(2026, 11, 12))
        self.assertEqual(patch.end.date(), date(2026, 11, 13))

    def test_missing_when_is_rejected(self) -> None:
        with self.assertRaises(CalendarError):
            parse_event_draft("Title: x\nUntil: 2026-11-12\nDescription:\n")


class CalendarDeskTests(unittest.TestCase):
    def test_init_does_not_fetch_calendars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            desk = CalendarDesk([Account("a@example.com", root)])
            # Construction must stay local so mail startup is not blocked.
            self.assertEqual(desk.calendars, {})
            self.assertEqual(desk.events, [])
            self.assertEqual([node.label for node in desk.visible], ["a@example.com"])

    def test_tree_lists_accounts_and_toggles_calendars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            desk = CalendarDesk([Account("a@example.com", root), Account("b@example.com", root)])
            # No oauth → empty until reload; accounts still appear.
            names = [node.label for node in desk.visible if node.depth == 0]
            self.assertEqual(names, ["a@example.com", "b@example.com"])
            desk.calendars["a@example.com"] = [
                CalendarInfo(id="primary", summary="Primary", primary=True),
                CalendarInfo(id="work", summary="Work"),
            ]
            desk.expanded.add("acct:a@example.com")
            desk.enabled = {"cal:a@example.com:primary"}
            desk._rebuild_tree()
            labels = [node.label for node in desk.visible if node.calendar_id]
            self.assertIn("[x] Primary", labels)
            self.assertIn("[ ] Work", labels)
            desk.cal_cursor = next(
                index for index, node in enumerate(desk.visible) if node.calendar_id == "work"
            )
            desk.focus = "cals"
            desk.activate()
            self.assertIn("cal:a@example.com:work", desk.enabled)

    def test_range_bounds_for_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            desk = CalendarDesk([Account("a@example.com", Path(tmp))])
            desk.cursor = date(2026, 11, 12)  # Thursday
            desk.layout = "week"
            start, stop = desk.range_bounds()
            self.assertEqual(start.date(), date(2026, 11, 9))
            self.assertEqual(stop.date(), date(2026, 11, 16))
            desk.layout = "day"
            start, stop = desk.range_bounds()
            self.assertEqual(start.date(), date(2026, 11, 12))
            self.assertEqual((stop - start).days, 1)

    def test_week_shift_reuses_event_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            desk = CalendarDesk([Account("a@example.com", Path(tmp))])
            desk.layout = "week"
            desk.cursor = date(2026, 11, 12)
            desk.calendars["a@example.com"] = [
                CalendarInfo(id="primary", summary="Primary", primary=True),
            ]
            desk.enabled = {"cal:a@example.com:primary"}
            tz = timezone.utc
            this_week = CalEvent(
                id="1",
                calendar_id="primary",
                summary="This week",
                start=datetime(2026, 11, 12, 10, 0, tzinfo=tz),
                end=datetime(2026, 11, 12, 11, 0, tzinfo=tz),
                account="a@example.com",
                calendar_name="Primary",
            )
            next_week = CalEvent(
                id="2",
                calendar_id="primary",
                summary="Next week",
                start=datetime(2026, 11, 19, 10, 0, tzinfo=tz),
                end=datetime(2026, 11, 19, 11, 0, tzinfo=tz),
                account="a@example.com",
                calendar_name="Primary",
            )
            calls: list[tuple[datetime, datetime]] = []

            def fake_fetch(start: datetime, stop: datetime):
                calls.append((start, stop))
                return [this_week, next_week]

            desk._fetch_events = fake_fetch  # type: ignore[method-assign]
            desk.reload_events(force=True)
            self.assertEqual(len(calls), 1)
            self.assertEqual([item.summary for item in desk.events], ["This week"])
            # ±1 year window around the cursor.
            self.assertEqual(calls[0][0].date(), date(2025, 11, 12))
            self.assertEqual(calls[0][1].date(), date(2027, 11, 12))
            desk.shift_range(1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(desk.cursor, date(2026, 11, 19))
            self.assertEqual([item.summary for item in desk.events], ["Next week"])
            desk.shift_range(-1)
            self.assertEqual(len(calls), 1)
            self.assertEqual([item.summary for item in desk.events], ["This week"])

    def test_opening_the_calendar_does_not_wait_for_the_year(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            desk = CalendarDesk([Account("a@example.com", Path(tmp))])
            desk.layout = "week"
            desk.cursor = date(2026, 11, 12)
            desk.calendars["a@example.com"] = [
                CalendarInfo(id="primary", summary="Primary", primary=True),
            ]
            desk.enabled = {"cal:a@example.com:primary"}
            tz = timezone.utc
            this_week = CalEvent(
                id="1",
                calendar_id="primary",
                summary="This week",
                start=datetime(2026, 11, 12, 10, 0, tzinfo=tz),
                end=datetime(2026, 11, 12, 11, 0, tzinfo=tz),
                account="a@example.com",
                calendar_name="Primary",
            )
            calls: list[tuple[datetime, datetime]] = []
            year_started = threading.Event()
            release_year = threading.Event()

            def fake_fetch(start: datetime, stop: datetime):
                calls.append((start, stop))
                if (stop - start).days > 31:
                    year_started.set()
                    release_year.wait(2)
                return [this_week]

            desk._fetch_events = fake_fetch  # type: ignore[method-assign]
            started = time.monotonic()
            desk.present()
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.3)
            self.assertEqual(desk.notice, "loading events")
            deadline = time.monotonic() + 2
            while not any((stop - start).days <= 8 for start, stop in calls):
                if time.monotonic() > deadline:
                    break
                time.sleep(0.01)
            self.assertTrue(year_started.wait(2))
            self.assertLessEqual((calls[0][1] - calls[0][0]).days, 8)
            self.assertTrue(desk.poll_cache_update())
            self.assertEqual([item.summary for item in desk.events], ["This week"])
            self.assertEqual(desk.notice, "")
            release_year.set()
            if desk._cache_thread is not None:
                desk._cache_thread.join(2)
            if desk._open_thread is not None:
                desk._open_thread.join(2)


class EventLineTests(unittest.TestCase):
    def test_visible_events_filter_by_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            desk = CalendarDesk([Account("a@example.com", Path(tmp))])
            desk.layout = "day"
            desk.cursor = date(2026, 11, 12)
            tz = datetime.now().astimezone().tzinfo or timezone.utc
            desk.events = [
                CalEvent(
                    id="1",
                    calendar_id="primary",
                    summary="Today",
                    start=datetime(2026, 11, 12, 9, 0, tzinfo=tz),
                    end=datetime(2026, 11, 12, 10, 0, tzinfo=tz),
                    account="a@example.com",
                ),
                CalEvent(
                    id="2",
                    calendar_id="primary",
                    summary="Tomorrow",
                    start=datetime(2026, 11, 13, 9, 0, tzinfo=tz),
                    end=datetime(2026, 11, 13, 10, 0, tzinfo=tz),
                    account="a@example.com",
                ),
            ]
            titles = [item.summary for item in desk.visible_events()]
            self.assertEqual(titles, ["Today"])

    def test_day_view_renders_hour_timetable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            desk = CalendarDesk([Account("a@example.com", Path(tmp))])
            desk.layout = "day"
            desk.focus = "view"
            desk.cursor = date(2026, 11, 12)
            desk.hour_scroll = 8
            tz = datetime.now().astimezone().tzinfo or timezone.utc
            desk.events = [
                CalEvent(
                    id="1",
                    calendar_id="primary",
                    summary="Standup",
                    start=datetime(2026, 11, 12, 9, 0, tzinfo=tz),
                    end=datetime(2026, 11, 12, 11, 0, tzinfo=tz),
                    account="a@example.com",
                )
            ]
            lines = render_calendar(desk, 24, 100, desk.accounts, 0)
            joined = "\n".join(lines)
            self.assertIn("09:00", joined)
            self.assertIn("Standup", joined)
            self.assertIn("08:00", joined)
            # Multi-hour events repeat their from–to label on each covered hour.
            labeled = [
                line
                for line in lines
                if "Standup" in line and ("┃" in line or ">09" in line or ">10" in line)
            ]
            self.assertGreaterEqual(len(labeled), 2)
            self.assertTrue(all("–" in line for line in labeled))

    def test_overlapping_events_share_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            desk = CalendarDesk([Account("a@example.com", Path(tmp))])
            desk.layout = "day"
            desk.cursor = date(2026, 11, 12)
            desk.hour_scroll = 9
            tz = datetime.now().astimezone().tzinfo or timezone.utc
            desk.events = [
                CalEvent(
                    id="1",
                    calendar_id="primary",
                    summary="Alpha",
                    start=datetime(2026, 11, 12, 10, 0, tzinfo=tz),
                    end=datetime(2026, 11, 12, 11, 0, tzinfo=tz),
                    account="a@example.com",
                ),
                CalEvent(
                    id="2",
                    calendar_id="primary",
                    summary="Beta",
                    start=datetime(2026, 11, 12, 10, 0, tzinfo=tz),
                    end=datetime(2026, 11, 12, 11, 0, tzinfo=tz),
                    account="a@example.com",
                ),
            ]
            lines = render_calendar(desk, 24, 100, desk.accounts, 0)
            joined = "\n".join(lines)
            self.assertIn("Alpha", joined)
            self.assertIn("Beta", joined)
            self.assertNotIn("+1", joined)

    def test_all_day_events_sit_above_hours(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            desk = CalendarDesk([Account("a@example.com", Path(tmp))])
            desk.layout = "day"
            desk.cursor = date(2026, 11, 12)
            desk.hour_scroll = 8
            tz = datetime.now().astimezone().tzinfo or timezone.utc
            desk.events = [
                CalEvent(
                    id="1",
                    calendar_id="primary",
                    summary="Holiday",
                    start=datetime(2026, 11, 12, 0, 0, tzinfo=tz),
                    end=datetime(2026, 11, 13, 0, 0, tzinfo=tz),
                    all_day=True,
                    account="a@example.com",
                ),
                CalEvent(
                    id="2",
                    calendar_id="primary",
                    summary="Standup",
                    start=datetime(2026, 11, 12, 9, 0, tzinfo=tz),
                    end=datetime(2026, 11, 12, 10, 0, tzinfo=tz),
                    account="a@example.com",
                ),
            ]
            lines = render_calendar(desk, 24, 100, desk.accounts, 0)
            all_day_at = next(i for i, line in enumerate(lines) if "all-day" in line)
            holiday_at = next(i for i, line in enumerate(lines) if "Holiday" in line)
            hour_at = next(i for i, line in enumerate(lines) if "09:00" in line or "09:" in line)
            self.assertLess(all_day_at, holiday_at)
            self.assertLess(holiday_at, hour_at)

    def test_long_timed_events_sit_in_day_band(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            desk = CalendarDesk([Account("a@example.com", Path(tmp))])
            desk.layout = "day"
            desk.cursor = date(2026, 11, 12)
            desk.hour_scroll = 8
            tz = datetime.now().astimezone().tzinfo or timezone.utc
            desk.events = [
                CalEvent(
                    id="1",
                    calendar_id="primary",
                    summary="Out of office",
                    start=datetime(2026, 11, 12, 0, 0, tzinfo=tz),
                    end=datetime(2026, 11, 12, 20, 0, tzinfo=tz),
                    account="a@example.com",
                ),
                CalEvent(
                    id="2",
                    calendar_id="primary",
                    summary="Standup",
                    start=datetime(2026, 11, 12, 9, 0, tzinfo=tz),
                    end=datetime(2026, 11, 12, 10, 0, tzinfo=tz),
                    account="a@example.com",
                ),
            ]
            lines = render_calendar(desk, 24, 100, desk.accounts, 0)
            band_at = next(i for i, line in enumerate(lines) if "all-day" in line)
            long_at = next(i for i, line in enumerate(lines) if "Out of office" in line)
            hour_at = next(i for i, line in enumerate(lines) if "09:00" in line or "09:" in line)
            standup_hours = [
                line for line in lines if "Standup" in line and ("┃" in line or "│" in line)
            ]
            self.assertLess(band_at, long_at)
            self.assertLess(long_at, hour_at)
            # Long block stays out of the hour grid; short meeting still appears there.
            self.assertFalse(any("Out of office" in line and "┃" in line for line in lines))
            self.assertTrue(standup_hours)

    def test_week_view_renders_day_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            desk = CalendarDesk([Account("a@example.com", Path(tmp))])
            desk.layout = "week"
            desk.cursor = date(2026, 11, 12)
            desk.hour_scroll = 9
            lines = render_calendar(desk, 24, 120, desk.accounts, 0)
            joined = "\n".join(lines)
            # Week header includes day numbers; hour rows use HH:00 (or HH:—— for now).
            self.assertIn("09", joined)
            self.assertTrue(
                any(token in joined for token in ("09:00", "09:——", "08:00", "10:00")),
            )
            self.assertTrue(any(line.count("│") >= 3 for line in lines))

    def test_month_agenda_marks_now(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            desk = CalendarDesk([Account("a@example.com", Path(tmp))])
            desk.layout = "month"
            desk.cursor = date.today()
            tz = datetime.now().astimezone().tzinfo or timezone.utc
            past = datetime.now().astimezone() - timedelta(days=2)
            future = datetime.now().astimezone() + timedelta(days=2)
            desk.events = [
                CalEvent(
                    id="1",
                    calendar_id="primary",
                    summary="Past meetup",
                    start=past,
                    end=past + timedelta(hours=1),
                    account="a@example.com",
                ),
                CalEvent(
                    id="2",
                    calendar_id="primary",
                    summary="Future meetup",
                    start=future,
                    end=future + timedelta(hours=1),
                    account="a@example.com",
                ),
            ]
            lines = render_calendar(desk, 30, 100, desk.accounts, 0)
            joined = "\n".join(lines)
            self.assertIn("── now ──", joined)
            self.assertIn("Past meetup", joined)
            self.assertIn("Future meetup", joined)
            now_at = next(i for i, line in enumerate(lines) if "── now ──" in line)
            past_at = next(i for i, line in enumerate(lines) if "Past meetup" in line)
            future_at = next(i for i, line in enumerate(lines) if "Future meetup" in line)
            self.assertLess(past_at, now_at)
            self.assertLess(now_at, future_at)


if __name__ == "__main__":
    unittest.main()
