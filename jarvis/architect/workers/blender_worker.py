"""JARVIS Architect — Blender worker: the look of the building (materials, light, cameras, furniture, renders,
exports). Run: blender -b --factory-startup --python-exit-code 1 --python blender_worker.py. One JSON request per
stdin line, one reply per stdout line (Blender's own prints go to stderr).

Geometry comes from FreeCAD's meshes (<mesh_dir>/<ID>.json, millimetres); Blender never invents walls. Every
object is named after its master-model element (WALL_001 → WALL_001_VIS, ROOM_001 → ROOM_001_FLOOR_VIS /
ROOM_001_CEIL_VIS, FURN_001 → FURN_001_VIS) and carries jarvis_id / jarvis_type / level as custom properties
(glTF extras), so a change of WALL_001 replaces exactly WALL_001_VIS. Furniture is built procedurally from boxes and
cylinders (a model file <library>/<kind>.glb, if present, is used instead: the future furniture library)."""
from __future__ import annotations

import json
import math
import os
import sys
import time
import traceback

OUT = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)

import bpy  # noqa: E402
import bmesh  # noqa: E402
from mathutils import Matrix, Vector  # noqa: E402

MM = 0.001
STATE = {"blend": None}
CUR = None
CAT = {"wall": "Стены", "door": "Двери", "window": "Окна", "room": "Полы и потолки", "slab": "Перекрытия",
       "roof": "Крыша", "column": "Колонны", "stair": "Лестницы", "furniture": "Мебель"}


def reply(obj):
    OUT.write(json.dumps(obj, ensure_ascii=False) + "\n")
    OUT.flush()


def progress(text):
    if CUR is not None:
        reply({"id": CUR, "progress": text})


# ----------------------------------------------------------------------------- scene

def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.unit_settings.system = "METRIC"
    sc.unit_settings.length_unit = "METERS"
    sc.render.engine = "BLENDER_EEVEE"
    w = bpy.data.worlds.new("JARVIS sky")
    w.use_nodes = True
    bg = w.node_tree.nodes.get("Background")
    bg.inputs[0].default_value = (0.78, 0.84, 0.92, 1)
    bg.inputs[1].default_value = 0.55
    sc.world = w
    items = [i.identifier for i in sc.view_settings.bl_rna.properties["view_transform"].enum_items]
    sc.view_settings.view_transform = "AgX" if "AgX" in items else "Filmic"
    sc.view_settings.exposure = -0.3


def open_blend(path, fresh):
    if STATE["blend"] == path and not fresh:
        return
    if os.path.exists(path) and not fresh:
        bpy.ops.wm.open_mainfile(filepath=path)
    else:
        reset_scene()
    STATE["blend"] = path


def collection(level: str, kind: str):
    root = bpy.context.scene.collection
    lname = f"{level}"
    lc = bpy.data.collections.get(lname)
    if lc is None:
        lc = bpy.data.collections.new(lname)
        root.children.link(lc)
    cname = f"{level} · {CAT.get(kind, kind)}"
    c = bpy.data.collections.get(cname)
    if c is None:
        c = bpy.data.collections.new(cname)
        lc.children.link(c)
    return c


def drop(name):
    ob = bpy.data.objects.get(name)
    if ob is not None:
        me = ob.data if ob.type == "MESH" else None
        bpy.data.objects.remove(ob, do_unlink=True)
        if me is not None and me.users == 0:
            bpy.data.meshes.remove(me)


# ----------------------------------------------------------------------------- materials

PATTERN_LINKS = []


