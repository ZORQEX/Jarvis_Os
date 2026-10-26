"""Plan geometry in pure Python (millimetres, X right, Y up): points, segments, polygons, wall joins and the rooms a
set of walls encloses (faces of the planar graph of wall centre lines). No numpy: the core venv has none, and every
function here is small enough to unit-test with exact numbers."""
from __future__ import annotations

import math
from collections import defaultdict

Pt = tuple[float, float]
EPS = 1e-6


def sub(a: Pt, b: Pt) -> Pt:
    return (a[0] - b[0], a[1] - b[1])


def add(a: Pt, b: Pt) -> Pt:
    return (a[0] + b[0], a[1] + b[1])


def mul(a: Pt, k: float) -> Pt:
    return (a[0] * k, a[1] * k)


def dot(a: Pt, b: Pt) -> float:
    return a[0] * b[0] + a[1] * b[1]


def cross(a: Pt, b: Pt) -> float:
    return a[0] * b[1] - a[1] * b[0]


def length(a: Pt) -> float:
    return math.hypot(a[0], a[1])


def dist(a: Pt, b: Pt) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def unit(a: Pt) -> Pt:
    n = length(a)
    return (a[0] / n, a[1] / n) if n > EPS else (0.0, 0.0)


def perp(a: Pt) -> Pt:
    """Left normal (counter-clockwise by 90°)."""
    return (-a[1], a[0])


def lerp(a: Pt, b: Pt, t: float) -> Pt:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def angle_deg(v: Pt) -> float:
    return math.degrees(math.atan2(v[1], v[0]))


def rnd(p: Pt, nd: int = 1) -> list[float]:
    return [round(p[0], nd), round(p[1], nd)]


def seg_point(p: Pt, a: Pt, b: Pt) -> tuple[float, float]:
    """(distance from p to segment ab, parameter t of the closest point, 0..1)."""
    ab = sub(b, a)
    l2 = dot(ab, ab)
    if l2 < EPS:
        return dist(p, a), 0.0
    t = max(0.0, min(1.0, dot(sub(p, a), ab) / l2))
    return dist(p, lerp(a, b, t)), t


def line_point_t(p: Pt, a: Pt, b: Pt) -> float:
    """Unclamped parameter of p projected on the line ab."""
    ab = sub(b, a)
    l2 = dot(ab, ab)
    return dot(sub(p, a), ab) / l2 if l2 > EPS else 0.0


def seg_intersect(a: Pt, b: Pt, c: Pt, d: Pt) -> tuple[Pt, float, float] | None:
    """Intersection of segments ab and cd (touching counts): (point, t on ab, u on cd); None if parallel or apart."""
    r, s = sub(b, a), sub(d, c)
    den = cross(r, s)
    if abs(den) < EPS:
        return None
    t = cross(sub(c, a), s) / den
    u = cross(sub(c, a), r) / den
    if -EPS <= t <= 1 + EPS and -EPS <= u <= 1 + EPS:
        return lerp(a, b, t), t, u
    return None


def line_intersect(a: Pt, da: Pt, b: Pt, db: Pt) -> Pt | None:
    """Intersection of infinite lines a + s·da and b + t·db."""
    den = cross(da, db)
    if abs(den) < EPS:
        return None
    s = cross(sub(b, a), db) / den
    return add(a, mul(da, s))


def collinear(a: Pt, b: Pt, c: Pt, d: Pt, ang_tol: float = 3.0, off_tol: float = 30.0) -> bool:
    """Segments on the same line (direction within ang_tol degrees, c and d within off_tol of line ab)."""
    u, v = unit(sub(b, a)), unit(sub(d, c))
    if abs(cross(u, v)) > math.sin(math.radians(ang_tol)):
        return False
    n = perp(u)
    return abs(dot(sub(c, a), n)) <= off_tol and abs(dot(sub(d, a), n)) <= off_tol


# ---------------------------------------------------------------- polygons

