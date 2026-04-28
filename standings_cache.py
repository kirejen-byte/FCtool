"""
Standings cache for the Intelligence tab.

Builds a flat friendly/hostile entity-id set from the main character's
personal, corp, and alliance contact lists, persists it to disk, and
exposes is_friendly() for the analyzers.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


def is_friendly(
    char_id: int | None,
    corp_id: int | None,
    alliance_id: int | None,
    friendly_ids: set[int],
    own_character_ids: set[int],
) -> bool:
    if char_id is not None and char_id in own_character_ids:
        return True
    for entity in (char_id, corp_id, alliance_id):
        if entity is not None and entity in friendly_ids:
            return True
    return False


class StandingsCache:
    def __init__(self, path: str):
        self.path = path
        self.friendly_ids: set[int] = set()
        self.hostile_ids: set[int] = set()
        self.fetched_at: datetime | None = None
        self.source_character_id: int | None = None

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        self.friendly_ids = set(data.get("friendly_ids") or [])
        self.hostile_ids = set(data.get("hostile_ids") or [])
        ts = data.get("fetched_at")
        if ts:
            try:
                self.fetched_at = datetime.fromisoformat(ts)
            except ValueError:
                self.fetched_at = None
        self.source_character_id = data.get("source_character_id")

    def save(self) -> None:
        payload = {
            "fetched_at": (self.fetched_at or datetime.now(timezone.utc)).isoformat(),
            "source_character_id": self.source_character_id,
            "friendly_ids": sorted(self.friendly_ids),
            "hostile_ids": sorted(self.hostile_ids),
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, self.path)

    def is_stale(self, max_age_hours: float = 24.0) -> bool:
        if self.fetched_at is None:
            return True
        age = datetime.now(timezone.utc) - self.fetched_at
        return age > timedelta(hours=max_age_hours)

    def refresh(self, auth) -> None:
        """Pull contacts from ESI and rebuild the cache."""
        friendly: set[int] = set()
        hostile: set[int] = set()
        for getter in (auth.get_personal_contacts,
                       auth.get_corp_contacts,
                       auth.get_alliance_contacts):
            try:
                rows = getter() or []
            except Exception:
                rows = []
            for row in rows:
                cid = row.get("contact_id")
                standing = row.get("standing", 0)
                if cid is None:
                    continue
                if standing > 0:
                    friendly.add(int(cid))
                elif standing < 0:
                    hostile.add(int(cid))
        self.friendly_ids = friendly
        self.hostile_ids = hostile
        self.fetched_at = datetime.now(timezone.utc)
        self.source_character_id = getattr(auth, "_character_id", None)
        self.save()

    def age_string(self) -> str:
        if self.fetched_at is None:
            return "never"
        delta = datetime.now(timezone.utc) - self.fetched_at
        hours = delta.total_seconds() / 3600
        if hours < 1:
            return f"{int(delta.total_seconds() / 60)}m old"
        if hours < 24:
            return f"{int(hours)}h old"
        return f"{int(hours / 24)}d old"