def material(mid: str, m: dict):
    name = "M_" + mid
    mat = bpy.data.materials.get(name)
    if mat is not None:
        return mat
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    col = tuple(m.get("color", (0.8, 0.8, 0.8))) + (1.0,)
    bsdf.inputs["Base Color"].default_value = col
    bsdf.inputs["Roughness"].default_value = float(m.get("rough", 0.6))
    bsdf.inputs["Metallic"].default_value = float(m.get("metal", 0.0))
    mat.diffuse_color = col
    if m.get("alpha") is not None:
        a = float(m["alpha"])
        bsdf.inputs["Alpha"].default_value = a
        if "Transmission Weight" in bsdf.inputs:
            bsdf.inputs["Transmission Weight"].default_value = 0.9
        try:
            mat.surface_render_method = "BLENDED"
        except (AttributeError, TypeError):
            pass
    pat = m.get("pattern", "plain")
    if pat != "plain":
        dark = tuple(max(0.0, c * 0.72) for c in col[:3]) + (1.0,)
        coord = nt.nodes.new("ShaderNodeTexCoord")
        mapping = nt.nodes.new("ShaderNodeMapping")
        nt.links.new(coord.outputs["Object"], mapping.inputs["Vector"])
        ramp = nt.nodes.new("ShaderNodeValToRGB")
        ramp.color_ramp.elements[0].color = dark
        ramp.color_ramp.elements[1].color = col
        if pat == "wood":
            tex = nt.nodes.new("ShaderNodeTexWave")
            tex.inputs["Scale"].default_value = 3.0
            tex.inputs["Distortion"].default_value = 6.0
            mapping.inputs["Scale"].default_value = (1.0, 8.0, 1.0)
            out = tex.outputs["Fac"]
        elif pat in ("tile", "brick"):
            tex = nt.nodes.new("ShaderNodeTexBrick")
            tex.inputs["Scale"].default_value = 1.6 if pat == "tile" else 4.0
            tex.inputs["Mortar Size"].default_value = 0.01
            tex.inputs["Color1"].default_value = col
            tex.inputs["Color2"].default_value = tuple(c * 0.92 for c in col[:3]) + (1.0,)
            tex.inputs["Mortar"].default_value = (0.75, 0.75, 0.75, 1) if pat == "tile" else (0.6, 0.58, 0.55, 1)
            if pat == "tile":
                tex.offset = 0.0
            out = None
        else:
            tex = nt.nodes.new("ShaderNodeTexNoise")
            tex.inputs["Scale"].default_value = 12.0
            out = tex.outputs["Fac"]
        nt.links.new(mapping.outputs["Vector"], tex.inputs["Vector"])
        if out is not None:
            nt.links.new(out, ramp.inputs["Fac"])
            src = ramp.outputs["Color"]
        else:
            src = tex.outputs["Color"]
        mat["pattern_src"] = src.node.name
        nt.links.new(src, bsdf.inputs["Base Color"])
    return mat


def pattern_links(on: bool):
    """glTF keeps only plain colours: unlink the procedural patterns for the export, link them back after."""
    for mat in bpy.data.materials:
        src = mat.get("pattern_src") if mat.node_tree else None
        if not src:
            continue
        nt = mat.node_tree
        bsdf = nt.nodes.get("Principled BSDF")
        inp = bsdf.inputs["Base Color"]
        if on and not inp.is_linked:
            node = nt.nodes.get(src)
            if node is not None:
                nt.links.new(node.outputs["Color"], inp)
        if not on:
            for ln in list(inp.links):
                nt.links.remove(ln)


# ----------------------------------------------------------------------------- meshes

def mesh_object(name: str, parts: list[dict], mats: list, col, props: dict, z_offset: float = 0.0):
    drop(name)
    verts, faces, mat_idx = [], [], []
    for p in parts:
        base = len(verts)
        v = p["v"]
        verts += [(v[i] * MM, v[i + 1] * MM, v[i + 2] * MM + z_offset) for i in range(0, len(v), 3)]
        f = p["f"]
        for i in range(0, len(f), 3):
            faces.append((f[i] + base, f[i + 1] + base, f[i + 2] + base))
            mat_idx.append(p.get("mat", 0))
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.validate(clean_customdata=False)
    for m in mats:
        me.materials.append(m)
    if mat_idx and len(me.polygons) == len(mat_idx):
        me.polygons.foreach_set("material_index", mat_idx)
    me.update()
    ob = bpy.data.objects.new(name, me)
    col.objects.link(ob)
    for k, v in props.items():
        ob[k] = v
    return ob


