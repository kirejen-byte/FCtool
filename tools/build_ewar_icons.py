"""Build the bundled EWAR icon sheet: ``assets/ewar/ewar_icons.png`` + NOTICE.

The preview's EWAR popup draws five small EVE module icons.  This DEV-TIME tool
fetches them ONCE from CCP's official image server, treats them so they read at
14 px, and lays them out in one sprite sheet; the application itself never
downloads anything and never imports this file (or Pillow -- a dev-only
dependency, exactly as for the emoji pack, and deliberately absent from
``requirements.txt``).

    py -3.12 tools/build_ewar_icons.py --plan     # print the five URLs, fetch nothing
    py -3.12 tools/build_ewar_icons.py --fetch    # download into tools/_cache/ewar/
    py -3.12 tools/build_ewar_icons.py --build    # write the sheet + NOTICE.txt
    py -3.12 tools/build_ewar_icons.py            # fetch (if needed) then build

THE ON-DISK CONTRACT -- ``ewar_icons.py`` derives every offset from these two
tuples and reads no metadata out of the file, so the layout is the contract:

    rows    = effects, in ``ewar_parse.ICON_ORDER``
    columns = sizes, in ``ewar_icons.ICON_SIZES``
    cell (i, j) is the ``ICON_SIZES[j]`` square whose top-left pixel is
        (sum(ICON_SIZES[:j]), i * max(ICON_SIZES))
    the sheet is ``sum(ICON_SIZES)`` x ``len(ICON_ORDER) * max(ICON_SIZES)``,
    truecolour RGBA (colour type 6, bit depth 8), non-interlaced,
    STRAIGHT alpha -- never premultiplied; the popup premultiplies.

THE SHEET CARRIES GLYPHS ONLY.  The dark chip behind an icon and its red
hairline border are composed in code by the popup, so one cell per effect per
size covers every state.

THE TREATMENT is spec decision D11, which the owner picked from rendered
candidates after finding a plain resize of the 64 px art blurry.  Each 256 px
source is cropped once, then every size is resized from that crop, sharpened
and given richer colour:

    crop     fills the cell; centred halfway between the glyph's outline and
             its visual weight, with a 10% margin.  Centring on the outline
             alone left the neut touching the left edge of its chip and the
             nos touching the top.
    resize   premultiplied Lanczos, so the colour under transparent pixels
             cannot bleed into the glyph's edge.
    sharpen, then colour, then contrast -- on RGB only; alpha keeps its resized
             values, so the glyph's edge does not ring.

The output bytes depend on Pillow's resampler (12.2.0 built the shipped sheet),
and tests/test_ewar_icons.py pins them, so a Pillow upgrade that moves them
shows up as an art change to look at, not as silent drift.

CCP's licence lets it withdraw material on seven days' notice, so the art must
be replaceable WITHOUT a code change: re-run this tool, or drop a sheet of the
same dimensions in place.  ``assets/ewar/NOTICE.txt`` carries the type IDs and
the source digests precisely so the set can be regenerated or swapped by
someone who never reads this file.

Downloaded bytes are UNTRUSTED: each response must be a PNG by signature and
within a size bound, nothing fetched is ever executed, and the cache holds
downloaded files only.  Run the tool in Python's isolated mode when it fetches:

    py -3.12 -I tools/build_ewar_icons.py --fetch
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import os
import ssl
import sys
import time
from urllib.parse import urlsplit

# The two canonical tuples live in the runtime modules; this tool imports them
# rather than keeping a third copy that could drift.  (Isolated mode keeps the
# repository root off sys.path, so put it there explicitly -- our own code, not
# anything that was downloaded.)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ewar_parse import ICON_ORDER          # noqa: E402  (after the path fix)
from ewar_icons import ICON_SIZES          # noqa: E402

#: effect -> (type ID, module whose artwork the effect borrows, sha256 of the
#: source icon as fetched).  The modules are the plainest representative of
#: each family; the icon stands for the EFFECT, not for the exact module that
#: applied it.  They are Tech I on purpose: that art has no Tech II corner
#: wedge.
#:
#: The digest is PINNED and re-checked against every byte this tool reads,
#: fetched or cached, on every run.  Without that, a substituted cache would be
#: baked into the sheet AND into NOTICE.txt's digest list, so the notice would
#: certify art nobody ever verified.  When CCP legitimately reissues an icon
#: the run refuses and says so; ``--accept-changed`` proceeds and prints the
#: new digests to paste in here, in the same commit as the new sheet.
SOURCES = {
    "jam":        (1957,  "Multispectral ECM I",
                   "b7447fe7f88fafc0c3a3839456cc380a0c2582f8c425cc207015b7c08eae879d"),
    "scramble":   (447,   "Warp Scrambler I",
                   "6e801038b83ae3950d43e2706cdc1a11c96a9f4f47dd50005b9d18eefeb03757"),
    "disruption": (3242,  "Warp Disruptor I",
                   "deeb190b45e3c77c643f17706b8795d6bb59cdc51d665f7171cd1df97acfd4ee"),
    "neut":       (533,   "Small Energy Neutralizer I",
                   "cd5566052fc5094e3b7dc8dea2779519a5bddcd9521fb812f5e0c7fdaffd284b"),
    "nos":        (530,   "Small Energy Nosferatu I",
                   "aea46934c2148e5df72974760589b1406d2ca5f581e3eef0ec5512d9d7fc24ad"),
}

IMAGE_HOST = "images.evetech.net"
#: The size asked of the server.  It is part of the cache file name as well as
#: the URL, so a cached icon can only ever be read back at the size it was
#: fetched at.
SOURCE_PX = 256
URL_TEMPLATE = "https://%s/types/%%d/icon?size=%d" % (IMAGE_HOST, SOURCE_PX)
USER_AGENT = "FCTool-build (+https://github.com/kirejen-byte/FCTool)"
HTTP_TIMEOUT_S = 20
HTTP_ATTEMPTS = 3
#: A 256 px type icon is under 10 KB; anything far larger is not what we asked
#: for.
MAX_BYTES = 512 * 1024

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: The treatment (see the module docstring).  These are the values the owner
#: approved; changing any of them changes every cell.
#:
#: A source pixel above this alpha counts towards the glyph's outline, which
#: keeps the soft drop shadow out of the crop.
OUTLINE_ALPHA = 90
#: Clear space kept around the outline, as a fraction of the crop's half-side.
CROP_PAD = 0.10
#: Pillow's two-stage downscale: a box reduction first, leaving at least this
#: ratio for the Lanczos pass.
REDUCING_GAP = 3.0
UNSHARP_RADIUS = 0.8
UNSHARP_PERCENT = 130
COLOUR_FACTOR = 1.25
CONTRAST_FACTOR = 1.12

CACHE_DIR = os.path.join(_ROOT, "tools", "_cache", "ewar")
ASSET_DIR = os.path.join(_ROOT, "assets", "ewar")
SHEET_NAME = "ewar_icons.png"
NOTICE_NAME = "NOTICE.txt"


class BuildError(Exception):
    """Anything that makes the build refuse to write an asset."""


# ----------------------------------------------------------------- the fetch

def url_for(type_id: int) -> str:
    return URL_TEMPLATE % type_id


def _check_url(url: str) -> tuple[str, str]:
    """Split ``url``, refusing anything but HTTPS on the one allowed host."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != IMAGE_HOST:
        raise BuildError("refusing %r: only https://%s is fetched" % (url, IMAGE_HOST))
    target = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    return parts.hostname, target


