"""Centralized ESI HTTP constants.

This module holds the EVE Swagger Interface (ESI) base URL, the courtesy
User-Agent string, and the API compatibility date that were previously
duplicated (or, for the compat date, absent) across roughly eight separate
modules. Importing these from one place keeps them in sync everywhere instead
of drifting between copies.

Versioning (B4): ESI's old path-versioned ``/latest`` interface is frozen and
deprecated. The modern API is called at the UNVERSIONED root and pins behaviour
with an ``X-Compatibility-Date`` request header (see
``docs/eve_reference/esi_dev_portal.md``). ``ESI_COMPAT_DATE`` is that pinned,
tested date; it rides on every request via ``ESI_HEADERS`` / ``ESI_HEADERS_JSON``
(which module-level callers pass explicitly, and which the ``ESIAuth`` requests
session merges into its default headers at construction — so every authenticated
call carries it too). Bump it deliberately after retesting against a newer date
from ``/meta/compatibility-dates``.
"""

# Unversioned ESI root (no ``/latest``, no ``/vN``). Behaviour is pinned
# per-request by X-Compatibility-Date, not by the path.
ESI_BASE = "https://esi.evetech.net"
USER_AGENT = "FCTool/1.0 (EVE FC Assistant)"
# API behaviour date FCTool is written and tested against — a valid date from
# ESI's /meta/compatibility-dates (verified live 2026-07-14: 200 + ETag on
# /universe/*, /sovereignty/systems present). ESI floors an unchanged route's
# response to the oldest identical date, so a response may echo an EARLIER
# X-Compatibility-Date than requested — that is expected, not an error.
ESI_COMPAT_DATE = "2026-06-09"
ESI_HEADERS = {"User-Agent": USER_AGENT, "X-Compatibility-Date": ESI_COMPAT_DATE}
ESI_HEADERS_JSON = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "X-Compatibility-Date": ESI_COMPAT_DATE,
}
