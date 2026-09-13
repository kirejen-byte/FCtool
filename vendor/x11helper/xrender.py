# xrender -- RENDER extension bindings for the vendored python-xlib.
#
# python-xlib 0.33 ships no RENDER support, and the Linux preview helper needs
# exactly seven requests out of it: QueryVersion, QueryPictFormats,
# CreatePicture, ChangePicture, FreePicture, Composite, SetPictureTransform and
# SetPictureFilter.  They are written here by hand against renderproto, in the
# same rq.Struct style as Xlib/ext/damage.py.
#
# This file is NOT part of upstream python-xlib -- it is FCTool's own code and
# carries FCTool's licence, not the LGPL of the tree it sits beside.
#
# Constraints (the helper runs inside a Steam 'sniper' runtime):
#   * Python 3.9 syntax, ASCII source, stdlib + Xlib imports only.
#   * No X server contact at import time; init(display) does the handshake.
#
# Usage:
#     import xrender
#     xrender.init(disp)                       # raises RenderError if absent
#     disp.render_query_version()              # -> (major, minor)
#     formats = disp.render_query_pict_formats()
#     fid = formats.find_standard(24, False)
#     pic = window.render_create_picture(fid, subwindow_mode=1)
#     pic.set_filter(xrender.FilterBilinear)
#     pic.set_transform([[sx, 0, 0], [0, sy, 0], [0, 0, 1]])
#     pic.composite(xrender.PictOpSrc, dst_pic, 0, 0, 0, 0, w, h)
#     pic.free()

from Xlib.protocol import rq

extname = "RENDER"

# The version we ask for.  The CARD32 repeat enum this code uses arrived back
# in RENDER 0.10; 0.11 is requested anyway, and servers answer with their own.
MAJOR_VERSION = 0
MINOR_VERSION = 11

# Minor opcodes (renderproto).
X_RenderQueryVersion = 0
X_RenderQueryPictFormats = 1
X_RenderCreatePicture = 4
X_RenderChangePicture = 5
X_RenderFreePicture = 7
X_RenderComposite = 8
X_RenderSetPictureTransform = 28
X_RenderSetPictureFilter = 30

# PICTFORMINFO type.
PictTypeIndexed = 0
PictTypeDirect = 1

# PictOp.
PictOpClear = 0
PictOpSrc = 1
PictOpDst = 2
PictOpOver = 3
PictOpOverReverse = 4
PictOpIn = 5
PictOpInReverse = 6
PictOpOut = 7
PictOpOutReverse = 8
PictOpAtop = 9
PictOpAtopReverse = 10
PictOpXor = 11
PictOpAdd = 12
PictOpSaturate = 13

# CreatePicture / ChangePicture value mask bits, in wire order.
CPRepeat = 1 << 0
CPAlphaMap = 1 << 1
CPAlphaXOrigin = 1 << 2
CPAlphaYOrigin = 1 << 3
CPClipXOrigin = 1 << 4
CPClipYOrigin = 1 << 5
CPClipMask = 1 << 6
CPGraphicsExposures = 1 << 7
CPSubwindowMode = 1 << 8
CPPolyEdge = 1 << 9
CPPolyMode = 1 << 10
CPDither = 1 << 11
CPComponentAlpha = 1 << 12

# Repeat.
RepeatNone = 0
RepeatNormal = 1
RepeatPad = 2
RepeatReflect = 3

# subwindow-mode (same values as core X).
ClipByChildren = 0
IncludeInferiors = 1

# Filter names.  The server matches these strings verbatim.
FilterNearest = "nearest"
FilterBilinear = "bilinear"
FilterFast = "fast"
FilterGood = "good"
FilterBest = "best"

# The None picture / format.
NONE = 0

PICTURE = rq.Card32
PICTFORMAT = rq.Card32


class RenderError(Exception):
    """RENDER is missing, or a lookup in its reply data found nothing.

    This is a client-side condition, not an X protocol error; protocol errors
    still arrive as Xlib.error.XError subclasses through the normal path.
    """


# --------------------------------------------------------------------------
# 16.16 fixed point
# --------------------------------------------------------------------------

