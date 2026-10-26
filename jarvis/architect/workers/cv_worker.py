"""JARVIS Architect — drawing worker (OpenCV, pdfium, ezdxf). Runs in its own venv (~/.local/share/jarvis/architect/.venv,
scripts/install.sh) as a long-lived child of the core; one JSON request per stdin line, one JSON reply per stdout line.

Ops:
  ping                       versions
  raster {path, out, page}   PDF page / WEBP / anything → PNG (pdfium for PDF)
  walls  {path, annotated}   wall segments, opening gaps and image quality in pixels of the original image, plus an
                             annotated copy (every candidate numbered W1…, O1…) for the vision step
  dxf    {path}              a DXF drawing → lines, arcs, texts, blocks in millimetres
  write_dxf {model, out}     the master model as a 2D plan DXF (walls, doors with swings, windows, room names, sizes)
  check_dxf {path}           read a DXF back: entity counts per layer (export verification)

The image is data: nothing in it is executed or followed; texts are read only by the vision step and only as data."""
from __future__ import annotations

import json
import math
import os
import sys
import traceback

OUT = os.fdopen(os.dup(1), "w", buffering=1)      # protocol channel; library prints go to stderr
os.dup2(2, 1)


def reply(obj: dict) -> None:
    OUT.write(json.dumps(obj, ensure_ascii=False) + "\n")
    OUT.flush()


# ----------------------------------------------------------------------------- raster input

def op_fit(path: str, out: str, max_side: int = 1568) -> dict:
    """A copy no larger than max_side (what the vision model sees); k = its pixels per original pixel."""
    import cv2
    import numpy as np
    im = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    h, w = im.shape[:2]
    k = min(1.0, max_side / max(h, w))
    if k < 1:
        im = cv2.resize(im, (round(w * k), round(h * k)), interpolation=cv2.INTER_AREA)
    cv2.imwrite(out, im)
    return {"out": out, "k": k, "w": im.shape[1], "h": im.shape[0]}


def op_raster(path: str, out: str, page: int = 1, max_side: int = 2600) -> dict:
    import cv2
    import numpy as np
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(path)
        n = len(pdf)
        if not 1 <= page <= n:
            raise ValueError(f"В PDF {n} стр., страницы {page} нет")
        pg = pdf[page - 1]
        w, h = pg.get_size()                 # points (1/72 in)
        scale = min(6.0, max(1.0, max_side / max(w, h)))
        img = pg.render(scale=scale).to_pil().convert("RGB")
        img.save(out)
        return {"out": out, "w": img.width, "h": img.height, "pages": n, "dpi": round(72 * scale)}
    data = np.fromfile(path, dtype=np.uint8)
    im = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if im is None:
        from PIL import Image          # WEBP / unusual formats
        pil = Image.open(path).convert("RGB")
        im = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    if im.ndim == 3 and im.shape[2] == 4:          # transparent PNG: white under it
        a = im[:, :, 3:4].astype(np.float32) / 255
        im = (im[:, :, :3].astype(np.float32) * a + 255 * (1 - a)).astype(np.uint8)
    h, w = im.shape[:2]
    cv2.imwrite(out, im)
    return {"out": out, "w": w, "h": h, "pages": 1}


# ----------------------------------------------------------------------------- wall detection

def _quality(gray) -> dict:
    import cv2
    import numpy as np
    lap = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    lo, hi = np.percentile(gray, [0.3, 99.7])
    contrast = float(hi - lo)
    noise = float(np.mean(np.abs(gray.astype(np.int16) - cv2.medianBlur(gray, 3).astype(np.int16))))
    dark = float((gray < 128).mean())
    score = min(1.0, contrast / 160) * min(1.0, 3.0 / max(noise, 0.5)) * (1.0 if 0.003 < dark < 0.5 else 0.6)
    return {"sharpness": round(lap, 1), "contrast": round(contrast, 1), "noise": round(noise, 2),
            "dark_share": round(dark, 4), "score": round(min(1.0, score), 3)}