def area(poly: list[Pt]) -> float:
    """Signed area (shoelace): positive for counter-clockwise."""
    s = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def centroid(poly: list[Pt]) -> Pt:
    a = area(poly)
    if abs(a) < EPS:
        xs, ys = [p[0] for p in poly], [p[1] for p in poly]
        return (sum(xs) / len(xs), sum(ys) / len(ys))
    cx = cy = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        f = x1 * y2 - x2 * y1
        cx += (x1 + x2) * f
        cy += (y1 + y2) * f
    return (cx / (6 * a), cy / (6 * a))


def inside(p: Pt, poly: list[Pt]) -> bool:
    """Point in polygon (ray casting; points on the boundary may go either way)."""
    x, y = p
    res = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            xc = xi + (y - yi) * (xj - xi) / (yj - yi)
            if x < xc:
                res = not res
        j = i
    return res


def interior_point(poly: list[Pt]) -> Pt:
    """A point well inside the polygon (the centroid when it is inside; else the middle of the widest horizontal
    span through it) — where a room's label goes, and how an old room is found in a re-computed one."""
    c = centroid(poly)
    if inside(c, poly):
        return c
    ys = sorted({p[1] for p in poly})
    best, best_w = c, -1.0
    for k in range(len(ys) - 1):
        y = (ys[k] + ys[k + 1]) / 2
        xs = []
        n = len(poly)
        for i in range(n):
            (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
            if (y1 > y) != (y2 > y):
                xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
        xs.sort()
        for i in range(0, len(xs) - 1, 2):
            if xs[i + 1] - xs[i] > best_w:
                best_w, best = xs[i + 1] - xs[i], ((xs[i] + xs[i + 1]) / 2, y)
    return best


def bbox(points: list[Pt]) -> tuple[float, float, float, float]:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def simplify(poly: list[Pt], tol: float = 1.0) -> list[Pt]:
    """Drop repeated and collinear vertices."""
    pts = []
    for p in poly:
        if not pts or dist(p, pts[-1]) > tol:
            pts.append(p)
    if len(pts) > 1 and dist(pts[0], pts[-1]) <= tol:
        pts.pop()
    changed = True
    while changed and len(pts) > 3:
        changed = False
        for i in range(len(pts)):
            a, b, c = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
            d, _ = seg_point(b, a, c)
            if d <= tol:
                pts.pop(i)
                changed = True
                break
    return pts


def offset_polygon(poly: list[Pt], d: float) -> list[Pt]:
    """Offset a simple polygon by d (outward for d > 0 whatever the orientation), mitred corners."""
    pts = simplify(poly)
    if len(pts) < 3:
        return pts
    if area(pts) < 0:
        pts = pts[::-1]
    out = []
    n = len(pts)
    for i in range(n):
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % n]
        u1, u2 = unit(sub(b, a)), unit(sub(c, b))
        n1, n2 = (u1[1], -u1[0]), (u2[1], -u2[0])          # right normals = outward for a CCW polygon
        p = line_intersect(add(a, mul(n1, d)), u1, add(b, mul(n2, d)), u2)
        out.append(p if p is not None else add(b, mul(n1, d)))
    return out


def rect(center: Pt, w: float, h: float, rot_deg: float = 0.0) -> list[Pt]:
    c, s = math.cos(math.radians(rot_deg)), math.sin(math.radians(rot_deg))
    out = []
    for dx, dy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
        out.append((center[0] + dx * c - dy * s, center[1] + dx * s + dy * c))
    return out


def wall_outline(a: Pt, b: Pt, t: float) -> list[Pt]:
    """The rectangle a wall of thickness t covers in plan."""
    n = mul(perp(unit(sub(b, a))), t / 2)
    return [add(a, n), add(b, n), sub(b, n), sub(a, n)]


# ---------------------------------------------------------------- rooms: faces of the wall graph

