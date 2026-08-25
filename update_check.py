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


class UpdateInfo(NamedTuple):
    """A release that is strictly newer than the running one."""

    tag: str
    url: str


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


def fetch_latest(timeout_s: float = 6) -> dict | None:
    """Ask GitHub for this repo's latest release.

    Returns ``{"tag": <tag_name>, "url": <html_url>}``, or ``None`` on ANY
    failure: a network error, a non-200 (including a 403 rate-limit), a body
    that is not JSON, or a payload without a usable ``tag_name``.

    A release whose payload carries no usable ``html_url`` still returns a
    result, pointed at :data:`RELEASES_PAGE` — the canonical page resolves to
    that same release, so a missing link is no reason to withhold the notice.
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
        return {"tag": tag.strip(), "url": url.strip()}
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
    return UpdateInfo(tag, url)
