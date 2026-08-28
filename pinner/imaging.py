"""2:3 vertical image compositor — Pinterest-optimized media.

Marketplace product shots are mostly square; Pinterest favors 2:3 vertical.
``to_vertical`` letterboxes onto a 2:3 canvas painted with the image's
dominant color, producing a clean, native-looking vertical pin. Images that
are already ~2:3 pass through untouched (bytes preserved).

Pure bytes-in/bytes-out: usable directly as the runner's image_fetcher
wrapper, independently testable, no filesystem."""

from __future__ import annotations

import io

RATIO_W, RATIO_H = 2, 3
TOLERANCE = 0.02  # already-vertical passthrough band
JPEG_QUALITY = 88
MAX_EDGE = 1500  # cap canvas height; Pinterest sweet spot, payload-friendly


def to_vertical(image_bytes: bytes, *, quality: int = JPEG_QUALITY) -> bytes:
    from PIL import Image

    src = Image.open(io.BytesIO(image_bytes))
    src.load()
    if src.mode != "RGB":
        src = src.convert("RGB")
    width, height = src.size
    if width < 8 or height < 8:
        raise ValueError(f"image too small to composite: {width}x{height}")

    target_ratio = RATIO_W / RATIO_H
    current = width / height
    if abs(current - target_ratio) <= TOLERANCE and height >= width:
        return image_bytes  # already vertical 2:3 — keep original bytes

    # Dominant color: 1x1 average (no deprecated getdata)
    dominant = src.resize((1, 1)).getpixel((0, 0))

    canvas_h = max(height, int(round(width / target_ratio)))
    canvas_h = min(canvas_h, MAX_EDGE)
    canvas_w = min(int(round(canvas_h * target_ratio)), MAX_EDGE)
    # If the cap shrunk the canvas below fitting the source, scale source down
    if width > canvas_w or height > canvas_h:
        scale = min(canvas_w / width, canvas_h / height)
        src = src.resize((max(1, int(width * scale)), max(1, int(height * scale))))
        width, height = src.size
        canvas_h = max(height, int(round(width / target_ratio)))
        canvas_w = int(round(canvas_h * target_ratio))

    canvas = Image.new("RGB", (canvas_w, canvas_h), dominant)
    canvas.paste(src, ((canvas_w - width) // 2, (canvas_h - height) // 2))
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=quality)
    return out.getvalue()
