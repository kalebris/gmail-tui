"""Turn an attached image into a sixel sequence.

``img2sixel`` does the encoding. It is the libsixel tool, already on this
machine. A Python ``sixel`` package would also do it, but it needs Pillow and
this Python has no installer. Without the tool, or on a terminal that cannot
draw sixel, the message is text only.
"""

from __future__ import annotations

import os
import select
import shutil
import subprocess
import sys
import tempfile
import termios
import time
import tty
from pathlib import Path

IMAGE_LIMIT = 2


def band_rows(window: int, count: int = 1) -> int:
    """Terminal rows one picture may fill inside the reader."""
    count = max(1, count)
    # Stay inside the visible reader. A taller band was never drawn.
    room = max(1, window)
    if count == 1:
        return max(1, room - 1)
    return max(1, room // count)


def image_band(
    lines: list[str],
    attachments: tuple,
    enabled: bool,
    preferred: int | None = None,
    rows: int | None = None,
) -> tuple[list[str], list[tuple[int, int]]]:
    """Blank rows where an attached picture will be drawn under the file list.

    Pass ``preferred`` when the attachments pane (or view-image) has focus;
    otherwise leave it unset so the reader stays text-only. The band is inserted
    at the blank line after the attachment list so headers and filenames stay
    on screen above the picture, with the body below.
    """
    if not enabled or preferred is None:
        return lines, []
    images = [item for item in attachments if is_bitmap(item.content_type)]
    chosen = next((item for item in images if item.index == preferred), None)
    if chosen is None:
        return lines, []
    height = rows if rows is not None else band_rows(24, 1)
    height = max(1, height)
    # First blank separates headers/attachments from the body.
    split = next((index for index, line in enumerate(lines) if line == ""), len(lines))
    band = [""] * height
    slots = [(split, chosen.index)]
    return lines[:split] + band + lines[split:], slots


def is_bitmap(content_type: str) -> bool:
    """True for an attached picture. Vector images and remote URLs are not."""
    kind = content_type.lower().split(";", 1)[0].strip()
    return kind.startswith("image/") and kind != "image/svg+xml"


def sixel_in_reply(reply: str) -> bool:
    """True when a primary device-attributes reply lists sixel (parameter 4)."""
    start = reply.find("\x1b[?")
    if start < 0:
        return False
    end = reply.find("c", start)
    if end < 0:
        return False
    parts = reply[start + 3 : end].split(";")
    return "4" in parts


def cell_size(reply: str) -> tuple[int, int] | None:
    """Pixel size of one cell from a ``CSI 6 ; height ; width t`` reply."""
    marker = "\x1b[6;"
    start = reply.find(marker)
    if start < 0:
        return None
    end = reply.find("t", start)
    if end < 0:
        return None
    bits = reply[start + len(marker) : end].split(";")
    if len(bits) != 2:
        return None
    try:
        height = int(bits[0])
        width = int(bits[1])
    except ValueError:
        return None
    if height <= 0 or width <= 0:
        return None
    return width, height


def probe_terminal() -> tuple[bool, tuple[int, int]]:
    """Ask the terminal once. A missing answer means no pictures."""
    cell = (8, 16)
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        return False, cell
    fd = sys.stdin.fileno()
    try:
        saved = termios.tcgetattr(fd)
    except termios.error:
        return False, cell
    try:
        tty.setcbreak(fd)
        os.write(sys.stdout.fileno(), b"\x1b[c\x1b[16t")
        data = _read_reply(fd)
    except OSError:
        return False, cell
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    text = data.decode("ascii", "replace")
    found = cell_size(text)
    return sixel_in_reply(text), found or cell


def encode_image(data: bytes, width: int, height: int) -> bytes | None:
    """Sixel bytes for a picture fitted inside ``width`` by ``height`` pixels."""
    if width < 8 or height < 6 or not data or shutil.which("img2sixel") is None:
        return None
    fitted_w, fitted_h = _fit(data, width, height)
    # Big phone photos need a file and a longer wait. Stdin plus an 8s limit
    # left the reader empty after the pane grew.
    timeout = 45 if len(data) >= 1_000_000 else 15
    temporary: Path | None = None
    try:
        # 15-bit color keeps photos accurate. A 16-color pass looked posterized.
        command = [
            "img2sixel",
            "-w",
            str(fitted_w),
            "-h",
            str(fitted_h),
            "-I",
        ]
        if len(data) >= 256_000:
            handle, name = tempfile.mkstemp(prefix="gmail-tui-sixel-", suffix=_suffix(data))
            os.close(handle)
            temporary = Path(name)
            temporary.write_bytes(data)
            command.append(str(temporary))
            done = subprocess.run(
                command,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        else:
            done = subprocess.run(
                command,
                input=data,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    payload = done.stdout
    if done.returncode != 0 or not payload.startswith(b"\x1bP"):
        return None
    size = sixel_raster_size(payload)
    if size is not None and (size[0] > width or size[1] > height):
        return None
    return payload


def sixel_raster_size(payload: bytes) -> tuple[int, int] | None:
    """Pixel size from a sixel raster attribute ``"1;1;width;height``."""
    marker = payload.find(b'"')
    if marker < 0:
        return None
    end = marker + 1
    while end < len(payload) and payload[end] in b"0123456789;":
        end += 1
    bits = payload[marker + 1 : end].split(b";")
    if len(bits) < 4:
        return None
    try:
        width = int(bits[2])
        height = int(bits[3])
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _suffix(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    return ".img"


def _fit(data: bytes, width: int, height: int) -> tuple[int, int]:
    size = _pixel_size(data)
    if size is None or size[0] <= 0 or size[1] <= 0:
        return width, height
    src_w, src_h = size
    scale = min(width / src_w, height / src_h)
    return max(1, int(src_w * scale)), max(1, int(src_h * scale))


def _pixel_size(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data.startswith(b"\xff\xd8"):
        return _jpeg_size(data)
    if data.startswith(b"P6") or data.startswith(b"P5"):
        return _pnm_size(data)
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    index = 2
    length = len(data)
    while index + 8 < length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in {0xC0, 0xC1, 0xC2}:
            height = int.from_bytes(data[index + 5 : index + 7], "big")
            width = int.from_bytes(data[index + 7 : index + 9], "big")
            if width > 0 and height > 0:
                return width, height
            return None
        if marker == 0xD8:
            index += 2
            continue
        if index + 4 > length:
            return None
        step = int.from_bytes(data[index + 2 : index + 4], "big")
        if step < 2:
            return None
        index += 2 + step
    return None


def _pnm_size(data: bytes) -> tuple[int, int] | None:
    header = data.split(b"\n", 3)
    if len(header) < 3:
        return None
    bits = header[1].split()
    if len(bits) < 2:
        return None
    try:
        width = int(bits[0])
        height = int(bits[1])
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _read_reply(fd: int) -> bytes:
    data = b""
    deadline = time.monotonic() + 0.15
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            if data:
                break
            continue
        chunk = os.read(fd, 512)
        if not chunk:
            break
        data += chunk
        if b"c" in data and b"t" in data:
            break
    return data
