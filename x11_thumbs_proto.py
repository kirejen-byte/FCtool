"""JSON-lines control protocol shared by FCTool and the Linux X11 thumbnail
helper.

One UTF-8 JSON object per line, ``\\n`` terminated, at most ``MAX_LINE`` bytes
per line.  The helper speaks first with ``hello`` (carrying the auth token);
anything else closes the connection.

THIS FILE IS COPIED VERBATIM INTO THE HELPER'S sys.path on the Linux side,
where the interpreter may be as old as **Python 3.9** (Steam ``sniper``):

  * stdlib only -- never import an FCTool module,
  * Python 3.9 syntax and APIs only (no ``match``, no runtime ``X | Y``
    unions, no ``dataclass(slots=)``, no ``zip(strict=)``),
  * no logging, no side effects at import time,
  * every string ASCII (the Windows console is cp1252).

``tests/test_x11_thumbs_proto.py`` enforces all of the above.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

MAX_LINE = 65536
PROTOCOL_VERSION = 1

# How much of a bad line is echoed back in a "malformed" message.
MALFORMED_RAW_CHARS = 200


def _log_safe(data: Any) -> str:
    """Return ``data`` (bytes or str) as a printable-ASCII-only string.

    Every byte/char outside the printable ASCII range 0x20..0x7e is
    escaped as ``\\xNN``; a literal backslash passes through unchanged.
    Used to sanitize untrusted wire bytes/strings before they land in a
    log line, so a forged payload cannot inject ASCII control characters
    (``\\r``, ``\\x00``, ``\\x1b``, ``\\x07``, ...) or non-ASCII bytes to
    spoof or corrupt log output.
    """
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    return "".join(
        chr(b) if 0x20 <= b <= 0x7e else "\\x%02x" % b
        for b in data
    )

# ---- message types -------------------------------------------------------
# helper -> app
T_HELLO = "hello"
T_ATTACHED = "attached"
T_DETACHED = "detached"
T_PROBE_RESULT = "probe_result"
T_STATS = "stats"
T_ERROR = "error"
# app -> helper
T_OK = "ok"
T_ATTACH = "attach"
T_UPDATE = "update"
T_VISIBLE = "visible"
T_DETACH = "detach"
T_PROBE = "probe"
T_QUIT = "quit"
# both directions (app asks with {id}, helper answers/pushes with {id,w,h})
T_SIZE = "size"
# synthesised locally by LineDecoder -- never sent on the wire
T_MALFORMED = "malformed"

MESSAGE_TYPES = (
    T_HELLO, T_OK, T_ATTACH, T_ATTACHED, T_UPDATE, T_VISIBLE, T_SIZE,
    T_DETACH, T_DETACHED, T_PROBE, T_PROBE_RESULT, T_QUIT, T_STATS, T_ERROR,
    T_MALFORMED,
)

ERROR_CODES = (
    "no_display", "no_render", "no_damage", "bad_window", "x_error",
    "internal",
)


class ProtoError(Exception):
    """A frame that cannot be encoded, or a stream that must be closed."""


# ---------------------------------------------------------------- encoding


def encode(msg: Dict[str, Any]) -> bytes:
    """Serialise ``msg`` to one compact ASCII JSON line ending in ``\\n``.

    Raises ProtoError when ``msg`` is not a dict, carries no string ``type``,
    holds something JSON cannot represent, or would exceed MAX_LINE bytes.
    """
    if not isinstance(msg, dict):
        raise ProtoError("message must be a dict, got %s" % type(msg).__name__)
    mtype = msg.get("type")
    if not isinstance(mtype, str) or not mtype:
        raise ProtoError("message needs a non-empty string 'type'")
    try:
        text = json.dumps(msg, separators=(",", ":"), ensure_ascii=True,
                          allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ProtoError("message is not JSON serializable: %s" % (exc,))
    line = text.encode("ascii") + b"\n"
    if len(line) > MAX_LINE:
        raise ProtoError("message is %d bytes, limit is %d"
                         % (len(line), MAX_LINE))
    return line


# ---------------------------------------------------------------- decoding


class LineDecoder(object):
    """Reassembles newline-framed JSON objects from arbitrary byte chunks.

    ``feed`` returns the messages completed by this chunk.  A line that is not
    a JSON object carrying a string ``type`` comes back as
    ``{"type": "malformed", "raw": <first MALFORMED_RAW_CHARS bytes, ASCII
    escaped>}`` -- the stream stays in sync, so the line after a bad one
    decodes normally.  Only a buffer that grows past ``max_line`` with no
    newline in it raises ProtoError; that stream is unrecoverable and the
    caller must close it.
    """

    def __init__(self, max_line: int = MAX_LINE) -> None:
        self.max_line = int(max_line)
        self._buf = b""

    def feed(self, data: bytes) -> List[Dict[str, Any]]:
        if isinstance(data, bytearray):
            data = bytes(data)
        if not isinstance(data, bytes):
            raise ProtoError("feed() expects bytes, got %s"
                             % type(data).__name__)
        out = []  # type: List[Dict[str, Any]]
        self._buf += data
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            raw = self._buf[:idx]
            self._buf = self._buf[idx + 1:]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            if not raw.strip():
                continue
            out.append(self._decode_line(raw))
        if len(self._buf) > self.max_line:
            self._buf = b""
            raise ProtoError("line exceeds %d bytes without a newline"
                             % self.max_line)
        return out

    def _decode_line(self, raw: bytes) -> Dict[str, Any]:
        # raw excludes the trailing "\n"; require payload + newline to fit
        # within max_line, the same rule encode() enforces on its own output.
        if len(raw) >= self.max_line:
            return self._malformed(raw)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._malformed(raw)
        if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
            return self._malformed(raw)
        return obj

    @staticmethod
    def _malformed(raw: bytes) -> Dict[str, Any]:
        # Cap on bytes first, then escape to ASCII-only -- a raw non-UTF-8
        # or control-character-laden line must never inject non-ASCII
        # (e.g. U+FFFD) or raw control bytes (\r, \x1b, ...) into a string
        # the app may log to the cp1252 console or a log file.
        text = _log_safe(raw[:MALFORMED_RAW_CHARS])
        return {"type": T_MALFORMED, "raw": text}


# -------------------------------------------------------------- validation
# Field kinds: int, str, bool, num, rect, pair, int_list, list, dict_wh_depth,
# id_or_null, error_code.

_REQUIRED = {
    T_HELLO: (("token", "str"), ("python", "str"), ("display", "str"),
              ("xrender", "pair"), ("damage", "pair"), ("shape", "bool"),
              ("composite", "bool"), ("screen", "dict_wh_depth")),
    T_OK: (),
    T_ATTACH: (("id", "int"), ("src", "int"), ("dst", "int"),
               ("rect", "rect"), ("min_interval_ms", "int"),
               ("heartbeat_ms", "int")),
    T_ATTACHED: (("id", "int"), ("src_w", "int"), ("src_h", "int")),
    T_UPDATE: (("id", "int"), ("rect", "rect")),
    T_VISIBLE: (("id", "int"), ("on", "bool")),
    T_SIZE: (("id", "int"),),
    T_DETACH: (("id", "int"),),
    T_DETACHED: (("id", "int"),),
    T_PROBE: (("src_list", "int_list"), ("seconds", "int")),
    T_PROBE_RESULT: (("results", "list"),),
    T_QUIT: (),
    T_STATS: (("frames", "int"), ("damage_events", "int"),
              ("coalesced", "int"), ("errors", "int"), ("uptime_s", "num")),
    T_ERROR: (("id", "id_or_null"), ("code", "error_code"), ("msg", "str")),
    T_MALFORMED: (("raw", "str"),),
}  # type: Dict[str, Tuple[Tuple[str, str], ...]]

_OPTIONAL = {
    T_SIZE: (("w", "int"), ("h", "int")),
}  # type: Dict[str, Tuple[Tuple[str, str], ...]]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check(kind: str, value: Any) -> Optional[str]:
    """Return None when ``value`` matches ``kind``, else a short problem."""
    if kind == "int":
        return None if _is_int(value) else "must be an int"
    if kind == "num":
        return None if _is_num(value) else "must be a number"
    if kind == "str":
        return None if isinstance(value, str) else "must be a string"
    if kind == "bool":
        return None if isinstance(value, bool) else "must be a bool"
    if kind == "list":
        return None if isinstance(value, list) else "must be a list"
    if kind == "int_list":
        if not isinstance(value, list):
            return "must be a list"
        for item in value:
            if not _is_int(item):
                return "must hold ints only"
        return None
    if kind == "pair":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return "must be a list of 2 ints"
        for item in value:
            if not _is_int(item):
                return "must be a list of 2 ints"
        return None
    if kind == "rect":
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            return "must be [l,t,r,b]"
        for item in value:
            if not _is_int(item):
                return "must be 4 ints"
        if value[2] <= value[0]:
            return "needs r > l"
        if value[3] <= value[1]:
            return "needs b > t"
        return None
    if kind == "dict_wh_depth":
        if not isinstance(value, dict):
            return "must be an object with w, h, depth"
        for key in ("w", "h", "depth"):
            if not _is_int(value.get(key)):
                return "needs int '%s'" % key
        return None
    if kind == "id_or_null":
        return None if value is None or _is_int(value) else "must be an int or null"
    if kind == "error_code":
        if not isinstance(value, str):
            return "must be a string"
        return None if value in ERROR_CODES else "is not a known error code"
    return "has no validator for kind %s" % kind  # pragma: no cover


def validate(msg: Any) -> Optional[str]:
    """Return None when ``msg`` is a well-formed message, else the FIRST
    problem as a short ASCII string suitable for a log line."""
    if not isinstance(msg, dict):
        return "message is not an object"
    mtype = msg.get("type")
    if not isinstance(mtype, str) or not mtype:
        return "message has no string 'type'"
    if mtype not in _REQUIRED:
        safe = _log_safe(mtype[:40])
        return "unknown message type '%s'" % safe
    for name, kind in _REQUIRED[mtype]:
        if name not in msg:
            return "%s: missing '%s'" % (mtype, name)
        problem = _check(kind, msg[name])
        if problem is not None:
            return "%s: '%s' %s" % (mtype, name, problem)
    for name, kind in _OPTIONAL.get(mtype, ()):
        if name in msg:
            problem = _check(kind, msg[name])
            if problem is not None:
                return "%s: '%s' %s" % (mtype, name, problem)
    return None