def to_fixed(value):
    """Encode a float as an X FIXED (16.16 signed).  Rounds, never truncates."""
    value = float(value)
    if abs(value) >= 32768.0:
        raise RenderError(
            "value %r is out of range for a 16.16 fixed-point FIXED "
            "(must satisfy abs(value) < 32768)" % (value,))
    return int(round(value * 65536.0))


def from_fixed(value):
    """Decode an X FIXED (16.16 signed) back to a float."""
    return float(value) / 65536.0


# --------------------------------------------------------------------------
# reply structures
# --------------------------------------------------------------------------

# PICTFORMINFO, 28 bytes.
PictFormInfo = rq.Struct(
    PICTFORMAT('id'),
    rq.Card8('type'),
    rq.Card8('depth'),
    rq.Pad(2),
    # DIRECTFORMAT: shift/mask pairs, red green blue alpha.
    rq.Card16('red_shift'),
    rq.Card16('red_mask'),
    rq.Card16('green_shift'),
    rq.Card16('green_mask'),
    rq.Card16('blue_shift'),
    rq.Card16('blue_mask'),
    rq.Card16('alpha_shift'),
    rq.Card16('alpha_mask'),
    # Plain Card32, not rq.Colormap: a Resource field inside a nested Struct
    # blows up under rq's rawdict reply parsing (Resource.parse_value takes no
    # rawdict keyword), and the helper never uses the colormap anyway.
    rq.Card32('colormap'),
)

# PICTVISUAL, 8 bytes.
PictVisual = rq.Struct(
    rq.Card32('visual'),
    PICTFORMAT('format'),
)

# PICTDEPTH: 8 fixed bytes then its visuals.
PictDepth = rq.Struct(
    rq.Card8('depth'),
    rq.Pad(1),
    rq.LengthOf('visuals', 2),
    rq.Pad(4),
    rq.List('visuals', PictVisual, pad=0),
)

# PICTSCREEN: 8 fixed bytes then its depths.
PictScreen = rq.Struct(
    rq.LengthOf('depths', 4),
    PICTFORMAT('fallback'),
    rq.List('depths', PictDepth, pad=0),
)


# Standard picture formats, keyed by (depth, alpha).  Values are
# (red_shift, red_mask, green_shift, green_mask, blue_shift, blue_mask,
#  alpha_shift, alpha_mask) -- PictStandardRGB24 / ARGB32 / A8.
_STANDARD = {
    (24, False): (16, 0xFF, 8, 0xFF, 0, 0xFF, 0, 0x00),
    (32, True): (16, 0xFF, 8, 0xFF, 0, 0xFF, 24, 0xFF),
    (8, True): (0, 0x00, 0, 0x00, 0, 0x00, 0, 0xFF),
}

_DIRECT_FIELDS = ('red_shift', 'red_mask', 'green_shift', 'green_mask',
                  'blue_shift', 'blue_mask', 'alpha_shift', 'alpha_mask')


class PictFormats(object):
    """The parsed QueryPictFormats reply, with the two lookups the helper needs.

    ``formats`` is the flat LISTofPICTFORMINFO; ``screens`` is the per-screen
    depth/visual tree.  Both are the raw parsed entries (indexable by field
    name), so callers can read anything this class does not wrap.
    """

    def __init__(self, formats, screens, subpixels=()):
        self.formats = list(formats)
        self.screens = list(screens)
        self.subpixels = list(subpixels)

    def find_standard(self, depth, alpha=False):
        """Return the PICTFORMAT id of a standard format, or raise RenderError.

        Supported: (24, False) = RGB24, (32, True) = ARGB32, (8, True) = A8.
        The alpha SHIFT is only compared when the alpha mask is non-zero: a
        server is free to report any shift alongside a zero mask.
        """
        key = (int(depth), bool(alpha))
        want = _STANDARD.get(key)
        if want is None:
            raise RenderError(
                "no standard picture format is defined for depth %d alpha=%s"
                % (depth, bool(alpha)))
        for fmt in self.formats:
            if fmt['type'] != PictTypeDirect:
                continue
            if fmt['depth'] != key[0]:
                continue
            got = tuple(fmt[name] for name in _DIRECT_FIELDS)
            if want[7] == 0:
                # Zero alpha mask: the shift carries no meaning.
                if got[:6] == want[:6] and got[7] == 0:
                    return fmt['id']
            elif got == want:
                return fmt['id']
        raise RenderError("server reports no standard depth-%d format (alpha=%s)"
                          % (depth, bool(alpha)))

    def find_for_visual(self, visual_id):
        """Return the PICTFORMAT id the server pairs with an X visual id."""
        for screen in self.screens:
            for depth in screen['depths']:
                for visual in depth['visuals']:
                    if visual['visual'] == visual_id:
                        return visual['format']
        raise RenderError("no picture format for visual 0x%x" % (visual_id,))


