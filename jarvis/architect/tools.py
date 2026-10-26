"""JARVIS Architect's tools in JARVIS's registry: every one runs through the Gateway (validation → policy → run →
verification → audit) and is offered to Claude through the MCP bridge like any other JARVIS tool.

    architect_build    2D plan (PNG/JPG/WEBP/PDF/DXF) → analysis → structured plan → checks → master model → FreeCAD
                       (BIM, OCCT checks) → Blender → 3D preview; asks only what it must (scale, doubtful elements)
    architect_answer   the answer to JARVIS's question (scale, which door…), then the pipeline goes on
    architect_command  a command in words («убери стену между кухней и гостиной») → operations → partial rebuild
    architect_edit     the same with explicit operations (for Claude and the window's buttons)
    architect_inspect  what the model holds: rooms, walls between which rooms, openings, furniture, ids, warnings
    architect_export   GLB / glTF / OBJ / .blend / FCStd / STEP / STL / BREP / DXF / JSON, each file checked
    architect_render   a Blender picture (from above, outside, inside a room)
    architect_view     the window's 3D view: a level, from above, inside, one element
    architect_project  list / open projects
    architect_tools    which engines are installed (FreeCAD, OCCT, Blender, OpenCV, OCR, Pascal Editor, Claude)

Risk: LOW for all — they only write inside the project's own folder (~/Documents/Jarvis/Архитектура/<project>),
every model change is kept as a revision (architect_edit op «undo» / «отмени последнее изменение»)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..tools.base import Risk, Tool, ToolError, ToolInputError, ToolResult, ToolSpec, Verification, resolve_in_scopes
from . import commands as cmds
from .service import INPUTS, NOT_YET, Architect, ArchitectError
from .workers import WorkerError, engines

OPS_DOC = ("Operations (JSON objects with \"op\"): add_wall{start,end,thickness?,height?,kind?}, delete_wall{id}, "
           "split_wall{id,at}, move_wall{id,dx,dy|by}, set_wall{id,thickness?,height?,kind?,material?}, "
           "set_walls{filter:{kind?,room?},thickness?,height?,material?}, resize_wall{id,length,anchor}, "
           "remove_wall_between{a,b} (room ids), add_opening{kind:door|window|opening,host,offset?,width?,height?,sill?,swing?}, "
           "delete_opening{id}, move_opening{id,by|to}, set_opening{id,width?,height?,sill?,swing?}, rename_room{id,name}, "
           "merge_rooms{a,b}, split_room{id,axis?,at?,name?}, add_room{name,width,depth,next_to?,side?}, delete_room{id}, "
           "set_level{id?,height?,name?}, add_level{name?}, add_furniture{kind,room?,position?,rotation?,opposite?,material?}, "
           "move_furniture{id,dx?,dy?,room?,rotation?,rotate_by?}, delete_furniture{id}, set_furniture{id,size?,material?}, "
           "set_material{target:walls|interior_walls|exterior_walls|floor|ceiling|<id>,material,room?}, add_column{center,width,depth}, "
           "add_stair{start,direction,width,length,steps?}, set_roof{kind:flat|none}, delete_element{id}, rescale{factor}, "
           "undo{}. Millimetres, plan X right / Y up. Furniture kinds: sofa armchair bed chair table desk wardrobe kitchen "
           "stove sink toilet bathtub shower refrigerator tv nightstand washer. Materials: plaster_white plaster_gray "
           "plaster_beige paint_blue paint_green paint_yellow paint_black brick_red concrete oak_light oak_dark walnut "
           "tile_white tile_gray marble carpet_gray.")


def _project(arch: Architect, args: dict) -> str:
    slug = args.get("project") or arch.active()
    if not slug:
        raise ToolInputError("Нет ни одного архитектурного проекта — сначала дайте план (architect_build с source)")
    arch.project_dir(slug)
    if not (arch.root / slug / "project.json").exists():
        raise ToolInputError(f"Нет проекта «{slug}»")
    return slug


def _summary_text(s: dict) -> str:
    rooms = ", ".join(f"{r['name']} {r.get('net_area_m2') or r['area_m2']} м²" for r in s["rooms"][:12])
    return (f"Помещений {len(s['rooms'])} ({rooms}); стен {len(s['walls'])}; "
            f"дверей {sum(1 for o in s['openings'] if o['type'] == 'door')}, "
            f"окон {sum(1 for o in s['openings'] if o['type'] == 'window')}; мебели {len(s['furniture'])}")


class _Base(Tool):
    def __init__(self, arch: Architect):
        self.arch = arch

    async def verify(self, args, result, ctx):
        """The model on disk is the one the tool reported, and the 3D file exists when the tool said it built it."""
        slug = result.data.get("project")
        if not slug:
            return Verification("unverified", "Нет проекта для проверки")
        m = self.arch.load(slug)
        if result.data.get("status") == "needs_input":
            return Verification("verified", "Модель сохранена; ждёт ответа на вопрос")
        if m is None:
            return Verification("failed", "Файл мастер-модели не найден")
        if result.data.get("revision") is not None and int(m.data.get("revision", -1)) < int(result.data["revision"]):
            return Verification("failed", "Ревизия модели на диске старше, чем сообщил инструмент")
        if result.data.get("built"):
            d = self.arch.project_dir(slug)
            need = [d / "cad" / "building.FCStd", d / "previews" / "building.glb"]
            missing = [p.name for p in need if not p.exists()]
            if missing:
                return Verification("failed", "Нет файлов: " + ", ".join(missing))
        return Verification("verified", f"Мастер-модель ревизии {m.data.get('revision')} сохранена"
                            + (", FreeCAD и 3D-превью обновлены" if result.data.get("built") else ""))


class ArchitectBuild(_Base):
    def __init__(self, arch: Architect, config):
        super().__init__(arch)
        self.config = config
        self.spec = ToolSpec(
            id="architect_build", title="Архитектура: 3D по плану",
            description=("Turn a 2D architectural plan (PNG, JPG, WEBP, PDF page 1, DXF; a scan, photo or hand sketch) into "
                         "an editable parametric 3D model: OpenCV + local OCR + Claude Vision → structured floor plan → "
                         "checks → master model → FreeCAD BIM model checked by Open CASCADE → Blender scene → 3D preview "
                         "in JARVIS's «Архитектура» workspace. Give the file path (attached files are listed in the "
                         "message). Without source it rebuilds the active project. If the result asks a question (scale, "
                         "doubtful element), ask the user exactly that and pass the answer to architect_answer. "
                         "DWG is not supported (ask for DXF/PDF)."),
            params={"type": "object", "properties": {
                "source": {"type": "string", "maxLength": 1000, "description": "full path of the plan file"},
                "name": {"type": "string", "maxLength": 80, "description": "project name"},
                "assume_scale": {"type": "boolean", "description": "the user agreed to an assumed scale"},
                "project": {"type": "string", "maxLength": 64}}},
            risk=Risk.LOW, side_effects="local", timeout_s=1500, model_callable=True, idempotent=False,
            verification="Мастер-модель сохранена, FCStd и GLB созданы, OCCT-проверка элементов",
            impact="Создаётся папка проекта в ~/Documents/Jarvis/Архитектура (исходник, анализ, модель, CAD, Blender, превью).")

    def validate(self, args, ctx):
        a = Tool.validate(self, args, ctx)
        if a.get("source"):
            roots = list(self.config.read_roots) + [self.arch.root]
            p = resolve_in_scopes(a["source"], roots)
            if not p.is_file():
                raise ToolInputError(f"Нет файла {p}")
            ext = p.suffix.lower()
            if ext in NOT_YET:
                raise ToolInputError(NOT_YET[ext])
            if ext not in INPUTS:
                raise ToolInputError(f"Формат {ext} не поддерживается: PNG, JPG, WEBP, PDF, DXF")
            if p.stat().st_size > 60 * 1024 * 1024:
                raise ToolInputError("Файл больше 60 МБ")
            a["source"] = str(p)
        elif not (a.get("project") or self.arch.active()):
            raise ToolInputError("Какой план? Укажите файл (PNG, JPG, PDF, DXF) или загрузите его в раздел «Архитектура»")
        return a

    def describe(self, args):
        return f"3D-модель по плану {Path(args['source']).name}" if args.get("source") else "Перестроить 3D-модель"

    async def run(self, args, ctx):
        arch = self.arch
        try:
            if args.get("source"):
                slug = arch.new_project(Path(args["source"]), args.get("name"))
            else:
                slug = _project(arch, args)
                arch.set_active(slug)
            arch.bus.publish("architect.updated", {"project": slug, "reason": "start"})
            res = await arch.build(slug, ctx.progress, ctx.task_id, bool(args.get("assume_scale")))
        except (ArchitectError, WorkerError) as e:
            raise ToolError(str(e)) from None
        arch.bus.publish("architect.updated", {"project": slug, "reason": "build"})
        s = res["model"]
        if res["status"] == "needs_input":
            return ToolResult({"project": slug, "status": "needs_input", "question": res["question"]["text"],
                               "summary": s, "revision": s["revision"]},
                              f"План распознан: {_summary_text(s)}. Вопрос: {res['question']['text']}", card="architect")
        rz = res.get("realize") or {}
        probs = rz.get("problems") or {}
        warn = res["report"].get("warnings", [])
        low = [q["text"] for q in res["report"].get("questions", []) if not q.get("blocking")]
        text = (f"3D-модель готова: {_summary_text(s)}. FreeCAD: {rz.get('cad', {}).get('objects', 0)} объектов, "
                f"OCCT-проверка: {'без ошибок' if not probs else 'проблем ' + str(len(probs))}"
                + (f"; исправлено автоматически: {len(rz.get('repaired') or [])}" if rz.get("repaired") else "")
                + f". Масштаб: {(arch.load(slug).data.get('source') or {}).get('scale_note') or 'из чертежа'}.")
        if low:
            text += " Уточнить: " + " ".join(low[:2])
        return ToolResult({"project": slug, "status": "ok", "built": True, "summary": s, "revision": s["revision"],
                           "warnings": warn[:10], "problems": probs, "questions": low[:5]}, text, card="architect")


class ArchitectAnswer(_Base):
    def __init__(self, arch: Architect):
        super().__init__(arch)
        self.spec = ToolSpec(
            id="architect_answer", title="Архитектура: ответ на вопрос",
            description=("The user's answer to JARVIS Architect's pending question (scale, which element, keep a doubtful "
                         "element). Pass the user's words as text; or structured: yes (bool), length_mm + axis "
                         "(width|depth) or wall id, choice (1-based option number). Then the pipeline continues."),
            params={"type": "object", "properties": {
                "text": {"type": "string", "maxLength": 400}, "yes": {"type": "boolean"},
                "length_mm": {"type": "number", "minimum": 100, "maximum": 500000},
                "axis": {"type": "string", "enum": ["width", "depth"]}, "wall": {"type": "string", "maxLength": 20},
                "choice": {"type": "integer", "minimum": 1, "maximum": 9}, "project": {"type": "string", "maxLength": 64}}},
            risk=Risk.LOW, side_effects="local", timeout_s=1500, model_callable=True, idempotent=False,
            verification="Вопрос снят, модель сохранена (и перестроена)")

    def describe(self, args):
        return "Ответ на вопрос по модели" + (f": «{args['text'][:60]}»" if args.get("text") else "")

    async def run(self, args, ctx):
        slug = _project(self.arch, args)
        q = self.arch.pending(slug)
        if not q:
            raise ToolInputError("Сейчас нет вопроса по модели")
        vals = {}
        if args.get("yes") is not None:
            vals["yes"] = bool(args["yes"])
        if args.get("length_mm"):
            vals.update({"length": float(args["length_mm"]), "axis": args.get("axis") or "width", "wall": args.get("wall")})
        if args.get("choice"):
            opts = q.get("options") or []
            if args["choice"] > len(opts):
                raise ToolInputError(f"Вариантов только {len(opts)}")
            vals = {"ops": opts[args["choice"] - 1]["ops"]}
        try:
            res = await self.arch.answer(slug, args.get("text") or "", vals or None, ctx.progress, ctx.task_id)
        except (ArchitectError, WorkerError) as e:
            raise ToolError(str(e)) from None
        self.arch.bus.publish("architect.updated", {"project": slug, "reason": "answer"})
        m = self.arch.load(slug)
        if res.get("status") == "needs_input":
            return ToolResult({"project": slug, "status": "needs_input", "question": res["question"]["text"]},
                              res["question"]["text"], card="architect")
        text = res.get("text") or ("3D-модель построена: " + _summary_text(res["model"]) if res.get("model") else "Готово.")
        return ToolResult({"project": slug, "status": res.get("status", "ok"), "built": "realize" in res,
                           "revision": m.data.get("revision") if m else None}, text, card="architect")


class ArchitectCommand(_Base):
    def __init__(self, arch: Architect):
        super().__init__(arch)
        self.spec = ToolSpec(
            id="architect_command", title="Архитектура: команда",
            description=("A command about the active building model in the user's words (Russian), e.g. «убери стену между "
                         "кухней и гостиной», «перемести дверь на 40 см вправо», «добавь диван в гостиную», «поставь "
                         "телевизор напротив дивана», «сделай пол тёмным дубом», «измени высоту потолка на 3 метра», «покажи "
                         "дом сверху», «сохрани проект», «экспортируй в STEP». JARVIS finds the exact elements, asks if several "
                         "fit, applies the change and rebuilds only what changed (FreeCAD → OCCT check → Blender → preview). "
                         "If it answers that it did not understand, use architect_inspect + architect_edit."),
            params={"type": "object", "required": ["text"], "properties": {
                "text": {"type": "string", "minLength": 2, "maxLength": 400},
                "selected": {"type": "string", "maxLength": 20, "description": "element selected in the window"},
                "project": {"type": "string", "maxLength": 64}}},
            risk=Risk.LOW, side_effects="local", timeout_s=900, model_callable=True, idempotent=False,
            verification="Изменение в мастер-модели, CAD и 3D перестроены, OCCT-проверка")

    def describe(self, args):
        return f"Модель: «{args['text'][:80]}»"

    async def run(self, args, ctx):
        arch = self.arch
        slug = _project(arch, args)
        m = arch.load(slug)
        sel = args.get("selected") or arch.db.get_kv("architect_selected")
        p = cmds.parse(args["text"], m, sel)
        if p is None:
            return ToolResult({"project": slug, "status": "not_understood"},
                              "Не понял команду для модели. Помещения: " + ", ".join(r["name"] for r in (m.data["rooms"] if m else [])),
                              card="architect")
        try:
            if p.kind == "build":
                res = await arch.build(slug, ctx.progress, ctx.task_id)
                arch.bus.publish("architect.updated", {"project": slug, "reason": "build"})
                if res["status"] == "needs_input":
                    return ToolResult({"project": slug, "status": "needs_input"}, res["question"]["text"], card="architect")
                return ToolResult({"project": slug, "status": "ok", "built": True, "revision": res["model"]["revision"]},
                                  "3D-модель перестроена: " + _summary_text(res["model"]), card="architect")
            if m is None:
                raise ToolInputError("У проекта ещё нет модели — сначала «сделай 3D по плану»")
            if p.kind == "question":
                arch.set_pending(slug, {"id": "Q_CHOOSE", **p.question, "blocking": True})
                return ToolResult({"project": slug, "status": "needs_input", "question": p.question["text"]},
                                  p.question["text"], card="architect")
            if p.kind == "view":
                arch.bus.publish("architect.view", {"project": slug, **p.view})
                arch.bus.publish("ui.navigate", {"view": "architect"})
                return ToolResult({"project": slug, "status": "ok", "view": p.view}, p.say + ".", card="architect")
            if p.kind == "info":
                return ToolResult({"project": slug, "status": "ok"}, p.say + ".", card="architect")
            if p.kind == "save":
                rev = arch.save(slug, m, "сохранено по просьбе")
                d = arch.project_dir(slug)
                return ToolResult({"project": slug, "status": "ok", "revision": rev},
                                  f"Проект сохранён (ревизия {rev}): {d}", card="architect")
            if p.kind == "undo":
                res = await arch.undo(slug, ctx.progress, ctx.task_id)
                arch.bus.publish("architect.updated", {"project": slug, "reason": "undo"})
                return ToolResult({"project": slug, "status": "ok", "built": True}, res["text"] + ".", card="architect")
            if p.kind == "export":
                res = await arch.export(slug, p.args["formats"], ctx.task_id)
                return _export_result(slug, res)
            if p.kind == "render":
                res = await arch.render(slug, p.args.get("view", "persp"), p.args.get("room"), ctx.task_id)
                arch.bus.publish("architect.updated", {"project": slug, "reason": "render"})
                return ToolResult({"project": slug, "status": "ok", "path": res["path"]},
                                  f"Визуализация готова ({res['ms'] / 1000:.1f} с): {res['path']}", card="architect",
                                  artifacts=[res["path"]])
            res = await arch.edit(slug, p.ops, ctx.progress, ctx.task_id)
        except (ArchitectError, WorkerError) as e:
            raise ToolError(str(e)) from None
        arch.bus.publish("architect.updated", {"project": slug, "reason": "edit", "affected": res["affected"]})
        probs = res.get("problems") or {}
        text = res["text"] + "." + (f" OCCT: проблем {len(probs)} ({', '.join(probs)[:120]})." if probs else "")
        return ToolResult({"project": slug, "status": "ok", "built": True, "affected": res["affected"],
                           "revision": res["model"]["revision"], "problems": probs}, text, card="architect")


def _export_result(slug: str, res: dict) -> ToolResult:
    ok = {f: v for f, v in res.items() if v.get("path") and (v.get("check") or {}).get("ok")}
    bad = {f: (v.get("error") or (v.get("check") or {}).get("why") or "не прошёл проверку") for f, v in res.items() if f not in ok}
    text = ("Экспорт: " + ", ".join(f"{f.upper()} {Path(v['path']).name}" for f, v in ok.items())
            + (". Не получилось: " + "; ".join(f"{f}: {w}" for f, w in bad.items()) if bad else "") + ".")
    return ToolResult({"project": slug, "status": "ok" if ok else "failed", "files": {f: v["path"] for f, v in ok.items()},
                       "failed": bad}, text, card="architect", artifacts=[v["path"] for v in ok.values()])


class ArchitectEdit(_Base):
    def __init__(self, arch: Architect):
        super().__init__(arch)
        self.spec = ToolSpec(
            id="architect_edit", title="Архитектура: изменить модель",
            description=("Change the active building model with explicit operations (all or nothing; then only the changed "
                         "elements are rebuilt in FreeCAD/OCCT and Blender). Use architect_inspect first to get ids. "
                         + OPS_DOC),
            params={"type": "object", "required": ["ops"], "properties": {
                "ops": {"type": "array", "minItems": 1, "maxItems": 30, "items": {"type": "object"}},
                "project": {"type": "string", "maxLength": 64}}},
            risk=Risk.LOW, side_effects="local", timeout_s=900, model_callable=True, idempotent=False,
            verification="Мастер-модель изменена, CAD и 3D перестроены, OCCT-проверка")

    def validate(self, args, ctx):
        a = Tool.validate(self, args, ctx)
        for op in a["ops"]:
            if not isinstance(op, dict) or not isinstance(op.get("op"), str) or not re.fullmatch(r"[a-z_]{2,40}", op["op"]):
                raise ToolInputError("Каждая операция — объект с полем «op»")
            if len(json.dumps(op)) > 4000:
                raise ToolInputError("Слишком большая операция")
        return a

    def describe(self, args):
        return "Модель: " + ", ".join(o["op"] for o in args["ops"])[:120]

    async def run(self, args, ctx):
        slug = _project(self.arch, args)
        try:
            if len(args["ops"]) == 1 and args["ops"][0]["op"] == "undo":
                res = await self.arch.undo(slug, ctx.progress, ctx.task_id)
                self.arch.bus.publish("architect.updated", {"project": slug, "reason": "undo"})
                return ToolResult({"project": slug, "status": "ok", "built": True}, res["text"] + ".", card="architect")
            res = await self.arch.edit(slug, args["ops"], ctx.progress, ctx.task_id)
        except (ArchitectError, WorkerError) as e:
            raise ToolError(str(e)) from None
        self.arch.bus.publish("architect.updated", {"project": slug, "reason": "edit", "affected": res["affected"]})
        return ToolResult({"project": slug, "status": "ok", "built": True, "affected": res["affected"],
                           "revision": res["model"]["revision"], "problems": res.get("problems") or {}},
                          res["text"] + ".", card="architect")


class ArchitectInspect(Tool):
    def __init__(self, arch: Architect):
        self.arch = arch
        self.spec = ToolSpec(
            id="architect_inspect", title="Архитектура: что в модели",
            description=("Read the active building model: levels, rooms (names, areas), walls (ids, length, thickness, "
                         "between which rooms), doors and windows (host wall, offset, width, rooms on both sides), "
                         "furniture, scale, pending question, warnings; with element=<id> one element in full; with "
                         "cad=true the FreeCAD document's objects and their OCCT validity."),
            params={"type": "object", "properties": {"project": {"type": "string", "maxLength": 64},
                                                     "element": {"type": "string", "maxLength": 20},
                                                     "cad": {"type": "boolean"}}},
            risk=Risk.LOW, side_effects="none", timeout_s=120, model_callable=True,
            verification="Прочитано из файла мастер-модели")

    async def run(self, args, ctx):
        slug = _project(self.arch, args)
        m = self.arch.load(slug)
        if m is None:
            return ToolResult({"project": slug, "model": None, "pending": self.arch.pending(slug)},
                              "Модели ещё нет: план загружен, но 3D не построено.")
        if args.get("element"):
            e = m.get(args["element"])
            if e is None:
                raise ToolInputError(f"Нет элемента {args['element']}")
            return ToolResult({"project": slug, "element": e}, f"{e['id']}: {e.get('name') or e['type']}")
        data = {"project": slug, "summary": m.summary(), "pending": self.arch.pending(slug)}
        if args.get("cad"):
            try:
                data["cad"] = await self.arch.inspect_cad(slug)
            except WorkerError as e:
                data["cad"] = {"error": str(e)}
        return ToolResult(data, _summary_text(m.summary()))

    async def verify(self, args, result, ctx):
        return Verification("verified", "Прочитано из файла мастер-модели")


class ArchitectExport(_Base):
    def __init__(self, arch: Architect):
        super().__init__(arch)
        self.spec = ToolSpec(
            id="architect_export", title="Архитектура: экспорт",
            description=("Export the active building: glb, gltf, obj, blend, fbx (Blender — with materials and furniture), "
                         "fcstd, step, stl, brep (FreeCAD/OCCT — exact solids), dxf (2D plan), json (master model). Each "
                         "file is read back to prove it is valid. Files go to the project's exports/ folder."),
            params={"type": "object", "required": ["formats"], "properties": {
                "formats": {"type": "array", "minItems": 1, "maxItems": 11, "items": {"type": "string", "enum": [
                    "glb", "gltf", "obj", "blend", "fbx", "fcstd", "step", "stl", "brep", "dxf", "json"]}},
                "project": {"type": "string", "maxLength": 64}}},
            risk=Risk.LOW, side_effects="local", timeout_s=900, model_callable=True, idempotent=True,
            verification="Каждый файл прочитан обратно (заголовок/структура формата)")

    def describe(self, args):
        return "Экспорт модели: " + ", ".join(f.upper() for f in args["formats"])

    async def run(self, args, ctx):
        slug = _project(self.arch, args)
        try:
            res = await self.arch.export(slug, args["formats"], ctx.task_id)
        except (ArchitectError, WorkerError) as e:
            raise ToolError(str(e)) from None
        self.arch.bus.publish("architect.updated", {"project": slug, "reason": "export"})
        return _export_result(slug, res)

    async def verify(self, args, result, ctx):
        files = result.data.get("files") or {}
        from .service import verify_file
        bad = [f for f, p in files.items() if not verify_file(p, f).get("ok")]
        if not files:
            return Verification("failed", "Ни один файл не создан")
        if bad:
            return Verification("failed", "Не прошли проверку: " + ", ".join(bad))
        return Verification("verified", "Проверены: " + ", ".join(f.upper() for f in files))


class ArchitectRender(_Base):
    def __init__(self, arch: Architect):
        super().__init__(arch)
        self.spec = ToolSpec(
            id="architect_render", title="Архитектура: визуализация",
            description="A Blender picture of the active building: view top | persp | room (with room id); engine eevee | cycles.",
            params={"type": "object", "properties": {
                "view": {"type": "string", "enum": ["top", "persp", "room"]}, "room": {"type": "string", "maxLength": 20},
                "engine": {"type": "string", "enum": ["eevee", "cycles", "workbench"]},
                "project": {"type": "string", "maxLength": 64}}},
            risk=Risk.LOW, side_effects="local", timeout_s=900, model_callable=True,
            verification="PNG создан")

    async def run(self, args, ctx):
        slug = _project(self.arch, args)
        try:
            r = await self.arch.render(slug, args.get("view") or "persp", args.get("room"), ctx.task_id,
                                       args.get("engine") or "eevee")
        except (ArchitectError, WorkerError) as e:
            raise ToolError(str(e)) from None
        self.arch.bus.publish("architect.updated", {"project": slug, "reason": "render"})
        return ToolResult({"project": slug, "path": r["path"], "status": "ok"}, f"Визуализация: {r['path']}",
                          artifacts=[r["path"]], card="architect")

    async def verify(self, args, result, ctx):
        p = Path(result.data["path"])
        ok = p.exists() and p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        return Verification("verified" if ok else "failed", "PNG создан" if ok else "PNG не создан")


class ArchitectView(Tool):
    def __init__(self, arch: Architect, bus):
        self.arch, self.bus = arch, bus
        self.spec = ToolSpec(
            id="architect_view", title="Архитектура: показать",
            description=("Show the building in the window's «Архитектура» workspace: mode top (from above), persp (whole "
                         "house), walk (inside, first person; target = room id), level (level id), focus (element id)."),
            params={"type": "object", "required": ["mode"], "properties": {
                "mode": {"type": "string", "enum": ["top", "persp", "walk", "level", "focus"]},
                "level": {"type": "string", "maxLength": 20}, "target": {"type": "string", "maxLength": 20}}},
            risk=Risk.LOW, side_effects="none", timeout_s=10, model_callable=True,
            verification="Окно получило команду")

    async def run(self, args, ctx):
        slug = self.arch.active()
        self.bus.publish("ui.navigate", {"view": "architect"})
        self.bus.publish("architect.view", {"project": slug, **args})
        return ToolResult({"project": slug, **args}, "Показываю модель.")

    async def verify(self, args, result, ctx):
        return Verification("unverified", "Окно получает команду через события; отдельного подтверждения нет")


class ArchitectProject(Tool):
    def __init__(self, arch: Architect):
        self.arch = arch
        self.spec = ToolSpec(
            id="architect_project", title="Архитектура: проекты",
            description="List architecture projects, or open one (make it active).",
            params={"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "open"]},
                                                     "project": {"type": "string", "maxLength": 64}}},
            risk=Risk.LOW, side_effects="none", timeout_s=10, model_callable=True, verification="Прочитано с диска")

    async def run(self, args, ctx):
        if args.get("action") == "open":
            slug = _project(self.arch, args)
            self.arch.set_active(slug)
            self.arch.bus.publish("architect.updated", {"project": slug, "reason": "open"})
            return ToolResult({"project": slug}, f"Открыт проект «{self.arch.meta(slug).get('name')}».")
        ps = self.arch.projects()
        return ToolResult({"projects": ps, "active": self.arch.active()},
                          "Проекты: " + (", ".join(p["name"] for p in ps) or "нет"))

    async def verify(self, args, result, ctx):
        return Verification("verified", "Прочитано с диска")


class ArchitectTools(Tool):
    def __init__(self, arch: Architect):
        self.arch = arch
        self.spec = ToolSpec(
            id="architect_tools", title="Архитектура: инструменты",
            description=("Which building-model engines are installed and working: FreeCAD (+ its Python API), Open "
                         "CASCADE, Blender (+ Python), the drawing recognition worker (OpenCV), tesseract, Pascal, Claude "
                         "Vision — versions and paths; check=true starts each worker and pings it."),
            params={"type": "object", "properties": {"check": {"type": "boolean"}}},
            risk=Risk.LOW, side_effects="none", timeout_s=240, model_callable=True, verification="Проверено на диске")

    async def run(self, args, ctx):
        info = [e.as_dict() for e in engines()]
        pings = {}
        if args.get("check"):
            for k, w in self.arch.workers.items():
                try:
                    pings[k] = await w.call("ping", timeout=120)
                except WorkerError as e:
                    pings[k] = {"error": str(e)}
        ok = [e["title"] for e in info if e["available"]]
        missing = [f"{e['title']} ({e['detail']})" for e in info if not e["available"]]
        return ToolResult({"engines": info, "ping": pings}, "Есть: " + ", ".join(ok) +
                          (". Нет: " + "; ".join(missing) if missing else "") + ".")

    async def verify(self, args, result, ctx):
        return Verification("verified", "Проверено на диске")


def tools(arch: Architect, config, bus) -> list[Tool]:
    return [ArchitectBuild(arch, config), ArchitectAnswer(arch), ArchitectCommand(arch), ArchitectEdit(arch),
            ArchitectInspect(arch), ArchitectExport(arch), ArchitectRender(arch), ArchitectView(arch, bus),
            ArchitectProject(arch), ArchitectTools(arch)]