def _binarize(gray, noisy: bool):
    import cv2
    import numpy as np
    h, w = gray.shape
    k = max(31, (min(h, w) // 12) | 1)
    small = cv2.resize(gray, (max(8, w // 4), max(8, h // 4)), interpolation=cv2.INTER_AREA)
    bg = cv2.resize(cv2.medianBlur(small, max(3, (k // 4) | 1)), (w, h), interpolation=cv2.INTER_LINEAR)
    norm = cv2.divide(gray, np.maximum(bg, 1), scale=255)        # even out shadows / paper tone (photos, scans)
    if noisy:
        norm = cv2.GaussianBlur(norm, (5, 5), 0)
    _, otsu = cv2.threshold(norm, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    otsu = cv2.morphologyEx(otsu, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3) if noisy else (2, 2)))
    return otsu


def _runs(mask, horizontal: bool, min_len: int) -> list[dict]:
    """Long straight strokes along one axis: [{a, b (along), c (centre across), t (thickness), n (pixels)}]."""
    import cv2
    import numpy as np
    kern = cv2.getStructuringElement(cv2.MORPH_RECT, (min_len, 1) if horizontal else (1, min_len))
    m = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kern)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        length = w if horizontal else h
        if length < min_len:
            continue
        sub = lab[y:y + h, x:x + w] == i
        counts = sub.sum(axis=0 if horizontal else 1)          # thickness at every step along the stroke
        counts = counts[counts > 0]
        t = float(np.median(counts)) if counts.size else 1.0
        ys, xs = np.nonzero(sub)
        across = (ys + y) if horizontal else (xs + x)
        out.append({"a": float(x if horizontal else y), "b": float(x + w - 1 if horizontal else y + h - 1),
                    "c": float(np.median(across)), "t": max(1.0, t), "n": int(area)})
    return out


def _pair_and_merge(runs: list[dict], thin: float, max_t: float, gap_small: float, gap_open: tuple[float, float],
                    wall_t: float = 0.0):
    """Double lines → one wall (thickness = spacing); collinear pieces → one wall; gaps of door/window size between
    pieces → opening candidates. Returns (walls, openings) along one axis."""
    runs = sorted(runs, key=lambda r: r["c"])
    used = set()
    walls = []
    for i, r in enumerate(runs):
        if i in used:
            continue
        if r["t"] <= thin * 2.2 and not (wall_t and r["t"] >= 0.3 * wall_t):
            best = None
            for j in range(i + 1, len(runs)):
                s = runs[j]
                d = s["c"] - r["c"]
                if d > max_t:
                    break
                if j in used or s["t"] > thin * 2.2 or (wall_t and s["t"] >= 0.3 * wall_t) or d < max(3.0, thin * 1.5):
                    continue
                ov = min(r["b"], s["b"]) - max(r["a"], s["a"])
                if ov >= 0.5 * min(r["b"] - r["a"], s["b"] - s["a"]):
                    # nothing parallel in between
                    between = any(runs[k]["c"] > r["c"] + 1 and runs[k]["c"] < s["c"] - 1 and
                                  min(runs[k]["b"], r["b"]) - max(runs[k]["a"], r["a"]) > 0.3 * ov
                                  for k in range(i + 1, j))
                    if not between:
                        best = j
                        break
            if best is not None:
                s = runs[best]
                used.update((i, best))
                walls.append({"a": min(r["a"], s["a"]), "b": max(r["b"], s["b"]), "c": (r["c"] + s["c"]) / 2,
                              "t": s["c"] - r["c"] + (r["t"] + s["t"]) / 2, "kind": "double", "conf": 0.85})
                continue
            used.add(i)
            walls.append({"a": r["a"], "b": r["b"], "c": r["c"], "t": r["t"], "kind": "single", "conf": 0.5})
        else:
            used.add(i)
            walls.append({"a": r["a"], "b": r["b"], "c": r["c"], "t": r["t"], "kind": "thick",
                          "conf": 0.9 if r["t"] > thin * 2.2 else 0.8})
    # merge collinear pieces; remember door/window-sized gaps
    walls.sort(key=lambda w: (round(w["c"] / max(2.0, thin * 2)), w["a"]))
    merged, openings = [], []
    for w in sorted(walls, key=lambda w: (w["c"], w["a"])):
        for m in merged:
            # same line: centres within half a thickness, plus ~2° of tilt over the distance (photos, sketches)
            span = abs((w["a"] + w["b"]) / 2 - (m["a"] + m["b"]) / 2)
            if abs(m["c"] - w["c"]) <= max(m["t"], w["t"]) / 2 + 2 + 0.035 * span and \
                    abs(m["t"] - w["t"]) <= max(m["t"], w["t"]) * 0.6 + 3:
                gap = w["a"] - m["b"]
                if gap <= max(gap_small, gap_open[0]):
                    m["b"] = max(m["b"], w["b"])
                    m["a"] = min(m["a"], w["a"])
                    break
                lo, hi = gap_open
                if lo <= gap <= hi:
                    openings.append({"c": (m["c"] + w["c"]) / 2, "a": m["b"], "b": w["a"], "t": max(m["t"], w["t"])})
                    m["b"] = max(m["b"], w["b"])
                    break
        else:
            merged.append(dict(w))
    # a thin line inside a found wall (window glass, a wall's outline) is not a wall of its own
    strong = [m for m in merged if m["kind"] != "single"]
    merged = [m for m in merged if m["kind"] != "single" or not any(
        abs(s["c"] - m["c"]) <= s["t"] / 2 + 2 and m["a"] >= s["a"] - 3 and m["b"] <= s["b"] + 3 for s in strong)]
    return merged, openings


def _glazing(binm, o: dict, horizontal: bool) -> str:
    """A gap in a wall with a thin line running through it along the wall (glazing) is a window; an empty gap is a
    door or a passage."""
    a, b, c, t = int(o["a"]) + 2, int(o["b"]) - 1, float(o["c"]), float(o.get("t", 6))
    if b - a < 4:
        return "door"
    lo, hi = int(c - t / 2) - 1, int(c + t / 2) + 2
    band = binm[max(0, lo):hi, a:b] if horizontal else binm[a:b, max(0, lo):hi].T
    if band.size == 0:
        return "door"
    frac = band.mean(axis=1) / 255.0
    return "window" if int((frac > 0.7).sum()) >= 1 else "door"


def _connect(segs: list[dict], tol_k: float = 1.0) -> None:
    """Extend / trim wall ends to meet perpendicular walls (corners and T-joins), in place. segs: x1,y1,x2,y2,t."""
    for s in segs:
        hor = abs(s["y2"] - s["y1"]) < abs(s["x2"] - s["x1"])
        for end in (1, 2):
            px, py = s[f"x{end}"], s[f"y{end}"]
            best = None
            for o in segs:
                if o is s:
                    continue
                ohor = abs(o["y2"] - o["y1"]) < abs(o["x2"] - o["x1"])
                if ohor == hor:
                    continue
                tol = max(s["t"], o["t"]) * tol_k + 6
                if hor:      # o vertical at x = o.x1 spanning y
                    ox = (o["x1"] + o["x2"]) / 2
                    lo, hi = sorted((o["y1"], o["y2"]))
                    if lo - tol <= py <= hi + tol and abs(px - ox) <= tol + s["t"]:
                        d = abs(px - ox)
                        if best is None or d < best[0]:
                            best = (d, ox, None, o)
                else:
                    oy = (o["y1"] + o["y2"]) / 2
                    lo, hi = sorted((o["x1"], o["x2"]))
                    if lo - tol <= px <= hi + tol and abs(py - oy) <= tol + s["t"]:
                        d = abs(py - oy)
                        if best is None or d < best[0]:
                            best = (d, None, oy, o)
            if best:
                if hor:
                    s[f"x{end}"] = best[1]
                else:
                    s[f"y{end}"] = best[2]
                o = best[3]
                # the other wall may stop short of this one: stretch it to this wall's centre line
                if hor:
                    lo, hi = sorted((o["y1"], o["y2"]))
                    if py < lo:
                        o["y1" if o["y1"] < o["y2"] else "y2"] = py
                    elif py > hi:
                        o["y2" if o["y2"] > o["y1"] else "y1"] = py
                else:
                    lo, hi = sorted((o["x1"], o["x2"]))
                    if px < lo:
                        o["x1" if o["x1"] < o["x2"] else "x2"] = px
                    elif px > hi:
                        o["x2" if o["x2"] > o["x1"] else "x1"] = px


def op_walls(path: str, annotated: str | None = None, max_side: int = 2000) -> dict:
    import cv2
    import numpy as np
    data = np.fromfile(path, dtype=np.uint8)
    im = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if im is None:
        raise ValueError("Не удалось прочитать изображение")
    H0, W0 = im.shape[:2]
    f = min(1.0, max_side / max(H0, W0))
    work = cv2.resize(im, (round(W0 * f), round(H0 * f)), interpolation=cv2.INTER_AREA) if f < 1 else im
    gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
    q = _quality(gray)
    noisy = q["noise"] > 3.0
    if noisy:                                       # a photo / scan: grain would read as short strokes
        gray = cv2.fastNlMeansDenoising(gray, None, min(30.0, q["noise"] * 3.5), 7, 21)
    binm = _binarize(gray, noisy)
    h, w = binm.shape
    # stroke widths: twice the distance to the background along stroke centres
    dt = cv2.distanceTransform(binm, cv2.DIST_L2, 3)
    ridge = (dt >= cv2.dilate(dt, np.ones((3, 3), np.uint8))) & (dt > 0)
    widths = dt[ridge] * 2
    thin = float(np.percentile(widths, 30)) if widths.size else 2.0
    thin = max(1.5, min(thin, 6.0))
    thick = float(np.percentile(widths, 92)) if widths.size else thin
    wall_t = thick if thick > 2.5 * thin else 0.0          # filled (poché) walls are much wider than lines
    min_len = int(max(18, 0.03 * min(h, w), 2.4 * wall_t) if wall_t else max(18, 0.05 * min(h, w)))
    hr = _runs(binm, True, min_len)
    vr = _runs(binm, False, min_len)
    max_t = max(8.0, 0.045 * min(h, w), wall_t * 1.6)
    gap_small = max(3.0, thin * 2.5, 0.02 * min(h, w))
    gap_open = (max(0.025 * min(h, w), wall_t * 1.5), 0.2 * min(h, w))
    hw, ho = _pair_and_merge(hr, thin, max_t, gap_small, gap_open, wall_t)
    vw, vo = _pair_and_merge(vr, thin, max_t, gap_small, gap_open, wall_t)
    _, raw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)   # thin lines kept (no opening)
    for o in ho:
        o["kind"] = _glazing(raw, o, True)
    for o in vo:
        o["kind"] = _glazing(raw, o, False)
    segs = [{"x1": s["a"], "y1": s["c"], "x2": s["b"], "y2": s["c"], "t": s["t"], "kind": s["kind"], "conf": s["conf"]}
            for s in hw] + \
           [{"x1": s["c"], "y1": s["a"], "x2": s["c"], "y2": s["b"], "t": s["t"], "kind": s["kind"], "conf": s["conf"]}
            for s in vw]
    n_strong = sum(1 for s in segs if s["kind"] != "single")
    mode = "walls" if n_strong >= 4 else "lines"          # a single-line drawing / hand sketch
    if mode == "walls":
        segs = [s for s in segs if s["kind"] != "single" or (max(abs(s["x2"] - s["x1"]), abs(s["y2"] - s["y1"])) > 0.25 * min(h, w))]
    else:
        for s in segs:
            s["conf"] = 0.7 if s["t"] >= thin * 1.2 else 0.6
    # diagonal walls (not along the axes)
    diag = []
    rest = binm.copy()
    for s in segs:
        cv2.line(rest, (int(s["x1"]), int(s["y1"])), (int(s["x2"]), int(s["y2"])), 0, int(s["t"]) + 6)
    lines = cv2.HoughLinesP(rest, 1, np.pi / 360, threshold=max(30, min_len), minLineLength=min_len * 2, maxLineGap=4)
    for ln in (lines[:, 0] if lines is not None else []):
        x1, y1, x2, y2 = map(float, ln)
        ang = abs(math.degrees(math.atan2(y2 - y1, x2 - x1))) % 180
        if min(ang, 180 - ang) < 10 or abs(ang - 90) < 10:
            continue
        if any(math.hypot(x1 - d["x1"], y1 - d["y1"]) < 8 and math.hypot(x2 - d["x2"], y2 - d["y2"]) < 8 for d in diag):
            continue
        diag.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2, "t": max(thin, 2.0), "kind": "diagonal", "conf": 0.45})
    segs = [s for s in segs if max(abs(s["x2"] - s["x1"]), abs(s["y2"] - s["y1"])) >= min_len]
    _connect(segs)
    segs += diag[:20]
    segs.sort(key=lambda s: (round(min(s["y1"], s["y2"]) / 10), min(s["x1"], s["x2"])))
    segs = segs[:160]
    inv = 1 / f
    walls = []
    for i, s in enumerate(segs, 1):
        walls.append({"id": f"W{i}", "x1": round(s["x1"] * inv, 1), "y1": round(s["y1"] * inv, 1),
                      "x2": round(s["x2"] * inv, 1), "y2": round(s["y2"] * inv, 1),
                      "thickness_px": round(s["t"] * inv, 1), "line": s["kind"], "confidence": s["conf"]})
    opens = []
    for o in ho:
        opens.append({"x": (o["a"] + o["b"]) / 2 * inv, "y": o["c"] * inv, "width_px": (o["b"] - o["a"]) * inv,
                      "kind_guess": o["kind"]})
    for o in vo:
        opens.append({"x": o["c"] * inv, "y": (o["a"] + o["b"]) / 2 * inv, "width_px": (o["b"] - o["a"]) * inv,
                      "kind_guess": o["kind"]})
    for i, o in enumerate(opens, 1):
        o["id"] = f"O{i}"
        best = None
        for wl in walls:
            d = _seg_dist(o["x"], o["y"], wl)
            if best is None or d < best[0]:
                best = (d, wl["id"])
        o["wall"] = best[1] if best and best[0] < 20 * inv else None
        o.update({k: round(v, 1) for k, v in o.items() if isinstance(v, float)})
    res = {"w": W0, "h": H0, "mode": mode, "thin_px": round(thin * inv, 2), "quality": q, "walls": walls,
           "openings": opens}
    if annotated:
        res["annotated"] = _annotate(im, walls, opens, annotated)
    return res


def _seg_dist(px: float, py: float, wl: dict) -> float:
    ax, ay, bx, by = wl["x1"], wl["y1"], wl["x2"], wl["y2"]
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _annotate(im, walls: list[dict], opens: list[dict], out: str, max_side: int = 1568) -> str:
    """The drawing, faded, with every wall candidate drawn in colour and numbered — the vision step answers about
    these numbers instead of guessing pixel positions."""
    import cv2
    import numpy as np
    H, W = im.shape[:2]
    f = min(1.0, max_side / max(H, W))
    base = cv2.resize(im, (round(W * f), round(H * f)), interpolation=cv2.INTER_AREA) if f < 1 else im.copy()
    base = cv2.addWeighted(base, 0.45, np.full_like(base, 255), 0.55, 0)
    colors = [(40, 40, 220), (30, 150, 30), (200, 80, 20), (160, 30, 160), (20, 140, 200), (90, 90, 90)]
    fs = max(0.4, min(0.8, max(base.shape[:2]) / 2000))
    for i, wl in enumerate(walls):
        c = colors[i % len(colors)]
        p1 = (int(wl["x1"] * f), int(wl["y1"] * f))
        p2 = (int(wl["x2"] * f), int(wl["y2"] * f))
        cv2.line(base, p1, p2, c, max(2, int(wl["thickness_px"] * f * 0.6)), cv2.LINE_AA)
        mx, my = (p1[0] + p2[0]) // 2, (p1[1] + p2[1]) // 2
        label = wl["id"]
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, fs, 1)
        cv2.rectangle(base, (mx - 2, my - th - 4), (mx + tw + 2, my + 2), (255, 255, 255), -1)
        cv2.putText(base, label, (mx, my), cv2.FONT_HERSHEY_SIMPLEX, fs, c, 1, cv2.LINE_AA)
    for o in opens:
        p = (int(o["x"] * f), int(o["y"] * f))
        cv2.circle(base, p, max(4, int(o["width_px"] * f / 2)), (255, 120, 0), 2)
        cv2.putText(base, o["id"], (p[0] + 4, p[1] - 4), cv2.FONT_HERSHEY_SIMPLEX, fs * 0.9, (255, 120, 0), 1, cv2.LINE_AA)
    cv2.imwrite(out, base)
    return out


# ----------------------------------------------------------------------------- local OCR (tesseract)

def op_ocr(path: str, walls: list | None = None, langs: str = "rus+eng") -> dict:
    """Words on the drawing (room names, sizes) without any cloud: tesseract on the drawing with the found walls
    painted out (they break its layout analysis), read straight and turned by 90° (vertical size lines)."""
    import shutil
    import subprocess
    import tempfile
    import cv2
    import numpy as np
    if not shutil.which("tesseract"):
        return {"tokens": [], "available": False}
    im = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    H, W = im.shape[:2]
    for wl in walls or []:
        cv2.line(im, (int(wl["x1"]), int(wl["y1"])), (int(wl["x2"]), int(wl["y2"])), (255, 255, 255),
                 int(wl.get("thickness_px", 4)) + 8)
    tokens = []
    with tempfile.TemporaryDirectory(prefix="jarvis-ocr-") as tmp:
        for rot in (0, 90):
            src = im if rot == 0 else cv2.rotate(im, cv2.ROTATE_90_CLOCKWISE)
            f = os.path.join(tmp, f"r{rot}.png")
            cv2.imwrite(f, src)
            r = subprocess.run(["tesseract", f, "-", "-l", langs, "--psm", "11", "tsv"], capture_output=True,
                               text=True, timeout=60)
            for line in r.stdout.splitlines()[1:]:
                p = line.split("\t")
                if len(p) != 12 or not p[11].strip():
                    continue
                conf = float(p[10])
                txt = p[11].strip()
                if conf < 45 or len(txt) < 2:
                    continue
                x, y, w, h = (int(v) for v in p[6:10])
                cx, cy = x + w / 2, y + h / 2
                if rot == 90:                       # back to the original picture
                    cx, cy = cy, H - 1 - cx
                    w, h = h, w
                tokens.append({"text": txt[:60], "x": round(cx, 1), "y": round(cy, 1), "w": w, "h": h,
                               "conf": round(conf, 1), "rot": rot})
    return {"tokens": tokens, "available": True}


# ----------------------------------------------------------------------------- DXF in

UNITS = {0: None, 1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0, 14: 100.0}


def op_dxf(path: str) -> dict:
    import ezdxf
    from ezdxf import recover
    try:
        doc = ezdxf.readfile(path)
    except ezdxf.DXFStructureError:
        doc, _ = recover.readfile(path)
    units = int(doc.header.get("$INSUNITS", 0) or 0)
    k = UNITS.get(units)
    msp = doc.modelspace()
    lines, arcs, texts, blocks = [], [], [], []

    def seg(a, b, layer):
        lines.append({"x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1], "layer": layer})

    def walk(entities, layer_override=None, depth=0):
        for e in entities:
            t = e.dxftype()
            layer = layer_override or e.dxf.get("layer", "0")
            if t == "LINE":
                seg(e.dxf.start, e.dxf.end, layer)
            elif t in ("LWPOLYLINE", "POLYLINE"):
                pts = [tuple(p[:2]) for p in (e.get_points("xy") if t == "LWPOLYLINE" else
                                              [v.dxf.location for v in e.vertices])]
                closed = e.closed if t == "LWPOLYLINE" else e.is_closed
                for a, b in zip(pts, pts[1:] + (pts[:1] if closed else [])):
                    seg(a, b, layer)
            elif t == "ARC":
                arcs.append({"x": e.dxf.center[0], "y": e.dxf.center[1], "r": e.dxf.radius,
                             "a0": e.dxf.start_angle, "a1": e.dxf.end_angle, "layer": layer})
            elif t in ("TEXT", "MTEXT"):
                txt = e.plain_text() if t == "MTEXT" else e.dxf.text
                p = e.dxf.insert
                texts.append({"text": str(txt)[:120], "x": p[0], "y": p[1], "layer": layer})
            elif t == "INSERT" and depth < 3:
                name = e.dxf.name
                p = e.dxf.insert
                blocks.append({"name": name, "x": p[0], "y": p[1], "rotation": e.dxf.get("rotation", 0.0),
                               "layer": layer})
                try:
                    walk(e.virtual_entities(), layer, depth + 1)
                except Exception:          # noqa: BLE001 — a broken block: skip its contents
                    pass

    walk(msp)
    if k is None:
        xs = [v for ln in lines for v in (ln["x1"], ln["x2"])]
        span = (max(xs) - min(xs)) if xs else 0
        k = 1000.0 if 0 < span < 200 else (10.0 if 200 <= span < 3000 else 1.0)    # metres / cm / mm by extent
        guessed = True
    else:
        guessed = False
    for ln in lines:
        for key in ("x1", "y1", "x2", "y2"):
            ln[key] = round(ln[key] * k, 2)
    for a in arcs:
        a["x"], a["y"], a["r"] = round(a["x"] * k, 2), round(a["y"] * k, 2), round(a["r"] * k, 2)
    for t in texts + blocks:
        t["x"], t["y"] = round(t["x"] * k, 2), round(t["y"] * k, 2)
    return {"units": units, "mm_per_unit": k, "units_guessed": guessed, "lines": lines[:20000], "arcs": arcs[:2000],
            "texts": texts[:2000], "blocks": blocks[:2000], "layers": sorted({ln["layer"] for ln in lines})}


def op_dxf_walls(path: str) -> dict:
    """A DXF plan → walls in millimetres (Y up): lines on wall layers (or all lines), parallel pairs = one wall with
    its thickness, ends joined like on images; door swings (arcs) and texts come along."""
    d = op_dxf(path)
    wall_layers = [ly for ly in d["layers"] if any(k in ly.casefold() for k in ("wall", "стен", "a-wall", "walls"))]
    lines = [ln for ln in d["lines"] if not wall_layers or ln["layer"] in wall_layers]
    hr, vr, diag = [], [], []
    for ln in lines:
        x1, y1, x2, y2 = ln["x1"], ln["y1"], ln["x2"], ln["y2"]
        if abs(y1 - y2) < 1 and abs(x2 - x1) > 200:
            hr.append({"a": min(x1, x2), "b": max(x1, x2), "c": (y1 + y2) / 2, "t": 0.0, "n": 1})
        elif abs(x1 - x2) < 1 and abs(y2 - y1) > 200:
            vr.append({"a": min(y1, y2), "b": max(y1, y2), "c": (x1 + x2) / 2, "t": 0.0, "n": 1})
        elif math.hypot(x2 - x1, y2 - y1) > 500:
            diag.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2, "t": 0.0, "kind": "diagonal", "conf": 0.5})
    hw, ho = _pair_and_merge(hr, 1.0, 700.0, 30.0, (500.0, 3500.0))
    vw, vo = _pair_and_merge(vr, 1.0, 700.0, 30.0, (500.0, 3500.0))
    segs = [{"x1": s["a"], "y1": s["c"], "x2": s["b"], "y2": s["c"], "t": s["t"] if s["kind"] == "double" else 0.0,
             "kind": s["kind"], "conf": 0.97 if s["kind"] == "double" else 0.7} for s in hw] + \
           [{"x1": s["c"], "y1": s["a"], "x2": s["c"], "y2": s["b"], "t": s["t"] if s["kind"] == "double" else 0.0,
             "kind": s["kind"], "conf": 0.97 if s["kind"] == "double" else 0.7} for s in vw]
    for s in segs:
        s["tt"] = s["t"] or 150.0
    tmp = [{**s, "t": s["tt"]} for s in segs]
    _connect(tmp, tol_k=0.6)
    walls = [{"id": f"W{i}", "x1": round(s["x1"], 1), "y1": round(s["y1"], 1), "x2": round(s["x2"], 1),
              "y2": round(s["y2"], 1), "thickness": round(s["t"], 1) if s["kind"] == "double" else None,
              "line": s["kind"], "confidence": s["conf"]} for i, s in enumerate(tmp, 1)]
    opens = [{"x": (o["a"] + o["b"]) / 2, "y": o["c"], "width": o["b"] - o["a"]} for o in ho] + \
            [{"x": o["c"], "y": (o["a"] + o["b"]) / 2, "width": o["b"] - o["a"]} for o in vo]
    return {"walls": walls, "openings": opens, "arcs": d["arcs"], "texts": d["texts"], "blocks": d["blocks"],
            "mm_per_unit": d["mm_per_unit"], "units": d["units"], "units_guessed": d["units_guessed"],
            "layers": d["layers"], "wall_layers": wall_layers}


# ----------------------------------------------------------------------------- DXF out

def op_write_dxf(model: dict, out: str, cad: dict | None = None) -> dict:
    import ezdxf
    from ezdxf.enums import TextEntityAlignment
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4
    doc.header["$MEASUREMENT"] = 1
    for name, color in (("WALLS", 7), ("DOORS", 3), ("WINDOWS", 5), ("ROOMS", 2), ("FURNITURE", 8), ("DIMENSIONS", 1),
                        ("COLUMNS", 6), ("STAIRS", 4)):
        doc.layers.add(name, color=color)
    msp = doc.modelspace()
    lines = (cad or {}).get("lines", {})

    def unit(dx, dy):
        n = math.hypot(dx, dy) or 1.0
        return dx / n, dy / n

    walls = {w["id"]: w for w in model.get("walls", [])}
    for w in walls.values():
        a, b = lines.get(w["id"], (w["start"], w["end"]))
        ux, uy = unit(b[0] - a[0], b[1] - a[1])
        nx, ny = -uy, ux
        t = float(w["thickness"]) / 2
        pts = [(a[0] + nx * t, a[1] + ny * t), (b[0] + nx * t, b[1] + ny * t), (b[0] - nx * t, b[1] - ny * t),
               (a[0] - nx * t, a[1] - ny * t)]
        msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "WALLS"})
        if w.get("kind") == "exterior":
            p1 = (w["start"][0] + nx * (t + 600), w["start"][1] + ny * (t + 600))
            p2 = (w["end"][0] + nx * (t + 600), w["end"][1] + ny * (t + 600))
            dim = msp.add_aligned_dim(p1=p1, p2=p2, distance=200, dxfattribs={"layer": "DIMENSIONS"})
            dim.render()
    for o in model.get("doors", []) + model.get("windows", []):
        w = walls.get(o.get("host"))
        if not w:
            continue
        sx, sy = w["start"]
        ux, uy = unit(w["end"][0] - sx, w["end"][1] - sy)
        nx, ny = -uy, ux
        c = (sx + ux * o["offset"], sy + uy * o["offset"])
        hw, t = o["width"] / 2, float(w["thickness"]) / 2
        a = (c[0] - ux * hw, c[1] - uy * hw)
        b = (c[0] + ux * hw, c[1] + uy * hw)
        if o["type"] == "window":
            for k in (-t, 0.0, t):
                msp.add_line((a[0] + nx * k, a[1] + ny * k), (b[0] + nx * k, b[1] + ny * k), dxfattribs={"layer": "WINDOWS"})
            continue
        for q in (a, b):          # jambs across the wall
            msp.add_line((q[0] - nx * t, q[1] - ny * t), (q[0] + nx * t, q[1] + ny * t), dxfattribs={"layer": "DOORS"})
        if o.get("kind") == "opening":
            continue
        hinge, other = (a, b) if o.get("swing", "left") != "right" else (b, a)
        h0 = (hinge[0] + nx * t, hinge[1] + ny * t)
        r = o["width"]
        leaf = (h0[0] + nx * r, h0[1] + ny * r)
        closed = (other[0] + nx * t, other[1] + ny * t)
        msp.add_line(h0, leaf, dxfattribs={"layer": "DOORS"})
        a_closed = math.degrees(math.atan2(closed[1] - h0[1], closed[0] - h0[0]))
        a_open = math.degrees(math.atan2(leaf[1] - h0[1], leaf[0] - h0[0]))
        if (a_open - a_closed) % 360 <= 180:
            msp.add_arc(h0, r, a_closed, a_open, dxfattribs={"layer": "DOORS"})
        else:
            msp.add_arc(h0, r, a_open, a_closed, dxfattribs={"layer": "DOORS"})
    for r in model.get("rooms", []):
        x, y = r.get("label") or r["polygon"][0]
        msp.add_text(r["name"], height=250, dxfattribs={"layer": "ROOMS"}).set_placement(
            (x, y + 150), align=TextEntityAlignment.MIDDLE_CENTER)
        msp.add_text(f"{r['area_m2']:.1f} м²", height=180, dxfattribs={"layer": "ROOMS"}).set_placement(
            (x, y - 200), align=TextEntityAlignment.MIDDLE_CENTER)
    for f in model.get("furniture", []):
        cx, cy = f["position"]
        wdt, dep = f["size"][0], f["size"][1]
        rot = math.radians(f.get("rotation", 0))
        c, s = math.cos(rot), math.sin(rot)
        pts = [(cx + dx * c - dy * s, cy + dx * s + dy * c) for dx, dy in
               ((-wdt / 2, -dep / 2), (wdt / 2, -dep / 2), (wdt / 2, dep / 2), (-wdt / 2, dep / 2))]
        msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "FURNITURE"})
    for col in model.get("columns", []):
        cx, cy = col["center"]
        msp.add_lwpolyline([(cx - col["width"] / 2, cy - col["depth"] / 2), (cx + col["width"] / 2, cy - col["depth"] / 2),
                            (cx + col["width"] / 2, cy + col["depth"] / 2), (cx - col["width"] / 2, cy + col["depth"] / 2)],
                           close=True, dxfattribs={"layer": "COLUMNS"})
    doc.saveas(out)
    return op_check_dxf(out)