# --------------------------------------------------------------------------
# requests
# --------------------------------------------------------------------------

class QueryVersion(rq.ReplyRequest):
    _request = rq.Struct(
        rq.Card8('opcode'),
        rq.Opcode(X_RenderQueryVersion),
        rq.RequestLength(),
        rq.Card32('major_version'),
        rq.Card32('minor_version'),
    )

    _reply = rq.Struct(
        rq.ReplyCode(),
        rq.Pad(1),
        rq.Card16('sequence_number'),
        rq.ReplyLength(),
        rq.Card32('major_version'),
        rq.Card32('minor_version'),
        rq.Pad(16),
    )


class QueryPictFormats(rq.ReplyRequest):
    _request = rq.Struct(
        rq.Card8('opcode'),
        rq.Opcode(X_RenderQueryPictFormats),
        rq.RequestLength(),
    )

    _reply = rq.Struct(
        rq.ReplyCode(),
        rq.Pad(1),
        rq.Card16('sequence_number'),
        rq.ReplyLength(),
        rq.LengthOf('formats', 4),
        rq.LengthOf('screens', 4),
        rq.Card32('num_depths'),
        rq.Card32('num_visuals'),
        rq.LengthOf('subpixels', 4),
        rq.Pad(4),
        rq.List('formats', PictFormInfo, pad=0),
        rq.List('screens', PictScreen, pad=0),
        rq.List('subpixels', rq.Card32Obj, pad=0),
    )


_PICTURE_ATTRS = (
    rq.Card32('repeat'),
    PICTURE('alpha_map'),
    rq.Int16('alpha_x_origin'),
    rq.Int16('alpha_y_origin'),
    rq.Int16('clip_x_origin'),
    rq.Int16('clip_y_origin'),
    rq.Pixmap('clip_mask'),
    rq.Card8('graphics_exposures'),
    rq.Card8('subwindow_mode'),
    rq.Card8('poly_edge'),
    rq.Card8('poly_mode'),
    rq.Card32('dither'),
    rq.Card8('component_alpha'),
)


class CreatePicture(rq.Request):
    _request = rq.Struct(
        rq.Card8('opcode'),
        rq.Opcode(X_RenderCreatePicture),
        rq.RequestLength(),
        PICTURE('picture'),
        rq.Drawable('drawable'),
        PICTFORMAT('format'),
        rq.ValueList('attrs', 4, 0, *_PICTURE_ATTRS),
    )


class ChangePicture(rq.Request):
    _request = rq.Struct(
        rq.Card8('opcode'),
        rq.Opcode(X_RenderChangePicture),
        rq.RequestLength(),
        PICTURE('picture'),
        rq.ValueList('attrs', 4, 0, *_PICTURE_ATTRS),
    )


class FreePicture(rq.Request):
    _request = rq.Struct(
        rq.Card8('opcode'),
        rq.Opcode(X_RenderFreePicture),
        rq.RequestLength(),
        PICTURE('picture'),
    )


class Composite(rq.Request):
    _request = rq.Struct(
        rq.Card8('opcode'),
        rq.Opcode(X_RenderComposite),
        rq.RequestLength(),
        rq.Card8('op'),
        rq.Pad(3),
        PICTURE('src'),
        PICTURE('mask'),
        PICTURE('dst'),
        rq.Int16('src_x'),
        rq.Int16('src_y'),
        rq.Int16('mask_x'),
        rq.Int16('mask_y'),
        rq.Int16('dst_x'),
        rq.Int16('dst_y'),
        rq.Card16('width'),
        rq.Card16('height'),
    )


# Nine FIXED values, row-major.  Signed: a mirrored or translated transform
# needs negative entries.
RenderTransform = rq.Struct(
    rq.Int32('matrix11'), rq.Int32('matrix12'), rq.Int32('matrix13'),
    rq.Int32('matrix21'), rq.Int32('matrix22'), rq.Int32('matrix23'),
    rq.Int32('matrix31'), rq.Int32('matrix32'), rq.Int32('matrix33'),
)


