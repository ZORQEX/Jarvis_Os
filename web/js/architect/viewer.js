// The 3D preview of «Архитектура»: the GLB that Blender exports from the master model (never a model of its own).
// Orbit / pan / zoom, click to select (the object's glTF extras carry jarvis_id), highlight, one level at a time, a
// view from above (orthographic), and a first-person walk inside a room (drag to look, WASD / arrows to move).
// Draws only when something changed (idle = no frames: the window must not burn CPU while nobody looks).
const V = "/static/vendor/three/";

export async function createViewer(host, { onSelect = () => {}, onStatus = () => {} } = {}) {
  const THREE = await import(V + "three.module.js");
  const { OrbitControls } = await import(V + "addons/controls/OrbitControls.js");
  const { GLTFLoader } = await import(V + "addons/loaders/GLTFLoader.js");
  const canvas = document.createElement("canvas");
  canvas.className = "av-canvas";
  canvas.tabIndex = 0;
  host.appendChild(canvas);
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: false, powerPreference: "low-power" });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.AgXToneMapping;
  const scene = new THREE.Scene();
  const bg = getComputedStyle(host).getPropertyValue("--surface-2").trim() || "#1c1f25";
  scene.background = new THREE.Color(bg);
  scene.add(new THREE.HemisphereLight(0xffffff, 0xb4b4bc, 1.7));
  const sun = new THREE.DirectionalLight(0xffffff, 1.8);
  sun.position.set(8, 14, 10);
  scene.add(sun);
  const persp = new THREE.PerspectiveCamera(45, 1, 0.05, 2000);
  const ortho = new THREE.OrthographicCamera(-10, 10, 10, -10, 0.05, 2000);
  let camera = persp;
  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = false;
  controls.screenSpacePanning = true;
  const grid = new THREE.GridHelper(60, 60, 0x555a66, 0x2a2e36);
  grid.material.transparent = true;
  grid.material.opacity = 0.35;
  scene.add(grid);

  const st = { root: null, byId: new Map(), selected: null, mode: "persp", level: null, roof: false, box: null,
    keys: new Set(), yaw: 0, pitch: 0, walkRaf: 0, dirty: true, raf: 0 };

  function render() {
    st.raf = 0;
    renderer.render(scene, camera);
  }
  function invalidate() {
    if (!st.raf) st.raf = requestAnimationFrame(render);
  }
  controls.addEventListener("change", invalidate);

  const ro = new ResizeObserver(() => {
    const w = host.clientWidth || 1, h = host.clientHeight || 1;
    renderer.setSize(w, h, false);
    persp.aspect = w / h;
    persp.updateProjectionMatrix();
    fitOrtho();
    invalidate();
  });
  ro.observe(host);

  function idOf(o) {
    for (let x = o; x; x = x.parent) if (x.userData && x.userData.jarvis_id) return x;
    return null;
  }
  function typeOf(o) {
    return (o.userData && o.userData.jarvis_type) || "";
  }

  async function load(url) {
    onStatus("Загружаю 3D…");
    const gltf = await new GLTFLoader().loadAsync(url);
    if (st.root) {
      scene.remove(st.root);
      st.root.traverse((o) => { o.geometry?.dispose?.(); });
    }
    st.root = gltf.scene;
    st.byId.clear();
    st.root.traverse((o) => {
      if (!o.isMesh) return;
      o.material = Array.isArray(o.material) ? o.material.map((m) => m.clone()) : o.material.clone();   // own materials: highlight one object only
      const top = idOf(o);
      if (top) {
        const id = top.userData.jarvis_id;
        if (!st.byId.has(id)) st.byId.set(id, []);
        st.byId.get(id).push(o);
      }
    });
    scene.add(st.root);
    st.box = new THREE.Box3().setFromObject(st.root);
    applyVisibility();
    if (!st.fitted) { setMode(st.mode, st.target); st.fitted = true; }
    if (st.selected) highlight(st.selected);
    onStatus("");
    invalidate();
    return [...st.byId.keys()];
  }

  function applyVisibility() {
    if (!st.root) return;
    st.root.traverse((o) => {
      const top = idOf(o);
      if (!top || o === st.root) return;
      const t = typeOf(top);
      let vis = true;
      if (st.level && top.userData.level && top.userData.level !== st.level) vis = false;
      if (t === "roof") vis = vis && st.roof && st.mode === "persp" && !st.level;
      if (t === "ceiling") vis = vis && st.mode === "walk";
      top.visible = vis;
    });
    invalidate();
  }

  function center() {
    const b = st.box || new THREE.Box3(new THREE.Vector3(-5, 0, -5), new THREE.Vector3(5, 3, 5));
    return { c: b.getCenter(new THREE.Vector3()), s: b.getSize(new THREE.Vector3()) };
  }
  function fitOrtho() {
    const { s } = center();
    const w = host.clientWidth || 1, h = host.clientHeight || 1;
    const a = w / h;
    const half = Math.max(s.z / 2, s.x / 2 / a) * 1.12 + 0.3;      // the whole footprint, a little margin
    ortho.left = -half * a; ortho.right = half * a; ortho.top = half; ortho.bottom = -half;
    ortho.zoom = 1;
    ortho.updateProjectionMatrix();
  }

  function useCamera(cam) {
    camera = cam;
    controls.object = cam;
  }

  function setMode(mode, target) {
    stopWalk();
    st.mode = mode;
    st.target = target || null;
    const { c, s } = center();
    const R = Math.max(s.x, s.z, 4);
    controls.enabled = true;
    controls.enableRotate = true;
    if (mode === "top") {
      useCamera(ortho);
      fitOrtho();
      ortho.position.set(c.x, c.y + R * 2 + 10, c.z + 0.001);
      ortho.up.set(0, 0, -1);
      controls.target.copy(c);
      controls.enableRotate = false;
      ortho.lookAt(c);
    } else if (mode === "walk") {
      useCamera(persp);
      controls.enabled = false;
      // stand in a corner of the room at eye height, looking across to the far corner (wide lens, like a photo)
      const b = new THREE.Box3();
      const obj = target && st.byId.get(target);
      if (obj) obj.forEach((m) => b.expandByObject(m));
      else b.copy(st.box || new THREE.Box3(new THREE.Vector3(-3, 0, -3), new THREE.Vector3(3, 3, 3)));
      const floor = b.min.y;
      const sx = b.max.x - b.min.x, sz = b.max.z - b.min.z;
      const eye = new THREE.Vector3(b.min.x + sx * 0.2, floor + 1.6, b.max.z - sz * 0.2);
      const look = new THREE.Vector3(b.max.x - sx * 0.15, floor + 1.2, b.min.z + sz * 0.15);
      persp.position.copy(eye);
      persp.fov = 70;
      persp.updateProjectionMatrix();
      persp.up.set(0, 1, 0);
      const d = look.clone().sub(eye);
      st.yaw = Math.atan2(d.x, -d.z);
      st.pitch = Math.atan2(d.y, Math.hypot(d.x, d.z));
      look3();
      canvas.focus();
    } else {
      useCamera(persp);
      persp.fov = 45;
      persp.updateProjectionMatrix();
      persp.up.set(0, 1, 0);
      const t = target && st.byId.get(target);
      let tc = c;
      if (t) {
        const b = new THREE.Box3();
        t.forEach((m) => b.expandByObject(m));
        tc = b.getCenter(new THREE.Vector3());
      }
      persp.position.set(tc.x - R * 0.9, c.y + R * 0.9, tc.z + R * 1.1);
      controls.target.copy(tc);
      persp.lookAt(tc);
    }
    if (mode !== "walk") controls.update();      // update() would pull the walking camera back onto the orbit
    applyVisibility();
  }

  function look3() {
    const dir = new THREE.Vector3(Math.sin(st.yaw) * Math.cos(st.pitch), Math.sin(st.pitch), -Math.cos(st.yaw) * Math.cos(st.pitch));
    persp.lookAt(persp.position.clone().add(dir));
    invalidate();
  }
  function walkLoop() {
    st.walkRaf = 0;
    if (st.mode !== "walk" || !st.keys.size) return;
    const step = 0.06;
    const f = new THREE.Vector3(Math.sin(st.yaw), 0, -Math.cos(st.yaw));
    const r = new THREE.Vector3(Math.cos(st.yaw), 0, Math.sin(st.yaw));
    if (st.keys.has("KeyW") || st.keys.has("ArrowUp")) persp.position.addScaledVector(f, step);
    if (st.keys.has("KeyS") || st.keys.has("ArrowDown")) persp.position.addScaledVector(f, -step);
    if (st.keys.has("KeyA")) persp.position.addScaledVector(r, -step);
    if (st.keys.has("KeyD")) persp.position.addScaledVector(r, step);
    if (st.keys.has("ArrowLeft")) st.yaw -= 0.03;
    if (st.keys.has("ArrowRight")) st.yaw += 0.03;
    look3();
    st.walkRaf = requestAnimationFrame(walkLoop);
  }
  function stopWalk() {
    st.keys.clear();
    if (st.walkRaf) cancelAnimationFrame(st.walkRaf);
    st.walkRaf = 0;
  }
  canvas.addEventListener("keydown", (e) => {
    if (st.mode !== "walk") return;
    if (["KeyW", "KeyA", "KeyS", "KeyD", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(e.code)) {
      e.preventDefault();
      e.stopPropagation();
      st.keys.add(e.code);
      if (!st.walkRaf) st.walkRaf = requestAnimationFrame(walkLoop);
    }
  });
  canvas.addEventListener("keyup", (e) => st.keys.delete(e.code));
  canvas.addEventListener("blur", () => st.keys.clear());

  // click = select; drag in walk mode = look around
  let down = null;
  canvas.addEventListener("pointerdown", (e) => { down = { x: e.clientX, y: e.clientY, yaw: st.yaw, pitch: st.pitch }; canvas.focus(); });
  canvas.addEventListener("pointermove", (e) => {
    if (!down || st.mode !== "walk" || !(e.buttons & 1)) return;
    st.yaw = down.yaw + (e.clientX - down.x) * 0.005;
    st.pitch = Math.max(-1.2, Math.min(1.2, down.pitch - (e.clientY - down.y) * 0.005));
    look3();
  });
  canvas.addEventListener("pointerup", (e) => {
    if (!down) return;
    const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y) > 4;
    down = null;
    if (moved || !st.root) return;
    const r = canvas.getBoundingClientRect();
    const ndc = new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    const ray = new THREE.Raycaster();
    ray.setFromCamera(ndc, camera);
    const hits = ray.intersectObject(st.root, true).filter((h) => {
      for (let x = h.object; x; x = x.parent) if (x.visible === false) return false;
      return true;
    });
    const top = hits.length ? idOf(hits[0].object) : null;
    const id = top ? top.userData.jarvis_id : null;
    select(id);
    onSelect(id);
  });

  function highlight(id) {
    for (const [k, meshes] of st.byId) {
      for (const m of meshes) {
        for (const mat of Array.isArray(m.material) ? m.material : [m.material]) {
          if (!mat.emissive) continue;
          mat.emissive.setHex(k === id ? 0x2f6bff : 0x000000);
          mat.emissiveIntensity = k === id ? 0.55 : 0;
        }
      }
    }
    invalidate();
  }
  function select(id) {
    st.selected = id;
    highlight(id);
  }

  return {
    load,
    setMode,
    select,
    focus: (id) => { select(id); setMode("persp", id); },
    setLevel: (lv) => { st.level = lv || null; applyVisibility(); },
    setRoof: (on) => { st.roof = !!on; applyVisibility(); },
    get mode() { return st.mode; },
    get ids() { return [...st.byId.keys()]; },
    snapshot: () => { render(); return canvas.toDataURL("image/png"); },
    dispose: () => { stopWalk(); ro.disconnect(); controls.dispose(); renderer.dispose(); canvas.remove(); },
  };
}
