"""Build the bundled emoji pack: one sprite sheet + one lookup table.

The ping editor draws Discord's standard emoji from ``assets/emoji/twemoji_22.png``
and looks them up in ``emoji_table.json.gz``.  This DEV-TIME tool builds both from
three pinned public inputs; the application itself never downloads anything and
never imports this file (or Pillow).

Inputs (pinned -- nothing else is ever requested):
  * Twemoji 16.0.1 72x72 PNGs (jdecked/twemoji, tag v16.0.1; CC-BY 4.0), fetched
    one file at a time from the jsDelivr mirror of that tag;
  * the npm package ``discord-emoji`` 2.5.3 (MIT) -- the shortcode names;
  * Unicode ``emoji-test.txt`` 16.0 (Unicode License v3) -- the emoji set.

The on-disk contract (read by ``emoji_data.py`` and the ping editor):

    emoji_table.json.gz   (gzip, mtime 0, UTF-8 JSON, keys sorted)
    { "version": 1, "emoji_version": "16.0",
      "tile_px": 22, "cell_px": 24, "cols": 62,
      "count": 3807,          # number of emoji entries
      "tiles": 3807,          # number of tiles in the sheet
      "emoji": [ [seq, name, [shortcode, ...], group, tile], ... ],
      "alt":   { "<non-fully-qualified sequence>": position in "emoji", ... } }

    assets/emoji/twemoji_22.png
      grid of ``cell_px`` cells, ``cols`` columns; tile t occupies the ``tile_px``
      square whose top-left is (1 + (t % cols) * cell_px, 1 + (t // cols) * cell_px);
      everything else is fully transparent.

``tile`` is explicit per entry.  In the normal build it equals the entry's
position; in the no-skin-tone fallback a toned entry carries its BASE emoji's
tile and ``tiles < count`` -- readers use ``tile``, never the list position.

Downloaded data is UNTRUSTED.  The cache holds downloaded files only; archives
are read member by member in memory and are never unpacked to disk; nothing that
was fetched is ever run.  Run the tool in Python's isolated mode whenever it
fetches or reads the cache -- which is why this is ONE self-contained file
(stdlib + Pillow, no sibling imports):

    py -3.12 -I tools/build_emoji_pack.py --plan     # print each input's URL and size
    py -3.12 -I tools/build_emoji_pack.py --fetch    # download into tools/_cache/emoji/
    py -3.12 -I tools/build_emoji_pack.py --build [--palette 256|rgba] [--out DIR]
                                          [--no-skin-tones]   # the over-budget fallback
                                          [--strict]          # refuse changed inputs
    py -3.12 -I tools/build_emoji_pack.py --compare <out.png>

SHEET ENCODING -- part of the contract.  The sheet is ALWAYS an 8-bit palette PNG
(colour type 3) with ``PLTE`` + ``tRNS``, not interlaced, EVERY scanline filter
type 0; ``write_palette_png`` writes it and ``tests/test_emoji_pack.py`` pins it.
Why (measured 2026-10-09, Tk 8.6.15): handing the WHOLE sheet to ``tk.PhotoImage``
takes 68-108 SECONDS.  Tk keeps a "valid region" for a photo that has fully
transparent pixels, the sheet has about 150,000 separate runs of them, and every
decoded row re-combines that region (the same pixels with alpha 1 instead of 0
decode in 98 ms).  So the reader never gives Tk the sheet: it inflates the rows
once with ``zlib`` and cuts each tile into its own tiny PNG -- and filter 0 is
what makes that cut a plain slice of the inflated bytes.

``--plan`` makes metadata requests only (a file listing, a package manifest, two
HEAD requests): it downloads no image, archive or data file.

The cache (``--cache DIR``, default ``tools/_cache/emoji/``, gitignored) holds
about 3,800 small files.  When the checkout lives in a cloud-synced folder, point
``--cache`` at a directory OUTSIDE it, or the sync client uploads every one.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import http.client
import io
import json
import os
import re
import ssl
import sys
import tarfile
import textwrap
import threading
import time
import zlib
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlsplit

TOOL_VERSION = 1

REPO_ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = REPO_ROOT / "tools" / "_cache" / "emoji"
# the pack's four files, relative to the repository root (or to --out)
REL_SHEET = "assets/emoji/twemoji_22.png"
REL_NOTICE = "assets/emoji/NOTICE.txt"
REL_SOURCE = "assets/emoji/SOURCE.json"
REL_TABLE = "emoji_table.json.gz"

# OD4, the owner's size gate on sheet + table: at or under the target the pack is
# accepted as built; up to the limit it needs the owner's yes; above it the build
# refuses (rebuild with --no-skin-tones).
SOFT_BUDGET_BYTES = 1_500_000
HARD_BUDGET_BYTES = 3_000_000
COMPARE_BACKGROUND = (0x31, 0x33, 0x38)          # Discord's dark chat background

# --palette 256: how the 256 entries are shared out (see quantise_sheet)
PALETTE_OPAQUE = 144             # + entry 0 (transparent) + the edge entries = 256
PALETTE_EDGES = 111
ALPHA_SNAP_LOW = 4               # alpha at or below this is zeroed BEFORE quantising.
#   This is NOT the floor of what survives.  The edge octree is also handed every
#   zeroed pixel, so its "transparent" entry swallows the faintest edges as well.
#   Measured on the shipped sheet (2026-10-09): every pixel up to alpha 8 becomes
#   0, so do 99.6% of alpha 9-16, 78% of 17-24 and 46% of 25-32; the highest
#   alpha that became 0 is 31, and the faintest alpha left in the sheet is 25.
#   Read "alpha below about 32 may become fully transparent".
ALPHA_SNAP_HIGH = 251            # alpha at or above this becomes fully opaque

# ---------------------------------------------------------------------------
# the contract's numbers
# ---------------------------------------------------------------------------
TABLE_VERSION = 1
EMOJI_VERSION = "16.0"
TILE_PX = 22
CELL_PX = 24
COLS = 62
EXPECTED_FULLY_QUALIFIED = 3781
EXPECTED_COUNT = 3807            # + the 26 regional-indicator letters

# ---------------------------------------------------------------------------
# pinned inputs
# ---------------------------------------------------------------------------
TWEMOJI_VERSION = "16.0.1"
TWEMOJI_LISTING_URL = ("https://data.jsdelivr.com/v1/packages/gh/jdecked/twemoji@"
                       + TWEMOJI_VERSION + "?structure=flat")
TWEMOJI_PNG_URL = ("https://cdn.jsdelivr.net/gh/jdecked/twemoji@" + TWEMOJI_VERSION
                   + "/assets/72x72/%s.png")
SHORTCODE_PACKAGE = "discord-emoji"
SHORTCODE_VERSION = "2.5.3"
NPM_META_URL = "https://registry.npmjs.org/%s/%s" % (SHORTCODE_PACKAGE, SHORTCODE_VERSION)
NPM_TARBALL_URL = "https://registry.npmjs.org/%s/-/%s-%s.tgz" % (
    SHORTCODE_PACKAGE, SHORTCODE_PACKAGE, SHORTCODE_VERSION)
EMOJI_TEST_URL = "https://unicode.org/Public/emoji/" + EMOJI_VERSION + "/emoji-test.txt"

# Every request (and every redirect target) must be https on one of these.
ALLOWED_HOSTS = frozenset({
    "cdn.jsdelivr.net", "data.jsdelivr.com",     # the Twemoji tag mirror + its listing
    "registry.npmjs.org",                        # discord-emoji
    "unicode.org", "www.unicode.org",            # emoji-test.txt
})

USER_AGENT = ("FCTool-emoji-pack-builder/%d (one-off dev-time asset build; "
              "https://github.com/kirejen-byte/FCtool)" % TOOL_VERSION)
MAX_CONNECTIONS = 6              # polite: the ceiling agreed for this fetch is 8
HTTP_TIMEOUT_S = 30
HTTP_ATTEMPTS = 4

# Hard ceilings on what a single response may be (the approval is for ~3.55 MB of
# PNGs, a small npm tarball and a ~0.6 MB text file -- anything far larger is a
# sign the wrong thing is being served, and is refused rather than stored).
LISTING_MAX_BYTES = 8_000_000
NPM_META_MAX_BYTES = 1_000_000
NPM_TARBALL_MAX_BYTES = 4_000_000
EMOJI_TEST_MAX_BYTES = 2_000_000
PNG_MAX_BYTES = 64_000
PNG_TOTAL_MAX_BYTES = 4_500_000

CACHE_EMOJI_TEST = "emoji-test.txt"
CACHE_LISTING = "twemoji-%s-listing.json" % TWEMOJI_VERSION
CACHE_TARBALL = "%s-%s.tgz" % (SHORTCODE_PACKAGE, SHORTCODE_VERSION)
CACHE_PNG_DIR = "72x72"
SHORTCODE_MEMBER = "package/esm/mod.js"          # the generated name -> emoji tables
SHORTCODE_LICENSE_MEMBER = "package/LICENSE"

VS16 = 0xFE0F
ZWJ = 0x200D
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

STATUSES = ("fully-qualified", "minimally-qualified", "unqualified", "component")
LETTER_GROUP = "Letters"

# The 8 Emoji 16.0 additions that discord-emoji 2.5.3 does not carry, named as
# Discord shows them.
# Source checked: all eight names (and their code points) were confirmed on
# 2026-10-09 against the stable map published at
# https://emzi0767.mzgit.io/discord-emoji/ (discordEmojiMap.min.json, its
# versionTimestamp 2026-10-09T09:04:06Z), read once, in memory, for these eight
# names only -- nothing else from that map is used or bundled.  Note the flag:
# Discord calls it ``flag_sark``, NOT ``flag_cq`` (the ISO-code pattern every
# other flag follows would have guessed wrong).
EXTRA_SHORTCODES = {
    (0x1FAE9,): ["face_with_bags_under_eyes"],
    (0x1FAC6,): ["fingerprint"],
    (0x1FABE,): ["leafless_tree"],
    (0x1FADC,): ["root_vegetable"],
    (0x1FA89,): ["harp"],
    (0x1FA8F,): ["shovel"],
    (0x1FADF,): ["splatter"],
    (0x1F1E8, 0x1F1F6): ["flag_sark"],        # flag: Sark
}

_TONE_WORDS = {
    "light skin tone": 1,
    "medium-light skin tone": 2,
    "medium skin tone": 3,
    "medium-dark skin tone": 4,
    "dark skin tone": 5,
}


class BuildError(Exception):
    """Anything that must stop the build with a plain sentence."""


# ---------------------------------------------------------------------------
# emoji-test.txt
# ---------------------------------------------------------------------------

TestLine = namedtuple("TestLine", "cps status name group")

_DATA_LINE = re.compile(
    r"^([0-9A-F]{2,6}(?: [0-9A-F]{2,6})*)\s*;\s*([a-z-]+)\s*"
    r"#\s*\S+\s+E[0-9]+\.[0-9]+\s+(.+?)\s*$")
_GROUP_LINE = re.compile(r"^#\s*group:\s*(.+?)\s*$")


def parse_emoji_test(text) -> list:
    """Every data line of ``emoji-test.txt`` as a ``TestLine``, in file order.

    ``cps`` is a tuple of code points; ``group`` is the enclosing ``# group:``.
    A data line that does not parse is a hard failure naming its line number --
    a silently skipped line would be a silently missing emoji.
    """
    lines = []
    group = ""
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            hit = _GROUP_LINE.match(line)
            if hit:
                group = hit.group(1)
            continue
        hit = _DATA_LINE.match(line)
        if not hit or hit.group(2) not in STATUSES:
            raise BuildError("emoji-test.txt line %d is not understood: %r"
                             % (number, raw[:120]))
        cps = tuple(int(h, 16) for h in hit.group(1).split(" "))
        lines.append(TestLine(cps, hit.group(2), hit.group(3), group))
    return lines


def seq_string(cps) -> str:
    return "".join(chr(c) for c in cps)


def seq_key(cps) -> str:
    """The sequence with every VS16 removed -- the form-independent identity.

    A fully-qualified sequence and its minimally-/unqualified forms share it, and
    so does whatever spelling a shortcode source happens to use.
    """
    return "".join(chr(c) for c in cps if c != VS16)


def letter_entries() -> list:
    """The 26 regional-indicator letters: ``[(cps, name, shortcode), ...]``."""
    out = []
    for offset in range(26):
        letter = chr(ord("a") + offset)
        out.append(((0x1F1E6 + offset,), "regional indicator " + letter,
                    "regional_indicator_" + letter))
    return out


# ---------------------------------------------------------------------------
# skin-tone names
# ---------------------------------------------------------------------------

def _split_name(name):
    head, sep, tail = name.partition(": ")
    return head, ([part.strip() for part in tail.split(",")] if sep else [])


def tone_numbers(name) -> tuple:
    """Discord's tone numbers (1 light .. 5 dark) named by a CLDR name, in order."""
    return tuple(_TONE_WORDS[q] for q in _split_name(name)[1] if q in _TONE_WORDS)


