"""Test drawings for JARVIS Architect with known answers (run with the architect venv: OpenCV, Pillow, ezdxf):
    ~/.local/share/jarvis/architect/.venv/bin/python tests/architect/make_plans.py tests/fixtures/architect

Every drawing is a real raster/PDF/DXF file the pipeline reads like any user file; truth.json holds what a correct
reading must find (rooms with areas, number of doors/windows, the overall size)."""
from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/noto/NotoSans-Regular.ttf"
PX = 10.0               # mm per pixel in the generated drawings
M = 150                 # margin, px

# The house (mm): 9 × 6 m, exterior 300, interior 120. Bedroom at the bottom, kitchen + living room at the top.
EXT = [((0, 0), (9000, 0)), ((9000, 0), (9000, 6000)), ((9000, 6000), (0, 6000)), ((0, 6000), (0, 0))]
INT = [((0, 3000), (9000, 3000)), ((4000, 3000), (4000, 6000))]
ROOMS = [("Кухня", (2000, 4500)), ("Гостиная", (6500, 4500)), ("Спальня", (4500, 1500))]
# doors: (wall a, wall b, centre mm, width); windows likewise
DOORS = [((4000, 3000), (4000, 6000), (4000, 4200), 900), ((0, 3000), (9000, 3000), (2000, 3000), 900),
         ((0, 0), (9000, 0), (7500, 0), 1000)]
WINDOWS = [((0, 6000), (9000, 6000), (2000, 6000), 1400), ((0, 6000), (9000, 6000), (6500, 6000), 1800),
           ((0, 0), (9000, 0), (3000, 0), 1500)]
FURN = [("sofa", (6500, 5400), 2100, 900, 0), ("bed", (4500, 1100), 1600, 2000, 0), ("table", (2000, 4300), 1200, 800, 0)]


def P(p, h_mm=6000):
    """mm (Y up) → px (Y down)."""
    return (M + p[0] / PX, M + (h_mm - p[1]) / PX)


def canvas(w_mm=9000, h_mm=6000):
    return Image.new("RGB", (int(w_mm / PX + 2 * M), int(h_mm / PX + 2 * M)), "white")


def text(dr, xy, s, size=26, fill="black", anchor="mm"):
    dr.text(xy, s, font=ImageFont.truetype(FONT, size), fill=fill, anchor=anchor)


def wall_rect(a, b, t):
    ax, ay = P(a)
    bx, by = P(b)
    h = t / PX / 2
    if abs(ay - by) < 1e-6:
        x0, x1 = sorted((ax, bx))
        return [x0 - h, ay - h, x1 + h, ay + h]
    y0, y1 = sorted((ay, by))
    return [ax - h, y0 - h, ax + h, y1 + h]


def opening_box(a, b, c, w, t):
    cx, cy = P(c)
    hw, h = w / PX / 2, t / PX / 2 + 1
    if abs(a[1] - b[1]) < 1e-6:
        return [cx - hw, cy - h, cx + hw, cy + h], "h"
    return [cx - h, cy - hw, cx + h, cy + hw], "v"


def draw_filled(dr, doors=True, windows=True, dims=False, labels=True, furniture=False):
    for a, b in EXT:
        dr.rectangle(wall_rect(a, b, 300), fill="black")
    for a, b in INT:
        dr.rectangle(wall_rect(a, b, 120), fill="black")
    if doors:
        for a, b, c, w in DOORS:
            t = 300 if (a, b) in EXT else 120
            box, ori = opening_box(a, b, c, w, t)
            dr.rectangle(box, fill="white")
            x0, y0, x1, y1 = box
            r = w / PX
            if ori == "h":
                dr.line([x0, y0, x0, y0 - r], fill="black", width=2)
                dr.arc([x0 - r, y0 - r, x0 + r, y0 + r], 270, 360, fill="black", width=1)
            else:
                dr.line([x1, y0, x1 + r, y0], fill="black", width=2)
                dr.arc([x1 - r, y0 - r, x1 + r, y0 + r], 0, 90, fill="black", width=1)
    if windows:
        for a, b, c, w in WINDOWS:
            box, ori = opening_box(a, b, c, w, 300)
            dr.rectangle(box, fill="white", outline="black", width=1)
            x0, y0, x1, y1 = box
            if ori == "h":
                dr.line([x0, (y0 + y1) / 2, x1, (y0 + y1) / 2], fill="black", width=1)
    if labels:
        for name, p in ROOMS:
            text(dr, P(p), name, 30)
    if furniture:
        for kind, c, w, d, rot in FURN:
            cx, cy = P(c)
            dr.rectangle([cx - w / PX / 2, cy - d / PX / 2, cx + w / PX / 2, cy + d / PX / 2], outline=(80, 80, 80), width=1)
            text(dr, (cx, cy), {"sofa": "диван", "bed": "кровать", "table": "стол"}[kind], 16, fill=(80, 80, 80))
    if dims:
        y = P((0, 0))[1] + 70
        x0, x1 = P((0, 0))[0], P((9000, 0))[0]
        dr.line([x0, y, x1, y], fill="black", width=1)
        for x in (x0, x1):
            dr.line([x, y - 10, x, y + 10], fill="black", width=1)
        text(dr, ((x0 + x1) / 2, y - 18), "9000", 24)
        x = P((9000, 0))[0] + 70
        y0, y1 = P((0, 6000))[1], P((0, 0))[1]
        dr.line([x, y0, x, y1], fill="black", width=1)
        for yy in (y0, y1):
            dr.line([x - 10, yy, x + 10, yy], fill="black", width=1)
        im = Image.new("RGBA", (200, 60), (255, 255, 255, 0))
        text(ImageDraw.Draw(im), (100, 30), "6000", 24)
        return im.rotate(90, expand=True), (int(x + 12), int((y0 + y1) / 2 - 100))
    return None


