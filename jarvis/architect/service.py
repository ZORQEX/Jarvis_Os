"""JARVIS Architect — the orchestrator. One project folder per building; one master model per project; the engines
build from it and never keep a version of their own.

    SOURCE → 2D ANALYSIS → STRUCTURED PLAN → VALIDATION → MASTER MODEL → CAD (FreeCAD/OCCT) → 3D (Blender) → PREVIEW
           → EDIT (commands, incremental) → EXPORT

Tool routing (best tool for each job, chosen here, never by the person): recognition → OpenCV + OCR + Claude
Vision; exact geometry, booleans, checks → FreeCAD on OCCT; look, materials, furniture, renders, GLB/OBJ/.blend →
Blender; FCStd/STEP/STL/BREP → FreeCAD; DXF plan → ezdxf. Pascal Editor is used only if installed (it is not on this
computer; see engines()).

Every operation is recorded (op id, JARVIS task id, operation, status, start, end, error, affected objects) in the
project's log/operations.jsonl and published as `architect.op`; the window's «Архитектура» shows them."""
from __future__ import annotations

import asyncio
import copy
import json
import re
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from . import commands as cmds
from .model import ArchModel, ModelError, pt
from .plan import model_from_plan
from .specs import cad_specs, diff, used_materials, vis_specs
from .validate import validate
from .vision import SCHEMA, SYSTEM, fuse, plan_from_dxf, prompt
from .workers import engines, make_workers

INPUTS = {".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".bmp": "image", ".tif": "image",
          ".tiff": "image", ".pdf": "pdf", ".dxf": "dxf"}
NOT_YET = {".dwg": "DWG — закрытый формат Autodesk: сохраните план как DXF или PDF (в любой CAD-программе: «Сохранить как»)"}
STAGES = ("source", "analysis", "plan", "model", "cad", "preview", "edit", "export")
EXPORTS = {"glb": "blender", "gltf": "blender", "obj": "blender", "blend": "blender", "fbx": "blender",
           "step": "freecad", "stl": "freecad", "brep": "freecad", "fcstd": "freecad", "dxf": "cv", "json": "core"}


class ArchitectError(RuntimeError):
    """Something the person must hear about (plain Russian)."""


def slugify(name: str) -> str:
    tr = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя", ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l",
                                                        "m", "n", "o", "p", "r", "s", "t", "u", "f", "h", "c", "ch", "sh",
                                                        "sch", "", "y", "", "e", "yu", "ya"]))
    s = "".join(tr.get(c, c) for c in name.casefold())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s or "plan")[:40]