def base_name(name) -> str:
    """The CLDR name of the same emoji without its skin tone(s)."""
    head, qualifiers = _split_name(name)
    kept = [q for q in qualifiers if q not in _TONE_WORDS]
    return head + (": " + ", ".join(kept) if kept else "")


# ---------------------------------------------------------------------------
# Twemoji file names
# ---------------------------------------------------------------------------

def twemoji_names(cps) -> tuple:
    """``(name, the other form)`` of the Twemoji file for a sequence.

    Lower-case hex code points joined by ``-``; every ``fe0f`` is dropped UNLESS
    the sequence contains ``200d``.  A handful of files break that rule, so the
    second element is the form to try when the first does not exist.
    """
    kept = "-".join("%x" % c for c in cps)
    stripped = "-".join("%x" % c for c in cps if c != VS16)
    return (kept, stripped) if ZWJ in cps else (stripped, kept)


def pick_tile_name(cps, available):
    """The Twemoji file name to use for ``cps`` given the names that exist, or None."""
    for name in twemoji_names(cps):
        if name in available:
            return name
    return None


# ---------------------------------------------------------------------------
# sheet grid
# ---------------------------------------------------------------------------

def tile_origin(tile, cols=COLS, cell_px=CELL_PX) -> tuple:
    """Top-left pixel of tile ``tile`` (the 1 px transparent ring is outside it)."""
    return 1 + (tile % cols) * cell_px, 1 + (tile // cols) * cell_px


def sheet_size(tiles, cols=COLS, cell_px=CELL_PX) -> tuple:
    rows = -(-tiles // cols)
    return cols * cell_px, rows * cell_px


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------

def merge_extra_shortcodes(shortcodes) -> dict:
    """``shortcodes`` plus ``EXTRA_SHORTCODES`` for the emoji it does not name."""
    merged = {key: list(names) for key, names in shortcodes.items()}
    for cps, names in EXTRA_SHORTCODES.items():
        if not merged.get(seq_key(cps)):
            merged[seq_key(cps)] = list(names)
    return merged


def build_table(lines, shortcodes=None, *, skin_tone_tiles=True) -> dict:
    """The ``emoji_table.json.gz`` document for parsed ``emoji-test.txt`` lines.

    ``shortcodes`` maps ``seq_key`` -> names (first = the one shown).  With
    ``skin_tone_tiles=False`` a toned entry carries its base emoji's tile.
    """
    shortcodes = shortcodes or {}
    entries = [(ln.cps, ln.name, ln.group) for ln in lines
               if ln.status == "fully-qualified"]
    entries += [(cps, name, LETTER_GROUP) for cps, name, _code in letter_entries()]
    letter_codes = {seq_key(cps): code for cps, _name, code in letter_entries()}

    by_key = {}
    by_name = {}
    for pos, (cps, name, _group) in enumerate(entries):
        key = seq_key(cps)
        if key in by_key:
            raise BuildError("two entries share the sequence %s"
                             % "-".join("%X" % c for c in cps))
        by_key[key] = pos
        by_name.setdefault(name, pos)

    # tiles: every entry its own, or (fallback) a toned entry borrows its base's
    tile_of = [None] * len(entries)
    next_tile = 0
    for pos, (_cps, name, _group) in enumerate(entries):
        if skin_tone_tiles or not tone_numbers(name):
            tile_of[pos] = next_tile
            next_tile += 1
    orphans = []
    for pos, (_cps, name, _group) in enumerate(entries):
        if tile_of[pos] is None:
            # "kiss: person, person, <tones>" has no "kiss: person, person" entry:
            # its base is the bare head, "kiss"
            base = by_name.get(base_name(name), by_name.get(_split_name(name)[0]))
            if base is None or tile_of[base] is None:
                orphans.append(name)
            else:
                tile_of[pos] = tile_of[base]
    if orphans:
        raise BuildError("no base emoji for the toned entries: " + "; ".join(orphans))

    rows = []
    for pos, (cps, name, group) in enumerate(entries):
        names = []
        key = seq_key(cps)
        for code in ([letter_codes[key]] if key in letter_codes else []) \
                + list(shortcodes.get(key, ())):
            if code not in names:
                names.append(code)
        rows.append([seq_string(cps), name, names, group, tile_of[pos]])

    fully_qualified = {row[0] for row in rows}
    alt = {}
    lost = []
    for ln in lines:
        if ln.status not in ("minimally-qualified", "unqualified"):
            continue
        seq = seq_string(ln.cps)
        pos = by_key.get(seq_key(ln.cps))
        if pos is None or seq in fully_qualified:
            lost.append("-".join("%X" % c for c in ln.cps))
        else:
            alt[seq] = pos
    if lost:
        raise BuildError("non-fully-qualified forms with no fully-qualified entry: "
                         + "; ".join(lost))

    return {
        "version": TABLE_VERSION,
        "emoji_version": EMOJI_VERSION,
        "tile_px": TILE_PX,
        "cell_px": CELL_PX,
        "cols": COLS,
        "count": len(rows),
        "tiles": next_tile,
        "emoji": rows,
        "alt": alt,
    }


def tile_owners(table) -> list:
    """For each tile, the position of the entry whose artwork fills it."""
    owners = [None] * table["tiles"]
    for pos, row in enumerate(table["emoji"]):
        if owners[row[4]] is None:
            owners[row[4]] = pos
    if None in owners:
        raise BuildError("the table leaves a tile with no entry")
    return owners


def serialise_table(table) -> bytes:
    """The table's on-disk bytes: gzip level 9, mtime 0, sorted keys, UTF-8.

    ``GzipFile`` (not ``gzip.compress``) so the header's OS byte is the fixed
    "unknown" value on every platform -- the artifact is byte-reproducible.
    """
    text = json.dumps(table, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", compresslevel=9, fileobj=buf,
                       mtime=0) as gz:
        gz.write(text)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# archives: read in memory, member by member -- never unpacked to disk
# ---------------------------------------------------------------------------

def check_member_path(name) -> None:
    if (not name or "\x00" in name or ".." in name
            or name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name)):
        raise BuildError("refusing an archive member with an unsafe path: %r" % (name,))


def read_tgz_members(blob, *, max_member_bytes=4_000_000, max_total_bytes=16_000_000,
                     max_members=2000) -> dict:
    """``{member path: bytes}`` for the regular files of a ``.tgz`` held in memory.

    Any member whose path contains ``..`` or is absolute refuses the WHOLE
    archive.  Links, directories and devices are ignored (nothing is followed,
    nothing touches the disk).  Sizes are capped.
    """
    members = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            for count, info in enumerate(tf, 1):
                if count > max_members:
                    raise BuildError("refusing an archive with more than %d members"
                                     % max_members)
                check_member_path(info.name)
                if not info.isfile():
                    continue
                if info.size > max_member_bytes:
                    raise BuildError("refusing an oversize archive member: %r (%d bytes)"
                                     % (info.name, info.size))
                handle = tf.extractfile(info)
                data = handle.read(max_member_bytes + 1) if handle else b""
                total += len(data)
                if len(data) > max_member_bytes or total > max_total_bytes:
                    raise BuildError("refusing an oversize archive (%r)" % (info.name,))
                members[info.name] = data
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        raise BuildError("the archive could not be read: %s" % exc) from exc
    return members


# ---------------------------------------------------------------------------
# network: https, pinned hosts, few connections, bounded bodies
# ---------------------------------------------------------------------------

def check_url(url):
    """``urlsplit(url)`` if it is plain https on a pinned host, else BuildError."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise BuildError("refusing a malformed URL: %r" % (url,)) from exc
    if (parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS
            or parts.username is not None or parts.password is not None
            or port not in (None, 443)):
        raise BuildError("refusing a URL outside the pinned hosts: %r" % (url,))
    return parts


class ShortBody(http.client.HTTPException):
    """A response body shorter than its ``Content-Length`` -- retried like any
    other transport failure."""


class Net:
    """A small polite HTTPS client: one keep-alive connection per thread and host,
    retries with backoff, every URL and redirect checked against the host list."""

    RETRY_STATUSES = (429, 500, 502, 503, 504)
    REDIRECT_STATUSES = (301, 302, 303, 307, 308)

    def __init__(self, user_agent=USER_AGENT, timeout=HTTP_TIMEOUT_S,
                 attempts=HTTP_ATTEMPTS, sleep=time.sleep, connect=None):
        self._user_agent = user_agent
        self._timeout = timeout
        self._attempts = attempts
        self._sleep = sleep
        self._local = threading.local()
        self._context = ssl.create_default_context()
        # ``connect(host)`` -> a connection object; the tests pass a stub here
        self._connect = connect or (lambda host: http.client.HTTPSConnection(
            host, timeout=self._timeout, context=self._context))

    # -- connections -------------------------------------------------------
    def _pool(self):
        pool = getattr(self._local, "pool", None)
        if pool is None:
            pool = self._local.pool = {}
        return pool

    def _drop(self, host):
        conn = self._pool().pop(host, None)
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def _once(self, method, url, max_bytes, extra=None):
        parts = check_url(url)
        host = parts.hostname
        pool = self._pool()
        reused = host in pool
        if not reused:
            pool[host] = self._connect(host)
        conn = pool[host]
        target = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        try:
            headers = {
                "User-Agent": self._user_agent,
                "Accept": "*/*",
                "Accept-Encoding": "identity",
            }
            headers.update(extra or {})
            conn.request(method, target, headers=headers)
            resp = conn.getresponse()
            headers = {k.lower(): v for k, v in resp.getheaders()}
            body = resp.read(max_bytes + 1)
            if len(body) > max_bytes:
                self._drop(host)
                raise BuildError("refusing an oversize response (> %d bytes) from %s"
                                 % (max_bytes, url))
            # ``read(n)`` hands back a cut-short body without complaint: a
            # connection that dies mid-file must not pass for the whole file
            stated = headers.get("content-length", "").strip()
            if (method != "HEAD" and re.fullmatch(r"[0-9]{1,12}", stated)
                    and len(body) != int(stated)):
                raise ShortBody("got %d of %s bytes" % (len(body), stated))
            return resp.status, headers, body, reused
        except (OSError, http.client.HTTPException) as exc:
            self._drop(host)
            exc.reused_connection = reused
            raise

    def _request(self, method, url, max_bytes, extra=None, ok=(200,)):
        for _hop in range(5):
            status, headers, body = self._with_retries(method, url, max_bytes, extra)
            if status in self.REDIRECT_STATUSES and headers.get("location"):
                url = urljoin(url, headers["location"])
                check_url(url)
                continue
            if status not in ok:
                raise BuildError("%s %s answered HTTP %d" % (method, url, status))
            return headers, body
        raise BuildError("too many redirects for %s" % url)

    def _with_retries(self, method, url, max_bytes, extra=None):
        problem = "no attempt made"
        stale_retry_used = False
        attempt = 0
        while attempt < self._attempts:
            delay = 2 ** attempt
            try:
                status, headers, body, _reused = self._once(method, url, max_bytes, extra)
            except (OSError, http.client.HTTPException) as exc:
                problem = "%s: %s" % (type(exc).__name__, exc)
                if getattr(exc, "reused_connection", False) and not stale_retry_used:
                    stale_retry_used = True      # a keep-alive the server closed:
                    continue                     # retry once at once on a new one
            else:
                if status not in self.RETRY_STATUSES:
                    return status, headers, body
                problem = "HTTP %d" % status
                wait = headers.get("retry-after", "")
                if re.fullmatch(r"[0-9]{1,3}", wait):
                    delay = max(delay, min(int(wait), 30))
            attempt += 1
            if attempt < self._attempts:
                self._sleep(delay)
        raise BuildError("%s %s failed after %d attempts (%s)"
                         % (method, url, self._attempts, problem))

    # -- the two verbs the tool uses ---------------------------------------
    def get(self, url, max_bytes) -> bytes:
        return self._request("GET", url, max_bytes)[1]

    def head(self, url) -> dict:
        """The final response's headers (lower-case names); no body is fetched.

        The CDNs in front of two of the inputs leave ``Content-Length`` off a
        HEAD answer, so the request carries ``Range: bytes=0-0``: the answer is
        then ``206`` with ``Content-Range: bytes 0-0/<total>`` -- still a HEAD,
        still zero body bytes.
        """
        return self._request("HEAD", url, 0, {"Range": "bytes=0-0"}, ok=(200, 206))[0]


def head_size(headers) -> tuple:
    """``(bytes that a plain GET will transfer or None, remark)`` from HEAD headers.

    Order of belief: an Apache-style ETag ``"<hex size>-<hex mtime>-gzip"`` (the
    server keeps the file gzip-compressed, so its Content-Range counts the
    COMPRESSED bytes while this tool asks for -- and receives -- the plain file),
    then ``Content-Range``'s total, then ``Content-Length``.
    """
    total = None
    hit = re.fullmatch(r"bytes [0-9]+-[0-9]+/([0-9]{1,12})",
                       headers.get("content-range", "").strip())
    if hit:
        total = int(hit.group(1))
    elif re.fullmatch(r"[0-9]{1,12}", headers.get("content-length", "").strip()):
        total = int(headers["content-length"].strip())
    etag = re.fullmatch(r'(?:W/)?"([0-9a-f]{1,10})-[0-9a-f]{6,16}-gzip"',
                        headers.get("etag", "").strip())
    if etag:
        plain = int(etag.group(1), 16)
        remark = "size is the plain file's, from the server's ETag"
        if total is not None:
            remark += " (it stores a gzip copy of %s bytes)" % format(total, ",")
        return plain, remark
    return total, ""


def _json(blob, what):
    try:
        doc = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BuildError("%s is not JSON: %s" % (what, exc)) from exc
    if not isinstance(doc, dict):
        raise BuildError("%s is not a JSON object" % what)
    return doc


_PNG_PATH = re.compile(r"^/assets/72x72/([0-9a-f]+(?:-[0-9a-f]+)*)\.png$")


def twemoji_png_listing(listing) -> dict:
    """``{file name without .png: (size, sha256 hex or "")}`` for the 72x72 PNGs.

    ``listing`` is jsDelivr's flat file listing of the pinned tag (untrusted: every
    field is type-checked, and only names of the expected shape are kept).
    """
    if listing.get("version") != TWEMOJI_VERSION or listing.get("name") != "jdecked/twemoji":
        raise BuildError("the file listing is not jdecked/twemoji %s" % TWEMOJI_VERSION)
    out = {}
    files = listing.get("files")
    for item in files if isinstance(files, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        hit = _PNG_PATH.match(item["name"])
        size = item.get("size")
        if not hit or type(size) is not int or size < 0:
            continue
        digest = ""
        if isinstance(item.get("hash"), str):
            try:
                raw = base64.b64decode(item["hash"], validate=True)
            except ValueError:
                raw = b""
            digest = raw.hex() if len(raw) == 32 else ""
        out[hit.group(1)] = (size, digest)
    return out


def needed_tile_names(lines, available) -> list:
    """The Twemoji file name for every table entry, in entry order.

    An emoji with no tile is a hard failure that lists every one of them.
    """
    cps_list = [ln.cps for ln in lines if ln.status == "fully-qualified"]
    cps_list += [cps for cps, _name, _code in letter_entries()]
    names = []
    missing = []
    for cps in cps_list:
        name = pick_tile_name(cps, available)
        if name is None:
            missing.append("/".join(twemoji_names(cps)))
        names.append(name)
    if missing:
        raise BuildError("%d emoji have no Twemoji tile: %s"
                         % (len(missing), "; ".join(missing)))
    return names


def _npm_dist(meta) -> dict:
    dist = meta.get("dist")
    if (meta.get("name") != SHORTCODE_PACKAGE or meta.get("version") != SHORTCODE_VERSION
            or not isinstance(dist, dict) or dist.get("tarball") != NPM_TARBALL_URL):
        raise BuildError("the npm manifest is not %s %s at the pinned tarball URL"
                         % (SHORTCODE_PACKAGE, SHORTCODE_VERSION))
    return dist


# ---------------------------------------------------------------------------
# --plan
# ---------------------------------------------------------------------------

PlanItem = namedtuple("PlanItem", "label url files bytes note")


def plan(net, lines=None) -> list:
    """What ``--fetch`` would download, learned from METADATA requests only.

    ``lines`` (optional) are the parsed lines of an ``emoji-test.txt`` that is
    already in the cache: the PNG line is then the exact files the table needs.
    Without them it is the whole 72x72 directory -- an upper bound on what
    ``--fetch`` takes.
    """
    listing_blob = net.get(TWEMOJI_LISTING_URL, LISTING_MAX_BYTES)
    pngs = twemoji_png_listing(_json(listing_blob, "the Twemoji file listing"))
    needed = needed_tile_names(lines, pngs) if lines is not None else None
    if needed is None:
        chosen = sorted(pngs)
        note = ("every file in assets/72x72/ -- an upper bound: --fetch takes only "
                "the files the %d emoji need" % EXPECTED_COUNT)
    else:
        chosen = sorted(set(needed))
        note = "exactly the files the %d table entries need" % len(needed)
    note += ("; plus the tag's file listing (metadata JSON, %s bytes, from %s -- "
             "read by this --plan, kept in the cache by --fetch)"
             % (format(len(listing_blob), ","), TWEMOJI_LISTING_URL))
    png_bytes = sum(pngs[name][0] for name in chosen)

    dist = _npm_dist(_json(net.get(NPM_META_URL, NPM_META_MAX_BYTES), "the npm manifest"))
    unpacked = dist.get("unpackedSize")
    inner = dist.get("fileCount")
    pkg_note = "one .tgz, read in memory member by member"
    if type(unpacked) is int and type(inner) is int:
        pkg_note += " (%d files, %s bytes unpacked)" % (inner, format(unpacked, ","))

    pkg_bytes, pkg_remark = head_size(net.head(NPM_TARBALL_URL))
    txt_bytes, txt_remark = head_size(net.head(EMOJI_TEST_URL))
    return [
        PlanItem("Twemoji %s 72x72 PNGs (jdecked/twemoji, CC-BY 4.0)" % TWEMOJI_VERSION,
                 TWEMOJI_PNG_URL % "<name>", len(chosen), png_bytes, note),
        PlanItem("npm %s %s (MIT)" % (SHORTCODE_PACKAGE, SHORTCODE_VERSION),
                 NPM_TARBALL_URL, 1, pkg_bytes,
                 "; ".join(part for part in (pkg_note, pkg_remark) if part)),
        PlanItem("Unicode emoji-test.txt %s (Unicode License v3)" % EMOJI_VERSION,
                 EMOJI_TEST_URL, 1, txt_bytes,
                 "; ".join(part for part in ("one text file", txt_remark) if part)),
    ]


def _bytes_text(value) -> str:
    if value is None:
        return "UNKNOWN (the server did not state a size)"
    return "%s bytes (%.2f MB)" % (format(value, ","), value / 1_000_000)


def format_plan(items) -> str:
    out = ["build_emoji_pack --plan: what --fetch would download "
           "(metadata requests only; nothing was downloaded)", ""]
    for number, item in enumerate(items, 1):
        out += [
            "%d. %s" % (number, item.label),
            "   URL   : %s" % item.url,
            "   files : %s" % format(item.files, ","),
            "   bytes : %s" % _bytes_text(item.bytes),
            "   note  : %s" % item.note,
            "",
        ]
    known = [item.bytes for item in items if item.bytes is not None]
    total = "TOTAL  : %s files, %s" % (
        format(sum(item.files for item in items), ","), _bytes_text(sum(known)))
    if len(known) != len(items):
        total += "  + the inputs of unknown size above"
    out.append(total)
    out.append("hosts  : " + ", ".join(sorted(ALLOWED_HOSTS)))
    out.append("fetch  : at most %d connections, User-Agent %r"
               % (MAX_CONNECTIONS, USER_AGENT))
    return "\n".join(out)


def _utf8(data, what) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildError("%s in the cache is not UTF-8 (%s) -- delete it and run "
                         "--fetch again" % (what, exc)) from exc


def _cached_test_lines(cache_dir):
    """Parsed ``emoji-test.txt`` when it is already in the cache, else None."""
    path = Path(cache_dir) / CACHE_EMOJI_TEST
    if not path.is_file():
        return None
    return parse_emoji_test(_utf8(path.read_bytes(), CACHE_EMOJI_TEST))


# ---------------------------------------------------------------------------
# --fetch
# ---------------------------------------------------------------------------

def _sha256(data) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_bytes(data)
    os.replace(part, path)


def _cached_or_get(net, url, path, max_bytes, check, why=None) -> tuple:
    """``(bytes, downloaded?)`` -- the cached file when it passes ``check``.

    ``check(bytes) -> bool`` is asked about a cached file (a bad one is fetched
    again, so a cut-short download cannot stick) and about a fresh download
    BEFORE it is written: nothing unverified ever reaches the cache.
    ``why(bytes) -> str`` (optional) says what is wrong with a refused download;
    its answer is added to the error.
    """
    path = Path(path)
    if path.is_file():
        data = path.read_bytes()
        if check(data):
            return data, False
    data = net.get(url, max_bytes)
    if not check(data):
        reason = why(data) if why is not None else ""
        raise BuildError("the download of %s is incomplete or is not the file that "
                         "was expected -- nothing was written to the cache%s"
                         % (url, " (%s)" % reason if reason else ""))
    _atomic_write(path, data)
    return data, True


def emoji_test_complete(data) -> bool:
    """True for a whole ``emoji-test.txt``: UTF-8, and its last line is ``#EOF``."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return text.rstrip().endswith("\n#EOF")


