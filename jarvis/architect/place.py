"""Where a piece of furniture goes when the person names only the room («добавь диван в гостиную») or another piece
(«поставь телевизор напротив дивана»): against the longest free stretch of wall (not across a door, tall things not
in front of a window), in a free corner, in the middle, or facing the other piece — never overlapping what stands
there already.

Furniture frame: width along local X, depth along local Y, the back at local +Y; rotation θ turns local into plan
coordinates, so the front faces R(θ)·(0, −1)."""
from __future__ import annotations

import math

from . import geometry as g
from .catalog import FURNITURE

GAP = 20.0            # mm between a piece and the wall face
DOOR_CLEAR = 350.0    # mm kept free on each side of a door


def front(rot: float) -> g.Pt:
    t = math.radians(rot)
    return (math.sin(t), -math.cos(t))


def facing(n: g.Pt) -> float:
    """Rotation that makes the front face direction n."""
    return (math.degrees(math.atan2(n[1], n[0])) + 90.0) % 360


def footprint(pos: g.Pt, size, rot: float) -> list[g.Pt]:
    return g.rect(pos, float(size[0]), float(size[1]), rot)


def _overlap(p: list[g.Pt], q: list[g.Pt]) -> bool:
    """Separating-axis test for two convex quads."""
    for poly in (p, q):
        for i in range(len(poly)):
            a, b = poly[i], poly[(i + 1) % len(poly)]
            ax = g.perp(g.unit(g.sub(b, a)))
            pa = [g.dot(x, ax) for x in p]
            qa = [g.dot(x, ax) for x in q]
            if max(pa) <= min(qa) + 1 or max(qa) <= min(pa) + 1:
                return False
    return True


def _edges(model, room: dict) -> list[dict]:
    """The room's boundary edges with the wall each lies on, its inner face and inward normal."""
    poly = [(float(p[0]), float(p[1])) for p in room["polygon"]]
    if g.area(poly) < 0:
        poly = poly[::-1]
    out = []
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        L = g.dist(a, b)
        if L < 300:
            continue
        u = g.unit(g.sub(b, a))
        n = g.perp(u)                                 # inward for a counter-clockwise room outline
        mid = g.lerp(a, b, 0.5)
        wall, t = None, 120.0
        for w in model.data["walls"]:
            d, _ = g.seg_point(mid, (float(w["start"][0]), float(w["start"][1])), (float(w["end"][0]), float(w["end"][1])))
            if d < 30:
                wall, t = w, float(w["thickness"])
                break
        fa, fb = g.add(a, g.mul(n, t / 2)), g.add(b, g.mul(n, t / 2))
        out.append({"a": fa, "b": fb, "u": u, "n": n, "len": L, "wall": wall})
    return out


def _blocked(model, e: dict, tall: bool, low_h: float) -> list[tuple[float, float]]:
    """Stretches of an edge (mm from its start) where nothing may stand: doors (+ clearance), windows for tall things."""
    out = []
    if not e["wall"]:
        return out
    for o in model.hosted(e["wall"]["id"]):
        c = model.opening_center(o)
        _, t = g.seg_point(c, e["a"], e["b"])
        s = t * g.dist(e["a"], e["b"])
        half = float(o["width"]) / 2
        if o["type"] == "door":
            out.append((s - half - DOOR_CLEAR, s + half + DOOR_CLEAR + float(o["width"]) * 0.2))
        elif tall or low_h > float(o.get("sill", 900)) - 50:
            out.append((s - half - 100, s + half + 100))
    return out


def _free(length: float, blocked: list[tuple[float, float]]) -> list[tuple[float, float]]:
    spans, cur = [], 0.0
    for s, e in sorted(blocked):
        if s > cur:
            spans.append((cur, min(s, length)))
        cur = max(cur, e)
    if cur < length:
        spans.append((cur, length))
    return [(s, e) for s, e in spans if e - s > 1]


def pick_room(model, kind: str) -> dict | None:
    prefs = FURNITURE[kind].get("rooms", ())
    rooms = model.data["rooms"]
    for p in prefs:
        hit = [r for r in rooms if r.get("kind") == p]
        if hit:
            return max(hit, key=lambda r: r["area_m2"])
    return max(rooms, key=lambda r: r["area_m2"]) if rooms else None


