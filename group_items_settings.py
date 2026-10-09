"""Group configuration: the eight Settings items (spec section 6.6).

``settings.staging_system``  the Tracking staging system (a name the system table must know)
``settings.channels``        channel names and their categories, merged through ``channel_store``
``settings.xup``             the x-up counter's words, channel and target
``settings.range_check``     the fleet-chat range check
``settings.zkill_sound``     the kill-alert sound, by catalogue key
``settings.market_staging``  the market system with its station or structure
``settings.contract_scan``   the three contract-scan switches
``settings.infra_scan``      scan-at-startup, and the scan regions in ``infrastructure.json``

Rules of this module
--------------------
* Every item writes ONLY the paths it declares (spec 6.7): the allowlist test is built from
  ``paths``. ``channel_store.save`` stays the only writer of the channels block; it rebinds
  that one top-level block (sibling keys inside it are kept) and touches nothing else - the
  legacy ``intel_channels`` block in particular is left exactly as it was. The one key it
  writes inside the block is ``registry``, so ``channels.registry`` is the declared path.
* Nothing here needs a character or the network (spec 9.1). A market STRUCTURE is the one
  value that does: it is never written here - one ``verify_structure`` task is queued for
  after the restart (spec 13). Until then the importer's own station or structure is kept
  when the market system is unchanged, and unset ("market not configured") when it changed.
* ``validate`` builds a clean payload from scratch and never raises; ``apply`` cleans what
  it is given once more, so a payload that skipped validation still cannot write a
  wrong-typed value. A value that is not in the payload is never read or written.
* ``SOUND_KEYS``, ``INFRA_VERSION``, ``STATION_ID_RANGE`` and ``STRUCTURE_ID_MIN`` MIRROR
  constants of ``fc_gui``, ``infra_store`` and ``market_esi``. Those modules are not
  importable here (the purity rule below); ``tests/test_group_items_settings.py`` holds
  the mirrors equal.

Pure: stdlib, ``group_bundle``, ``group_items`` and ``channel_store`` only. No Tk, no
``ui_theme``, no ``fc_gui`` - this module runs in the boot hook.
"""

import channel_store
from group_bundle import MAX_NAME, clean_text
from group_items import (ABSENT, MAX_LIST, Attention, ConfigFieldsItem, Field, Item, ItemResult,
                         Record, get_path, set_path, value_text)

# -- mirrors (guarded by tests) -------------------------------------------------
SOUND_KEYS = ("sonar", "klaxon", "blip", "horn", "comms", "bell")   # fc_gui _ALERT_SOUNDS
INFRA_VERSION = 1                               # infra_store.SCHEMA_VERSION
STATION_ID_RANGE = (60_000_000, 64_000_000)     # NPC stations: lo <= id < hi (market_esi)
STRUCTURE_ID_MIN = 1_000_000_000_000            # player structures: id >= this (market_esi)

# -- limits ---------------------------------------------------------------------
MAX_CHANNELS = MAX_LIST                         # spec section 5: 500 channels
MAX_CHANNEL_NAME = 100
MAX_CHANNEL_TAGS = 12
MAX_REGIONS = 100
MAX_PLACE_NAME = 120                            # a station or structure name
_MAX_TAG = MAX_NAME
_MAX_ID = 2 ** 63 - 1                           # every EVE id fits a signed 64-bit integer
_SHOWN = 5                                      # ids quoted in one problem line

_VERIFY_NOTE = "verified after restart"
_KEEP_NOTE = "verified after restart - your current market pick stays until then"
_PICK_NOTE = "pick a station or structure in Settings"


# -- small helpers --------------------------------------------------------------

def _view(state) -> dict:
    """The config for reading; a missing or unreadable one reads as empty."""
    config = getattr(state, "config", None)
    return config if isinstance(config, dict) else {}


def _writable(state) -> dict:
    """The config FOR WRITING, created when the file is missing. Call it only once there is
    something to write: an apply that changes nothing must leave a missing config missing."""
    if state.config is None:
        state.config = {}
    if not isinstance(state.config, dict):
        raise TypeError("config is not readable")
    return state.config


