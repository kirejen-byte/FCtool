"""
intel_resolver — async pilot-name -> standing resolver with an in-memory cache.

Answers cached names instantly; enqueues unknowns to a single daemon worker
thread that batches them into resolve_fn calls (default: resolve_names). De-dupes
in-flight names. Never blocks the caller; never raises into the UI.
"""

from __future__ import annotations

import queue
import threading
from collections import OrderedDict
from typing import Callable

from intel_monitor import Resolution, resolve_names
from app_log import get_logger

log = get_logger(__name__)


class IntelResolver:
    def __init__(
        self,
        resolve_fn: Callable[[list[str], set, set], list[Resolution]] | None = None,
        friendly: set[int] | None = None,
        hostile: set[int] | None = None,
        cache_cap: int = 5000,
    ):
        self._resolve_fn = resolve_fn or resolve_names
        self.friendly = friendly if friendly is not None else set()
        self.hostile = hostile if hostile is not None else set()
        self._cache_cap = cache_cap
        self._cache: "OrderedDict[str, Resolution]" = OrderedDict()
        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self._queue: "queue.Queue[tuple[list[str], Callable]]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()

    # ── cache ────────────────────────────────────────────────────────────
    def _cache_put(self, res: Resolution) -> None:
        key = res.name.lower()
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = res
            while len(self._cache) > self._cache_cap:
                self._cache.popitem(last=False)  # evict oldest

    def lookup_cached(self, name: str) -> Resolution | None:
        with self._lock:
            return self._cache.get(name.lower())

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put((None, None))  # unblock the worker

    # ── request ──────────────────────────────────────────────────────────
    def request(self, names: list[str],
                on_resolved: Callable[[dict[str, Resolution]], None]) -> None:
        """Answer cached names immediately via on_resolved; enqueue the rest to
        the worker. De-dupes in-flight names. on_resolved is invoked with a
        dict[name -> Resolution] for whatever subset is known/becomes known."""
        cached: dict[str, Resolution] = {}
        to_fetch: list[str] = []
        with self._lock:
            for n in names:
                hit = self._cache.get(n.lower())
                if hit is not None:
                    cached[n] = hit
                elif n.lower() in self._inflight:
                    continue
                else:
                    self._inflight.add(n.lower())
                    to_fetch.append(n)
        if cached:
            try:
                on_resolved(cached)
            except Exception:
                log.exception("IntelResolver: cached on_resolved callback failed")
        if to_fetch:
            self._queue.put((to_fetch, on_resolved))

    # ── worker ───────────────────────────────────────────────────────────
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                batch, cb = self._queue.get()
            except Exception:
                continue
            if batch is None:  # stop sentinel
                break
            try:
                resolutions = self._resolve_fn(batch, self.friendly, self.hostile)
            except Exception:
                log.exception("IntelResolver: resolve_fn failed for %d names",
                              len(batch))
                resolutions = []
            by_name = {r.name: r for r in resolutions}
            out: dict[str, Resolution] = {}
            for n in batch:
                r = by_name.get(n)
                if r is not None:
                    self._cache_put(r)
                    out[n] = r
            with self._lock:
                for n in batch:
                    self._inflight.discard(n.lower())
            if out and cb is not None:
                try:
                    cb(out)
                except Exception:
                    log.exception("IntelResolver: on_resolved callback failed")