def emoji_test_problem(data) -> str:
    """What stops ``data`` from being THE ``emoji-test.txt`` this tool is pinned to,
    as a sentence; ``""`` when nothing does.

    Whole (``emoji_test_complete``), every data line understood, and exactly
    ``EXPECTED_FULLY_QUALIFIED`` fully-qualified lines.  ``fetch`` asks this BEFORE
    the cache write, so a complete file of another edition is never stored -- and
    one that is already in the cache is fetched again instead of being believed.
    """
    if not emoji_test_complete(data):
        return "emoji-test.txt is cut short (no #EOF line) or is not UTF-8"
    try:
        lines = parse_emoji_test(data.decode("utf-8"))
    except BuildError as exc:
        return str(exc)
    fully_qualified = sum(1 for ln in lines if ln.status == "fully-qualified")
    if fully_qualified != EXPECTED_FULLY_QUALIFIED:
        return ("emoji-test.txt has %d fully-qualified lines, expected %d"
                % (fully_qualified, EXPECTED_FULLY_QUALIFIED))
    return ""


def listing_complete(data) -> bool:
    """True for well-formed JSON that is the pinned tag's listing and names PNGs."""
    try:
        return bool(twemoji_png_listing(_json(data, "the Twemoji file listing")))
    except BuildError:
        return False


