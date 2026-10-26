# ADR-010: JARVIS Architect — a 2D plan → a parametric BIM model → its look, inside JARVIS

Date: 2026-10-26 · Status: accepted · Builds on ADR-005 (all AI through Claude Code), ADR-007 (one JARVIS), ADR-009

## Context
The request: a skill inside the existing JARVIS (not a second app, chat or AI) that turns a 2D plan — a CAD export,
scan, photo, hand sketch, PDF, image or DXF — into an editable parametric 3D building: AI only for understanding,
exact geometry from CAD engines, the look from Blender, one master model as the truth, voice and window editing,
partial rebuilds, honest questions instead of guesses, exports that are really checked.

On this computer before: no FreeCAD, no Open CASCADE, no Blender, no 3D viewer in the window. Installed for it
(2026-10-26, official Arch packages, the user typed the password in the system dialog): freecad 1.1.4 (with
opencascade 7.9.3), blender 5.2.2; the drawing venv ~/.local/share/jarvis/architect/.venv (OpenCV 4.12, pdfium,
ezdxf 1.4.2, Pillow); tesseract 5.5.3 with rus was already there. Pascal Editor is not installed and is not used.

## Decision

```
voice / chat / «Архитектура» window
        │  router: «сделай 3D по этому плану» + file, answers, «убери стену…» while a model is open → no AI
        ▼
Director._run_architect → Gateway (validation → policy → run → verify → audit) → architect_* tools
        ▼
architect/service.Architect — the orchestrator, one project folder per building
  SOURCE ─► 2D ANALYSIS ─────────────────────────► STRUCTURED PLAN ─► VALIDATION ─► MASTER MODEL
            cv_worker (OpenCV walls, openings,        jarvis.floorplan/1   validate.py     model.py (ids,
            window glazing, quality, PDF/WEBP          (mm, confidence,    (repairs, errors, rooms = faces
            raster, DXF lines) + tesseract OCR         source per element)  questions)      of the walls)
            + Claude Vision over a numbered copy
                                                                                │ specs.py: per-element
                                                                                ▼ specs + hashes (what changed)
                      FreeCAD worker (Arch BIM on OCCT) ──meshes──► Blender worker ──GLB──► three.js viewer
                      checks every solid, repairs, clashes          look, furniture, light,     (orbit, top,
                      → building.FCStd, STEP/STL/BREP               renders → .blend/OBJ/GLB    walk, select)
```

* **Master model** (`model/architectural_model.json`): millimetres, stable ids (WALL_001, ROOM_001, DOOR_001,
  WINDOW_001, STAIR_001, COLUMN_001, SLAB_001, ROOF_001, FURN_001), each element with level, confidence and source.
  Rooms are not drawn: they are the closed faces of the wall graph (`geometry.planar_faces`) and keep their ids and
  names across edits (matched by an interior point; merged rooms keep the bigger id, «Кухня-гостиная»). Every edit is
  an operation (`ArchModel.apply`, all-or-nothing) and a revision (`model/history/rev_NNNN.json`, undo).
* **Never image → 3D**: the analysis writes the structured plan first; `plan.check_plan` validates it strictly, then
  `validate.validate` repairs small things (straighten < 2.5°, merge duplicates, drop < 15 cm, openings back inside
  their wall) and turns doubts into questions. A blocking question (scale not found) stops before CAD; the person's
  answer («да», «ширина дома 9 метров») continues the same pipeline.
* **Vision = OpenCV + Claude, set-of-marks**: OpenCV finds walls (filled poché, double lines, single pen lines,
  diagonals), openings and glazing; the annotated copy numbers them (W1…, O1…). Claude (JARVIS's own Claude Code,
  `ClaudeChatProvider.vision`: `claude -p`, Read only on a temp folder, `--json-schema`) answers about those numbers —
  wall or not, kind, room names, doors vs windows, sizes written on the drawing, furniture — instead of inventing
  pixels. Without Claude (offline profile, limit, no sign-in) OpenCV + tesseract still build the plan, with lower
  confidences, and the answer says so.
* **Scale** (`vision.estimate_scale`): written sizes (Claude's reading with the size line's ends, else OCR numbers
  next to walls) → written room areas → an assumption (door ≈ 900 mm, exterior wall ≈ 300 mm, longest side ≈ 10 m)
  that is asked about, never silently used.
