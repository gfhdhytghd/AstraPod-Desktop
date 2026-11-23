"""Compare the production Classic LCD renderer with a local 320×240 reference.

Capture the old revision with --capture, then pass that PNG as --baseline
when running the comparison on a newer revision.
"""
from __future__ import annotations

import argparse
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from PIL import Image, ImageChops, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SIZE = (320, 240)
# Reference coordinates stay fixed across revisions. The panel mask covers
# the music note and count, leaving the split and the visible panel backing.
PANEL_CONTENT = (190, 54, 279, 188)
REGIONS = {
    "Overall": (0, 0, 320, 240),
    "Title bar": (0, 0, 160, 20),
    "Title text": (0, 0, 40, 20),
    "Battery": (126, 0, 158, 18),
    "Music": (0, 20, 160, 45),
    "Videos": (0, 45, 160, 69),
    "Photos": (0, 69, 160, 94),
    "Podcasts": (0, 94, 160, 118),
    "Extras": (0, 118, 160, 143),
    "Settings": (0, 143, 160, 167),
    "Shuffle Songs": (0, 167, 160, 192),
    "Menu below the rows": (0, 192, 160, 240),
    "Selection fill (between label and chevron)": (56, 20, 140, 45),
    "Chevron": (140, 20, 160, 45),
    "Split and adjacent edges": (157, 0, 182, 240),
    "Panel beside the title bar": (182, 0, 320, 20),
    "Right panel backing": (182, 20, 320, 240),
}


def read_lcd(path: Path) -> Image.Image:
    with Image.open(path) as image:
        if image.size != SIZE:
            raise ValueError(f"{path}: expected {SIZE[0]}×{SIZE[1]}, got {image.size}")
        return image.convert("RGB")


def capture_lcd() -> Image.Image:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from astrapod_desktop.pages.device import IPodDrawing
    from astrapod_desktop.qt import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication([])
    drawing = IPodDrawing(SimpleNamespace(library_state=None))
    drawing.connected = drawing.stock = True
    # No device, glare, frame, or cover. _paint_screen is also used by paintEvent.
    lcd = QtGui.QImage(*SIZE, QtGui.QImage.Format.Format_RGB32)
    lcd.setDevicePixelRatio(1)
    painter = QtGui.QPainter(lcd)
    try:
        drawing._paint_screen(painter, QtCore.QRectF(0, 0, *SIZE))
    finally:
        painter.end()
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    if not lcd.save(buffer, "PNG"):
        raise RuntimeError("Could not encode the LCD render")
    with Image.open(io.BytesIO(bytes(buffer.data()))) as image:
        result = image.convert("RGB")
    drawing.close()
    app.quit()
    return result


def compare(reference, render, mask, heatmap_path):
    difference = ImageChops.difference(reference, render)
    mean = difference.convert("L", (1 / 3, 1 / 3, 1 / 3, 0))
    heatmap = ImageOps.colorize(mean, "#080912", "#fff77e", "#ed3448",
                                midpoint=70)
    heatmap.paste("#454851", (0, 0), ImageOps.invert(mask))
    heatmap.save(heatmap_path)

    metrics = {}
    for name, box in REGIONS.items():
        region_mask = mask.crop(box)
        pixels = region_mask.histogram()[255]
        histogram = difference.crop(box).histogram(mask=region_mask)
        error = sum((index % 256) * count for index, count in enumerate(histogram))
        metrics[name] = pixels, error / (3 * pixels)
    return metrics


def save_composite(reference, baseline, current, output):
    frames = [
        ("Firmware reference", reference),
        ("Before", baseline),
        ("After", current),
        ("50% reference / after overlay", Image.blend(reference, current, 0.5)),
    ]
    width, height = SIZE
    canvas = Image.new("RGB", (len(frames) * (width + 8) + 8, height + 34), "#20232b")
    draw = ImageDraw.Draw(canvas)
    for index, (label, frame) in enumerate(frames):
        left = 8 + index * (width + 8)
        draw.text((left, 7), label, fill="white")
        canvas.paste(frame, (left, 26))
    canvas.save(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, help="Only save the current LCD to this PNG")
    parser.add_argument("--reference", type=Path, help="Local firmware reference PNG")
    parser.add_argument("--baseline", type=Path, help="Old LCD PNG saved with --capture")
    parser.add_argument("--output", type=Path, help="Directory for comparison artifacts")
    args = parser.parse_args()
    if not args.capture and not (args.reference and args.baseline and args.output):
        parser.error("provide --capture or all of --reference, --baseline and --output")
    try:
        if args.capture:
            capture_lcd().save(args.capture)
            return
        reference = read_lcd(args.reference)
        baseline = read_lcd(args.baseline)
        current = capture_lcd()
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"{exc}\n")

    args.output.mkdir(parents=True, exist_ok=True)
    mask = Image.new("L", SIZE, 255)
    mask.paste(0, PANEL_CONTENT)
    before = compare(reference, baseline, mask, args.output / "before-diff.png")
    after = compare(reference, current, mask, args.output / "after-diff.png")
    current.save(args.output / "after-lcd.png")
    save_composite(reference, baseline, current, args.output / "side-by-side.png")
    lines = [
        "320×240 LCD at DPR 1. RGB mean absolute error per channel (0–255).",
        "Region boxes use half-open LCD coordinates (left, top, right, bottom).",
        f"Masked reference panel content: {PANEL_CONTENT}. Mask appears grey in heatmaps.",
        "The preview uses an empty cover panel to measure its backing.",
        "",
        f"{'Region':43} {'Box':23} {'Pixels':>7} {'Before':>8} {'After':>8} {'Delta':>8}",
    ]
    for name, (pixels, previous) in before.items():
        current_error = after[name][1]
        lines.append(f"{name:43} {str(REGIONS[name]):23} {pixels:7d} {previous:8.3f}"
                     f" {current_error:8.3f} {current_error - previous:+8.3f}")
    report = "\n".join(lines) + "\n"
    (args.output / "metrics.txt").write_text(report, encoding="utf-8")
    print(report, end="")


if __name__ == "__main__":
    main()