def box(bm, x0, y0, z0, x1, y1, z1, mi):
    r = bmesh.ops.create_cube(bm, size=1.0)
    vs = r["verts"]
    bmesh.ops.scale(bm, vec=Vector((x1 - x0, y1 - y0, z1 - z0)), verts=vs)
    bmesh.ops.translate(bm, vec=Vector(((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)), verts=vs)
    for f in {f for v in vs for f in v.link_faces}:
        f.material_index = mi


def cyl(bm, cx, cy, z0, z1, r, mi, seg=20, sx=1.0, sy=1.0):
    res = bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=seg, radius1=r, radius2=r, depth=z1 - z0)
    vs = res["verts"]
    bmesh.ops.scale(bm, vec=Vector((sx, sy, 1.0)), verts=vs)
    bmesh.ops.translate(bm, vec=Vector((cx, cy, (z0 + z1) / 2)), verts=vs)
    for f in {f for v in vs for f in v.link_faces}:
        f.material_index = mi


# Each builder draws in millimetres around the origin: width along X, depth along Y (back at +Y), height up.
# Material slots: 0 main, 1 secondary (legs, frame), 2 accent (cushions, blanket), 3 metal, 4 glass, 5 dark.
def f_sofa(bm, w, d, h):
    seat = h * 0.48
    box(bm, -w / 2, -d / 2, 80, w / 2, d / 2, seat - 120, 0)
    box(bm, -w / 2, d / 2 - d * 0.25, 80, w / 2, d / 2, h, 0)
    for s in (-1, 1):
        box(bm, s * w / 2 - (s > 0) * w * 0.09, -d / 2, 80, s * w / 2 + (s < 0) * w * 0.09, d / 2, h * 0.68, 0)
    n = 2 if w < 1800 else 3
    cw = (w * 0.82) / n
    for i in range(n):
        x0 = -w * 0.41 + i * cw
        box(bm, x0 + 8, -d / 2 + 20, seat - 120, x0 + cw - 8, d / 2 - d * 0.25, seat, 2)
    for x in (-w / 2 + 60, w / 2 - 60):
        for y in (-d / 2 + 60, d / 2 - 60):
            box(bm, x - 25, y - 25, 0, x + 25, y + 25, 80, 1)


def f_armchair(bm, w, d, h):
    f_sofa(bm, w, d, h)


def f_bed(bm, w, d, h):
    box(bm, -w / 2, -d / 2, 0, w / 2, d / 2, 300, 1)
    box(bm, -w / 2 + 25, -d / 2 + 25, 300, w / 2 - 25, d / 2 - 60, 520, 0)
    box(bm, -w / 2 - 20, d / 2 - 70, 0, w / 2 + 20, d / 2, h, 1)
    for s in ((-1, 1) if w >= 1200 else (0,)):
        cx = s * w / 4
        box(bm, cx - w * 0.2, d / 2 - 480, 520, cx + w * 0.2, d / 2 - 90, 640, 0)
    box(bm, -w / 2 + 15, -d / 2 + 15, 515, w / 2 - 15, -d / 2 + d * 0.62, 560, 2)


def f_chair(bm, w, d, h):
    seat = 450
    box(bm, -w / 2, -d / 2, seat - 40, w / 2, d / 2, seat, 0)
    for x in (-w / 2 + 25, w / 2 - 25):
        for y in (-d / 2 + 25, d / 2 - 25):
            box(bm, x - 18, y - 18, 0, x + 18, y + 18, seat - 40, 0)
    box(bm, -w / 2, d / 2 - 30, seat, w / 2, d / 2, h, 0)


def f_table(bm, w, d, h):
    box(bm, -w / 2, -d / 2, h - 40, w / 2, d / 2, h, 0)
    for x in (-w / 2 + 60, w / 2 - 60):
        for y in (-d / 2 + 60, d / 2 - 60):
            box(bm, x - 30, y - 30, 0, x + 30, y + 30, h - 40, 0)


