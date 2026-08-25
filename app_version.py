"""The app's own version, and the one way to turn a release tag into numbers.

``APP_VERSION`` is the version that is CURRENTLY RELEASED on GitHub — the
release cycle bumps it in the same commit that bumps everything else, and the
packaging battery gates on it matching the version being built. Keep this
module trivially greppable: one plain string assignment and one small helper,
no imports beyond the stdlib, nothing computed.

``parse_version`` is the shared parser for both sides of the update comparison
(our own constant and whatever tag GitHub hands back). It is deliberately
STRICT and deliberately TOTAL:

* strict — a pre-release or build-metadata suffix (``v5.1.0-rc1``,
  ``5.1.0+build7``) is NOT a version here, it is ``None``. GitHub's
  ``/releases/latest`` already excludes drafts and pre-releases, so a suffixed
  tag reaching us means something unexpected happened; the fail-silent
  direction for an update *advertisement* is to say nothing at all rather than
  to guess at ordering.
* total — it never raises, for any input, including non-strings. A version
  string is untrusted remote data on one side of that comparison, and a
  traceback out of a background thread is a far worse outcome than a missed
  update notice.
"""
from __future__ import annotations

# The currently RELEASED version. Bumped by the release cycle (and gated by the
# packaging battery), never by a feature commit.
APP_VERSION = "5.1.0"


def parse_version(tag) -> tuple | None:
    """Return ``tag`` as a tuple of ints, or ``None`` if it is not a version.

    Accepts an optional leading ``v``/``V`` and surrounding whitespace, then
    one or more dot-separated runs of digits::

        parse_version("v5.1.0")   -> (5, 1, 0)
        parse_version("5.1")      -> (5, 1)
        parse_version("v5.1.0rc") -> None
        parse_version(None)       -> None

    Never raises. Note the tuple length follows the tag, so callers comparing
    two of these must zero-pad to a common length first (``"5.1"`` and
    ``"5.1.0"`` are the same version) — ``update_check.check`` does.
    """
    try:
        if not isinstance(tag, str):
            return None
        text = tag.strip()
        if text[:1] in ("v", "V"):
            text = text[1:]
        if not text:
            return None
        parts = text.split(".")
        # str.isdigit() is True for unicode digit forms too (e.g. superscripts),
        # which int() would then reject or mis-read -- pin it to plain ASCII.
        if not all(p and all(c in "0123456789" for c in p) for p in parts):
            return None
        return tuple(int(p) for p in parts)
    except Exception:
        return None