def fetch_icon(type_id: int, *, connect=None, sleep=time.sleep) -> bytes:
    """The raw PNG bytes of one type icon.  Retries, then gives up loudly.

    ``connect(host)`` is injectable so the fetch can be exercised without a
    network; the default opens a verified TLS connection."""
    url = url_for(type_id)
    host, target = _check_url(url)
    opener = connect or (lambda h: http.client.HTTPSConnection(
        h, timeout=HTTP_TIMEOUT_S, context=ssl.create_default_context()))
    last = None
    for attempt in range(HTTP_ATTEMPTS):
        conn = opener(host)
        try:
            conn.request("GET", target, headers={
                "User-Agent": USER_AGENT,
                "Accept": "image/png",
                "Accept-Encoding": "identity",
            })
            resp = conn.getresponse()
            body = resp.read(MAX_BYTES + 1)
            if resp.status != 200:
                raise BuildError("GET %s answered HTTP %d" % (url, resp.status))
            if len(body) > MAX_BYTES:
                raise BuildError("refusing an oversize response (> %d bytes) from %s"
                                 % (MAX_BYTES, url))
            if not body.startswith(PNG_SIGNATURE):
                raise BuildError("%s did not answer with a PNG" % url)
            return body
        except (OSError, http.client.HTTPException) as exc:
            last = exc
            if attempt + 1 < HTTP_ATTEMPTS:
                sleep(1.0 * (attempt + 1))
        finally:
            try:
                conn.close()
            except OSError:
                pass
    raise BuildError("could not fetch %s: %s" % (url, last))