class Architect:
    def __init__(self, config, paths, db, bus, audit, claude=None):
        self.config, self.paths, self.db, self.bus, self.audit = config, paths, db, bus, audit
        self.claude = claude
        self.root = config.expand(getattr(config, "architect_dir", "") or str(Path(config.write_dir) / "Архитектура"))
        self.workers = make_workers()
        self.locks: dict[str, asyncio.Lock] = {}
        self.recent_ops: list[dict] = []
        self.last_activity = 0.0
        self.library = str(self.root / "_library")       # <kind>.glb furniture models, if the person adds them

    # ------------------------------------------------------------ projects
    def project_dir(self, slug: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9\-]{0,60}", slug or ""):
            raise ArchitectError("Неверное имя проекта")
        d = (self.root / slug).resolve()
        if not d.is_relative_to(self.root.resolve()):
            raise ArchitectError("Проект вне папки архитектуры")
        return d

    def projects(self) -> list[dict]:
        out = []
        if not self.root.exists():
            return out
        for d in sorted(self.root.iterdir(), key=lambda p: -p.stat().st_mtime):
            meta = d / "project.json"
            if d.is_dir() and meta.exists():
                try:
                    m = json.loads(meta.read_text("utf-8"))
                except ValueError:
                    continue
                out.append({"id": d.name, "name": m.get("name") or d.name, "created_at": m.get("created_at"),
                            "updated_at": d.stat().st_mtime, "source": m.get("source_name"),
                            "has_model": (d / "model" / "architectural_model.json").exists()})
        return out

    def active(self) -> str | None:
        a = self.db.get_kv("architect_active")
        if a and (self.root / a / "project.json").exists():
            return a
        p = self.projects()
        return p[0]["id"] if p else None

    def set_active(self, slug: str) -> None:
        self.project_dir(slug)
        self.db.set_kv("architect_active", slug)

    def meta(self, slug: str) -> dict:
        try:
            return json.loads((self.project_dir(slug) / "project.json").read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def save_meta(self, slug: str, m: dict) -> None:
        f = self.project_dir(slug) / "project.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(f)

    def stage(self, slug: str, name: str, state: str, detail: str = "") -> None:
        m = self.meta(slug)
        p = m.setdefault("pipeline", {s: {"state": "idle"} for s in STAGES})
        p[name] = {"state": state, "detail": detail[:300], "at": time.time()}
        self.save_meta(slug, m)
        self.bus.publish("architect.stage", {"project": slug, "stage": name, "state": state, "detail": detail[:300]})

    def new_project(self, source: Path, name: str | None = None) -> str:
        ext = source.suffix.lower()
        if ext in NOT_YET:
            raise ArchitectError(NOT_YET[ext])
        if ext not in INPUTS:
            raise ArchitectError(f"Формат {ext or 'без расширения'} не поддерживается: подойдут PNG, JPG, WEBP, PDF, DXF")
        base = slugify(name or source.stem)
        slug = base
        n = 2
        while (self.root / slug).exists():
            slug = f"{base}-{n}"
            n += 1
        d = self.project_dir(slug)
        for sub in ("source", "analysis", "model/history", "cad/mesh", "blender", "previews", "exports", "log"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        dst = d / "source" / ("plan" + ext)
        shutil.copy2(source, dst)
        self.save_meta(slug, {"name": name or source.stem, "created_at": time.time(), "source": str(dst.relative_to(d)),
                              "source_name": source.name, "source_kind": INPUTS[ext],
                              "pipeline": {s: {"state": "idle"} for s in STAGES}})
        self.stage(slug, "source", "done", source.name)
        self.set_active(slug)
        return slug

    def load(self, slug: str) -> ArchModel | None:
        f = self.project_dir(slug) / "model" / "architectural_model.json"
        if not f.exists():
            return None
        return ArchModel(json.loads(f.read_text("utf-8")))

    def save(self, slug: str, m: ArchModel, note: str = "") -> int:
        d = self.project_dir(slug)
        rev = int(m.data.get("revision", 0))
        raw = json.dumps(m.data, ensure_ascii=False, indent=1)
        f = d / "model" / "architectural_model.json"
        tmp = f.with_suffix(".tmp")
        tmp.write_text(raw, "utf-8")
        tmp.replace(f)
        (d / "model" / "history").mkdir(parents=True, exist_ok=True)
        (d / "model" / "history" / f"rev_{rev:04d}.json").write_text(raw, "utf-8")
        hist = d / "model" / "history.jsonl"
        with hist.open("a", encoding="utf-8") as h:
            h.write(json.dumps({"rev": rev, "at": time.time(), "note": note[:300]}, ensure_ascii=False) + "\n")
        return rev

    def lock(self, slug: str) -> asyncio.Lock:
        return self.locks.setdefault(slug, asyncio.Lock())

    # ------------------------------------------------------------ observability
    @asynccontextmanager
    async def op(self, slug: str | None, operation: str, task_id: str | None = None):
        rec = {"op_id": "op_" + uuid.uuid4().hex[:10], "task_id": task_id, "project": slug, "operation": operation,
               "status": "running", "started_at": time.time(), "ended_at": None, "error": None, "affected": [],
               "detail": ""}
        self._publish_op(rec)
        try:
            yield rec
            rec["status"] = rec.get("status_final") or "done"
        except Exception as e:
            rec["status"], rec["error"] = "failed", str(e)[:500]
            raise
        finally:
            rec["ended_at"] = time.time()
            rec["ms"] = round((rec["ended_at"] - rec["started_at"]) * 1000)
            rec.pop("status_final", None)
            self._publish_op(rec)
            if slug:
                try:
                    with (self.project_dir(slug) / "log" / "operations.jsonl").open("a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                except OSError:
                    pass
            self.audit.write("architect." + rec["status"], project=slug, operation=operation, task_id=task_id,
                             ms=rec["ms"], error=rec["error"], affected=rec["affected"][:30])
            self.last_activity = time.time()

    def _publish_op(self, rec: dict) -> None:
        self.recent_ops = [r for r in self.recent_ops if r["op_id"] != rec["op_id"]][-80:] + [dict(rec)]
        self.bus.publish("architect.op", {"op": dict(rec)}, rec.get("task_id"))

    def ops_log(self, slug: str, limit: int = 60) -> list[dict]:
        f = self.project_dir(slug) / "log" / "operations.jsonl"
        if not f.exists():
            return []
        lines = f.read_text("utf-8").splitlines()[-limit:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out

    # ------------------------------------------------------------ analysis
    def use_cloud(self) -> tuple[bool, str]:
        prof = self.db.get_kv("privacy_profile") or self.config.privacy_profile
        if prof != "AUTHORIZED_CLOUD":
            return False, "режим приватности: без облака"
        c = self.claude
        if c is None or not c.configured():
            return False, "Claude Code не подключён"
        tr = c.trouble_now() if hasattr(c, "trouble_now") else None
        if tr is not None:
            return False, getattr(tr, "text", "Claude сейчас недоступен")
        return True, ""

    async def analyze(self, slug: str, progress=lambda m, f=None: None, task_id: str | None = None) -> dict:
        """Source → structured floor plan (analysis/floorplan.json)."""
        d = self.project_dir(slug)
        meta = self.meta(slug)
        src = d / meta["source"]
        kind = meta.get("source_kind")
        an = d / "analysis"
        an.mkdir(exist_ok=True)
        cv = self.workers["cv"]
        async with self.op(slug, "analyze_plan", task_id) as rec:
            self.stage(slug, "analysis", "running", "распознаю чертёж")
            if kind == "dxf":
                progress("Читаю DXF", 0.2)
                dx = await cv.call("dxf_walls", {"path": str(src)}, timeout=120)
                plan = plan_from_dxf(dx, {"file": meta["source"], "kind": "dxf"})
                rec["detail"] = f"DXF: стен {len(plan['walls'])}, дверей {len(plan['openings'])}, подписей {len(plan['rooms'])}"
            else:
                progress("Готовлю изображение", 0.05)
                raster = an / "source.png"
                r = await cv.call("raster", {"path": str(src), "out": str(raster), "page": 1}, timeout=120)
                progress("Ищу стены (OpenCV)", 0.15)
                walls = await cv.call("walls", {"path": str(raster), "annotated": str(an / "annotated.png")}, timeout=180)
                (an / "cv.json").write_text(json.dumps(walls, ensure_ascii=False, indent=1), "utf-8")
                progress("Читаю подписи (OCR)", 0.3)
                ocr = await cv.call("ocr", {"path": str(raster), "walls": walls["walls"]}, timeout=180)
                (an / "ocr.json").write_text(json.dumps(ocr, ensure_ascii=False, indent=1), "utf-8")
                ai, k, why_not = None, 1.0, ""
                ok, why_not = self.use_cloud()
                if ok:
                    progress("Claude разбирает план", 0.4)
                    try:
                        fit = await cv.call("fit", {"path": str(raster), "out": str(an / "vision_in.png")}, timeout=60)
                        k = float(fit["k"])
                        ai = await self.claude.vision(SYSTEM, prompt(walls, ocr.get("tokens", []), fit["w"], fit["h"], k),
                                                      [an / "vision_in.png", an / "annotated.png"], SCHEMA,
                                                      budget=float(getattr(self.config, "architect_vision_timeout_s", 300)))
                        (an / "vision.json").write_text(json.dumps(ai, ensure_ascii=False, indent=1), "utf-8")
                    except Exception as e:   # noqa: BLE001 — local analysis still works; the reason is said
                        why_not = f"Claude не разобрал чертёж ({str(e)[:160]}) — распознал локально"
                        ai = None
                plan = fuse(walls, ocr.get("tokens", []), ai, k, {"file": meta["source"], "kind": kind,
                                                                    "pages": r.get("pages", 1)})
                if why_not:
                    plan.setdefault("warnings", []).append("Без Claude: " + why_not)
                rec["detail"] = (f"стен {len(plan.get('walls', []))}, помещений {len(plan.get('rooms', []))}, "
                                 f"проёмов {len(plan.get('openings', []))}, "
                                 f"масштаб {plan.get('source', {}).get('scale_note', '')}")
            (an / "floorplan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), "utf-8")
            self.stage(slug, "analysis", "done", rec["detail"])
            if plan.get("not_a_plan"):
                self.stage(slug, "plan", "error", "на изображении нет плана")
                raise ArchitectError("На изображении не найден архитектурный план")
            return plan

    # ------------------------------------------------------------ model + geometry
    def make_model(self, slug: str, plan: dict) -> tuple[ArchModel, dict]:
        self.stage(slug, "plan", "running", "проверяю геометрию")
        m, rep = model_from_plan(plan, self.meta(slug).get("name") or slug)
        self.stage(slug, "plan", "error" if rep["errors"] else "done",
                   f"стен {len(m.data['walls'])}, помещений {len(m.data['rooms'])}, "
                   f"дверей {len(m.data['doors'])}, окон {len(m.data['windows'])}"
                   + (f"; ошибок: {len(rep['errors'])}" if rep["errors"] else ""))
        return m, rep

    async def realize(self, slug: str, m: ArchModel, progress=lambda msg, f=None: None, fresh: bool = False,
                      task_id: str | None = None, render: bool = False) -> dict:
        """Master model → FreeCAD (only the changed elements) → Blender (only the changed objects) → GLB preview."""
        d = self.project_dir(slug)
        out: dict = {"cad": None, "blender": None, "problems": {}, "clashes": [], "repaired": []}
        cad_state_f, bl_state_f = d / "cad" / "state.json", d / "blender" / "state.json"
        cad_state = {} if fresh or not cad_state_f.exists() else json.loads(cad_state_f.read_text("utf-8"))
        bl_state = {} if fresh or not bl_state_f.exists() else json.loads(bl_state_f.read_text("utf-8"))
        if not (d / "cad" / "building.FCStd").exists():
            cad_state, bl_state, fresh = {}, {}, True
        specs = cad_specs(m)
        changed, removed = diff(specs, cad_state.get("hashes", {}))
        meshes = dict(cad_state.get("meshes", {}))
        if changed or removed or fresh:
            async with self.op(slug, "update_cad" if not fresh else "create_cad", task_id) as rec:
                rec["affected"] = sorted(changed)[:200] + [f"-{x}" for x in removed][:50]
                self.stage(slug, "cad", "running", f"FreeCAD: {len(changed)} изм., {len(removed)} удал.")
                progress(f"FreeCAD/OCCT: {len(changed)} элементов", 0.6)
                r = await self.workers["freecad"].call("sync", {
                    "path": str(d / "cad" / "building.FCStd"), "levels": m.data["levels"], "specs": changed,
                    "removed": removed, "fresh": fresh, "mesh_dir": str(d / "cad" / "mesh")}, timeout=900)
                meshes.update(r.get("meshes", {}))
                for x in removed:
                    meshes.pop(x, None)
                for rid, a in (r.get("net_area") or {}).items():
                    room = m.get(rid)
                    if room:
                        room["net_area_m2"] = a
                out["cad"] = {k: r.get(k) for k in ("objects", "ms", "saved")}
                out["problems"], out["clashes"], out["repaired"] = r.get("problems", {}), r.get("clashes", []), r.get("repaired", [])
                cad_state = {"hashes": {k: v["_hash"] for k, v in specs.items()}, "meshes": meshes, "at": time.time()}
                cad_state_f.write_text(json.dumps(cad_state), "utf-8")
                bad = {k: v for k, v in out["problems"].items()}
                rec["detail"] = (f"{len(changed)} обновлено за {r.get('ms')} мс; OCCT: "
                                 + ("всё в порядке" if not bad else f"проблем {len(bad)}")
                                 + (f"; исправлено {len(out['repaired'])}" if out["repaired"] else "")
                                 + (f"; пересечений {len(out['clashes'])}" if out["clashes"] else ""))
                self.stage(slug, "cad", "error" if bad else "done", rec["detail"])
        vs = vis_specs(m, meshes)
        vchanged, vremoved = diff(vs, bl_state.get("hashes", {}))
        glb = d / "previews" / "building.glb"
        if vchanged or vremoved or fresh or not glb.exists():
            if self.workers["blender"].argv_fn and _blender_ok():
                async with self.op(slug, "sync_blender", task_id) as rec:
                    rec["affected"] = sorted(vchanged)[:200]
                    self.stage(slug, "preview", "running", f"Blender: {len(vchanged)} объектов")
                    progress(f"Blender: {len(vchanged)} объектов", 0.85)
                    r = await self.workers["blender"].call("sync", {
                        "blend": str(d / "blender" / "building.blend"), "mesh_dir": str(d / "cad" / "mesh"),
                        "specs": vchanged, "removed": vremoved, "materials": used_materials(vs), "fresh": fresh,
                        "glb": str(glb), "library": self.library}, timeout=600)
                    bl_state = {"hashes": {k: v["_hash"] for k, v in vs.items()}, "at": time.time()}
                    bl_state_f.write_text(json.dumps(bl_state), "utf-8")
                    out["blender"] = r
                    rec["detail"] = f"{len(r.get('updated', []))} объектов за {r.get('ms')} мс, GLB {r.get('glb', {}).get('size', 0) // 1024} КБ"
                    self.stage(slug, "preview", "done", rec["detail"])
            else:
                self.stage(slug, "preview", "error", "Blender не установлен — 3D-превью недоступно")
                out["blender"] = {"error": "Blender не установлен"}
        if render:
            await self.render(slug, "persp", task_id=task_id)
        return out

    # ------------------------------------------------------------ the whole pipeline
    async def build(self, slug: str, progress=lambda m, f=None: None, task_id: str | None = None,
                    assume_scale: bool = False) -> dict:
        async with self.lock(slug):
            plan = await self.analyze(slug, progress, task_id)
            m, rep = self.make_model(slug, plan)
            if rep["errors"]:
                self.save(slug, m, "модель с ошибками")
                raise ArchitectError("План не прошёл проверку: " + "; ".join(rep["errors"][:3]))
            return await self._continue(slug, m, rep, progress, task_id, assume_scale)

    async def _continue(self, slug: str, m: ArchModel, rep: dict, progress, task_id, assume_scale: bool) -> dict:
        blocking = [q for q in rep["questions"] if q.get("blocking")]
        if assume_scale:
            blocking = [q for q in blocking if q["kind"] != "scale"]
            src = m.data.get("source") or {}
            if src.get("scale_source") == "assumed":
                src["scale_source"], src["scale_confidence"] = "assumed_confirmed", 0.7
            m.data["questions"] = [q for q in m.data.get("questions", []) if q.get("kind") != "scale"]
        self.stage(slug, "model", "done", f"ревизия {m.data.get('revision', 0)}")
        if blocking:
            self.save(slug, m, "ждёт ответа")
            self.set_pending(slug, blocking[0])
            self.stage(slug, "cad", "waiting", "ждёт ответа: " + blocking[0]["text"][:120])
            return {"status": "needs_input", "question": blocking[0], "model": m.summary(), "report": rep}
        self.save(slug, m, "мастер-модель создана")
        res = await self.realize(slug, m, progress, fresh=True, task_id=task_id)
        self.save(slug, m, "CAD и 3D построены")
        self.set_pending(slug, None)
        return {"status": "ok", "model": m.summary(), "report": rep, "realize": res}

    # ------------------------------------------------------------ questions
    def pending(self, slug: str | None) -> dict | None:
        if not slug:
            return None
        return self.meta(slug).get("pending")

    def set_pending(self, slug: str, q: dict | None) -> None:
        m = self.meta(slug)
        if q is None:
            m.pop("pending", None)
        else:
            m["pending"] = {**q, "asked_at": time.time()}
        self.save_meta(slug, m)
        self.bus.publish("architect.question", {"project": slug, "question": q})

    async def answer(self, slug: str, text: str = "", values: dict | None = None, progress=lambda m, f=None: None,
                     task_id: str | None = None) -> dict:
        q = self.pending(slug)
        if not q:
            raise ArchitectError("Сейчас нет вопроса по модели")
        m = self.load(slug)
        if m is None:
            raise ArchitectError("У проекта ещё нет модели")
        a = dict(values or {})
        if text and not a:
            a = cmds.answer(text, q, m) or {}
        if not a:
            raise ArchitectError("Не понял ответ. " + q["text"])
        if q.get("kind") == "choose":
            if a.get("cancel") or not a.get("ops"):
                self.set_pending(slug, None)
                return {"status": "cancelled", "text": "Хорошо, ничего не меняю."}
            self.set_pending(slug, None)
            return await self.edit(slug, a["ops"], progress, task_id)
        if q.get("kind") == "scale":
            src = m.data.get("source") or {}
            if a.get("yes") is True:
                src["scale_source"], src["scale_confidence"] = "assumed_confirmed", 0.7
            elif a.get("yes") is False:
                raise ArchitectError("Тогда назовите любой размер, например «ширина дома 10 метров».")
            elif a.get("length"):
                L = float(a["length"])
                walls = m.data["walls"]
                if a.get("wall"):
                    cur = m.wall_len(m.need(a["wall"], "wall"))
                else:
                    pts = [pt(p) for w in walls for p in (w["start"], w["end"])]
                    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                    cur = (max(xs) - min(xs)) if a.get("axis") != "depth" else (max(ys) - min(ys))
                if cur <= 0:
                    raise ArchitectError("Не к чему приложить размер")
                m.apply({"op": "rescale", "factor": L / cur})
                src["scale_source"], src["scale_confidence"] = "user", 1.0
                src["scale_note"] = f"по размеру, который вы назвали ({L / 1000:g} м)"
            elif a.get("ratio"):
                raise ArchitectError("Масштаб «1:N» не переводится в пиксели без размера бумаги — назовите любой размер "
                                     "на чертеже, например «стена WALL_001 — 6 метров».")
            m.data["source"] = src
            m.data["questions"] = [x for x in m.data.get("questions", []) if x.get("kind") != "scale"]
            rep = validate(m)
            self.set_pending(slug, None)
            async with self.lock(slug):
                return await self._continue(slug, m, rep, progress, task_id, assume_scale=True)
        if q.get("kind") == "confirm":
            self.set_pending(slug, None)
            if a.get("yes") is False and q.get("target"):
                return await self.edit(slug, [{"op": "delete_element", "id": q["target"]}], progress, task_id)
            if q.get("target"):
                e = m.get(q["target"])
                if e:
                    e["confidence"] = 1.0
                    self.save(slug, m, f"подтверждено {q['target']}")
            return {"status": "ok", "text": "Оставил как есть."}
        self.set_pending(slug, None)
        return {"status": "ok", "text": "Принято."}

    # ------------------------------------------------------------ edits
    async def edit(self, slug: str, ops: list[dict], progress=lambda m, f=None: None, task_id: str | None = None) -> dict:
        async with self.lock(slug):
            m = self.load(slug)
            if m is None:
                raise ArchitectError("У проекта ещё нет модели — сначала постройте её по плану")
            before = copy.deepcopy(m.data)
            texts, touched = [], set()
            async with self.op(slug, "edit_model", task_id) as rec:
                self.stage(slug, "edit", "running", ", ".join(o.get("op", "?") for o in ops)[:200])
                try:
                    for op in ops:
                        r = m.apply(op)
                        texts.append(r["text"])
                        touched.update(r["changed"] + r["added"] + r["removed"])
                except ModelError as e:
                    m.data = before                 # all or nothing
                    self.stage(slug, "edit", "error", str(e))
                    raise ArchitectError(str(e)) from None
                rep = validate(m, repair=False)
                if rep["errors"]:
                    m.data = before
                    self.stage(slug, "edit", "error", rep["errors"][0])
                    raise ArchitectError("Изменение отменено — модель стала бы неверной: " + "; ".join(rep["errors"][:2]))
                m.data["warnings"] = rep["warnings"]
                rec["affected"] = sorted(touched)
                rec["detail"] = "; ".join(texts)[:300]
                self.save(slug, m, rec["detail"])
                res = await self.realize(slug, m, progress, task_id=task_id)
                self.save(slug, m, "после перестройки")
                self.stage(slug, "edit", "done", rec["detail"])
        problems = res.get("problems") or {}
        return {"status": "ok", "text": "; ".join(texts), "affected": sorted(touched), "realize": res,
                "problems": problems, "model": m.summary()}

    async def undo(self, slug: str, progress=lambda m, f=None: None, task_id: str | None = None) -> dict:
        d = self.project_dir(slug)
        m = self.load(slug)
        if m is None:
            raise ArchitectError("Нечего отменять")
        hist = d / "model" / "history.jsonl"
        notes = [json.loads(x) for x in hist.read_text("utf-8").splitlines() if x.strip()] if hist.exists() else []
        cur = int(m.data.get("revision", 0))
        prev = [n["rev"] for n in notes if n["rev"] < cur and (d / "model" / "history" / f"rev_{n['rev']:04d}.json").exists()]
        # the last revision whose content differs from the current one
        for rev in sorted(set(prev), reverse=True):
            data = json.loads((d / "model" / "history" / f"rev_{rev:04d}.json").read_text("utf-8"))
            if _content(data) != _content(m.data):
                async with self.lock(slug):
                    data["revision"] = cur + 1
                    m2 = ArchModel(data)
                    async with self.op(slug, "undo", task_id) as rec:
                        rec["detail"] = f"возврат к ревизии {rev}"
                        self.save(slug, m2, f"отмена: возврат к ревизии {rev}")
                        res = await self.realize(slug, m2, progress, task_id=task_id)
                return {"status": "ok", "text": f"Вернул модель к ревизии {rev}", "realize": res}
        raise ArchitectError("Отменять нечего — это первая версия модели")

    # ------------------------------------------------------------ export, render
    async def export(self, slug: str, formats: list[str], task_id: str | None = None) -> dict:
        d = self.project_dir(slug)
        m = self.load(slug)
        if m is None:
            raise ArchitectError("У проекта ещё нет модели")
        out_dir = d / "exports"
        out_dir.mkdir(exist_ok=True)
        name = slugify(m.data.get("name") or slug)
        res: dict = {}
        async with self.op(slug, "export_model", task_id) as rec:
            self.stage(slug, "export", "running", ", ".join(formats))
            fc = [f for f in formats if EXPORTS.get(f) == "freecad"]
            bl = [f for f in formats if EXPORTS.get(f) == "blender"]
            if fc:
                r = await self.workers["freecad"].call("export", {"path": str(d / "cad" / "building.FCStd"),
                                                                  "out_dir": str(out_dir), "formats": fc, "name": name}, timeout=600)
                res.update(r)
            if bl:
                r = await self.workers["blender"].call("export", {"blend": str(d / "blender" / "building.blend"),
                                                                  "out_dir": str(out_dir), "formats": bl, "name": name}, timeout=600)
                res.update(r)
            if "dxf" in formats:
                lines = {}
                for k, s in cad_specs(m).items():
                    if s["type"] == "wall":
                        lines[k] = (s["a"], s["b"])
                r = await self.workers["cv"].call("write_dxf", {"model": m.data, "out": str(out_dir / f"{name}.dxf"),
                                                                "cad": {"lines": lines}}, timeout=120)
                res["dxf"] = r
            if "json" in formats:
                p = out_dir / f"{name}.architectural_model.json"
                p.write_text(json.dumps(m.data, ensure_ascii=False, indent=1), "utf-8")
                res["json"] = {"path": str(p), "size": p.stat().st_size}
            for f in formats:
                if f not in EXPORTS:
                    res[f] = {"error": f"формат {f} не поддерживается"}
            res = {f: {**v, "check": verify_file(v.get("path"), f)} if v.get("path") else v for f, v in res.items()}
            bad = {f: v for f, v in res.items() if v.get("error") or not (v.get("check") or {}).get("ok")}
            rec["affected"] = [v.get("path") for v in res.values() if v.get("path")]
            rec["detail"] = ", ".join(f"{f}: {'ok' if f not in bad else 'ошибка'}" for f in res)
            self.stage(slug, "export", "error" if bad else "done", rec["detail"])
        return res

    async def render(self, slug: str, view: str = "persp", room: str | None = None, task_id: str | None = None,
                     engine: str = "eevee") -> dict:
        d = self.project_dir(slug)
        m = self.load(slug)
        if m is None:
            raise ArchitectError("У проекта ещё нет модели")
        args = {"blend": str(d / "blender" / "building.blend"), "view": view, "engine": engine, "samples": 48,
                "res": [1280, 800]}
        name = view
        if view == "room":
            r = m.need(room, "room")
            poly = [pt(p) for p in r["polygon"]]
            from . import geometry as g
            c = pt(r.get("anchor") or g.interior_point(poly))
            far = max(poly, key=lambda p: g.dist(p, c))
            eye = g.lerp(c, far, 0.72)
            args["room"] = {"c": list(c), "eye": list(eye), "z": float(m.level(r["level"])["elevation"])}
            name = "room_" + r["id"].lower()
        args["out"] = str(d / "previews" / f"{name}.png")
        async with self.op(slug, "render", task_id) as rec:
            r = await self.workers["blender"].call("render", args, timeout=600)
            rec["affected"] = [r["path"]]
            rec["detail"] = f"{view}: {r['ms']} мс ({r['engine']})"
        return r

    async def inspect_cad(self, slug: str) -> dict:
        d = self.project_dir(slug)
        return await self.workers["freecad"].call("inspect", {"path": str(d / "cad" / "building.FCStd")}, timeout=120)

    async def stop(self) -> None:
        for w in self.workers.values():
            await w.stop()

    # ------------------------------------------------------------ state for the window
    def file_url(self, slug: str, rel: str) -> str:
        return f"/architect/file?p={slug}/{rel}"

    def state(self, slug: str | None = None, model: bool = True) -> dict:
        slug = slug or self.active()
        out = {"projects": self.projects(), "active": slug, "engines": [e.as_dict() for e in engines()],
               "workers": {k: {"running": w.running(), "restarts": w.restarts, "last_error": w.last_error}
                           for k, w in self.workers.items()},
               "root": str(self.root)}
        if not slug:
            return out
        d = self.project_dir(slug)
        meta = self.meta(slug)
        m = self.load(slug)
        out["project"] = {"id": slug, "name": meta.get("name"), "pipeline": meta.get("pipeline", {}),
                          "pending": meta.get("pending"), "source_name": meta.get("source_name"),
                          "dir": str(d)}
        files = {}
        for rel in ("analysis/source.png", "analysis/annotated.png", "previews/building.glb", "previews/persp.png",
                    "previews/top.png"):
            if (d / rel).exists():
                files[rel] = self.file_url(slug, rel) + f"&v={int((d / rel).stat().st_mtime)}"
        for p in sorted((d / "previews").glob("room_*.png")):
            files["previews/" + p.name] = self.file_url(slug, "previews/" + p.name) + f"&v={int(p.stat().st_mtime)}"
        out["files"] = files
        out["exports"] = [{"name": p.name, "path": str(p), "size": p.stat().st_size}
                          for p in sorted((d / "exports").glob("*")) if p.is_file()]
        out["ops"] = self.ops_log(slug, 40)
        if model and m is not None:
            out["model"] = m.data
            out["summary"] = m.summary()
        try:
            out["plan_source"] = json.loads((d / "analysis" / "floorplan.json").read_text("utf-8")).get("source")
        except (OSError, ValueError):
            out["plan_source"] = None
        return out


def _content(data: dict) -> str:
    return json.dumps({k: v for k, v in data.items() if k not in ("revision", "updated_at")}, sort_keys=True,
                      ensure_ascii=False, default=str)


def _blender_ok() -> bool:
    from .workers import blender_exe
    return bool(blender_exe())


def verify_file(path: str | None, fmt: str) -> dict:
    """An exported file is real: it exists, is not empty and starts / parses as its format says."""
    if not path:
        return {"ok": False, "why": "нет файла"}
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return {"ok": False, "why": "файл пустой или не создан"}
    head = p.read_bytes()[:4096]
    try:
        if fmt == "glb":
            import struct
            magic, ver, length = struct.unpack("<4sII", head[:12])
            clen, ctype = struct.unpack("<I4s", head[12:20])
            js = json.loads(p.read_bytes()[20:20 + clen])
            nodes = [n.get("name", "") for n in js.get("nodes", [])]
            return {"ok": magic == b"glTF" and ver == 2 and length == p.stat().st_size and bool(nodes),
                    "nodes": len(nodes), "named": sum(1 for n in nodes if n.endswith("_VIS"))}
        if fmt == "gltf":
            js = json.loads(p.read_text("utf-8"))
            return {"ok": bool(js.get("nodes")), "nodes": len(js.get("nodes", []))}
        if fmt == "obj":
            txt = p.read_text("utf-8", "replace")
            v = sum(1 for ln in txt.splitlines() if ln.startswith("v "))
            o = sum(1 for ln in txt.splitlines() if ln.startswith(("o ", "g ")))
            return {"ok": v > 0, "vertices": v, "objects": o}
        if fmt == "blend":
            return {"ok": head[:7] == b"BLENDER" or head[:2] == b"\x28\xb5" or head[:4] == b"\x28\xb5\x2f\xfd",
                    "compressed": head[:4] == b"\x28\xb5\x2f\xfd"}
        if fmt == "step":
            return {"ok": head.startswith(b"ISO-10303-21"), "header": head[:12].decode("ascii", "replace")}
        if fmt == "stl":
            return {"ok": head[:5] == b"solid" or len(head) > 84}
        if fmt == "fcstd":
            import zipfile
            with zipfile.ZipFile(p) as z:
                return {"ok": "Document.xml" in z.namelist(), "entries": len(z.namelist())}
        if fmt == "brep":
            return {"ok": b"DBRep_DrawableShape" in head or b"CASCADE" in head}
        if fmt == "dxf":
            txt = head.decode("ascii", "replace")
            return {"ok": "SECTION" in txt and "HEADER" in txt}
        if fmt == "json":
            json.loads(p.read_text("utf-8"))
            return {"ok": True}
        if fmt == "fbx":
            return {"ok": head.startswith(b"Kaydara FBX Binary")}
    except Exception as e:      # noqa: BLE001
        return {"ok": False, "why": str(e)[:200]}
    return {"ok": True}
