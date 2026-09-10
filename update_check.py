"""Update awareness — is there a newer FCTool release on GitHub?

The repository IS the distribution channel: users download a zip from GitHub
Releases, so nothing tells them a new one exists unless the app looks. This
module is that look, and nothing more — it never downloads, never installs and
never writes anything to disk.

Shape
-----
* :func:`fetch_latest` — the ONE HTTP call: an unauthenticated GET of
  ``/repos/kirejen-byte/FCtool/releases/latest``. No token, no ``gh``, no
  scopes. GitHub's unauthenticated limit is 60 requests/hour/IP; the app's
  cadence is roughly two calls a DAY, so the limit is three orders of magnitude
  away — and a 403 rate-limit answer is treated as "no answer" anyway.
* :func:`check` — the pure comparison. Injectable ``fetch`` so tests (and any
  future caller) run entirely offline.
* :func:`select_asset` — the pure pick of the build zip out of a release's
  attachments, so a caller that DOES install (``self_update``) is handed a
  name, a size, a URL and a checksum rather than having to re-read the API.
  This module still downloads nothing and writes nothing.

Fail-silent is a hard requirement, not a nicety
-----------------------------------------------
Every failure mode — no network, DNS down, GitHub 5xx, rate limited, garbage
JSON, a tag shape we don't understand — resolves to ``None``, which the caller
renders as "no update available". There is no dialog, no warning-level log
line and no status text: a user flying a fleet must never be interrupted
because a courtesy version check could not reach github.com. At most one
throttled DEBUG line per hour records that something failed, so a genuinely
broken check is still diagnosable from fctool.log without ever becoming noise.

Comparison is zero-padded (see :func:`app_version.parse_version`), so ``5.1``
and ``5.1.0`` are the same version, and STRICTLY newer wins: equal, older, or
either side unparseable all mean "say nothing".
"""
from __future__ import annotations

import re
import time
from typing import NamedTuple

import requests

from app_log import get_logger
from app_version import APP_VERSION, parse_version

log = get_logger(__name__)

# The public releases API for this repo, and the human page a user is sent to
# when a release carries no html_url of its own. Both resolve to the same
# release; /releases/latest excludes drafts and pre-releases by definition.
RELEASES_API = "https://api.github.com/repos/kirejen-byte/FCtool/releases/latest"
RELEASES_PAGE = "https://github.com/kirejen-byte/FCtool/releases/latest"

# GitHub REJECTS an API request with no User-Agent (403), so this is required,
# not courtesy. Carrying APP_VERSION also makes the call self-describing in
# GitHub's logs. Deliberately NOT esi_constants.USER_AGENT: that string is
# pinned to "FCTool/1.0" for ESI and has nothing to do with this call.
USER_AGENT = f"FCTool/{APP_VERSION} (+https://github.com/kirejen-byte/FCtool)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}

# One DEBUG line per hour at most. A box that is simply offline would otherwise
# write a line every twelve hours forever, which is fine, but a caller polling
# harder (or a future retry) must not be able to turn fctool.log into a diary.
_LOG_THROTTLE_S = 3600.0
_last_log_ts = 0.0

# The build zip attached to every release: "FCTool5.6.1.zip" — the version
# WITHOUT a leading v (the tag has one, the asset does not). Matched
# case-insensitively because the name is typed by hand at release time.
ASSET_RE = re.compile(r"^FCTool[0-9.]+\.zip$", re.IGNORECASE)


class AssetInfo(NamedTuple):
    """One file attached to a GitHub release.

    ``digest`` is GitHub's server-computed checksum VERBATIM — normally
    ``"sha256:<64 hex>"``, ``""`` when the release predates the field or
    carries something unusable. Judging it is the downloader's job, not this
    module's: everything here stays a courtesy lookup that writes no disk.
    """

    name: str
    size: int
    url: str
    digest: str


class UpdateInfo(NamedTuple):
    """A release that is strictly newer than the running one.

    ``notes`` (the release body markdown) and ``asset`` (the build zip, when
    the release attaches exactly one recognisable candidate) are DEFAULTED:
    the pre-self-update two-argument construction still works everywhere, and
    a release that carries neither is still worth announcing — the caller
    degrades to "open the release page".
    """

    tag: str
    url: str
    notes: str = ""
    asset: AssetInfo | None = None


def _quiet_log(message: str) -> None:
    """Record ``message`` at DEBUG, at most once an hour. Never raises."""
    global _last_log_ts
    try:
        now = time.monotonic()
        if now - _last_log_ts < _LOG_THROTTLE_S:
            return
        _last_log_ts = now
        log.debug("update check: %s", message)
    except Exception:
        pass


def _parse_assets(raw) -> list[AssetInfo]:
    """Turn GitHub's ``assets[]`` into :class:`AssetInfo` values.

    Every entry is judged on its own: one malformed asset must never cost the
    user the whole release notice, so a bad entry is dropped silently and the
    rest survive. A non-list ``raw`` yields an empty list.

    An entry is kept only with a non-blank string ``name``, a real ``int``
    ``size`` (``bool`` is deliberately excluded — ``True`` is an ``int`` in
    Python and a nonsense byte count) and a non-blank string
    ``browser_download_url``. Range checks on the size belong to the
    downloader's preflight, not here.
    """
    out: list[AssetInfo] = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        try:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            size = entry.get("size")
            url = entry.get("browser_download_url")
            if not isinstance(name, str) or not name.strip():
                continue
            if not isinstance(size, int) or isinstance(size, bool):
                continue
            if not isinstance(url, str) or not url.strip():
                continue
            digest = entry.get("digest")
            digest = digest.strip() if isinstance(digest, str) else ""
            out.append(AssetInfo(name.strip(), size, url.strip(), digest))
        except Exception:
            continue
    return out