def _count(n, one, many) -> str:
    return f"{n} {one if n == 1 else many}"


def _key_set(keys) -> set:
    if isinstance(keys, str):
        return {keys}
    try:
        return {key for key in keys if isinstance(key, str)}
    except TypeError:
        return set()


def _same(current, value) -> bool:
    return type(current) is type(value) and current == value


# -- settings.staging_system ----------------------------------------------------

class _StagingSystemItem(ConfigFieldsItem):
    """One system name. An unknown one survives validation so that the apply can say so:
    the field is skipped and the Tracking section is flagged."""

    keep_unknown_systems = True

    def apply(self, state, payload, mode, conflict, ctx) -> ItemResult:
        result = super().apply(state, payload, mode, conflict, ctx)
        raw = payload.get("system") if isinstance(payload, dict) else None
        name = clean_text(raw, MAX_NAME) if isinstance(raw, str) else ""
        if "system" in result.skipped and name:
            result.attention.append(Attention(
                self.sections[0], self.id,
                f"The group's staging system '{name}' is not known to this version of "
                f"FCTool. Set it yourself."))
        return result


# -- settings.channels ----------------------------------------------------------

def _clean_tags(raw):
    """Tags of one channel -> ``(tags, clipped)``. ``Intel`` never travels with this item:
    which channels feed Intelligence Fusion is the Intelligence category's own item."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return [], False
    texts = [clean_text(tag, _MAX_TAG) for tag in raw if isinstance(tag, str)]
    intel = channel_store.TAG_INTEL.casefold()
    tags = [tag for tag in channel_store.normalize_tags(texts) if tag.casefold() != intel]
    return tags[:MAX_CHANNEL_TAGS], len(tags) > MAX_CHANNEL_TAGS


class _ChannelsItem(Item):
    """``[{"name": ..., "tags": [...]}, ...]`` - merge only: a channel is added, or its tags
    are folded into the entry the importer already has. Nothing is ever removed."""

    kind = "collection"
    files = ("config",)
    # `channels.registry`, not the whole block: the one key `channel_store.save` writes
    paths = (("config", (channel_store.CONFIG_KEY, channel_store.REGISTRY_KEY)),)

    def _clean(self, payload):
        """-> ``(records, problems)``. Total."""
        if not isinstance(payload, list):
            return [], [f"{self.label}: not a list of channels"]
        records, seen = [], set()
        junk = long_names = repeats = extra = clipped = 0
        for entry in payload:
            raw = entry.get("name") if isinstance(entry, dict) else None
            # Measured BEFORE any clip: a clip right-strips, which would hide the overflow
            # of a name whose 101st character is a space.
            name = clean_text(raw, len(raw)) if isinstance(raw, str) else ""
            if not name:
                junk += 1
                continue
            if len(name) > MAX_CHANNEL_NAME:    # clipping it would name another channel
                long_names += 1
                continue
            key = name.casefold()
            if key in seen:
                repeats += 1
                continue
            if len(records) >= MAX_CHANNELS:
                extra += 1
                continue
            tags, over = _clean_tags(entry.get("tags"))
            clipped += 1 if over else 0
            seen.add(key)
            records.append({"name": name, "tags": tags})
        problems = []
        if junk:
            problems.append(_count(junk, "entry that is not a channel",
                                   "entries that are not channels") + " left out")
        if long_names:
            problems.append(_count(long_names, "channel name", "channel names")
                            + f" longer than {MAX_CHANNEL_NAME} characters left out")
        if repeats:
            problems.append(_count(repeats, "repeated channel", "repeated channels")
                            + " left out (the first one counts)")
        if extra:
            problems.append(f"only the first {MAX_CHANNELS} channels are kept ({extra} left out)")
        if clipped:
            problems.append(_count(clipped, "channel has", "channels have")
                            + f" more than {MAX_CHANNEL_TAGS} categories; the first "
                              f"{MAX_CHANNEL_TAGS} are kept")
        return records, [f"{self.label}: {line}" for line in problems]

    def export(self, state, ctx):
        config = getattr(state, "config", None)
        if not isinstance(config, dict):
            return ABSENT
        try:
            channels = channel_store.load(config).channels()
        except Exception:
            return ABSENT
        raw = [{"name": channel.name, "tags": list(channel.tags)} for channel in channels]
        return self._clean(raw)[0] or ABSENT

    def validate(self, payload, ctx):
        records, problems = self._clean(payload)
        return (records or ABSENT), problems

    def _rows(self, payload):
        if not isinstance(payload, list):
            return []
        rows = []
        for entry in payload:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                tags = entry.get("tags")
                rows.append((entry["name"], [tag for tag in tags if isinstance(tag, str)]
                             if isinstance(tags, list) else []))
        return rows

    def records(self, payload) -> list:
        return [Record(name, name, ", ".join(tags)) for name, tags in self._rows(payload)]

    def summary(self, payload) -> str:
        return _count(len(self._rows(payload)), "channel", "channels")

    def lines(self, payload) -> list:
        return [(name, ", ".join(tags) or "(no category)") for name, tags in self._rows(payload)]

    def apply(self, state, payload, mode, conflict, ctx) -> ItemResult:
        result = ItemResult(self.id)
        records, _ = self._clean(payload)
        if not records:
            return result
        registry = channel_store.load(_view(state))
        for record in records:
            name = record["name"]
            before = registry.get(name)
            # A channel the importer feeds to Intelligence Fusion keeps its spelling: the
            # ingest gate is case-sensitive, and only the fusion item may re-spell it.
            feeds_intel = before is not None and before.has_tag(channel_store.TAG_INTEL)
            outcome = registry.merge(name, record["tags"], adopt_spelling=not feeds_intel)
            respelled = before is not None and before.name != name
            if outcome == "added":
                result.added.append(name)
            elif outcome == "updated":
                result.updated.append(name)
                if respelled and not feeds_intel:
                    result.notes.append(f"'{before.name}' is now spelled '{name}'")
            else:
                result.unchanged.append(name)
            if respelled and feeds_intel:
                result.notes.append(f"{name}: kept your spelling '{before.name}' "
                                    f"(it feeds Intelligence Fusion)")
        if result.added or result.updated:
            channel_store.save(_writable(state), registry)
        return result.settle()


# -- settings.market_staging ----------------------------------------------------

_MARKET = "market"
_SYSTEM_ID, _SYSTEM_NAME = "staging_system_id", "staging_system_name"
_REGION_ID = "staging_region_id"
_STATION_ID, _STATION_NAME = "staging_station_id", "staging_station_name"
_STRUCTURE_ID, _STRUCTURE_NAME = "staging_structure_id", "staging_structure_name"
_MARKET_KEYS = (_SYSTEM_ID, _SYSTEM_NAME, _REGION_ID, _STATION_ID, _STATION_NAME,
                _STRUCTURE_ID, _STRUCTURE_NAME)
# (id key, name key, the word for it in a note)
_OWN_STATION = (_STATION_ID, _STATION_NAME, "station")
_OWN_STRUCTURE = (_STRUCTURE_ID, _STRUCTURE_NAME, "structure")


def _whole(value):
    """``value`` as a whole number, or ``None``. A hand-edited config may hold an id as
    text or as a float; a switch is not an id. Never raises."""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _same_system(current, system) -> bool:
    """Is the stored market system the one of ``system`` (a clean payload system)?

    The stored ID decides whenever it is a positive whole number (after coercion: a
    hand-edited config may hold it as text or as a float) - the market scans by id, so a
    setup whose name says one system while its ids are another's IS on that other system.
    Only a setup without a usable id is judged by its name, ignoring case - deliberately
    tolerant there: calling a system "changed" unsets the importer's station and structure."""
    stored_id = _whole(current.get(_SYSTEM_ID))
    if stored_id is not None and stored_id > 0:
        return stored_id == system["id"]
    name = current.get(_SYSTEM_NAME)
    return isinstance(name, str) and name.strip().casefold() == system["name"].casefold()