def f_desk(bm, w, d, h):
    box(bm, -w / 2, -d / 2, h - 30, w / 2, d / 2, h, 0)
    box(bm, -w / 2, -d / 2, 0, -w / 2 + 30, d / 2, h - 30, 0)
    box(bm, w / 2 - 420, -d / 2, 0, w / 2, d / 2, h - 30, 1)
    for i in range(3):
        z = 40 + i * (h - 80) / 3
        box(bm, w / 2 - 400, -d / 2 - 8, z + 8, w / 2 - 20, -d / 2, z + (h - 80) / 3 - 8, 0)


def f_wardrobe(bm, w, d, h):
    box(bm, -w / 2, -d / 2 + 10, 0, w / 2, d / 2, h, 0)
    n = max(2, round(w / 550))
    dw = w / n
    for i in range(n):
        x0 = -w / 2 + i * dw
        box(bm, x0 + 3, -d / 2, 60, x0 + dw - 3, -d / 2 + 12, h - 20, 1)
        hx = x0 + dw - 60 if i % 2 == 0 else x0 + 60
        box(bm, hx - 8, -d / 2 - 25, h * 0.45, hx + 8, -d / 2, h * 0.6, 3)


def f_kitchen(bm, w, d, h):
    box(bm, -w / 2, -d / 2 + 50, 0, w / 2, d / 2, 100, 5)
    box(bm, -w / 2, -d / 2 + 20, 100, w / 2, d / 2, 860, 0)
    box(bm, -w / 2, -d / 2, 860, w / 2, d / 2, 900, 1)
    box(bm, -w / 2, d / 2 - 350, 1450, w / 2, d / 2, 2150, 0)
    n = max(2, round(w / 600))
    dw = w / n
    for i in range(n):
        x0 = -w / 2 + i * dw
        box(bm, x0 + 3, -d / 2 + 8, 110, x0 + dw - 3, -d / 2 + 20, 850, 0)
        box(bm, x0 + dw / 2 - 60, -d / 2 - 10, 760, x0 + dw / 2 + 60, -d / 2 + 8, 780, 3)
        box(bm, x0 + 3, d / 2 - 362, 1460, x0 + dw - 3, d / 2 - 350, 2140, 0)
    box(bm, -w * 0.15, -d / 2 + 80, 830, w * 0.15, d / 2 - 120, 880, 3)


def f_stove(bm, w, d, h):
    box(bm, -w / 2, -d / 2, 0, w / 2, d / 2, h - 30, 3)
    box(bm, -w / 2, -d / 2, h - 30, w / 2, d / 2, h, 5)
    for x in (-w / 4, w / 4):
        for y in (-d / 4, d / 4):
            cyl(bm, x, y, h, h + 6, min(w, d) / 7, 3)
    box(bm, -w / 2 + 40, -d / 2 - 5, 120, w / 2 - 40, -d / 2, h - 150, 4)


def f_sink(bm, w, d, h):
    box(bm, -w / 2, -d / 2, 0, w / 2, d / 2, h - 150, 1)
    box(bm, -w / 2, -d / 2, h - 150, w / 2, d / 2, h, 0)
    cyl(bm, 0, d / 2 - 60, h, h + 220, 16, 3)
    box(bm, -12, d / 2 - 180, h + 200, 12, d / 2 - 60, h + 220, 3)


def f_toilet(bm, w, d, h):
    box(bm, -w / 2, d / 2 - 180, 380, w / 2, d / 2, h, 0)
    cyl(bm, 0, -d * 0.1, 0, 400, w / 2 - 10, 0, sy=1.35)
    cyl(bm, 0, -d * 0.1, 400, 425, w / 2, 0, sy=1.35)


def f_bathtub(bm, w, d, h):
    t = 70
    box(bm, -w / 2, -d / 2, 0, w / 2, d / 2, 120, 0)
    box(bm, -w / 2, -d / 2, 120, w / 2, -d / 2 + t, h, 0)
    box(bm, -w / 2, d / 2 - t, 120, w / 2, d / 2, h, 0)
    box(bm, -w / 2, -d / 2, 120, -w / 2 + t, d / 2, h, 0)
    box(bm, w / 2 - t, -d / 2, 120, w / 2, d / 2, h, 0)
    cyl(bm, w / 2 - 120, d / 2 - 40, h, h + 150, 14, 3)