def double_line(dr):
    """Walls as two thin lines (a CAD export), doors as gaps with swings, windows as three thin lines."""
    def rect_lines(a, b, t, gaps):
        x0, y0, x1, y1 = wall_rect(a, b, t)
        hor = abs(a[1] - b[1]) < 1e-6
        cuts = sorted(gaps)
        if hor:
            for yy in (y0, y1):
                xs = [x0] + [v for g in cuts for v in g] + [x1]
                for i in range(0, len(xs), 2):
                    dr.line([xs[i], yy, xs[i + 1], yy], fill="black", width=2)
        else:
            for xx in (x0, x1):
                ys = [y0] + [v for g in cuts for v in g] + [y1]
                for i in range(0, len(ys), 2):
                    dr.line([xx, ys[i], xx, ys[i + 1]], fill="black", width=2)
    allw = [(a, b, 300) for a, b in EXT] + [(a, b, 120) for a, b in INT]
    for a, b, t in allw:
        gaps = []
        for da, db, c, w in DOORS:
            if (da, db) == (a, b):
                box, ori = opening_box(a, b, c, w, t)
                gaps.append((box[0], box[2]) if ori == "h" else (box[1], box[3]))
        rect_lines(a, b, t, gaps)
        for da, db, c, w in DOORS:
            if (da, db) == (a, b):
                box, ori = opening_box(a, b, c, w, t)
                x0, y0, x1, y1 = box
                for q in ((x0, y0, x0, y1), (x1, y0, x1, y1)) if ori == "h" else ((x0, y0, x1, y0), (x0, y1, x1, y1)):
                    dr.line(q, fill="black", width=2)
                r = w / PX
                if ori == "h":
                    dr.line([x0, y0, x0, y0 - r], fill="black", width=1)
                    dr.arc([x0 - r, y0 - r, x0 + r, y0 + r], 270, 360, fill="black", width=1)
                else:
                    dr.line([x1, y0, x1 + r, y0], fill="black", width=1)
                    dr.arc([x1 - r, y0 - r, x1 + r, y0 + r], 0, 90, fill="black", width=1)
    for a, b, c, w in WINDOWS:
        box, ori = opening_box(a, b, c, w, 300)
        x0, y0, x1, y1 = box
        dr.rectangle([x0, y0 + 2, x1, y1 - 2], fill="white")
        for yy in (y0 + 1, (y0 + y1) / 2, y1 - 1):
            dr.line([x0, yy, x1, yy], fill="black", width=1)
        dr.line([x0, y0, x0, y1], fill="black", width=2)
        dr.line([x1, y0, x1, y1], fill="black", width=2)
    for name, p in ROOMS:
        text(dr, P(p), name, 30)


def sketch(out: Path):
    """A hand drawing like the one in the task: single wobbly pen lines, capital letters, no sizes."""
    rnd = random.Random(7)
    img = Image.new("RGB", (1000, 760), (250, 249, 244))
    dr = ImageDraw.Draw(img)

    def pen(x0, y0, x1, y1):
        n = 24
        pts = []
        for i in range(n + 1):
            t = i / n
            jx, jy = rnd.gauss(0, 1.3), rnd.gauss(0, 1.3)
            pts.append((x0 + (x1 - x0) * t + jx, y0 + (y1 - y0) * t + jy))
        dr.line(pts, fill=(25, 25, 60), width=4, joint="curve")
    L, T, R, B, MID, CX = 120, 100, 880, 660, 380, 500
    pen(L - 4, T, R + 3, T + 2)
    pen(R, T - 3, R + 2, B + 4)
    pen(R + 3, B, L - 2, B - 2)
    pen(L, B + 3, L - 1, T - 4)
    pen(L - 2, MID, R + 2, MID + 3)
    pen(CX, MID - 2, CX + 2, B + 2)
    f = ImageFont.truetype(FONT, 36)
    dr.text(((L + R) / 2, (T + MID) / 2), "СПАЛЬНЯ", font=f, fill=(25, 25, 60), anchor="mm")
    dr.text(((L + CX) / 2, (MID + B) / 2), "КУХНЯ", font=f, fill=(25, 25, 60), anchor="mm")
    dr.text(((CX + R) / 2, (MID + B) / 2), "ВАННА", font=f, fill=(25, 25, 60), anchor="mm")
    img.save(out)


