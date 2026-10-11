// konva-app.js — the same prototype with Konva (vendor/konva.min.js, MIT) as the view layer.
//
// Konva owns the stage (zoom, pan, layers, the polygon preview and brush cursor as nodes);
// maskcore.js still owns every mask pixel. The point of this page is to see what Konva buys
// over plain Canvas 2D for this interface; it exposes the same window.spike API, so
// check/run_check.mjs --page konva.html runs the identical checks against it.

import { Mask, MaskCanvas, MaskSet, paintPolygon, paintStroke, snapToCenter, union } from "./maskcore.js";
import { PALETTE, buildSidebar, checkClassRadio, exportSubmit as exportBody, setClassification as setClassificationIn, setStatus, updateAreas as updateAreasOf } from "./panel.js";

const params = new URLSearchParams(location.search);
const WORK_URL = params.get("work") || "fixtures/work.json";
const ONTOLOGY_URL = params.get("ontology") || "fixtures/ontology.json";
const $ = (id) => document.getElementById(id);
const viewport = $("viewport");

const state = {
  work: null,
  ontology: null,
  image: null,
  masks: null,
  maskCanvases: new Map(),
  priorLayers: [],
  classifications: {},
  activeClass: null,
  tool: "brush",
  erase: false,
  brushDiameter: 12,
  pointer: null,
  stroke: null,
  polygon: [],
  openedAt: performance.timeOrigin + performance.now(),
  opLog: [],
  timings: {},
  showGrid: true,
};

// Konva.pixelRatio: Konva sizes its canvases by devicePixelRatio by default; left as is.
Konva.dragButtons = [1, 2]; // middle / right button always pan; the pan tool adds the left one
const stage = new Konva.Stage({ container: "viewport", width: viewport.clientWidth, height: viewport.clientHeight });
const imageLayer = new Konva.Layer({ listening: false });
const maskLayer = new Konva.Layer({ listening: false, imageSmoothingEnabled: false });
const overlayLayer = new Konva.Layer({ listening: false });
stage.add(imageLayer, maskLayer, overlayLayer);
const polygonPreview = new Konva.Line({ stroke: "#ffe36e", strokeWidth: 1.5, dash: [6, 4], strokeScaleEnabled: false, closed: false });
const vertexGroup = new Konva.Group();
const brushCursor = new Konva.Circle({ stroke: "#fff", strokeWidth: 1, strokeScaleEnabled: false, visible: false });
overlayLayer.add(polygonPreview, vertexGroup, brushCursor);
const priorNodes = []; // Konva.Image per prior layer
const classNodes = new Map();

async function load() {
  const [work, ontology] = await Promise.all([fetchJSON(WORK_URL), fetchJSON(ONTOLOGY_URL)]);
  state.work = work;
  state.ontology = ontology;
  const imageUrl = new URL(work.packet.uri, new URL(WORK_URL, location.href)).href;
  state.image = await loadImage(imageUrl);
  const { naturalWidth: width, naturalHeight: height } = state.image;
  imageLayer.add(new Konva.Image({ image: state.image }));

  const classNames = ontology.masks.map((m) => m.name);
  state.masks = new MaskSet(classNames, width, height);
  for (const prior of work.packet.show_prior ? work.packet.prior_labels : []) {
    classNames.forEach((name, i) => {
      const rle = prior.value.masks?.[name];
      if (!rle) return;
      const t0 = performance.now();
      const mask = Mask.fromRLE(rle);
      state.timings.priorDecodeMs = performance.now() - t0;
      const canvas = new MaskCanvas(mask, PALETTE[i % PALETTE.length], 70);
      const node = new Konva.Image({ image: canvas.canvas });
      maskLayer.add(node);
      priorNodes.push(node);
      state.priorLayers.push({ label: prior.label, stage: prior.stage, name, canvas, visible: true, area: mask.area(), node });
    });
  }
  classNames.forEach((name, i) => {
    const canvas = new MaskCanvas(state.masks.mask(name), PALETTE[i % PALETTE.length], 150);
    state.maskCanvases.set(name, canvas);
    const node = new Konva.Image({ image: canvas.canvas });
    maskLayer.add(node);
    classNodes.set(name, node);
  });
  state.activeClass = classNames[0];
  buildSidebar(state, { selectClass, layerChanged: (layer) => { layer.node.visible(layer.visible); maskLayer.batchDraw(); } });
  fitView();
  setStatus(`Loaded ${work.packet.item} (${width} x ${height}) with Konva ${Konva.version}.`);
}