def op_check_dxf(path: str) -> dict:
    import ezdxf
    doc = ezdxf.readfile(path)
    counts: dict[str, int] = {}
    for e in doc.modelspace():
        counts[e.dxf.layer] = counts.get(e.dxf.layer, 0) + 1
    return {"path": path, "layers": counts, "units": doc.header.get("$INSUNITS"), "size": os.path.getsize(path)}


def op_ping() -> dict:
    import cv2
    import ezdxf
    import numpy
    import pypdfium2
    return {"python": sys.version.split()[0], "opencv": cv2.__version__, "numpy": numpy.__version__,
            "ezdxf": ezdxf.__version__, "pdfium": pypdfium2.V_PYPDFIUM2}


OPS = {"ping": op_ping, "fit": op_fit, "raster": op_raster, "walls": op_walls, "ocr": op_ocr, "dxf": op_dxf, "dxf_walls": op_dxf_walls,
       "write_dxf": op_write_dxf,
       "check_dxf": op_check_dxf}


def main() -> None:
    reply({"ready": True, "pid": os.getpid()})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid = req.get("id")
        try:
            fn = OPS[req["op"]]
            res = fn(**(req.get("args") or {}))
            reply({"id": rid, "ok": True, "result": res})
        except Exception as e:   # noqa: BLE001 — the reply carries the error; the worker stays up
            reply({"id": rid, "ok": False, "error": f"{e.__class__.__name__}: {e}"[:600],
                   "trace": traceback.format_exc()[-1500:]})


if __name__ == "__main__":
    main()
