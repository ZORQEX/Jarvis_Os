"""Structured floor plan → Master Architectural Model.

The structured plan (analysis/floorplan.json, schema jarvis.floorplan/1) is the step between a drawing and the model:
everything the analysis found, already in millimetres, each with a confidence and where it came from — but no ids,
no rooms-from-walls, no checks yet. Both the image path (vision.py: OpenCV + Claude) and the DXF path (exact CAD
lines) produce it, so the model is always built the same way and always validated."""
from __future__ import annotations

from . import geometry as g
from .catalog import FURNITURE, room_kind
from .model import ArchModel, ModelError, new_model, pt
from .validate import validate

PLAN_SCHEMA = "jarvis.floorplan/1"


def check_plan(plan: dict) -> list[str]:
    """Strict shape check of a structured plan (the analysis output is data from outside: nothing is trusted)."""
    errs = []
    if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA:
        return ["не тот формат плана"]
    if plan.get("units") != "mm":
        errs.append("единицы плана должны быть мм")

    def num(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool) and abs(x) < 1e7

    def point(p):
        return isinstance(p, (list, tuple)) and len(p) == 2 and all(num(v) for v in p)

    for i, w in enumerate(plan.get("walls") or []):
        if not (isinstance(w, dict) and point(w.get("start")) and point(w.get("end"))):
            errs.append(f"стена {i}: нет начала или конца")
        elif w.get("thickness") is not None and not num(w["thickness"]):
            errs.append(f"стена {i}: толщина не число")
    for key, need in (("openings", "center"), ("rooms", "point"), ("columns", "center"), ("furniture", "center"),
                      ("stairs", "start")):
        for i, e in enumerate(plan.get(key) or []):
            if not (isinstance(e, dict) and point(e.get(need))):
                errs.append(f"{key} {i}: нет координат")
    for key in ("walls", "openings", "rooms", "stairs", "columns", "furniture"):
        for e in plan.get(key) or []:
            c = e.get("confidence", 1) if isinstance(e, dict) else 1
            if not num(c) or not 0 <= c <= 1:
                errs.append(f"{key}: уверенность вне 0…1")
                break
    return errs


def model_from_plan(plan: dict, name: str = "Проект") -> tuple[ArchModel, dict]:
    errs = check_plan(plan)
    if errs:
        raise ModelError("План не прошёл проверку: " + "; ".join(errs[:5]))
    m = ArchModel(new_model(name))
    d = m.data
    d["source"] = dict(plan.get("source") or {})
    dft = d["defaults"]
    ids: dict[str, str] = {}
    for w in plan.get("walls") or []:
        kind = w.get("kind") if w.get("kind") in ("exterior", "interior", "partition") else None
        t = w.get("thickness")
        src = w.get("thickness_source") or ("drawing" if t else "default")
        if not t:
            t = dft["exterior_thickness"] if kind == "exterior" else dft["interior_thickness"]
        t = min(800.0, max(60.0, float(t)))
        try:
            nw = m.add_wall(w["start"], w["end"], t, None, kind, None, float(w.get("confidence", 0.8)),
                            w.get("source", "vision"))
        except ModelError:
            continue
        nw["thickness_source"] = src
        if w.get("id"):
            ids[str(w["id"])] = nw["id"]
    m.derive()
    # walls drawn without a known kind: thickness by what they are after rooms are known
    for w in d["walls"]:
        if w.get("thickness_source") == "default" and w.get("kind") == "exterior" \
                and w["thickness"] < dft["exterior_thickness"]:
            w["thickness"] = dft["exterior_thickness"]
    lost = m.assign_labels([{"name": str(r.get("name") or "")[:60] or "Помещение", "kind": r.get("kind") or room_kind(str(r.get("name") or "")),
                             "point": r["point"], "confidence": float(r.get("confidence", 0.7)),
                             "area_m2": r.get("area_m2"), "source": r.get("source", "vision")}
                            for r in plan.get("rooms") or []])
    report_notes = [f"Подпись «{x}» не попала ни в одно помещение" for x in lost if x]
    for o in plan.get("openings") or []:
        kind = o.get("kind") if o.get("kind") in ("door", "window", "opening") else "door"
        c = pt(o["center"])
        host = d_get(m, ids.get(str(o.get("wall"))) if o.get("wall") else None)
        if host is None:
            best = None
            for w in d["walls"]:
                dd, t = g.seg_point(c, pt(w["start"]), pt(w["end"]))
                if dd <= float(w["thickness"]) / 2 + 250 and (best is None or dd < best[0]):
                    best = (dd, w)
            host = best[1] if best else None
        if host is None:
            report_notes.append(f"{'Дверь' if kind != 'window' else 'Окно'} у точки {g.rnd(c, 0)} не стоит ни в одной стене — пропущено")
            continue
        a, b = pt(host["start"]), pt(host["end"])
        off = g.dot(g.sub(c, a), g.unit(g.sub(b, a)))
        width = o.get("width")
        try:
            m.add_opening(kind, host["id"], off, float(width) if width else None, o.get("height"), o.get("sill"),
                          o.get("swing") if o.get("swing") in ("left", "right", "double", "sliding") else None,
                          float(o.get("confidence", 0.7)), o.get("source", "vision"))
        except ModelError as e:
            report_notes.append(str(e))
    for s in plan.get("stairs") or []:
        try:
            m.op_add_stair(s["start"], float(s.get("direction", 0)), float(s.get("width") or 1000),
                           float(s.get("length") or 3000), s.get("steps"))
            d["stairs"][-1].update({"confidence": float(s.get("confidence", 0.6)), "source": s.get("source", "vision")})
        except (ModelError, ValueError, TypeError):
            pass
    for c in plan.get("columns") or []:
        m.op_add_column(c["center"], float(c.get("width") or 300), float(c.get("depth") or c.get("width") or 300),
                        shape=c.get("shape") if c.get("shape") in ("rect", "round") else "rect")
        d["columns"][-1].update({"confidence": float(c.get("confidence", 0.6)), "source": c.get("source", "vision")})
    m.derive()
    for f in plan.get("furniture") or []:
        kind = f.get("kind")
        if kind not in FURNITURE:
            continue
        size = list(FURNITURE[kind]["size"])
        if f.get("width"):
            size[0] = float(f["width"])
        if f.get("depth"):
            size[1] = float(f["depth"])
        item = m.add_furniture(kind, f["center"], float(f.get("rotation") or 0), size, None, None, None, None,
                               float(f.get("confidence", 0.6)), f.get("source", "vision"))
        item["room"] = m.room_at(pt(item["position"]))
    m.derive()
    rep = validate(m)
    rep["warnings"] = report_notes + list(plan.get("warnings") or []) + rep["warnings"]
    d["questions"] = rep["questions"]
    d["warnings"] = rep["warnings"]
    return m, rep


def d_get(m: ArchModel, oid: str | None) -> dict | None:
    return m.get(oid) if oid else None
