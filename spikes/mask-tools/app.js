// app.js — the plain Canvas 2D prototype of the labeling page.
//
// One visible canvas shows the image, the prior-label layers, and the per-class masks being
// edited, under a zoom/pan transform. All mask pixels live in maskcore.js; this file only
// maps pointer events to image coordinates, calls the rasterizers, and draws.
//
// The page exposes `window.spike` so the Playwright harness (check/run_check.mjs) can drive
// it and read back exactly which image-space points each stroke or polygon used.

import { Mask, MaskCanvas, MaskSet, paintPolygon, paintStroke, snapToCenter, union } from "./maskcore.js";
import { PALETTE, buildSidebar, checkClassRadio, exportSubmit as exportBody, setClassification as setClassificationIn, setStatus, updateAreas as updateAreasOf } from "./panel.js";

const params = new URLSearchParams(location.search);
const WORK_URL = params.get("work") || "fixtures/work.json";
const ONTOLOGY_URL = params.get("ontology") || "fixtures/ontology.json";

const $ = (id) => document.getElementById(id);
const viewport = $("viewport");
const canvas = $("canvas");
const ctx = canvas.getContext("2d");

const state = {
  work: null,
  ontology: null,
  image: null,
  masks: null, // MaskSet
  maskCanvases: new Map(), // class -> MaskCanvas
  priorLayers: [], // {label, stage, name, canvas: MaskCanvas, visible}
  classifications: {},
  activeClass: null,
  tool: "brush",
  erase: false,
  brushDiameter: 12,
  scale: 1,
  tx: 0,
  ty: 0,
  pointer: null, // last pointer position in image coords, for the brush cursor
  stroke: null, // {points, dirty} while a brush stroke is in progress
  polygon: [], // vertices of the polygon being drawn
  panning: null,
  openedAt: performance.timeOrigin + performance.now(),
  opLog: [], // every edit in order: stroke, polygon, undo, redo (replayed by check_export.py)
  timings: {},
  renderQueued: false,
  showGrid: true,
};

// --- Loading ---------------------------------------------------------------------------------

async function load() {
  const [work, ontology] = await Promise.all([fetchJSON(WORK_URL), fetchJSON(ONTOLOGY_URL)]);
  state.work = work;
  state.ontology = ontology;
  const packet = work.packet;
  const imageUrl = new URL(packet.uri, new URL(WORK_URL, location.href)).href;
  state.image = await loadImage(imageUrl);
  const { naturalWidth: width, naturalHeight: height } = state.image;

  const classNames = ontology.masks.map((m) => m.name);
  state.masks = new MaskSet(classNames, width, height);
  classNames.forEach((name, i) => {
    state.maskCanvases.set(name, new MaskCanvas(state.masks.mask(name), PALETTE[i % PALETTE.length], 150));
  });
  state.activeClass = classNames[0];

  for (const prior of packet.show_prior ? packet.prior_labels : []) {
    classNames.forEach((name, i) => {
      const rle = prior.value.masks?.[name];
      if (!rle) return;
      const t0 = performance.now();
      const mask = Mask.fromRLE(rle);
      state.timings.priorDecodeMs = performance.now() - t0;
      const layer = new MaskCanvas(mask, PALETTE[i % PALETTE.length], 70);
      state.priorLayers.push({ label: prior.label, stage: prior.stage, name, canvas: layer, visible: true, area: mask.area() });
    });
  }

  buildSidebar(state, { selectClass, layerChanged: render });
  resizeCanvas();
  fitView();
  render();
  setStatus(`Loaded ${packet.item} (${width} x ${height}), slot ${packet.slot} in stage ${packet.stage}.`);
}

function fetchJSON(url) {
  return fetch(url).then((r) => {
    if (!r.ok) throw new Error(`${url}: ${r.status}`);
    return r.json();
  });
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error(`cannot load ${url}`));
    img.src = url;
  });
}

function selectClass(name) {
  state.activeClass = name;
  checkClassRadio(name);
  cancelPolygon();
}

const setClassification = (name, value) => setClassificationIn(state, name, value);
const updateAreas = () => updateAreasOf(state);

// --- View transform ---------------------------------------------------------------------------

function resizeCanvas() {
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(viewport.clientWidth * dpr);
  canvas.height = Math.round(viewport.clientHeight * dpr);
  canvas.style.width = `${viewport.clientWidth}px`;
  canvas.style.height = `${viewport.clientHeight}px`;
}

function fitView() {
  const { image } = state;
  const scale = Math.min(viewport.clientWidth / image.naturalWidth, viewport.clientHeight / image.naturalHeight);
  setView(scale, (viewport.clientWidth - image.naturalWidth * scale) / 2, (viewport.clientHeight - image.naturalHeight * scale) / 2);
}