def _has_pick(current) -> bool:
    """Whether the stored market holds a station or a structure of its own (an id above 0)."""
    for id_key in (_STATION_ID, _STRUCTURE_ID):
        place_id = _whole(current.get(id_key))
        if place_id is not None and place_id > 0:
            return True
    return False


def _is_station_id(value) -> bool:
    return type(value) is int and STATION_ID_RANGE[0] <= value < STATION_ID_RANGE[1]


def _is_structure_id(value) -> bool:
    return type(value) is int and STRUCTURE_ID_MIN <= value <= _MAX_ID


def _place(raw, id_ok):
    """``{"id", "name"}`` from an untrusted station or structure, or ``None``."""
    if not isinstance(raw, dict) or not id_ok(raw.get("id")):
        return None
    name = raw.get("name")
    return {"id": raw["id"],
            "name": clean_text(name, MAX_PLACE_NAME) if isinstance(name, str) else ""}


def _lookup(ctx, name):
    """``(canonical name, system id, region id)`` from the system table, or ``None``."""
    try:
        hit = ctx.system_ids(name)
    except Exception:
        return None
    if not isinstance(hit, (tuple, list)) or len(hit) != 3:
        return None
    canonical, system_id, region_id = hit
    if not isinstance(canonical, str) or not canonical:
        return None
    if type(system_id) is not int or not 0 < system_id <= _MAX_ID:
        return None
    if type(region_id) is not int or not 0 < region_id <= _MAX_ID:
        region_id = 0
    return canonical, system_id, region_id