def f_shower(bm, w, d, h):
    box(bm, -w / 2, -d / 2, 0, w / 2, d / 2, 100, 0)
    box(bm, -w / 2, -d / 2, 100, w / 2, -d / 2 + 8, h, 4)
    box(bm, -w / 2, -d / 2, 100, -w / 2 + 8, d / 2, h, 4)
    cyl(bm, 0, d / 2 - 120, h - 200, h - 170, 90, 3)
    box(bm, -10, d / 2 - 130, h - 170, 10, d / 2, h - 150, 3)


def f_refrigerator(bm, w, d, h):
    box(bm, -w / 2, -d / 2 + 20, 0, w / 2, d / 2, h, 0)
    box(bm, -w / 2 + 3, -d / 2, 40, w / 2 - 3, -d / 2 + 20, h * 0.36, 0)
    box(bm, -w / 2 + 3, -d / 2, h * 0.37, w / 2 - 3, -d / 2 + 20, h - 10, 0)
    for z0, z1 in ((h * 0.22, h * 0.33), (h * 0.42, h * 0.62)):
        box(bm, w / 2 - 70, -d / 2 - 30, z0, w / 2 - 50, -d / 2, z1, 3)


def f_tv(bm, w, d, h):
    cab = 450
    box(bm, -w / 2, -d / 2, 0, w / 2, d / 2, cab, 1)
    sw = w * 0.92
    sh = sw * 0.565
    box(bm, -sw / 2, -20, cab + 120, sw / 2, 20, cab + 120 + sh, 5)
    box(bm, -60, -40, cab, 60, 40, cab + 15, 3)
    box(bm, -20, -15, cab + 15, 20, 15, cab + 130, 3)


def f_nightstand(bm, w, d, h):
    box(bm, -w / 2, -d / 2, 0, w / 2, d / 2, h, 0)
    box(bm, -w / 2 + 20, -d / 2 - 8, h * 0.55, w / 2 - 20, -d / 2, h - 30, 1)


def _washer_door(bm, w, d, h):
    r = min(w, h) * 0.3
    res = bmesh.ops.create_cone(bm, cap_ends=True, segments=28, radius1=r, radius2=r, depth=30)
    vs = res["verts"]
    bmesh.ops.rotate(bm, cent=Vector((0, 0, 0)), matrix=Matrix.Rotation(math.radians(90), 3, "X"), verts=vs)
    bmesh.ops.translate(bm, vec=Vector((0, -d / 2 - 15, h * 0.45)), verts=vs)
    for f in {f for v in vs for f in v.link_faces}:
        f.material_index = 4


FURNITURE = {"sofa": f_sofa, "armchair": f_armchair, "bed": f_bed, "chair": f_chair, "table": f_table, "desk": f_desk,
             "wardrobe": f_wardrobe, "kitchen": f_kitchen, "stove": f_stove, "sink": f_sink, "toilet": f_toilet,
             "bathtub": f_bathtub, "shower": f_shower, "refrigerator": f_refrigerator, "tv": f_tv,
             "nightstand": f_nightstand, "washer": None}


