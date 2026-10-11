"""Ansiblex access: which stored jump gates the user's own alliance can use.

Game rule (2026-10): an Ansiblex jump gate is usable only by pilots in the
ALLIANCE that owns it. The infrastructure store records every scanned gate with
``owner_id`` = the owning CORPORATION, so this module answers "is that
corporation in my alliance?" and lets the host drop the gates of other
alliances from the map's bridge lines, the structure chips and all routing.

THE RULE IS FAIL-OPEN. A gate is hidden only when we KNOW it belongs to another
alliance: the user's own alliance is known, the gate has an owner, and that
owner's alliance has been resolved. Everything else is left exactly as it was --
gates with no ``owner_id`` (pasted or manual, a lane the user declared), gates
whose owner is not resolved yet, and everything while the user's own alliance
is unknown. An ESI outage must never silently drop the user's own gates from
routing.

Layers, top to bottom:

* pure functions -- :func:`is_foreign_gate` (the ONE rule), :func:`usable_entries`,
  :func:`bridge_inputs`, :func:`gate_owner_ids`, :func:`resolve_alliance`,
  :func:`own_alliance_of`;
* :class:`OwnerAllianceBook` -- the persisted ``{corporation: alliance}`` book
  (``gate_owner_alliances.json``), stale-while-revalidate, copy-on-write;
* :class:`AllianceResolver` -- owns the one worker thread that asks ESI;
* :class:`GateAccess` -- the facade the host holds as ``_gate_access``;
  :func:`of` hands back an inert one for a host that has none, so every call
  site is a one-liner that degrades to "nothing is foreign".

Standard library only at import time. ``infra_overlay`` / ``map_overlays`` /
``app_io`` / ``requests`` are imported inside the functions that need them; Tk
and ``fc_gui`` are never imported. The map and infra modules must not import
this one -- the host (``fc_gui``) is the only composer.

THREADS. Every read here (``usable`` / ``bridge_inputs`` / ``is_foreign``) is
safe from any thread: the book publishes a read-only mapping and every mutation
REBINDS a fresh one, so a reader holding a snapshot never sees it change. The
book is mutated on the Tk thread only (``GateAccess.apply``, reached through the
host's UI dispatcher). The resolver's worker touches no Tk and never mutates
the book; it posts one result batch back. ``kick_soon`` and ``nudge`` are the
two calls a worker may make towards the resolver: both only queue a ``kick``.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from types import MappingProxyType

log = logging.getLogger(__name__)

NO_ALLIANCE = 0                  # a corporation that is in no alliance
BOOK_VERSION = 1
REFRESH_AFTER_S = 24 * 3600      # a record older than this is due a re-fetch
NUDGE_EVERY_S = 600              # routing queues at most one census this often
# A failed owner is left alone this long, so a dead corporation cannot cost a
# fetch per kick. HALF a nudge period on purpose: this memory starts when a pass
# ENDS and the nudge clock when one STARTS, so a memory as long as the period
# made the next nudge land just inside it and a failed owner was retried only
# on every second nudge.
RETRY_FAILED_AFTER_S = 300

_EMPTY = MappingProxyType({})


def _entity_id(value):
    """A positive ``int`` id, or None. ``bool`` is not an id."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _alliance_value(value):
    """A stored alliance -- an ``int`` >= 0 (0 = none) -- or None for junk."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _aware_utc(moment):
    """``moment`` as an aware-UTC datetime; None means now; naive means UTC."""
    if moment is None:
        return datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _stamp_utc(at):
    """A persisted ``"at"`` stamp as an aware-UTC datetime, or None.

    FCTool's stamps are MIXED aware / naive, and comparing the two raises. A
    naive stamp is taken as UTC and any other offset is normalised BEFORE a
    comparison can see it. Missing or unparseable = None, which reads as "due".
    """
    if not isinstance(at, str) or not at:
        return None
    try:
        return _aware_utc(datetime.fromisoformat(at))
    except Exception:
        return None


# -- the rule ----------------------------------------------------------------

def is_foreign_gate(entry, owner_alliances, own_alliance) -> bool:
    """True when ``entry`` is a gate KNOWN to belong to another alliance.

    ``owner_alliances`` maps ``corporation id -> alliance id`` (0 = none) and
    holds only the owners that were resolved. ``own_alliance`` is None
    (unknown), 0 (known: the user is in no alliance) or the alliance id.

    Usable means ``owner alliance > 0 and owner alliance == own alliance``, so a
    resolved "in no alliance" owner is foreign, and a user in no alliance can
    use no owner-known gate. Never raises: anything odd is "not foreign".
    """
    if own_alliance is None or not owner_alliances:
        return False
    try:
        if entry.get("category") != "gate":
            return False
        owner = entry.get("owner_id")
        if owner is None:
            return False
        alliance = owner_alliances.get(owner)
        if alliance is None:
            return False
        return not (alliance > 0 and alliance == own_alliance)
    except Exception:
        return False


def _split(entries, owner_alliances, own_alliance):
    """``(usable, foreign)`` in one pass, input order kept.

    The category test is repeated here only so that the ~95% of records that
    are not gates cost one dict read; the verdict is :func:`is_foreign_gate`'s.
    """
    usable, foreign = [], []
    for entry in entries:
        if entry.get("category") == "gate" and is_foreign_gate(
                entry, owner_alliances, own_alliance):
            foreign.append(entry)
        else:
            usable.append(entry)
    return usable, foreign


def usable_entries(entries, owner_alliances, own_alliance) -> list:
    """``entries`` minus the foreign gates, as a new list in input order.

    One cheap pass, and no pass at all when nothing can be foreign (the own
    alliance is unknown, or no owner is resolved). Never raises.
    """
    try:
        entries = list(entries or ())
    except TypeError:
        return []
    if own_alliance is None or not owner_alliances:
        return entries
    try:
        return _split(entries, owner_alliances, own_alliance)[0]
    except Exception as exc:
        log.warning("gate access: filter failed (%s); nothing hidden", exc)
        return entries


def bridge_inputs(entries, owner_alliances, own_alliance):
    """``(usable_entries, blocked_id_pairs)`` for the two bridge chokes.

    ``blocked_id_pairs`` is a ``set`` of ``frozenset({system_id, system_id})``
    the caller must suppress from BOTH bridge sources -- the store gates and
    the manual config pairs -- exactly as a ``reinforced`` gate does. It is the
    id pairs of the live foreign gates MINUS the id pairs the usable gates
    corroborate: a stale foreign record must never suppress a pair one of our
    own gates also covers.

    Resolved the same pure, local way the chokes resolve names
    (``map_overlays.resolve_bridges``; no ESI, no Tk). No resolver work at all
    when there is no foreign gate. Never raises -- the callers are routing
    chokes that must degrade, not abort.
    """
    try:
        entries = list(entries or ())
    except TypeError:
        return [], set()
    if own_alliance is None or not owner_alliances:
        return entries, set()
    try:
        usable, foreign = _split(entries, owner_alliances, own_alliance)
    except Exception as exc:
        log.warning("gate access: filter failed (%s); nothing hidden", exc)
        return entries, set()
    if not foreign:
        return usable, set()
    try:
        import infra_overlay
        import map_overlays
        blocked = {frozenset(pair) for pair in map_overlays.resolve_bridges(
            infra_overlay.gate_pairs(foreign))}
        if blocked:
            blocked -= {frozenset(pair) for pair in map_overlays.resolve_bridges(
                infra_overlay.corroborated_gate_pairs(usable))}
    except Exception as exc:
        log.warning("gate access: pair resolve failed (%s); no pair blocked", exc)
        blocked = set()
    return usable, blocked


def gate_owner_ids(entries) -> set:
    """The distinct ``owner_id`` of every gate that is not ``dead`` -- the
    census the resolver works from."""
    owners = set()
    for entry in entries or ():
        try:
            if entry.get("category") != "gate" or entry.get("status") == "dead":
                continue
            owner = _entity_id(entry.get("owner_id"))
        except Exception:
            continue
        if owner is not None:
            owners.add(owner)
    return owners


def resolve_alliance(owner_id, get_fn):
    """The alliance of corporation ``owner_id``, asked through ``get_fn``.

    ``get_fn("/corporations/{id}/")`` returns the parsed body or None. The
    answer is the corporation's ``alliance_id``; :data:`NO_ALLIANCE` when the
    corporation was fetched and has none; None when the fetch failed or the
    body is not a corporation (unknown -- never recorded, so never hides a
    gate).
    """
    corp = get_fn(f"/corporations/{owner_id}/")
    if not isinstance(corp, dict) or not ("name" in corp or "ticker" in corp):
        return None
    alliance = corp.get("alliance_id")
    if alliance is None:
        return NO_ALLIANCE
    return _entity_id(alliance)


def own_alliance_of(standings):
    """The user's own alliance out of a ``StandingsCache``-shaped object.

    None = unknown (no object, or the own corporation is not resolved yet);
    :data:`NO_ALLIANCE` = known, and in no alliance; else the alliance id.
    Attribute reads only, so it is safe from any thread. The corporation is
    read FIRST: ``StandingsCache.refresh`` assigns the alliance before the
    corporation, so a known corporation always comes with its alliance.
    """
    try:
        if standings is None or not standings.own_identity_known:
            return None
        alliance = standings.own_alliance_id
    except Exception:
        return None
    if alliance is None:
        return NO_ALLIANCE
    return _entity_id(alliance)


# -- the persisted book --------------------------------------------------------

class OwnerAllianceBook:
    """``{corporation id: alliance id}`` for gate owners, kept on disk.

    File shape::

        {"version": 1,
         "owners": {"<corp_id>": {"alliance_id": <int >= 0>, "at": "<UTC ISO>"}}}

    A missing file is an empty book, silently. A corrupt, wrong-shape or
    junk-valued file or record is discarded with a log line; nothing here
    raises.

    STALE-WHILE-REVALIDATE: a record older than :data:`REFRESH_AFTER_S` is
    still USED and is merely :meth:`due` a re-fetch; a failed re-fetch keeps
    the old value. Nothing is ever dropped for age -- dropping it would show a
    gate we knew to be foreign again every time ESI had a bad day.

    Copy-on-write: :meth:`snapshot` is a read-only mapping and every mutation
    rebinds a new one, so worker threads may read while the Tk thread writes.
    """

    def __init__(self, path):
        self._path = path
        # {corp_id: (alliance_id, aware-UTC datetime | None)}; rebound, never
        # edited in place.
        self._records = {}
        self._values = _EMPTY

    def _publish(self, records):
        self._records = records
        self._values = MappingProxyType(
            {corp: record[0] for corp, record in records.items()})

    def snapshot(self):
        """The read-only ``{corp_id: alliance_id}`` mapping of known owners."""
        return self._values

    def load(self):
        try:
            with open(self._path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            self._publish({})
            return
        except Exception as exc:
            log.warning("gate owner alliances: %s is unreadable (%s); discarding",
                        self._path, exc)
            self._publish({})
            return
        owners = data.get("owners") if isinstance(data, dict) else None
        if (not isinstance(owners, dict)
                or data.get("version") != BOOK_VERSION):
            log.warning("gate owner alliances: %s is not a version-%d book; "
                        "discarding", self._path, BOOK_VERSION)
            self._publish({})
            return
        records, dropped = {}, 0
        for key, record in owners.items():
            try:
                corp = _entity_id(int(key))
            except (TypeError, ValueError):
                corp = None
            alliance = (_alliance_value(record.get("alliance_id"))
                        if isinstance(record, dict) else None)
            if corp is None or alliance is None:
                dropped += 1
                continue
            records[corp] = (alliance, _stamp_utc(record.get("at")))
        if dropped:
            log.warning("gate owner alliances: dropped %d junk record(s) from %s",
                        dropped, self._path)
        self._publish(records)

    def save(self) -> bool:
        """Write the book atomically. Best-effort: False (and a log line) on any
        failure, never an exception."""
        try:
            import app_io
            owners = {
                str(corp): {"alliance_id": alliance,
                            "at": (stamp.isoformat(timespec="seconds")
                                   if stamp is not None else "")}
                for corp, (alliance, stamp) in sorted(self._records.items())}
            app_io.atomic_write_json(
                self._path, {"version": BOOK_VERSION, "owners": owners},
                indent=None)
            return True
        except Exception as exc:
            log.warning("gate owner alliances: save failed (%s)", exc)
            return False

    def due(self, owner_ids, now=None) -> set:
        """The ``owner_ids`` to (re-)fetch: unknown, undated, older than
        :data:`REFRESH_AFTER_S`, or stamped in the FUTURE (clock skew, a hand
        edit -- such a record would otherwise never be due again). A due owner
        that is known is STILL used."""
        now = _aware_utc(now)
        records = self._records
        out = set()
        for corp in owner_ids or ():
            record = records.get(corp)
            if record is None or record[1] is None:
                out.add(corp)
                continue
            age = (now - record[1]).total_seconds()
            if age < 0 or age > REFRESH_AFTER_S:
                out.add(corp)
        return out

    def record(self, results, now=None) -> bool:
        """Fold ``{corp_id: alliance_id}`` answers in, stamped ``now``.

        Returns whether any VALUE changed or was newly learned -- a re-fetch
        with the same answer only renews its stamp. Junk (a failure's None, a
        negative, a non-int) is skipped, so a failed re-fetch keeps the old
        value.
        """
        if not isinstance(results, dict) or not results:
            return False
        stamp = _aware_utc(now)
        records = dict(self._records)
        wrote = changed = False
        for key, value in results.items():
            corp, alliance = _entity_id(key), _alliance_value(value)
            if corp is None or alliance is None:
                continue
            old = records.get(corp)
            if old is None or old[0] != alliance:
                changed = True
            records[corp] = (alliance, stamp)
            wrote = True
        if wrote:
            self._publish(records)
        return changed


# -- the resolver --------------------------------------------------------------

def _new_session():
    """One ``requests.Session`` for a pass, or None when it cannot be built
    (the fetch then opens its own connection per call)."""
    try:
        import requests
        return requests.Session()
    except Exception:
        return None


class AllianceResolver:
    """Asks ESI for the alliance of each due owner on ONE daemon thread.

    ``get_fn(path, session=None)`` is the host's public ESI GET (parsed body or
    None). The worker reuses one session for the pass, touches no Tk, never
    mutates the book, and posts ONE batch back through ``post_ui``; the
    Tk-thread half then hands the successes to ``on_results``.

    One pass at a time. A thread that fails to start resets the in-flight flag
    (a stranded flag would wedge the resolver for the session). An owner whose
    fetch failed is left alone for :data:`RETRY_FAILED_AFTER_S`, counted from
    the END of the pass that failed -- memory for this session only -- so a
    dead corporation cannot cost a fetch per kick. Any kick after that retries
    it; nothing here retries by itself.

    A kick REFUSED because a pass was running is remembered: when that pass
    has been applied and the flag is down, ``follow_up()`` runs ONCE (the
    facade's census), so an owner that arrived mid-pass still gets fetched. It
    ends by itself -- the census finds the answers recorded and the failures
    still inside their memory, and starts nothing.
    """

    def __init__(self, get_fn, post_ui, on_results, *, follow_up=None,
                 session_factory=None, clock=time.monotonic,
                 thread_factory=threading.Thread):
        self._get_fn = get_fn
        self._post_ui = post_ui
        self._on_results = on_results
        self._follow_up = follow_up
        self._session_factory = session_factory or _new_session
        self._clock = clock
        self._thread_factory = thread_factory
        self._lock = threading.Lock()
        self._in_flight = False
        self._refused = False            # a kick was turned away by this pass
        self._failed_at = {}             # {corp_id: clock()}; rebound, not edited

    @property
    def in_flight(self) -> bool:
        return self._in_flight

    def kick(self, owner_ids) -> bool:
        """Start one pass over ``owner_ids``. False when there is nothing to
        fetch, a pass is already running (remembered -- see the class note),
        or the thread would not start."""
        if not owner_ids:
            return False
        failed, now = self._failed_at, self._clock()
        todo = sorted(owner for owner in owner_ids
                      if owner not in failed
                      or now - failed[owner] >= RETRY_FAILED_AFTER_S)
        if not todo:
            return False
        with self._lock:
            if self._in_flight:
                self._refused = True
                return False
            self._in_flight = True
            self._refused = False
        try:
            self._thread_factory(target=self._run, args=(todo,),
                                 daemon=True).start()
        except Exception:
            log.warning("gate access: resolver thread failed to start",
                        exc_info=True)
            self._in_flight = False
            return False
        return True

    def _run(self, todo):
        """WORKER THREAD. Fetch, then post one batch. No Tk, no book."""
        results, failed = {}, []
        session = None
        try:
            session = self._session_factory()

            def fetch(path):
                return self._get_fn(path, session=session)

            for owner in todo:
                try:
                    alliance = resolve_alliance(owner, fetch)
                except Exception:
                    alliance = None
                if alliance is None:
                    failed.append(owner)
                else:
                    results[owner] = alliance
        except Exception:
            log.warning("gate access: resolve pass failed", exc_info=True)
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            try:
                self._post_ui(self._finish, results, failed)
            except Exception:
                log.warning("gate access: could not post the results",
                            exc_info=True)
                self._in_flight = False

    def _finish(self, results, failed):
        """TK THREAD (through ``post_ui``). The flag is lowered LAST and in a
        ``finally``, so "not in flight" means applied, and nothing strands it.

        Only THEN, and only when this pass turned a kick away, does the one
        follow-up census run. The host's re-push kicks from inside the apply,
        so any change can bring a refusal; the census is cheap (one store
        read), re-pushes nothing, and may start the next pass because the flag
        is already down.
        """
        refused = False
        try:
            now = self._clock()
            memory = {owner: at for owner, at in self._failed_at.items()
                      if owner not in results
                      and now - at < RETRY_FAILED_AFTER_S}
            for owner in failed:
                memory[owner] = now
            self._failed_at = memory
            if results:
                self._on_results(results)
        except Exception:
            log.warning("gate access: applying the results failed",
                        exc_info=True)
        finally:
            # Lowered and read under the lock kick() raises them under, so a
            # kick lands wholly before (refused, and followed up below) or
            # wholly after (it starts its own pass and clears the mark).
            # _refused is reset by the next pass's start, so it is only read.
            with self._lock:
                self._in_flight = False
                refused = self._refused
        if refused and self._follow_up is not None:
            try:
                self._follow_up()
            except Exception:
                log.warning("gate access: the follow-up census failed",
                            exc_info=True)


# -- the facade ----------------------------------------------------------------

class GateAccess:
    """What the host holds as ``_gate_access``.

    Built with nothing it is INERT -- nothing is foreign, nothing is fetched --
    which is what :func:`of` returns for a host that has none.

    ``own_alliance_fn()`` -> None / 0 / alliance id (read at call time);
    ``store_fn()`` -> the infrastructure store or None; ``post_ui(fn, *args)``
    queues onto the Tk thread; ``on_changed()`` runs on the Tk thread when a
    resolved answer changed what is foreign (the host re-pushes the map).
    """

    def __init__(self, book=None, resolver=None, *, own_alliance_fn=None,
                 store_fn=None, post_ui=None, on_changed=None,
                 clock=time.monotonic):
        self._book = book
        self._resolver = resolver
        self._own_alliance_fn = own_alliance_fn
        self._store_fn = store_fn
        self._post_ui = post_ui
        self._on_changed = on_changed
        self._clock = clock
        self._nudge_not_before = float("-inf")   # clock() of the next nudge

    # -- reads: any thread, never raise --

    def own_alliance(self):
        fn = self._own_alliance_fn
        if fn is None:
            return None
        try:
            return _alliance_value(fn())
        except Exception:
            return None

    def owner_alliances(self):
        book = self._book
        return book.snapshot() if book is not None else _EMPTY

    def is_foreign(self, entry) -> bool:
        owners = self.owner_alliances()
        return bool(owners) and is_foreign_gate(
            entry, owners, self.own_alliance())

    def usable(self, entries) -> list:
        owners = self.owner_alliances()
        return usable_entries(
            entries, owners, self.own_alliance() if owners else None)

    def bridge_inputs(self, entries):
        owners = self.owner_alliances()
        return bridge_inputs(
            entries, owners, self.own_alliance() if owners else None)

    # -- the resolver's side --

    def kick(self, entries=None) -> bool:
        """Resolve the gate owners that are due. ``entries`` is a store
        snapshot the caller already holds; omitted, the store is read here. A
        clean census is a no-op. Never raises."""
        book, resolver = self._book, self._resolver
        if book is None or resolver is None:
            return False
        try:
            if entries is None:
                store = self._store_fn() if self._store_fn is not None else None
                if store is None:
                    return False
                entries = store.entries_view()
            owners = gate_owner_ids(entries)
            due = book.due(owners) if owners else None
            return resolver.kick(due) if due else False
        except Exception as exc:
            log.warning("gate access: kick failed (%s)", exc)
            return False

    def kick_soon(self):
        """:meth:`kick`, queued onto the Tk thread. Safe from any thread."""
        if self._resolver is None or self._post_ui is None:
            return
        try:
            self._post_ui(self.kick)
        except Exception as exc:
            log.warning("gate access: could not queue a kick (%s)", exc)

    def nudge(self):
        """Routing activity keeps the book fresh: queue one :meth:`kick` onto
        the Tk thread, at most once per :data:`NUDGE_EVERY_S`.

        For the session in which nothing else kicks -- the first pass failed
        (ESI down at start), the map is never opened, no scan runs -- and for
        the 24-hour refresh in a session that long. The routing choke calls
        this on WORKER threads, so it only queues: no Tk, no thread, no store
        read here. The fast path is one clock read and one compare.

        A failed owner is retried by the NEXT nudge, so within about one nudge
        period of routing activity: its memory (:data:`RETRY_FAILED_AFTER_S`,
        counted from the end of the failed pass) is over by then. Only a pass
        that itself ran longer than ``NUDGE_EVERY_S - RETRY_FAILED_AFTER_S``
        pushes the retry to the nudge after. A nudge is a route lookup, not a
        timer: with no route lookup and no other kick (a map push, a scan, a
        standings refresh) nothing retries.

        Safe from any thread and never raises. Two threads racing past the
        compare can both queue; a kick is census-guarded, so that is harmless.
        """
        if self._resolver is None or self._post_ui is None:
            return
        try:
            now = self._clock()
            if now < self._nudge_not_before:
                return
            self._nudge_not_before = now + NUDGE_EVERY_S
            self._post_ui(self.kick)
        except Exception as exc:
            log.warning("gate access: could not queue a nudge (%s)", exc)

    def apply(self, results) -> bool:
        """TK THREAD. Record one batch of answers and save; when a value
        changed, tell the host. Returns whether anything changed."""
        book = self._book
        if book is None or not results:
            return False
        changed = book.record(results)
        book.save()
        if changed:
            self._notify()
        return changed

    def own_changed(self):
        """TK THREAD. The user's own alliance may have changed (a standings
        refresh finished): re-push, then resolve whatever is due."""
        if self._book is None:
            return
        self._notify()
        self.kick()

    def _notify(self):
        fn = self._on_changed
        if fn is None:
            return
        try:
            fn()
        except Exception as exc:
            log.warning("gate access: change hook failed (%s)", exc)


_INERT = GateAccess()


def of(host) -> GateAccess:
    """``host._gate_access``, or an inert facade when the host has none (a bare
    test host, or a call that lands before ``__init__`` built it)."""
    return getattr(host, "_gate_access", None) or _INERT


def build(path, *, own_alliance_fn, store_fn, get_fn, post_ui, on_changed=None,
          **resolver_options) -> GateAccess:
    """The working stack over the book at ``path`` (loaded here).

    ``resolver_options`` go to :class:`AllianceResolver`; a ``clock`` among
    them also times the facade's :meth:`GateAccess.nudge`. The resolver's
    follow-up census is the facade's own :meth:`GateAccess.kick`.
    """
    book = OwnerAllianceBook(path)
    book.load()
    access = GateAccess(book, own_alliance_fn=own_alliance_fn, store_fn=store_fn,
                        post_ui=post_ui, on_changed=on_changed,
                        clock=resolver_options.get("clock", time.monotonic))
    access._resolver = AllianceResolver(get_fn, post_ui, access.apply,
                                        follow_up=access.kick,
                                        **resolver_options)
    return access
