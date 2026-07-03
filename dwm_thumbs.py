"""dwmapi ctypes bindings for live window thumbnails.

Verified against MS Learn + Proopai/eve-o-preview source (2026-07-03):
- DwmRegisterThumbnail is the 3-arg form (the 4-arg example on the
  DwmUpdateThumbnailProperties doc page is stale — do not copy it).
- Destination HWND must be a TOP-LEVEL window owned by THIS process:
  pass GetAncestor(winfo_id(), GA_ROOT), never winfo_id() itself.
- Nothing renders until the first update call (fVisible defaults FALSE).
- Update only on change; the live image refreshes with zero calls from us.
- rcSource is deliberately never exposed here (full-client view only — spec §4).
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

DWM_TNP_RECTDESTINATION = 0x00000001
DWM_TNP_RECTSOURCE = 0x00000002
DWM_TNP_OPACITY = 0x00000004
DWM_TNP_VISIBLE = 0x00000008
DWM_TNP_SOURCECLIENTAREAONLY = 0x00000010


class DWM_THUMBNAIL_PROPERTIES(ctypes.Structure):
    _fields_ = [
        ("dwFlags", wintypes.DWORD),
        ("rcDestination", wintypes.RECT),
        ("rcSource", wintypes.RECT),
        ("opacity", ctypes.c_ubyte),
        ("fVisible", wintypes.BOOL),
        ("fSourceClientAreaOnly", wintypes.BOOL),
    ]


assert ctypes.sizeof(DWM_THUMBNAIL_PROPERTIES) == 48


def aspect_fit(dest_w: int, dest_h: int, src_w: int, src_h: int):
    """Largest (x, y, w, h) inside dest preserving src aspect, centered."""
    if src_w <= 0 or src_h <= 0 or dest_w <= 0 or dest_h <= 0:
        return (0, 0, max(dest_w, 0), max(dest_h, 0))
    scale = min(dest_w / src_w, dest_h / src_h)
    w = max(1, int(src_w * scale))
    h = max(1, int(src_h * scale))
    return ((dest_w - w) // 2, (dest_h - h) // 2, w, h)


class _RealDwm:  # pragma: no cover — exercised by spike S1 + live use
    def __init__(self):
        d = ctypes.WinDLL("dwmapi")
        d.DwmRegisterThumbnail.argtypes = [wintypes.HWND, wintypes.HWND,
                                           ctypes.POINTER(wintypes.HANDLE)]
        d.DwmRegisterThumbnail.restype = ctypes.HRESULT
        d.DwmUnregisterThumbnail.argtypes = [wintypes.HANDLE]
        d.DwmUnregisterThumbnail.restype = ctypes.HRESULT
        d.DwmUpdateThumbnailProperties.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(DWM_THUMBNAIL_PROPERTIES)]
        d.DwmUpdateThumbnailProperties.restype = ctypes.HRESULT
        d.DwmQueryThumbnailSourceSize.argtypes = [wintypes.HANDLE,
                                                  ctypes.POINTER(wintypes.SIZE)]
        d.DwmQueryThumbnailSourceSize.restype = ctypes.HRESULT
        self._d = d

    def register(self, dest_hwnd: int, src_hwnd: int) -> int:
        handle = wintypes.HANDLE()
        self._d.DwmRegisterThumbnail(dest_hwnd, src_hwnd, ctypes.byref(handle))
        return handle.value

    def unregister(self, thumb: int) -> None:
        self._d.DwmUnregisterThumbnail(thumb)

    def update(self, thumb: int, rect, visible=True, opacity=255, client_only=True):
        props = DWM_THUMBNAIL_PROPERTIES()
        props.dwFlags = (DWM_TNP_RECTDESTINATION | DWM_TNP_VISIBLE
                         | DWM_TNP_OPACITY | DWM_TNP_SOURCECLIENTAREAONLY)
        props.rcDestination = wintypes.RECT(rect[0], rect[1], rect[2], rect[3])
        props.opacity = opacity
        props.fVisible = visible
        props.fSourceClientAreaOnly = client_only
        self._d.DwmUpdateThumbnailProperties(thumb, ctypes.byref(props))

    def query_source_size(self, thumb: int):
        size = wintypes.SIZE()
        self._d.DwmQueryThumbnailSourceSize(thumb, ctypes.byref(size))
        return (size.cx, size.cy)


_real = None


def _real_dwm():  # pragma: no cover
    global _real
    if _real is None:
        _real = _RealDwm()
    return _real


class Thumbnail:
    """One live-thumbnail relationship. Owned by exactly one tile, Tk thread only."""

    def __init__(self, dest_hwnd: int, src_hwnd: int, dwm=None):
        self._dwm = dwm or _real_dwm()
        self.src_hwnd = src_hwnd
        self._thumb = self._dwm.register(dest_hwnd, src_hwnd)

    def show(self, dest_rect, visible=True, opacity=255):
        """dest_rect: (left, top, right, bottom) in destination CLIENT coords, physical px."""
        self._dwm.update(self._thumb, dest_rect, visible=visible, opacity=opacity)

    def source_size(self):
        return self._dwm.query_source_size(self._thumb)

    def close(self):
        try:
            self._dwm.unregister(self._thumb)
        except OSError:
            pass  # source/DWM already gone — expected on client crash / DWM restart