def furniture_object(s: dict, mats: dict, col, library: str | None):
    name = s["id"] + "_VIS"
    drop(name)
    w, d, h = (float(x) for x in s["size"])
    lib = os.path.join(library, s["kind"] + ".glb") if library else None
    if lib and os.path.exists(lib):
        before = set(bpy.data.objects)
        bpy.ops.import_scene.gltf(filepath=lib)
        new = [o for o in bpy.data.objects if o not in before and o.type == "MESH"]
        if new:
            bpy.ops.object.select_all(action="DESELECT")
            for o in new:
                o.select_set(True)
            bpy.context.view_layer.objects.active = new[0]
            if len(new) > 1:
                bpy.ops.object.join()
            ob = bpy.context.view_layer.objects.active
            ob.name = name
            for c in list(ob.users_collection):
                c.objects.unlink(ob)
            col.objects.link(ob)
            dims = ob.dimensions
            if min(dims) > 0:
                ob.scale = (w * MM / dims.x, d * MM / dims.y, h * MM / dims.z)
            src = "library"
        else:
            ob, src = None, ""
    else:
        ob, src = None, ""
    if ob is None:
        bm = bmesh.new()
        fn = FURNITURE.get(s["kind"])
        if s["kind"] == "washer":
            box(bm, -w / 2, -d / 2, 0, w / 2, d / 2, h, 0)
            _washer_door(bm, w, d, h)
        elif fn:
            fn(bm, w, d, h)
        else:
            box(bm, -w / 2, -d / 2, 0, w / 2, d / 2, h, 0)
        bmesh.ops.scale(bm, vec=Vector((MM, MM, MM)), verts=bm.verts)
        me = bpy.data.meshes.new(name)
        bm.to_mesh(me)
        bm.free()
        for slot in ("main", "secondary", "accent", "metal", "glass", "dark"):
            me.materials.append(mats[slot])
        ob = bpy.data.objects.new(name, me)
        col.objects.link(ob)
        src = "procedural"
    ob.location = (s["pos"][0] * MM, s["pos"][1] * MM, float(s.get("z", 0)) * MM)
    ob.rotation_euler = (0, 0, math.radians(float(s.get("rotation", 0))))
    ob["jarvis_id"], ob["jarvis_type"], ob["level"] = s["id"], "furniture", s.get("level", "")
    ob["kind"], ob["name"], ob["model"] = s["kind"], s.get("label", ""), src
    return ob


# ----------------------------------------------------------------------------- lights, cameras

def scene_setup(s: dict):
    bb = s.get("bbox") or [0, 0, 10000, 10000]
    cx, cy = (bb[0] + bb[2]) / 2 * MM, (bb[1] + bb[3]) / 2 * MM
    size = max(bb[2] - bb[0], bb[3] - bb[1]) * MM
    top = float(s.get("top", 3000)) * MM
    col = collection("SCENE", "scene")
    for name in [o.name for o in col.objects]:
        drop(name)
    sun = bpy.data.lights.new("Sun", "SUN")
    sun.energy = 2.2
    sun.angle = math.radians(3)
    so = bpy.data.objects.new("SUN_VIS", sun)
    so.rotation_euler = (math.radians(42), math.radians(8), math.radians(35))
    col.objects.link(so)
    for r in s.get("rooms", []):
        ar = bpy.data.lights.new("L_" + r["id"], "AREA")
        ar.shape = "RECTANGLE"
        ar.size, ar.size_y = max(0.6, r["w"] * MM * 0.5), max(0.6, r["d"] * MM * 0.5)
        ar.energy = max(40.0, r["area"] * 7)
        lo = bpy.data.objects.new("LIGHT_" + r["id"], ar)
        lo.location = (r["c"][0] * MM, r["c"][1] * MM, (r["z"] + r["h"]) * MM - 0.05)
        col.objects.link(lo)
    cam = bpy.data.cameras.new("Top")
    cam.type = "ORTHO"
    cam.ortho_scale = size * 1.15
    co = bpy.data.objects.new("CAM_TOP", cam)
    co.location = (cx, cy, top + 30)
    col.objects.link(co)
    cam2 = bpy.data.cameras.new("Persp")
    cam2.lens = 28
    c2 = bpy.data.objects.new("CAM_PERSP", cam2)
    c2.location = (cx - size * 0.9, cy - size * 1.1, size * 0.85)
    look(c2, (cx, cy, 0.8))
    col.objects.link(c2)
    bpy.context.scene.camera = c2


def look(ob, target):
    d = Vector(target) - ob.location
    ob.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()


# ----------------------------------------------------------------------------- ops

def op_ping():
    return {"blender": bpy.app.version_string, "python": sys.version.split()[0]}


