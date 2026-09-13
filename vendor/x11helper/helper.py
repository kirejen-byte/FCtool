# helper.py -- FCTool's native Linux X11 preview helper.
#
# FCTool.exe runs under Proton/Wine, where DWM thumbnails are permanent
# E_NOTIMPL stubs and a PE process cannot talk X11.  So FCTool spawns THIS
# script with the Steam runtime's native python3 (CreateProcessW fork+execs a
# non-PE file) and drives it over a localhost TCP control channel.
#
# Technique (EVE Preview Manager's, read from its source):
#
#   * one override-redirect CHILD X window inside each existing FCTool tile
#     (a child rides along when Wine re-stacks the tile; a sibling would be
#     pushed under it every 250 ms retop),
#   * a RENDER Picture on the EVE client's whole window with
#     subwindow_mode=IncludeInferiors and the "bilinear" filter,
#   * per frame: fresh GetGeometry(src) -> SetPictureTransform(diag(sx, sy)) ->
#     Composite(PictOpSrc) -> DamageSubtract,
#   * updates driven by XDamage RawRectangles, never a timer,
#   * ZERO pixel bytes cross the wire: no GetImage, no readback, ever.
#
# Constraints (Steam 'sniper' runtime ships Python 3.9):
#
#   * Python 3.9 syntax and APIs only -- no match, no runtime X | Y unions, no
#     dataclass(slots=), no zip(strict=), no itertools.pairwise, no bit_count,
#   * stdlib + the vendored Xlib + xrender + x11_thumbs_proto, nothing else,
#   * ASCII source and ASCII log lines (stderr is CLOSED under Wine anyway),
#   * NOTHING touches X at import time: Xlib is imported lazily inside
#     XSession.open() so this module imports (and unit-tests) on Windows.
#
# Launched as (Task 6 owns the exact string):
#     python3 -c "import sys; sys.path.insert(0, sys.argv[-1]); \
#                 import helper; helper.main(sys.argv[1:])" \
#             --host 127.0.0.1 --port N --token T --vendor /path/x11helper.zip
#
# Exit codes: 0 normal/quit/EOF, 1 control channel unusable, 2 bad args or a
# Python older than 3.9, 3 no X display (an X authorisation failure lands HERE
# too: python-xlib folds a missing or stale cookie into DisplayConnectionError,
# so the helper reports code "no_display" and quotes the server's own wording --
# "authentication", "cookie" -- in the message), 4 missing RENDER/DAMAGE/SHAPE,
# 5 X authorisation failure -- RESERVED and unreachable today, kept so the
# number is never reused.

import os
import select
import socket
import sys
import time

import x11_thumbs_proto as proto


__all__ = ["main", "XSession", "Thumb", "HelperCore", "SocketTransport",
           "build_hello", "parse_args"]

# ---- exit codes ----------------------------------------------------------

EXIT_OK = 0
EXIT_NO_TRANSPORT = 1
EXIT_BAD_ARGS = 2
EXIT_NO_DISPLAY = 3
EXIT_NO_EXTENSION = 4
#: Unreachable in practice (see the exit-code table above); never reuse the 5.
EXIT_AUTH = 5

# ---- tunables ------------------------------------------------------------

#: Floor for the per-thumb composite interval when 'attach' names a silly one.
MIN_INTERVAL_FLOOR_MS = 1
DEFAULT_MIN_INTERVAL_MS = 33
#: 'attach' may send 0 to mean "no heartbeat at all" (damage-driven only).
DEFAULT_HEARTBEAT_MS = 500
HEARTBEAT_DISABLED_MS = 0

#: 'stats' cadence, and the longest the select() loop ever sleeps.
STATS_INTERVAL_S = 5.0
MAX_SELECT_TIMEOUT_S = 0.25

#: Probe mode's helper-owned test window.
PROBE_W = 320
PROBE_H = 180
PROBE_MAX_SECONDS = 30

CONNECT_TIMEOUT_S = 10.0
RECV_CHUNK = 65536

_USAGE = ("usage: helper.py --host H --port N --token T "
          "[--vendor PATH] [--display D]")


