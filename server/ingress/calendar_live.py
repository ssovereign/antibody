"""Live calendar provider adapter (Google Calendar API v3, read scope).

Uses selected calendar IDs and a read-only OAuth access token supplied by the
operator (GOOGLE_CALENDAR_TOKEN, GOOGLE_CALENDAR_IDS). Missing credentials
produce an Unconfigured state - never silent sample data. Events go through
the same normalized-event parser and observation format as imported files.

Handles incremental sync tokens, cursor expiration (HTTP 410 -> full resync),
cancellations, recurrence instances (singleEvents=true) and deduplication by
provider identity plus revision (etag). It never accepts invites, sends
responses, modifies events or follows attachments.

Not exercised against the live Google service in this repository's tests
(no credentials available); covered with a mocked transport.
"""
from __future__ import annotations

import json
import os

from ..canonical import dumps, loads, utcnow
from .parsers import parse_event_json

API = "https://www.googleapis.com/calendar/v3"
SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"


class GoogleCalendarAdapter:
    provider = "google_calendar"

    def __init__(self, core, project_id: str, token: str | None = None, calendar_ids: list[str] | None = None, transport=None):
        self.core = core
        self.project_id = project_id
        self.token = token if token is not None else os.environ.get("GOOGLE_CALENDAR_TOKEN")
        ids = calendar_ids if calendar_ids is not None else [c for c in os.environ.get("GOOGLE_CALENDAR_IDS", "").split(",") if c]
        self.calendar_ids = ids
        self.transport = transport

    def state(self) -> dict:
        if not self.token or not self.calendar_ids:
            return {"state": "unconfigured", "detail": "Set GOOGLE_CALENDAR_TOKEN (read-only scope) and GOOGLE_CALENDAR_IDS to enable."}
        return {"state": "configured", "calendars": self.calendar_ids, "scope": SCOPE}

    def _record_state(self, state: str, detail: dict, cursor: str | None = None, key: str = "google_calendar") -> None:
        self.core.db.execute("INSERT OR REPLACE INTO integrations VALUES (?,?,?,?,?,?)", (self.project_id, key, state, dumps(detail), cursor, utcnow()))

    def sync(self) -> dict:
        import httpx
        st = self.state()
        if st["state"] != "configured":
            self._record_state("unconfigured", st)
            return {"state": "unconfigured", "sources": []}
        sources, stats = [], {"events": 0, "deduplicated": 0, "cancelled": 0, "resyncs": 0}
        with httpx.Client(transport=self.transport, timeout=15, headers={"Authorization": f"Bearer {self.token}"}) as client:
            for cal in self.calendar_ids:
                key = f"google_calendar:{cal}"
                row = self.core.db.one("SELECT cursor FROM integrations WHERE project_id=? AND integration=?", (self.project_id, key))
                sync_token = row["cursor"] if row else None
                items, page, next_sync = [], None, None
                while True:
                    params = {"singleEvents": "true", "maxResults": "250"}
                    if sync_token:
                        params["syncToken"] = sync_token
                    if page:
                        params["pageToken"] = page
                    r = client.get(f"{API}/calendars/{cal}/events", params=params)
                    if r.status_code == 410:  # cursor expired: full resync
                        stats["resyncs"] += 1
                        sync_token, page, items = None, None, []
                        continue
                    if r.status_code in (401, 403):
                        self._record_state("auth_error", {"status": r.status_code})
                        return {"state": "auth_error", "sources": sources}
                    r.raise_for_status()
                    data = r.json()
                    items.extend(data.get("items", []))
                    page = data.get("nextPageToken")
                    if not page:
                        next_sync = data.get("nextSyncToken")
                        break
                fresh = []
                for it in items:
                    uid, rev = it.get("id", ""), it.get("etag") or it.get("updated", "0")
                    seen = self.core.db.scalar("SELECT 1 FROM calendar_events WHERE project_id=? AND provider=? AND calendar_id=? AND event_uid=? AND revision=?",
                                               (self.project_id, self.provider, cal, uid, rev))
                    if seen:
                        stats["deduplicated"] += 1
                        continue
                    status = it.get("status", "confirmed").upper()
                    stats["cancelled"] += status == "CANCELLED"
                    fresh.append({"uid": uid, "revision": rev, "status": status, "title": it.get("summary", ""),
                                  "description": it.get("description", ""), "location": it.get("location", ""),
                                  "organizer": (it.get("organizer") or {}).get("email", ""),
                                  "attendees": [a.get("email", "") for a in it.get("attendees", [])],
                                  "start": (it.get("start") or {}).get("dateTime") or (it.get("start") or {}).get("date"),
                                  "end": (it.get("end") or {}).get("dateTime") or (it.get("end") or {}).get("date"),
                                  "time_zone": (it.get("start") or {}).get("timeZone"),
                                  "recurring_event_id": it.get("recurringEventId"),
                                  "attachments": [a.get("title", "attachment") for a in it.get("attachments", [])]})
                if fresh:
                    payload = json.dumps({"events": fresh}).encode("utf-8")
                    parsed = parse_event_json(payload, f"{cal}.json", provider=self.provider)
                    parsed.metadata.update({"account_ref": "configured-token", "calendar_id": cal, "live_provider": True})
                    src = self.core.sources._store(self.project_id, "calendar", parsed, payload, "EXTERNAL_DATA", label=f"Google Calendar {cal}", dedupe=False)
                    for ev in fresh:
                        self.core.db.execute("INSERT OR IGNORE INTO calendar_events VALUES (?,?,?,?,?,?,?,?)",
                                             (self.project_id, self.provider, "configured-token", cal, ev["uid"], ev["revision"], src["source_id"], ev["status"]))
                    sources.append(src)
                    stats["events"] += len(fresh)
                self._record_state("synced", {"calendar": cal, **stats}, next_sync, key=key)
        self._record_state("synced", stats)
        return {"state": "synced", "sources": sources, "stats": stats}
