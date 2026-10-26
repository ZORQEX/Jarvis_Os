"""The Master Architectural Model: the one source of truth for a building. FreeCAD (CAD truth of the geometry),
Blender (the look) and the window's viewer are all built FROM it and never keep a version of their own.

Units: millimetres, X right, Y up (north up as drawn), Z up. Every element has a stable id (WALL_001, ROOM_001,
DOOR_001, WINDOW_001, STAIR_001, COLUMN_001, SLAB_001, ROOF_001, FURN_001), a level, a confidence (0..1) and where it
came from (source: cv | vision | dxf | user | derived). Rooms are not drawn: they are the closed areas the walls
make (geometry.planar_faces) and keep their ids and names across edits (matched by an interior point).

Edits are operations (`apply`): a dict {"op": ..., ...} → what changed. The CAD and Blender stages then rebuild only
the elements whose derived specs changed (service.Architect, spec hashes), not the whole house."""
from __future__ import annotations

import copy
import re
import time

from . import geometry as g
from .catalog import DEFAULT_MATERIALS, FURNITURE, MATERIALS, room_kind, room_title

SCHEMA = "jarvis.architect/1"
PREFIX = {"level": "LEVEL", "wall": "WALL", "room": "ROOM", "door": "DOOR", "window": "WINDOW", "stair": "STAIR",
          "column": "COLUMN", "slab": "SLAB", "roof": "ROOF", "furniture": "FURN"}
LISTS = {"level": "levels", "wall": "walls", "room": "rooms", "door": "doors", "window": "windows", "stair": "stairs",
         "column": "columns", "slab": "slabs", "furniture": "furniture"}
DEFAULTS = {"wall_height": 2700.0, "exterior_thickness": 300.0, "interior_thickness": 120.0, "slab_thickness": 200.0,
            "door_width": 900.0, "door_height": 2100.0, "window_width": 1200.0, "window_height": 1400.0,
            "window_sill": 900.0, "roof_thickness": 250.0, "roof_overhang": 300.0}
ID_RE = re.compile(r"^(LEVEL|WALL|ROOM|DOOR|WINDOW|STAIR|COLUMN|SLAB|ROOF|FURN)_\d{3,}$")
SNAP = 40.0               # mm: wall ends closer than this are one point


class ModelError(ValueError):
    """An edit that cannot be done as asked (the text says why, in Russian)."""


def new_model(name: str = "Проект") -> dict:
    return {"schema": SCHEMA, "name": name, "units": "mm", "revision": 0, "updated_at": time.time(),
            "defaults": dict(DEFAULTS), "materials": dict(DEFAULT_MATERIALS),
            "source": {}, "levels": [{"id": "LEVEL_000", "type": "level", "name": "Первый этаж", "elevation": 0.0,
                                      "height": DEFAULTS["wall_height"], "index": 0}],
            "walls": [], "rooms": [], "doors": [], "windows": [], "stairs": [], "columns": [], "slabs": [],
            "roof": None, "furniture": [], "questions": [], "warnings": [], "counters": {}}


def pt(p) -> g.Pt:
    return (float(p[0]), float(p[1]))