def _log(text):
    """Write one ASCII line to stderr.  Never raises: under Wine a GUI
    parent's child has stdio CLOSED, so every write here can fail."""
    try:
        sys.stderr.write("[fctool-x11] " + str(text) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


# ==========================================================================
# errors
# ==========================================================================

class XCallError(Exception):
    """An X protocol error attributed to one call.

    ``code`` is a protocol error code from ``x11_thumbs_proto.ERROR_CODES``
    ("bad_window" for BadWindow/BadDrawable/BadPixmap, "x_error" otherwise),
    so HelperCore can forward it to FCTool without translating again.
    """

    def __init__(self, msg, code="x_error", resource=0):
        Exception.__init__(self, msg)
        self.msg = str(msg)
        self.code = code if code in proto.ERROR_CODES else "x_error"
        self.resource = int(resource or 0)


class NoDisplayError(Exception):
    """No usable X display (DISPLAY unset, socket refused, name garbage)."""


class AuthError(Exception):
    """The X server refused our credentials (xauth cookie missing/stale)."""


class MissingExtensionError(Exception):
    """A required X extension is absent.  ``code`` is a proto error code."""

    def __init__(self, msg, code="internal"):
        Exception.__init__(self, msg)
        self.msg = str(msg)
        self.code = code if code in proto.ERROR_CODES else "internal"


class UsageError(Exception):
    """Bad command line."""


#: Exception CLASS NAMES that mean the X connection itself is gone.  Matched by
#: name on purpose: helper.py must not import Xlib (nothing may touch X at
#: import time), and python-xlib can raise these from ANY call once the server
#: hangs up.  Everything else is one thumbnail's problem, never the helper's.
DISPLAY_DEAD_ERRORS = ("ConnectionClosedError", "DisplayConnectionError")


def _is_display_dead(exc):
    """True when ``exc`` means the X server connection has died."""
    for klass in type(exc).__mro__:
        if klass.__name__ in DISPLAY_DEAD_ERRORS:
            return True
    return False


def _as_internal(exc, where):
    """Wrap a non-X exception so the per-thumb teardown reports 'internal'."""
    return XCallError("%s: %s: %s" % (where, type(exc).__name__, exc),
                      "internal")


# ==========================================================================
# argument parsing
# ==========================================================================

class Options(object):
    """Parsed command line.  argparse is avoided on purpose: it prints to
    stdout/stderr (closed under Wine) and exits the process itself."""

    def __init__(self):
        self.host = "127.0.0.1"
        self.port = 0
        self.token = ""
        self.vendor = ""
        self.display = None


def parse_args(argv):
    """Parse the helper's argv (without argv[0]).  Raises UsageError.

    Every flag takes a value: probing is a 'probe' MESSAGE, so there is no
    --probe switch to parse (FCTool never passed one).
    """
    opts = Options()
    items = list(argv or ())
    index = 0
    seen_port = False
    seen_token = False
    while index < len(items):
        arg = items[index]
        if index + 1 >= len(items):
            raise UsageError("%s needs a value\n%s" % (arg, _USAGE))
        value = items[index + 1]
        index += 2
        if arg == "--host":
            opts.host = value
        elif arg == "--port":
            try:
                opts.port = int(value)
            except (TypeError, ValueError):
                raise UsageError("--port must be a number\n%s" % _USAGE)
            seen_port = True
        elif arg == "--token":
            opts.token = value
            seen_token = True
        elif arg == "--vendor":
            opts.vendor = value
        elif arg == "--display":
            opts.display = value
        else:
            raise UsageError("unknown argument %s\n%s"
                             % (arg[:40], _USAGE))
    if not seen_port or not 0 < opts.port < 65536:
        raise UsageError("--port is required and must be 1..65535\n%s" % _USAGE)
    if not seen_token or not opts.token:
        raise UsageError("--token is required\n%s" % _USAGE)
    return opts


# ==========================================================================
# the X session
# ==========================================================================

class XSession(object):
    """Everything that touches the X server, behind ~25 small methods.

    HelperCore talks only to this object, so the whole orchestration layer is
    unit-testable on Windows against a recording fake.

    ERROR MODEL (two paths, deliberately):

    * **Setup and teardown calls** (create_child, shapes, map/unmap/configure,
      picture creation, damage create/destroy) run through ``_checked``:
      the call is issued, then an explicit ``sync()`` waits for the server, and
      any error that arrived in that window is raised here as ``XCallError``.
      One round trip -- these happen a handful of times per tile, never per
      frame.
    * **Per-frame calls** (set_transform, composite, damage_subtract,
      move_resize) are fire-and-forget: syncing on every frame would serialise
      the client against the server and throw EPM's whole no-readback win away.
      Their errors arrive asynchronously at the global error handler installed
      in ``open()`` and are delivered by ``next_events()`` as
      ``("error", code, resource)`` tuples; HelperCore matches the resource id
      against its thumbs and tears down just that one.
    """

    def __init__(self):
        self.display = None
        self.display_name = ""
        self._formats = None
        self._errors = []
        # Lazily imported modules (see open()).
        self._X = None
        self._xerror = None
        self._damage = None
        # The event code DAMAGE was registered under (see open()); damage
        # events are matched on THIS, never on a class -- see next_events().
        self._damage_code = None
        self._damage_cls_name = "DamageNotify"
        self._shape = None
        self._composite = None
        self._xrender = None

    # ---- lifecycle ------------------------------------------------------

    def open(self, display_name=None):
        """Connect, verify the extensions, register RENDER, install the error
        handler.  Raises NoDisplayError / AuthError / MissingExtensionError."""
        try:
            from Xlib import X as X_mod
            from Xlib import display as display_mod
            from Xlib import error as error_mod
            from Xlib.ext import composite as composite_mod
            from Xlib.ext import damage as damage_mod
            from Xlib.ext import shape as shape_mod
            import xrender as xrender_mod
        except ImportError as exc:
            raise NoDisplayError("cannot import the vendored Xlib: %s" % (exc,))

        self._X = X_mod
        self._xerror = error_mod
        self._damage = damage_mod
        self._shape = shape_mod
        self._composite = composite_mod
        self._xrender = xrender_mod

        try:
            disp = display_mod.Display(display_name)
        except (error_mod.XauthError, error_mod.XNoAuthError) as exc:
            raise AuthError("X authorisation failed: %s" % (exc,))
        except (error_mod.DisplayNameError,
                error_mod.DisplayConnectionError) as exc:
            raise NoDisplayError("cannot open the X display: %s" % (exc,))
        except OSError as exc:
            raise NoDisplayError("cannot open the X display: %s" % (exc,))

        self.display = disp
        try:
            self.display_name = str(disp.get_display_name())
        except Exception:
            self.display_name = str(display_name or os.environ.get("DISPLAY", ""))

        if not disp.has_extension("DAMAGE"):
            raise MissingExtensionError("the X server has no DAMAGE extension",
                                        "no_damage")
        self._damage_code = self._query_damage_code(disp)
        if self._damage_code is None:
            raise MissingExtensionError(
                "the X server registered no DamageNotify event code",
                "no_damage")
        if not disp.has_extension("SHAPE"):
            raise MissingExtensionError("the X server has no SHAPE extension",
                                        "internal")
        try:
            xrender_mod.init(disp)
        except xrender_mod.RenderError as exc:
            raise MissingExtensionError(str(exc), "no_render")

        try:
            self._formats = disp.render_query_pict_formats()
        except Exception as exc:
            raise MissingExtensionError("RENDER QueryPictFormats failed: %s"
                                        % (exc,), "no_render")

        disp.set_error_handler(self._on_async_error)
        return self

    def _query_damage_code(self, disp):
        """The wire event code the server's DAMAGE extension was given.

        python-xlib does NOT deliver ``Xlib.ext.damage.DamageNotify``
        instances: ``Display.extension_add_event()`` registers a CLONE of the
        class (``type(evt.__name__, evt.__bases__, evt.__dict__.copy())`` with
        a fresh ``_code``) and the protocol decoder builds every event from
        that clone, so ``isinstance(ev, damage.DamageNotify)`` is ALWAYS False.
        The event code is the only stable identity -- this reads it back out of
        the ``extension_event`` DictWrapper the registration populated, and
        falls back to asking the server for the extension's first event code.

        Returns None when neither source can name it.
        """
        code = None
        try:
            code = getattr(disp.extension_event, self._damage_cls_name)
        except Exception:
            code = None
        if code is None:
            try:
                info = disp.query_extension("DAMAGE")
            except Exception:
                info = None
            first = getattr(info, "first_event", None)
            if first is not None:
                code = first + self._damage.DamageNotifyCode
        if code is None:
            return None
        try:
            return int(code) & 0x7f
        except (TypeError, ValueError):
            return None

    def close(self):
        disp, self.display = self.display, None
        if disp is not None:
            try:
                disp.close()
            except Exception:
                pass

    def fileno(self):
        return self.display.fileno()

    def flush(self):
        try:
            self.display.flush()
        except Exception as exc:
            _log("flush failed: %s" % (exc,))

    def sync(self):
        try:
            self.display.sync()
        except Exception as exc:
            _log("sync failed: %s" % (exc,))

    # ---- facts ----------------------------------------------------------

    def screen_info(self):
        screen = self.display.screen()
        return {"w": int(screen.width_in_pixels),
                "h": int(screen.height_in_pixels),
                "depth": int(screen.root_depth)}

    def root(self):
        """The default screen's root window id (probe mode's parent)."""
        return int(self.display.screen().root.id)

    def extension_versions(self):
        out = {"xrender": [0, 0], "damage": [0, 0],
               "shape": False, "composite": False}
        try:
            major, minor = self.display.render_query_version()
            out["xrender"] = [int(major), int(minor)]
        except Exception as exc:
            _log("RENDER QueryVersion failed: %s" % (exc,))
        try:
            reply = self.display.damage_query_version()
            out["damage"] = [int(reply.major_version), int(reply.minor_version)]
        except Exception as exc:
            _log("DAMAGE QueryVersion failed: %s" % (exc,))
        try:
            out["shape"] = bool(self.display.has_extension("SHAPE"))
            out["composite"] = bool(self.display.has_extension("Composite"))
        except Exception:
            pass
        return out

    # ---- windows --------------------------------------------------------

    def _window(self, xid):
        return self.display.create_resource_object("window", int(xid))

    def create_child(self, parent_xid, x, y, w, h):
        """An override-redirect InputOutput child of ``parent_xid``.

        CopyFromParent depth/visual/class so it always matches the tile, and
        NO background pixmap -- the child must never paint itself, or it would
        flash white between composites.
        """
        parent = self._window(parent_xid)
        X = self._X
        win = self._checked(
            parent.create_window,
            x, y, max(1, int(w)), max(1, int(h)), 0, X.CopyFromParent,
            window_class=X.InputOutput,
            visual=X.CopyFromParent,
            background_pixmap=X.NONE,
            override_redirect=1,
            event_mask=X.StructureNotifyMask)
        return int(win.id)

    def set_input_shape_empty(self, xid):
        """Click-through: an EMPTY input shape, exactly what Wine itself uses
        for WS_EX_TRANSPARENT.  Pointer events fall through to the tile."""
        shape = self._shape
        self._checked(self._window(xid).shape_rectangles,
                      shape.SO.Set, shape.SK.Input, 0, 0, 0, [])

    def map(self, xid):
        self._checked(self._window(xid).map)

    def unmap(self, xid):
        self._checked(self._window(xid).unmap)

    def move_resize(self, xid, x, y, w, h):
        # Per-frame path: fire and forget (see the class docstring).
        self._window(xid).configure(x=int(x), y=int(y),
                                    width=max(1, int(w)),
                                    height=max(1, int(h)))

    def destroy(self, xid):
        self._checked(self._window(xid).destroy)

    def geometry(self, xid):
        """Fresh GetGeometry -> (x, y, w, h, depth).  Never cached: the source
        can be resized at any moment and a stale transform means a stretched
        or cropped thumbnail."""
        try:
            geom = self._window(xid).get_geometry()
        except Exception as exc:
            raise self._as_call_error(exc, xid)
        return (int(geom.x), int(geom.y), int(geom.width), int(geom.height),
                int(geom.depth))

    def viewable(self, xid):
        try:
            attrs = self._window(xid).get_attributes()
        except Exception as exc:
            raise self._as_call_error(exc, xid)
        return attrs.map_state == self._X.IsViewable

    def select_structure(self, xid):
        """StructureNotify on a FOREIGN window.  Event masks are per-client in
        X, so this never disturbs the owner (unlike SubstructureRedirect)."""
        self._checked(self._window(xid).change_attributes,
                      event_mask=self._X.StructureNotifyMask)

    def try_redirect_automatic(self, xid):
        """Ask Composite to redirect this source window.

        A compositing WM/Xwayland has already done this; asking again is
        harmless.  On a bare un-composited X server it is what makes the source
        renderable at all.  BadAccess (someone else owns the redirect) and
        BadMatch are expected and swallowed -- HelperCore is what asks only
        once per source.
        """
        if self._composite is None:
            return False
        try:
            if not self.display.has_extension("Composite"):
                return False
        except Exception:
            return False
        try:
            self._checked(self._window(xid).composite_redirect_window,
                          self._composite.RedirectAutomatic)
        except XCallError as exc:
            _log("composite redirect declined for 0x%x: %s" % (int(xid), exc.msg))
            return False
        return True

    # ---- RENDER ---------------------------------------------------------

    def _format_for(self, xid):
        xrender = self._xrender
        try:
            visual = self._window(xid).get_attributes().visual
        except Exception:
            visual = None
        if visual is not None:
            try:
                return self._formats.find_for_visual(int(visual))
            except xrender.RenderError:
                pass
        depth = self.geometry(xid)[4]
        return self._formats.find_standard(depth, depth == 32)

    def create_source_picture(self, xid):
        """A Picture over the EVE client's whole window.

        ``IncludeInferiors`` is cheap insurance: DXVK presents into a
        same-depth CHILD of the whole window, and the server default
        (ClipByChildren) would clip exactly that content away.  The bilinear
        filter is what makes the server-side downscale look like a thumbnail
        instead of aliased noise.
        """
        xrender = self._xrender
        fmt = self._format_for(xid)
        pic = self._checked(self._window(xid).render_create_picture, fmt,
                            subwindow_mode=xrender.IncludeInferiors)
        self._checked(pic.set_filter, xrender.FilterBilinear)
        return pic

    def create_dest_picture(self, xid):
        fmt = self._format_for(xid)
        return self._checked(self._window(xid).render_create_picture, fmt)

    def set_transform(self, pic, sx, sy):
        # Per-frame path: fire and forget.
        pic.set_transform([[float(sx), 0.0, 0.0],
                           [0.0, float(sy), 0.0],
                           [0.0, 0.0, 1.0]])

    def composite(self, src_pic, dst_pic, w, h):
        # Per-frame path: fire and forget.  PictOpSrc, full destination rect.
        src_pic.composite(self._xrender.PictOpSrc, dst_pic,
                          0, 0, 0, 0, int(w), int(h))

    def free_picture(self, pic):
        if pic is None:
            return
        try:
            pic.free()
        except Exception as exc:
            _log("FreePicture failed: %s" % (exc,))

    # ---- DAMAGE ---------------------------------------------------------

    def damage_create(self, xid):
        damage = self._damage
        return int(self._checked(self._window(xid).damage_create,
                                 damage.DamageReportRawRectangles))

    def damage_subtract(self, did):
        # Per-frame path: fire and forget.
        self.display.damage_subtract(int(did))

    def damage_destroy(self, did):
        self._checked(self.display.damage_destroy, int(did))

    # ---- events ---------------------------------------------------------

    def next_events(self):
        """Drain the queue into normalised tuples.

        ``("damage", xid)`` / ``("configure", xid, w, h)`` /
        ``("destroy", xid)`` / ``("error", code, resource)``.  Anything else is
        dropped here so HelperCore never sees an Xlib object.
        """
        out = self._take_errors()
        try:
            pending = self.display.pending_events()
        except Exception as exc:
            # A dead connection is the ONE failure that must not be swallowed:
            # the fd stays readable forever, so _run_loop would spin instead of
            # exiting 3.
            if _is_display_dead(exc):
                raise
            _log("pending_events failed: %s" % (exc,))
            return out
        X = self._X
        for _ in range(pending):
            try:
                ev = self.display.next_event()
            except Exception as exc:
                _log("next_event failed: %s" % (exc,))
                break
            etype = getattr(ev, "type", None)
            # Damage is matched on the EVENT CODE, never with isinstance():
            # the decoded object is an instance of the anonymous clone
            # extension_add_event() registered, NOT of damage.DamageNotify
            # (see _query_damage_code) -- an isinstance test here silently
            # dropped every damage event and left the previews repainting on
            # the heartbeat alone.  The class-name check is belt and braces.
            if self._is_damage_event(ev, etype):
                out.append(("damage", _xid_of(ev.drawable)))
                continue
            if etype == X.ConfigureNotify:
                out.append(("configure", _xid_of(ev.window),
                            int(ev.width), int(ev.height)))
            elif etype == X.DestroyNotify:
                out.append(("destroy", _xid_of(ev.window)))
        out.extend(self._take_errors())
        return out

    def _is_damage_event(self, ev, etype):
        """True for a decoded DamageNotify, matched by code then by name."""
        if self._damage_code is not None and etype is not None:
            try:
                if (int(etype) & 0x7f) == self._damage_code:
                    return True
            except (TypeError, ValueError):
                pass
        return type(ev).__name__ == self._damage_cls_name

    # ---- error plumbing -------------------------------------------------

    def _take_errors(self):
        out = self._errors
        self._errors = []
        return out

    def _on_async_error(self, error, request):
        """Global handler: an error for a fire-and-forget request."""
        self._errors.append(("error", _error_code(error),
                             _xid_of(getattr(error, "resource_id", 0))))
        return 1

    def _checked(self, func, *args, **kwargs):
        """Issue ``func``, sync, and raise XCallError if the server complained.

        ``CatchError`` alone is not enough: several vendored calls
        (damage_create, shape_rectangles, damage_destroy) take no ``onerror``
        keyword, so the sync-and-inspect pattern is used uniformly and the
        errors it consumes are removed from the async queue.
        """
        catcher = None
        if _accepts_onerror(func):
            catcher = self._xerror.CatchError()
            kwargs["onerror"] = catcher
        mark = len(self._errors)
        result = func(*args, **kwargs)
        self.sync()
        if catcher is not None:
            err = catcher.get_error()
            if err is not None:
                raise self._as_call_error(err, 0)
        if len(self._errors) > mark:
            fresh = self._errors[mark:]
            del self._errors[mark:]
            code, resource = fresh[0][1], fresh[0][2]
            raise XCallError("X error %s on resource 0x%x" % (code, resource),
                             code, resource)
        return result

    def _as_call_error(self, exc, xid):
        if isinstance(exc, XCallError):
            return exc
        xerror = self._xerror
        if xerror is not None and isinstance(exc, xerror.XError):
            return XCallError(str(exc), _error_code(exc),
                              _xid_of(getattr(exc, "resource_id", xid)) or xid)
        return XCallError("%s: %s" % (type(exc).__name__, exc), "x_error", xid)


def _accepts_onerror(func):
    """True when ``func`` takes an ``onerror`` keyword (vendored calls vary)."""
    code = getattr(func, "__code__", None)
    if code is None:
        code = getattr(getattr(func, "__func__", None), "__code__", None)
    if code is None:
        return False
    names = code.co_varnames[:code.co_argcount + code.co_kwonlyargcount]
    return "onerror" in names


def _xid_of(value):
    """Resource objects, ints and Nones all become a plain int id."""
    if value is None:
        return 0
    ident = getattr(value, "id", None)
    if ident is not None:
        value = ident
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


_RESOURCE_ERRORS = ("BadWindow", "BadDrawable", "BadPixmap")


def _error_code(error):
    """Map an Xlib error class onto a protocol error code."""
    name = type(error).__name__
    if name in _RESOURCE_ERRORS:
        return "bad_window"
    return "x_error"


# ==========================================================================
# one thumbnail
# ==========================================================================

class Thumb(object):
    """State for one attached thumbnail."""

    def __init__(self, tid, src, dst, child, rect,
                 min_interval_ms=DEFAULT_MIN_INTERVAL_MS,
                 heartbeat_ms=DEFAULT_HEARTBEAT_MS, owns_dst=False):
        self.id = int(tid)
        self.src = int(src)
        self.dst = int(dst)
        self.child = int(child)
        self.rect = tuple(rect)
        self.src_pic = None
        self.dst_pic = None
        self.damage = None
        self.last_composite_ts = 0.0
        self.dirty = False
        self.last_size = (0, 0)
        self.min_interval_ms = max(MIN_INTERVAL_FLOOR_MS, int(min_interval_ms))
        #: 0 means DISABLED (damage-driven only) -- it must not collapse to the
        #: minimum interval, which would be the busiest heartbeat of all.
        heartbeat_ms = int(heartbeat_ms)
        self.heartbeat_ms = (HEARTBEAT_DISABLED_MS if heartbeat_ms <= 0
                             else max(self.min_interval_ms, heartbeat_ms))
        self.visible = True
        #: probe mode owns the destination window and must destroy it too.
        self.owns_dst = bool(owns_dst)

    def size(self):
        """The destination size in pixels, from the tile rect."""
        left, top, right, bottom = self.rect
        return (max(0, int(right) - int(left)), max(0, int(bottom) - int(top)))

    def __repr__(self):
        return "<Thumb %d src=0x%x dst=0x%x>" % (self.id, self.src, self.dst)


def _rect_xywh(rect):
    left, top, right, bottom = (int(v) for v in rect)
    return (left, top, max(1, right - left), max(1, bottom - top))


# ==========================================================================
# orchestration
# ==========================================================================

class HelperCore(object):
    """Protocol + scheduling, with every X call delegated to ``session``.

    Pure orchestration: no sockets, no clock reads beyond ``clock``, no X.
    """

    def __init__(self, session, transport, clock=time.monotonic,
                 sleep=time.sleep):
        self.session = session
        self.transport = transport
        self.clock = clock
        self.sleep = sleep
        self.thumbs = {}
        self.running = True
        self.frames = 0
        self.damage_events = 0
        self.coalesced = 0
        self.errors = 0
        self.started_ts = clock()
        self._next_stats_ts = self.started_ts + STATS_INTERVAL_S
        self._probe_counts = None
        #: sources already offered to Composite (asked once, never again).
        self._redirected = set()

    # ---- outgoing -------------------------------------------------------

    def send(self, msg):
        try:
            self.transport.send(msg)
        except Exception as exc:
            _log("send failed: %s" % (exc,))

    def _error(self, tid, code, msg):
        self.errors += 1
        self.send({"type": proto.T_ERROR, "id": tid,
                   "code": code if code in proto.ERROR_CODES else "internal",
                   "msg": _ascii(msg)})

    # ---- incoming -------------------------------------------------------

    def handle_message(self, msg):
        if not isinstance(msg, dict):
            self._error(None, "internal", "message is not an object")
            return
        mtype = msg.get("type")
        if mtype == proto.T_MALFORMED:
            self._error(None, "internal",
                        "malformed line: %s" % (msg.get("raw"),))
            return
        problem = proto.validate(msg)
        if problem is not None:
            self._error(None, "internal", problem)
            return
        # One bad message must never kill a helper whose stderr is closed.
        try:
            if mtype == proto.T_ATTACH:
                self._on_attach(msg)
            elif mtype == proto.T_UPDATE:
                self._on_update(msg)
            elif mtype == proto.T_VISIBLE:
                self._on_visible(msg)
            elif mtype == proto.T_SIZE:
                self._on_size(msg)
            elif mtype == proto.T_DETACH:
                self._on_detach(msg)
            elif mtype == proto.T_PROBE:
                self._on_probe(msg)
            elif mtype == proto.T_QUIT:
                self.running = False
            elif mtype == proto.T_OK:
                pass
            else:
                self._error(None, "internal",
                            "unexpected message '%s'" % (mtype,))
        except Exception as exc:
            # A dead display is nobody's fault and must reach _run_loop, which
            # is the only place allowed to stop -- see composite()/tick().
            if _is_display_dead(exc):
                raise
            self._error(msg.get("id"), "internal",
                        "%s failed: %s: %s" % (mtype, type(exc).__name__, exc))

    # ---- attach / detach ------------------------------------------------

    def _build_thumb(self, tid, src, dst, rect, min_interval_ms, heartbeat_ms,
                     owns_dst=False):
        """Create every X resource for one thumbnail.  Raises XCallError with
        the partial work already cleaned up.

        ``dst`` is always the CALLER's resource (FCTool's HWND-backed window,
        or the probe's own helper-owned window) -- this method only ever
        creates the child, the pictures and the damage.  So a partial-build
        failure here releases those, but never touches ``dst`` itself: the
        caller that created it (see ``_on_probe``) is the single owner of
        destroying it on a build failure.
        """
        session = self.session
        x, y, w, h = _rect_xywh(rect)
        if int(src) not in self._redirected:
            self._redirected.add(int(src))
            session.try_redirect_automatic(src)
        child = session.create_child(dst, x, y, w, h)
        thumb = Thumb(tid, src, dst, child, rect,
                      min_interval_ms=min_interval_ms,
                      heartbeat_ms=heartbeat_ms, owns_dst=owns_dst)
        try:
            session.set_input_shape_empty(child)
            session.map(child)
            session.select_structure(src)
            thumb.src_pic = session.create_source_picture(src)
            thumb.dst_pic = session.create_dest_picture(child)
            thumb.damage = session.damage_create(src)
        except XCallError:
            thumb.owns_dst = False
            self._release(thumb)
            raise
        return thumb

    def _on_attach(self, msg):
        tid = msg["id"]
        if tid in self.thumbs:
            self._release(self.thumbs.pop(tid))
        try:
            thumb = self._build_thumb(tid, msg["src"], msg["dst"], msg["rect"],
                                      msg.get("min_interval_ms",
                                              DEFAULT_MIN_INTERVAL_MS),
                                      msg.get("heartbeat_ms",
                                              DEFAULT_HEARTBEAT_MS))
        except XCallError as exc:
            self._error(tid, exc.code, exc.msg)
            return
        self.thumbs[tid] = thumb
        try:
            src_w, src_h = self.session.geometry(thumb.src)[2:4]
        except XCallError as exc:
            self._fail_thumb(thumb, exc)
            return
        thumb.last_size = (src_w, src_h)
        self.send({"type": proto.T_ATTACHED, "id": tid,
                   "src_w": int(src_w), "src_h": int(src_h)})
        # Paint once now: a client that never damages (minimised EVE, a paused
        # client) would otherwise show nothing until the first heartbeat.
        self.composite(thumb)

    def _on_detach(self, msg):
        tid = msg["id"]
        thumb = self.thumbs.pop(tid, None)
        if thumb is not None:
            self._release(thumb)
            self._forget_redirect(thumb.src)
        # A detach for an id we never had (or already dropped after an error)
        # is answered anyway: the app is entitled to its reply either way.
        self.send({"type": proto.T_DETACHED, "id": tid})

    def _forget_redirect(self, src):
        """Let a later attach offer this source to Composite again.

        The redirect is asked once per source, but a detached source may be
        gone for good (client closed) and the NEXT window behind that tile is a
        different one.  Tiles still showing this source keep the memo.
        """
        src = int(src)
        for thumb in self.thumbs.values():
            if thumb.src == src:
                return
        self._redirected.discard(src)

    def _release(self, thumb):
        """Free every X resource of ``thumb``.  Never raises."""
        session = self.session
        for pic in (thumb.src_pic, thumb.dst_pic):
            if pic is not None:
                try:
                    session.free_picture(pic)
                except Exception as exc:
                    _log("free_picture: %s" % (exc,))
        thumb.src_pic = None
        thumb.dst_pic = None
        if thumb.damage is not None:
            try:
                session.damage_destroy(thumb.damage)
            except Exception as exc:
                _log("damage_destroy: %s" % (exc,))
            thumb.damage = None
        if thumb.child:
            try:
                session.destroy(thumb.child)
            except Exception as exc:
                _log("destroy child: %s" % (exc,))
            thumb.child = 0
        if thumb.owns_dst and thumb.dst:
            try:
                session.destroy(thumb.dst)
            except Exception as exc:
                _log("destroy probe window: %s" % (exc,))
            thumb.owns_dst = False

    def _fail_thumb(self, thumb, exc):
        """Tear one thumbnail down and report it; the others keep running."""
        self.thumbs.pop(thumb.id, None)
        self._release(thumb)
        # Same bookkeeping as a clean detach: the next window behind this
        # tile may be a different source, so offer it to Composite again.
        self._forget_redirect(thumb.src)
        self._error(thumb.id, getattr(exc, "code", "x_error"),
                    getattr(exc, "msg", str(exc)))

    # ---- update / visible / size ----------------------------------------

    def _thumb_or_error(self, tid):
        thumb = self.thumbs.get(tid)
        if thumb is None:
            self._error(tid, "bad_window", "no thumbnail with id %s" % (tid,))
        return thumb

    def _on_update(self, msg):
        thumb = self._thumb_or_error(msg["id"])
        if thumb is None:
            return
        thumb.rect = tuple(msg["rect"])
        x, y, w, h = _rect_xywh(thumb.rect)
        try:
            self.session.move_resize(thumb.child, x, y, w, h)
        except XCallError as exc:
            self._fail_thumb(thumb, exc)
            return
        except Exception as exc:
            if _is_display_dead(exc):
                raise
            self._fail_thumb(thumb, _as_internal(exc, "_on_update"))
            return
        self.composite(thumb)

    def _on_visible(self, msg):
        thumb = self._thumb_or_error(msg["id"])
        if thumb is None:
            return
        on = bool(msg["on"])
        try:
            if on:
                self.session.map(thumb.child)
            else:
                self.session.unmap(thumb.child)
        except XCallError as exc:
            self._fail_thumb(thumb, exc)
            return
        except Exception as exc:
            if _is_display_dead(exc):
                raise
            self._fail_thumb(thumb, _as_internal(exc, "_on_visible"))
            return
        thumb.visible = on
        if on:
            self.composite(thumb)

    def _on_size(self, msg):
        thumb = self._thumb_or_error(msg["id"])
        if thumb is None:
            return
        try:
            src_w, src_h = self.session.geometry(thumb.src)[2:4]
        except XCallError as exc:
            self._fail_thumb(thumb, exc)
            return
        except Exception as exc:
            if _is_display_dead(exc):
                raise
            self._fail_thumb(thumb, _as_internal(exc, "_on_size"))
            return
        thumb.last_size = (src_w, src_h)
        self.send({"type": proto.T_SIZE, "id": thumb.id,
                   "w": int(src_w), "h": int(src_h)})

    # ---- events ---------------------------------------------------------

    def handle_event(self, ev):
        if not ev:
            return
        kind = ev[0]
        try:
            if kind == "damage":
                self._on_damage(int(ev[1]))
            elif kind == "configure":
                self._on_configure(int(ev[1]))
            elif kind == "destroy":
                self._on_destroy(int(ev[1]))
            elif kind == "error":
                self._on_x_error(ev[1], int(ev[2]) if len(ev) > 2 else 0)
        except Exception as exc:
            # Same rule as handle_message(): a dead display must reach
            # _run_loop, not be reported as one more internal failure.
            if _is_display_dead(exc):
                raise
            self._error(None, "internal", "event %s failed: %s: %s"
                        % (kind, type(exc).__name__, exc))

    def _on_damage(self, xid):
        self.damage_events += 1
        now = self.clock()
        for thumb in self._by_src(xid):
            self._note_probe(thumb.id, "damage_events", 1)
            if not thumb.visible:
                continue
            elapsed_ms = (now - thumb.last_composite_ts) * 1000.0
            if elapsed_ms >= thumb.min_interval_ms:
                self.composite(thumb, now)
            else:
                # EPM's burstiness fix: a client can damage far faster than the
                # tile can usefully show.  Mark and let tick() catch up once.
                thumb.dirty = True
                self.coalesced += 1

    def _on_configure(self, xid):
        for thumb in self._by_src(xid):
            thumb.dirty = True

    def _on_destroy(self, xid):
        for thumb in self._by_src(xid):
            self._fail_thumb(thumb,
                             XCallError("source window 0x%x was destroyed"
                                        % (xid,), "bad_window", xid))

    def _on_x_error(self, code, resource):
        self.errors += 1
        hit = None
        for thumb in list(self.thumbs.values()):
            if resource and resource in (thumb.src, thumb.child, thumb.dst,
                                         thumb.damage or 0):
                hit = thumb
                break
        if hit is None:
            _log("X error %s on 0x%x (no thumbnail owns it)" % (code, resource))
            return
        # _fail_thumb books its own error; this one was already counted.
        self.errors -= 1
        self._fail_thumb(hit, XCallError("X error %s on resource 0x%x"
                                         % (code, resource), code, resource))

    def _by_src(self, xid):
        return [t for t in list(self.thumbs.values()) if t.src == xid]

    # ---- compositing ----------------------------------------------------

    def composite(self, thumb, now=None):
        """One frame: fresh geometry, transform, PictOpSrc, DamageSubtract.

        Returns True when a frame was actually drawn.  A skip (unviewable
        source, degenerate size) still stamps the clock so the source is
        retried on the heartbeat instead of on every single tick.
        """
        if now is None:
            now = self.clock()
        session = self.session
        started = now
        try:
            if not session.viewable(thumb.src):
                thumb.dirty = False
                thumb.last_composite_ts = now
                return False
            geom = session.geometry(thumb.src)
            src_w, src_h = int(geom[2]), int(geom[3])
            dst_w, dst_h = thumb.size()
            if src_w <= 1 or src_h <= 1 or dst_w <= 1 or dst_h <= 1:
                thumb.dirty = False
                thumb.last_composite_ts = now
                return False
            session.set_transform(thumb.src_pic,
                                  float(src_w) / float(dst_w),
                                  float(src_h) / float(dst_h))
            session.composite(thumb.src_pic, thumb.dst_pic, dst_w, dst_h)
            session.damage_subtract(thumb.damage)
            session.flush()
        except XCallError as exc:
            self._fail_thumb(thumb, exc)
            return False
        except Exception as exc:
            # Not every per-frame failure is an XCallError: the vendored Xlib
            # raises RenderError, and a bad reply raises plain TypeErrors.  One
            # thumbnail must never take a helper with CLOSED stderr down with
            # it -- but a dead display is nobody's fault and must reach
            # _run_loop, which is the only place allowed to stop.
            if _is_display_dead(exc):
                raise
            self._fail_thumb(thumb, _as_internal(exc, "composite"))
            return False
        thumb.dirty = False
        thumb.last_composite_ts = now
        self.frames += 1
        self._note_probe(thumb.id, "composites", 1)
        self._note_probe(thumb.id, "elapsed_ms",
                         max(0.0, (self.clock() - started) * 1000.0))
        if (src_w, src_h) != thumb.last_size:
            thumb.last_size = (src_w, src_h)
            # Probe thumbnails carry helper-internal negative ids that FCTool
            # holds no handle for -- never push those onto the wire.
            if thumb.id >= 0:
                self.send({"type": proto.T_SIZE, "id": thumb.id,
                           "w": src_w, "h": src_h})
        return True

    def tick(self, now=None):
        """Deferred composites (coalescing + heartbeat) and the stats beat."""
        if now is None:
            now = self.clock()
        for thumb in list(self.thumbs.values()):
            # Same containment as composite(): one thumbnail's bad frame ends
            # that thumbnail, not the tick and not the helper.
            try:
                if not thumb.visible:
                    continue
                elapsed_ms = (now - thumb.last_composite_ts) * 1000.0
                if thumb.dirty:
                    if elapsed_ms >= thumb.min_interval_ms:
                        self.composite(thumb, now)
                elif thumb.heartbeat_ms and elapsed_ms >= thumb.heartbeat_ms:
                    # Wine flushes its window surface with an IncludeInferiors
                    # GC, which can paint straight over our child.  The
                    # heartbeat is the repair (0 = the app disabled it).
                    self.composite(thumb, now)
            except XCallError as exc:
                self._fail_thumb(thumb, exc)
            except Exception as exc:
                if _is_display_dead(exc):
                    raise
                self._fail_thumb(thumb, _as_internal(exc, "tick"))
        self._maybe_stats(now)

    def next_timeout(self, now=None):
        """Seconds until the next scheduled composite, capped for liveness."""
        if now is None:
            now = self.clock()
        timeout = MAX_SELECT_TIMEOUT_S
        for thumb in self.thumbs.values():
            if not thumb.visible:
                continue
            if thumb.dirty:
                window_ms = thumb.min_interval_ms
            elif thumb.heartbeat_ms:
                window_ms = thumb.heartbeat_ms
            else:
                continue        # heartbeat disabled: nothing is ever due
            due = thumb.last_composite_ts + window_ms / 1000.0
            timeout = min(timeout, due - now)
        timeout = min(timeout, self._next_stats_ts - now)
        if timeout < 0.0:
            return 0.0
        return timeout

    def _maybe_stats(self, now):
        if now < self._next_stats_ts:
            return
        self._next_stats_ts = now + STATS_INTERVAL_S
        self.send({"type": proto.T_STATS,
                   "frames": self.frames,
                   "damage_events": self.damage_events,
                   "coalesced": self.coalesced,
                   "errors": self.errors,
                   "uptime_s": round(now - self.started_ts, 3)})

    # ---- probe ----------------------------------------------------------

    def _note_probe(self, tid, key, amount):
        if self._probe_counts is None:
            return
        entry = self._probe_counts.get(tid)
        if entry is not None:
            entry[key] = entry.get(key, 0) + amount

    def _on_probe(self, msg):
        """Attach every source into a helper-owned test window for N seconds
        and report what the server actually did."""
        src_list = [int(v) for v in msg["src_list"]]
        seconds = max(0, min(PROBE_MAX_SECONDS, int(msg["seconds"])))
        results = []
        entries = {}
        probes = []
        self._probe_counts = {}
        try:
            for index, src in enumerate(src_list):
                entry = {"src": src, "viewable": False, "depth": 0,
                         "src_w": 0, "src_h": 0, "src_size": [0, 0],
                         "damage_events": 0, "composites": 0,
                         "avg_composite_ms": 0.0, "error": None}
                results.append(entry)
                tid = -(index + 1)
                entries[tid] = entry
                self._probe_counts[tid] = {"damage_events": 0,
                                           "composites": 0, "elapsed_ms": 0.0}
                try:
                    entry["viewable"] = bool(self.session.viewable(src))
                    geom = self.session.geometry(src)
                    entry["src_w"], entry["src_h"] = int(geom[2]), int(geom[3])
                    entry["src_size"] = [entry["src_w"], entry["src_h"]]
                    entry["depth"] = int(geom[4])
                    dst = self._probe_window()
                    try:
                        thumb = self._build_thumb(tid, src, dst,
                                                  (0, 0, PROBE_W, PROBE_H),
                                                  DEFAULT_MIN_INTERVAL_MS,
                                                  DEFAULT_HEARTBEAT_MS,
                                                  owns_dst=True)
                    except Exception:
                        # The probe window is already MAPPED: without this it
                        # would sit on the user's screen until the helper dies.
                        self._destroy_quietly(dst)
                        raise
                except XCallError as exc:
                    entry["error"] = _ascii(exc.msg)
                    continue
                except Exception as exc:
                    # One source's non-XCallError failure (RenderError, a bad
                    # reply, ...) must not abort the whole probe -- a dead
                    # display is the one exception that still must reach
                    # _run_loop.
                    if _is_display_dead(exc):
                        raise
                    entry["error"] = _ascii(_as_internal(exc, "probe").msg)
                    continue
                thumb.last_size = (entry["src_w"], entry["src_h"])
                self.thumbs[tid] = thumb
                probes.append(thumb)
                self.composite(thumb)
            self._pump(seconds)
            for tid, entry in entries.items():
                counts = self._probe_counts.get(tid) or {}
                entry["damage_events"] = int(counts.get("damage_events", 0))
                composites = int(counts.get("composites", 0))
                entry["composites"] = composites
                if composites:
                    entry["avg_composite_ms"] = round(
                        float(counts.get("elapsed_ms", 0.0)) / composites, 3)
        finally:
            self._probe_counts = None
            for thumb in probes:
                if self.thumbs.pop(thumb.id, None) is not None:
                    self._release(thumb)
        self.send({"type": proto.T_PROBE_RESULT, "results": results,
                   "round_trip_ms": self._round_trip_ms()})

    def _destroy_quietly(self, xid):
        """Destroy one helper-owned window; a failure here is only a log."""
        try:
            self.session.destroy(xid)
        except Exception as exc:
            _log("destroy probe window: %s" % (exc,))

    def _probe_window(self):
        """A 320x180 override-redirect window at the screen origin, mapped and
        click-through, that exists only for the probe."""
        win = self.session.create_child(self.session.root(), 0, 0,
                                        PROBE_W, PROBE_H)
        self.session.set_input_shape_empty(win)
        self.session.map(win)
        return win

    def _round_trip_ms(self):
        started = self.clock()
        try:
            self.session.sync()
        except Exception as exc:
            _log("probe sync failed: %s" % (exc,))
            return 0.0
        return round(max(0.0, (self.clock() - started) * 1000.0), 3)

    def _pump(self, seconds):
        """Service X events and composites for ``seconds``, nothing else."""
        deadline = self.clock() + float(seconds)
        while self.clock() < deadline:
            events = self.session.next_events()
            for ev in events:
                self.handle_event(ev)
            self.tick(self.clock())
            if not events:
                self.sleep(0.01)

    # ---- shutdown -------------------------------------------------------

    def shutdown(self):
        for tid in list(self.thumbs.keys()):
            thumb = self.thumbs.pop(tid, None)
            if thumb is not None:
                self._release(thumb)


def _ascii(text):
    """Log/report strings must survive a cp1252 console on the Windows side."""
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


# ==========================================================================
# transport
# ==========================================================================

class SocketTransport(object):
    """Non-blocking JSON-lines transport over one connected TCP socket.

    Writes are buffered, so a slow reader on the FCTool side can never block a
    composite; ``pending`` tells the select loop to watch for writability.
    """

    def __init__(self, sock):
        self.sock = sock
        self.sock.setblocking(False)
        self.closed = False
        self._out = b""
        self._decoder = proto.LineDecoder()

    def fileno(self):
        return self.sock.fileno()

    @property
    def pending(self):
        return bool(self._out)

    def send(self, msg):
        if self.closed:
            return
        try:
            self._out += proto.encode(msg)
        except proto.ProtoError as exc:
            _log("cannot encode %s: %s" % (msg.get("type"), exc))
            return
        self.flush()

    def flush(self):
        while self._out and not self.closed:
            try:
                sent = self.sock.send(self._out)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                _log("send failed: %s" % (exc,))
                self.closed = True
                return
            if sent <= 0:
                return
            self._out = self._out[sent:]

    def recv_messages(self):
        out = []
        while not self.closed:
            try:
                chunk = self.sock.recv(RECV_CHUNK)
            except (BlockingIOError, InterruptedError):
                break
            except OSError as exc:
                _log("recv failed: %s" % (exc,))
                self.closed = True
                break
            if not chunk:
                self.closed = True
                break
            try:
                out.extend(self._decoder.feed(chunk))
            except proto.ProtoError as exc:
                _log("control stream is unrecoverable: %s" % (exc,))
                self.closed = True
                break
        return out

    def close(self):
        self.closed = True
        try:
            self.sock.close()
        except Exception:
            pass


# ==========================================================================
# entry point
# ==========================================================================

def build_hello(session, token):
    """The first message on the wire: what this X server can actually do."""
    versions = session.extension_versions()
    return {"type": proto.T_HELLO,
            "token": str(token),
            "python": _ascii(sys.version.replace("\n", " ")),
            "display": _ascii(getattr(session, "display_name", "") or ""),
            "xrender": list(versions.get("xrender") or [0, 0]),
            "damage": list(versions.get("damage") or [0, 0]),
            "shape": bool(versions.get("shape")),
            "composite": bool(versions.get("composite")),
            "screen": session.screen_info()}


def _connect(opts):
    sock = socket.create_connection((opts.host, opts.port),
                                    timeout=CONNECT_TIMEOUT_S)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass
    return sock


def _fail_early(session, transport, code, detail, exit_code):
    """Report one fatal startup failure, put everything back, exit with it."""
    transport.send({"type": proto.T_ERROR, "id": None,
                    "code": code if code in proto.ERROR_CODES else "internal",
                    "msg": _ascii(detail)})
    transport.flush()
    transport.close()
    try:
        session.close()
    except Exception as exc:
        _log("session close: %s" % (exc,))
    return exit_code


def main(argv=None):
    """Run the helper.  Returns a process exit code; never raises."""
    if sys.version_info < (3, 9):
        _log("needs Python 3.9 or newer, this is %d.%d"
             % (sys.version_info[0], sys.version_info[1]))
        return EXIT_BAD_ARGS
    if argv is None:
        argv = sys.argv[1:]
    try:
        opts = parse_args(argv)
    except UsageError as exc:
        _log(str(exc))
        return EXIT_BAD_ARGS

    if opts.vendor and opts.vendor not in sys.path:
        sys.path.insert(0, opts.vendor)

    try:
        sock = _connect(opts)
    except OSError as exc:
        _log("cannot reach the control channel at %s:%d: %s"
             % (opts.host, opts.port, exc))
        return EXIT_NO_TRANSPORT
    transport = SocketTransport(sock)

    session = XSession()
    try:
        session.open(opts.display)
    except AuthError as exc:
        # Unreachable in practice: see the exit-code table at the top.
        return _fail_early(session, transport, "no_display", exc, EXIT_AUTH)
    except NoDisplayError as exc:
        return _fail_early(session, transport, "no_display", exc,
                           EXIT_NO_DISPLAY)
    except MissingExtensionError as exc:
        return _fail_early(session, transport, exc.code, exc.msg,
                           EXIT_NO_EXTENSION)
    except Exception as exc:
        # main() never raises: an unclassified failure while connecting is
        # still, from FCTool's side, "there is no usable display".
        return _fail_early(session, transport, "no_display",
                           "%s: %s" % (type(exc).__name__, exc),
                           EXIT_NO_DISPLAY)

    core = HelperCore(session, transport)
    code = EXIT_OK
    try:
        # Inside the guard: build_hello QUERIES the server, so it can die too,
        # and the finally below is what closes the display and the socket.
        core.send(build_hello(session, opts.token))
        code = _run_loop(core, session, transport)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        _log("helper loop died: %s: %s" % (type(exc).__name__, exc))
        core._error(None, "no_display",
                    "%s: %s" % (type(exc).__name__, exc))
        code = EXIT_NO_DISPLAY
    finally:
        try:
            core.shutdown()
        except Exception as exc:
            _log("shutdown: %s" % (exc,))
        session.close()
        # Best effort: let a final 'error'/'stats' line reach FCTool before the
        # socket goes away.  A dead peer just makes this a no-op.
        transport.flush()
        transport.close()
    return code


def _run_loop(core, session, transport):
    """select() over the X connection and the control socket.

    Returns the process exit code: EXIT_OK for quit/EOF, EXIT_NO_DISPLAY when
    the X connection itself dies.  NOTHING else stops it -- a failing frame,
    a bad message or a broken event is reported on the wire and the loop goes
    on, because a helper with closed stderr that exits looks to the user like
    previews that simply stopped.

    EOF on the control socket is THE kill path: Wine's process handle for a
    Unix child is not trustworthy, so FCTool closing the socket is how the
    helper learns to die.
    """
    try:
        x_fd = session.fileno()
    except Exception as exc:
        core._error(None, "no_display", "%s: %s" % (type(exc).__name__, exc))
        return EXIT_NO_DISPLAY
    sock_fd = transport.fileno()
    while core.running and not transport.closed:
        timeout = core.next_timeout()
        wlist = [sock_fd] if transport.pending else []
        try:
            readable, writable, _ = select.select([x_fd, sock_fd], wlist, [],
                                                  timeout)
        except InterruptedError:
            continue
        except OSError as exc:
            _log("select failed: %s" % (exc,))
            break
        if sock_fd in writable:
            transport.flush()
        if sock_fd in readable:
            try:
                for msg in transport.recv_messages():
                    core.handle_message(msg)
                    if not core.running:
                        break
            except Exception as exc:
                if _is_display_dead(exc):
                    core._error(None, "no_display",
                                "%s: %s" % (type(exc).__name__, exc))
                    return EXIT_NO_DISPLAY
                core._error(None, "internal",
                            "helper loop: %s: %s" % (type(exc).__name__, exc))
        # Always drain X: pending_events() can hold events the socket already
        # delivered into Xlib's own buffer, which select() will never re-report.
        try:
            for ev in session.next_events():
                core.handle_event(ev)
            core.tick()
        except Exception as exc:
            if _is_display_dead(exc):
                core._error(None, "no_display",
                            "%s: %s" % (type(exc).__name__, exc))
                return EXIT_NO_DISPLAY
            core._error(None, "internal",
                        "helper loop: %s: %s" % (type(exc).__name__, exc))
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised only on Linux
    sys.exit(main(sys.argv[1:]))
