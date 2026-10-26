"""What each engine must build, derived from the master model — and how JARVIS knows what to rebuild.

`cad_specs` gives every element its exact CAD description (a wall's built line after the corner joins, an opening's
place along its host's built line, a room's outline plus the specs of the walls around it…). Each spec has a hash;
the stage rebuilds only the elements whose hash differs from the last build (dependency tracking for free: a wall
that gets thicker changes the joins of its neighbours, so their specs change too; a door that moves changes only
itself, FreeCAD then re-cuts its wall). `vis_specs` does the same for Blender, with the CAD mesh hashes inside, so a
sofa that moves touches nothing but the sofa."""
from __future__ import annotations

import hashlib
import json

from . import geometry as g
from .catalog import FURNITURE, MATERIALS
from .model import ArchModel, pt

WET = ("bathroom", "toilet", "laundry")


def _hash(spec: dict) -> str:
    raw = json.dumps({k: v for k, v in spec.items() if k != "_hash"}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _r(p) -> list[float]:
    return [round(float(p[0]), 1), round(float(p[1]), 1)]


def cad_specs(m: ArchModel) -> dict[str, dict]:
    d = m.data
    out: dict[str, dict] = {}
    lines: dict[str, tuple] = {}
    for lv in d["levels"]:
        walls = [w for w in d["walls"] if w["level"] == lv["id"]]
        joins = g.wall_joins(walls)
        for w in walls:
            a, b, s0 = g.cad_line(w, joins)
            lines[w["id"]] = (a, b, s0)
            L = g.dist(a, b)
            out[w["id"]] = {"type": "wall", "id": w["id"], "label": w["id"], "level": w["level"], "a": _r(a), "b": _r(b),
                            "z": float(w["base"]), "thickness": float(w["thickness"]), "height": float(w["height"]),
                            "kind": w.get("kind"), "confidence": float(w.get("confidence", 1)),
                            "full_volume": round(L * float(w["thickness"]) * float(w["height"]))}
    for o in m.openings():
        host = d_get(m, o.get("host"))
        if host is None or o["host"] not in lines:
            continue
        a, b, s0 = lines[o["host"]]
        out[o["id"]] = {"type": o["type"], "id": o["id"], "label": o["id"], "level": host["level"], "host": host["id"],
                        "a": _r(a), "b": _r(b), "along": round(float(o["offset"]) + s0, 1), "width": float(o["width"]),
                        "height": float(o["height"]), "sill": float(o.get("sill", 0)), "z": float(host["base"]),
                        "thickness": float(host["thickness"]), "kind": o.get("kind"), "swing": o.get("swing"),
                        "confidence": float(o.get("confidence", 1)), "host_hash": _hash(out[host["id"]])}
    for r in d["rooms"]:
        lv = m.level(r["level"])
        h = float(r.get("ceiling_height") or lv["height"])
        walls = {w: _hash(out[w]) for w in r.get("walls", []) if w in out}
        foot = [[out[w]["a"], out[w]["b"], out[w]["thickness"]] for w in sorted(walls)]
        out[r["id"]] = {"type": "room", "id": r["id"], "label": r["name"], "level": r["level"], "poly": [_r(p) for p in r["polygon"]],
                        "z": float(lv["elevation"]), "height": h, "walls": sorted(walls), "footprints": foot,
                        "kind": r.get("kind"), "confidence": float(r.get("confidence", 1))}
    for s in d["slabs"]:
        out[s["id"]] = {"type": "slab", "id": s["id"], "label": "Перекрытие " + m.level(s["level"])["name"],
                        "level": s["level"], "poly": [_r(p) for p in s["polygon"]], "z": float(s["elevation"]),
                        "thickness": float(s["thickness"])}
    roof = d.get("roof")
    if roof and roof.get("kind") != "none" and roof.get("polygon"):
        out[roof["id"]] = {"type": "roof", "id": roof["id"], "label": "Крыша", "level": roof["level"],
                           "poly": [_r(p) for p in roof["polygon"]], "z": float(roof["elevation"]),
                           "thickness": float(roof["thickness"])}
    for c in d["columns"]:
        out[c["id"]] = {"type": "column", "id": c["id"], "label": c["id"], "level": c["level"], "c": _r(c["center"]),
                        "width": float(c["width"]), "depth": float(c["depth"]), "height": float(c["height"]),
                        "z": float(c["base"]), "shape": c.get("shape", "rect")}
    for s in d["stairs"]:
        out[s["id"]] = {"type": "stair", "id": s["id"], "label": s["id"], "level": s["level"], "start": _r(s["start"]),
                        "direction": float(s["direction"]), "width": float(s["width"]), "length": float(s["length"]),
                        "height": float(s["height"]), "steps": int(s["steps"]), "z": float(s["base"])}
    for f in d["furniture"]:
        out[f["id"]] = {"type": "furniture", "id": f["id"], "label": f["name"], "level": f["level"],
                        "pos": _r(f["position"]), "rotation": float(f["rotation"]), "size": [float(x) for x in f["size"]],
                        "z": float(f.get("elevation", 0)), "kind": f["kind"]}
    for s in out.values():
        s["_hash"] = _hash(s)
    return out


def d_get(m: ArchModel, oid):
    return m.get(oid) if oid else None


def furniture_materials(f: dict) -> dict[str, str]:
    main = f.get("material") or FURNITURE[f["kind"]]["material"]
    sec = {"fabric_gray": "wood_oak", "fabric_white": "wood_oak", "fabric_blue": "wood_oak"}.get(main, "wood_oak")
    if f["kind"] in ("bed",):
        sec = "wood_oak"
    if f["kind"] in ("sink", "kitchen"):
        sec = "marble" if f["kind"] == "kitchen" else "wood_white"
    if f["kind"] == "tv":
        sec = "wood_oak"
    accent = {"bed": "fabric_blue", "sofa": "fabric_gray", "armchair": "fabric_gray"}.get(f["kind"], main)
    return {"main": main, "secondary": sec, "accent": accent, "metal": "metal_steel", "glass": "glass",
            "dark": "tv_black"}


def vis_specs(m: ArchModel, meshes: dict[str, str]) -> dict[str, dict]:
    """Blender's view of the model: CAD elements by their mesh hash + material, rooms (floor and ceiling finishes),
    procedural furniture, and one SCENE entry (lights per room, cameras around the footprint)."""
    d = m.data
    mats = d["materials"]
    out: dict[str, dict] = {}
    for w in d["walls"]:
        if w["id"] in meshes:
            out[w["id"]] = {"type": "wall", "level": w["level"], "mesh": meshes[w["id"]], "label": w["id"],
                            "roles": {"main": w.get("material") or mats["wall"]}}
    for o in m.openings():
        if o["id"] in meshes:
            frame = o.get("material") or (mats["door"] if o["type"] == "door" else mats["window"])
            roles = {"frame": "frame_white" if o["type"] == "window" else frame, "glass": "glass", "leaf": frame,
                     "main": frame}
            out[o["id"]] = {"type": o["type"], "level": o["level"], "mesh": meshes[o["id"]], "label": o["id"],
                            "roles": roles}
    for r in d["rooms"]:
        if r["id"] in meshes:
            lv = m.level(r["level"])
            floor = r.get("floor_material") or (mats.get("floor_wet", "tile_white") if r.get("kind") in WET
                                                else mats["floor"])
            out[r["id"]] = {"type": "room", "level": r["level"], "mesh": meshes[r["id"]], "label": r["name"],
                            "floor": floor, "ceiling": r.get("ceiling_material") or mats["ceiling"],
                            "height": float(r.get("ceiling_height") or lv["height"])}
    for s in d["slabs"]:
        if s["id"] in meshes:
            out[s["id"]] = {"type": "slab", "level": s["level"], "mesh": meshes[s["id"]], "label": s["id"],
                            "roles": {"main": s.get("material") or mats["slab"]}}
    roof = d.get("roof")
    if roof and roof["id"] in meshes and roof.get("kind") != "none":
        out[roof["id"]] = {"type": "roof", "level": roof["level"], "mesh": meshes[roof["id"]], "label": "Крыша",
                           "roles": {"main": roof.get("material") or mats["roof"]}}
    for c in d["columns"]:
        if c["id"] in meshes:
            out[c["id"]] = {"type": "column", "level": c["level"], "mesh": meshes[c["id"]], "label": c["id"],
                            "roles": {"main": c.get("material") or mats.get("column", "column_concrete")}}
    for s in d["stairs"]:
        if s["id"] in meshes:
            out[s["id"]] = {"type": "stair", "level": s["level"], "mesh": meshes[s["id"]], "label": s["id"],
                            "roles": {"main": s.get("material") or mats["stair"]}}
    for f in d["furniture"]:
        out[f["id"]] = {"type": "furniture", "level": f["level"], "kind": f["kind"], "pos": _r(f["position"]),
                        "rotation": float(f["rotation"]), "size": [float(x) for x in f["size"]],
                        "z": float(f.get("elevation", 0)), "label": f["name"], "materials": furniture_materials(f)}
    pts = [pt(p) for w in d["walls"] for p in (w["start"], w["end"])]
    if pts:
        x0, y0, x1, y1 = g.bbox(pts)
        rooms = []
        for r in d["rooms"]:
            poly = [pt(p) for p in r["polygon"]]
            bx0, by0, bx1, by1 = g.bbox(poly)
            lv = m.level(r["level"])
            rooms.append({"id": r["id"], "c": _r(r.get("anchor") or g.interior_point(poly)), "w": bx1 - bx0,
                          "d": by1 - by0, "area": r["area_m2"], "z": float(lv["elevation"]),
                          "h": float(r.get("ceiling_height") or lv["height"])})
        top = max((float(w["base"]) + float(w["height"]) for w in d["walls"]), default=3000.0)
        out["SCENE"] = {"type": "scene", "bbox": [x0, y0, x1, y1], "top": top, "rooms": rooms, "level": "SCENE"}
    for k, s in out.items():
        s["id"] = k
        s["_hash"] = _hash(s)
    return out


def used_materials(vspecs: dict) -> dict[str, dict]:
    ids = set()
    for s in vspecs.values():
        for key in ("roles", "materials"):
            ids.update((s.get(key) or {}).values())
        for key in ("floor", "ceiling"):
            if s.get(key):
                ids.add(s[key])
    return {i: MATERIALS[i] for i in ids if i in MATERIALS}


def diff(new: dict[str, dict], old: dict[str, str]) -> tuple[dict, list]:
    """(changed specs, removed ids) against the hashes of the last build."""
    changed = {k: v for k, v in new.items() if old.get(k) != v["_hash"]}
    removed = sorted(k for k in old if k not in new)
    return changed, removed
