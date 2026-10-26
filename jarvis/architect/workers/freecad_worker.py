"""JARVIS Architect — FreeCAD worker: the parametric CAD model (BIM objects of FreeCAD's Arch/BIM workbench on the
Open CASCADE kernel). Runs as a long-lived child of the core: `python3 -u freecad_worker.py <FreeCAD lib dir>`; one
JSON request per stdin line, one reply per stdout line (library prints go to stderr).

The core sends per-element CAD specs derived from the master model (service.cad_specs) — only those that changed —
and this worker updates exactly those objects in building.FCStd:

    Site «Участок» → Building «Здание» → Level (Building Storey) → groups Стены / Двери / Окна / Помещения /
    Перекрытия / Колонны / Лестницы / Мебель; every object carries JarvisId (WALL_001…), Kind, Confidence.

Walls are Arch walls on a base line (Width, Height, Align=Center: change a property in FreeCAD and it rebuilds);
doors and windows are Arch windows hosted in their wall (the opening is cut by OCCT booleans); rooms are Arch spaces
with the net floor area computed by OCCT (room face minus the wall solids). After every change each touched solid is
checked by OCCT (BRepCheck: isValid, check, solids, volume), repaired if needed (ShapeFix, removeSplitter, a plain
Part solid in place of a failing Arch object), checked again, and clashes between walls are measured (common volume).
Tessellated meshes of the touched objects go to <mesh_dir>/<ID>.json for Blender."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
import traceback

OUT = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
if len(sys.argv) > 1 and sys.argv[1]:
    sys.path.append(sys.argv[1])

import FreeCAD as App  # noqa: E402
import Part  # noqa: E402

V = App.Vector
_ARCH = None
DOCS: dict[str, object] = {}
CUR_ID = None
GROUPS = {"wall": "Стены", "door": "Двери", "window": "Окна", "room": "Помещения", "slab": "Перекрытия",
          "column": "Колонны", "stair": "Лестницы", "furniture": "Мебель", "roof": "Крыша"}
SOLID_TYPES = ("wall", "slab", "roof", "column", "stair", "furniture")


def reply(obj: dict) -> None:
    OUT.write(json.dumps(obj, ensure_ascii=False) + "\n")
    OUT.flush()


def progress(text: str) -> None:
    if CUR_ID is not None:
        reply({"id": CUR_ID, "progress": text})


def arch():
    global _ARCH
    if _ARCH is None:
        import Arch
        import Draft
        _ARCH = (Arch, Draft)
    return _ARCH


# ----------------------------------------------------------------------------- document

def jid(o) -> str | None:
    return getattr(o, "JarvisId", None)


def by_id(doc) -> dict:
    return {jid(o): o for o in doc.Objects if jid(o)}


def tag(o, sid: str, kind: str, spec: dict) -> None:
    for name, typ in (("JarvisId", "App::PropertyString"), ("JarvisType", "App::PropertyString"),
                      ("Kind", "App::PropertyString"), ("Confidence", "App::PropertyFloat"),
                      ("SpecHash", "App::PropertyString")):
        if not hasattr(o, name):
            o.addProperty(typ, name, "Jarvis")
    o.JarvisId, o.JarvisType = sid, kind
    o.Kind = str(spec.get("kind") or "")
    o.Confidence = float(spec.get("confidence", 1.0))
    o.SpecHash = spec.get("_hash", "")
    o.Label = spec.get("label") or sid


def open_doc(path: str, fresh: bool):
    doc = DOCS.get(path)
    if doc is not None and fresh:
        App.closeDocument(doc.Name)
        DOCS.pop(path, None)
        doc = None
    if doc is None:
        if os.path.exists(path) and not fresh:
            doc = App.openDocument(path)
        else:
            doc = App.newDocument("JarvisBuilding")
        DOCS[path] = doc
    return doc


def structure(doc, levels: list[dict]):
    """Site → Building → Levels → category groups (created once, kept)."""
    A, _ = arch()
    objs = by_id(doc)
    site = objs.get("SITE")
    if site is None:
        site = A.makeSite([])
        tag(site, "SITE", "site", {"label": "Участок"})
    bld = objs.get("BUILDING")
    if bld is None:
        bld = A.makeBuilding([])
        tag(bld, "BUILDING", "building", {"label": "Здание"})
        site.addObject(bld)
    out = {}
    for lv in levels:
        lo = objs.get(lv["id"])
        if lo is None:
            lo = A.makeFloor([])
            tag(lo, lv["id"], "level", {"label": lv.get("name") or lv["id"]})
            bld.addObject(lo)
        lo.Label = lv.get("name") or lv["id"]
        lo.Placement.Base = V(0, 0, 0)
        if hasattr(lo, "Height"):
            lo.Height = float(lv.get("height", 2700))
        groups = {}
        for g in lo.Group:
            if g.TypeId == "App::DocumentObjectGroup" and jid(g):
                groups[g.Kind] = g
        for kind, title in GROUPS.items():
            if kind not in groups:
                grp = doc.addObject("App::DocumentObjectGroup", "G_" + kind)
                tag(grp, f"{lv['id']}_{kind.upper()}S", "group", {"label": title, "kind": kind})
                lo.addObject(grp)
                groups[kind] = grp
        out[lv["id"]] = groups
    return out


# ----------------------------------------------------------------------------- builders

def _rot_for(u):
    """Placement rotation of an Arch window in a wall along u: local X along the wall, local Y up."""
    ang = math.degrees(math.atan2(u[1], u[0]))
    return App.Rotation(V(0, 0, 1), ang).multiply(App.Rotation(V(1, 0, 0), 90))


def make_wall(doc, s: dict):
    A, D = arch()
    a, b, z = s["a"], s["b"], float(s["z"])
    line = D.makeLine(V(a[0], a[1], z), V(b[0], b[1], z))
    line.Label = s["id"] + "_ось"
    w = A.makeWall(line, width=float(s["thickness"]), height=float(s["height"]), align="Center")
    if hasattr(w, "IfcType"):
        w.IfcType = "Wall"
    return w, [line]


def plain_wall_shape(s: dict, cuts: list[dict]):
    """The same wall as a plain OCCT solid (used when the Arch wall fails a check): a box along the line, openings
    cut out by booleans, then ShapeFix."""
    a, b, z = s["a"], s["b"], float(s["z"])
    ux, uy = b[0] - a[0], b[1] - a[1]
    L = math.hypot(ux, uy)
    ux, uy = ux / L, uy / L
    nx, ny = -uy, ux
    t = float(s["thickness"]) / 2
    pts = [V(a[0] + nx * t, a[1] + ny * t, z), V(b[0] + nx * t, b[1] + ny * t, z),
           V(b[0] - nx * t, b[1] - ny * t, z), V(a[0] - nx * t, a[1] - ny * t, z)]
    face = Part.Face(Part.makePolygon(pts + [pts[0]]))
    solid = face.extrude(V(0, 0, float(s["height"])))
    for c in cuts:
        box = opening_box(c, extra=50)
        solid = solid.cut(box)
    solid = solid.removeSplitter()
    solid.fix(1e-4, 1e-4, 1e-4)
    return solid


def opening_box(o: dict, extra: float = 20.0):
    a, b = o["a"], o["b"]
    ux, uy = b[0] - a[0], b[1] - a[1]
    L = math.hypot(ux, uy) or 1.0
    ux, uy = ux / L, uy / L
    nx, ny = -uy, ux
    c = float(o["along"])
    hw, t = float(o["width"]) / 2, float(o["thickness"]) / 2 + extra
    z0 = float(o["z"]) + float(o["sill"])
    p = lambda s, k: V(a[0] + ux * s + nx * k, a[1] + uy * s + ny * k, z0)   # noqa: E731
    pts = [p(c - hw, -t), p(c + hw, -t), p(c + hw, t), p(c - hw, t)]
    return Part.Face(Part.makePolygon(pts + [pts[0]])).extrude(V(0, 0, float(o["height"])))


def make_opening(doc, s: dict, host):
    A, D = arch()
    a, b = s["a"], s["b"]
    ux, uy = b[0] - a[0], b[1] - a[1]
    L = math.hypot(ux, uy) or 1.0
    u = (ux / L, uy / L)
    n = (-u[1], u[0])
    w, h = float(s["width"]), float(s["height"])
    start = float(s["along"]) - w / 2
    frame_depth = min(70.0, float(s["thickness"]) * 0.6)
    base = V(a[0] + u[0] * start + n[0] * frame_depth / 2, a[1] + u[1] * start + n[1] * frame_depth / 2,
             float(s["z"]) + float(s["sill"]))
    pl = App.Placement(base, _rot_for(u))
    if s["type"] == "door" and s.get("kind") == "opening":
        box = doc.addObject("Part::Feature", "Opening")
        box.Shape = opening_box(s, extra=60)
        host.Subtractions = list(host.Subtractions) + [box]
        return box
    if s["type"] == "door":
        preset = "Simple door"
        win = A.makeWindowPreset(preset, width=w, height=h, h1=50, h2=50, h3=50, w1=frame_depth, w2=40, o1=0, o2=frame_depth / 2 - 20,
                                 placement=pl)
        if hasattr(win, "IfcType"):
            win.IfcType = "Door"
    else:
        preset = "Open 2-pane" if w >= 1100 else "Open 1-pane"
        win = A.makeWindowPreset(preset, width=w, height=h, h1=50, h2=50, h3=50, w1=frame_depth, w2=40, o1=0, o2=frame_depth / 2 - 20,
                                 placement=pl)
    win.Hosts = [host]
    return win


def poly_face(poly, z):
    pts = [V(p[0], p[1], z) for p in poly]
    return Part.Face(Part.makePolygon(pts + [pts[0]]))


def make_room(doc, s: dict, walls: dict):
    A, _ = arch()
    z, h = float(s["z"]), float(s["height"])
    base = doc.addObject("Part::Feature", "RoomBase")
    base.Shape = poly_face(s["poly"], z).extrude(V(0, 0, h))
    base.Label = s["id"] + "_объём"
    sp = A.makeSpace(base)
    if hasattr(sp, "Visibility"):
        base.Visibility = False
    return sp, [base]


def net_floor(s: dict, walls: dict):
    """The room's clear floor: its outline minus the plan footprints of the walls around it (OCCT boolean cut;
    door openings do not add their threshold to the room)."""
    z = float(s["z"]) + 5.0
    face = poly_face(s["poly"], z)
    tools = []
    for a, b, t in s.get("footprints", []):
        ux, uy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(ux, uy) or 1.0
        nx, ny = -uy / L * t / 2, ux / L * t / 2
        pts = [V(a[0] + nx, a[1] + ny, z - 50), V(b[0] + nx, b[1] + ny, z - 50), V(b[0] - nx, b[1] - ny, z - 50),
               V(a[0] - nx, a[1] - ny, z - 50)]
        tools.append(Part.Face(Part.makePolygon(pts + [pts[0]])).extrude(V(0, 0, 100)))
    if tools:
        tool = tools[0] if len(tools) == 1 else tools[0].multiFuse(tools[1:])
        res = face.cut(tool)
        faces = sorted(res.Faces, key=lambda f: -f.Area)
        if faces:
            return faces[0]
    return face


def make_slab(doc, s: dict):
    A, _ = arch()
    base = doc.addObject("Part::Feature", "SlabBase")
    base.Shape = poly_face(s["poly"], float(s["z"]))
    base.Label = s["id"] + "_контур"
    st = A.makeStructure(base, height=float(s["thickness"]))
    st.IfcType = "Roof" if s["type"] == "roof" else "Slab"
    base.Visibility = False
    return st, [base]


def make_column(doc, s: dict):
    A, _ = arch()
    if s.get("shape") == "round":
        base = doc.addObject("Part::Feature", "ColumnBase")
        base.Shape = Part.Face(Part.Wire(Part.makeCircle(float(s["width"]) / 2, V(s["c"][0], s["c"][1], float(s["z"])))))
        col = A.makeStructure(base, height=float(s["height"]))
        base.Visibility = False
        extra = [base]
    else:
        col = A.makeStructure(length=float(s["width"]), width=float(s["depth"]), height=float(s["height"]))
        col.Placement.Base = V(s["c"][0], s["c"][1], float(s["z"]))
        extra = []
    col.IfcType = "Column"
    return col, extra


def make_stair(doc, s: dict):
    A, _ = arch()
    st = A.makeStairs(length=float(s["length"]), width=float(s["width"]), height=float(s["height"]),
                      steps=int(s["steps"]))
    st.Placement = App.Placement(V(s["start"][0], s["start"][1], float(s["z"])),
                                 App.Rotation(V(0, 0, 1), float(s.get("direction", 0))))
    return st, []


def make_furniture(doc, s: dict):
    w, d, h = (float(x) for x in s["size"])
    box = Part.makeBox(w, d, h, V(-w / 2, -d / 2, 0))
    box.rotate(V(0, 0, 0), V(0, 0, 1), float(s.get("rotation", 0)))
    box.translate(V(s["pos"][0], s["pos"][1], float(s["z"])))
    o = doc.addObject("Part::Feature", "Furniture")
    o.Shape = box
    if not hasattr(o, "IfcType"):
        o.addProperty("App::PropertyString", "IfcType", "IFC")
    o.IfcType = "Furniture"
    return o, []


# ----------------------------------------------------------------------------- checks (OCCT)

def check_shape(o, kind: str) -> list[str]:
    """OCCT checks of one element: BRepCheck of the whole shape, then the BOP self-intersection check of every
    solid on its own (a window is several touching solids — frame, glass — which is fine between them)."""
    s = o.Shape
    probs = []
    if s.isNull():
        return ["пустая геометрия"]
    if not s.isValid():
        probs.append("геометрия не прошла проверку OCCT (isValid)")
    try:
        s.check(False)
    except Exception as e:          # noqa: BLE001 — BRepCheck reports problems as an exception text
        probs.append("BRepCheck: " + _first(e))
    for i, so in enumerate(s.Solids):
        try:
            so.check(True)
        except Exception as e:      # noqa: BLE001
            probs.append(f"тело {i + 1}: " + _first(e))
            break
    if kind in SOLID_TYPES and (not s.Solids or s.Volume <= 1.0):
        probs.append("нет твёрдого тела (solid)")
    if kind == "room" and s.Volume <= 1.0:
        probs.append("пустой объём помещения")
    return probs


def _first(e) -> str:
    lines = [x.strip() for x in str(e).splitlines() if x.strip()]
    return " ".join(lines[:3])[:200] or e.__class__.__name__


def repair(doc, o, kind: str, spec: dict, cuts: list[dict]) -> tuple[object, str]:
    """Fix an object that failed the check: ShapeFix + removeSplitter on Part objects; a failing Arch wall is
    replaced by the same wall as a plain OCCT solid with its openings cut (named and tagged the same)."""
    if kind == "wall":
        shape = plain_wall_shape(spec, cuts)
        if shape.isValid() and shape.Solids:
            base = getattr(o, "Base", None)
            doc.removeObject(o.Name)
            if base is not None:
                try:
                    doc.removeObject(base.Name)
                except Exception:      # noqa: BLE001
                    pass
            n = doc.addObject("Part::Feature", "WallSolid")
            n.Shape = shape
            return n, "стена Arch заменена точным телом OCCT (вырезы проёмов булевыми операциями)"
        return o, ""
    if o.TypeId == "Part::Feature":
        s = o.Shape.copy()
        s.fix(1e-4, 1e-4, 1e-4)
        try:
            s = s.removeSplitter()
        except Exception:              # noqa: BLE001
            pass
        if s.isValid():
            o.Shape = s
            return o, "исправлено ShapeFix"
    return o, ""


def clashes(objs: dict, ids: set[str]) -> list[str]:
    """Walls that overlap other walls by more than a litre (OCCT common), among the touched ones."""
    out = []
    walls = {k: v for k, v in objs.items() if getattr(v, "JarvisType", "") == "wall" and not v.Shape.isNull()}
    for a in sorted(ids & set(walls)):
        sa = walls[a].Shape
        for b, wb in walls.items():
            if b == a or (b in ids and b < a):
                continue
            if not sa.BoundBox.intersect(wb.Shape.BoundBox):
                continue
            try:
                v = sa.common(wb.Shape).Volume
            except Exception:          # noqa: BLE001
                continue
            if v > 1e6:
                out.append(f"{a} и {b} пересекаются ({v / 1e9:.3f} м³)")
    return out


# ----------------------------------------------------------------------------- meshes

def window_roles(o) -> list[str]:
    parts = list(getattr(o, "WindowParts", []) or [])
    roles = []
    for i in range(0, len(parts), 5):
        t = (parts[i + 1] if i + 1 < len(parts) else "").lower()
        roles.append("glass" if "glass" in t else "leaf" if "panel" in t else "frame")
    return roles


def tess(o, kind: str, tol: float = 1.5, face=None) -> dict:
    parts = []

    def add(shape, role):
        if shape is None or shape.isNull():
            return
        v, f = shape.tessellate(tol)
        if not f:
            return
        parts.append({"role": role, "v": [round(c, 2) for p in v for c in (p.x, p.y, p.z)],
                      "f": [i for t in f for i in t]})
    if face is not None:
        add(face, "floor")
    elif kind in ("door", "window") and o.TypeId != "Part::Feature":
        roles = window_roles(o)
        solids = o.Shape.Solids
        if roles and len(roles) == len(solids):
            for s, r in zip(solids, roles):
                add(s, r)
        else:
            for s in solids:
                add(s, "frame")
    else:
        add(o.Shape, "main")
    return {"parts": parts}


def write_mesh(mesh_dir: str, sid: str, data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":"))
    h = hashlib.sha1(raw.encode()).hexdigest()[:16]
    with open(os.path.join(mesh_dir, sid + ".json"), "w") as f:
        f.write(raw)
    return h


# ----------------------------------------------------------------------------- ops

def op_ping() -> dict:
    t0 = time.time()
    arch()
    return {"freecad": ".".join(App.Version()[:3]), "occt": getattr(Part, "OCC_VERSION", "?"),
            "python": sys.version.split()[0], "arch_ms": round((time.time() - t0) * 1000)}


def op_sync(path: str, levels: list[dict], specs: dict, removed: list[str], fresh: bool = False,
            mesh_dir: str = "", tess_tol: float = 1.5) -> dict:
    """Bring building.FCStd to the given specs: create/replace the changed elements, delete the removed ones,
    recompute, check with OCCT (repair when needed), re-mesh what was touched, save."""
    t0 = time.time()
    doc = open_doc(path, fresh)
    groups = structure(doc, levels)
    objs = by_id(doc)
    touched: set[str] = set()
    # 1 — hosts of openings that change or go away must be re-meshed (their hole moves)
    for sid in list(specs) + list(removed):
        o = objs.get(sid)
        if o is not None and getattr(o, "JarvisType", "") in ("door", "window"):
            for h in list(getattr(o, "Hosts", []) or []):
                if jid(h):
                    touched.add(jid(h))
            if o.TypeId == "Part::Feature":          # a plain opening box lives in its wall's Subtractions
                for w in objs.values():
                    if o in list(getattr(w, "Subtractions", []) or []):
                        touched.add(jid(w))
    # 2 — delete removed and changed (openings first: they reference walls)
    order = {"door": 0, "window": 0, "furniture": 1, "room": 1, "column": 1, "stair": 1, "slab": 2, "roof": 2, "wall": 3}
    doomed = [sid for sid in list(removed) + list(specs) if sid in objs]
    doomed.sort(key=lambda sid: order.get(getattr(objs[sid], "JarvisType", ""), 4))
    for sid in doomed:
        o = objs[sid]
        extras = [getattr(o, "Base", None)]
        if getattr(o, "JarvisType", "") in ("door", "window") and o.TypeId == "Part::Feature":
            for w in objs.values():
                subs = list(getattr(w, "Subtractions", []) or [])
                if o in subs:
                    subs.remove(o)
                    w.Subtractions = subs
        name = o.Name
        doc.removeObject(name)
        for e in extras:
            if e is not None:
                try:
                    if e.Name in [x.Name for x in doc.Objects]:
                        doc.removeObject(e.Name)
                except Exception:      # noqa: BLE001
                    pass
        objs.pop(sid, None)
    doc.recompute()
    # 3 — create
    report = {"created": [], "repaired": [], "problems": {}, "clashes": [], "net_area": {}, "meshes": {}}
    by_kind: dict[str, list] = {}
    for sid, s in specs.items():
        by_kind.setdefault(s["type"], []).append(s)
    for s in by_kind.get("wall", []):
        o, extra = make_wall(doc, s)
        tag(o, s["id"], "wall", s)
        groups[s["level"]]["wall"].addObject(o)
        objs[s["id"]] = o
        touched.add(s["id"])
    doc.recompute()
    for s in by_kind.get("door", []) + by_kind.get("window", []):
        host = objs.get(s["host"])
        if host is None:
            report["problems"][s["id"]] = ["нет стены-основы " + s["host"]]
            continue
        try:
            o = make_opening(doc, s, host)
        except Exception as e:          # noqa: BLE001 — Arch preset failed: a plain hole instead
            o = doc.addObject("Part::Feature", "Opening")
            o.Shape = opening_box(s, extra=60)
            host.Subtractions = list(host.Subtractions) + [o]
            report["repaired"].append(f"{s['id']}: вместо рамы Arch — проём булевой операцией ({e})"[:200])
        tag(o, s["id"], s["type"], s)
        groups[s["level"]][s["type"]].addObject(o)
        objs[s["id"]] = o
        touched.update((s["id"], s["host"]))
    doc.recompute()
    walls = {k: v for k, v in objs.items() if getattr(v, "JarvisType", "") == "wall"}
    for kind, maker in (("slab", make_slab), ("roof", make_slab), ("column", make_column), ("stair", make_stair),
                        ("furniture", make_furniture)):
        for s in by_kind.get(kind, []):
            try:
                o, extra = maker(doc, s)
            except Exception as e:      # noqa: BLE001
                report["problems"][s["id"]] = [f"не построено: {e}"[:200]]
                continue
            tag(o, s["id"], kind, s)
            grp = groups[s["level"]]["roof" if kind == "roof" else kind]
            grp.addObject(o)
            objs[s["id"]] = o
            touched.add(s["id"])
    doc.recompute()
    for s in by_kind.get("room", []):
        try:
            o, extra = make_room(doc, s, walls)
        except Exception as e:          # noqa: BLE001
            report["problems"][s["id"]] = [f"помещение не построено: {e}"[:200]]
            continue
        tag(o, s["id"], "room", s)
        groups[s["level"]]["room"].addObject(o)
        objs[s["id"]] = o
        touched.add(s["id"])
    doc.recompute()
    # 4 — check every touched object with OCCT; repair; check again
    hosted: dict[str, list] = {}
    for sid, s in specs.items():
        if s["type"] in ("door", "window"):
            hosted.setdefault(s["host"], []).append(s)
    for sid in sorted(touched):
        o = objs.get(sid)
        if o is None:
            continue
        kind = getattr(o, "JarvisType", "")
        probs = check_shape(o, kind)
        if kind == "wall" and not probs:
            full = float(specs.get(sid, {}).get("full_volume") or 0)
            if full and o.Shape.Volume > full * 1.001 + 1e5:
                probs.append("объём больше расчётного")
        if probs:
            spec = specs.get(sid) or {}
            if kind == "wall" and spec:
                cuts = [h for h in hosted.get(sid, [])]
                n, how = repair(doc, o, kind, spec, cuts)
                if how:
                    tag(n, sid, kind, spec)
                    groups[spec["level"]]["wall"].addObject(n)
                    objs[sid] = n
                    doc.recompute()
                    again = check_shape(n, kind)
                    report["repaired"].append(f"{sid}: {how}")
                    if again:
                        report["problems"][sid] = again
                    continue
            elif o.TypeId == "Part::Feature":
                n, how = repair(doc, o, kind, spec, [])
                if how:
                    again = check_shape(n, kind)
                    report["repaired"].append(f"{sid}: {how}")
                    if again:
                        report["problems"][sid] = again
                    continue
            report["problems"][sid] = probs
    # openings really cut their host?
    for sid, s in specs.items():
        if s["type"] not in ("door", "window") or s["host"] not in objs:
            continue
        host = objs[s["host"]]
        full = float(specs.get(s["host"], {}).get("full_volume") or 0) or None
        if full is None:
            continue
        cut = full - host.Shape.Volume
        need = float(s["width"]) * float(s["height"]) * float(s["thickness"]) * 0.5
        if cut < need:
            report["problems"].setdefault(sid, []).append("проём не вырезан в стене")
    report["clashes"] = clashes(objs, touched)
    # 5 — rooms: net floor areas (OCCT) and floor meshes; meshes of everything touched
    if mesh_dir:
        os.makedirs(mesh_dir, exist_ok=True)
    walls = {k: v for k, v in objs.items() if getattr(v, "JarvisType", "") == "wall"}
    for sid in sorted(touched):
        o = objs.get(sid)
        if o is None:
            continue
        kind = getattr(o, "JarvisType", "")
        if kind == "room":
            s = specs.get(sid)
            if s is None:
                continue
            f = net_floor(s, walls)
            report["net_area"][sid] = round(f.Area / 1e6, 2)
            if mesh_dir:
                report["meshes"][sid] = write_mesh(mesh_dir, sid, tess(o, kind, tess_tol, face=f))
        elif mesh_dir and kind:
            report["meshes"][sid] = write_mesh(mesh_dir, sid, tess(o, kind, tess_tol))
    for sid in removed:
        if mesh_dir:
            try:
                os.remove(os.path.join(mesh_dir, sid + ".json"))
            except OSError:
                pass
    report["created"] = sorted(specs)
    doc.recompute()
    doc.saveAs(path)
    report["saved"] = path
    report["objects"] = len([o for o in doc.Objects if jid(o)])
    report["ms"] = round((time.time() - t0) * 1000)
    return report


def op_inspect(path: str) -> dict:
    """What building.FCStd holds: every Jarvis object with its type, label, validity and volume; the tree."""
    doc = DOCS.get(path) or App.openDocument(path)
    DOCS[path] = doc
    items = []
    for o in doc.Objects:
        if not jid(o):
            continue
        s = getattr(o, "Shape", None)
        ok = bool(s is not None and not s.isNull() and s.isValid())
        items.append({"id": jid(o), "type": getattr(o, "JarvisType", ""), "label": o.Label, "freecad_type": o.TypeId,
                      "ifc": getattr(o, "IfcType", ""), "valid": ok,
                      "volume_m3": round(s.Volume / 1e9, 4) if ok and s.Solids else 0.0,
                      "parent": [p.Label for p in o.InList if p.TypeId != "Part::Feature"][:2]})
    return {"objects": items, "count": len(items)}


def op_export(path: str, out_dir: str, formats: list[str], name: str = "building") -> dict:
    """STEP / STL / BREP / FCStd of the building elements, each read back to prove it is a real file."""
    import Import
    import Mesh
    doc = DOCS.get(path) or App.openDocument(path)
    DOCS[path] = doc
    elems = [o for o in doc.Objects if getattr(o, "JarvisType", "") in ("wall", "door", "window", "slab", "roof",
                                                                         "column", "stair", "furniture")
             and not o.Shape.isNull()]
    out = {}
    os.makedirs(out_dir, exist_ok=True)
    for fmt in formats:
        try:
            if fmt == "step":
                p = os.path.join(out_dir, name + ".step")
                Import.export(elems, p)
                back = Part.read(p)
                out[fmt] = {"path": p, "size": os.path.getsize(p), "solids": len(back.Solids), "valid": back.isValid()}
            elif fmt == "stl":
                p = os.path.join(out_dir, name + ".stl")
                Mesh.export(elems, p)
                m = Mesh.Mesh(p)
                out[fmt] = {"path": p, "size": os.path.getsize(p), "facets": m.CountFacets}
            elif fmt == "brep":
                p = os.path.join(out_dir, name + ".brep")
                Part.makeCompound([o.Shape for o in elems]).exportBrep(p)
                back = Part.Shape()
                back.read(p)
                out[fmt] = {"path": p, "size": os.path.getsize(p), "solids": len(back.Solids)}
            elif fmt == "fcstd":
                p = os.path.join(out_dir, name + ".FCStd")
                doc.saveCopy(p)
                d2 = App.openDocument(p)
                n = len([o for o in d2.Objects if jid(o)])
                App.closeDocument(d2.Name)
                out[fmt] = {"path": p, "size": os.path.getsize(p), "objects": n}
            else:
                out[fmt] = {"error": "формат не поддерживается FreeCAD-исполнителем"}
        except Exception as e:          # noqa: BLE001
            out[fmt] = {"error": f"{e.__class__.__name__}: {e}"[:300]}
    return out


def op_close(path: str) -> dict:
    doc = DOCS.pop(path, None)
    if doc is not None:
        App.closeDocument(doc.Name)
    return {"closed": doc is not None}


OPS = {"ping": op_ping, "sync": op_sync, "inspect": op_inspect, "export": op_export, "close": op_close}


def main() -> None:
    global CUR_ID
    reply({"ready": True, "pid": os.getpid(), "freecad": ".".join(App.Version()[:3])})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        CUR_ID = req.get("id")
        try:
            res = OPS[req["op"]](**(req.get("args") or {}))
            reply({"id": CUR_ID, "ok": True, "result": res})
        except Exception as e:          # noqa: BLE001 — the error goes back; the worker stays up
            reply({"id": CUR_ID, "ok": False, "error": f"{e.__class__.__name__}: {e}"[:600],
                   "trace": traceback.format_exc()[-2000:]})
        CUR_ID = None


if __name__ == "__main__":
    main()