function setView(scale, tx, ty) {
  state.scale = scale;
  state.tx = tx;
  state.ty = ty;
  $("zoom").textContent = `${(scale * 100).toFixed(0)}%`;
  render();
}

function toImage(clientX, clientY) {
  const rect = canvas.getBoundingClientRect();
  return [(clientX - rect.left - state.tx) / state.scale, (clientY - rect.top - state.ty) / state.scale];
}

function toClient(x, y) {
  const rect = canvas.getBoundingClientRect();
  return { x: rect.left + state.tx + x * state.scale, y: rect.top + state.ty + y * state.scale };
}

// --- Rendering ----------------------------------------------------------------------------------

function render() {
  if (state.renderQueued) return;
  state.renderQueued = true;
  requestAnimationFrame(draw);
}

function draw() {
  state.renderQueued = false;
  const t0 = performance.now();
  const dpr = window.devicePixelRatio || 1;
  const { image, scale, tx, ty } = state;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = "#1b1d22";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  if (!image) return;

  ctx.setTransform(dpr * scale, 0, 0, dpr * scale, dpr * tx, dpr * ty);
  ctx.imageSmoothingEnabled = scale < 1;
  ctx.drawImage(image, 0, 0);
  ctx.imageSmoothingEnabled = false; // mask pixels stay crisp when zoomed in
  for (const layer of state.priorLayers) if (layer.visible) ctx.drawImage(layer.canvas.canvas, 0, 0);
  for (const mc of state.maskCanvases.values()) ctx.drawImage(mc.canvas, 0, 0);

  // Overlays in screen space.
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  if (state.showGrid && scale >= 8) drawGrid();
  if (state.polygon.length) drawPolygonPreview();
  if (state.tool === "brush" && state.pointer) {
    const [bx, by] = snapToCenter(state.pointer);
    ctx.beginPath();
    ctx.arc(tx + bx * scale, ty + by * scale, (state.brushDiameter / 2) * scale, 0, Math.PI * 2);
    ctx.strokeStyle = state.erase ? "#ff5050" : "#ffffff";
    ctx.lineWidth = 1;
    ctx.stroke();
  }
  state.timings.lastRenderMs = performance.now() - t0;
}

function drawGrid() {
  const { image, scale, tx, ty } = state;
  ctx.strokeStyle = "rgba(255,255,255,0.15)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  const x0 = Math.max(0, Math.floor(-tx / scale));
  const x1 = Math.min(image.naturalWidth, Math.ceil((viewport.clientWidth - tx) / scale));
  const y0 = Math.max(0, Math.floor(-ty / scale));
  const y1 = Math.min(image.naturalHeight, Math.ceil((viewport.clientHeight - ty) / scale));
  for (let x = x0; x <= x1; x++) {
    ctx.moveTo(tx + x * scale, ty + y0 * scale);
    ctx.lineTo(tx + x * scale, ty + y1 * scale);
  }
  for (let y = y0; y <= y1; y++) {
    ctx.moveTo(tx + x0 * scale, ty + y * scale);
    ctx.lineTo(tx + x1 * scale, ty + y * scale);
  }
  ctx.stroke();
}

function drawPolygonPreview() {
  const { scale, tx, ty, polygon } = state;
  ctx.beginPath();
  polygon.forEach(([x, y], i) => {
    const sx = tx + x * scale;
    const sy = ty + y * scale;
    if (i === 0) ctx.moveTo(sx, sy);
    else ctx.lineTo(sx, sy);
  });
  if (state.pointer) ctx.lineTo(tx + state.pointer[0] * scale, ty + state.pointer[1] * scale);
  ctx.strokeStyle = state.erase ? "#ff5050" : "#ffe36e";
  ctx.lineWidth = 1.5;
  ctx.setLineDash([6, 4]);
  ctx.stroke();
  ctx.setLineDash([]);
  polygon.forEach(([x, y], i) => {
    ctx.beginPath();
    ctx.arc(tx + x * scale, ty + y * scale, i === 0 ? 6 : 3.5, 0, Math.PI * 2);
    ctx.fillStyle = i === 0 ? "#ffe36e" : "#ffffff";
    ctx.fill();
  });
}

// --- Editing ------------------------------------------------------------------------------------

function applyStroke(points, diameter, erase) {
  const t0 = performance.now();
  const mask = state.masks.beginEdit(state.activeClass);
  points = points.map(snapToCenter);
  const dirty = paintStroke(mask, points, diameter, erase ? 0 : 1);
  state.masks.touched(dirty);
  const box = state.masks.endEdit();
  if (box) state.maskCanvases.get(state.activeClass).refresh(box);
  state.timings.lastStrokeMs = performance.now() - t0;
  state.opLog.push({ kind: "stroke", class: state.activeClass, points, diameter, erase });
  updateAreas();
  render();
}