def _tarball_check(dist):
    shasum = dist.get("shasum")
    integrity = dist.get("integrity")
    if not isinstance(shasum, str) or not re.fullmatch(r"[0-9a-f]{40}", shasum):
        raise BuildError("the npm manifest carries no sha1 for the tarball")

    def check(data) -> bool:
        if hashlib.sha1(data).hexdigest() != shasum:
            return False
        if isinstance(integrity, str) and integrity.startswith("sha512-"):
            want = integrity[len("sha512-"):]
            return base64.b64encode(hashlib.sha512(data).digest()).decode("ascii") == want
        return True
    return check


def _fetch_png(net, name, size, digest, png_dir) -> int:
    """Make ``<png_dir>/<name>.png`` present and verified; bytes downloaded (0 = cached)."""
    dest = Path(png_dir) / (name + ".png")

    def good(data) -> bool:
        return (len(data) == size and data.startswith(PNG_SIGNATURE)
                and (not digest or _sha256(data) == digest))

    if dest.is_file() and good(dest.read_bytes()):
        return 0
    data = net.get(TWEMOJI_PNG_URL % name, PNG_MAX_BYTES)
    if not good(data):
        raise BuildError("%s.png does not match the listing (size or sha256)" % name)
    _atomic_write(dest, data)
    return len(data)