class _Nodes:
    """Points snapped together within tol (a grid hash, so lookups stay fast)."""

    def __init__(self, tol: float):
        self.tol, self.pts, self.grid = tol, [], defaultdict(list)

    def key(self, p: Pt) -> int:
        gx, gy = int(math.floor(p[0] / self.tol)), int(math.floor(p[1] / self.tol))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in self.grid[(gx + dx, gy + dy)]:
                    if dist(self.pts[i], p) <= self.tol:
                        return i
        self.pts.append(p)
        self.grid[(gx, gy)].append(len(self.pts) - 1)
        return len(self.pts) - 1


def planar_faces(segments: list[tuple[Pt, Pt, str]], tol: float = 40.0, min_area: float = 0.5e6) -> list[dict]:
    """Closed areas bounded by the segments (wall centre lines): [{"polygon": [...] CCW, "area": mm², "edges": {tag:
    length of its boundary}}]. Segments are split where they cross or where one ends on another (T-joins); loose ends
    are dropped; the unbounded outer face is not returned."""
    segs = [(tuple(a), tuple(b), tag) for a, b, tag in segments if dist(a, b) > tol]
    cuts: list[list[float]] = [[0.0, 1.0] for _ in segs]
    for i, (a, b, _) in enumerate(segs):
        for j, (c, d, _) in enumerate(segs):
            if i == j:
                continue
            hit = seg_intersect(a, b, c, d)
            if hit:
                cuts[i].append(hit[1])
            for p in (c, d):                   # an end of another wall lying on this one (T-join, within tol)
                dd, t = seg_point(p, a, b)
                if dd <= tol and 0 < t < 1:
                    cuts[i].append(t)
            if hit is None:                    # an end of this wall short of / past the other wall: snap onto it
                for k, p in ((0.0, a), (1.0, b)):
                    dd, t = seg_point(p, c, d)
                    if dd <= tol and 0 < t < 1:
                        cuts[j].append(t)
    nodes = _Nodes(tol)
    edges: dict[tuple[int, int], set] = {}
    for (a, b, tag), ts in zip(segs, cuts):
        ts = sorted(set(round(t, 6) for t in ts if -EPS <= t <= 1 + EPS))
        for t0, t1 in zip(ts, ts[1:]):
            if (t1 - t0) * dist(a, b) <= tol / 2:
                continue
            u, v = nodes.key(lerp(a, b, t0)), nodes.key(lerp(a, b, t1))
            if u == v:
                continue
            edges.setdefault((min(u, v), max(u, v)), set()).add(tag)
    adj: dict[int, set] = defaultdict(set)
    for u, v in edges:
        adj[u].add(v)
        adj[v].add(u)
    # drop loose ends (walls that do not close anything)
    stack = [n for n in adj if len(adj[n]) < 2]
    while stack:
        n = stack.pop()
        for m in list(adj[n]):
            adj[m].discard(n)
            edges.pop((min(n, m), max(n, m)), None)
            if len(adj[m]) < 2:
                stack.append(m)
        adj[n].clear()
    P = nodes.pts
    order = {n: sorted(adj[n], key=lambda m: math.atan2(P[m][1] - P[n][1], P[m][0] - P[n][0])) for n in adj if adj[n]}
    used = set()
    faces = []
    for u in order:
        for v in order[u]:
            if (u, v) in used:
                continue
            loop, a, b = [], u, v
            for _ in range(len(edges) * 2 + 4):
                used.add((a, b))
                loop.append(a)
                nb = order[b]
                i = nb.index(a)
                c = nb[(i - 1) % len(nb)]        # the next edge clockwise from the one we came by: face on the left
                a, b = b, c
                if (a, b) == (u, v):
                    break
            else:
                continue
            poly = [P[k] for k in loop]
            ar = area(poly)
            if ar <= min_area:
                continue
            tags: dict[str, float] = defaultdict(float)
            for k in range(len(loop)):
                e = (min(loop[k], loop[(k + 1) % len(loop)]), max(loop[k], loop[(k + 1) % len(loop)]))
                for tag in edges.get(e, ()):
                    tags[tag] += dist(P[e[0]], P[e[1]])
            faces.append({"polygon": simplify(poly, 0.5), "area": ar, "edges": dict(tags)})
    return faces