def op_sync(blend: str, mesh_dir: str, specs: dict, removed: list, materials: dict, fresh: bool = False,
            glb: str | None = None, library: str | None = None) -> dict:
    t0 = time.time()
    open_blend(blend, fresh)
    mats = {mid: material(mid, m) for mid, m in materials.items()}
    for sid in removed:
        for suffix in ("_VIS", "_FLOOR_VIS", "_CEIL_VIS"):
            drop(sid + suffix)
        drop("LIGHT_" + sid)
    made = []
    for sid, s in specs.items():
        kind = s["type"]
        if kind == "scene":
            scene_setup(s)
            continue
        col = collection(s.get("level") or "LEVEL_000", kind)
        props = {"jarvis_id": sid, "jarvis_type": kind, "level": s.get("level", ""), "name": s.get("label", "")}
        if kind == "furniture":
            fm = {slot: mats[s["materials"][slot]] for slot in s["materials"]}
            furniture_object(s, fm, col, library)
            made.append(sid)
            continue
        path = os.path.join(mesh_dir, sid + ".json")
        if not os.path.exists(path):
            continue
        with open(path) as f:
            data = json.load(f)
        parts = data.get("parts", [])
        if kind == "room":
            fl = [mats[s["floor"]]]
            for p in parts:
                p["mat"] = 0
            mesh_object(sid + "_FLOOR_VIS", parts, fl, col, {**props, "jarvis_type": "floor"}, z_offset=0.0)
            ceil_parts = [{"v": p["v"], "f": [p["f"][i + k] for i in range(0, len(p["f"]), 3) for k in (0, 2, 1)],
                           "mat": 0} for p in parts]
            mesh_object(sid + "_CEIL_VIS", ceil_parts, [mats[s["ceiling"]]], col, {**props, "jarvis_type": "ceiling"},
                        z_offset=(float(s["height"]) - 10) * MM)
            made.append(sid)
            continue
        roles = s.get("roles") or {"main": s.get("material")}
        order = list(roles)
        slot = {r: i for i, r in enumerate(order)}
        for p in parts:
            p["mat"] = slot.get(p.get("role"), 0)
        mesh_object(sid + "_VIS", parts, [mats[roles[r]] for r in order], col, props)
        made.append(sid)
    bpy.ops.wm.save_as_mainfile(filepath=blend, compress=True)
    out = {"objects": len([o for o in bpy.data.objects if o.get("jarvis_id")]), "updated": made,
           "saved": blend, "ms": round((time.time() - t0) * 1000)}
    if glb:
        out["glb"] = export_glb(glb)
    return out


def export_glb(path: str) -> dict:
    t0 = time.time()
    pattern_links(False)
    try:
        bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", export_extras=True, export_apply=True,
                                  export_lights=False, export_cameras=False, use_visible=False)
    finally:
        pattern_links(True)
    return {"path": path, "size": os.path.getsize(path), "ms": round((time.time() - t0) * 1000)}


def op_export(blend: str, out_dir: str, formats: list, name: str = "building") -> dict:
    open_blend(blend, False)
    os.makedirs(out_dir, exist_ok=True)
    res = {}
    for fmt in formats:
        try:
            if fmt == "glb":
                res[fmt] = export_glb(os.path.join(out_dir, name + ".glb"))
            elif fmt == "gltf":
                p = os.path.join(out_dir, name + ".gltf")
                pattern_links(False)
                try:
                    bpy.ops.export_scene.gltf(filepath=p, export_format="GLTF_SEPARATE", export_extras=True,
                                              export_lights=False, export_cameras=False)
                finally:
                    pattern_links(True)
                res[fmt] = {"path": p, "size": os.path.getsize(p)}
            elif fmt == "obj":
                p = os.path.join(out_dir, name + ".obj")
                bpy.ops.wm.obj_export(filepath=p, export_materials=True, export_selected_objects=False,
                                      forward_axis="NEGATIVE_Z", up_axis="Y")
                res[fmt] = {"path": p, "size": os.path.getsize(p)}
            elif fmt == "blend":
                p = os.path.join(out_dir, name + ".blend")
                bpy.ops.wm.save_as_mainfile(filepath=p, copy=True, compress=True)
                res[fmt] = {"path": p, "size": os.path.getsize(p)}
            elif fmt == "fbx":
                p = os.path.join(out_dir, name + ".fbx")
                bpy.ops.export_scene.fbx(filepath=p, use_selection=False, apply_unit_scale=True)
                res[fmt] = {"path": p, "size": os.path.getsize(p)}
            else:
                res[fmt] = {"error": "формат не поддерживается Blender-исполнителем"}
        except Exception as e:          # noqa: BLE001
            res[fmt] = {"error": f"{e.__class__.__name__}: {e}"[:300]}
    return res


