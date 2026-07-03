"""Tests for dwm_thumbs — pure struct/constants/math + fake-backend recording."""
import ctypes

import pytest

import dwm_thumbs as dt


def test_struct_layout_is_48_bytes():
    assert ctypes.sizeof(dt.DWM_THUMBNAIL_PROPERTIES) == 48


def test_flag_constants_match_windows_sdk():
    assert dt.DWM_TNP_RECTDESTINATION == 0x1
    assert dt.DWM_TNP_RECTSOURCE == 0x2
    assert dt.DWM_TNP_OPACITY == 0x4
    assert dt.DWM_TNP_VISIBLE == 0x8
    assert dt.DWM_TNP_SOURCECLIENTAREAONLY == 0x10


@pytest.mark.parametrize("dest,src,expected", [
    ((384, 216), (1920, 1080), (0, 0, 384, 216)),      # same aspect: fills
    ((384, 216), (1080, 1080), (84, 0, 216, 216)),     # square source: pillarbox, centered
    ((384, 216), (2560, 1080), (0, 27, 384, 162)),     # ultrawide: letterbox
    ((384, 216), (0, 0), (0, 0, 384, 216)),            # degenerate source: fill
])
def test_aspect_fit(dest, src, expected):
    assert dt.aspect_fit(dest[0], dest[1], src[0], src[1]) == expected


class FakeDwm:
    def __init__(self, src_size=(1920, 1080), fail_update=False):
        self.calls = []
        self.src_size = src_size
        self.fail_update = fail_update
        self._next = 100

    def register(self, dest, src):
        self._next += 1
        self.calls.append(("register", dest, src, self._next))
        return self._next

    def unregister(self, thumb):
        self.calls.append(("unregister", thumb))

    def update(self, thumb, rect, visible=True, opacity=255, client_only=True):
        if self.fail_update:
            raise OSError("dwm gone")
        self.calls.append(("update", thumb, rect, visible, opacity, client_only))

    def query_source_size(self, thumb):
        return self.src_size


def test_thumbnail_handle_lifecycle_records_calls():
    fake = FakeDwm()
    h = dt.Thumbnail(dest_hwnd=111, src_hwnd=222, dwm=fake)
    h.show((0, 20, 384, 236))
    h.close()
    kinds = [c[0] for c in fake.calls]
    assert kinds == ["register", "update", "unregister"]
    # Thumbnail.show() passes dest_rect through UNCHANGED — letterboxing is the
    # tile's job (preview_tile._push_thumb_rect), not this class's.
    assert fake.calls[1][2] == (0, 20, 384, 236)


def test_close_swallows_unregister_errors():
    class Boom(FakeDwm):
        def unregister(self, thumb):
            raise OSError("already dead")

    h = dt.Thumbnail(dest_hwnd=1, src_hwnd=2, dwm=Boom())
    h.show((0, 0, 10, 10))
    h.close()  # must not raise