def fetch(net, cache_dir=CACHE_DIR, out=print) -> dict:
    """Download the three inputs into ``cache_dir`` (resumable; verified)."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # whole, understood AND the pinned edition (its fully-qualified count): all three
    # are settled before the file may reach the cache
    test_bytes, fresh = _cached_or_get(net, EMOJI_TEST_URL, cache_dir / CACHE_EMOJI_TEST,
                                       EMOJI_TEST_MAX_BYTES,
                                       lambda data: not emoji_test_problem(data),
                                       why=emoji_test_problem)
    out("emoji-test.txt      %10s bytes  %s" % (format(len(test_bytes), ","),
                                               "downloaded" if fresh else "cached"))
    lines = parse_emoji_test(test_bytes.decode("utf-8"))

    listing_bytes, fresh = _cached_or_get(net, TWEMOJI_LISTING_URL, cache_dir / CACHE_LISTING,
                                          LISTING_MAX_BYTES, listing_complete)
    out("Twemoji listing     %10s bytes  %s (metadata)"
        % (format(len(listing_bytes), ","), "downloaded" if fresh else "cached"))
    pngs = twemoji_png_listing(_json(listing_bytes, "the Twemoji file listing"))
    names = sorted(set(needed_tile_names(lines, pngs)))
    png_total = sum(pngs[name][0] for name in names)
    if png_total > PNG_TOTAL_MAX_BYTES:
        raise BuildError("the PNGs total %d bytes -- above the %d-byte ceiling of the "
                         "approved download" % (png_total, PNG_TOTAL_MAX_BYTES))

    dist = _npm_dist(_json(net.get(NPM_META_URL, NPM_META_MAX_BYTES), "the npm manifest"))
    tarball, fresh = _cached_or_get(net, NPM_TARBALL_URL, cache_dir / CACHE_TARBALL,
                                    NPM_TARBALL_MAX_BYTES, _tarball_check(dist))
    out("discord-emoji tgz   %10s bytes  %s" % (format(len(tarball), ","),
                                               "downloaded" if fresh else "cached"))

    fetched_files, fetched_bytes = fetch_pngs(net, names, pngs, cache_dir / CACHE_PNG_DIR, out)
    out("Twemoji PNGs        %10s bytes in %d files (%d downloaded now, %s bytes)"
        % (format(png_total, ","), len(names), fetched_files, format(fetched_bytes, ",")))
    return {"png_files": len(names), "png_bytes": png_total,
            "emoji_test_bytes": len(test_bytes), "tarball_bytes": len(tarball)}


def fetch_pngs(net, names, pngs, png_dir, out=print) -> tuple:
    """Make every named PNG present and verified: ``(files, bytes)`` downloaded now.

    The FIRST failure stops the run: the worker that fails raises the flag
    itself, the queued files are never requested, and the error is re-raised.
    (At most ``MAX_CONNECTIONS - 1`` requests already in flight still finish.)
    """
    png_dir = Path(png_dir)
    png_dir.mkdir(parents=True, exist_ok=True)
    done = [0, 0, 0]                 # files, bytes downloaded, files downloaded
    lock = threading.Lock()
    stop = threading.Event()

    def work(name):
        if stop.is_set():
            return
        try:
            size, digest = pngs[name]
            got = _fetch_png(net, name, size, digest, png_dir)
        except BaseException:
            stop.set()
            raise
        with lock:
            done[0] += 1
            done[1] += got
            done[2] += 1 if got else 0
            if done[0] % 500 == 0 or done[0] == len(names):
                out("Twemoji PNGs        %d / %d" % (done[0], len(names)))

    with ThreadPoolExecutor(max_workers=MAX_CONNECTIONS) as pool:
        futures = [pool.submit(work, name) for name in names]
        try:
            for future in as_completed(futures):
                future.result()
        except BaseException:
            stop.set()
            for future in futures:
                future.cancel()
            raise
    return done[2], done[1]


# ---------------------------------------------------------------------------
# the shortcode source: read as TEXT, never run
# ---------------------------------------------------------------------------

_JS_PAIR = re.compile(r'^\s*"([^"\\]{1,100})"\s*:\s*"([^"\\]{1,40})"\s*,?\s*$')
# What a shortcode may be made of: an ALLOW-list, plus the one name Discord
# spells with a non-ASCII letter (n with tilde) -- named here, so a second such
# name is a build failure to look at, not something that slips through.
_SHORTCODE_NAME = re.compile(r"[a-z0-9_+\-]{1,100}")
NON_ASCII_SHORTCODES = frozenset({"pi" + chr(0xF1) + "ata"})


def shortcode_name_ok(name) -> bool:
    return bool(_SHORTCODE_NAME.fullmatch(name)) or name in NON_ASCII_SHORTCODES


_JS_FRAME = re.compile(
    r'^\s*(?://.*|/\*.*\*/|const [a-z]+ = \{|\};|export \{[a-z, ]+\};)?\s*$')


def text_key(text) -> str:
    """``seq_key`` for a sequence that arrives as a string."""
    return "".join(ch for ch in text if ord(ch) != VS16)


def parse_shortcode_module(text) -> dict:
    """``{seq_key: [name, ...]}`` from discord-emoji's generated ``mod.js``.

    The file is a list of ``const <category> = { "name": "<emoji>", ... };``
    blocks.  It is matched line by line as text; a line of any other shape is a
    hard failure (so is a name given to two different emoji, and a name that is
    not on the allow-list -- ``shortcode_name_ok``).  Names keep the
    file's order: the first one listed for an emoji is the one shown.
    """
    out = {}
    owner = {}
    for number, line in enumerate(text.splitlines(), 1):
        hit = _JS_PAIR.match(line)
        if not hit:
            if _JS_FRAME.match(line):
                continue
            raise BuildError("the shortcode module's line %d is not understood: %r"
                             % (number, line[:120]))
        name, key = hit.group(1), text_key(hit.group(2))
        if not shortcode_name_ok(name):
            raise BuildError("the shortcode module's line %d names an emoji %s, which "
                             "is outside the allowed characters [a-z0-9_+-]"
                             % (number, ascii(name)))
        if owner.setdefault(name, key) != key:
            raise BuildError("the shortcode %r names two different emoji" % (name,))
        names = out.setdefault(key, [])
        if name not in names:
            names.append(name)
    if not out:
        raise BuildError("the shortcode module holds no names")
    return out


# ---------------------------------------------------------------------------
# --build / --compare
# ---------------------------------------------------------------------------

Inputs = namedtuple("Inputs", "lines shortcodes tile_names pngs png_dir license_text record")


def _read_cached(cache_dir, name) -> bytes:
    path = Path(cache_dir) / name
    if not path.is_file():
        raise BuildError("%s is not in the cache %s -- run --fetch first"
                         % (name, cache_dir))
    return path.read_bytes()


def load_inputs(cache_dir) -> Inputs:
    """Everything ``--build`` needs, read (and re-checked) from the download cache."""
    cache_dir = Path(cache_dir)
    test_bytes = _read_cached(cache_dir, CACHE_EMOJI_TEST)
    if not emoji_test_complete(test_bytes):
        raise BuildError("%s in the cache is cut short or is not UTF-8 -- delete it "
                         "and run --fetch again" % CACHE_EMOJI_TEST)
    lines = parse_emoji_test(_utf8(test_bytes, CACHE_EMOJI_TEST))
    fully_qualified = sum(1 for ln in lines if ln.status == "fully-qualified")
    if fully_qualified != EXPECTED_FULLY_QUALIFIED:
        raise BuildError("emoji-test.txt has %d fully-qualified lines, expected %d"
                         % (fully_qualified, EXPECTED_FULLY_QUALIFIED))

    listing_bytes = _read_cached(cache_dir, CACHE_LISTING)
    pngs = twemoji_png_listing(_json(listing_bytes, "the Twemoji file listing"))
    tile_names = needed_tile_names(lines, pngs)

    tarball = _read_cached(cache_dir, CACHE_TARBALL)
    members = read_tgz_members(tarball)
    for member in (SHORTCODE_MEMBER, SHORTCODE_LICENSE_MEMBER):
        if member not in members:
            raise BuildError("the %s tarball has no %s" % (SHORTCODE_PACKAGE, member))
    module = members[SHORTCODE_MEMBER]
    shortcodes = merge_extra_shortcodes(
        parse_shortcode_module(_utf8(module, SHORTCODE_MEMBER)))
    license_text = _utf8(members[SHORTCODE_LICENSE_MEMBER], SHORTCODE_LICENSE_MEMBER)
    license_text = license_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if (len(license_text) > 4000 or not license_text.isascii()
            or not all(ch == "\n" or ch.isprintable() for ch in license_text)
            or "MIT License" not in license_text
            or "Permission is hereby granted" not in license_text):
        raise BuildError("the %s LICENSE is not the MIT text that was expected"
                         % SHORTCODE_PACKAGE)

    record = {
        "emoji_test": {
            "url": EMOJI_TEST_URL, "license": "Unicode-3.0",
            "bytes": len(test_bytes), "sha256": _sha256(test_bytes),
        },
        "shortcodes": {
            "package": SHORTCODE_PACKAGE, "version": SHORTCODE_VERSION, "license": "MIT",
            "url": NPM_TARBALL_URL, "bytes": len(tarball), "sha256": _sha256(tarball),
            "member": SHORTCODE_MEMBER, "member_sha256": _sha256(module),
        },
        "twemoji": {
            "repository": "jdecked/twemoji", "version": TWEMOJI_VERSION,
            "license": "CC-BY-4.0", "url_pattern": TWEMOJI_PNG_URL % "<name>",
            "listing_url": TWEMOJI_LISTING_URL, "listing_bytes": len(listing_bytes),
            "listing_sha256": _sha256(listing_bytes),
        },
    }
    return Inputs(lines, shortcodes, tile_names, pngs, cache_dir / CACHE_PNG_DIR,
                  license_text, record)


def _pillow():
    """Pillow, imported only here (a build-time tool; the app never imports it)."""
    try:
        import PIL
        from PIL import Image, ImageChops, ImageDraw
    except ImportError as exc:
        raise BuildError("--build and --compare need Pillow in this interpreter "
                         "(py -3.12 -m pip install pillow): %s" % exc) from exc
    Image.MAX_IMAGE_PIXELS = 4_000_000
    return PIL, Image, ImageChops, ImageDraw


def render_sheet(table, tile_names, pngs, png_dir) -> tuple:
    """``(RGBA sheet, {"files", "bytes", "manifest_sha256"})`` for a table.

    Every PNG is re-checked against the listing (size, sha256, signature, 72x72)
    before it is decoded.  ``manifest_sha256`` is the sha256 of the sorted lines
    ``"<name>.png <sha256>\\n"`` -- one checksum that pins every tile's input.
    """
    _pil, Image, _chops, _draw = _pillow()
    owners = tile_owners(table)
    sheet = Image.new("RGBA", sheet_size(len(owners)), (0, 0, 0, 0))
    manifest = {}
    total = 0
    for tile, pos in enumerate(owners):
        name = tile_names[pos]
        path = Path(png_dir) / (name + ".png")
        if not path.is_file():
            raise BuildError("%s.png is not in the cache -- run --fetch first" % name)
        data = path.read_bytes()
        size, digest = pngs[name]
        sha = _sha256(data)
        if (len(data) != size or not data.startswith(PNG_SIGNATURE)
                or (digest and sha != digest)):
            raise BuildError("%s.png in the cache does not match the listing" % name)
        try:
            with Image.open(io.BytesIO(data), formats=["PNG"]) as im:
                if im.size != (72, 72):
                    raise BuildError("%s.png is %dx%d, expected 72x72"
                                     % ((name,) + im.size))
                small = im.convert("RGBA").resize((TILE_PX, TILE_PX),
                                                  Image.Resampling.LANCZOS)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
            raise BuildError("%s.png could not be decoded: %s" % (name, exc)) from exc
        sheet.paste(small, tile_origin(tile))
        manifest[name] = sha
        total += len(data)

    # colour under a fully transparent pixel is meaningless: zero it, so the
    # bytes are stable and the palette spends nothing on it
    visible = sheet.getchannel("A").point(lambda v: 255 if v else 0)
    sheet = Image.composite(sheet, Image.new("RGBA", sheet.size, (0, 0, 0, 0)), visible)
    lines = "".join("%s.png %s\n" % (name, manifest[name]) for name in sorted(manifest))
    return sheet, {"files": len(manifest), "bytes": total,
                   "manifest_sha256": _sha256(lines.encode("ascii"))}


def _unpremultiply(palette) -> list:
    out = []
    for i in range(0, len(palette), 4):
        r, g, b, a = palette[i:i + 4]
        if a == 0:
            out += [0, 0, 0, 0]
        else:
            out += [min(255, (c * 255 + a // 2) // a) for c in (r, g, b)] + [a]
    return out


def quantise_sheet(sheet):
    """The sheet as ONE 256-entry palette image whose palette carries alpha.

    Pillow's only RGBA quantiser (fast octree) run over the whole sheet smears
    flags and speckles dark outlines, and it turns opaque interiors faintly
    translucent.  Measured against the RGBA sheet on Discord's dark background
    (2026-10-09, rms error per channel): 9.4 for that, 6.3 for this, which treats
    the three kinds of pixel separately:

      entry 0            fully transparent.  Alpha <= ALPHA_SNAP_LOW is zeroed
                         first, but the real floor is about 32: see the note
                         at ALPHA_SNAP_LOW (the edge octree's own transparent
                         entry takes most pixels fainter than that);
      next 144 entries   opaque colours (alpha >= ALPHA_SNAP_HIGH becomes 255),
                         picked by median cut + k-means over the opaque pixels;
      last 111 entries   the antialiased edges, picked by the octree in
                         PREMULTIPLIED space, where an error in a faint pixel's
                         colour counts for as little as it shows.

    So a transparent pixel stays exactly transparent and an opaque one exactly
    opaque -- both are checked by the caller.  (A FAINT pixel may also end up
    transparent, as above; that is by measurement, not by promise.)
    """
    _pil, Image, ImageChops, _draw = _pillow()
    low, high = ALPHA_SNAP_LOW, ALPHA_SNAP_HIGH
    alpha = sheet.getchannel("A").point(
        [0 if v <= low else 255 if v >= high else v for v in range(256)])
    opaque_mask = alpha.point(lambda v: 255 if v == 255 else 0)
    clear_mask = alpha.point(lambda v: 255 if v == 0 else 0)

    opaque = Image.composite(sheet.convert("RGB"),
                             Image.new("RGB", sheet.size, (0, 0, 0)), opaque_mask)
    opaque = opaque.quantize(PALETTE_OPAQUE, method=Image.Quantize.MEDIANCUT,
                             kmeans=1, dither=Image.Dither.NONE)
    opaque_palette = opaque.getpalette("RGB")

    edges = sheet.copy()
    edges.putalpha(alpha)
    edges = Image.composite(Image.new("RGBA", sheet.size, (0, 0, 0, 0)), edges,
                            ImageChops.lighter(opaque_mask, clear_mask))
    edges = Image.frombytes("RGBA", sheet.size, edges.convert("RGBa").tobytes())
    edges = edges.quantize(PALETTE_EDGES, method=Image.Quantize.FASTOCTREE,
                           dither=Image.Dither.NONE)
    edge_palette = _unpremultiply(edges.getpalette("RGBA"))
    if len(opaque_palette) > 3 * PALETTE_OPAQUE or len(edge_palette) > 4 * PALETTE_EDGES:
        raise BuildError("a quantiser returned more colours than it was asked for")

    first_edge = 1 + PALETTE_OPAQUE
    opaque_index = Image.frombytes("L", sheet.size, opaque.tobytes()).point(
        lambda v: min(255, v + 1))
    edge_index = Image.frombytes("L", sheet.size, edges.tobytes()).point(
        lambda v: min(255, v + first_edge))
    index = Image.composite(opaque_index, edge_index, opaque_mask)
    index = Image.composite(Image.new("L", sheet.size, 0), index, clear_mask)

    palette = [0, 0, 0, 0]
    for i in range(0, len(opaque_palette), 3):
        palette += opaque_palette[i:i + 3] + [255]
    palette += [0, 0, 0, 0] * (first_edge - len(palette) // 4)
    palette += edge_palette
    palette += [0, 0, 0, 0] * (256 - len(palette) // 4)
    out = Image.frombytes("P", sheet.size, index.tobytes())
    out.putpalette(bytes(palette), "RGBA")
    return out


def _png_chunk(tag, body) -> bytes:
    return (len(body).to_bytes(4, "big") + tag + body
            + zlib.crc32(tag + body).to_bytes(4, "big"))


def write_palette_png(width, height, indices, palette_rgba) -> bytes:
    """An 8-bit palette PNG (colour type 3, ``PLTE`` + ``tRNS``), not interlaced,
    with EVERY scanline stored unfiltered (filter type 0).

    Written here, with ``zlib``, rather than by the imaging library, because that
    layout is part of the pack's contract: the application never gives Tk the
    whole sheet (see the module docstring).  It inflates the rows once and cuts
    each tile out as its own tiny PNG -- and with filter 0 on every row a tile is
    a plain slice of the inflated bytes, with no unfiltering to undo.

    ``indices`` is ``width * height`` palette indices, row by row; ``palette_rgba``
    is 256 entries of r, g, b, a.  One IDAT, deflate level 9: the same bytes for
    the same input on a given zlib.
    """
    if len(indices) != width * height or len(palette_rgba) != 1024:
        raise BuildError("the palette sheet does not have the size it claims")
    rows = b"".join(b"\x00" + indices[y * width:(y + 1) * width] for y in range(height))
    plte = bytes(v for i in range(0, 1024, 4) for v in palette_rgba[i:i + 3])
    trns = bytes(palette_rgba[3::4])
    packer = zlib.compressobj(9, zlib.DEFLATED, 15, 9)
    return (PNG_SIGNATURE
            + _png_chunk(b"IHDR", width.to_bytes(4, "big") + height.to_bytes(4, "big")
                         + bytes((8, 3, 0, 0, 0)))
            + _png_chunk(b"PLTE", plte)
            + _png_chunk(b"tRNS", trns)
            + _png_chunk(b"IDAT", packer.compress(rows) + packer.flush())
            + _png_chunk(b"IEND", b""))


def encode_sheet(sheet, palette) -> tuple:
    """``(PNG bytes, the sheet as it will decode -- RGBA)`` for ``"256"`` or ``"rgba"``.

    ``256`` -- the SHIPPED format -- quantises the whole sheet to one 256-entry
    palette that carries alpha and writes it with ``write_palette_png``.  A pixel
    that was fully transparent must still be fully transparent afterwards (the
    1 px ring between tiles depends on it) and an opaque pixel must still be
    opaque.  ``rgba`` exists for the size and quality comparison only.
    """
    _pil, Image, ImageChops, _draw = _pillow()
    if palette == "rgba":
        buf = io.BytesIO()
        sheet.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), sheet
    if palette != "256":
        raise BuildError("unknown palette mode %r" % (palette,))
    out = quantise_sheet(sheet)
    shown = out.convert("RGBA")
    before, after = sheet.getchannel("A"), shown.getchannel("A")
    was_clear = before.point(lambda v: 0 if v else 255)
    if ImageChops.multiply(was_clear, after).getbbox() is not None:
        raise BuildError("quantising made a fully transparent pixel visible")
    was_opaque = before.point(lambda v: 255 if v == 255 else 0)
    if ImageChops.multiply(was_opaque, ImageChops.invert(after)).getbbox() is not None:
        raise BuildError("quantising made an opaque pixel translucent")
    data = write_palette_png(out.width, out.height, out.tobytes(),
                             list(out.getpalette("RGBA")))
    # the bytes just written must decode to exactly the image that was checked
    with Image.open(io.BytesIO(data), formats=["PNG"]) as back:
        if back.convert("RGBA").tobytes() != shown.tobytes():
            raise BuildError("the written sheet does not decode to the quantised image")
    return data, shown


# Licence statements for NOTICE.txt, VERBATIM from their sources (read
# 2026-10-09).  A licence notice is not paraphrased: change these only by
# re-reading the source.  "(c)" stands for the copyright sign and "(-)" for an
# en dash, so this file stays pure ASCII.
#
# https://www.unicode.org/license.txt -- the whole file, line for line.
UNICODE_LICENSE_V3_LINES = (
    "UNICODE LICENSE V3",
    "",
    "COPYRIGHT AND PERMISSION NOTICE",
    "",
    "Copyright (c) 1991-2026 Unicode, Inc.",
    "",
    "NOTICE TO USER: Carefully read the following legal agreement. BY",
    "DOWNLOADING, INSTALLING, COPYING OR OTHERWISE USING DATA FILES, AND/OR",
    "SOFTWARE, YOU UNEQUIVOCALLY ACCEPT, AND AGREE TO BE BOUND BY, ALL OF THE",
    "TERMS AND CONDITIONS OF THIS AGREEMENT. IF YOU DO NOT AGREE, DO NOT",
    "DOWNLOAD, INSTALL, COPY, DISTRIBUTE OR USE THE DATA FILES OR SOFTWARE.",
    "",
    "Permission is hereby granted, free of charge, to any person obtaining a",
    'copy of data files and any associated documentation (the "Data Files") or',
    'software and any associated documentation (the "Software") to deal in the',
    "Data Files or Software without restriction, including without limitation",
    "the rights to use, copy, modify, merge, publish, distribute, and/or sell",
    "copies of the Data Files or Software, and to permit persons to whom the",
    "Data Files or Software are furnished to do so, provided that either (a)",
    "this copyright and permission notice appear with all copies of the Data",
    "Files or Software, or (b) this copyright and permission notice appear in",
    "associated Documentation.",
    "",
    'THE DATA FILES AND SOFTWARE ARE PROVIDED "AS IS", WITHOUT WARRANTY OF ANY',
    "KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF",
    "MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT OF",
    "THIRD PARTY RIGHTS.",
    "",
    "IN NO EVENT SHALL THE COPYRIGHT HOLDER OR HOLDERS INCLUDED IN THIS NOTICE",
    "BE LIABLE FOR ANY CLAIM, OR ANY SPECIAL INDIRECT OR CONSEQUENTIAL DAMAGES,",
    "OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS,",
    "WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION,",
    "ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THE DATA",
    "FILES OR SOFTWARE.",
    "",
    "Except as contained in this notice, the name of a copyright holder shall",
    "not be used in advertising or otherwise to promote the sale, use or other",
    "dealings in these Data Files or Software without prior written",
    "authorization of the copyright holder.",
)
# https://raw.githubusercontent.com/jdecked/twemoji/v16.0.1/README.md, the two
# licence lines of its "License" section.
TWEMOJI_README_LICENSE_LINES = (
    "Code licensed under the MIT License: <http://opensource.org/licenses/MIT>",
    "Graphics licensed under CC-BY 4.0: <https://creativecommons.org/licenses/by/4.0/>",
)
# https://raw.githubusercontent.com/jdecked/twemoji/v16.0.1/LICENSE, its two
# copyright lines.
TWEMOJI_COPYRIGHT_LINES = (
    "Copyright (c) 2022(-)present Jason Sofonia & Justine De Caires",
    "Copyright (c) 2014(-)2021 Twitter",
)


def notice_text(shortcode_license, palette) -> str:
    """``assets/emoji/NOTICE.txt`` -- the attributions that travel with the tiles."""
    c = chr(0xA9)
    changes = ("Changes made: every 72x72 graphic was resized to %dx%d pixels and all of "
               "them were combined into one sprite sheet" % (TILE_PX, TILE_PX))
    changes += (", which was then reduced to one shared 256-colour palette."
                if palette == "256" else ".")
    return "\n".join([
        "FCTool emoji pack - third-party notices",
        "=======================================",
        "",
        "The sprite sheet in this folder (twemoji_22.png) and the lookup table shipped",
        "beside the application (emoji_table.json.gz) are built by",
        "tools/build_emoji_pack.py from the three sources below.  SOURCE.json records",
        "their versions and checksums.",
        "",
        "",
        "1. Emoji artwork - Twemoji (CC-BY 4.0)",
        "--------------------------------------",
        "",
        "Emoji artwork: Twemoji, %s Twitter, Inc and contributors / jdecked/twemoji," % c,
        "CC-BY 4.0, resized.",
        "",
        "Source: https://github.com/jdecked/twemoji  (tag v%s, assets/72x72)" % TWEMOJI_VERSION,
        "",
        "That repository's README.md states, under \"License\":",
        "",
    ] + ["    " + line for line in TWEMOJI_README_LICENSE_LINES] + [
        "",
        "and its LICENSE file names the copyright holders:",
        "",
    ] + ["    " + line.replace("(-)", chr(0x2013)) for line in TWEMOJI_COPYRIGHT_LINES] + [
        "",
        "Full text of the graphics licence:",
        "https://github.com/jdecked/twemoji/blob/v%s/LICENSE-GRAPHICS" % TWEMOJI_VERSION,
        "",
    ] + textwrap.wrap(changes, 79) + [
        "",
        "",
        "2. Emoji names and sequences - Unicode emoji-test.txt %s (Unicode License v3)"
        % EMOJI_VERSION,
        "-----------------------------------------------------------------------------",
        "",
        "Source: %s" % EMOJI_TEST_URL,
        "That file's own notice: %s 2024 Unicode%s, Inc.  For terms of use and license,"
        % (c, chr(0xAE)),
        "see https://www.unicode.org/terms_of_use.html",
        "",
        "The licence, verbatim from https://www.unicode.org/license.txt:",
        "",
    ] + [line.replace("(c)", c) for line in UNICODE_LICENSE_V3_LINES] + [
        "",
        "",
        "3. Shortcode names - npm %s %s (MIT)" % (SHORTCODE_PACKAGE, SHORTCODE_VERSION),
        "-----------------------------------------------",
        "",
        "Source: %s" % NPM_TARBALL_URL,
        "",
        shortcode_license,
        "",
    ])


def _source_text(source) -> str:
    return json.dumps(source, indent=2, sort_keys=True) + "\n"


def _inside(path, root) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return False
    return True


#: The input checksums ``--build`` compares with the committed SOURCE.json:
#: (section, key, what to call it).
PINNED_INPUT_HASHES = (
    ("emoji_test", "sha256", "emoji-test.txt"),
    ("shortcodes", "sha256", "the discord-emoji tarball"),
    ("shortcodes", "member_sha256", "the shortcode module inside the tarball"),
    ("twemoji", "listing_sha256", "the Twemoji file listing"),
    ("twemoji", "manifest_sha256", "the Twemoji PNGs"),
)


def changed_inputs(record, committed) -> list:
    """Sentences naming every input whose checksum differs from ``committed``
    (the parsed SOURCE.json of the pack in the repository); ``[]`` when all match.

    The inputs are pinned by URL and version only -- a CDN could serve other
    bytes under the same name.  This is what notices.
    """
    out = []
    old_inputs = committed.get("inputs") if isinstance(committed, dict) else None
    if not isinstance(old_inputs, dict):
        return ["the committed SOURCE.json records no inputs to compare with"]
    for section, key, label in PINNED_INPUT_HASHES:
        old = old_inputs.get(section)
        old = old.get(key) if isinstance(old, dict) else None
        new = record[section][key]
        if old != new:
            out.append("%s changed: the committed pack was built from %s, the cache "
                       "holds %s" % (label, old, new))
    return out


def _committed_source(repo_root):
    """The repository's SOURCE.json parsed, None when there is none yet."""
    path = Path(repo_root) / REL_SOURCE
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}


