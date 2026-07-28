"""Tagged chat-channel registry — the data layer behind Settings > Channels.

Design: Settings' "Intel Channels" box becomes "Channels", a single list of the
chat channels the owner cares about, where every entry carries zero or more
**tags**. Intelligence Fusion then tracks the ``Intel``-tagged subset instead of
its own private list, and the MOTD palette reads the same registry rather than a
second one. One list, many consumers, no drift.

Pure module: no Tkinter, no network, no threads, no disk. It takes the loaded
``config`` dict in and hands normalized data back; **persistence is the
caller's job** (see the save seam below).

Config shape
------------
The registry lives under its OWN top-level block — it deliberately does not
overload ``config["intel_channels"]``, whose two keys keep their existing
meanings (``tracked`` = the legacy intel list, ``cached_discovered`` = the
30-day discovery cache that backs the picker's suggestions)::

    config["channels"] = {
        "registry": [
            {"name": "Intel.WOMP", "tags": ["Intel"]},
            {"name": "Cap Chain", "tags": ["Cap chain", "Links"]},
        ]
    }

Precedence rule (load-bearing — read before changing anything here)
------------------------------------------------------------------
1. **If ``config["channels"]["registry"]`` is present and is a list, it is the
   SOURCE OF TRUTH.** ``config["intel_channels"]["tracked"]`` is not consulted
   at all — not merged, not unioned. A present-but-EMPTY registry is an honest
   empty answer (the owner removed every channel), never a trigger to re-seed.
2. **Otherwise the registry is MIGRATED in memory** from
   ``config["intel_channels"]["tracked"]``, every entry tagged ``Intel``. This
   also covers a corrupt block (``channels`` not a dict, ``registry`` not a
   list): recovering the owner's intel channels beats presenting an empty list.
3. **The loader NEVER mutates ``config``** — same rule as the v1→v2
   ``fittings.saved_motds`` template migration. The migrated form reaches disk
   only when something calls :func:`save`, i.e. on the first real user edit.
4. ``intel_channels.tracked`` is left alone by :func:`save`, deliberately: an
   install that writes a registry and is then rolled back still has its intel
   channels. Once the registry block exists it is the only thing consulted, so
   the stale key is inert. Retiring it is a separate, later decision.

Because of (2), :func:`intel_tagged` on an untouched config returns exactly the
list Intelligence Fusion tracks today — the migration is behaviour-preserving by
construction, and ``test_channel_store.py`` proves it.

Normalization (matches the house rules the GUI already applies)
--------------------------------------------------------------
* Names and tags are whitespace-stripped; empty ones are dropped.
* Names de-dupe **case-insensitively, first occurrence wins**, preserving that
  occurrence's casing and position (``fc_gui.normalize_tracked_channels`` /
  ``GapSelection`` precedent). EVE channel names are case-preserving but not
  case-distinct in practice, and a duplicate is always a data error.
* Tags de-dupe the same way within one channel, and keep their given order.
* A tag that case-insensitively matches one of :data:`PRELOADED_TAGS` is stored
  in that canonical casing, so ``intel`` and ``Intel`` cannot become two rows in
  a future tag picker. Any OTHER tag is preserved verbatim — the model stores
  arbitrary strings and the preloaded four are a starting vocabulary, not a
  closed enum.
* Tag lookups (:meth:`Registry.tagged`, :meth:`Registry.names`) are
  case-insensitive regardless.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The owner's starting tag vocabulary. NOT an enum — a channel may carry any
#: string, and may carry several tags at once (a cap-chain channel that is also
#: an intel channel is one entry with two tags). These four exist so the UI has
#: something to offer on a fresh install and so casing stays canonical.
PRELOADED_TAGS: tuple[str, ...] = ("Cap chain", "Logistics", "Links", "Intel")

#: The one tag Intelligence Fusion keys on. Never re-type the literal.
TAG_INTEL = "Intel"

#: Top-level config block owned by this module, and the key inside it.
CONFIG_KEY = "channels"
REGISTRY_KEY = "registry"

#: Legacy source consulted only by the migration (see the precedence rule).
_LEGACY_BLOCK = "intel_channels"
_LEGACY_TRACKED = "tracked"

_CANONICAL_TAGS = {t.casefold(): t for t in PRELOADED_TAGS}


def _clean(value) -> str:
    """Strip a value into a name/tag string; anything not a ``str`` gives "".

    Deliberately NOT ``str(value)``: a hand-edited or corrupt config entry of
    ``17`` is garbage, not a channel called "17", and coercing it would smuggle
    junk into a list the owner then has to clean out by hand. Names and tags are
    strings or they are nothing.
    """
    return value.strip() if isinstance(value, str) else ""


def canonical_tag(tag) -> str:
    """Return ``tag`` stripped, with preloaded-tag casing canonicalized.

    ``" intel "`` -> ``"Intel"``; ``"Cap Chain"`` -> ``"Cap chain"``. An unknown
    tag comes back stripped but otherwise verbatim. Empty/garbage -> ``""``.
    """
    clean = _clean(tag)
    if not clean:
        return ""
    return _CANONICAL_TAGS.get(clean.casefold(), clean)


def normalize_tags(tags) -> tuple[str, ...]:
    """Clean, canonicalize and case-insensitively de-dupe a tag sequence.

    Order is preserved (first occurrence wins). A string is treated as a single
    tag rather than as an iterable of characters — a caller passing ``"Intel"``
    means one tag, and silently exploding it into five would be absurd. Any
    other non-sequence yields ``()``. Never raises.
    """
    if tags is None:
        return ()
    if isinstance(tags, str):
        tags = (tags,)
    if not isinstance(tags, (list, tuple, set, frozenset)):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = canonical_tag(raw)
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return tuple(out)


@dataclass(frozen=True)
class Channel:
    """One registry entry: an exact channel name plus its tags.

    ``name`` keeps the owner's casing verbatim (EVE chat-log filenames are
    derived from it). ``tags`` is an immutable, order-stable tuple so a caller
    cannot mutate a registry entry behind the registry's back.
    """

    name: str
    tags: tuple[str, ...] = ()

    def has_tag(self, tag) -> bool:
        """Case-insensitive tag membership test. Never raises."""
        key = _clean(tag).casefold()
        if not key:
            return False
        return any(t.casefold() == key for t in self.tags)

    def to_dict(self) -> dict:
        """JSON-ready form, exactly as persisted under ``channels.registry``."""
        return {"name": self.name, "tags": list(self.tags)}


def _channel_from(raw) -> Channel | None:
    """Build a :class:`Channel` from one persisted entry, or None if unusable.

    Accepts both the dict form and a bare string (a name with no tags), so a
    hand-edited config that lists plain names still loads.
    """
    if isinstance(raw, dict):
        name = _clean(raw.get("name"))
        tags = normalize_tags(raw.get("tags"))
    else:
        name = _clean(raw)
        tags = ()
    if not name:
        return None
    return Channel(name=name, tags=tags)


@dataclass
class Registry:
    """The tagged channel list, in memory.

    Mutating methods change only this object — nothing is written anywhere until
    a caller hands it to :func:`save` and then persists ``config`` itself
    (``FCToolGUI._save_config``). That split mirrors the rest of the app: the
    store decides *what* the data is, the GUI decides *when* it hits disk.
    """

    _channels: list[Channel] = field(default_factory=list)
    #: True when this registry was seeded from ``intel_channels.tracked``
    #: because no ``channels`` block existed (or it was corrupt). Diagnostic —
    #: it lets a caller log/notice the one-time migration. It does NOT change
    #: any behaviour here, and it is not persisted.
    migrated: bool = False

    # ── reads ────────────────────────────────────────────────────────────────

    def channels(self) -> list[Channel]:
        """Every entry, in registry order. Returns a NEW list."""
        return list(self._channels)

    def tagged(self, tag) -> list[Channel]:
        """Entries carrying ``tag`` (case-insensitive), in registry order."""
        return [c for c in self._channels if c.has_tag(tag)]

    def names(self, tag=None) -> list[str]:
        """Channel NAMES — all of them, or only those carrying ``tag``.

        ``names()`` is the whole list; ``names("Intel")`` is what Intelligence
        Fusion tracks. Always a new list.
        """
        src = self._channels if tag is None else self.tagged(tag)
        return [c.name for c in src]

    def tags(self) -> list[str]:
        """Every tag in use across the registry, first-seen order, de-duped.

        Preloaded tags are NOT included unless a channel actually carries one —
        a caller wanting the full offer list unions this with
        :data:`PRELOADED_TAGS` itself.
        """
        out: list[str] = []
        seen: set[str] = set()
        for chan in self._channels:
            for tag in chan.tags:
                key = tag.casefold()
                if key not in seen:
                    seen.add(key)
                    out.append(tag)
        return out

    def has(self, name) -> bool:
        """Is ``name`` in the registry (case-insensitive)?"""
        return self._index_of(name) is not None

    def get(self, name) -> Channel | None:
        """The entry for ``name`` (case-insensitive), or None."""
        idx = self._index_of(name)
        return None if idx is None else self._channels[idx]

    def __len__(self) -> int:
        return len(self._channels)

    def __iter__(self):
        return iter(tuple(self._channels))

    # ── mutations ────────────────────────────────────────────────────────────

    def add(self, name, tags=()) -> bool:
        """Append a channel. Returns True if it was added.

        A blank name is refused (False). An existing name — matched
        case-insensitively — is left **completely untouched**, tags included,
        and False is returned: first occurrence wins, the same rule the loader
        applies to duplicate persisted entries. Use :meth:`set_tags` to change
        an existing channel's tags; that way "add" can never silently rewrite
        something the owner already curated.
        """
        clean = _clean(name)
        if not clean:
            return False
        if self._index_of(clean) is not None:
            return False
        self._channels.append(Channel(name=clean, tags=normalize_tags(tags)))
        return True

    def remove(self, name) -> bool:
        """Drop a channel by name (case-insensitive). True if one went."""
        idx = self._index_of(name)
        if idx is None:
            return False
        del self._channels[idx]
        return True

    def set_tags(self, name, tags) -> bool:
        """Replace a channel's tags wholesale. True if the channel existed.

        Position in the registry is preserved. ``tags`` is normalized like any
        other tag input, so passing ``[]`` clears the tags (a channel with no
        tags is legal — it is simply in no consumer's list).
        """
        idx = self._index_of(name)
        if idx is None:
            return False
        current = self._channels[idx]
        self._channels[idx] = Channel(name=current.name, tags=normalize_tags(tags))
        return True

    def rename(self, name, new_name) -> bool:
        """Rename a channel in place, keeping its tags and position.

        Refuses a blank new name, and refuses a name that would collide with a
        DIFFERENT existing entry (case-insensitive) — merging two channels is a
        decision for the caller, not a side effect of a rename. Re-casing an
        entry to itself is allowed and returns True.
        """
        idx = self._index_of(name)
        if idx is None:
            return False
        clean = _clean(new_name)
        if not clean:
            return False
        other = self._index_of(clean)
        if other is not None and other != idx:
            return False
        current = self._channels[idx]
        self._channels[idx] = Channel(name=clean, tags=current.tags)
        return True

    # ── serialization ────────────────────────────────────────────────────────

    def to_block(self) -> dict:
        """The ``config["channels"]`` block this registry represents."""
        return {REGISTRY_KEY: [c.to_dict() for c in self._channels]}

    # ── internals ────────────────────────────────────────────────────────────

    def _index_of(self, name):
        key = _clean(name).casefold()
        if not key:
            return None
        for i, chan in enumerate(self._channels):
            if chan.name.casefold() == key:
                return i
        return None


def _dedupe(channels) -> list[Channel]:
    """Case-insensitive de-dupe of parsed entries, first occurrence wins."""
    out: list[Channel] = []
    seen: set[str] = set()
    for chan in channels:
        key = chan.name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(chan)
    return out


def _legacy_tracked(config) -> list[str]:
    """``config["intel_channels"]["tracked"]``, cleaned. Never raises."""
    if not isinstance(config, dict):
        return []
    block = config.get(_LEGACY_BLOCK)
    if not isinstance(block, dict):
        return []
    raw = block.get(_LEGACY_TRACKED)
    if not isinstance(raw, (list, tuple)):
        return []
    return [n for n in (_clean(x) for x in raw) if n]


def load(config) -> Registry:
    """Build the :class:`Registry` for ``config``. Never mutates, never raises.

    See the module docstring's precedence rule: a present list registry wins
    outright; anything else migrates from ``intel_channels.tracked`` with the
    ``Intel`` tag and sets :attr:`Registry.migrated`.
    """
    block = config.get(CONFIG_KEY) if isinstance(config, dict) else None
    raw = block.get(REGISTRY_KEY) if isinstance(block, dict) else None

    if isinstance(raw, (list, tuple)):
        parsed = [c for c in (_channel_from(item) for item in raw) if c is not None]
        return Registry(_channels=_dedupe(parsed))

    seeded = [Channel(name=n, tags=(TAG_INTEL,)) for n in _legacy_tracked(config)]
    return Registry(_channels=_dedupe(seeded), migrated=True)


def save(config, registry: Registry) -> dict:
    """Write ``registry`` into ``config["channels"]`` and return that block.

    **This is the only mutation seam.** It does not touch disk — the caller
    persists ``config`` afterwards (``FCToolGUI._save_config``), exactly as the
    fittings/MOTD-template paths do. Unknown sibling keys already inside the
    ``channels`` block are preserved, so a future addition to that block is not
    erased by an older code path saving the registry. ``intel_channels`` is
    deliberately left untouched (precedence rule 4).
    """
    if not isinstance(config, dict):
        raise TypeError("config must be a dict")
    existing = config.get(CONFIG_KEY)
    block = dict(existing) if isinstance(existing, dict) else {}
    block.update(registry.to_block())
    config[CONFIG_KEY] = block
    return block


def intel_tagged(config) -> list[str]:
    """The exact channel list Intelligence Fusion should track, from ``config``.

    Convenience for the one consumer that must not have to know about tags. On a
    config that has never been touched by this module, the result is identical
    to today's ``intel_channels.tracked`` (post-normalization) — that parity is
    the migration's whole contract.
    """
    return load(config).names(TAG_INTEL)