def cache_path(type_id: int, cache_dir: str = CACHE_DIR) -> str:
    return os.path.join(cache_dir, "type_%d_%d.png" % (type_id, SOURCE_PX))


def _verify(effect: str, blob: bytes, *, accept_changed: bool, fresh: bool) -> str:
    """The sha256 of ``blob``, after checking it against the pinned digest.

    Runs on EVERY byte the build reads, fetched or cached: a cache is just a
    directory on disk, and an unverified one would be baked into the sheet and
    then certified by the digest list in NOTICE.txt.

    ``fresh`` says this blob came off CCP's server in THIS run.  Only a fresh
    blob may be adopted by ``accept_changed``: NOTICE.txt is the licence
    artefact, and it must never put a digest beside CCP's name for bytes that
    were never fetched from CCP.  Otherwise dropping a forged PNG into the
    cache and running ``--build --accept-changed`` would bake the forgery into
    the sheet AND have the notice certify it."""
    type_id, name, expected = SOURCES[effect]
    if not blob.startswith(PNG_SIGNATURE):
        raise BuildError("the source for %s (type %d) is not a PNG" % (effect, type_id))
    digest = hashlib.sha256(blob).hexdigest()
    if digest != expected:
        if not accept_changed:
            raise BuildError(
                "%s (%s, type %d) does not match its pinned digest:\n"
                "    expected %s\n    got      %s\n"
                "Either the cache was substituted, or CCP reissued the icon. "
                "Re-run with --force to re-download; if the new art is what you "
                "want, --accept-changed proceeds and prints the digests to pin."
                % (effect, name, type_id, expected, digest))
        if not fresh:
            raise BuildError(
                "refusing to adopt %s (%s, type %d) from the cache: its bytes "
                "do not match the pinned digest and were not downloaded in "
                "this run, so nothing has seen them come from CCP. NOTICE.txt "
                "would certify them anyway. Re-run with --force."
                % (effect, name, type_id))
        print("  CHANGED  %s: pinned %s, got %s" % (effect, expected, digest))
    return digest


def fetch_all(cache_dir: str = CACHE_DIR, *, force: bool = False,
              download: bool = True, accept_changed: bool = False) -> dict[str, str]:
    """Make sure every source icon is in ``cache_dir`` and verified.  Returns
    effect -> sha256 of the bytes that will be built into the sheet."""
    os.makedirs(cache_dir, exist_ok=True)
    digests = {}
    for effect in ICON_ORDER:
        type_id, name, _expected = SOURCES[effect]
        path = cache_path(type_id, cache_dir)
        fresh = False
        if (force or not os.path.exists(path)) and download:
            print("fetching %s (%s, type %d)" % (effect, name, type_id))
            blob = fetch_icon(type_id)
            with open(path, "wb") as fh:
                fh.write(blob)
            fresh = True
        else:
            if not os.path.exists(path):
                raise BuildError("missing cached source %s -- run --fetch first" % path)
            with open(path, "rb") as fh:
                blob = fh.read()
            print("cached   %s (%s, type %d)" % (effect, name, type_id))
        digests[effect] = _verify(effect, blob, accept_changed=accept_changed,
                                  fresh=fresh)
    return digests


# ----------------------------------------------------------------- the sheet

def sheet_size() -> tuple[int, int]:
    """The sheet's dimensions, derived from the two canonical tuples."""
    return sum(ICON_SIZES), len(ICON_ORDER) * max(ICON_SIZES)


def cell_origin(effect: str, px: int) -> tuple[int, int]:
    """The top-left pixel of one cell -- the same arithmetic the runtime
    decoder does, kept here so the generator cannot disagree with it."""
    row = ICON_ORDER.index(effect)
    col = ICON_SIZES.index(px)
    return sum(ICON_SIZES[:col]), row * max(ICON_SIZES)


def _imaging():
    """Pillow, imported late so that ``--plan`` and ``--fetch`` work without it."""
    try:
        from PIL import Image, ImageEnhance, ImageFilter
    except ImportError as exc:          # pragma: no cover - dev-tool guard
        raise BuildError(
            "Pillow is needed to BUILD the sheet (it is a dev-only dependency "
            "and must not be added to requirements.txt): %s" % exc)
    return Image, ImageEnhance, ImageFilter


def _outline_box(im):
    """The bounding box of the pixels that count as the glyph, or None."""
    return im.getchannel("A").point(
        lambda v: 255 if v > OUTLINE_ALPHA else 0).getbbox()