function applyPolygon(vertices, erase) {
  const t0 = performance.now();
  const mask = state.masks.beginEdit(state.activeClass);
  const dirty = paintPolygon(mask, vertices, erase ? 0 : 1);
  state.masks.touched(dirty);
  const box = state.masks.endEdit();
  if (box) state.maskCanvases.get(state.activeClass).refresh(box);
  state.timings.lastPolygonMs = performance.now() - t0;
  state.opLog.push({ kind: "polygon", class: state.activeClass, vertices, erase });
  updateAreas();
  render();
}

function undo() {
  const change = state.masks.undo();
  if (change) state.opLog.push({ kind: "undo" });
  afterHistory(change);
}

function redo() {
  const change = state.masks.redo();
  if (change) state.opLog.push({ kind: "redo" });
  afterHistory(change);
}

function afterHistory(change) {
  if (!change) return;
  state.maskCanvases.get(change.name).refresh(change.box);
  updateAreas();
  render();
}

function closePolygon() {
  if (state.polygon.length >= 3) applyPolygon(state.polygon.slice(), state.erase);
  state.polygon = [];
  render();
}

function cancelPolygon() {
  state.polygon = [];
  render();
}

// --- Pointer events -----------------------------------------------------------------------------

canvas.addEventListener("contextmenu", (e) => e.preventDefault());

canvas.addEventListener("pointerdown", (e) => {
  if (!state.image) return;
  canvas.setPointerCapture(e.pointerId);
  const p = toImage(e.clientX, e.clientY);
  state.pointer = p;
  const wantsPan = state.tool === "pan" || e.button === 1 || e.button === 2 || e.shiftKey;
  if (wantsPan) {
    state.panning = { startX: e.clientX, startY: e.clientY, tx: state.tx, ty: state.ty };
    return;
  }
  if (e.button !== 0) return;
  if (state.tool === "brush") {
    const mask = state.masks.beginEdit(state.activeClass);
    const start = snapToCenter(p);
    state.stroke = { points: [start], mask, t0: performance.now() };
    const dirty = paintStroke(mask, [start], state.brushDiameter, state.erase ? 0 : 1);
    state.masks.touched(dirty);
    refreshActive(dirty);
  } else if (state.tool === "polygon") {
    const first = state.polygon[0];
    if (first && state.polygon.length >= 3) {
      const c = toClient(first[0], first[1]);
      if (Math.hypot(c.x - e.clientX, c.y - e.clientY) <= 8) {
        closePolygon();
        return;
      }
    }
    state.polygon.push(p);
    render();
  }
});

canvas.addEventListener("pointermove", (e) => {
  if (!state.image) return;
  const tMove = performance.now();
  if (state.panning) {
    const { startX, startY, tx, ty } = state.panning;
    setView(state.scale, tx + e.clientX - startX, ty + e.clientY - startY);
    return;
  }
  const events = e.getCoalescedEvents ? e.getCoalescedEvents() : [e];
  let moved = null; // the box touched by this event only, so repainting stays cheap
  for (const ev of events.length ? events : [e]) {
    const p = toImage(ev.clientX, ev.clientY);
    state.pointer = p;
    if (state.stroke) {
      const last = state.stroke.points[state.stroke.points.length - 1];
      const next = snapToCenter(p);
      if (next[0] === last[0] && next[1] === last[1]) continue; // still in the same pixel
      state.stroke.points.push(next);
      const dirty = paintStroke(state.stroke.mask, [last, next], state.brushDiameter, state.erase ? 0 : 1);
      state.masks.touched(dirty);
      moved = union(moved, dirty);
    }
  }
  if (moved) refreshActive(moved);
  if (state.stroke) {
    // Time spent in this handler (paint + repaint of the touched box), for the perf notes.
    state.timings.moveHandlerMsTotal = (state.timings.moveHandlerMsTotal || 0) + performance.now() - tMove;
    state.timings.moveCount = (state.timings.moveCount || 0) + 1;
  }
  render();
});

function endStroke() {
  if (!state.stroke) return;
  const { points, t0 } = state.stroke;
  state.stroke = null;
  const box = state.masks.endEdit();
  if (box) state.maskCanvases.get(state.activeClass).refresh(box);
  state.timings.lastStrokeMs = performance.now() - t0;
  state.opLog.push({ kind: "stroke", class: state.activeClass, points, diameter: state.brushDiameter, erase: state.erase });
  updateAreas();
  render();
}

