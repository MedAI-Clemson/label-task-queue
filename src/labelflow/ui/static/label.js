// label.js — the labeling page: the image, earlier labels as layers, and the masks being
// edited on one Canvas 2D, with brush and polygon tools, classification controls, and the
// submit. Ported from the interface spike (spikes/mask-tools/app.js + panel.js); every mask
// pixel is computed in maskcore.js, this file only maps pointer events to image coordinates
// and draws.
//
// mount({work, ontology, page}) wires the page's elements (see templates/label.html) and
// returns a small API that the browser test drives (set the view, convert coordinates,
// read areas, export the submit body).
//
// What is reported as "shown": the packet's `shown` exactly as the server gave it. The page
// offers every earlier label of the packet as a layer (a blind stage has none), so the
// packet's list is what was shown; toggling a layer off does not change the report.
//
// "Start from this layer" (one button per earlier label) replaces the editor's masks and
// classifications with that label's, as one undoable step, and the label then reports it as
// `shown.started_from`. Undo of that step takes the start back; "Clear all" does not: the
// labeler did start from it, and the export shows how much of it they kept.

import { Mask, MaskCanvas, MaskSet, paintPolygon, paintStroke, snapToCenter, union } from "./maskcore.js";

export const PALETTE = [
  [66, 135, 245],
  [245, 99, 66],
  [60, 190, 110],
  [200, 80, 200],
  [240, 190, 40],
];

const $ = (id) => document.getElementById(id);

