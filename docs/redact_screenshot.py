#!/usr/bin/env python3
"""Blur only From/To/Subject in the mail list; leave dates, counts, chrome sharp."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageFilter

SRC = Path(
    "/home/kalebris/.cursor/projects/home-kalebris-projects-work-GTUI"
    "/assets/image-67c2c165-fbb4-41cf-9896-ae4c6e68c839.png"
)
OUT_PNG = Path(__file__).resolve().parent / "screenshot.png"
OUT_JPG = Path(__file__).resolve().parent / "screenshot.jpg"

# On this capture the header pipes are at x≈251, 264, 313 (after U, A, Date).
# Blur starts just after the Date pipe; Labels on the far right stay sharp.
LEFT, RIGHT = 320, 880
Y0, Y1 = 24, 32


def main() -> None:
    im = Image.open(SRC).convert("RGB")
    crop = im.crop((LEFT, Y0, RIGHT, Y1))
    im.paste(crop.filter(ImageFilter.GaussianBlur(radius=5.0)), (LEFT, Y0))
    im.save(OUT_PNG)
    im.save(OUT_JPG, "JPEG", quality=100, subsampling=0)
    print(f"wrote {OUT_PNG} From/To/Subject x={LEFT}:{RIGHT} y={Y0}:{Y1}")


if __name__ == "__main__":
    main()