class _MarketStagingItem(Item):
    """``{"system": {"name", "id", "region_id"}, "station": {"id", "name"} | None,
    "structure": {"id", "name"} | None}``.

    The system is the identity of the payload: its ids always come from THIS build's system
    table, and a system the table does not know is kept with ``id`` 0 - the apply then
    writes nothing and flags the Market section. At most one of station and structure
    survives validation (the structure wins, as it does in the market source).
    """

    kind = "value"
    files = ("config",)
    paths = tuple(("config", (_MARKET, key)) for key in _MARKET_KEYS)

    def _clean(self, payload, ctx):
        """-> ``(clean payload | ABSENT, problems)``. Total."""
        if not isinstance(payload, dict):
            return ABSENT, [f"{self.label}: not a market staging block"]
        system = payload.get("system")
        raw_name = system.get("name") if isinstance(system, dict) else None
        name = clean_text(raw_name, MAX_NAME) if isinstance(raw_name, str) else ""
        if not name:
            return ABSENT, [f"{self.label}: no market system named"]
        problems = []
        hit = _lookup(ctx, name)
        if hit:
            canonical, system_id, region_id = hit
            file_ids = (system.get("id"), system.get("region_id"))
            if any(type(theirs) is int and theirs > 0 and theirs != mine
                   for theirs, mine in zip(file_ids, (system_id, region_id))):
                problems.append(f"{self.label}: the ids in the file for '{canonical}' differ "
                                f"from this version's system table; the table's are used")
            clean_system = {"name": canonical, "id": system_id, "region_id": region_id}
        else:
            problems.append(f"{self.label}: '{name}' is not a known system")
            clean_system = {"name": name, "id": 0, "region_id": 0}

        raw_station, raw_structure = payload.get("station"), payload.get("structure")
        station = _place(raw_station, _is_station_id)
        structure = _place(raw_structure, _is_structure_id)
        if raw_station is not None and station is None:
            problems.append(f"{self.label}: the station is left out (not an NPC station id)")
        if raw_structure is not None and structure is None:
            problems.append(f"{self.label}: the structure is left out (not a structure id)")
        if station is not None and structure is not None:
            station = None
            problems.append(f"{self.label}: a station and a structure were both given; "
                            f"the station is left out")
        return {"system": clean_system, "station": station, "structure": structure}, problems

    def export(self, state, ctx):
        market = _view(state).get(_MARKET)
        if not isinstance(market, dict):
            return ABSENT
        name = market.get(_SYSTEM_NAME)
        if not isinstance(name, str) or not name.strip():
            return ABSENT

        def place(id_key, name_key):
            place_id = market.get(id_key)
            if type(place_id) is not int or place_id <= 0:
                return None
            return {"id": place_id, "name": market.get(name_key)}

        payload, _ = self._clean({"system": {"name": name},
                                  "station": place(_STATION_ID, _STATION_NAME),
                                  "structure": place(_STRUCTURE_ID, _STRUCTURE_NAME)}, ctx)
        if payload is ABSENT or payload["system"]["id"] <= 0:
            return ABSENT
        return payload

    def validate(self, payload, ctx):
        return self._clean(payload, ctx)

    @staticmethod
    def _parts(payload):
        """``(system, station, structure)`` of a payload; ``None`` for a part it lacks."""
        if not isinstance(payload, dict):
            return None, None, None
        system = payload.get("system")
        if not (isinstance(system, dict) and isinstance(system.get("name"), str)):
            return None, None, None

        def part(key):
            value = payload.get(key)
            return value if isinstance(value, dict) and type(value.get("id")) is int else None

        return system, part("station"), part("structure")

    @staticmethod
    def _place_text(place) -> str:
        name = place.get("name")
        return name if isinstance(name, str) and name else str(place["id"])

    def records(self, payload) -> list:
        system, station, structure = self._parts(payload)
        if system is None:
            return []
        rows = [Record("system", "Market system", system["name"])]
        if station is not None:
            rows.append(Record("station", "Station", self._place_text(station)))
        if structure is not None:
            rows.append(Record("structure", "Structure", self._place_text(structure)))
        return rows

    def summary(self, payload) -> str:
        return " - ".join(row.detail for row in self.records(payload))

    def lines(self, payload) -> list:
        return [(row.label, row.detail) for row in self.records(payload)]

    def subset(self, payload, keys):
        """The system is required: a station or a structure is nothing without it."""
        wanted = _key_set(keys)
        system, station, structure = self._parts(payload)
        if system is None or "system" not in wanted:
            return ABSENT
        return {"system": dict(system),
                "station": dict(station) if station is not None and "station" in wanted else None,
                "structure": dict(structure)
                if structure is not None and "structure" in wanted else None}

    def apply(self, state, payload, mode, conflict, ctx) -> ItemResult:
        result = ItemResult(self.id)
        clean, _ = self._clean(payload, ctx)
        if clean is ABSENT:
            return result
        system, station, structure = clean["system"], clean["station"], clean["structure"]
        if system["id"] <= 0:
            result.status = "skipped"
            result.skipped.extend(row.key for row in self.records(clean))
            result.warnings.append(f"{self.label}: '{system['name']}' is not a known system")
            result.attention.append(Attention(
                self.sections[0], self.id,
                f"The group's market system '{system['name']}' is not known to this version "
                f"of FCTool. Set the market staging yourself."))
            return result

        stored = _view(state).get(_MARKET)
        current = dict(stored) if isinstance(stored, dict) else {}    # as it was before
        written = set()

        def put(key, value):
            if not _same(current.get(key, ABSENT), value):
                set_path(_writable(state), (_MARKET, key), value)
                written.add(key)

        def unset(*places):
            """Zero the importer's own station and / or structure, and say which one went."""
            for id_key, name_key, what in places:
                old_id, old_name = _whole(current.get(id_key)), current.get(name_key)
                put(id_key, 0)
                put(name_key, "")
                if old_id is not None and old_id > 0:       # there was one to lose
                    shown = clean_text(old_name if isinstance(old_name, str) else "",
                                       MAX_PLACE_NAME) or old_id
                    result.notes.append(f"your {what} '{shown}' was unset")

        system_changed = not _same_system(current, system)
        put(_SYSTEM_ID, system["id"])
        put(_SYSTEM_NAME, system["name"])
        put(_REGION_ID, system["region_id"])
        if written:
            result.updated.append("system")
            old = current.get(_SYSTEM_NAME, ABSENT)
            result.notes.append(
                f"Market system: {value_text(old, 60)} -> {system['name']}"
                if not _same(old, system["name"]) else
                f"Market system: {system['name']} (ids brought up to date)")
        else:
            result.unchanged.append("system")

        before = set(written)
        if station is not None:
            same_station = _same(current.get(_STATION_ID, ABSENT), station["id"])
            put(_STATION_ID, station["id"])
            if station["name"] or not same_station:     # a blank name never erases a good one
                put(_STATION_NAME, station["name"])
            unset(_OWN_STRUCTURE)
            if written - before:
                result.updated.append("station")
                result.notes.append(f"Station: {self._place_text(station)}")
            else:
                result.unchanged.append("station")
        elif structure is not None:
            if _same(current.get(_STRUCTURE_ID, ABSENT), structure["id"]):
                result.unchanged.append("structure")    # already the importer's: nothing more
            else:
                # Never written here: whether THIS user's character can reach the structure
                # is only known after one ESI call. Meanwhile a pick in the same system is
                # KEPT (a re-import must not cost a member who cannot dock there the pick
                # that works for them); one in another system is unset - the market is
                # "not configured" until the verification.
                if system_changed:
                    unset(_OWN_STATION, _OWN_STRUCTURE)
                (result.updated if written - before else result.unchanged).append("structure")
                result.tasks.append({"kind": "verify_structure",
                                     "structure_id": structure["id"],
                                     "name": structure["name"],
                                     "system_id": system["id"],
                                     "attempts": 0})
                # "your current market pick stays" only to a setup that has one to keep
                result.notes.append(_KEEP_NOTE if not system_changed and _has_pick(current)
                                    else _VERIFY_NOTE)
        elif system_changed:
            unset(_OWN_STATION, _OWN_STRUCTURE)
            result.notes.append(_PICK_NOTE)
        return result.settle()


