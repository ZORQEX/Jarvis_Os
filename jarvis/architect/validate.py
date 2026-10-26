"""Checking a model before (and after) it becomes geometry: what is wrong (errors), doubtful (warnings), what only the
person can answer (questions; «blocking» ones stop the 3D build), and what was repaired on the way (fixed).

Repairs are small and safe: almost-straight walls made straight, duplicate and too-short walls dropped, openings
moved back inside their wall. Anything bigger becomes a question, never a guess."""
from __future__ import annotations

from . import geometry as g
from .model import ArchModel, ModelError, pt

LOW = 0.5          # below this an element is «не уверен»
SCALE_OK = 0.6     # below this the scale is a blocking question


def straighten(m: ArchModel, fixed: list) -> None:
    """Walls within 2.5° of horizontal / vertical become exactly so (drawings and sketches are rarely skewed on
    purpose); joined ends move together."""
    for w in m.data["walls"]:
        a, b = pt(w["start"]), pt(w["end"])
        v = g.sub(b, a)
        ang = abs(g.angle_deg(v)) % 180
        if 0 < min(ang, 180 - ang) < 2.5 and abs(v[1]) > 0.5:
            y = (a[1] + b[1]) / 2
            _move_end(m, w, "start", (a[0], y))
            _move_end(m, w, "end", (b[0], y))
            fixed.append(f"{w['id']}: выпрямлена (горизонталь)")
        elif 0 < abs(ang - 90) < 2.5 and abs(v[0]) > 0.5:
            x = (a[0] + b[0]) / 2
            _move_end(m, w, "start", (x, a[1]))
            _move_end(m, w, "end", (x, b[1]))
            fixed.append(f"{w['id']}: выпрямлена (вертикаль)")


def _move_end(m: ArchModel, w: dict, key: str, p: g.Pt) -> None:
    old = pt(w[key])
    for v in m.data["walls"]:
        for k in ("start", "end"):
            if g.dist(pt(v[k]), old) < 1:
                v[k] = g.rnd(p)


def dedupe(m: ArchModel, fixed: list) -> None:
    """Two walls on the same line covering the same stretch → one (the thicker / more certain survives)."""
    walls = m.data["walls"]
    gone = set()
    for i, a in enumerate(walls):
        for b in walls[i + 1:]:
            if a["id"] in gone or b["id"] in gone or a.get("level") != b.get("level"):
                continue
            pa, pb = (pt(a["start"]), pt(a["end"])), (pt(b["start"]), pt(b["end"]))
            if not g.collinear(*pa, *pb, ang_tol=3, off_tol=max(a["thickness"], b["thickness"]) / 2):
                continue
            La, Lb = g.dist(*pa), g.dist(*pb)
            t0 = g.line_point_t(pb[0], *pa) * La
            t1 = g.line_point_t(pb[1], *pa) * La
            lo, hi = sorted((t0, t1))
            overlap = min(hi, La) - max(lo, 0)
            if overlap < 0.8 * min(La, Lb):
                continue
            keep, drop = (a, b) if (a["thickness"], a.get("confidence", 0), La) >= (b["thickness"], b.get("confidence", 0), Lb) \
                else (b, a)
            # extend the survivor to cover both
            u = g.unit(g.sub(pt(keep["end"]), pt(keep["start"])))
            s = pt(keep["start"])
            ts = [0.0, g.dist(pt(keep["start"]), pt(keep["end"]))] + \
                 [g.dot(g.sub(pt(drop[k]), s), u) for k in ("start", "end")]
            keep["start"], keep["end"] = g.rnd(g.add(s, g.mul(u, min(ts)))), g.rnd(g.add(s, g.mul(u, max(ts))))
            for o in m.hosted(drop["id"]):
                c = m.opening_center(o)
                o["host"] = keep["id"]
                o["offset"] = round(g.dot(g.sub(c, pt(keep["start"])), u), 1)
            gone.add(drop["id"])
            fixed.append(f"{drop['id']}: дубль {keep['id']} — объединены")
    m.data["walls"] = [w for w in walls if w["id"] not in gone]