export function mount({ work, ontology, page }) {
  const viewport = $("viewport");
  const canvas = $("canvas");
  const ctx = canvas.getContext("2d");
  const packet = work.packet;

  const state = {
    work,
    ontology,
    image: null,
    masks: null, // MaskSet, when the label task has an ontology
    maskCanvases: new Map(), // class -> MaskCanvas
    priorLayers: [], // {label, stage, name, canvas: MaskCanvas, visible, area}
    classifications: {},
    activeClass: null,
    tool: "brush",
    erase: false,
    brushDiameter: 12,
    scale: 1,
    tx: 0,
    ty: 0,
    pointer: null,
    stroke: null,
    polygon: [],
    panning: null,
    openedAt: performance.timeOrigin + performance.now(),
    opLog: [],
    renderQueued: false,
    showGrid: true,
    submitting: false,
    startedFrom: null, // the earlier label copied into the editor, reported as started_from
  };

  // --- loading ------------------------------------------------------------------------------

  async function load() {
    state.image = await loadImage(page.image_url);
    const { naturalWidth: width, naturalHeight: height } = state.image;
    if (ontology) {
      const classNames = ontology.masks.map((m) => m.name);
      state.masks = new MaskSet(classNames, width, height);
      classNames.forEach((name, i) => {
        state.maskCanvases.set(name, new MaskCanvas(state.masks.mask(name), PALETTE[i % PALETTE.length], 150));
      });
      state.activeClass = classNames[0] || null;
      for (const prior of packet.show_prior ? packet.prior_labels : []) {
        classNames.forEach((name, i) => {
          const rle = prior.value.masks && prior.value.masks[name];
          if (!rle) return;
          let mask;
          try {
            mask = Mask.fromRLE(rle);
          } catch (error) {
            setStatus(`Earlier label ${prior.label}: its ${name} mask could not be decoded (${error.message}).`);
            return;
          }
          const layer = new MaskCanvas(mask, PALETTE[i % PALETTE.length], 70);
          state.priorLayers.push({ label: prior.label, stage: prior.stage, name, canvas: layer, visible: true, area: mask.area(), value: prior.value });
        });
      }
      buildSidebar();
      state.masks.swapMeta = (meta) => {
        const now = editorMeta();
        restoreMeta(meta);
        return now;
      };
    }
    resizeCanvas();
    fitView();
    render();
    setStatus(`${packet.item} (${width} x ${height}), slot ${packet.slot} in stage ${packet.stage}.`);
  }

  function loadImage(url) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error(`The image could not be loaded from ${url}.`));
      img.src = url;
    });
  }

  // --- the side panel -----------------------------------------------------------------------

  function buildSidebar() {
    const classList = $("classes");
    classList.innerHTML = "";
    ontology.masks.forEach((m, i) => {
      const li = document.createElement("li");
      const rgb = PALETTE[i % PALETTE.length];
      li.innerHTML = `<label><input type="radio" name="class" value="${esc(m.name)}" ${i === 0 ? "checked" : ""}>
        <span class="swatch" style="background: rgb(${rgb})"></span>${esc(m.name)}
        <span class="area" id="area-${esc(m.name)}">0 px</span></label>`;
      li.querySelector("input").addEventListener("change", () => selectClass(m.name));
      classList.appendChild(li);
    });

    const classifications = $("classifications");
    classifications.innerHTML = "";
    ontology.classifications.forEach((c) => {
      const div = document.createElement("div");
      const options = ["", ...c.values].map((v) => `<option value="${esc(v)}">${v ? esc(v) : "(not set)"}</option>`).join("");
      div.innerHTML = `<label for="cls-${esc(c.name)}">${esc(c.name)}${c.required ? " <span title=\"required\">*</span>" : ""}</label>
        <select id="cls-${esc(c.name)}" ${c.required ? "required" : ""}>${options}</select>`;
      div.querySelector("select").addEventListener("change", (e) => setClassification(c.name, e.target.value));
      classifications.appendChild(div);
    });

    // Each earlier label: a heading with its "Start from this layer" button, a toggle per
    // mask class, and its classifications as text.
    const layers = $("layers");
    layers.innerHTML = "";
    for (const prior of packet.show_prior ? packet.prior_labels : []) {
      const head = document.createElement("li");
      head.className = "layer-head";
      head.innerHTML = `<span>label ${prior.label} (${esc(prior.stage)})</span>
        <button type="button" data-start="${prior.label}" title="Replace your masks and classifications with label ${prior.label}'s, to edit from there (one Undo step). Your label records that you started from label ${prior.label}; that stays recorded even if you then use Clear all.">Start from this layer</button>`;
      head.querySelector("button").addEventListener("click", () => startFrom(prior.label));
      layers.appendChild(head);
      state.priorLayers.forEach((layer, k) => {
        if (layer.label !== prior.label) return;
        const li = document.createElement("li");
        li.className = "layer-class";
        li.innerHTML = `<label><input type="checkbox" checked data-layer="${k}" data-label="${layer.label}" data-class="${esc(layer.name)}">
          ${esc(layer.name)} <span class="area">${layer.area} px</span></label>`;
        li.querySelector("input").addEventListener("change", (e) => {
          layer.visible = e.target.checked;
          render();
        });
        layers.appendChild(li);
      });
      const values = Object.entries(prior.value.classifications || {});
      if (values.length) {
        const li = document.createElement("li");
        li.className = "layer-class muted";
        li.textContent = values.map(([k, v]) => `${k} = ${v}`).join(", ");
        layers.appendChild(li);
      }
    }

    const flags = $("flags");
    flags.innerHTML = "";
    const consensus = packet.show_prior ? packet.consensus : null;
    if (consensus) {
      const detail = consensus.detail || {};
      for (const [field, d] of Object.entries(detail.classifications || {})) {
        flags.appendChild(flagRow(field, d.agreed, `values ${(d.values || []).join(" / ")}`));
      }
      for (const [field, d] of Object.entries(detail.masks || {})) {
        flags.appendChild(flagRow(field, d.agreed, `min IoU ${d.min_iou} (threshold ${d.threshold})`));
      }
      $("flags-caption").textContent = `Consensus check at stage ${consensus.stage} (rule ${consensus.rule_version}, labels ${consensus.label_ids.join(", ")}): ${consensus.agreed ? "agreed" : "disagreed"}.`;
    } else {
      $("flags-caption").textContent = packet.show_prior ? "No consensus check on this item yet." : "";
    }
  }

  function flagRow(field, agreed, detail) {
    const li = document.createElement("li");
    li.className = agreed ? "agree" : "disagree";
    li.textContent = `${agreed ? "agree" : "DISAGREE"} - ${field}: ${detail}`;
    return li;
  }

  function esc(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  function selectClass(name) {
    state.activeClass = name;
    const radio = document.querySelector(`input[name=class][value="${CSS.escape(name)}"]`);
    if (radio) radio.checked = true;
    cancelPolygon();
  }

  function setClassification(name, value) {
    if (value === "" || value == null) delete state.classifications[name];
    else state.classifications[name] = value;
    const select = $(`cls-${name}`);
    if (select) select.value = value || "";
  }

  function updateAreas() {
    if (!state.masks) return;
    for (const [name, mask] of state.masks.masks) {
      const el = $(`area-${name}`);
      if (el) el.textContent = `${mask.area()} px`;
    }
  }

  function setStatus(text) {
    $("status").textContent = text;
  }

  // --- the view -----------------------------------------------------------------------------

  function resizeCanvas() {
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(viewport.clientWidth * dpr);
    canvas.height = Math.round(viewport.clientHeight * dpr);
    canvas.style.width = `${viewport.clientWidth}px`;
    canvas.style.height = `${viewport.clientHeight}px`;
  }

  function fitView() {
    const { image } = state;
    if (!image) return;
    const scale = Math.min(viewport.clientWidth / image.naturalWidth, viewport.clientHeight / image.naturalHeight);
    setView(scale, (viewport.clientWidth - image.naturalWidth * scale) / 2, (viewport.clientHeight - image.naturalHeight * scale) / 2);
  }

  function setView(scale, tx, ty) {
    state.scale = scale;
    state.tx = tx;
    state.ty = ty;
    const zoom = $("zoom");
    if (zoom) zoom.textContent = `${(scale * 100).toFixed(0)}%`;
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

  // --- rendering ----------------------------------------------------------------------------

  function render() {
    if (state.renderQueued) return;
    state.renderQueued = true;
    requestAnimationFrame(draw);
  }

  function draw() {
    state.renderQueued = false;
    const dpr = window.devicePixelRatio || 1;
    const { image, scale, tx, ty } = state;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = "#202126";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    if (!image) return;
    ctx.setTransform(dpr * scale, 0, 0, dpr * scale, dpr * tx, dpr * ty);
    ctx.imageSmoothingEnabled = scale < 1;
    ctx.drawImage(image, 0, 0);
    ctx.imageSmoothingEnabled = false;
    for (const layer of state.priorLayers) if (layer.visible) ctx.drawImage(layer.canvas.canvas, 0, 0);
    for (const mc of state.maskCanvases.values()) ctx.drawImage(mc.canvas, 0, 0);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (state.showGrid && scale >= 8) drawGrid();
    if (state.polygon.length) drawPolygonPreview();
    if (state.masks && state.tool === "brush" && state.pointer) {
      const [bx, by] = snapToCenter(state.pointer);
      ctx.beginPath();
      ctx.arc(tx + bx * scale, ty + by * scale, (state.brushDiameter / 2) * scale, 0, Math.PI * 2);
      ctx.strokeStyle = state.erase ? "#ff5050" : "#ffffff";
      ctx.lineWidth = 1;
      ctx.stroke();
    }
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
      if (i === 0) ctx.moveTo(tx + x * scale, ty + y * scale);
      else ctx.lineTo(tx + x * scale, ty + y * scale);
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

  // --- editing ------------------------------------------------------------------------------

  function canEdit() {
    return state.masks && state.activeClass;
  }

  function applyStroke(points, diameter, erase) {
    if (!canEdit()) return;
    const mask = state.masks.beginEdit(state.activeClass);
    points = points.map(snapToCenter);
    state.masks.touched(paintStroke(mask, points, diameter, erase ? 0 : 1));
    const box = state.masks.endEdit();
    if (box) state.maskCanvases.get(state.activeClass).refresh(box);
    state.opLog.push({ kind: "stroke", class: state.activeClass, points, diameter, erase });
    updateAreas();
    render();
  }

  function applyPolygon(vertices, erase) {
    if (!canEdit()) return;
    const mask = state.masks.beginEdit(state.activeClass);
    state.masks.touched(paintPolygon(mask, vertices, erase ? 0 : 1));
    const box = state.masks.endEdit();
    if (box) state.maskCanvases.get(state.activeClass).refresh(box);
    state.opLog.push({ kind: "polygon", class: state.activeClass, vertices, erase });
    updateAreas();
    render();
  }

  function undo() {
    if (!state.masks) return;
    const change = state.masks.undo();
    if (change) state.opLog.push({ kind: "undo" });
    if (change && change.changes) setStatus("Undid the last step.");
    afterHistory(change);
  }

  function redo() {
    if (!state.masks) return;
    const change = state.masks.redo();
    if (change) state.opLog.push({ kind: "redo" });
    if (change && change.changes) setStatus("Redid the last step.");
    afterHistory(change);
  }

  function afterHistory(change) {
    if (!change) return;
    for (const { name, box } of change.changes || [change]) state.maskCanvases.get(name).refresh(box);
    updateAreas();
    render();
  }

  // --- starting from an earlier label, and clearing -----------------------------------------

  /** Whether the editor holds anything: a mask pixel or a classification. */
  function hasDrawing() {
    if (Object.keys(state.classifications).length) return true;
    for (const mask of state.masks.masks.values()) if (mask.data.some((v) => v)) return true;
    return false;
  }

  /** What a group step restores besides the masks. */
  function editorMeta() {
    return { classifications: { ...state.classifications }, startedFrom: state.startedFrom };
  }

  function restoreMeta(meta) {
    setClassifications(meta.classifications);
    setStartedFrom(meta.startedFrom);
  }

  function setClassifications(values) {
    state.classifications = {};
    for (const c of ontology.classifications) setClassification(c.name, values[c.name] ?? "");
  }

  function setStartedFrom(label) {
    state.startedFrom = label;
    const note = $("started-from");
    if (!note) return;
    note.hidden = label == null;
    note.textContent =
      label == null
        ? ""
        : `You started from label ${label}. That is recorded with your label, even if you clear everything; Undo of the start takes it back.`;
    note.title = label == null ? "" : "Clearing the editor afterwards does not remove this: you did start from it.";
  }

  /** Replace the masks and classifications with an earlier label's, as one undoable step. */
  function startFrom(labelId, { confirm = true } = {}) {
    const prior = packet.show_prior ? packet.prior_labels.find((p) => p.label === labelId) : null;
    if (!state.masks || !prior) return false;
    cancelPolygon();
    const { width, height } = state.masks;
    const datas = new Map();
    for (const name of state.masks.masks.keys()) {
      const encoded = prior.value.masks && prior.value.masks[name];
      if (!encoded) continue;
      let mask;
      try {
        mask = Mask.fromRLE(encoded);
      } catch (error) {
        setStatus(`Label ${labelId} cannot be copied: its ${name} mask could not be decoded (${error.message}).`);
        return false;
      }
      if (mask.width !== width || mask.height !== height) {
        setStatus(`Label ${labelId} cannot be copied: its ${name} mask is ${mask.width} x ${mask.height}, but the image is ${width} x ${height}.`);
        return false;
      }
      datas.set(name, mask.data);
    }
    if (confirm && hasDrawing() && !window.confirm(`Replace what you have drawn and chosen with label ${labelId}? Undo brings your work back.`)) {
      return false;
    }
    const changes = state.masks.replaceAll(datas, editorMeta());
    const values = {};
    for (const c of ontology.classifications) {
      const value = (prior.value.classifications || {})[c.name];
      if (value != null) values[c.name] = value;
    }
    setClassifications(values);
    setStartedFrom(labelId);
    state.opLog.push({ kind: "start_from", label: labelId });
    afterHistory({ changes });
    setStatus(`Started from label ${labelId} (${prior.stage}): edit it, then submit.`);
    return true;
  }

  /** Erase every mask and classification in one undoable step; started_from stays. */
  function clearAll() {
    if (!state.masks || !hasDrawing()) return false;
    cancelPolygon();
    const changes = state.masks.replaceAll(new Map(), editorMeta());
    setClassifications({});
    state.opLog.push({ kind: "clear_all" });
    afterHistory({ changes });
    const kept = state.startedFrom == null ? "" : ` You still started from label ${state.startedFrom}; that stays recorded.`;
    setStatus(`Cleared every mask and classification (Undo brings them back).${kept}`);
    return true;
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

  function refreshActive(box) {
    if (box) state.maskCanvases.get(state.activeClass).refresh(box);
  }

  // --- pointer events -----------------------------------------------------------------------

  canvas.addEventListener("contextmenu", (e) => e.preventDefault());

  canvas.addEventListener("pointerdown", (e) => {
    if (!state.image) return;
    canvas.setPointerCapture(e.pointerId);
    const p = toImage(e.clientX, e.clientY);
    state.pointer = p;
    const wantsPan = state.tool === "pan" || e.button === 1 || e.button === 2 || e.shiftKey || !canEdit();
    if (wantsPan) {
      state.panning = { startX: e.clientX, startY: e.clientY, tx: state.tx, ty: state.ty };
      return;
    }
    if (e.button !== 0) return;
    if (state.tool === "brush") {
      const mask = state.masks.beginEdit(state.activeClass);
      const start = snapToCenter(p);
      state.stroke = { points: [start], mask };
      const dirty = paintStroke(mask, [start], state.brushDiameter, state.erase ? 0 : 1);
      state.masks.touched(dirty);
      refreshActive(dirty);
      render();
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
    if (state.panning) {
      const { startX, startY, tx, ty } = state.panning;
      setView(state.scale, tx + e.clientX - startX, ty + e.clientY - startY);
      return;
    }
    const events = e.getCoalescedEvents ? e.getCoalescedEvents() : [e];
    let moved = null;
    for (const ev of events.length ? events : [e]) {
      const p = toImage(ev.clientX, ev.clientY);
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
    if (moved) refreshActive(moved);
    render();
  });

  function endStroke() {
    if (!state.stroke) return;
    const { points } = state.stroke;
    state.stroke = null;
    const box = state.masks.endEdit();
    if (box) state.maskCanvases.get(state.activeClass).refresh(box);
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
    if (state.tool !== "polygon") return;
    e.preventDefault();
    const p = toImage(e.clientX, e.clientY);
    while (state.polygon.length > 1) {
      const last = state.polygon[state.polygon.length - 1];
      if (Math.hypot(last[0] - p[0], last[1] - p[1]) * state.scale > 2) break;
      state.polygon.pop();
    }
    closePolygon();
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
      setView(scale, sx - ((sx - state.tx) * scale) / state.scale, sy - ((sy - state.ty) * scale) / state.scale);
    },
    { passive: false },
  );

  window.addEventListener("keydown", (e) => {
    const tag = e.target.tagName;
    if (tag === "SELECT" || tag === "INPUT" || tag === "TEXTAREA" || tag === "BUTTON") return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
      e.preventDefault();
      if (e.shiftKey) redo();
      else undo();
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
    else if (e.key === "]") setBrush(Math.min(100, state.brushDiameter + 1));
    else if (e.key === "f") fitView();
  });

  window.addEventListener("resize", () => {
    resizeCanvas();
    render();
  });

  // --- the toolbar --------------------------------------------------------------------------

  function setTool(tool) {
    state.tool = tool;
    if (tool !== "polygon") cancelPolygon();
    document.querySelectorAll("[data-tool]").forEach((b) => {
      const on = b.dataset.tool === tool;
      b.classList.toggle("active", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
    canvas.style.cursor = tool === "pan" ? "grab" : "crosshair";
    render();
  }

  function setErase(on) {
    state.erase = on;
    if ($("erase")) $("erase").checked = on;
    render();
  }

  function setBrush(d) {
    state.brushDiameter = d;
    if ($("brush-size")) $("brush-size").value = d;
    if ($("brush-size-label")) $("brush-size-label").textContent = `${d} px`;
    render();
  }

  document.querySelectorAll("[data-tool]").forEach((b) => b.addEventListener("click", () => setTool(b.dataset.tool)));
  const on = (id, event, handler) => $(id) && $(id).addEventListener(event, handler);
  on("erase", "change", (e) => setErase(e.target.checked));
  on("brush-size", "input", (e) => setBrush(Number(e.target.value)));
  on("undo", "click", undo);
  on("redo", "click", redo);
  on("clear-all", "click", clearAll);
  on("fit", "click", fitView);
  on("grid", "change", (e) => {
    state.showGrid = e.target.checked;
    render();
  });

  // --- submit -------------------------------------------------------------------------------

  /** The body of POST .../submit: label, shown, duration_ms. */
  function submitBody(compressed = true) {
    let label;
    if (state.masks) {
      label = { classifications: { ...state.classifications }, masks: state.masks.toLabelMasks(compressed) };
    } else {
      label = JSON.parse($("raw-label").value);
    }
    const shown = { ...work.shown };
    if (state.startedFrom != null) shown.started_from = state.startedFrom;
    const duration_ms = Math.max(0, Math.round(performance.timeOrigin + performance.now() - state.openedAt));
    return { label, shown, duration_ms };
  }

  function showProblems(detail, problems) {
    const box = $("submit-problems");
    box.innerHTML = "";
    if (!detail) return;
    const div = document.createElement("div");
    div.className = "problem";
    div.setAttribute("role", "alert");
    const p = document.createElement("p");
    p.textContent = detail;
    div.appendChild(p);
    if (problems && problems.length) {
      const ul = document.createElement("ul");
      for (const problem of problems) {
        const li = document.createElement("li");
        li.textContent = problem;
        ul.appendChild(li);
      }
      div.appendChild(ul);
    }
    box.appendChild(div);
  }

  async function submit() {
    if (state.submitting) return null;
    if (state.masks) {
      const missing = ontology.classifications.filter((c) => c.required && !state.classifications[c.name]).map((c) => c.name);
      if (missing.length) {
        showProblems("The label is not complete.", missing.map((name) => `Choose a value for '${name}'.`));
        return null;
      }
    }
    let body;
    try {
      body = submitBody(true);
    } catch (error) {
      showProblems(`The label is not valid JSON: ${error.message}`, []);
      return null;
    }
    state.submitting = true;
    $("submit").disabled = true;
    setStatus("Submitting…");
    try {
      const response = await fetch(`${page.api_base}/submit`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(body),
      });
      let data = {};
      try {
        data = await response.json();
      } catch (error) {
        data = {};
      }
      if (response.ok) {
        showProblems(null);
        setStatus(`Submitted label ${data.label_id}. Getting your next work…`);
        state.submitted = data.label_id;
        $("next-form").submit();
        return data.label_id;
      }
      if (response.status === 401) {
        showProblems("Your login has expired. Log in again in another tab, then submit again.", []);
      } else {
        showProblems(data.detail || `The server answered ${response.status}.`, data.problems || []);
      }
      setStatus("The label was not accepted.");
    } catch (error) {
      showProblems(`The label could not be sent: ${error.message}`, []);
      setStatus("The label was not sent.");
    } finally {
      state.submitting = false;
      $("submit").disabled = false;
    }
    return null;
  }

  on("submit", "click", submit);

  // --- go -----------------------------------------------------------------------------------

  const ready = load().catch((error) => {
    setStatus(`Failed to load: ${error.message}`);
    showProblems(error.message, []);
    throw error;
  });

  return {
    ready,
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
    startFrom,
    clearAll,
    startedFrom: () => state.startedFrom,
    submit,
    submitBody,
    toggleLayer(label, name, visible) {
      for (const layer of state.priorLayers) {
        if (layer.label === label && layer.name === name) layer.visible = visible;
      }
      render();
    },
    layersShown: () => [...new Set(state.priorLayers.map((l) => l.label))],
    opLog: () => state.opLog,
    area: (name) => (state.masks ? state.masks.mask(name).area() : 0),
  };
}