def place(model, kind: str, room_id: str | None, opposite: str | None = None, size=None,
          ignore: str | None = None) -> tuple[g.Pt, float, str]:
    from .model import ModelError
    f = FURNITURE[kind]
    size = [float(x) for x in (size or f["size"])]
    room = model.get(room_id) if room_id else None
    target = model.get(opposite) if opposite else None
    if target is not None and room is None:
        room = model.get(target.get("room")) if target.get("room") else None
    if room is None:
        room = pick_room(model, kind)
    if room is None:
        raise ModelError("В модели нет помещений — некуда поставить мебель")
    poly = [(float(p[0]), float(p[1])) for p in room["polygon"]]
    others = [footprint((float(x["position"][0]), float(x["position"][1])), x["size"], float(x["rotation"]))
              for x in model.data["furniture"] if x["id"] != ignore and x.get("room") == room["id"]]

    def fits(pos, rot) -> bool:
        fp = footprint(pos, size, rot)
        return all(g.inside(p, poly) for p in fp) and not any(_overlap(fp, o) for o in others)

    edges = _edges(model, room)
    tall = size[2] > 1200
    if target is not None:
        tpos = (float(target["position"][0]), float(target["position"][1]))
        d = front(float(target["rotation"]))
        best = None
        for e in edges:
            hit = g.seg_intersect(tpos, g.add(tpos, g.mul(d, 1e6)), e["a"], e["b"])
            if hit and (best is None or g.dist(tpos, hit[0]) < g.dist(tpos, best[1])):
                best = (e, hit[0])
        if best:
            e, p = best
            s = g.dist(e["a"], p)
            spans = _free(g.dist(e["a"], e["b"]), _blocked(model, e, tall, size[2]))
            for a, b in sorted(spans, key=lambda sp: min(abs(s - sp[0]), abs(s - sp[1])) if not sp[0] <= s <= sp[1] else 0):
                if b - a < size[0]:
                    continue
                c = min(max(s, a + size[0] / 2), b - size[0] / 2)
                pos = g.add(g.lerp(e["a"], e["b"], c / g.dist(e["a"], e["b"])), g.mul(e["n"], size[1] / 2 + GAP))
                rot = facing(e["n"])
                if fits(pos, rot):
                    return pos, rot, f"напротив: {target['name'].lower()} {target['id']}"
    mode = f.get("place", "wall")
    if mode == "corner":
        for e in sorted(edges, key=lambda e: -e["len"]):
            for s in (size[0] / 2 + 10, g.dist(e["a"], e["b"]) - size[0] / 2 - 10):
                pos = g.add(g.lerp(e["a"], e["b"], s / g.dist(e["a"], e["b"])), g.mul(e["n"], size[1] / 2 + GAP))
                rot = facing(e["n"])
                if fits(pos, rot) and not any(a <= s <= b for a, b in _blocked(model, e, tall, size[2])):
                    return pos, rot, "в углу"
    if mode in ("wall", "wall_head", "corner"):
        cands = []
        for e in edges:
            L = g.dist(e["a"], e["b"])
            for a, b in _free(L, _blocked(model, e, tall, size[2])):
                if b - a >= size[0] + 40:
                    cands.append((b - a, e, a, b))
        for _, e, a, b in sorted(cands, key=lambda c: -c[0]):
            L = g.dist(e["a"], e["b"])
            for c in ((a + b) / 2, a + size[0] / 2 + 20, b - size[0] / 2 - 20):
                pos = g.add(g.lerp(e["a"], e["b"], c / L), g.mul(e["n"], size[1] / 2 + GAP))
                rot = facing(e["n"])
                if fits(pos, rot):
                    where = f"у стены {e['wall']['id']}" if e["wall"] else "у стены"
                    return pos, rot, where
    if mode == "near":
        tables = [x for x in model.data["furniture"] if x["kind"] in ("table", "desk") and x.get("room") == room["id"]]
        for t in tables:
            tp = (float(t["position"][0]), float(t["position"][1]))
            for k in range(8):
                ang = float(t["rotation"]) + k * 90 + (45 if k >= 4 else 0)
                d = front(ang)
                pos = g.add(tp, g.mul(d, float(t["size"][1]) / 2 + size[1] / 2 + 80))
                rot = facing(g.mul(d, -1))
                if fits(pos, rot):
                    return pos, rot, f"у стола {t['id']}"
    c = (float(room["anchor"][0]), float(room["anchor"][1])) if room.get("anchor") else g.interior_point(poly)
    for k in range(25):
        ring = (k // 8 + 1) * 400 if k else 0
        ang = (k % 8) * 45
        pos = (c[0] + ring * math.cos(math.radians(ang)), c[1] + ring * math.sin(math.radians(ang)))
        for rot in (0.0, 90.0):
            if fits(pos, rot):
                return pos, rot, "в центре" if mode == "center" else "свободной стены не нашлось — поставлен посередине"
    raise ModelError(f"В «{room['name']}» не хватает места для: {f['name'].lower()} "
                     f"({size[0] / 10:g}×{size[1] / 10:g} см)")