def validate(m: ArchModel, repair: bool = True) -> dict:
    rep: dict = {"errors": [], "warnings": [], "questions": [], "fixed": []}
    d = m.data
    if repair:
        straighten(m, rep["fixed"])
        dedupe(m, rep["fixed"])
        short = [w for w in d["walls"] if m.wall_len(w) < 150]
        for w in short:
            m.op_delete_wall(w["id"])
            rep["fixed"].append(f"{w['id']}: короче 15 см — убрана")
        m.derive()
    if not d["walls"]:
        rep["errors"].append("Стены не найдены — модель строить не из чего")
        return rep
    for w in d["walls"]:
        t, h = float(w["thickness"]), float(w["height"])
        if not 50 <= t <= 1000:
            rep["errors"].append(f"{w['id']}: толщина {t:.0f} мм вне 50…1000 мм")
        if not 1000 <= h <= 15000:
            rep["errors"].append(f"{w['id']}: высота {h:.0f} мм вне 1…15 м")
        if float(w.get("confidence", 1)) < LOW:
            rep["warnings"].append(f"{w['id']}: не уверен, что это стена (уверенность {float(w['confidence']):.2f})")
        if w.get("thickness_source") == "default" and w.get("kind") == "exterior":
            rep["warnings"].append(f"{w['id']}: толщина не видна на чертеже — взято {t / 10:g} см")
    if not d["rooms"]:
        rep["errors"].append("Не найден замкнутый контур стен: ни одного помещения")
    for r in d["rooms"]:
        if r["area_m2"] < 1.0:
            rep["warnings"].append(f"{r['id']}: очень маленькое помещение ({r['area_m2']} м²)")
        drawn = r.get("area_on_drawing_m2")
        if drawn and r["area_m2"] and abs(drawn - r["area_m2"]) / drawn > 0.25:
            rep["warnings"].append(f"{r['id']} «{r['name']}»: по модели {r['area_m2']} м², на чертеже {drawn} м²")
    for o in m.openings():
        host = m.get(o.get("host"))
        if host is None:
            rep["errors"].append(f"{o['id']}: нет стены-основы {o.get('host')}")
            continue
        try:
            before = (o["offset"], o["height"])
            m.fit_opening(o)
            if (o["offset"], o["height"]) != before:
                rep["fixed"].append(f"{o['id']}: возвращён в пределы стены {host['id']}")
        except ModelError as e:
            rep["errors"].append(str(e))
        if float(o.get("confidence", 1)) < LOW:
            what = "дверь" if o["type"] == "door" else "окно"
            rep["questions"].append({"id": "Q_" + o["id"], "kind": "confirm", "target": o["id"], "blocking": False,
                                     "text": f"Не уверен, что {o['id']} — {what} (уверенность {float(o['confidence']):.2f}). "
                                             "Оставить?"})
    by_host: dict[str, list] = {}
    for o in m.openings():
        by_host.setdefault(o.get("host"), []).append(o)
    for wid, ops in by_host.items():
        ops.sort(key=lambda o: o["offset"])
        for a, b in zip(ops, ops[1:]):
            if a["offset"] + a["width"] / 2 > b["offset"] - b["width"] / 2 - 50:
                rep["warnings"].append(f"{a['id']} и {b['id']} в стене {wid} накладываются")
    for f in d["furniture"]:
        if not f.get("room"):
            rep["warnings"].append(f"{f['id']} ({f['name']}) стоит вне помещений")
    src = d.get("source") or {}
    if src.get("kind") in ("image", "pdf") and float(src.get("scale_confidence") or 0) < SCALE_OK:
        mm = float(src.get("mm_per_px") or 0)
        how = src.get("scale_note") or "предположение"
        rep["questions"].append({
            "id": "Q_SCALE", "kind": "scale", "blocking": True, "default": mm,
            "text": (f"Масштаб на чертеже не найден. Использовать предполагаемый: 1 пиксель = {mm:.1f} мм ({how})? "
                     "Скажите «да» или назовите любой размер, например «ширина дома 10 метров» или "
                     "«стена WALL_001 — 6 метров»." if mm else
                     "Масштаб на чертеже не найден. Назовите любой размер, например «ширина дома 10 метров».")})
    for q in d.get("questions", []):
        if q.get("kind") in ("custom",) and q["id"] not in {x["id"] for x in rep["questions"]}:
            rep["questions"].append(q)
    return rep