class SetPictureTransform(rq.Request):
    _request = rq.Struct(
        rq.Card8('opcode'),
        rq.Opcode(X_RenderSetPictureTransform),
        rq.RequestLength(),
        PICTURE('picture'),
        rq.Object('transform', RenderTransform),
    )


class SetPictureFilter(rq.Request):
    _request = rq.Struct(
        rq.Card8('opcode'),
        rq.Opcode(X_RenderSetPictureFilter),
        rq.RequestLength(),
        PICTURE('picture'),
        rq.LengthOf('filter', 2),
        rq.Pad(2),
        rq.String8('filter'),
        # LISTofFIXED filter parameters.  Always empty here: none of the
        # filters this helper uses takes parameters.
    )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _pict_id(picture):
    """Accept a Picture object, any resource-ish object, an int, or None.

    Anything carrying an ``id`` attribute (a ``Picture``, or any Xlib
    resource) is a freed resource -- not a bare integer -- when that ``id``
    is None, so that case raises RenderError instead of falling through to
    ``int(None)``'s TypeError.
    """
    if picture is None:
        return NONE
    if hasattr(picture, 'id'):
        pid = picture.id
        if pid is None:
            raise RenderError("picture has already been freed")
        return int(pid)
    return int(picture)


def _flat_transform(matrix):
    """Flatten a 3x3 (nested or flat) of floats into nine FIXED ints."""
    rows = list(matrix)
    if len(rows) == 3 and all(hasattr(r, '__len__') and not isinstance(r, str)
                              for r in rows):
        flat = []
        for row in rows:
            row = list(row)
            if len(row) != 3:
                raise RenderError("transform rows must hold 3 values, got %d"
                                  % (len(row),))
            flat.extend(row)
    else:
        flat = rows
    if len(flat) != 9:
        raise RenderError("transform must be 3x3 (9 values), got %d"
                          % (len(flat),))
    return tuple(to_fixed(v) for v in flat)


# --------------------------------------------------------------------------
# Picture
# --------------------------------------------------------------------------

class Picture(object):
    """A RENDER PICTURE resource.

    Not an Xlib resource subclass on purpose: pictures are not drawables and
    carry no core-protocol methods.
    """

    def __init__(self, display, pid, owns_id=False):
        self.display = display
        self.id = pid
        self._owns_id = owns_id

    def __resource__(self):
        return self.id

    def __repr__(self):
        return "<Picture 0x%x>" % (self.id or 0,)

    def _opcode(self):
        return self.display.get_extension_major(extname)

    def _require_live(self):
        if self.id is None:
            raise RenderError("picture has already been freed")

    def change(self, onerror=None, **attrs):
        """ChangePicture with the same value-mask attributes as create."""
        self._require_live()
        ChangePicture(display=self.display,
                      onerror=onerror,
                      opcode=self._opcode(),
                      picture=self.id,
                      attrs=attrs)

    def set_transform(self, matrix, onerror=None):
        """SetPictureTransform from a 3x3 of floats (16.16 fixed on the wire)."""
        self._require_live()
        SetPictureTransform(display=self.display,
                            onerror=onerror,
                            opcode=self._opcode(),
                            picture=self.id,
                            transform=_flat_transform(matrix))

    def set_filter(self, name, onerror=None):
        """SetPictureFilter -- 'nearest', 'bilinear', 'fast', 'good', 'best'."""
        self._require_live()
        SetPictureFilter(display=self.display,
                         onerror=onerror,
                         opcode=self._opcode(),
                         picture=self.id,
                         filter=name)

    def composite(self, op, dst, src_x, src_y, dst_x, dst_y, width, height,
                  mask=None, mask_x=0, mask_y=0, onerror=None):
        """Composite this picture (the source) onto dst."""
        self._require_live()
        Composite(display=self.display,
                  onerror=onerror,
                  opcode=self._opcode(),
                  op=op,
                  src=self.id,
                  mask=_pict_id(mask),
                  dst=_pict_id(dst),
                  src_x=src_x, src_y=src_y,
                  mask_x=mask_x, mask_y=mask_y,
                  dst_x=dst_x, dst_y=dst_y,
                  width=width, height=height)

    def free(self, onerror=None):
        """FreePicture, and release the resource id.  Safe to call twice."""
        if self.id is None:
            return
        pid = self.id
        self.id = None
        FreePicture(display=self.display,
                    onerror=onerror,
                    opcode=self.display.get_extension_major(extname),
                    picture=pid)
        if self._owns_id:
            self.display.free_resource_id(pid)


