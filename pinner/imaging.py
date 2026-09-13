"""2:3 vertical image compositor — Pinterest-optimized media.

Marketplace product shots are mostly square; Pinterest favors 2:3 vertical.
``to_vertical`` letterboxes onto a 2:3 canvas painted with the image's
dominant color, producing a clean, native-looking vertical pin. Images that
are already ~2:3 pass through untouched (bytes preserved).

Pure bytes-in/bytes-out: usable directly as the runner's image_fetcher
wrapper, independently testable, no filesystem."""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

RATIO_W, RATIO_H = 2, 3
TOLERANCE = 0.02  # already-vertical passthrough band
JPEG_QUALITY = 88
# Pinterest-recommended exact pin canvas (1000x1500, 2:3). Padding-path
# canvases are this size; native 2:3 sources still pass through untouched.
CANVAS_W, CANVAS_H = 1000, 1500


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

    if current >= 1.0:
        logger.warning(
            "[imaging] %sx%s source is %s — padding to 2:3 for Pinterest reach",
            width, height, "square" if current == 1.0 else "horizontal",
        )

    # Dominant color: 1x1 average (no deprecated getdata)
    dominant = src.resize((1, 1)).getpixel((0, 0))

    # Exact Pinterest-optimal canvas; the source is centered at native size
    # (downscaled only if larger than the canvas) — no quality-losing upscale.
    canvas_w, canvas_h = CANVAS_W, CANVAS_H
    if width > canvas_w or height > canvas_h:
        scale = min(canvas_w / width, canvas_h / height)
        src = src.resize((max(1, int(width * scale)), max(1, int(height * scale))))
        width, height = src.size

    canvas = Image.new("RGB", (canvas_w, canvas_h), dominant)
    canvas.paste(src, ((canvas_w - width) // 2, (canvas_h - height) // 2))
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=quality)
    return out.getvalue()
