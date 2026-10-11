// panel.js — the sidebar (class picker, classifications, prior-label layers, agreement flags)
// and the export of the submit body. Shared by index.html (Canvas 2D) and konva.html so the
// two prototypes differ only in how they draw and handle the pointer.

export const PALETTE = [
  [66, 135, 245],
  [245, 99, 66],
  [60, 190, 110],
  [200, 80, 200],
  [240, 190, 40],
];

const $ = (id) => document.getElementById(id);

/** Fill the sidebar from `state` (work, ontology, priorLayers, classifications). */
export function buildSidebar(state, hooks) {
  const { ontology, work } = state;

  const classList = $("classes");
  classList.innerHTML = "";
  ontology.masks.forEach((m, i) => {
    const li = document.createElement("li");
    const rgb = PALETTE[i % PALETTE.length];
    li.innerHTML = `<label><input type="radio" name="class" value="${m.name}" ${i === 0 ? "checked" : ""}>
      <span class="swatch" style="background: rgb(${rgb})"></span> ${m.name}
      <span class="area" id="area-${m.name}">0 px</span></label>`;
    li.querySelector("input").addEventListener("change", () => hooks.selectClass(m.name));
    classList.appendChild(li);
  });

  const classifications = $("classifications");
  classifications.innerHTML = "";
  ontology.classifications.forEach((c) => {
    const div = document.createElement("div");
    const options = ["", ...c.values].map((v) => `<option value="${v}">${v || "(not set)"}</option>`).join("");
    div.innerHTML = `<label>${c.name}${c.required ? " *" : ""} <select id="cls-${c.name}">${options}</select></label>`;
    div.querySelector("select").addEventListener("change", (e) => setClassification(state, c.name, e.target.value));
    classifications.appendChild(div);
  });

  const layers = $("layers");
  layers.innerHTML = "";
  if (!work.packet.show_prior) layers.innerHTML = "<li>This stage is blind: no earlier labels.</li>";
  state.priorLayers.forEach((layer, k) => {
    const li = document.createElement("li");
    li.innerHTML = `<label><input type="checkbox" checked data-layer="${k}"> label ${layer.label} (${layer.stage}) / ${layer.name}
      <span class="area">${layer.area} px</span></label>`;
    li.querySelector("input").addEventListener("change", (e) => {
      layer.visible = e.target.checked;
      hooks.layerChanged(layer);
    });
    layers.appendChild(li);
  });

  const flags = $("flags");
  flags.innerHTML = "";
  const consensus = work.packet.consensus;
  if (consensus) {
    for (const [field, d] of Object.entries(consensus.detail.classifications)) {
      flags.appendChild(flagRow(field, d.agreed, `values ${d.values.join(" / ")}`));
    }
    for (const [field, d] of Object.entries(consensus.detail.masks)) {
      flags.appendChild(flagRow(field, d.agreed, `min IoU ${d.min_iou} (threshold ${d.threshold})`));
    }
    $("flags-caption").textContent = `Consensus check at stage ${consensus.stage}, rule ${consensus.rule_version}, labels ${consensus.label_ids.join(", ")}: ${consensus.agreed ? "agreed" : "disagreed"}.`;
  } else {
    $("flags-caption").textContent = "No consensus check on this item yet.";
  }

  $("export").addEventListener("click", () => {
    const body = exportSubmit(state, $("compressed").checked);
    $("output").value = JSON.stringify(body, null, 1);
    setStatus(`Exported in ${state.timings.lastExportMs.toFixed(1)} ms; ${JSON.stringify(body).length} bytes.`);
  });
}

function flagRow(field, agreed, detail) {
  const li = document.createElement("li");
  li.className = agreed ? "agree" : "disagree";
  li.textContent = `${agreed ? "agree" : "DISAGREE"} - ${field}: ${detail}`;
  return li;
}

export function checkClassRadio(name) {
  const radio = document.querySelector(`input[name=class][value="${name}"]`);
  if (radio) radio.checked = true;
}

export function setClassification(state, name, value) {
  if (value === "" || value == null) delete state.classifications[name];
  else state.classifications[name] = value;
  const select = $(`cls-${name}`);
  if (select) select.value = value || "";
}

export function updateAreas(state) {
  for (const [name, mask] of state.masks.masks) {
    const el = $(`area-${name}`);
    if (el) el.textContent = `${mask.area()} px`;
  }
}

export function setStatus(text) {
  $("status").textContent = text;
}

/** The body of POST .../slots/{slot}/submit: label, shown, duration_ms. */
export function exportSubmit(state, compressed = true) {
  const t0 = performance.now();
  const label = {
    classifications: { ...state.classifications },
    masks: state.masks.toLabelMasks(compressed),
  };
  const shown = {
    ...state.work.shown,
    // Report the earlier labels that were offered on this page (every prior layer of the
    // packet); a blind stage has none. The packet's content hash is reported as given.
    prior_label_ids: [...new Set(state.priorLayers.map((l) => l.label))],
  };
  const duration_ms = Math.round(performance.timeOrigin + performance.now() - state.openedAt);
  state.timings.lastExportMs = performance.now() - t0;
  return { label, shown, duration_ms };
}
