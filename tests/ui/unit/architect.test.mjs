// «Архитектура»: the 2D plan is drawn from the master model only (pure function), every element is clickable by id.
import { test } from "node:test";
import assert from "node:assert/strict";
import { planSvg, wallOutline, bounds, describe, findEl } from "../../../web/js/architect/plan2d.js";

const model = {
  name: "Дом", levels: [{ id: "LEVEL_000", name: "Первый этаж" }],
  walls: [
    { id: "WALL_001", type: "wall", kind: "exterior", level: "LEVEL_000", start: [0, 0], end: [9000, 0], thickness: 300, height: 2700, confidence: 0.98, rooms: ["ROOM_001"] },
    { id: "WALL_002", type: "wall", kind: "interior", level: "LEVEL_000", start: [4000, 0], end: [4000, 6000], thickness: 120, height: 2700, confidence: 0.4, rooms: [] },
  ],
  rooms: [{ id: "ROOM_001", type: "room", name: "Кухня", kind: "kitchen", level: "LEVEL_000", polygon: [[0, 0], [4000, 0], [4000, 3000], [0, 3000]], label: [2000, 1500], area_m2: 12, net_area_m2: 10.57 }],
  doors: [{ id: "DOOR_001", type: "door", kind: "door", host: "WALL_001", offset: 1500, width: 900, height: 2100, swing: "left", level: "LEVEL_000", rooms: ["ROOM_001", "outside"] }],
  windows: [{ id: "WINDOW_001", type: "window", host: "WALL_001", offset: 6000, width: 1500, height: 1400, sill: 900, level: "LEVEL_000", confidence: 0.3 }],
  furniture: [{ id: "FURN_001", type: "furniture", kind: "sofa", name: "Диван", position: [2000, 2500], rotation: 0, size: [2100, 950, 850], level: "LEVEL_000", room: "ROOM_001" }],
  columns: [], stairs: [],
};

test("wall outline is the wall's footprint", () => {
  assert.deepEqual(wallOutline(model.walls[0]), [[0, 150], [9000, 150], [9000, -150], [0, -150]]);
  assert.deepEqual(bounds(model), { x0: 0, y0: 0, x1: 9000, y1: 6000 });
});

test("every element of the model is in the plan with its id; doubtful ones are marked", () => {
  const svg = planSvg(model, { selected: "WALL_001" });
  for (const id of ["WALL_001", "WALL_002", "ROOM_001", "DOOR_001", "WINDOW_001", "FURN_001"]) assert.match(svg, new RegExp(`data-id="${id}"`));
  assert.match(svg, /class="ap-wall ext sel"/);
  assert.match(svg, /class="ap-wall low"/);              // confidence 0.4
  assert.match(svg, /class="ap-win low"/);               // confidence 0.3
  assert.match(svg, /10,57 м²/);                         // the net floor area, Russian decimal comma
  assert.match(svg, />9000</);                           // overall size
  assert.doesNotMatch(svg, /<script/);
});

test("names from a drawing are escaped (data, never markup)", () => {
  const m = structuredClone(model);
  m.rooms[0].name = '<img src=x onerror="alert(1)">';
  assert.doesNotMatch(planSvg(m), /<img src=x/);
});

test("underlay is placed by the analysis scale and origin", () => {
  const svg = planSvg(model, { underlay: { url: "/architect/file?p=a/analysis/source.png", w: 1200, h: 900, mm_per_px: 10, origin_px: [150, 750] } });
  assert.match(svg, /<image class="ap-under" href="\/architect\/file\?p=a\/analysis\/source.png" x="-1500" y="-7500" width="12000" height="9000"/);
});

test("descriptions and lookup", () => {
  assert.equal(describe(model, model.doors[0]), "900×2100 мм · Кухня — улица");
  assert.equal(findEl(model, "FURN_001").name, "Диван");
  assert.equal(findEl(model, "NOPE_001"), null);
});