# -- settings.infra_scan --------------------------------------------------------

_INFRA_FLAG = ("infra", "auto_scan_on_start")
_FLAG_KEY, _REGIONS_KEY = "auto_scan_on_start", "regions"
_FLAG_LABEL, _REGIONS_LABEL = "Auto-scan configured regions at startup", "Scan regions"


def _region_known(ctx, region_id) -> bool:
    try:
        return bool(ctx.region_known(region_id))
    except Exception:
        return False


class _InfraScanItem(Item):
    """``{"auto_scan_on_start": bool, "regions": [int, ...]}`` (any subset).

    The flag lives in ``config.json``, the region list in ``infrastructure.json`` - of which
    this item reads and writes the ``regions`` key and nothing else: the structures a scan
    found and the scan history stay the importer's own. ``partial_ok``: an unreadable or
    newer infrastructure file costs the regions (with a warning), never the flag.
    """

    kind = "value"
    files = ("config", "infra")
    paths = (("config", _INFRA_FLAG), ("infra", (_REGIONS_KEY,)))
    modes = ("merge", "replace")
    partial_ok = True
    never_saved = ("The structures your scans found", "Your scan history")

    def _clean_regions(self, raw, ctx):
        """-> ``(region ids, problems)``: positive whole numbers the region table knows,
        each once, at most ``MAX_REGIONS``. Total."""
        if not isinstance(raw, list):
            return [], [f"{self.label} - {_REGIONS_LABEL}: is not a list of region ids"]
        regions, seen, unknown = [], set(), []
        junk = extra = 0
        for entry in raw:
            if type(entry) is not int or not 0 < entry <= _MAX_ID:
                junk += 1
                continue
            if entry in seen:
                continue
            seen.add(entry)
            if not _region_known(ctx, entry):
                unknown.append(entry)
            elif len(regions) >= MAX_REGIONS:
                extra += 1
            else:
                regions.append(entry)
        problems = []
        if unknown:
            more = len(unknown) - _SHOWN
            shown = ", ".join(str(region) for region in unknown[:_SHOWN])
            problems.append(_count(len(unknown), "unknown region", "unknown regions")
                            + f" left out ({shown}{f' and {more} more' if more > 0 else ''})")
        if junk:
            problems.append(_count(junk, "entry that is not a region id",
                                   "entries that are not region ids") + " left out")
        if extra:
            problems.append(f"only the first {MAX_REGIONS} regions are kept ({extra} left out)")
        return regions, [f"{self.label} - {_REGIONS_LABEL}: {line}" for line in problems]

    def _clean(self, payload, ctx):
        if not isinstance(payload, dict):
            return ABSENT, [f"{self.label}: not a settings block"]
        clean, problems = {}, []
        if _FLAG_KEY in payload:
            if type(payload[_FLAG_KEY]) is bool:
                clean[_FLAG_KEY] = payload[_FLAG_KEY]
            else:
                problems.append(f"{self.label} - {_FLAG_LABEL}: is not on or off")
        if _REGIONS_KEY in payload:
            regions, lines = self._clean_regions(payload[_REGIONS_KEY], ctx)
            problems.extend(lines)
            if regions:                         # an empty list is "no list", never "clear mine"
                clean[_REGIONS_KEY] = regions
        return (clean or ABSENT), problems

    @staticmethod
    def _newer(infra) -> bool:
        version = infra.get("version")
        return type(version) is int and version > INFRA_VERSION

    def export(self, state, ctx):
        payload = {}
        flag = get_path(_view(state), _INFRA_FLAG, ABSENT)
        if type(flag) is bool:
            payload[_FLAG_KEY] = flag
        infra = getattr(state, "infra", None)
        if isinstance(infra, dict) and not self._newer(infra):
            regions, _ = self._clean_regions(infra.get(_REGIONS_KEY), ctx)
            if regions:
                payload[_REGIONS_KEY] = regions
        return payload or ABSENT

    def validate(self, payload, ctx):
        return self._clean(payload, ctx)

    def _present(self, payload):
        """``[(key, label, text)]`` for the parts a payload carries."""
        if not isinstance(payload, dict):
            return []
        rows = []
        if type(payload.get(_FLAG_KEY)) is bool:
            rows.append((_FLAG_KEY, _FLAG_LABEL, value_text(payload[_FLAG_KEY])))
        regions = payload.get(_REGIONS_KEY)
        if isinstance(regions, list) and regions:
            rows.append((_REGIONS_KEY, _REGIONS_LABEL,
                         value_text([region for region in regions if type(region) is int])))
        return rows

    def records(self, payload) -> list:
        return [Record(key, label, text) for key, label, text in self._present(payload)]

    def lines(self, payload) -> list:
        return [(label, text) for _key, label, text in self._present(payload)]

    def summary(self, payload) -> str:
        parts = []
        for key, _label, text in self._present(payload):
            if key == _FLAG_KEY:
                parts.append(f"auto-scan {text}")
            else:
                parts.append(_count(len(payload[_REGIONS_KEY]), "region", "regions"))
        return ", ".join(parts)

    def _apply_regions(self, state, incoming, mode, result) -> None:
        infra = state.infra
        reason = None
        if infra is not None and not isinstance(infra, dict):
            reason = "could not be read"
        elif isinstance(infra, dict) and self._newer(infra):
            reason = "was written by a newer FCTool"
        if reason:
            result.skipped.append(_REGIONS_KEY)
            result.warnings.append(f"{self.label}: infrastructure.json {reason}; "
                                   f"your scan regions were not changed")
            return
        stored = infra.get(_REGIONS_KEY) if isinstance(infra, dict) else None
        kept = stored if isinstance(stored, list) else []
        mine = [region for region in kept if type(region) is int]
        added = [region for region in incoming if region not in mine]
        if mode == "replace":
            new = list(incoming)
            removed = [region for region in dict.fromkeys(mine) if region not in incoming]
        else:
            new = list(kept) + added            # an import never deletes: mine stay, verbatim
            removed = []
        if isinstance(stored, list) and new == stored:
            result.unchanged.append(_REGIONS_KEY)
            return
        if infra is None:
            state.infra = {"version": INFRA_VERSION, "entries": {}, _REGIONS_KEY: new,
                           "scan_state": {"regions": {}, "systems": {}}}   # as the store saves
        else:
            infra[_REGIONS_KEY] = new
        result.updated.append(_REGIONS_KEY)
        result.removed.extend(str(region) for region in removed)
        parts = ([f"{len(added)} added"] if added else []) + (
            [f"{len(removed)} removed"] if removed else [])
        result.notes.append(f"{_REGIONS_LABEL}: {', '.join(parts) or 'put in the same order'}")

    def apply(self, state, payload, mode, conflict, ctx) -> ItemResult:
        result = ItemResult(self.id)
        clean, _ = self._clean(payload, ctx)
        if clean is ABSENT:
            return result
        if _FLAG_KEY in clean:
            value = clean[_FLAG_KEY]
            current = get_path(_view(state), _INFRA_FLAG, ABSENT)
            if _same(current, value):
                result.unchanged.append(_FLAG_KEY)
            else:
                set_path(_writable(state), _INFRA_FLAG, value)
                result.updated.append(_FLAG_KEY)
                result.notes.append(f"{_FLAG_LABEL}: {value_text(current)} -> "
                                    f"{value_text(value)}")
        if _REGIONS_KEY in clean:
            self._apply_regions(state, clean[_REGIONS_KEY], mode, result)
        if result.skipped and not (result.added or result.updated or result.removed):
            result.status = "skipped"
        return result.settle()