canvas.addEventListener("pointerup", () => {
  state.panning = null;
  endStroke();
});
canvas.addEventListener("pointercancel", () => {
  state.panning = null;
  endStroke();
});
canvas.addEventListener("pointerleave", () => {
  state.pointer = null;
  render();
});
canvas.addEventListener("dblclick", (e) => {
  if (state.tool === "polygon") {
    e.preventDefault();
    // The two clicks of the double-click added two vertices at the same place; drop them.
    const p = toImage(e.clientX, e.clientY);
    while (state.polygon.length > 1) {
      const last = state.polygon[state.polygon.length - 1];
      if (Math.hypot(last[0] - p[0], last[1] - p[1]) * state.scale > 2) break;
      state.polygon.pop();
    }
    closePolygon();
  }
});

canvas.addEventListener(
  "wheel",
  (e) => {
    e.preventDefault();
    const factor = Math.exp(-e.deltaY * 0.0015);
    const scale = Math.min(64, Math.max(0.05, state.scale * factor));
    const rect = canvas.getBoundingClientRect();
    const sx = e.clientX - rect.left;
    const sy = e.clientY - rect.top;
    // Keep the image point under the cursor fixed.
    const tx = sx - ((sx - state.tx) * scale) / state.scale;
    const ty = sy - ((sy - state.ty) * scale) / state.scale;
    setView(scale, tx, ty);
  },
  { passive: false },
);

// Only the active class changes during a stroke; repaint just the box the latest segment touched.
function refreshActive(box) {
  if (box) state.maskCanvases.get(state.activeClass).refresh(box);
}

window.addEventListener("keydown", (e) => {
  if (e.target.tagName === "SELECT" || e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
    e.preventDefault();
    e.shiftKey ? redo() : undo();
  } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "y") {
    e.preventDefault();
    redo();
  } else if (e.key === "Enter" && state.tool === "polygon") closePolygon();
  else if (e.key === "Escape") cancelPolygon();
  else if (e.key === "Backspace" && state.polygon.length) {
    state.polygon.pop();
    render();
  } else if (e.key === "b") setTool("brush");
  else if (e.key === "p") setTool("polygon");
  else if (e.key === "h") setTool("pan");
  else if (e.key === "e") setErase(!state.erase);
  else if (e.key === "[") setBrush(Math.max(1, state.brushDiameter - 1));
  else if (e.key === "]") setBrush(state.brushDiameter + 1);
  else if (e.key === "f") fitView();
});

window.addEventListener("resize", () => {
  resizeCanvas();
  render();
});

// --- Toolbar ------------------------------------------------------------------------------------

function setTool(tool) {
  state.tool = tool;
  if (tool !== "polygon") cancelPolygon();
  document.querySelectorAll("[data-tool]").forEach((b) => b.classList.toggle("active", b.dataset.tool === tool));
  canvas.style.cursor = tool === "pan" ? "grab" : "crosshair";
  render();
}

function setErase(on) {
  state.erase = on;
  $("erase").checked = on;
  render();
}

function setBrush(d) {
  state.brushDiameter = d;
  $("brush-size").value = d;
  $("brush-size-label").textContent = `${d} px`;
  render();
}

document.querySelectorAll("[data-tool]").forEach((b) => b.addEventListener("click", () => setTool(b.dataset.tool)));
$("erase").addEventListener("change", (e) => setErase(e.target.checked));
$("brush-size").addEventListener("input", (e) => setBrush(Number(e.target.value)));
$("undo").addEventListener("click", undo);
$("redo").addEventListener("click", redo);
$("fit").addEventListener("click", fitView);
$("grid").addEventListener("change", (e) => {
  state.showGrid = e.target.checked;
  render();
});
// --- Harness API ----------------------------------------------------------------------------------

window.spike = {
  ready: load(),
  state,
  setTool,
  setErase,
  setBrush,
  selectClass,
  setClassification,
  setView,
  fitView,
  toClient,
  toImage,
  applyStroke,
  applyPolygon,
  undo,
  redo,
  closePolygon,
  export: (compressed) => exportBody(state, compressed),
  toggleLayer(label, name, visible) {
    for (const layer of state.priorLayers) {
      if (layer.label === label && layer.name === name) layer.visible = visible;
    }
    render();
  },
  renderNow() {
    draw();
    return state.timings.lastRenderMs;
  },
  timings: () => ({ ...state.timings }),
  opLog: () => state.opLog,
  area: (name) => state.masks.mask(name).area(),
};

window.spike.ready.catch((err) => setStatus(`Failed to load: ${err.message}`));