def op_render(blend: str, out: str, view: str = "persp", room: dict | None = None, engine: str = "eevee",
              samples: int = 32, res: list | None = None) -> dict:
    """A picture of the model: from above (no roof, no ceilings), in perspective, or inside a room at eye height."""
    t0 = time.time()
    open_blend(blend, False)
    sc = bpy.context.scene
    hide = []
    for ob in bpy.data.objects:
        t = ob.get("jarvis_type", "")
        if (view == "top" and t in ("roof", "ceiling")) or (view == "persp" and t == "ceiling") or \
                (view == "room" and t == "roof"):
            if not ob.hide_render:
                ob.hide_render = True
                hide.append(ob)
    cam = None
    if view == "top":
        cam = bpy.data.objects.get("CAM_TOP")
    elif view == "room" and room:
        cd = bpy.data.cameras.new("Room")
        cd.lens = 16
        cam = bpy.data.objects.new("CAM_ROOM_TMP", cd)
        bpy.context.scene.collection.objects.link(cam)
        cam.location = (room["eye"][0] * MM, room["eye"][1] * MM, (room["z"] + 1600) * MM)
        look(cam, (room["c"][0] * MM, room["c"][1] * MM, (room["z"] + 1100) * MM))
    else:
        cam = bpy.data.objects.get("CAM_PERSP")
    if cam is None:
        raise RuntimeError("В сцене нет камеры — сначала постройте модель")
    sc.camera = cam
    sc.render.engine = {"eevee": "BLENDER_EEVEE", "cycles": "CYCLES", "workbench": "BLENDER_WORKBENCH"}.get(engine, "BLENDER_EEVEE")
    if sc.render.engine == "CYCLES":
        sc.cycles.samples = int(samples)
        sc.cycles.device = "CPU"
        sc.cycles.use_denoising = True
    elif sc.render.engine == "BLENDER_EEVEE":
        sc.eevee.taa_render_samples = int(samples)
    else:
        sc.display.shading.light = "STUDIO"
        sc.display.shading.color_type = "MATERIAL"
    w, h = res or (1280, 800)
    sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage = int(w), int(h), 100
    sc.render.filepath = out
    sc.render.image_settings.file_format = "PNG"
    try:
        bpy.ops.render.render(write_still=True)
    finally:
        for ob in hide:
            ob.hide_render = False
        if view == "room" and cam is not None:
            bpy.data.objects.remove(cam, do_unlink=True)
    return {"path": out, "size": os.path.getsize(out), "engine": sc.render.engine, "ms": round((time.time() - t0) * 1000)}


def op_inspect(blend: str) -> dict:
    open_blend(blend, False)
    items = [{"name": o.name, "id": o.get("jarvis_id"), "type": o.get("jarvis_type"), "level": o.get("level"),
              "materials": [m.name for m in getattr(o.data, "materials", [])] if o.type == "MESH" else []}
             for o in bpy.data.objects if o.get("jarvis_id")]
    return {"objects": items, "count": len(items)}


OPS = {"ping": op_ping, "sync": op_sync, "export": op_export, "render": op_render, "inspect": op_inspect}


def main():
    global CUR
    reply({"ready": True, "pid": os.getpid(), "blender": bpy.app.version_string})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        CUR = req.get("id")
        try:
            res = OPS[req["op"]](**(req.get("args") or {}))
            reply({"id": CUR, "ok": True, "result": res})
        except Exception as e:          # noqa: BLE001
            reply({"id": CUR, "ok": False, "error": f"{e.__class__.__name__}: {e}"[:600],
                   "trace": traceback.format_exc()[-2000:]})
        CUR = None


main()
