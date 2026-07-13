"""Named overview-pack repository. Pattern: fittings_store.py (schema-versioned
atomic JSON in app_dir; refuse files from a newer FCTool)."""
from __future__ import annotations

import os
import shutil
import time
import uuid
from dataclasses import dataclass

from app_io import atomic_write_json
from app_path import app_dir
import overview_schema as osch

SCHEMA_VERSION = 1
DEFAULT_FILENAME = "overview_packs.json"


@dataclass
class PackRecord:
    pack_id: str
    name: str
    pack: osch.OverviewPack
    source: str = "editor"        # "editor" | "imported:<file>" | "dat:<account>"
    notes: str = ""
    created: float = 0.0
    modified: float = 0.0
    fingerprint: str = ""


class OverviewStore:
    def __init__(self, path=None):
        self.path = path or os.path.join(app_dir(), DEFAULT_FILENAME)
        self._packs = {}          # pack_id -> raw dict
        self._load()

    # -- persistence --------------------------------------------------------
    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            import json
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            try:
                shutil.copyfile(self.path, self.path + ".corrupt")
            except OSError:
                pass
            return
        schema = data.get("schema", 0)
        if schema > SCHEMA_VERSION:
            raise ValueError(
                f"overview pack store schema {schema} is newer than this "
                f"FCTool supports ({SCHEMA_VERSION})")
        self._packs = dict(data.get("packs", {}))

    def _save(self):
        atomic_write_json(self.path, {
            "schema": SCHEMA_VERSION,
            "packs": self._packs,
        }, indent=2, ensure_ascii=False)

    # -- record mapping -------------------------------------------------------
    def _to_record(self, pack_id, raw):
        return PackRecord(
            pack_id=pack_id,
            name=raw.get("name", ""),
            pack=osch.from_wire(raw.get("wire", {})),
            source=raw.get("source", "editor"),
            notes=raw.get("notes", ""),
            created=raw.get("created", 0.0),
            modified=raw.get("modified", 0.0),
            fingerprint=raw.get("fingerprint", ""),
        )

    # -- API ------------------------------------------------------------------
    def list_packs(self):
        recs = [self._to_record(pid, raw) for pid, raw in self._packs.items()]
        return sorted(recs, key=lambda r: r.name.casefold())

    def get_pack(self, pack_id):
        raw = self._packs.get(pack_id)
        return self._to_record(pack_id, raw) if raw else None

    def add_pack(self, name, pack, source="editor", notes=""):
        pack_id = uuid.uuid4().hex[:12]
        now = time.time()
        self._packs[pack_id] = {
            "name": name,
            "wire": osch.to_wire(pack),
            "source": source,
            "notes": notes,
            "created": now,
            "modified": now,
            "fingerprint": osch.fingerprint(pack),
        }
        self._save()
        return self.get_pack(pack_id)

    def update_pack(self, pack_id, pack=None, name=None, notes=None):
        raw = self._packs[pack_id]
        if pack is not None:
            raw["wire"] = osch.to_wire(pack)
            raw["fingerprint"] = osch.fingerprint(pack)
        if name is not None:
            raw["name"] = name
        if notes is not None:
            raw["notes"] = notes
        raw["modified"] = time.time()
        self._save()
        return self.get_pack(pack_id)

    def duplicate_pack(self, pack_id):
        src = self.get_pack(pack_id)
        return self.add_pack(src.name + " (copy)", src.pack,
                             source=src.source, notes=src.notes)

    def delete_pack(self, pack_id):
        self._packs.pop(pack_id, None)
        self._save()
