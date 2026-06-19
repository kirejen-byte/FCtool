"""Centralized ESI HTTP constants.

This module holds the EVE Swagger Interface (ESI) base URL and the
User-Agent string that were previously duplicated across roughly eight
separate modules. Importing these from one place keeps the base URL and
the courtesy User-Agent (which CCP asks third-party apps to set) in sync
everywhere instead of drifting between copies.
"""

ESI_BASE = "https://esi.evetech.net/latest"
USER_AGENT = "FCTool/1.0 (EVE FC Assistant)"
ESI_HEADERS = {"User-Agent": USER_AGENT}
ESI_HEADERS_JSON = {"User-Agent": USER_AGENT, "Accept": "application/json"}