* **Tool router** (`service.py`, never the person): recognition → OpenCV + OCR + Claude Vision; exact geometry,
  booleans, solid checks → FreeCAD on OCCT; look, materials, furniture, renders, GLB/OBJ/.blend/FBX → Blender;
  FCStd/STEP/STL/BREP → FreeCAD; DXF plan → ezdxf; DWG → refused with how to get DXF/PDF.
* **FreeCAD = CAD truth**: Arch objects — Site «Участок» → Building «Здание» → Building Storey per level → groups
  Стены / Двери / Окна / Помещения / Перекрытия / Колонны / Лестницы / Мебель / Крыша; walls are Arch walls on a base
  line (Width/Height editable in FreeCAD), doors/windows are Arch window presets hosted in their wall (OCCT cuts the
  opening), rooms are Arch spaces, slabs/roof/columns Arch structures, stairs Arch stairs. Corner and T-joins are
  computed from the wall graph (`geometry.wall_joins`): the higher-priority wall runs through, the other stops at its
  face — no gaps, no overlaps. After every change each touched solid is checked (isValid, BRepCheck, BOP self-
  intersection per solid, volume), repaired (ShapeFix, removeSplitter, or a plain OCCT solid with boolean-cut openings
  in place of a failing Arch wall), checked again; wall clashes are measured with OCCT `common`; the clear floor area of
  each room is the room face minus the wall footprints (OCCT cut).
* **Blender = look**: meshes from FreeCAD (Blender never invents walls), one object per element named `<ID>_VIS`
  with jarvis_id / jarvis_type / level as glTF extras; room floors and ceilings with their own finishes; procedural
  3D furniture (17 kinds; `<architect>/_library/<kind>.glb` replaces one if present); sun, a light per room, cameras;
  EEVEE/Cycles renders.
* **Partial rebuild** (`specs.py`): every element's derived CAD spec and Blender spec are hashed; a stage rebuilds
  only what differs from its last build. Measured: a moved door → FreeCAD 1 object (+ its wall re-cut) in 0.15 s,
  Blender 2 objects in 0.04 s; a new sofa → FreeCAD 1 box + Blender 1 object; a floor material → Blender only.
* **Workers** (`workers.py`): FreeCAD (system Python + FreeCAD lib), Blender (`blender -b`), drawing worker (own venv),
  long-lived JSON-lines processes started on demand, restarted and retried once after a crash, stopped after 10 idle
  minutes; reasons said plainly («не хватает модуля Python …»). The core's venv never leaks into them.
* **Observability**: every operation → op id, JARVIS task id, operation, status, start, end, error, affected ids
  (project `log/operations.jsonl`, event `architect.op`, audit `architect.*`); pipeline stages → `architect.stage`.
* **Window** («Архитектура», Ctrl+0, `web/js/workspaces/architect.js`): drop a plan → build; stages; JARVIS's question
  with answer buttons; 2D plan (`architect/plan2d.js`, pure, with the source drawing as an underlay placed by the
  found scale) and 3D (`architect/viewer.js`, vendored three.js 0.186.1, draws only on change) share one selection
  («эту стену» = it); inspector edits go through `architect_edit`; exports; operations. The viewer is never a source
  of truth.
* **Safety**: drawings are data — the prompt says so, the answer must match a strict schema and is validated again,
  nothing from it is executed; names are escaped in the window. Tools write only inside the project folder; risk LOW
  (every change is a revision). Files are served to the window only from the projects folder, by type, sandboxed.

## Consequences
+ The acceptance flow works end to end, by voice and in the window (TEST_MATRIX «JARVIS Architect»).
+ Local-first: everything but the vision step runs on this computer; the vision step uses the existing Claude Code
  sign-in (no new key).
− Not done yet (honest): DWG input; IFC export (needs IfcOpenShell); importing edits made inside FreeCAD's own GUI back
  into the master model (master → CAD is one-way today); curved walls; pitched roofs (flat only); one drawing = one
  level (`add_level` copies the exterior walls up); GLB carries plain colours (procedural textures only in Blender
  renders); a real furniture model library (the hook exists, only procedural models are shipped).