def bad_photo(src: Path, out: Path):
    im = cv2.imread(str(src))
    h, w = im.shape[:2]
    M2 = cv2.getRotationMatrix2D((w / 2, h / 2), 1.2, 1.0)
    im = cv2.warpAffine(im, M2, (w, h), borderValue=(255, 255, 255))
    im = cv2.GaussianBlur(im, (5, 5), 1.6)
    im = (im.astype(np.float32) * 0.55 + 90).clip(0, 255)
    grad = np.linspace(0.75, 1.05, w, dtype=np.float32)[None, :, None]
    im = (im * grad).clip(0, 255)
    rng = np.random.default_rng(3)
    im = (im + rng.normal(0, 14, im.shape)).clip(0, 255).astype(np.uint8)
    cv2.imwrite(str(out), im, [cv2.IMWRITE_JPEG_QUALITY, 55])


def dxf(out: Path):
    import ezdxf
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    doc.layers.add("A-WALL")
    doc.layers.add("A-DOOR")
    doc.layers.add("A-ANNO")
    msp = doc.modelspace()

    def wall(a, b, t):
        hor = abs(a[1] - b[1]) < 1e-6
        h = t / 2
        if hor:
            x0, x1 = sorted((a[0], b[0]))
            for y in (a[1] - h, a[1] + h):
                msp.add_line((x0 - h, y), (x1 + h, y), dxfattribs={"layer": "A-WALL"})
        else:
            y0, y1 = sorted((a[1], b[1]))
            for x in (a[0] - h, a[0] + h):
                msp.add_line((x, y0 - h), (x, y1 + h), dxfattribs={"layer": "A-WALL"})
    for a, b in EXT:
        wall(a, b, 300)
    for a, b in INT:
        wall(a, b, 120)
    for a, b, c, w in DOORS:
        ux, uy = (b[0] - a[0], b[1] - a[1])
        n = math.hypot(ux, uy)
        ux, uy = ux / n, uy / n
        hinge = (c[0] - ux * w / 2, c[1] - uy * w / 2)                 # the hinge sits on the wall line
        a0 = math.degrees(math.atan2(uy, ux))
        msp.add_arc(hinge, w, a0, a0 + 90, dxfattribs={"layer": "A-DOOR"})
    for name, p in ROOMS:
        msp.add_text(name, height=250, dxfattribs={"layer": "A-ANNO", "insert": p})
    doc.saveas(out)


def main(dst: str):
    d = Path(dst)
    d.mkdir(parents=True, exist_ok=True)
    truth = {"size_mm": [9000, 6000], "rooms": {"Кухня": 12.0, "Гостиная": 15.0, "Спальня": 27.0},
             "doors": len(DOORS), "windows": len(WINDOWS), "px_mm": PX}
    # 1 — simple house, three rooms, sizes on the drawing
    img = canvas()
    dr = ImageDraw.Draw(img)
    rot = draw_filled(dr, doors=False, windows=False, dims=True)
    img.paste(rot[0], rot[1], rot[0])
    img.save(d / "simple3.png")
    # 2 — doors and windows (double-line CAD export)
    img = canvas()
    dr = ImageDraw.Draw(img)
    double_line(dr)
    img.save(d / "doors_windows.png")
    # 3 — furniture drawn on the plan
    img = canvas()
    dr = ImageDraw.Draw(img)
    rot = draw_filled(dr, dims=True, furniture=True)
    img.paste(rot[0], rot[1], rot[0])
    img.save(d / "furniture.png")
    # 4 — hand sketch
    sketch(d / "sketch.png")
    # 5 — full plan with sizes (the acceptance drawing «plan.png»)
    img = canvas()
    dr = ImageDraw.Draw(img)
    rot = draw_filled(dr, dims=True)
    img.paste(rot[0], rot[1], rot[0])
    img.save(d / "plan.png")
    img.save(d / "plan.webp", quality=92)
    img.save(d / "plan.pdf", resolution=100)
    # 6 — no scale anywhere
    img = canvas()
    dr = ImageDraw.Draw(img)
    draw_filled(dr)
    img.save(d / "noscale.png")
    # 7 — a bad photo of plan.png
    bad_photo(d / "plan.png", d / "bad.jpg")
    # DXF
    dxf(d / "plan.dxf")
    (d / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1), "utf-8")
    print("ok", sorted(p.name for p in d.iterdir()))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/architect")