# --------------------------------------------------------------------------
# bound methods
# --------------------------------------------------------------------------

def query_version(self, major=MAJOR_VERSION, minor=MINOR_VERSION):
    """Display method: negotiate the RENDER version.  Returns (major, minor)."""
    reply = QueryVersion(display=self.display,
                         opcode=self.display.get_extension_major(extname),
                         major_version=major,
                         minor_version=minor)
    return (reply.major_version, reply.minor_version)


def query_pict_formats(self):
    """Display method: return the server's PictFormats."""
    reply = QueryPictFormats(display=self.display,
                             opcode=self.display.get_extension_major(extname))
    return PictFormats(reply.formats, reply.screens, reply.subpixels)


def create_picture(self, format, onerror=None, **attrs):
    """Drawable method: create a Picture over this drawable.

    Keyword attributes are the CreatePicture value list -- the ones the helper
    uses are ``repeat``, ``subwindow_mode`` and ``graphics_exposures``.  A
    keyword left out (or passed as None) is not sent at all, so the server
    keeps its default.
    """
    attrs = dict((k, v) for k, v in attrs.items() if v is not None)
    pid = self.display.allocate_resource_id()
    CreatePicture(display=self.display,
                  onerror=onerror,
                  opcode=self.display.get_extension_major(extname),
                  picture=pid,
                  drawable=self.id,
                  format=format,
                  attrs=attrs)
    return Picture(self.display, pid, owns_id=True)


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------

_DISPLAY_METHODS = (
    ('render_query_version', query_version),
    ('render_query_pict_formats', query_pict_formats),
)

_DRAWABLE_METHODS = (
    ('render_create_picture', create_picture),
)

# extension_add_method('drawable', ...) fans out to these class names.
_DRAWABLE_CLASSES = ('drawable', 'window', 'pixmap')


def init(display, info=None):
    """Register RENDER on an already-constructed Xlib Display.

    Xlib only wires extensions up inside ``Display.__init__``, from its own
    ``Xlib.ext.__extensions__`` table; RENDER is not in it and the vendored tree
    is kept verbatim, so this module is loaded by hand.  That means the
    resource classes must be re-finalized here -- ``extension_add_method``
    alone just fills ``class_extension_dicts``, which nothing reads after
    construction.

    Raises RenderError when the server has no RENDER extension.
    """
    if info is None:
        info = display.query_extension(extname)
    if info is None:
        raise RenderError("the X server does not support the RENDER extension")

    display.display.set_extension_major(extname, info.major_opcode)

    for name, func in _DISPLAY_METHODS:
        if name not in display.display_extension_methods:
            display.extension_add_method('display', name, func)

    for name, func in _DRAWABLE_METHODS:
        already = display.class_extension_dicts.get('drawable', {})
        if name not in already:
            display.extension_add_method('drawable', name, func)

    _finalize_resource_classes(display)

    if extname not in display.extensions:
        display.extensions.append(extname)
    return info


def _finalize_resource_classes(display):
    """Rebuild the resource classes so the added methods are actually bound.

    Mirrors the tail of ``Xlib.display.Display.__init__``, but only for the
    classes this module touches.
    """
    proto = display.display
    for class_name in _DRAWABLE_CLASSES:
        methods = display.class_extension_dicts.get(class_name)
        if not methods:
            continue
        origcls = proto.resource_classes[class_name]
        proto.resource_classes[class_name] = type(origcls.__name__,
                                                  (origcls,), dict(methods))

    # Objects built before this point still carry the old class; the screen
    # roots are the ones a caller is most likely to reuse.
    info = getattr(proto, 'info', None)
    window_class = proto.resource_classes['window']
    for screen in getattr(info, 'roots', ()) or ():
        screen.root = window_class(proto, screen.root.id)