def _asset_name(entry) -> str | None:
    """The ``name`` of one candidate asset, or ``None`` when there isn't one.

    Duck-typed and exception-proof on purpose: :func:`select_asset` is a
    public, total function, so it must survive being handed a list of
    whatever — including an object whose attribute access raises.
    """
    try:
        name = getattr(entry, "name", None)
    except Exception:
        return None
    return name if isinstance(name, str) and name.strip() else None


def select_asset(assets, tag) -> AssetInfo | None:
    """Pick the build zip for ``tag`` out of a release's assets.

    Two rules, in order:

    1. **Exact wins.** ``FCTool<ver>.zip`` where ``ver`` is ``tag`` without
       its leading ``v`` — the name the release cycle actually produces.
       Compared case-insensitively, since it is typed by hand.
    2. **Otherwise, one candidate or none.** The SINGLE asset matching
       :data:`ASSET_RE` is taken; zero matches and several matches both mean
       ``None``, because guessing which of two builds a user wants is exactly
       the mistake that turns a courtesy feature into a broken install. The
       caller then degrades to "open the release page".

    Total: any input at all (a non-list, garbage entries, an unusable tag)
    yields ``None`` or a match, never an exception.
    """
    try:
        if not isinstance(assets, list):
            return None
        candidates = [(a, _asset_name(a)) for a in assets]
        candidates = [(a, n) for a, n in candidates if n]

        if isinstance(tag, str) and tag.strip():
            # ONE leading v, exactly as app_version.parse_version drops it.
            ver = tag.strip()
            ver = ver[1:] if ver[:1] in ("v", "V") else ver
            want = f"FCTool{ver}.zip".lower()
            for asset, name in candidates:
                if name.strip().lower() == want:
                    return asset

        matches = [a for a, n in candidates if ASSET_RE.match(n.strip())]
        return matches[0] if len(matches) == 1 else None
    except Exception as exc:
        _quiet_log(f"select_asset {type(exc).__name__}: {exc}")
        return None


def fetch_latest(timeout_s: float = 6) -> dict | None:
    """Ask GitHub for this repo's latest release.

    Returns ``{"tag": <tag_name>, "url": <html_url>}``, or ``None`` on ANY
    failure: a network error, a non-200 (including a 403 rate-limit), a body
    that is not JSON, or a payload without a usable ``tag_name``.

    A release whose payload carries no usable ``html_url`` still returns a
    result, pointed at :data:`RELEASES_PAGE` — the canonical page resolves to
    that same release, so a missing link is no reason to withhold the notice.

    The result also carries ``"body"`` (the release notes) and ``"assets"``
    (a ``list[AssetInfo]``) — but ONLY when the release actually supplies
    them; a bare release omits both keys rather than including them empty.
    Callers must read them with ``.get()``.
    """
    try:
        resp = requests.get(RELEASES_API, headers=dict(HEADERS), timeout=timeout_s)
        if getattr(resp, "status_code", None) != 200:
            _quiet_log(f"HTTP {getattr(resp, 'status_code', '?')}")
            return None
        payload = resp.json()
        if not isinstance(payload, dict):
            _quiet_log("unexpected payload shape")
            return None
        tag = payload.get("tag_name")
        if not isinstance(tag, str) or not tag.strip():
            _quiet_log("release carries no tag_name")
            return None
        url = payload.get("html_url")
        if not isinstance(url, str) or not url.strip():
            url = RELEASES_PAGE
        result = {"tag": tag.strip(), "url": url.strip()}

        # ADDITIVE, and only when the release actually carries the data: a
        # bare release still returns the exact two-key dict this function
        # always returned. check() reads both through .get(), so an absent
        # key and an empty one mean the same thing downstream.
        body = payload.get("body")
        if isinstance(body, str) and body.strip():
            result["body"] = body
        assets = _parse_assets(payload.get("assets"))
        if assets:
            result["assets"] = assets
        return result
    except Exception as exc:
        _quiet_log(f"{type(exc).__name__}: {exc}")
        return None


def check(current: str = APP_VERSION, fetch=fetch_latest) -> UpdateInfo | None:
    """Return the newer release as an :class:`UpdateInfo`, or ``None``.

    ``None`` covers every "say nothing" case: the fetch found nothing, the
    remote tag or ``current`` will not parse, or the remote is the same as (or
    older than) what is running. Pure apart from the injected ``fetch`` — and
    a ``fetch`` that raises is treated as one that found nothing, so a caller
    on a background thread cannot be handed an exception.
    """
    try:
        result = fetch()
    except Exception as exc:
        _quiet_log(f"fetch raised {type(exc).__name__}: {exc}")
        return None
    if not isinstance(result, dict):
        return None

    tag = result.get("tag")
    url = result.get("url")
    if not isinstance(tag, str) or not isinstance(url, str) or not tag or not url:
        return None

    remote = parse_version(tag)
    mine = parse_version(current)
    if remote is None or mine is None:
        return None

    # Zero-pad to a common length before comparing: "5.1" and "5.1.0" are the
    # same version, but the bare tuples (5, 1) and (5, 1, 0) are not.
    width = max(len(remote), len(mine))
    remote += (0,) * (width - len(remote))
    mine += (0,) * (width - len(mine))
    if remote <= mine:
        return None

    # Optional extras. A fetch that predates them (or a caller's stub that
    # returns only tag/url) simply leaves the defaults in place.
    notes = result.get("body")
    if not isinstance(notes, str):
        notes = ""
    return UpdateInfo(tag, url, notes, select_asset(result.get("assets"), tag))