def outer_boundary(segments: list[tuple[Pt, Pt, str]], tol: float = 40.0) -> list[Pt] | None:
    """The outline of all closed faces together (the building's footprint along wall centre lines)."""
    faces = planar_faces(segments, tol, min_area=0.1e6)
    if not faces:
        return None
    # The union's boundary = edges used by exactly one face.
    count: dict[tuple, int] = defaultdict(int)
    for f in faces:
        poly = f["polygon"]
        for i in range(len(poly)):
            a, b = poly[i], poly[(i + 1) % len(poly)]
            ka, kb = (round(a[0]), round(a[1])), (round(b[0]), round(b[1]))
            count[(min(ka, kb), max(ka, kb))] += 1
    # Faces were simplified independently, so split collinear edges first: rebuild with the face edges as segments.
    segs = []
    for f in faces:
        poly = f["polygon"]
        for i in range(len(poly)):
            segs.append((poly[i], poly[(i + 1) % len(poly)], "f"))
    nodes = _Nodes(tol)
    cuts: dict[tuple[int, int], int] = defaultdict(int)
    pieces = []
    for i, (a, b, _) in enumerate(segs):
        ts = [0.0, 1.0]
        for j, (c, d, _) in enumerate(segs):
            if i != j:
                for p in (c, d):
                    dd, t = seg_point(p, a, b)
                    if dd <= tol / 4 and 0 < t < 1:
                        ts.append(t)
        ts = sorted(set(ts))
        for t0, t1 in zip(ts, ts[1:]):
            u, v = nodes.key(lerp(a, b, t0)), nodes.key(lerp(a, b, t1))
            if u != v:
                cuts[(min(u, v), max(u, v))] += 1
                pieces.append((u, v))
    boundary = [(u, v) for (u, v), c in cuts.items() if c == 1]
    if not boundary:
        return None
    nxt: dict[int, list[int]] = defaultdict(list)
    for u, v in boundary:
        nxt[u].append(v)
        nxt[v].append(u)
    # walk the longest closed loop
    best: list[int] = []
    seen_e = set()
    for start in list(nxt):
        if len(nxt[start]) != 2:
            continue
        loop, prev, cur = [start], None, start
        for _ in range(len(boundary) + 2):
            cand = [m for m in nxt[cur] if m != prev and (min(cur, m), max(cur, m)) not in seen_e]
            if not cand:
                break
            m = cand[0]
            seen_e.add((min(cur, m), max(cur, m)))
            prev, cur = cur, m
            if cur == start:
                break
            loop.append(cur)
        if cur == start and len(loop) > len(best):
            best = loop
    if len(best) < 3:
        return None
    poly = simplify([nodes.pts[k] for k in best], 0.5)
    return poly if area(poly) > 0 else poly[::-1]


# ---------------------------------------------------------------- wall joins (clean corners in CAD)

def _priority(w: dict) -> tuple:
    return (0 if w.get("kind") == "exterior" else 1, -float(w.get("thickness", 0)), w["id"])


