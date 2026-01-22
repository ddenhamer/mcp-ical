#!/usr/bin/env python3
"""
EventKit-based Calendar MCP server (macOS) — fast & local.

Tools:
- list_calendars()
- list_events(start, end, calendar_ids?, calendar_names?, include_notes?, max_results?)

Designed for stdio MCP (spawned by a desktop host via uv).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
import threading

from mcp.server.fastmcp import FastMCP

# PyObjC / EventKit + Foundation
import EventKit
import Foundation


mcp = FastMCP(
    "Apple Calendar (EventKit, local)",
    json_response=True,
    instructions=(
        "Read-only access to macOS Calendar via EventKit. "
        "Use list_calendars to find calendars; list_events to query a time range."
    ),
)

_store: Optional[EventKit.EKEventStore] = None
_authorized: Optional[bool] = None
_auth_lock = threading.Lock()


def _local_tzinfo():
    return datetime.now().astimezone().tzinfo


def _parse_iso_or_date(value: str) -> tuple[datetime, bool]:
    """
    Parse:
      - YYYY-MM-DD (date-only)
      - ISO 8601 datetime (with or without tz)
      - trailing Z

    Returns (dt_with_tz, is_date_only)
    """
    s = (value or "").strip()
    if not s:
        raise ValueError("start/end must be provided as ISO datetime or YYYY-MM-DD")

    is_date_only = "T" not in s
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"

    if is_date_only:
        dt = datetime.fromisoformat(s).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        dt = datetime.fromisoformat(s).replace(microsecond=0)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_local_tzinfo())

    return dt, is_date_only


def _nsdate(dt: datetime) -> Foundation.NSDate:
    # NSDate from POSIX timestamp (seconds since 1970)
    return Foundation.NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())


def _dt_from_nsdate(ns: Foundation.NSDate) -> datetime:
    return datetime.fromtimestamp(float(ns.timeIntervalSince1970()), tz=_local_tzinfo())


def _ensure_event_store_authorized() -> EventKit.EKEventStore:
    """
    Ensure EKEventStore exists and Calendar access has been requested.
    If your *host app* is the responsible process (unsandboxed + rights),
    the prompt/permission should attach to that app when it spawns this.
    """
    global _store, _authorized

    with _auth_lock:
        if _store is None:
            _store = EventKit.EKEventStore.alloc().init()

        if _authorized is None:
            done = threading.Event()
            box: dict[str, Any] = {"granted": False, "error": None}

            def completion(granted, error):
                box["granted"] = bool(granted)
                box["error"] = error
                done.set()

            _store.requestAccessToEntityType_completion_(EventKit.EKEntityTypeEvent, completion)

            # Wait for user response (or policy decision)
            done.wait(timeout=60)
            _authorized = bool(box["granted"])

        if not _authorized:
            raise RuntimeError(
                "Calendar access not granted. "
                "Ensure your host app has Calendar rights and the user approved access."
            )

        return _store


@mcp.tool()
def list_calendars() -> dict[str, Any]:
    """List all Apple Calendar calendars on macOS (names, IDs, sources) for event management."""
    store = _ensure_event_store_authorized()
    cals = store.calendarsForEntityType_(EventKit.EKEntityTypeEvent) or []

    items = []
    for c in cals:
        items.append(
            {
                "name": str(c.title() or ""),
                "identifier": str(c.calendarIdentifier() or ""),
                "source": str(c.source().title() if c.source() else ""),
            }
        )

    items.sort(key=lambda x: (x["name"].lower(), x["source"].lower()))
    return {"count": len(items), "calendars": items}


@mcp.tool()
def list_events(
    start: str,
    end: str,
    calendar_ids: list[str] | None = None,
    calendar_names: list[str] | None = None,
    include_notes: bool = False,
    max_results: int = 500,
) -> dict[str, Any]:
    """
    List Apple Calendar events on macOS overlapping a date/time range (schedule, appointments, meetings).
    
    Supports filtering by calendar names/IDs, including notes, and result limits.
    """
    store = _ensure_event_store_authorized()

    start_dt, start_date_only = _parse_iso_or_date(start)
    end_dt, end_date_only = _parse_iso_or_date(end)

    # Interpret start=end date-only as "that day"
    if start_date_only and end_date_only and start_dt == end_dt:
        end_dt = end_dt + timedelta(days=1)

    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt

    # calendar selection
    cals = store.calendarsForEntityType_(EventKit.EKEntityTypeEvent) or []

    if calendar_ids:
        wanted = {cid.strip() for cid in calendar_ids if cid and cid.strip()}
        cals = [c for c in cals if str(c.calendarIdentifier() or "") in wanted]

    if calendar_names:
        wanted = {n.strip().lower() for n in calendar_names if n and n.strip()}
        cals = [c for c in cals if str(c.title() or "").strip().lower() in wanted]

    pred = store.predicateForEventsWithStartDate_endDate_calendars_(
        _nsdate(start_dt), _nsdate(end_dt), cals
    )
    events = store.eventsMatchingPredicate_(pred) or []
    events = sorted(events, key=lambda e: float(e.startDate().timeIntervalSince1970()))

    cap = max(1, int(max_results))
    out = []
    for e in events[:cap]:
        ev = {
            "title": str(e.title() or ""),
            "calendar": str(e.calendar().title() if e.calendar() else ""),
            "calendar_id": str(e.calendar().calendarIdentifier() if e.calendar() else ""),
            "start": _dt_from_nsdate(e.startDate()).isoformat(),
            "end": _dt_from_nsdate(e.endDate()).isoformat(),
            "all_day": bool(e.isAllDay()),
            "location": str(e.location() or ""),
        }
        if include_notes:
            ev["notes"] = str(e.notes() or "")
        out.append(ev)

    return {
        "range": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
        "timezone": str(_local_tzinfo()),
        "count": len(out),
        "truncated": len(events) > len(out),
        "events": out,
    }


if __name__ == "__main__":
    # stdio transport (spawned MCP server)
    mcp.run(transport="stdio")