class ArchModel:
    def __init__(self, data: dict | None = None):
        self.data = data if data is not None else new_model()
        d = self.data
        for k, v in new_model().items():
            d.setdefault(k, copy.deepcopy(v))
        for k, v in DEFAULTS.items():
            d["defaults"].setdefault(k, v)

    # ------------------------------------------------------------ access
    def all(self) -> list[dict]:
        d = self.data
        out = []
        for key in ("levels", "walls", "rooms", "doors", "windows", "stairs", "columns", "slabs", "furniture"):
            out += d[key]
        if d.get("roof"):
            out.append(d["roof"])
        return out

    def get(self, oid: str) -> dict | None:
        oid = str(oid or "").upper()
        for e in self.all():
            if e["id"] == oid:
                return e
        return None

    def need(self, oid: str, kind: str | tuple | None = None) -> dict:
        e = self.get(oid)
        if e is None:
            raise ModelError(f"Нет элемента {oid}")
        kinds = (kind,) if isinstance(kind, str) else kind
        if kinds and e["type"] not in kinds:
            raise ModelError(f"{oid} — это не {'/'.join(kinds)}")
        return e

    def level(self, lid: str | None = None) -> dict:
        lv = self.data["levels"]
        if lid:
            for x in lv:
                if x["id"] == lid:
                    return x
        return lv[0]

    def next_id(self, kind: str) -> str:
        c = self.data["counters"]
        pre = PREFIX[kind]
        used = {e["id"] for e in self.all()}
        n = int(c.get(pre, 0))
        for e in used:
            if e.startswith(pre + "_"):
                try:
                    n = max(n, int(e.split("_")[1]))
                except ValueError:
                    pass
        n += 1
        c[pre] = n
        return f"{pre}_{n:03d}"

    def openings(self) -> list[dict]:
        return self.data["doors"] + self.data["windows"]

    def hosted(self, wid: str) -> list[dict]:
        return [o for o in self.openings() if o.get("host") == wid]

    # ------------------------------------------------------------ derived data
    def wall_len(self, w: dict) -> float:
        return g.dist(pt(w["start"]), pt(w["end"]))

    def derive(self) -> None:
        """Re-compute what follows from the walls: rooms (ids kept), which rooms each wall and opening touches,
        the floor slab and the roof outline. Cheap (pure Python), run after every edit."""
        self.snap_walls()
        self.recompute_rooms()
        d = self.data
        rooms = d["rooms"]
        for w in d["walls"]:
            w["rooms"] = []
        for r in rooms:
            for wid in r.get("walls", []):
                w = self.get(wid)
                if w and r["id"] not in w["rooms"]:
                    w["rooms"].append(r["id"])
        for w in d["walls"]:
            if w.get("kind") in (None, "", "auto"):
                w["kind"] = "exterior" if len(w["rooms"]) <= 1 else "interior"
        for o in self.openings():
            o["rooms"] = self.sides(o)
        self.derive_slabs()
        for f in d["furniture"]:
            f["room"] = self.room_at(pt(f["position"]), f.get("level")) or f.get("room")
        d["updated_at"] = time.time()

    def snap_walls(self) -> None:
        """Ends closer than SNAP become one point (the first one seen)."""
        pts: list[g.Pt] = []
        for w in self.data["walls"]:
            for k in ("start", "end"):
                p = pt(w[k])
                for q in pts:
                    if g.dist(p, q) <= SNAP:
                        p = q
                        break
                else:
                    pts.append(p)
                w[k] = [round(p[0], 1), round(p[1], 1)]

    def segments(self, level: str | None = None) -> list[tuple[g.Pt, g.Pt, str]]:
        return [(pt(w["start"]), pt(w["end"]), w["id"]) for w in self.data["walls"]
                if level is None or w.get("level") == level]

    def recompute_rooms(self) -> None:
        d = self.data
        old = d["rooms"]
        new_rooms = []
        for lv in d["levels"]:
            faces = g.planar_faces(self.segments(lv["id"]), tol=SNAP)
            olds = [r for r in old if r.get("level") == lv["id"]]
            claimed: dict[str, list[dict]] = {}
            for f in faces:
                claimed[id(f)] = [r for r in olds if g.inside(pt(r.get("anchor") or r["label"]), f["polygon"])]
            taken: set[str] = set()
            for f in sorted(faces, key=lambda f: -f["area"]):
                prev = [r for r in claimed[id(f)] if r["id"] not in taken]
                if prev:
                    keep = max(prev, key=lambda r: r.get("area_m2", 0))
                    room = dict(keep)
                    if len(prev) > 1 and not keep.get("user_named"):
                        names = [r["name"] for r in sorted(prev, key=lambda r: -r.get("area_m2", 0))]
                        kinds = {r.get("kind") for r in prev}
                        room["name"] = "Кухня-гостиная" if kinds == {"kitchen", "living"} else "-".join(names)
                        room["kind"] = "kitchen_living" if kinds == {"kitchen", "living"} else keep.get("kind", "room")
                        room["merged_from"] = [r["id"] for r in prev]
                    for r in prev:
                        taken.add(r["id"])
                else:
                    room = {"id": self.next_id("room"), "type": "room", "name": "", "kind": "room", "level": lv["id"],
                            "confidence": 0.6, "source": "derived"}
                poly = [g.rnd(p) for p in f["polygon"]]
                room["polygon"] = poly
                room["level"] = lv["id"]
                room["walls"] = sorted(t for t, ln in f["edges"].items() if ln > SNAP)
                room["area_m2"] = round(f["area"] / 1e6, 2)
                ip = g.interior_point([pt(p) for p in poly])
                room["label"] = g.rnd(ip)
                room["anchor"] = g.rnd(ip)
                room.setdefault("ceiling_height", None)
                new_rooms.append(room)
        # names: unnamed rooms get «Помещение N», repeated names a number («Спальня 2»)
        seen: dict[str, int] = {}
        for r in sorted(new_rooms, key=lambda r: r["id"]):
            if not r.get("name"):
                r["name"] = room_title(r.get("kind", "room")) if r.get("kind") not in (None, "room", "other") else ""
            base = r["name"] or "Помещение"
            seen[base] = seen.get(base, 0) + 1
            if not r["name"]:
                r["name"] = f"Помещение {seen[base]}"
            elif seen[base] > 1 and not r.get("user_named"):
                r["name"] = f"{base} {seen[base]}"
        d["rooms"] = sorted(new_rooms, key=lambda r: r["id"])

    def assign_labels(self, labels: list[dict]) -> list[str]:
        """Names read on the drawing → the rooms they sit in ({"name", "kind"?, "point": [x, y] mm, "confidence"}).
        Two labels in one room (an open kitchen-living room) → both names. Returns labels that fell in no room."""
        lost = []
        got: dict[str, list[dict]] = {}
        for lb in labels:
            rid = self.room_at(pt(lb["point"]), lb.get("level"))
            if rid:
                got.setdefault(rid, []).append(lb)
            else:
                lost.append(str(lb.get("name")))
        for r in self.data["rooms"]:
            lbs = got.get(r["id"])
            if not lbs:
                continue
            lbs.sort(key=lambda x: -float(x.get("confidence", 0.5)))
            for x in lbs:            # «СПАЛЬНЯ» on a sketch → «Спальня»
                n = str(x["name"]).strip()
                if len(n) > 3 and n.isupper():
                    x["name"] = n[:1] + n[1:].lower()
            kinds = [x.get("kind") or room_kind(x["name"]) for x in lbs]
            if len(lbs) == 1:
                r["name"], r["kind"] = str(lbs[0]["name"])[:60], kinds[0]
            elif set(kinds) == {"kitchen", "living"}:
                r["name"], r["kind"] = "Кухня-гостиная", "kitchen_living"
            else:
                r["name"], r["kind"] = "-".join(str(x["name"]) for x in lbs)[:60], kinds[0]
            r["confidence"] = round(min(float(x.get("confidence", 0.5)) for x in lbs), 2)
            r["source"] = lbs[0].get("source", "vision")
            if lbs[0].get("area_m2"):
                r["area_on_drawing_m2"] = lbs[0]["area_m2"]
            r["anchor"] = g.rnd(pt(lbs[0]["point"])) if g.inside(pt(lbs[0]["point"]), [pt(p) for p in r["polygon"]]) \
                else r["anchor"]
        self.recompute_rooms()          # numbering of repeated names
        return lost

    def room_at(self, p: g.Pt, level: str | None = None) -> str | None:
        for r in self.data["rooms"]:
            if (level is None or r.get("level") == level) and g.inside(p, [pt(q) for q in r["polygon"]]):
                return r["id"]
        return None

    def opening_center(self, o: dict) -> g.Pt:
        w = self.need(o["host"], "wall")
        a, b = pt(w["start"]), pt(w["end"])
        return g.add(a, g.mul(g.unit(g.sub(b, a)), float(o["offset"])))

    def sides(self, o: dict) -> list[str]:
        """Rooms on both sides of an opening ("outside" for the exterior)."""
        w = self.get(o.get("host"))
        if not w:
            return []
        c = self.opening_center(o)
        n = g.perp(g.unit(g.sub(pt(w["end"]), pt(w["start"]))))
        off = float(w["thickness"]) / 2 + 60
        out = []
        for s in (1, -1):
            out.append(self.room_at(g.add(c, g.mul(n, off * s)), w.get("level")) or "outside")
        return out

    def derive_slabs(self) -> None:
        """Floor slab per level under the whole footprint (outer wall faces), and a flat roof over the top level.
        Their ids stay; their outline follows the walls."""
        d = self.data
        slabs = {s["level"]: s for s in d["slabs"] if s.get("kind") == "floor"}
        keep = [s for s in d["slabs"] if s.get("kind") != "floor"]
        top_poly = None
        for lv in d["levels"]:
            segs = self.segments(lv["id"])
            outline = g.outer_boundary(segs, tol=SNAP) if segs else None
            if not outline:
                continue
            ext = [w for w in d["walls"] if w.get("level") == lv["id"] and w.get("kind") == "exterior"]
            t = max((float(w["thickness"]) for w in ext), default=d["defaults"]["exterior_thickness"])
            poly = [g.rnd(p) for p in g.offset_polygon(outline, t / 2)]
            s = slabs.get(lv["id"]) or {"id": self.next_id("slab"), "type": "slab", "kind": "floor", "level": lv["id"],
                                        "thickness": d["defaults"]["slab_thickness"], "material": d["materials"]["slab"],
                                        "source": "derived", "confidence": 1.0}
            s["polygon"] = poly
            s["elevation"] = float(lv["elevation"]) - float(s["thickness"])
            keep.append(s)
            top_poly = (poly, lv)
        d["slabs"] = sorted(keep, key=lambda s: s["id"])
        roof = d.get("roof")
        if top_poly and (roof is None or roof.get("kind") != "none"):
            poly, lv = top_poly
            if roof is None:
                roof = {"id": self.next_id("roof"), "type": "roof", "kind": "flat", "thickness": d["defaults"]["roof_thickness"],
                        "overhang": d["defaults"]["roof_overhang"], "material": d["materials"]["roof"],
                        "source": "derived", "confidence": 1.0}
            roof["level"] = lv["id"]
            roof["polygon"] = [g.rnd(p) for p in g.offset_polygon([pt(p) for p in poly], float(roof.get("overhang", 0)))]
            top = max((float(w["base"]) + float(w["height"]) for w in d["walls"] if w.get("level") == lv["id"]),
                      default=float(lv["elevation"]) + float(lv["height"]))
            roof["elevation"] = top
            d["roof"] = roof
        elif not top_poly and roof and roof.get("kind") != "none":
            d["roof"] = None

    # ------------------------------------------------------------ building blocks
    def add_wall(self, start, end, thickness=None, height=None, kind=None, level=None, confidence=1.0,
                 source="user", material=None) -> dict:
        lv = self.level(level)
        dft = self.data["defaults"]
        if g.dist(pt(start), pt(end)) < 100:
            raise ModelError("Стена короче 10 см — не добавлена")
        w = {"id": self.next_id("wall"), "type": "wall", "kind": kind or "auto", "level": lv["id"],
             "start": g.rnd(pt(start)), "end": g.rnd(pt(end)),
             "thickness": float(thickness or (dft["exterior_thickness"] if kind == "exterior" else dft["interior_thickness"])),
             "height": float(height or lv["height"]), "base": float(lv["elevation"]),
             "material": material or self.data["materials"]["wall"], "confidence": round(float(confidence), 2),
             "source": source, "rooms": []}
        self.data["walls"].append(w)
        return w

    def add_opening(self, kind: str, host: str, offset: float, width=None, height=None, sill=None, swing=None,
                    confidence=1.0, source="user") -> dict:
        w = self.need(host, "wall")
        dft = self.data["defaults"]
        door = kind in ("door", "opening")
        o = {"id": self.next_id("door" if door else "window"), "type": "door" if door else "window",
             "kind": kind, "host": w["id"], "offset": round(float(offset), 1),
             "width": float(width or (dft["door_width"] if door else dft["window_width"])),
             "height": float(height or (dft["door_height"] if door else dft["window_height"])),
             "sill": 0.0 if door else float(dft["window_sill"] if sill is None else sill),
             "level": w["level"], "confidence": round(float(confidence), 2), "source": source,
             "material": self.data["materials"]["door" if door else "window"]}
        if door:
            o["swing"] = swing or "left"
        self.fit_opening(o)
        (self.data["doors"] if door else self.data["windows"]).append(o)
        return o

    def fit_opening(self, o: dict, strict: bool = False) -> None:
        """Keep an opening inside its wall (100 mm from each end); too wide for the wall → error."""
        w = self.need(o["host"], "wall")
        L = self.wall_len(w)
        m = 100.0
        if o["width"] > L - 2 * m:
            raise ModelError(f"Проём {o['id']} шириной {o['width']:.0f} мм не помещается в стену {w['id']} "
                             f"длиной {L:.0f} мм")
        lo, hi = m + o["width"] / 2, L - m - o["width"] / 2
        if not lo <= o["offset"] <= hi:
            if strict:
                raise ModelError(f"{o['id']} вышел бы за край стены {w['id']}: можно сдвинуть ещё на "
                                 f"{max(0, (hi - o['offset']) if o['offset'] > hi else (o['offset'] - lo)):.0f} мм")
            o["offset"] = round(min(hi, max(lo, o["offset"])), 1)
        top = float(w["height"])
        if o["sill"] + o["height"] > top - 20:
            if strict:
                raise ModelError(f"{o['id']} выше стены {w['id']} ({top:.0f} мм)")
            o["height"] = max(300.0, top - 20 - o["sill"])

    def add_furniture(self, kind: str, position, rotation: float = 0.0, size=None, room=None, level=None,
                      material=None, name=None, confidence=1.0, source="user") -> dict:
        if kind not in FURNITURE:
            raise ModelError(f"Не знаю мебель «{kind}»")
        f = FURNITURE[kind]
        lv = self.level(level)
        item = {"id": self.next_id("furniture"), "type": "furniture", "kind": kind, "name": name or f["name"],
                "position": g.rnd(pt(position)), "rotation": round(float(rotation) % 360, 1),
                "size": [float(x) for x in (size or f["size"])], "level": lv["id"], "elevation": float(lv["elevation"]),
                "material": material or f["material"], "room": room, "confidence": round(float(confidence), 2),
                "source": source}
        self.data["furniture"].append(item)
        return item

    # ------------------------------------------------------------ operations
    def apply(self, op: dict) -> dict:
        """One edit. Returns {"text": what was done (Russian), "changed": ids, "added": ids, "removed": ids}."""
        name = str(op.get("op", ""))
        fn = getattr(self, "op_" + name, None)
        if fn is None:
            raise ModelError(f"Неизвестная операция «{name}»")
        before = {e["id"]: copy.deepcopy(e) for e in self.all()}
        snapshot = copy.deepcopy(self.data)
        try:
            text = fn(**{k: v for k, v in op.items() if k != "op"})
            self.derive()
        except Exception:
            self.data.clear()                 # all or nothing: a failed edit leaves the model as it was
            self.data.update(snapshot)
            raise
        after = {e["id"]: e for e in self.all()}
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(k for k in set(after) & set(before) if _strip(after[k]) != _strip(before[k]))
        self.data["revision"] = int(self.data.get("revision", 0)) + 1
        return {"text": text, "changed": changed, "added": added, "removed": removed}

    # walls
    def op_add_wall(self, start, end, thickness=None, height=None, kind=None, level=None) -> str:
        w = self.add_wall(start, end, thickness, height, kind, level)
        return f"Добавлена стена {w['id']} длиной {self.wall_len(w) / 1000:.2f} м"

    def op_delete_wall(self, id) -> str:
        w = self.need(id, "wall")
        gone = [o["id"] for o in self.hosted(w["id"])]
        self.data["walls"].remove(w)
        self.data["doors"] = [o for o in self.data["doors"] if o.get("host") != w["id"]]
        self.data["windows"] = [o for o in self.data["windows"] if o.get("host") != w["id"]]
        return f"Убрана стена {w['id']}" + (f" (вместе с {', '.join(gone)})" if gone else "")

    def split(self, w: dict, at: float) -> dict:
        """Cut a wall in two at `at` mm from its start; returns the new second piece (openings go with their piece)."""
        L = self.wall_len(w)
        at = float(at)
        if not 100 <= at <= L - 100:
            raise ModelError(f"Точка разделения вне стены {w['id']} (0…{L:.0f} мм)")
        a, b = pt(w["start"]), pt(w["end"])
        m = g.lerp(a, b, at / L)
        nw = copy.deepcopy(w)
        nw["id"] = self.next_id("wall")
        w["end"] = g.rnd(m)
        nw["start"] = g.rnd(m)
        self.data["walls"].append(nw)
        for o in self.hosted(w["id"]):
            if o["offset"] > at:
                o["host"], o["offset"] = nw["id"], round(o["offset"] - at, 1)
        return nw

    def op_split_wall(self, id, at) -> str:
        w = self.need(id, "wall")
        nw = self.split(w, at)
        return f"Стена {w['id']} разделена: {w['id']} и {nw['id']}"

    def shared(self, ra: dict, rb: dict) -> list[tuple[dict, float, float]]:
        """Stretches of walls between two rooms: [(wall, from mm, to mm)] along each wall from its start."""
        out = []
        pa, pb = [pt(p) for p in ra["polygon"]], [pt(p) for p in rb["polygon"]]
        for wid in set(ra.get("walls", [])) & set(rb.get("walls", [])):
            w = self.get(wid)
            if not w:
                continue
            a, b = pt(w["start"]), pt(w["end"])
            L = g.dist(a, b)
            n = g.perp(g.unit(g.sub(b, a)))
            off = float(w["thickness"]) / 2 + 60
            step = 50.0
            run, k = None, 0.0
            while k <= L + 1e-6:
                p = g.lerp(a, b, min(k, L) / L)
                s1, s2 = g.add(p, g.mul(n, off)), g.sub(p, g.mul(n, off))
                hit = (g.inside(s1, pa) and g.inside(s2, pb)) or (g.inside(s1, pb) and g.inside(s2, pa))
                if hit and run is None:
                    run = k
                if not hit and run is not None:
                    out.append((w, run, k - step))
                    run = None
                k += step
            if run is not None:
                out.append((w, run, L))
        res = []
        for w, s, e in out:
            nodes = self.nodes_on(w)
            s = min(nodes, key=lambda n: abs(n - s)) if min(abs(n - s) for n in nodes) <= 2.5 * step else s
            e = min(nodes, key=lambda n: abs(n - e)) if min(abs(n - e) for n in nodes) <= 2.5 * step else e
            if e - s > 150:
                res.append((w, max(0.0, s), min(self.wall_len(w), e)))
        return res

    def nodes_on(self, w: dict) -> list[float]:
        """Where things meet a wall, in mm from its start: its two ends and every other wall's end lying on it."""
        a, b = pt(w["start"]), pt(w["end"])
        L = g.dist(a, b)
        out = [0.0, L]
        for v in self.data["walls"]:
            if v["id"] == w["id"] or v.get("level") != w.get("level"):
                continue
            for k in ("start", "end"):
                d, t = g.seg_point(pt(v[k]), a, b)
                if d <= float(w["thickness"]) / 2 + SNAP and 0 < t < 1:
                    out.append(t * L)
        return sorted(out)

    def op_remove_wall_between(self, a, b) -> str:
        ra, rb = self.need(a, "room"), self.need(b, "room")
        parts = self.shared(ra, rb)
        if not parts:
            raise ModelError(f"У «{ra['name']}» и «{rb['name']}» нет общей стены")
        done, gone = [], []
        # far stretches first: a wall keeps its start, so the offsets of the nearer stretches stay valid
        for w, s, e in sorted(parts, key=lambda x: (x[0]["id"], -x[1])):
            L = self.wall_len(w)
            # a stretch reaching (almost) to a wall end takes the whole end (the corner joins stay clean)
            s = 0.0 if s < float(w["thickness"]) + 150 else s
            e = L if L - e < float(w["thickness"]) + 150 else e
            if e < L - 1:
                self.split(w, e)
            target = self.split(w, s) if s > 1 else w
            gone += [o["id"] for o in self.hosted(target["id"])]
            self.op_delete_wall(target["id"])
            done.append(target["id"])
        return (f"Убрана стена между «{ra['name']}» и «{rb['name']}» ({', '.join(done)}): теперь это одно помещение"
                + (f"; вместе со стеной убраны {', '.join(gone)}" if gone else ""))

    def connected_ends(self, w: dict) -> dict[str, list[tuple[dict, str]]]:
        out: dict[str, list] = {"start": [], "end": []}
        for k in ("start", "end"):
            p = pt(w[k])
            for v in self.data["walls"]:
                if v["id"] == w["id"]:
                    continue
                for k2 in ("start", "end"):
                    if g.dist(p, pt(v[k2])) <= SNAP:
                        out[k].append((v, k2))
        return out

    def op_move_wall(self, id, dx=0.0, dy=0.0, by=None) -> str:
        """Move a wall (dx, dy in mm, or `by` mm along its normal: + = to its left); walls joined to its ends and
        walls ending on it stretch with it, openings keep their places along the wall."""
        w = self.need(id, "wall")
        a, b = pt(w["start"]), pt(w["end"])
        if by is not None:
            n = g.perp(g.unit(g.sub(b, a)))
            dx, dy = n[0] * float(by), n[1] * float(by)
        dv = (float(dx), float(dy))
        if g.length(dv) < 1:
            raise ModelError("Сдвиг меньше миллиметра")
        ends = self.connected_ends(w)
        tees = []
        for v in self.data["walls"]:
            if v["id"] == w["id"]:
                continue
            for k2 in ("start", "end"):
                dd, t = g.seg_point(pt(v[k2]), a, b)
                if dd <= float(w["thickness"]) / 2 + 30 and 0.02 < t < 0.98:
                    tees.append((v, k2))
        w["start"], w["end"] = g.rnd(g.add(a, dv)), g.rnd(g.add(b, dv))
        for k, lst in ends.items():
            for v, k2 in lst:
                v[k2] = list(w[k])
        for v, k2 in tees:
            v[k2] = g.rnd(g.add(pt(v[k2]), dv))
        for v in [x for lst in ends.values() for x, _ in lst] + [x for x, _ in tees]:
            if self.wall_len(v) < 100:
                raise ModelError(f"После сдвига стена {v['id']} стала бы короче 10 см")
            for o in self.hosted(v["id"]):
                self.fit_opening(o)
        return f"Стена {w['id']} сдвинута на {g.length(dv) / 10:.0f} см"

    def op_set_wall(self, id, thickness=None, height=None, kind=None, material=None, base=None) -> str:
        w = self.need(id, "wall")
        parts = []
        if thickness is not None:
            t = float(thickness)
            if not 50 <= t <= 1500:
                raise ModelError("Толщина стены должна быть от 5 до 150 см")
            w["thickness"] = t
            parts.append(f"толщина {t / 10:g} см")
        if height is not None:
            h = float(height)
            if not 500 <= h <= 20000:
                raise ModelError("Высота стены должна быть от 0,5 до 20 м")
            w["height"] = h
            for o in self.hosted(w["id"]):
                self.fit_opening(o)
            parts.append(f"высота {h / 1000:g} м")
        if kind in ("exterior", "interior", "partition"):
            w["kind"] = kind
            parts.append({"exterior": "наружная", "interior": "внутренняя", "partition": "перегородка"}[kind])
        if material:
            if material not in MATERIALS:
                raise ModelError(f"Нет материала {material}")
            w["material"] = material
            parts.append(MATERIALS[material]["name"].lower())
        if base is not None:
            w["base"] = float(base)
        w["confidence"] = 1.0
        return f"Стена {w['id']}: " + (", ".join(parts) or "без изменений")

    def op_resize_wall(self, id, length, anchor="start") -> str:
        w = self.need(id, "wall")
        a, b = pt(w["start"]), pt(w["end"])
        L, new = g.dist(a, b), float(length)
        if new < 200:
            raise ModelError("Стена не может быть короче 20 см")
        u = g.unit(g.sub(b, a))
        if anchor == "end":
            w["start"] = g.rnd(g.sub(b, g.mul(u, new)))
            for o in self.hosted(w["id"]):
                o["offset"] = round(o["offset"] + (new - L), 1)
        elif anchor == "center":
            c = g.lerp(a, b, 0.5)
            w["start"], w["end"] = g.rnd(g.sub(c, g.mul(u, new / 2))), g.rnd(g.add(c, g.mul(u, new / 2)))
            for o in self.hosted(w["id"]):
                o["offset"] = round(o["offset"] + (new - L) / 2, 1)
        else:
            w["end"] = g.rnd(g.add(a, g.mul(u, new)))
        for o in self.hosted(w["id"]):
            self.fit_opening(o)
        return f"Длина стены {w['id']}: {new / 1000:.2f} м"

    def op_set_walls(self, filter=None, thickness=None, height=None, material=None) -> str:
        f = filter or {}
        walls = [w for w in self.data["walls"]
                 if (not f.get("kind") or w.get("kind") == f["kind"]) and (not f.get("level") or w["level"] == f["level"])
                 and (not f.get("room") or f["room"] in w.get("rooms", []))]
        if not walls:
            raise ModelError("Нет подходящих стен")
        for w in walls:
            self.op_set_wall(w["id"], thickness=thickness, height=height, material=material)
        return f"Изменено стен: {len(walls)}"

    # openings
    def op_add_opening(self, kind, host, offset=None, width=None, height=None, sill=None, swing=None) -> str:
        w = self.need(host, "wall")
        off = float(offset) if offset is not None else self.wall_len(w) / 2
        o = self.add_opening(kind, w["id"], off, width, height, sill, swing)
        return f"Добавлен{'а дверь' if o['type'] == 'door' else 'о окно'} {o['id']} в стене {w['id']}"

    def op_delete_opening(self, id) -> str:
        o = self.need(id, ("door", "window"))
        (self.data["doors"] if o["type"] == "door" else self.data["windows"]).remove(o)
        return f"Убран{'а дверь' if o['type'] == 'door' else 'о окно'} {o['id']}"

    def op_move_opening(self, id, by=None, to=None, host=None) -> str:
        o = self.need(id, ("door", "window"))
        old = float(o["offset"])
        if host:
            self.need(host, "wall")
            o["host"] = host
        if to is not None:
            o["offset"] = float(to)
        elif by is not None:
            o["offset"] = float(o["offset"]) + float(by)
        w = self.need(o["host"], "wall")
        lo, hi = 100 + o["width"] / 2, self.wall_len(w) - 100 - o["width"] / 2
        if not lo <= o["offset"] <= hi:
            room_left = (hi - old) if o["offset"] > old else (old - lo)
            raise ModelError(f"{o['id']} вышел бы за край стены {w['id']}: в эту сторону можно сдвинуть не больше "
                             f"чем на {max(0.0, room_left) / 10:.0f} см")
        self.fit_opening(o, strict=True)
        o["confidence"] = 1.0
        what = "Дверь" if o["type"] == "door" else "Окно"
        return f"{what} {o['id']} " + (f"сдвинут{'а' if o['type'] == 'door' else 'о'} на {abs(float(by)) / 10:g} см"
                                       if by is not None else "перемещен" + ("а" if o["type"] == "door" else "о"))

    def op_set_opening(self, id, width=None, height=None, sill=None, swing=None, kind=None) -> str:
        o = self.need(id, ("door", "window"))
        old = dict(o)
        if width is not None:
            o["width"] = float(width)
        if height is not None:
            o["height"] = float(height)
        if sill is not None and o["type"] == "window":
            o["sill"] = float(sill)
        if swing in ("left", "right", "double", "sliding") and o["type"] == "door":
            o["swing"] = swing
        if kind in ("door", "opening") and o["type"] == "door":
            o["kind"] = kind
        if not 300 <= o["width"] <= 6000 or not 300 <= o["height"] <= 6000:
            o.update(old)
            raise ModelError("Размер проёма должен быть от 30 см до 6 м")
        try:
            self.fit_opening(o, strict=True)
        except ModelError:
            o.update(old)
            raise
        o["confidence"] = 1.0
        return f"{o['id']}: ширина {o['width'] / 10:g} см, высота {o['height'] / 10:g} см"

    # rooms
    def op_rename_room(self, id, name) -> str:
        r = self.need(id, "room")
        old = r["name"]
        r["name"] = str(name).strip()[:60]
        r["kind"] = room_kind(r["name"]) if room_kind(r["name"]) != "other" else r.get("kind", "room")
        r["user_named"] = True
        return f"«{old}» теперь «{r['name']}»"

    def op_merge_rooms(self, a, b) -> str:
        return self.op_remove_wall_between(a, b)

    def neighbours(self, r: dict) -> list[tuple[dict, float]]:
        out = []
        for o in self.data["rooms"]:
            if o["id"] == r["id"] or o.get("level") != r.get("level"):
                continue
            ln = sum(e - s for _, s, e in self.shared(r, o))
            if ln > 0:
                out.append((o, ln))
        return sorted(out, key=lambda x: -x[1])

    def op_delete_room(self, id) -> str:
        r = self.need(id, "room")
        nb = self.neighbours(r)
        if not nb:
            raise ModelError(f"«{r['name']}» не граничит с другими помещениями — убирать нечего")
        other = nb[0][0]
        self.op_remove_wall_between(r["id"], other["id"])
        return f"«{r['name']}» убрано: объединено с «{other['name']}» (стена между ними снесена)"

    def op_split_room(self, id, axis="auto", at=0.5, name=None, thickness=None) -> str:
        r = self.need(id, "room")
        poly = [pt(p) for p in r["polygon"]]
        x0, y0, x1, y1 = g.bbox(poly)
        if axis == "auto":
            axis = "x" if (x1 - x0) >= (y1 - y0) else "y"    # cut across the longer side
        f = min(0.9, max(0.1, float(at)))
        big = 1e7
        if axis == "x":
            x = x0 + (x1 - x0) * f
            a, b = (x, y0 - big), (x, y1 + big)
        else:
            y = y0 + (y1 - y0) * f
            a, b = (x0 - big, y), (x1 + big, y)
        hits = []
        for i in range(len(poly)):
            h = g.seg_intersect(a, b, poly[i], poly[(i + 1) % len(poly)])
            if h:
                hits.append(h[0])
        hits.sort(key=lambda p: (p[0], p[1]))
        if len(hits) < 2:
            raise ModelError("Не получилось провести стену поперёк помещения")
        w = self.add_wall(hits[0], hits[-1], thickness=thickness or self.data["defaults"]["interior_thickness"],
                          kind="interior", level=r["level"])
        anchor_new = g.lerp(hits[0], hits[-1], 0.5)
        n = g.perp(g.unit(g.sub(hits[-1], hits[0])))
        side = g.add(anchor_new, g.mul(n, 300))
        r_old_anchor = pt(r["anchor"])
        if not g.inside(side, poly) or g.dist(side, r_old_anchor) < g.dist(g.sub(anchor_new, g.mul(n, 300)), r_old_anchor):
            side = g.sub(anchor_new, g.mul(n, 300))
        # the old room keeps the half with its label; the new half becomes a new named room on derive()
        self.derive()
        if name:
            nid = self.room_at(side, r["level"])
            if nid and nid != r["id"]:
                self.op_rename_room(nid, name)
        return f"«{r['name']}» разделено стеной {w['id']}"

    def op_add_room(self, name, width, depth, next_to=None, side="right", at=None, level=None) -> str:
        W, D = float(width), float(depth)
        if W < 800 or D < 800:
            raise ModelError("Комната меньше 80 см — так нельзя")
        lv = self.level(level)
        if at is not None:
            x0, y0 = pt(at)
        else:
            ref = self.need(next_to, "room") if next_to else None
            pts = [pt(p) for p in (ref["polygon"] if ref else [q for w in self.data["walls"] for q in (w["start"], w["end"])])]
            if not pts:
                x0, y0 = 0.0, 0.0
            else:
                bx0, by0, bx1, by1 = g.bbox(pts)
                x0, y0 = {"right": (bx1, by0), "left": (bx0 - W, by0), "top": (bx0, by1),
                          "bottom": (bx0, by0 - D)}.get(side, (bx1, by0))
        corners = [(x0, y0), (x0 + W, y0), (x0 + W, y0 + D), (x0, y0 + D)]
        made = []
        for i in range(4):
            a, b = corners[i], corners[(i + 1) % 4]
            covered = any(g.collinear(a, b, pt(w["start"]), pt(w["end"]), 2, 30)
                          and g.seg_point(g.lerp(a, b, 0.5), pt(w["start"]), pt(w["end"]))[0] < 30
                          for w in self.data["walls"] if w["level"] == lv["id"])
            if not covered:
                made.append(self.add_wall(a, b, kind="auto", level=lv["id"]))
        self.derive()
        rid = self.room_at(g.lerp(corners[0], corners[2], 0.5), lv["id"])
        if rid:
            self.op_rename_room(rid, name)
        return f"Добавлено помещение «{name}» {W / 1000:g}×{D / 1000:g} м ({len(made)} новых стен)"

    # levels
    def op_set_level(self, id=None, height=None, elevation=None, name=None) -> str:
        lv = self.level(id)
        out = []
        if height is not None:
            h = float(height)
            if not 1800 <= h <= 12000:
                raise ModelError("Высота этажа должна быть от 1,8 до 12 м")
            old = float(lv["height"])
            lv["height"] = h
            for w in self.data["walls"]:
                if w["level"] == lv["id"] and abs(float(w["height"]) - old) < 1:
                    w["height"] = h
                    for o in self.hosted(w["id"]):
                        self.fit_opening(o)
            for r in self.data["rooms"]:
                if r["level"] == lv["id"]:
                    r["ceiling_height"] = None
            out.append(f"высота потолка {h / 1000:g} м")
        if elevation is not None:
            lv["elevation"] = float(elevation)
        if name:
            lv["name"] = str(name)[:40]
            out.append(f"название «{lv['name']}»")
        return f"{lv['name']}: " + ", ".join(out or ["без изменений"])

    def op_add_level(self, name=None, height=None, copy_walls=True) -> str:
        top = max(self.data["levels"], key=lambda x: x["elevation"])
        idx = len(self.data["levels"])
        lid = self.next_id("level")
        lv = {"id": lid, "type": "level", "name": name or f"{idx + 1}-й этаж", "index": idx,
              "elevation": float(top["elevation"]) + float(top["height"]) + self.data["defaults"]["slab_thickness"],
              "height": float(height or top["height"])}
        self.data["levels"].append(lv)
        n = 0
        if copy_walls:
            for w in [w for w in self.data["walls"] if w["level"] == top["id"] and w.get("kind") == "exterior"]:
                self.add_wall(w["start"], w["end"], w["thickness"], lv["height"], "exterior", lid)
                n += 1
        return f"Добавлен {lv['name']}" + (f": наружные стены повторены ({n})" if n else "")

    # furniture
    def op_add_furniture(self, kind, room=None, position=None, rotation=None, size=None, material=None,
                         opposite=None, name=None) -> str:
        from .place import place
        if kind not in FURNITURE:
            raise ModelError(f"Не знаю мебель «{kind}»")
        if position is None:
            pos, rot, note = place(self, kind, room, opposite=opposite, size=size)
        else:
            pos, rot, note = pt(position), float(rotation or 0), ""
        r = self.get(room) if room else None
        f = self.add_furniture(kind, pos, rot if rotation is None else float(rotation), size, room,
                               r["level"] if r else None, material, name)
        where = f" в «{r['name']}»" if r else ""
        return f"Добавлено: {f['name'].lower()} {f['id']}{where}" + (f" ({note})" if note else "")

    def op_move_furniture(self, id, dx=0.0, dy=0.0, position=None, rotation=None, rotate_by=None, room=None) -> str:
        from .place import place
        f = self.need(id, "furniture")
        if room:
            pos, rot, _ = place(self, f["kind"], room, size=f["size"], ignore=f["id"])
            f["position"], f["rotation"] = g.rnd(pos), rot
        elif position is not None:
            f["position"] = g.rnd(pt(position))
        else:
            f["position"] = g.rnd(g.add(pt(f["position"]), (float(dx), float(dy))))
        if rotation is not None:
            f["rotation"] = round(float(rotation) % 360, 1)
        if rotate_by is not None:
            f["rotation"] = round((float(f["rotation"]) + float(rotate_by)) % 360, 1)
        return f"Перемещено: {f['name'].lower()} {f['id']}"

    def op_delete_furniture(self, id) -> str:
        f = self.need(id, "furniture")
        self.data["furniture"].remove(f)
        return f"Убрано: {f['name'].lower()} {f['id']}"

    def op_set_furniture(self, id, size=None, material=None, name=None) -> str:
        f = self.need(id, "furniture")
        if size:
            if len(size) != 3 or min(float(x) for x in size) < 50:
                raise ModelError("Размер мебели: ширина, глубина, высота в мм")
            f["size"] = [float(x) for x in size]
        if material:
            if material not in MATERIALS:
                raise ModelError(f"Нет материала {material}")
            f["material"] = material
        if name:
            f["name"] = str(name)[:60]
        return f"Изменено: {f['name'].lower()} {f['id']}"

    # materials
    def op_set_material(self, target, material, room=None) -> str:
        if material not in MATERIALS:
            raise ModelError(f"Нет материала {material}")
        m = MATERIALS[material]["name"]
        rooms = [self.need(room, "room")] if room else self.data["rooms"]
        if target in ("walls", "interior_walls", "exterior_walls"):
            n = 0
            for w in self.data["walls"]:
                if target == "interior_walls" and w.get("kind") == "exterior":
                    continue
                if target == "exterior_walls" and w.get("kind") != "exterior":
                    continue
                if room and room not in w.get("rooms", []):
                    continue
                w["material"] = material
                n += 1
            if not room and target == "walls":
                self.data["materials"]["wall"] = material
            return f"Стены ({n}): {m.lower()}"
        if target in ("floor", "ceiling"):
            for r in rooms:
                r[target + "_material"] = material
            if not room:
                self.data["materials"][target] = material
            where = f" в «{rooms[0]['name']}»" if room else ""
            return f"{'Пол' if target == 'floor' else 'Потолок'}{where}: {m.lower()}"
        e = self.need(target)
        e["material"] = material
        return f"{e['id']}: {m.lower()}"

    # structure
    def op_add_column(self, center, width=300.0, depth=300.0, height=None, shape="rect", level=None) -> str:
        lv = self.level(level)
        c = {"id": self.next_id("column"), "type": "column", "center": g.rnd(pt(center)), "width": float(width),
             "depth": float(depth), "height": float(height or lv["height"]), "shape": shape, "level": lv["id"],
             "base": float(lv["elevation"]), "material": self.data["materials"].get("column", "column_concrete"),
             "confidence": 1.0, "source": "user"}
        self.data["columns"].append(c)
        return f"Добавлена колонна {c['id']}"

    def op_delete_element(self, id) -> str:
        e = self.need(id)
        if e["type"] == "wall":
            return self.op_delete_wall(id)
        if e["type"] in ("door", "window"):
            return self.op_delete_opening(id)
        if e["type"] == "furniture":
            return self.op_delete_furniture(id)
        if e["type"] == "room":
            return self.op_delete_room(id)
        key = LISTS.get(e["type"])
        if key and e in self.data[key]:
            self.data[key].remove(e)
            return f"Убран {e['id']}"
        if e["type"] == "roof":
            self.data["roof"] = {**e, "kind": "none"}
            return "Крыша убрана"
        raise ModelError(f"{e['id']} нельзя удалить")

    def op_add_stair(self, start, direction=0.0, width=1000.0, length=3000.0, steps=None, level=None) -> str:
        lv = self.level(level)
        h = float(lv["height"]) + self.data["defaults"]["slab_thickness"]
        s = {"id": self.next_id("stair"), "type": "stair", "start": g.rnd(pt(start)), "direction": float(direction),
             "width": float(width), "length": float(length), "steps": int(steps or max(3, round(h / 175))),
             "height": h, "level": lv["id"], "base": float(lv["elevation"]), "material": self.data["materials"]["stair"],
             "confidence": 1.0, "source": "user"}
        self.data["stairs"].append(s)
        return f"Добавлена лестница {s['id']} ({s['steps']} ступеней)"

    def op_set_roof(self, kind="flat", thickness=None, overhang=None) -> str:
        r = self.data.get("roof") or {"id": self.next_id("roof"), "type": "roof", "material": self.data["materials"]["roof"],
                                      "source": "user", "confidence": 1.0,
                                      "thickness": self.data["defaults"]["roof_thickness"],
                                      "overhang": self.data["defaults"]["roof_overhang"]}
        r["kind"] = kind if kind in ("flat", "none") else "flat"
        if thickness is not None:
            r["thickness"] = float(thickness)
        if overhang is not None:
            r["overhang"] = float(overhang)
        self.data["roof"] = r
        return "Крыша убрана" if r["kind"] == "none" else "Плоская крыша"

    def op_rescale(self, factor) -> str:
        """The drawing's scale was wrong: every plan coordinate and opening offset × factor (thicknesses stay)."""
        k = float(factor)
        if not 0.05 <= k <= 20:
            raise ModelError("Масштаб изменился бы больше чем в 20 раз — проверьте размер")
        for w in self.data["walls"]:
            w["start"], w["end"] = g.rnd(g.mul(pt(w["start"]), k)), g.rnd(g.mul(pt(w["end"]), k))
        for o in self.openings():
            o["offset"] = round(float(o["offset"]) * k, 1)
        for f in self.data["furniture"]:
            f["position"] = g.rnd(g.mul(pt(f["position"]), k))
        for c in self.data["columns"]:
            c["center"] = g.rnd(g.mul(pt(c["center"]), k))
        for s in self.data["stairs"]:
            s["start"] = g.rnd(g.mul(pt(s["start"]), k))
            s["length"] = round(float(s["length"]) * k, 1)
        for r in self.data["rooms"]:
            for key in ("anchor", "label"):
                if r.get(key):
                    r[key] = g.rnd(g.mul(pt(r[key]), k))
        src = self.data.get("source") or {}
        if src.get("mm_per_px"):
            src["mm_per_px"] = round(float(src["mm_per_px"]) * k, 4)
        for o in self.openings():
            try:
                self.fit_opening(o)
            except ModelError:
                pass
        return f"Масштаб исправлен (×{k:.3f})"

    # ------------------------------------------------------------ summaries
    def summary(self) -> dict:
        d = self.data
        rooms = [{"id": r["id"], "name": r["name"], "area_m2": r["area_m2"], "net_area_m2": r.get("net_area_m2"),
                  "level": r["level"], "walls": r.get("walls", []), "confidence": r.get("confidence")}
                 for r in d["rooms"]]
        names = {r["id"]: r["name"] for r in d["rooms"]}
        walls = [{"id": w["id"], "kind": w.get("kind"), "length_mm": round(self.wall_len(w)),
                  "thickness": w["thickness"], "height": w["height"],
                  "between": [names.get(x, x) for x in w.get("rooms", [])] or ["снаружи"],
                  "openings": [o["id"] for o in self.hosted(w["id"])], "confidence": w.get("confidence")}
                 for w in d["walls"]]
        ops = [{"id": o["id"], "type": o["type"], "host": o["host"], "offset": o["offset"], "width": o["width"],
                "height": o["height"], "between": [names.get(x, x) for x in o.get("rooms", [])],
                "confidence": o.get("confidence")} for o in self.openings()]
        furn = [{"id": f["id"], "kind": f["kind"], "name": f["name"], "room": names.get(f.get("room"), None),
                 "position": f["position"], "rotation": f["rotation"]} for f in d["furniture"]]
        return {"name": d["name"], "revision": d.get("revision", 0), "levels": d["levels"], "rooms": rooms,
                "walls": walls, "openings": ops, "furniture": furn, "stairs": len(d["stairs"]),
                "columns": len(d["columns"]), "questions": d.get("questions", []), "warnings": d.get("warnings", []),
                "scale": {k: (d.get("source") or {}).get(k) for k in ("mm_per_px", "scale_source", "scale_confidence")}}


def _strip(e: dict) -> dict:
    return {k: v for k, v in e.items() if k not in ("rooms", "updated_at")}