def build(cache_dir, out_root, palette="256", skin_tone_tiles=True, out=print,
          strict=False, repo_root=REPO_ROOT) -> dict:
    """Build the pack from the cache and write its four files under ``out_root``.

    ``rgba`` is a comparison build and is refused anywhere inside the repository
    (the shipped sheet is always the palette PNG).  The cache's checksums are
    compared with the repository's SOURCE.json: a difference is a WARNING that
    names the input -- or, with ``strict``, a refusal before anything is written.
    """
    out_root = Path(out_root)
    if palette == "rgba" and _inside(out_root, repo_root):
        raise BuildError("--palette rgba is a comparison build and never goes into the "
                         "repository: give --out a directory outside %s" % repo_root)
    pil, _image, _chops, _draw = _pillow()
    inputs = load_inputs(cache_dir)
    table = build_table(inputs.lines, inputs.shortcodes, skin_tone_tiles=skin_tone_tiles)
    if table["count"] != EXPECTED_COUNT:
        raise BuildError("the table has %d entries, expected %d"
                         % (table["count"], EXPECTED_COUNT))
    bare = [row[1] for row in table["emoji"] if not row[2]]
    if bare:
        raise BuildError("%d emoji have no shortcode: %s" % (len(bare), "; ".join(bare)))
    known = {text_key(row[0]) for row in table["emoji"]}
    stray = sorted(key for key in inputs.shortcodes if key not in known)

    sheet, twemoji = render_sheet(table, inputs.tile_names, inputs.pngs, inputs.png_dir)
    sheet_bytes, _shown = encode_sheet(sheet, palette)
    table_bytes = serialise_table(table)
    total = len(sheet_bytes) + len(table_bytes)
    if total > HARD_BUDGET_BYTES:
        raise BuildError(
            "sheet %d + table %d = %d bytes is above the %d-byte limit%s"
            % (len(sheet_bytes), len(table_bytes), total, HARD_BUDGET_BYTES,
               " -- rebuild with --no-skin-tones" if skin_tone_tiles else ""))

    record = dict(inputs.record)
    record["twemoji"] = dict(record["twemoji"], **twemoji)
    committed = _committed_source(repo_root)
    if committed is None:
        out("note: no %s in the repository yet -- nothing to compare the inputs with"
            % REL_SOURCE)
    else:
        changed = changed_inputs(record, committed)
        for sentence in changed:
            out("WARNING: " + sentence)
        if changed and strict:
            raise BuildError("--strict: %d input(s) differ from the committed %s -- "
                             "nothing was written" % (len(changed), REL_SOURCE))
        if not changed:
            out("inputs: all %d checksums match the committed %s"
                % (len(PINNED_INPUT_HASHES), REL_SOURCE))
    source = {
        "tool": "tools/build_emoji_pack.py",
        "tool_version": TOOL_VERSION,
        "pillow_version": pil.__version__,
        # the sheet (256) and the table are deflated by Python's own zlib
        "zlib_version": zlib.ZLIB_RUNTIME_VERSION,
        "sheet_format": ("PNG, 8-bit palette + tRNS, not interlaced, every scanline "
                         "filter 0" if palette == "256" else "PNG, RGBA (comparison only)"),
        "emoji_version": EMOJI_VERSION,
        "palette": palette,
        "skin_tone_tiles": bool(skin_tone_tiles),
        "counts": {
            "emoji": table["count"],
            "tiles": table["tiles"],
            "fully_qualified": EXPECTED_FULLY_QUALIFIED,
            "letters": 26,
            "alt": len(table["alt"]),
            "shortcodes": sum(len(row[2]) for row in table["emoji"]),
            "extra_shortcodes": len(EXTRA_SHORTCODES),
            "source_sequences_not_in_table": len(stray),
        },
        "inputs": record,
        "outputs": {
            REL_SHEET: {"bytes": len(sheet_bytes), "sha256": _sha256(sheet_bytes)},
            REL_TABLE: {"bytes": len(table_bytes), "sha256": _sha256(table_bytes)},
            "combined_bytes": total,
        },
    }

    _atomic_write(out_root / REL_SHEET, sheet_bytes)
    _atomic_write(out_root / REL_TABLE, table_bytes)
    _atomic_write(out_root / REL_NOTICE,
                  notice_text(inputs.license_text, palette).encode("utf-8"))
    _atomic_write(out_root / REL_SOURCE, _source_text(source).encode("ascii"))

    out("palette %s%s -> %s" % (palette, "" if skin_tone_tiles else " (no skin-tone tiles)",
                                out_root))
    out("  sheet    %10s bytes  %dx%d, %d tiles" % ((format(len(sheet_bytes), ","),)
                                                   + sheet.size + (table["tiles"],)))
    out("  table    %10s bytes  %d emoji, %d alt forms, %d shortcodes"
        % (format(len(table_bytes), ","), table["count"], len(table["alt"]),
           source["counts"]["shortcodes"]))
    verdict = ("within the %s-byte target" % format(SOFT_BUDGET_BYTES, ",")
               if total <= SOFT_BUDGET_BYTES else
               "ABOVE the %s-byte target (limit %s): needs the owner's yes"
               % (format(SOFT_BUDGET_BYTES, ","), format(HARD_BUDGET_BYTES, ",")))
    out("  combined %10s bytes  %s" % (format(total, ","), verdict))
    return source