def _visual_weight(im) -> tuple[float, float]:
    """Where the eye says the glyph is: alpha times brightness per pixel, with
    a small floor so that a dark part of the glyph still counts for something
    and a dim shadow counts for little."""
    width, height = im.size
    pixels = im.load()
    total = sum_x = sum_y = 0.0
    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            v = (a / 255) * (0.15 + (0.3 * r + 0.59 * g + 0.11 * b) / 255)
            total += v
            sum_x += v * x
            sum_y += v * y
    return sum_x / total, sum_y / total


def crop_to_glyph(im):
    """The square of ``im`` that the glyph fills.

    Centred halfway between the outline and the visual weight: the outline
    alone leaves a lopsided glyph looking off-centre, and the weight alone
    shrinks it, because the crop must still reach its far edge.  The half-side
    is measured from that centre to the FARTHEST edge of the outline, so the
    whole outline stays inside, with :data:`CROP_PAD` to spare.  The box may
    run past the source image; Pillow fills that with transparent pixels."""
    box = _outline_box(im)
    if box is None:
        raise BuildError("a source icon has no visible glyph to crop to")
    left, top, right, bottom = box
    weight_x, weight_y = _visual_weight(im)
    centre_x = ((left + right) / 2 + weight_x) / 2
    centre_y = ((top + bottom) / 2 + weight_y) / 2
    half = max(centre_x - left, right - centre_x,
               centre_y - top, bottom - centre_y) * (1 + CROP_PAD)
    return im.crop((int(centre_x - half), int(centre_y - half),
                    int(centre_x + half), int(centre_y + half)))


def render_cell(glyph, px: int):
    """One ``px`` square cell from a cropped glyph, in straight RGBA.

    The resize runs premultiplied so the undefined colour under transparent
    pixels cannot bleed into the edge.  The sharpen and the two colour steps
    then work on RGB alone and the resized alpha is put back untouched."""
    Image, ImageEnhance, ImageFilter = _imaging()
    cell = glyph.convert("RGBa").resize(
        (px, px), Image.LANCZOS, reducing_gap=REDUCING_GAP).convert("RGBA")
    alpha = cell.getchannel("A")
    rgb = cell.convert("RGB").filter(ImageFilter.UnsharpMask(
        radius=UNSHARP_RADIUS, percent=UNSHARP_PERCENT, threshold=0))
    rgb = ImageEnhance.Color(rgb).enhance(COLOUR_FACTOR)
    rgb = ImageEnhance.Contrast(rgb).enhance(CONTRAST_FACTOR)
    out = rgb.convert("RGBA")
    out.putalpha(alpha)
    return out


def build_sheet(cache_dir: str = CACHE_DIR):
    """Compose the sprite sheet as a Pillow RGBA image."""
    Image, _enhance, _filter = _imaging()
    width, height = sheet_size()
    sheet = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for effect in ICON_ORDER:
        type_id, _name, _expected = SOURCES[effect]
        path = cache_path(type_id, cache_dir)
        if not os.path.exists(path):
            raise BuildError("missing cached source %s -- run --fetch first" % path)
        with Image.open(path) as src:
            glyph = crop_to_glyph(src.convert("RGBA"))
        for px in ICON_SIZES:
            sheet.paste(render_cell(glyph, px), cell_origin(effect, px))
    return sheet


def write_sheet(out_dir: str = ASSET_DIR, cache_dir: str = CACHE_DIR) -> str:
    sheet = build_sheet(cache_dir)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, SHEET_NAME)
    # No optimize=, no palette: the reader accepts colour type 6 / depth 8 only.
    sheet.save(path, format="PNG", compress_level=9)
    return path


# ----------------------------------------------------------------- the notice

