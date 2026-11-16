"""Sixel is optional. The message text does not wait for a picture."""

from __future__ import annotations

import shutil
import threading
import unittest
from types import SimpleNamespace

from gmail_tui.sixel import (
    band_rows,
    cell_size,
    encode_image,
    image_band,
    is_bitmap,
    sixel_in_reply,
    sixel_raster_size,
)
from gmail_tui.textutil import Attachment
from gmail_tui.tui import _SixelPump


class SixelTests(unittest.TestCase):
    def test_device_attributes_name_sixel(self) -> None:
        self.assertTrue(sixel_in_reply("\x1b[?62;4;9c"))
        self.assertFalse(sixel_in_reply("\x1b[?62;9c"))
        self.assertFalse(sixel_in_reply(""))
        self.assertEqual(cell_size("\x1b[6;16;8t"), (8, 16))
        self.assertIsNone(cell_size("no"))

    def test_a_selected_picture_uses_the_reader_height(self) -> None:
        self.assertEqual(band_rows(20, 1), 19)
        self.assertLessEqual(band_rows(12, 1), 12)
        self.assertGreater(band_rows(20, 1), band_rows(20, 2))

    def test_only_bitmap_attachments_reserve_rows(self) -> None:
        picture = Attachment(1, "cat.jpg", "image/jpeg", 10)
        sheet = Attachment(2, "notes.pdf", "application/pdf", 10)
        drawing = Attachment(3, "logo.svg", "image/svg+xml", 10)
        lines = ["From: Ada", "Attachments:", "  1  cat.jpg", "", "Hello body"]
        plain, slots = image_band(lines, (picture, sheet, drawing), False)
        self.assertEqual(plain, lines)
        self.assertEqual(slots, [])
        # Without a preferred attachment the reader stays text-only.
        idle, idle_slots = image_band(lines, (picture, sheet, drawing), True, rows=6)
        self.assertEqual(idle, lines)
        self.assertEqual(idle_slots, [])
        later = Attachment(4, "dog.png", "image/png", 10)
        preferred, preferred_slots = image_band(
            lines, (picture, later, sheet), True, preferred=4, rows=10
        )
        # Picture sits under the attachment list; body stays after the band.
        self.assertEqual(preferred_slots, [(3, 4)])
        self.assertEqual(preferred[:3], lines[:3])
        self.assertEqual(preferred[3:13], [""] * 10)
        self.assertEqual(preferred[13], "")
        self.assertEqual(preferred[14], "Hello body")
        self.assertTrue(is_bitmap("image/png"))
        self.assertFalse(is_bitmap("image/svg+xml"))
        self.assertFalse(is_bitmap("application/pdf"))

    def test_img2sixel_encodes_a_bitmap(self) -> None:
        if shutil.which("img2sixel") is None:
            self.skipTest("img2sixel is not installed")
        picture = b"P6\n8 4\n255\n" + bytes([20, 40, 200]) * 32
        encoded = encode_image(picture, 32, 16)
        self.assertIsNotNone(encoded)
        assert encoded is not None
        self.assertTrue(encoded.startswith(b"\x1bP"))
        self.assertIn(b"\x1b\\", encoded)
        self.assertEqual(sixel_raster_size(encoded), (32, 16))
        too_tall = encode_image(picture, 8, 4)
        # A raster larger than the box is rejected so it cannot leave the pane.
        if too_tall is not None:
            size = sixel_raster_size(too_tall)
            self.assertIsNotNone(size)
            assert size is not None
            self.assertLessEqual(size[0], 8)
            self.assertLessEqual(size[1], 4)

    def test_a_cached_picture_does_not_rearm_ready(self) -> None:
        desk = SimpleNamespace(stop=threading.Event(), session=None)
        pump = _SixelPump(desk)
        session = SimpleNamespace(sixel_frames=(), sixel_pending=True, sixel_ready=False)
        pixels = (80, 40)
        pump.cache[("m1", 1, pixels)] = b"\x1bPq"
        pump.ask(session, "m1", (1,), pixels)
        self.assertEqual(session.sixel_frames, (("m1", 1, b"\x1bPq"),))
        self.assertFalse(session.sixel_pending)
        self.assertFalse(session.sixel_ready)

    def test_preload_does_not_cancel_a_warm_encode(self) -> None:
        desk = SimpleNamespace(stop=threading.Event(), session=None)
        pump = _SixelPump(desk)
        session = SimpleNamespace(sixel_frames=(), sixel_pending=False, sixel_ready=False)
        pixels = (80, 40)
        pump.cache[("m1", 1, pixels)] = b"\x1bPq"
        pump.generation = 4
        pump.queue = [("m1", 2, pixels)]
        pump.ask(session, "m1", (1,), pixels, preload=(2, 3))
        self.assertEqual(pump.generation, 4)
        self.assertEqual(pump.queue, [("m1", 2, pixels), ("m1", 3, pixels)])
        self.assertFalse(session.sixel_ready)
        self.assertFalse(session.sixel_pending)