# Shortcodes of the tiles shown on the --compare sheet: faces, gradients, thin
# outlines, skin tones, flags, glossy objects, flat symbols, the newest emoji.
COMPARE_NAMES = (
    "grinning", "joy", "sob", "pleading_face", "smiling_imp", "clown", "poop", "skull",
    "skull_crossbones", "alien", "robot", "heart", "sparkling_heart", "fire", "sparkles",
    "star", "zap", "rainbow", "sunny", "cloud", "snowflake", "ocean", "thumbsup",
    "thumbsup_tone1", "thumbsup_tone2", "thumbsup_tone3", "thumbsup_tone4",
    "thumbsup_tone5", "wave", "eyes", "family_mwgb", "man_technologist", "ninja",
    "dog", "cat", "unicorn", "butterfly", "peacock", "rose", "evergreen_tree",
    "earth_americas", "full_moon", "apple", "pizza", "beer", "coffee", "soccer", "trophy",
    "guitar", "video_game", "rocket", "airplane", "red_car", "rotating_light", "warning",
    "white_check_mark", "x", "100", "one", "crossed_swords", "shield", "bomb", "gem",
    "moneybag", "bell", "crystal_ball", "flag_us", "flag_gb", "flag_jp", "flag_br",
    "rainbow_flag", "pirate_flag", "regional_indicator_a", "harp", "splatter",
    "black_circle", "white_circle", "new_moon",
)