def wall_joins(walls: list[dict], tol: float | None = None) -> dict[str, dict]:
    """How each wall end meets the others: {"WALL_001": {"start": {"kind": "L"|"T"|"inline"|"free", "with": [...],
    "shift": mm}, "end": {...}}}. shift > 0 extends the wall past its end, < 0 trims it back, so that in CAD the solids
    meet without gaps or overlaps: at an L-corner the higher-priority wall (exterior, then thicker) runs through and the
    other stops at its face; at a T-join the wall ending on another stops at its face."""
    out: dict[str, dict] = {w["id"]: {"start": {"kind": "free", "with": [], "shift": 0.0},
                                      "end": {"kind": "free", "with": [], "shift": 0.0}} for w in walls}
    ends = []
    for w in walls:
        a, b = tuple(w["start"]), tuple(w["end"])
        if dist(a, b) < EPS:
            continue
        ends.append((w, "start", a, unit(sub(b, a))))     # direction pointing away from the end, into the wall
        ends.append((w, "end", b, unit(sub(a, b))))
    for w, which, p, d_in in ends:
        t_w = float(w["thickness"])
        tl = tol if tol is not None else max(40.0, t_w * 0.75)
        # other walls ending at the same point
        mates = [(v, wh2, d2) for v, wh2, p2, d2 in ends if v["id"] != w["id"] and dist(p, p2) <= tl]
        if mates:
            # a wall continuing straight through this point (opposite directions): this end is a joint of a run
            straight = [m for m in mates if dot(m[2], d_in) < -0.985]
            others = [m for m in mates if m not in straight]
            rec = out[w["id"]][which]
            rec["with"] = [m[0]["id"] for m in mates]
            if straight and not others:
                rec["kind"] = "inline"
                continue
            if straight:
                # this wall and its straight continuation run through; perpendicular ones stop at their face
                rec["kind"] = "inline"
                continue
            group = [w] + [m[0] for m in others]
            through = [m for m in mates if any(dot(m[2], o[2]) < -0.985 for o in mates if o is not m)]
            if through:
                # two others form a straight run through this point: this wall stops at their face (a T made of ends)
                host = max((m[0] for m in through), key=lambda v: float(v["thickness"]))
                sin_t = abs(cross(d_in, [m for m in through if m[0] is host][0][2])) or 1.0
                rec["kind"] = "T"
                rec["shift"] = -float(host["thickness"]) / 2 / max(sin_t, 0.5)
                continue
            primary = min(group, key=_priority)
            rec["kind"] = "L"
            if primary["id"] == w["id"]:
                worst = max(others, key=lambda m: float(m[0]["thickness"]))
                sin_t = abs(cross(d_in, worst[2]))
                if sin_t < 0.5:          # sharper than 30°: leave as is (the overlap is reported by the OCCT check)
                    continue
                rec["shift"] = float(worst[0]["thickness"]) / 2 / sin_t
            else:
                other = [m for m in mates if m[0]["id"] == primary["id"]][0]
                sin_t = abs(cross(d_in, other[2]))
                if sin_t < 0.5:
                    continue
                rec["shift"] = -float(primary["thickness"]) / 2 / sin_t
            continue
        # this end lying on another wall's middle: a T-join
        for v in walls:
            if v["id"] == w["id"]:
                continue
            a, b = tuple(v["start"]), tuple(v["end"])
            dd, t = seg_point(p, a, b)
            L = dist(a, b)
            if dd <= max(tl, float(v["thickness"]) / 2 + 20) and L > EPS and tl / L < t < 1 - tl / L:
                sin_t = abs(cross(d_in, unit(sub(b, a))))
                if sin_t < 0.5:
                    continue
                rec = out[w["id"]][which]
                rec["kind"], rec["with"] = "T", [v["id"]]
                # Stop at the near face of the host wall, wherever the end was drawn (centre line, face, or past it).
                # Along the host's normal n: the CAD end lies at side - shift·inward; the near face at s·t/2.
                n = perp(unit(sub(b, a)))
                side = dot(sub(p, a), n)
                inward = dot(d_in, n)
                s = 1.0 if inward > 0 else -1.0
                rec["shift"] = (side - s * float(v["thickness"]) / 2) / inward
                break
    return out


def cad_line(w: dict, joins: dict[str, dict]) -> tuple[Pt, Pt, float]:
    """The wall's line as built in CAD (ends moved by the joins) and how far the CAD start lies before the model
    start (to place openings, which are measured from the model start)."""
    a, b = tuple(w["start"]), tuple(w["end"])
    d = unit(sub(b, a))
    j = joins.get(w["id"]) or {}
    s0 = float((j.get("start") or {}).get("shift", 0.0))
    s1 = float((j.get("end") or {}).get("shift", 0.0))
    return sub(a, mul(d, s0)), add(b, mul(d, s1)), s0
