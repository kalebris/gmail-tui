"""Google Calendar REST client (stdlib HTTP)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from gmail_tui.gmail import GmailError, Offline, RateLimited

_BASE = "https://www.googleapis.com/calendar/v3"


class CalendarError(GmailError):
    pass


class CalendarHttpStatus(CalendarError):
    def __init__(self, code: int, body: str) -> None:
        super().__init__(_explain_http(code, body))
        self.code = code
        self.body = body


def _explain_http(code: int, body: str) -> str:
    """Short, actionable text for the calendar sidebar."""
    lower = body.casefold()
    message = ""
    try:
        payload = json.loads(body)
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            message = str(err.get("message") or "")
    except json.JSONDecodeError:
        message = body.strip().splitlines()[0][:200] if body.strip() else ""
    if code == 403 and "has not been used" in lower and "calendar api" in lower:
        return "enable Calendar API in Google Cloud Console, then retry"
    if code == 403 and (
        "insufficient" in lower
        or "insufficient authentication scopes" in lower
        or "insufficient permission" in lower
    ):
        return "re-login this account (needs Calendar permission)"
    if code == 401:
        return "session expired — run gmail-tui login again"
    if message:
        return f"HTTP {code}: {message[:160]}"
    return f"Calendar HTTP {code}: {body[:200]}"


@dataclass(frozen=True)
class CalendarInfo:
    id: str
    summary: str
    primary: bool = False
    background: str = ""
    access_role: str = ""
    selected: bool = True


@dataclass
class CalEvent:
    id: str
    calendar_id: str
    summary: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str = ""
    description: str = ""
    html_link: str = ""
    account: str = ""
    calendar_name: str = ""


@dataclass
class EventPatch:
    summary: str = ""
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool = False
    location: str = ""
    description: str = ""


class HttpCalendar:
    def __init__(self, token: str, *, refresh=None, timeout: float = 30.0) -> None:
        if not token:
            raise ValueError("Calendar access token is empty")
        self.token = token
        self.timeout = timeout
        self._refresh = refresh

    def reauthorize(self, *, force: bool = False) -> bool:
        if self._refresh is None:
            return False
        token = self._refresh(force=force)
        if not token:
            return False
        self.token = token
        return True

    def list_calendars(self) -> list[CalendarInfo]:
        items: list[CalendarInfo] = []
        token = ""
        while True:
            query = [("maxResults", "250"), ("minAccessRole", "reader")]
            if token:
                query.append(("pageToken", token))
            data = self._json("GET", "/users/me/calendarList", query=query)
            for row in data.get("items") or []:
                if not isinstance(row, dict):
                    continue
                cal_id = _str(row.get("id"))
                if not cal_id:
                    continue
                items.append(
                    CalendarInfo(
                        id=cal_id,
                        summary=_str(row.get("summary")) or cal_id,
                        primary=bool(row.get("primary")),
                        background=_str(row.get("backgroundColor")),
                        access_role=_str(row.get("accessRole")),
                        selected=bool(row.get("selected", True)),
                    )
                )
            token = _str(data.get("nextPageToken"))
            if not token:
                break
        items.sort(key=lambda item: (not item.primary, item.summary.casefold()))
        return items

    def list_events(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
    ) -> list[CalEvent]:
        items: list[CalEvent] = []
        token = ""
        while True:
            query = [
                ("timeMin", _rfc3339(time_min)),
                ("timeMax", _rfc3339(time_max)),
                ("singleEvents", "true"),
                ("orderBy", "startTime"),
                ("maxResults", "2500"),
                (
                    "fields",
                    "items(id,summary,location,description,htmlLink,start,end),nextPageToken",
                ),
            ]
            if token:
                query.append(("pageToken", token))
            path = f"/calendars/{urllib.parse.quote(calendar_id, safe='')}/events"
            data = self._json("GET", path, query=query)
            for row in data.get("items") or []:
                if not isinstance(row, dict):
                    continue
                event = _parse_event(row, calendar_id)
                if event is not None:
                    items.append(event)
            token = _str(data.get("nextPageToken"))
            if not token:
                break
        return items

    def create_event(self, calendar_id: str, patch: EventPatch) -> CalEvent:
        path = f"/calendars/{urllib.parse.quote(calendar_id, safe='')}/events"
        data = self._json("POST", path, body=_event_body(patch))
        event = _parse_event(data, calendar_id)
        if event is None:
            raise CalendarError("Calendar create returned no event")
        return event

    def update_event(self, calendar_id: str, event_id: str, patch: EventPatch) -> CalEvent:
        path = (
            f"/calendars/{urllib.parse.quote(calendar_id, safe='')}"
            f"/events/{urllib.parse.quote(event_id, safe='')}"
        )
        data = self._json("PUT", path, body=_event_body(patch))
        event = _parse_event(data, calendar_id)
        if event is None:
            raise CalendarError("Calendar update returned no event")
        return event

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        path = (
            f"/calendars/{urllib.parse.quote(calendar_id, safe='')}"
            f"/events/{urllib.parse.quote(event_id, safe='')}"
        )
        self._json("DELETE", path)

    def _json(
        self,
        method: str,
        path: str,
        query: list[tuple[str, str]] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = _BASE + path
        if query:
            url = url + "?" + urllib.parse.urlencode(query)
        payload = None if body is None else json.dumps(body).encode()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            request = urllib.request.Request(url, data=payload, headers=headers, method=method)
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 401 and self.reauthorize(force=True):
                headers["Authorization"] = f"Bearer {self.token}"
                try:
                    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        raw = response.read()
                        status = response.status
                except urllib.error.HTTPError as again:
                    detail = again.read().decode("utf-8", errors="replace")
                    raise CalendarHttpStatus(again.code, detail) from again
                except urllib.error.URLError as again:
                    raise Offline(str(again.reason) if again.reason else str(again)) from again
            else:
                if exc.code == 403 and "Rate Limit" in detail:
                    raise RateLimited(detail) from exc
                if method == "DELETE" and exc.code == 404:
                    return {}
                raise CalendarHttpStatus(exc.code, detail) from exc
        except urllib.error.URLError as exc:
            raise Offline(str(exc.reason) if exc.reason else str(exc)) from exc
        if status >= 400:
            raise CalendarHttpStatus(status, raw.decode("utf-8", errors="replace"))
        if not raw:
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise CalendarError("Calendar response was not an object")
        return parsed


def _event_body(patch: EventPatch) -> dict[str, Any]:
    if patch.start is None or patch.end is None:
        raise CalendarError("event needs a start and end")
    body: dict[str, Any] = {"summary": patch.summary}
    if patch.location:
        body["location"] = patch.location
    if patch.description:
        body["description"] = patch.description
    if patch.all_day:
        body["start"] = {"date": patch.start.date().isoformat()}
        body["end"] = {"date": patch.end.date().isoformat()}
    else:
        body["start"] = {"dateTime": _rfc3339(patch.start)}
        body["end"] = {"dateTime": _rfc3339(patch.end)}
    return body


def _parse_event(row: dict[str, Any], calendar_id: str) -> CalEvent | None:
    event_id = _str(row.get("id"))
    if not event_id:
        return None
    start_raw = row.get("start") or {}
    end_raw = row.get("end") or {}
    if not isinstance(start_raw, dict) or not isinstance(end_raw, dict):
        return None
    all_day = "date" in start_raw and "dateTime" not in start_raw
    start = _parse_stamp(start_raw)
    end = _parse_stamp(end_raw)
    if start is None or end is None:
        return None
    return CalEvent(
        id=event_id,
        calendar_id=calendar_id,
        summary=_str(row.get("summary")) or "(no title)",
        start=start,
        end=end,
        all_day=all_day,
        location=_str(row.get("location")),
        description=_str(row.get("description")),
        html_link=_str(row.get("htmlLink")),
    )


def _parse_stamp(block: dict[str, Any]) -> datetime | None:
    if block.get("dateTime"):
        text = _str(block.get("dateTime"))
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if block.get("date"):
        try:
            day = date.fromisoformat(_str(block.get("date")))
        except ValueError:
            return None
        return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return None


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _str(value: object) -> str:
    return str(value or "").strip()