def compare_image(rgba_sheet, palette_sheet, table, caption=""):
    """A contact sheet on Discord's dark background: for each sample tile the
    256-colour version beside the RGBA one, at 4x (nearest) and at true size."""
    _pil, Image, _chops, ImageDraw = _pillow()
    by_name = {}
    for row in table["emoji"]:
        for name in row[2]:
            by_name.setdefault(name, row[4])
    samples = [(name, by_name[name]) for name in COMPARE_NAMES if name in by_name]
    if len(samples) < 55:
        raise BuildError("only %d of the comparison samples exist in the table"
                         % len(samples))

    zoom = 4
    big = TILE_PX * zoom
    cell_w, cell_h = 2 * big + 28, big + TILE_PX + 40
    cols = 10
    rows = -(-len(samples) // cols)
    top = 44
    canvas = Image.new("RGBA", (cols * cell_w + 12, top + rows * cell_h + 8),
                       COMPARE_BACKGROUND + (255,))
    draw = ImageDraw.Draw(canvas)
    draw.text((8, 6), "Each pair: LEFT = 256-colour palette, RIGHT = RGBA truecolour.  "
                      "Top: 4x (nearest).  Below: true size (22 px).  "
                      "Background #313338.", fill=(220, 221, 222, 255))
    draw.text((8, 22), caption, fill=(160, 163, 168, 255))
    for index, (name, tile) in enumerate(samples):
        x0 = 8 + (index % cols) * cell_w
        y0 = top + (index // cols) * cell_h
        tx, ty = tile_origin(tile)
        box = (tx, ty, tx + TILE_PX, ty + TILE_PX)
        for side, source in enumerate((palette_sheet, rgba_sheet)):
            small = source.crop(box)
            large = small.resize((big, big), Image.Resampling.NEAREST)
            canvas.alpha_composite(large, (x0 + side * (big + 6), y0))
            canvas.alpha_composite(small, (x0 + side * (big + 6) + (big - TILE_PX) // 2,
                                           y0 + big + 4))
        draw.text((x0, y0 + big + TILE_PX + 8), name[:28], fill=(160, 163, 168, 255))
    return canvas.convert("RGB")


def compare(cache_dir, out_path, out=print) -> None:
    """Write the ``--compare`` contact sheet (nothing else is written)."""
    inputs = load_inputs(cache_dir)
    table = build_table(inputs.lines, inputs.shortcodes)
    sheet, _twemoji = render_sheet(table, inputs.tile_names, inputs.pngs, inputs.png_dir)
    palette_bytes, palette_sheet = encode_sheet(sheet, "256")
    rgba_bytes, _same = encode_sheet(sheet, "rgba")
    caption = ("sheet bytes: 256-colour %s, RGBA %s (table adds about %s)"
               % (format(len(palette_bytes), ","), format(len(rgba_bytes), ","),
                  format(len(serialise_table(table)), ",")))
    image = compare_image(sheet, palette_sheet, table, caption)
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    _atomic_write(out_path, buf.getvalue())
    out("wrote %s (%dx%d, %s)" % ((out_path,) + image.size + (caption,)))


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------

def main(argv) -> int:
    parser = argparse.ArgumentParser(
        prog="build_emoji_pack",
        description="Build the bundled emoji sprite sheet and shortcode table.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true",
                      help="print each input's URL and size; download nothing")
    mode.add_argument("--fetch", action="store_true",
                      help="download the inputs into tools/_cache/emoji/")
    mode.add_argument("--build", action="store_true", help="build the pack")
    mode.add_argument("--compare", metavar="OUT.png",
                      help="write a 256-colour vs RGBA contact sheet")
    parser.add_argument(
        "--palette", choices=("256", "rgba"), default="256",
        help="256 is the shipped format; rgba is for comparison and needs --out "
             "outside the repository")
    parser.add_argument(
        "--cache", default=str(CACHE_DIR), metavar="DIR",
        help="download cache (default tools/_cache/emoji/). It holds about 3,800 "
             "small files: point it OUTSIDE any cloud-synced folder")
    parser.add_argument(
        "--out", default=str(REPO_ROOT), metavar="DIR",
        help="--build: the root to write under (default: the repository, i.e. the "
             "shipped paths); a scratch directory for a trial build")
    parser.add_argument(
        "--no-skin-tones", action="store_true",
        help="--build: the over-budget fallback -- a toned emoji reuses its base tile")
    parser.add_argument(
        "--strict", action="store_true",
        help="--build: refuse (instead of warn) when an input's checksum differs "
             "from the committed assets/emoji/SOURCE.json")
    args = parser.parse_args(argv)

    try:
        if args.plan:
            print(format_plan(plan(Net(), _cached_test_lines(args.cache))))
        elif args.fetch:
            fetch(Net(), args.cache)
        elif args.build:
            build(args.cache, args.out, args.palette,
                  skin_tone_tiles=not args.no_skin_tones, strict=args.strict)
        else:
            compare(args.cache, args.compare)
        return 0
    except BuildError as exc:
        print("build_emoji_pack: ERROR - %s" % exc, file=sys.stderr)
        return 2
    except Exception as exc:        # a dev tool ends in one line, never a traceback
        print("build_emoji_pack: ERROR - unexpected %s: %s" % (type(exc).__name__, exc),
              file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