# -- the items ------------------------------------------------------------------

ITEMS = (
    _StagingSystemItem(
        id="settings.staging_system", label="Staging system", order=10,
        sections=("Tracking",),
        fields=(Field("system", "Staging system", "system", ("zkillboard", "staging_system")),)),
    _ChannelsItem(
        id="settings.channels", label="Channels and their categories", order=20),
    ConfigFieldsItem(
        id="settings.xup", label="X-up counter", order=30,
        fields=(
            Field("trigger_word", "Trigger word", "str", ("xup", "trigger_word"), max_len=30),
            Field("fire_word", "Fire word", "str", ("xup", "fire_word"), max_len=30),
            Field("channel_name", "Channel", "str", ("xup", "channel_name"), max_len=60),
            Field("threshold", "Threshold", "int", ("xup", "threshold"), lo=1, hi=500),
            Field("tie_to_dps", "Target follows the DPS count", "bool", ("xup", "tie_to_dps")),
            Field("case_sensitive", "Case sensitive", "bool", ("xup", "case_sensitive")),
        )),
    ConfigFieldsItem(
        id="settings.range_check", label="Range check", order=40,
        fields=(
            Field("enabled", "Range check on fleet chat", "bool", ("range_check", "enabled")),
            Field("keyword", "Keyword", "str", ("range_check", "keyword"), max_len=60),
        )),
    ConfigFieldsItem(
        id="settings.zkill_sound", label="zKill alert sound", order=50,
        fields=(Field("sound", "Alert sound", "enum", ("zkillboard", "alert_sound"),
                      choices=SOUND_KEYS),)),
    _MarketStagingItem(
        id="settings.market_staging", label="Market staging", order=60, sections=("Market",)),
    ConfigFieldsItem(
        id="settings.contract_scan", label="Contract scanning", order=70,
        fields=(
            Field("scan_contracts", "Scan contracts", "bool", (_MARKET, "scan_contracts")),
            Field("include_alliance_contracts", "Include corp/alliance contracts", "bool",
                  (_MARKET, "include_alliance_contracts")),
            Field("contracts_scope", "Contract scope", "enum", (_MARKET, "contracts_scope"),
                  choices=("system", "region")),
        )),
    _InfraScanItem(
        id="settings.infra_scan", label="Infrastructure scan", order=80),
)