NOTICE_TEMPLATE = """\
FCTool EWAR icon sheet - third-party notices
============================================

The sprite sheet in this folder ({sheet}) is built by
tools/build_ewar_icons.py from artwork published by CCP hf. on its official
image server, https://images.evetech.net/ .

(c) CCP hf. All rights reserved.

EVE Online and the EVE logo are the registered trademarks of CCP hf. All
rights are reserved worldwide. FCTool is a free fan-made tool; CCP hf. is
not affiliated with it and does not endorse it.

CCP's Developer License Agreement grants the right to use, display and
distribute CCP's Game Data - which includes the images CCP makes available -
within an application, and allows CCP to require that material be removed on
seven days' notice. The art is therefore shipped as a plain asset: it can be
regenerated, replaced or removed WITHOUT a code change, from the facts below
alone.


Sources
-------

Each icon was fetched once, at build time, from

    https://images.evetech.net/types/<type ID>/icon?size={source_px}

and was then altered so that it reads at these small sizes. Each icon was
cropped to fill its cell (centred halfway between the glyph's outline and its
visual weight, with a 10% margin), resized to each of the sizes listed under
"Layout", sharpened, and colour-adjusted (saturation +25%, contrast +12%).
Nothing else was changed and nothing was composed into the art: the dark chip
and the red border drawn around an icon in the application are composed at
paint time and are not part of this artwork.

{sources}

Layout
------

The sheet is {width} x {height} pixels, truecolour RGBA (PNG colour type 6, bit
depth 8), non-interlaced, with STRAIGHT (not premultiplied) alpha.

Rows are the effects, in this order, top to bottom:

{rows}

Columns are the icon sizes, in this order, left to right:

{cols}

Cell (row i, column j) is the square of side SIZE[j] whose top-left pixel is at
x = SIZE[0] + ... + SIZE[j-1], y = i * {cell}. Everything outside a cell is
fully transparent.

To swap the art: write a sheet of exactly these dimensions, in this layout, in
this format, over {sheet}. No code reads anything else out of the file, and a
replacement need not be cropped or adjusted the way this one was.
"""


def notice_text(digests: dict[str, str]) -> str:
    width, height = sheet_size()
    lines = []
    for effect in ICON_ORDER:
        type_id, name, _expected = SOURCES[effect]
        lines.append("  %-11s type ID %-6d %s" % (effect, type_id, name))
        digest = digests.get(effect)
        if digest:
            lines.append("  %-11s sha256  %s" % ("", digest))
    return NOTICE_TEMPLATE.format(
        sheet=SHEET_NAME,
        source_px=SOURCE_PX,
        sources="\n".join(lines),
        width=width,
        height=height,
        cell=max(ICON_SIZES),
        rows="\n".join("  %d. %s" % (i + 1, eff) for i, eff in enumerate(ICON_ORDER)),
        cols="\n".join("  %d. %d px" % (j + 1, px) for j, px in enumerate(ICON_SIZES)),
    )


def write_notice(digests: dict[str, str], out_dir: str = ASSET_DIR) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, NOTICE_NAME)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(notice_text(digests))
    return path


# ------------------------------------------------------------------- the CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plan", action="store_true",
                    help="print the URLs and the layout; fetch nothing")
    ap.add_argument("--fetch", action="store_true", help="download the source icons")
    ap.add_argument("--build", action="store_true", help="write the sheet and the notice")
    ap.add_argument("--force", action="store_true", help="re-download cached sources")
    ap.add_argument("--accept-changed", action="store_true",
                    help="proceed when a source no longer matches its pinned "
                         "digest, and print the new digests to pin; implies "
                         "--force, since only freshly downloaded bytes may be "
                         "adopted")
    ap.add_argument("--cache", default=CACHE_DIR, help="cache directory")
    ap.add_argument("--out", default=ASSET_DIR, help="asset output directory")
    args = ap.parse_args(argv)

    width, height = sheet_size()
    if args.plan:
        print("sheet: %d x %d RGBA, rows %s, columns %s"
              % (width, height, list(ICON_ORDER), list(ICON_SIZES)))
        for effect in ICON_ORDER:
            type_id, name, _expected = SOURCES[effect]
            print("  %-11s %s  <- %s" % (effect, url_for(type_id), name))
        return 0

    do_fetch = args.fetch or not args.build
    do_build = args.build or not args.fetch
    # Adopting a changed source means re-downloading it: --build alone would
    # otherwise adopt whatever is in the cache and have NOTICE.txt certify it
    # as CCP's.  _verify refuses that too; this makes the usual invocation do
    # the right thing instead of merely failing.
    force = args.force or args.accept_changed
    if args.accept_changed:
        do_fetch = True
    try:
        # Verifies every source byte against its pinned digest whether it was
        # just downloaded or came out of the cache, so NOTICE.txt can never
        # certify art that nothing checked.
        digests = fetch_all(args.cache, force=force, download=do_fetch,
                            accept_changed=args.accept_changed)
        if do_build:
            sheet = write_sheet(args.out, args.cache)
            notice = write_notice(digests, args.out)
            print("wrote %s (%d x %d, %d bytes)"
                  % (sheet, width, height, os.path.getsize(sheet)))
            print("wrote %s (%d bytes)" % (notice, os.path.getsize(notice)))
    except BuildError as exc:
        print("build failed: %s" % exc, file=sys.stderr)
        return 1
    except Exception as exc:
        # A poisoned or truncated cache file reaches the imaging library, which
        # raises its own types (UnidentifiedImageError, OSError). A dev tool may
        # fail; it should not fail as a traceback.
        print("build failed: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