const fetchJSON = (url) => fetch(url).then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${url}: ${r.status}`))));
const loadImage = (url) =>
  new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error(`cannot load ${url}`));
    img.src = url;
  });

// --- View --------------------------------------------------------------------------------------

function setView(scale, tx, ty) {
  stage.scale({ x: scale, y: scale });
  stage.position({ x: tx, y: ty });
  $("zoom").textContent = `${(scale * 100).toFixed(0)}%`;
  imageLayer.imageSmoothingEnabled(scale < 1);
  stage.batchDraw();
}

function fitView() {
  const { naturalWidth: w, naturalHeight: h } = state.image;
  const scale = Math.min(stage.width() / w, stage.height() / h);
  setView(scale, (stage.width() - w * scale) / 2, (stage.height() - h * scale) / 2);
}

function toImage(clientX, clientY) {
  const rect = stage.container().getBoundingClientRect();
  return [(clientX - rect.left - stage.x()) / stage.scaleX(), (clientY - rect.top - stage.y()) / stage.scaleY()];
}

function toClient(x, y) {
  const rect = stage.container().getBoundingClientRect();
  return { x: rect.left + stage.x() + x * stage.scaleX(), y: rect.top + stage.y() + y * stage.scaleY() };
}

stage.on("wheel", (e) => {
  e.evt.preventDefault();
  const old = stage.scaleX();
  const scale = Math.min(64, Math.max(0.05, old * Math.exp(-e.evt.deltaY * 0.0015)));
  const p = stage.getPointerPosition();
  setView(scale, p.x - ((p.x - stage.x()) * scale) / old, p.y - ((p.y - stage.y()) * scale) / old);
});
stage.on("dragend", () => setView(stage.scaleX(), stage.x(), stage.y()));

// --- Overlay nodes --------------------------------------------------------------------------------

function drawOverlay() {
  const pts = state.polygon.flat();
  if (state.pointer && state.polygon.length) pts.push(...state.pointer);
  polygonPreview.points(pts);
  polygonPreview.stroke(state.erase ? "#ff5050" : "#ffe36e");
  vertexGroup.destroyChildren();
  state.polygon.forEach(([x, y], i) => {
    vertexGroup.add(new Konva.Circle({ x, y, radius: (i === 0 ? 6 : 3.5) / stage.scaleX(), fill: i === 0 ? "#ffe36e" : "#fff" }));
  });
  if (state.tool === "brush" && state.pointer) {
    const [bx, by] = snapToCenter(state.pointer);
    brushCursor.setAttrs({ x: bx, y: by, radius: state.brushDiameter / 2, stroke: state.erase ? "#ff5050" : "#fff", visible: true });
  } else brushCursor.visible(false);
  overlayLayer.batchDraw();
}

// --- Editing (identical to app.js apart from redraw calls) ------------------------------------------

const setClassification = (name, value) => setClassificationIn(state, name, value);
const updateAreas = () => updateAreasOf(state);

function selectClass(name) {
  state.activeClass = name;
  checkClassRadio(name);
  cancelPolygon();
}

function applyStroke(points, diameter, erase) {
  const t0 = performance.now();
  const mask = state.masks.beginEdit(state.activeClass);
  points = points.map(snapToCenter);
  state.masks.touched(paintStroke(mask, points, diameter, erase ? 0 : 1));
  const box = state.masks.endEdit();
  if (box) state.maskCanvases.get(state.activeClass).refresh(box);
  state.timings.lastStrokeMs = performance.now() - t0;
  state.opLog.push({ kind: "stroke", class: state.activeClass, points, diameter, erase });
  updateAreas();
  maskLayer.batchDraw();
}

function applyPolygon(vertices, erase) {
  const t0 = performance.now();
  const mask = state.masks.beginEdit(state.activeClass);
  state.masks.touched(paintPolygon(mask, vertices, erase ? 0 : 1));
  const box = state.masks.endEdit();
  if (box) state.maskCanvases.get(state.activeClass).refresh(box);
  state.timings.lastPolygonMs = performance.now() - t0;
  state.opLog.push({ kind: "polygon", class: state.activeClass, vertices, erase });
  updateAreas();
  maskLayer.batchDraw();
}

function afterHistory(change, kind) {
  if (!change) return;
  state.opLog.push({ kind });
  state.maskCanvases.get(change.name).refresh(change.box);
  updateAreas();
  maskLayer.batchDraw();
}
const undo = () => afterHistory(state.masks.undo(), "undo");
const redo = () => afterHistory(state.masks.redo(), "redo");

function closePolygon() {
  if (state.polygon.length >= 3) applyPolygon(state.polygon.slice(), state.erase);
  state.polygon = [];
  drawOverlay();
}
function cancelPolygon() {
  state.polygon = [];
  drawOverlay();
}

// --- Pointer events through Konva ------------------------------------------------------------------

stage.container().addEventListener("contextmenu", (e) => e.preventDefault());

stage.on("pointerdown", (e) => {
  const ev = e.evt;
  if (!state.image || ev.button !== 0 || state.tool === "pan" || ev.shiftKey) return;
  const p = toImage(ev.clientX, ev.clientY);
  state.pointer = p;
  if (state.tool === "brush") {
    const mask = state.masks.beginEdit(state.activeClass);
    const start = snapToCenter(p);
    state.stroke = { points: [start], mask, t0: performance.now() };
    const dirty = paintStroke(mask, [start], state.brushDiameter, state.erase ? 0 : 1);
    state.masks.touched(dirty);
    state.maskCanvases.get(state.activeClass).refresh(dirty);
    maskLayer.batchDraw();
  } else if (state.tool === "polygon") {
    const first = state.polygon[0];
    if (first && state.polygon.length >= 3) {
      const c = toClient(first[0], first[1]);
      if (Math.hypot(c.x - ev.clientX, c.y - ev.clientY) <= 8) return closePolygon();
    }
    state.polygon.push(p);
    drawOverlay();
  }
});

stage.on("pointermove", (e) => {
  if (!state.image) return;
  const tMove = performance.now();
  const ev = e.evt;
  const events = ev.getCoalescedEvents ? ev.getCoalescedEvents() : [ev];
  let moved = null;
  for (const one of events.length ? events : [ev]) {
    const p = toImage(one.clientX, one.clientY);
    state.pointer = p;
    if (state.stroke) {
      const last = state.stroke.points[state.stroke.points.length - 1];
      const next = snapToCenter(p);
      if (next[0] === last[0] && next[1] === last[1]) continue;
      state.stroke.points.push(next);
      const dirty = paintStroke(state.stroke.mask, [last, next], state.brushDiameter, state.erase ? 0 : 1);
      state.masks.touched(dirty);
      moved = union(moved, dirty);
    }
  }
  if (moved) {
    state.maskCanvases.get(state.activeClass).refresh(moved);
    maskLayer.batchDraw();
  }
  if (state.stroke) {
    state.timings.moveHandlerMsTotal = (state.timings.moveHandlerMsTotal || 0) + performance.now() - tMove;
    state.timings.moveCount = (state.timings.moveCount || 0) + 1;
  }
  drawOverlay();
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
  maskLayer.batchDraw();
}
stage.on("pointerup pointercancel", endStroke);
stage.on("pointerleave", () => {
  state.pointer = null;
  drawOverlay();
});
// Konva's own "dblclick" fires for any two clicks within 400 ms, wherever they land (it checks
// the target shape, not the position), so quick successive vertex clicks would close the
// polygon. The browser's native dblclick requires the clicks to coincide, so use that.
stage.container().addEventListener("dblclick", (e) => {
  if (state.tool !== "polygon") return;
  const p = toImage(e.clientX, e.clientY);
  while (state.polygon.length > 1) {
    const last = state.polygon[state.polygon.length - 1];
    if (Math.hypot(last[0] - p[0], last[1] - p[1]) * stage.scaleX() > 2) break;
    state.polygon.pop();
  }
  closePolygon();
});

window.addEventListener("keydown", (e) => {
  if (["SELECT", "INPUT", "TEXTAREA"].includes(e.target.tagName)) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
    e.preventDefault();
    e.shiftKey ? redo() : undo();
  } else if (e.key === "Enter" && state.tool === "polygon") closePolygon();
  else if (e.key === "Escape") cancelPolygon();
  else if (e.key === "Backspace" && state.polygon.length) {
    state.polygon.pop();
    drawOverlay();
  } else if (e.key === "b") setTool("brush");
  else if (e.key === "p") setTool("polygon");
  else if (e.key === "h") setTool("pan");
  else if (e.key === "e") setErase(!state.erase);
  else if (e.key === "f") fitView();
});

window.addEventListener("resize", () => {
  stage.size({ width: viewport.clientWidth, height: viewport.clientHeight });
  stage.batchDraw();
});

// --- Toolbar ---------------------------------------------------------------------------------------

function setTool(tool) {
  state.tool = tool;
  if (tool !== "polygon") cancelPolygon();
  document.querySelectorAll("[data-tool]").forEach((b) => b.classList.toggle("active", b.dataset.tool === tool));
  stage.draggable(tool === "pan");
  Konva.dragButtons = tool === "pan" ? [0, 1, 2] : [1, 2];
  stage.container().style.cursor = tool === "pan" ? "grab" : "crosshair";
  drawOverlay();
}
function setErase(on) {
  state.erase = on;
  $("erase").checked = on;
  drawOverlay();
}
function setBrush(d) {
  state.brushDiameter = d;
  $("brush-size").value = d;
  $("brush-size-label").textContent = `${d} px`;
  drawOverlay();
}
document.querySelectorAll("[data-tool]").forEach((b) => b.addEventListener("click", () => setTool(b.dataset.tool)));
$("erase").addEventListener("change", (e) => setErase(e.target.checked));
$("brush-size").addEventListener("input", (e) => setBrush(Number(e.target.value)));
$("undo").addEventListener("click", undo);
$("redo").addEventListener("click", redo);
$("fit").addEventListener("click", fitView);
$("grid").addEventListener("change", () => setStatus("The pixel grid is not implemented on the Konva page."));

// Pan drags must not start a stroke, and Konva's drag sets its own cursor; nothing else to do.
stage.draggable(false);

// --- Harness API (same as app.js) -------------------------------------------------------------------

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
    for (const layer of state.priorLayers) if (layer.label === label && layer.name === name) layer.node.visible((layer.visible = visible));
    maskLayer.batchDraw();
  },
  renderNow() {
    const t0 = performance.now();
    stage.draw();
    return (state.timings.lastRenderMs = performance.now() - t0);
  },
  timings: () => ({ ...state.timings }),
  opLog: () => state.opLog,
  area: (name) => state.masks.mask(name).area(),
};
window.spike.ready.catch((err) => setStatus(`Failed to load: ${err.message}`));
