import { materializeFlowInStudio } from "./flowStudio.js";
import { semanticColor } from "./constants.js";

const API = "/api";

const state = {
  definitions: [],
  flows: [],
  projects: [],
  projectId: null,
  projectFlows: [],
  bindings: [],
  record: null,
  document: null,
  selected: { kind: "flow", id: null },
  pendingSource: null,
  diagnostics: [],
  compiled: null,
  valid: null,
  runnable: null,
  dirty: false,
  analysisTimer: null,
  inputAssets: [],
  inputBindings: {},
  uploadingInputs: {},
  run: null,
  runEvents: null,
};

const el = {};
let definitionsPromise = null;

function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function jsonFetch(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail);
    } catch (_) {}
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  if (response.status === 204) return null;
  return response.json();
}

function blankDocument(name = "Untitled Flow") {
  return {
    flow_id: null,
    name,
    revision: 1,
    schema_version: 1,
    inputs: {},
    nodes: {},
    outputs: {},
    validation: { semantic: "strict" },
    editor: { nodes: {}, viewport: {}, boundaries: { inputs_visible: true, outputs_visible: true } },
  };
}

function setDirty(value = true) {
  state.dirty = value;
  if (el.save) {
    el.save.disabled = !state.document || !state.dirty;
    el.save.textContent = state.dirty ? "Save *" : "Save";
  }
}

function scheduleAnalysis() {
  clearTimeout(state.analysisTimer);
  state.analysisTimer = setTimeout(() => analyze().catch(showError), 250);
}

function markChanged() {
  setDirty(true);
  scheduleAnalysis();
  renderTitle();
}

function uniqueId(base, collection) {
  const stem = String(base || "item").toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "") || "item";
  let candidate = stem;
  let i = 2;
  while (Object.prototype.hasOwnProperty.call(collection, candidate)) candidate = `${stem}_${i++}`;
  return candidate;
}

function definitionById(id) {
  return state.definitions.find((d) => d.id === id) || null;
}

function layoutKey(kind, id) {
  if (kind === "inputs") return "virtual:inputs";
  if (kind === "outputs") return "virtual:outputs";
  return kind === "node" ? id : `${kind}:${id}`;
}

function layoutFor(kind, id, index = 0) {
  const key = layoutKey(kind, id);
  const stored = state.document?.editor?.nodes?.[key];
  if (stored) return stored;

  // Migrate the visual position of pre-M9.2 documents without touching their
  // runtime model: use the first legacy virtual-node position when present.
  if (kind === "inputs") {
    const legacy = Object.entries(state.document?.editor?.nodes || {}).find(([k]) => k.startsWith("input:"));
    if (legacy) return legacy[1];
    return { x: 50, y: 90 };
  }
  if (kind === "outputs") {
    const legacy = Object.entries(state.document?.editor?.nodes || {}).find(([k]) => k.startsWith("output:"));
    if (legacy) return legacy[1];
    return visibleCanvasPosition("right", 0);
  }
  return { x: 430 + (index % 4) * 280, y: 110 + Math.floor(index / 4) * 220 };
}

function setLayout(kind, id, x, y) {
  if (!state.document.editor) state.document.editor = { nodes: {}, viewport: {} };
  if (!state.document.editor.nodes) state.document.editor.nodes = {};
  state.document.editor.nodes[layoutKey(kind, id)] = { x: Math.round(x), y: Math.round(y) };
}

function boundaryVisible(kind) {
  const key = kind === "inputs" ? "inputs_visible" : "outputs_visible";
  return state.document?.editor?.boundaries?.[key] !== false;
}

function setBoundaryVisible(kind, visible) {
  state.document.editor ||= { nodes:{}, viewport:{}, boundaries:{} };
  state.document.editor.boundaries ||= {};
  state.document.editor.boundaries[kind === "inputs" ? "inputs_visible" : "outputs_visible"] = !!visible;
}

function cancelConnection() {
  if (!state.pendingSource) return false;
  state.pendingSource = null;
  el.connectionHint?.classList.add("hidden");
  el.canvas?.classList.remove("connection-active");
  el.canvas?.classList.remove("connection-active");
  renderCanvas();
  return true;
}

function renderProjects() {
  if (el.apply) el.apply.disabled = !state.record;
}

function renderProjectFlows() {
  if (!el.projectList) return;
  if (!state.projectId) {
    el.projectList.innerHTML = `<div class="flow-list-empty">Select a project.</div>`;
    return;
  }
  if (!state.projectFlows.length) {
    el.projectList.innerHTML = `<div class="flow-list-empty">No Project Flows yet.</div>`;
    return;
  }
  el.projectList.innerHTML = state.projectFlows.map((flow) => `
    <button class="flow-list-item ${state.record?.flow_id === flow.flow_id ? "active" : ""}" type="button" data-flow-id="${esc(flow.flow_id)}">
      <span>${esc(flow.name || "Untitled Flow")}</span><small>r${esc(flow.revision)}</small>
    </button>`).join("");
}

function renderBindings() {
  if (!el.bindingList) return;
  if (!state.projectId) {
    el.bindingList.innerHTML = `<div class="flow-list-empty">No active project.</div>`;
    return;
  }
  if (!state.bindings.length) {
    el.bindingList.innerHTML = `<div class="flow-list-empty">Nothing applied yet.</div>`;
    return;
  }
  el.bindingList.innerHTML = state.bindings.map((binding) => `
    <div class="flow-binding-item">
      <div class="row"><strong>${esc(binding.flow_output_id)}</strong><span class="flow-binding-status ${esc(binding.status)}">${esc(binding.status)}</span></div>
      <small>${esc(binding.flow_id)}</small>
      <small>Run ${esc(binding.current_run_id)}</small>
      ${binding.status === "managed" ? `<button type="button" data-detach-binding="${esc(binding.binding_id)}">Detach</button>` : ""}
    </div>`).join("");
}

function renderFlowList() {
  if (!el.list) return;
  if (!state.flows.length) {
    el.list.innerHTML = `<div class="flow-list-empty">No saved flows yet.</div>`;
    return;
  }
  el.list.innerHTML = state.flows.map((flow) => `
    <button class="flow-list-item ${state.record?.flow_id === flow.flow_id ? "active" : ""}" type="button" data-flow-id="${esc(flow.flow_id)}">
      <span>${esc(flow.name || "Untitled Flow")}</span><small>r${esc(flow.revision)}</small>
    </button>`).join("");
}

function renderPalette() {
  if (!el.palette) return;
  const query = (el.paletteSearch?.value || "").trim().toLowerCase();
  const defs = state.definitions.filter((d) => {
    const hay = `${d.label || ""} ${d.id} ${d.category || ""} ${d.description || ""}`.toLowerCase();
    return !query || hay.includes(query);
  });
  if (!defs.length) {
    el.palette.innerHTML = `<div class="flow-palette-empty">No matching nodes.</div>`;
    return;
  }
  const groups = new Map();
  for (const def of defs) {
    const category = def.category || "processing";
    if (!groups.has(category)) groups.set(category, []);
    groups.get(category).push(def);
  }
  el.palette.innerHTML = [...groups.entries()].map(([category, entries]) => `
    <div class="flow-palette-category">${esc(category)}</div>
    ${entries.map((def) => `
      <div class="flow-palette-node ${def.capability?.available === false ? "unavailable" : ""}" draggable="true" data-definition-id="${esc(def.id)}" title="${esc(def.description || def.id)}">
        <strong>${esc(def.label || def.id)}</strong>
        <span>${esc(def.id)}${def.capability?.available === false ? " · unavailable" : ""}</span>
      </div>`).join("")}
  `).join("");
}

function portHtml({ direction, ownerKind, ownerId, portId, displayLabel = null, semantic, connected, sourceRef, artifactType = "audio" }) {
  const action = direction === "output" ? "Start connection" : "Connect here";
  const color = semanticColor(semantic, null, artifactType);
  const style = `--flow-port-color:${color}`;
  const dot = `<button type="button" class="flow-port-dot" style="${style}" data-port-anchor="${esc(`${ownerKind}:${ownerId}:${portId}:${direction}`)}" title="${action}" aria-label="${action}: ${esc(displayLabel || portId)}"></button>`;
  const disconnect = direction === "input" && connected ? `<button type="button" class="flow-port-disconnect" data-disconnect-port title="Disconnect" aria-label="Disconnect ${esc(displayLabel || portId)}">×</button>` : "";
  const text = `<span>${esc(displayLabel || portId)}</span><span class="flow-port-semantic">${esc(semantic || "")}</span>`;
  return `<div class="flow-port ${direction} ${connected ? "connected" : ""}" style="${style}" data-port-direction="${direction}" data-owner-kind="${ownerKind}" data-owner-id="${esc(ownerId)}" data-port-id="${esc(portId)}" ${sourceRef ? `data-source-ref="${esc(sourceRef)}"` : ""}>${direction === "input" ? `${dot}${text}${disconnect}` : `${text}${dot}`}</div>`;
}

function sourceSemanticType(source) {
  if (!source || typeof source !== "string") return null;
  if (source.startsWith("inputs.")) {
    return state.document?.inputs?.[source.slice(7)]?.semantic_type || null;
  }
  const dot = source.indexOf(".");
  if (dot < 1) return null;
  const nodeId = source.slice(0, dot);
  const portId = source.slice(dot + 1);
  const node = state.document?.nodes?.[nodeId];
  const def = definitionById(node?.definition_id);
  return def?.outputs?.[portId]?.semantic_type || null;
}


function executionNodeCard(id, index) {
  const pos = layoutFor("node", id, index);
  const selected = state.selected.kind === "node" && state.selected.id === id ? "selected" : "";
  const node = state.document.nodes[id] || {};
  const def = definitionById(node.definition_id);
  const inputs = def?.inputs || {};
  const outputs = def?.outputs || {};
  return `<article class="flow-node ${selected}" data-node-kind="node" data-node-id="${esc(id)}" style="left:${pos.x}px;top:${pos.y}px">
    <div class="flow-node-head"><strong>${esc(def?.label || id)}</strong><span class="flow-node-type">${esc(id)}</span></div>
    <div class="flow-node-ports">
      ${Object.entries(inputs).map(([portId, port]) => portHtml({ direction:"input", ownerKind:"node", ownerId:id, portId, semantic:port.semantic_type, connected:!!node.inputs?.[portId] })).join("")}
      ${Object.entries(outputs).map(([portId, port]) => portHtml({ direction:"output", ownerKind:"node", ownerId:id, portId, displayLabel:port.label || portId, semantic:port.semantic_type, connected:false, sourceRef:`${id}.${portId}` })).join("")}
      ${!def ? `<div class="flow-port"><span>Unknown definition: ${esc(node.definition_id)}</span></div>` : ""}
    </div>
  </article>`;
}

function virtualIoCard(kind) {
  const isInputs = kind === "inputs";
  if (!boundaryVisible(kind)) return "";
  const entries = Object.entries(isInputs ? state.document.inputs || {} : state.document.outputs || {});
  // Keep both virtual boundary nodes visible even when empty so users can
  // connect a source directly to the Flow Outputs block to create an output.
  const pos = layoutFor(kind, kind, 0);
  const selectedKind = isInputs ? "input" : "output";
  const hasSelectedPort = state.selected.kind === selectedKind && entries.some(([id]) => id === state.selected.id);
  const blockSelected = state.selected.kind === kind;
  const rows = entries.map(([id, value]) => {
    const selected = state.selected.kind === selectedKind && state.selected.id === id ? "selected-port" : "";
    if (isInputs) {
      return portHtml({
        direction:"output",
        ownerKind:"input",
        ownerId:id,
        portId:id,
        semantic:value.semantic_type,
        connected:false,
        sourceRef:`inputs.${id}`,
      }).replace('class="flow-port ', `class="flow-port ${selected} `);
    }
    return portHtml({
      direction:"input",
      ownerKind:"output",
      ownerId:id,
      portId:id,
      displayLabel:value.label || id,
      semantic:sourceSemanticType(value.source) || "unknown_audio",
      connected:!!value.source,
    }).replace('class="flow-port ', `class="flow-port ${selected} `);
  }).join("");
  return `<article class="flow-node virtual io-group ${(hasSelectedPort || blockSelected) ? "selected" : ""}" data-node-kind="${kind}" data-node-id="${kind}" ${!isInputs ? 'data-auto-output-target="true"' : ""} style="left:${pos.x}px;top:${pos.y}px">
    <div class="flow-node-head"><strong>${isInputs ? "Flow Inputs" : "Flow Outputs"}</strong><span class="flow-node-type">virtual · ${entries.length}</span></div>
    <div class="flow-node-ports">${rows || (!isInputs ? '<div class="flow-port-empty">Connect a node output here</div>' : "")}</div>
  </article>`;
}

function renderCanvas() {
  if (!el.nodesLayer) return;
  if (!state.document) {
    el.nodesLayer.innerHTML = "";
    el.empty?.classList.remove("hidden");
    drawEdges();
    return;
  }
  const cards = [];
  const inputsCard = virtualIoCard("inputs");
  if (inputsCard) cards.push(inputsCard);
  Object.keys(state.document.nodes || {}).forEach((id, i) => cards.push(executionNodeCard(id, i)));
  const outputsCard = virtualIoCard("outputs");
  if (outputsCard) cards.push(outputsCard);
  el.nodesLayer.innerHTML = cards.join("");
  el.empty?.classList.toggle("hidden", cards.length > 0);
  requestAnimationFrame(drawEdges);
}

function sourceAnchor(source) {
  if (!source) return null;
  if (source.startsWith("inputs.")) {
    const id = source.slice(7);
    return `[data-port-anchor="input:${CSS.escape(id)}:${CSS.escape(id)}:output"]`;
  }
  const dot = source.indexOf(".");
  if (dot < 1) return null;
  const nodeId = source.slice(0, dot);
  const portId = source.slice(dot + 1);
  return `[data-port-anchor="node:${CSS.escape(nodeId)}:${CSS.escape(portId)}:output"]`;
}

function targetAnchor(kind, ownerId, portId) {
  return `[data-port-anchor="${kind}:${CSS.escape(ownerId)}:${CSS.escape(portId)}:input"]`;
}

function drawPath(svg, a, b, semanticType = null) {
  const canvasRect = el.canvas.getBoundingClientRect();
  const ar = a.getBoundingClientRect();
  const br = b.getBoundingClientRect();
  const x1 = ar.left + ar.width / 2 - canvasRect.left;
  const y1 = ar.top + ar.height / 2 - canvasRect.top;
  const x2 = br.left + br.width / 2 - canvasRect.left;
  const y2 = br.top + br.height / 2 - canvasRect.top;
  const dx = Math.max(70, Math.abs(x2 - x1) * .45);
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("class", "flow-edge");
  path.setAttribute("stroke", semanticColor(semanticType));
  path.setAttribute("d", `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`);
  svg.appendChild(path);
}

function drawEdges() {
  if (!el.edges || !state.document) {
    if (el.edges) el.edges.innerHTML = "";
    return;
  }
  el.edges.innerHTML = "";
  for (const [nodeId, node] of Object.entries(state.document.nodes || {})) {
    for (const [portId, source] of Object.entries(node.inputs || {})) {
      const a = el.canvas.querySelector(sourceAnchor(source) || ".__none");
      const b = el.canvas.querySelector(targetAnchor("node", nodeId, portId));
      if (a && b) drawPath(el.edges, a, b, sourceSemanticType(source));
    }
  }
  for (const [outputId, output] of Object.entries(state.document.outputs || {})) {
    const a = el.canvas.querySelector(sourceAnchor(output.source) || ".__none");
    const b = el.canvas.querySelector(targetAnchor("output", outputId, outputId));
    if (a && b) drawPath(el.edges, a, b, sourceSemanticType(output.source));
  }
}

function setSelected(kind, id) {
  state.selected = { kind, id };
  renderCanvas();
  renderInspector();
}

function field(label, control, help = "") {
  return `<div class="flow-field"><label>${esc(label)}</label>${control}${help ? `<div class="flow-field-help">${esc(help)}</div>` : ""}</div>`;
}

function parameterControl(nodeId, name, def, value) {
  const common = `data-param-name="${esc(name)}"`;
  if (def.choices?.length) {
    return `<select ${common}>${def.choices.map((v) => `<option value="${esc(v)}" ${String(v) === String(value) ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>`;
  }
  if (def.type === "boolean") return `<input type="checkbox" ${common} ${value ? "checked" : ""}>`;
  if (def.type === "integer" || def.type === "number") {
    const step = def.ui?.step ?? (def.type === "integer" ? 1 : "any");
    return `<input type="number" ${common} value="${esc(value ?? "")}" ${def.minimum != null ? `min="${esc(def.minimum)}"` : ""} ${def.maximum != null ? `max="${esc(def.maximum)}"` : ""} step="${esc(step)}">`;
  }
  return `<input type="text" ${common} value="${esc(value ?? "")}" placeholder="${esc(def.ui?.placeholder || "")}">`;
}

function renderInspector() {
  if (!el.inspectorBody) return;
  if (!state.document) {
    el.inspectorTitle.textContent = "Flow";
    el.inspectorBody.innerHTML = `<div class="flow-list-empty">Select a flow to edit it.</div>`;
    return;
  }
  const { kind, id } = state.selected;
  if (kind === "node" && id && state.document.nodes[id]) {
    const node = state.document.nodes[id];
    const def = definitionById(node.definition_id);
    el.inspectorTitle.textContent = id;
    const parameters = def?.parameters || {};
    el.inspectorBody.innerHTML = `
      ${field("Node ID", `<input value="${esc(id)}" disabled>`)}
      ${field("Definition", `<input value="${esc(def?.label || node.definition_id || "")}" disabled>`, node.definition_id || "")}
      ${Object.entries(def?.inputs || {}).map(([portId]) => field(`Input · ${portId}`, `<input value="${esc(node.inputs?.[portId] || "")}" data-binding-port="${esc(portId)}" placeholder="Click a source port, then this input">`)).join("")}
      ${Object.entries(parameters).map(([name, pdef]) => {
        const value = Object.prototype.hasOwnProperty.call(node.params || {}, name) ? node.params[name] : pdef.default;
        return field(pdef.label || name, parameterControl(id, name, pdef, value), pdef.description || "");
      }).join("")}
      ${Object.entries(def?.outputs || {}).map(([portId]) => {
        const retain = node.outputs?.[portId]?.retain || "cache";
        return field(`Retention · ${portId}`, `<select data-node-output-retain="${esc(portId)}"><option value="transient" ${retain === "transient" ? "selected" : ""}>transient</option><option value="cache" ${retain === "cache" ? "selected" : ""}>cache</option><option value="project" ${retain === "project" ? "selected" : ""}>project</option></select>`, "Lifecycle policy for this internal node artifact. Publication remains a Flow Output concern.");
      }).join("")}
      <div class="flow-inspector-actions"><button type="button" data-action="delete-node" class="danger">Delete node</button></div>`;
    return;
  }
  if (kind === "input" && id && state.document.inputs[id]) {
    const input = state.document.inputs[id];
    el.inspectorTitle.textContent = `Input · ${id}`;
    el.inspectorBody.innerHTML = `
      ${field("Input ID", `<input value="${esc(id)}" disabled>`)}
      ${field("Artifact type", `<input data-input-field="type" value="${esc(input.type || "")}">`)}
      ${field("Semantic type", `<input data-input-field="semantic_type" value="${esc(input.semantic_type || "")}">`)}
      <div class="flow-inspector-actions"><button type="button" data-action="delete-input" class="danger">Delete input</button></div>`;
    return;
  }
  if (kind === "outputs") {
    const count = Object.keys(state.document.outputs || {}).length;
    el.inspectorTitle.textContent = "Flow Outputs";
    el.inspectorBody.innerHTML = `
      ${field("Published outputs", `<input value="${count}" disabled>`, "Each row remains a stable FlowOutput identity; this inspector controls the visual boundary block.")}
      <div class="flow-inspector-actions">
        <button type="button" data-action="clear-outputs" class="danger" ${count ? "" : "disabled"}>Delete all outputs</button>
        <button type="button" data-action="delete-output-block" class="danger">Delete output block</button>
      </div>`;
    return;
  }
  if (kind === "output" && id && state.document.outputs[id]) {
    const output = state.document.outputs[id];
    el.inspectorTitle.textContent = `Output · ${id}`;
    el.inspectorBody.innerHTML = `
      ${field("Output ID", `<input value="${esc(id)}" disabled>`, "Stable technical identity used by Studio bindings and reruns.")}
      ${field("Label", `<input data-output-field="label" value="${esc(output.label || "")}" placeholder="${esc(sourceDefaultLabel(output.source) || "e.g. Lead vocals")}">`, "Human-facing Studio lane name. Changing it does not change the stable Output ID.")}
      ${field("Source", `<input data-output-field="source" value="${esc(output.source || "")}" placeholder="Click a source port, then the output port">`)}
      ${field("Published to consumers", `<input data-output-field="publish" type="checkbox" ${output.publish !== false ? "checked" : ""}>`, "Studio will consume only published Flow Outputs.")}
      <div class="flow-inspector-actions"><button type="button" data-action="delete-output" class="danger">Delete output</button></div>`;
    return;
  }
  el.inspectorTitle.textContent = "Flow";
  el.inspectorBody.innerHTML = `
    ${field("Name", `<input data-flow-field="name" value="${esc(state.document.name || "")}">`)}
    ${field("Flow ID", `<input value="${esc(state.record?.flow_id || "Not saved")}" disabled>`)}
    ${field("Schema version", `<input value="${esc(state.document.schema_version || 1)}" disabled>`)}
    ${field("Semantic validation", `<select data-flow-field="semantic"><option value="strict" ${state.document.validation?.semantic === "strict" ? "selected" : ""}>strict</option><option value="warn" ${state.document.validation?.semantic === "warn" ? "selected" : ""}>warn</option><option value="off" ${state.document.validation?.semantic === "off" ? "selected" : ""}>off</option></select>`)}
    ${state.record ? `<div class="flow-inspector-actions"><button type="button" data-action="duplicate-flow">Duplicate</button><button type="button" data-action="delete-flow" class="danger">Delete</button></div>` : ""}`;
}

function renderDiagnostics() {
  if (!el.diagnostics) return;
  const items = state.diagnostics || [];
  el.diagCount.textContent = items.length ? `(${items.length})` : "";
  if (!items.length) {
    el.diagnostics.innerHTML = `<div class="flow-list-empty">No diagnostics.</div>`;
    return;
  }
  el.diagnostics.innerHTML = items.map((d) => {
    const loc = [d.node_id && `node:${d.node_id}`, d.port_id && `port:${d.port_id}`, d.output_id && `output:${d.output_id}`, d.parameter_id && `param:${d.parameter_id}`].filter(Boolean).join(" · ");
    return `<div class="flow-diagnostic ${esc(d.severity)}"><div class="flow-diagnostic-code">${esc(d.source)} · ${esc(d.code)}</div><div>${esc(d.message)}</div>${loc ? `<div class="flow-diagnostic-location">${esc(loc)}</div>` : ""}</div>`;
  }).join("");
}

function paintBadge(element, label, value) {
  if (!element) return;
  element.classList.remove("ok", "bad");
  element.textContent = `${label} ${value == null ? "—" : value ? "yes" : "no"}`;
  if (value != null) element.classList.add(value ? "ok" : "bad");
}


function renderRunPanel() {
  if (!el.runPanel) return;
  const inputs = Object.entries(state.document?.inputs || {});
  const canRunSaved = !!state.record && !state.dirty && state.runnable === true;
  const bindingsComplete = inputs.every(([inputId]) => !!state.inputBindings[inputId]);
  const uploadPending = Object.keys(state.uploadingInputs || {}).length > 0;
  if (el.run) el.run.disabled = !canRunSaved || !bindingsComplete || uploadPending || !!(state.run && !["succeeded","partially_failed","failed","cancelled"].includes(state.run.status));

  const inputHtml = inputs.length ? inputs.map(([inputId, input]) => {
    const options = ['<option value="">Select library track…</option>', ...state.inputAssets.map((asset) =>
      `<option value="${esc(asset.asset_id)}" ${state.inputBindings[inputId] === asset.asset_id ? "selected" : ""}>${esc(asset.title)}</option>`
    )];
    const uploading = !!state.uploadingInputs[inputId];
    return `<div class="flow-run-input"><label>${esc(inputId)}${input?.semantic_type ? ` · ${esc(input.semantic_type)}` : ""}</label><select data-run-input="${esc(inputId)}" ${uploading ? "disabled" : ""}>${options.join("")}</select><div class="flow-run-input-actions"><label class="flow-upload-button ${uploading ? "disabled" : ""}">${uploading ? "Uploading…" : "Choose audio file…"}<input type="file" accept="audio/*,.mp3,.wav,.flac,.mp4,.m4a,.ogg,.opus" data-run-upload="${esc(inputId)}" ${uploading ? "disabled" : ""}></label></div></div>`;
  }).join("") : `<div class="flow-run-empty">This Flow has no inputs.</div>`;

  let runHtml = `<div class="flow-run-empty">Choose a library track or upload an audio file for each Flow Input, then Run.</div>`;
  if (state.run) {
    const terminal = ["succeeded","partially_failed","failed","cancelled"].includes(state.run.status);
    const nodes = (state.run.nodes || []).map((node) => {
      const progress = node.progress == null ? 0 : Math.max(0, Math.min(100, Number(node.progress) <= 1 ? Number(node.progress) * 100 : Number(node.progress)));
      return `<div class="flow-node-run"><div class="row"><strong>${esc(node.node_id)}</strong><span>${esc(node.status)}</span></div>${node.message ? `<small>${esc(node.message)}</small>` : ""}<div class="flow-run-progress"><span style="width:${progress}%"></span></div>${node.cache_hit ? `<div class="flow-cache-hit">cache hit</div>` : ""}</div>`;
    }).join("");
    runHtml = `<div class="flow-run-summary"><div class="row"><span class="flow-run-status">${esc(state.run.status)}</span><small>${esc(state.run.run_id || "")}</small></div>${nodes}<div class="flow-run-actions">${terminal ? `<button type="button" data-run-action="retry">Retry</button>` : `<button type="button" data-run-action="cancel">Cancel</button>`}</div></div>`;
  }
  el.runPanel.innerHTML = inputHtml + runHtml;
}

function closeRunEvents() {
  if (state.runEvents) { state.runEvents.close(); state.runEvents = null; }
}

function watchRun(runId) {
  closeRunEvents();
  const source = new EventSource(`${API}/flow-runs/${encodeURIComponent(runId)}/events`);
  state.runEvents = source;
  source.onmessage = (event) => {
    try {
      state.run = JSON.parse(event.data);
      renderRunPanel();
      if (["succeeded","partially_failed","failed","cancelled"].includes(state.run.status)) {
        closeRunEvents();
        if (state.projectId) loadProjectContext().catch(showError);
      }
    } catch (error) { showError(error); }
  };
  source.onerror = () => {
    // EventSource also reports normal server-side stream closure as an error.
    // Refresh once through the REST snapshot before deciding it is a problem.
    jsonFetch(`/flow-runs/${encodeURIComponent(runId)}`, { cache:"no-store" })
      .then((run) => { state.run = run; renderRunPanel(); if (["succeeded","partially_failed","failed","cancelled"].includes(run.status)) closeRunEvents(); })
      .catch(() => closeRunEvents());
  };
}

async function loadInputAssets() {
  const payload = await jsonFetch("/flow-input-assets", { cache:"no-store" });
  state.inputAssets = payload.assets || [];
  renderRunPanel();
}


async function uploadRunInput(inputId, file) {
  if (!inputId || !file) return;
  state.uploadingInputs[inputId] = true;
  renderRunPanel();
  try {
    const form = new FormData();
    form.append("file", file, file.name);
    const response = await fetch(`${API}/flow-input-assets/upload`, { method:"POST", body:form });
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const payload = await response.json();
        detail = typeof payload.detail === "string" ? payload.detail : (payload.detail?.message || JSON.stringify(payload.detail));
      } catch (_) {}
      throw new Error(detail);
    }
    const payload = await response.json();
    const asset = payload.asset;
    if (!asset?.asset_id) throw new Error("Upload did not return a Flow input asset");
    const existing = state.inputAssets.findIndex((item) => item.asset_id === asset.asset_id);
    if (existing >= 0) state.inputAssets[existing] = asset; else state.inputAssets.unshift(asset);
    state.inputBindings[inputId] = asset.asset_id;
  } finally {
    delete state.uploadingInputs[inputId];
    renderRunPanel();
  }
}

async function startRun() {
  if (!state.record || state.dirty) throw new Error("Save the Flow before running it.");
  const inputIds = Object.keys(state.document?.inputs || {});
  const missing = inputIds.filter((id) => !state.inputBindings[id]);
  if (missing.length) throw new Error(`Select an input asset for: ${missing.join(", ")}`);
  const input_assets = Object.fromEntries(inputIds.map((id) => [id, state.inputBindings[id]]));
  const run = await jsonFetch("/flow-runs", {
    method:"POST",
    body:JSON.stringify({ flow_id:state.record.flow_id, expected_revision:state.record.revision, input_assets }),
  });
  state.run = run;
  renderRunPanel();
  watchRun(run.run_id);
}

async function cancelRun() {
  if (!state.run?.run_id) return;
  state.run = await jsonFetch(`/flow-runs/${encodeURIComponent(state.run.run_id)}/cancel`, { method:"POST" });
  renderRunPanel();
}

async function retryRun() {
  if (!state.run?.run_id) return;
  const run = await jsonFetch(`/flow-runs/${encodeURIComponent(state.run.run_id)}/retry`, { method:"POST" });
  state.run = run;
  renderRunPanel();
  watchRun(run.run_id);
}

function renderTitle() {
  if (!el.title) return;
  el.title.textContent = state.document?.name || "No flow selected";
  el.revision.textContent = state.record ? `revision ${state.record.revision}${state.dirty ? " · unsaved" : ""}` : state.document ? "unsaved draft" : "";
  paintBadge(el.compiled, "compile", state.compiled);
  paintBadge(el.valid, "valid", state.valid);
  paintBadge(el.runnable, "runnable", state.runnable);
}

function renderAll() {
  renderFlowList();
  renderProjects();
  renderProjectFlows();
  renderBindings();
  renderPalette();
  renderCanvas();
  renderInspector();
  renderDiagnostics();
  renderTitle();
  renderRunPanel();
}

function showError(error) {
  console.error("[flow-editor]", error);
  state.diagnostics = [{ source:"editor", severity:"error", code:"editor_error", message:error.message || String(error) }];
  renderDiagnostics();
}

async function loadDefinitions() {
  const payload = await jsonFetch("/node-definitions", { cache: "no-store" });
  state.definitions = payload.definitions || [];
  renderPalette();
  // A Flow may already be open when the registry finishes loading (for
  // example when opened from the top composer Edit button during startup).
  // Re-render so execution-node ports and derived edges can be resolved.
  if (state.document) {
    renderCanvas();
    renderInspector();
    renderDiagnostics();
  }
}

function ensureDefinitionsLoaded() {
  if (state.definitions.length) return Promise.resolve();
  if (!definitionsPromise) {
    definitionsPromise = loadDefinitions().finally(() => { definitionsPromise = null; });
  }
  return definitionsPromise;
}

async function loadFlows() {
  const payload = await jsonFetch("/flows?kind=user", { cache: "no-store" });
  state.flows = payload.flows || [];
  renderFlowList();
}

async function loadProjects() {
  const payload = await jsonFetch("/projects", { cache: "no-store" });
  state.projects = payload.projects || [];
  if (state.projectId && !state.projects.some((project) => project.project_id === state.projectId)) state.projectId = null;
  if (!state.projectId && state.projects.length) state.projectId = state.projects[0].project_id;
  await loadProjectContext();
  renderProjects();
}

async function loadProjectContext() {
  if (!state.projectId) {
    state.projectFlows = [];
    state.bindings = [];
    renderProjectFlows();
    renderBindings();
    renderProjects();
    return;
  }
  const [flowPayload, bindingPayload] = await Promise.all([
    jsonFetch(`/projects/${encodeURIComponent(state.projectId)}/flows`, { cache:"no-store" }),
    jsonFetch(`/projects/${encodeURIComponent(state.projectId)}/studio-bindings`, { cache:"no-store" }),
  ]);
  state.projectFlows = flowPayload.flows || [];
  state.bindings = bindingPayload.bindings || [];
  renderProjectFlows();
  renderBindings();
  renderProjects();
}

async function createProject() {
  const name = prompt("Project name");
  if (!name?.trim()) return;
  const project = await jsonFetch("/projects", { method:"POST", body:JSON.stringify({ name:name.trim() }) });
  state.projectId = project.project_id;
  await loadProjects();
}

async function copyCurrentToProject() {
  if (!state.projectId || !state.record || state.record.kind === "project") return;
  if (state.dirty) throw new Error("Save the current Flow before copying it into a Project.");
  const projectFlow = await jsonFetch(`/projects/${encodeURIComponent(state.projectId)}/flows`, {
    method:"POST",
    body:JSON.stringify({ source_flow_id:state.record.flow_id, name:state.document?.name || state.record.name }),
  });
  await loadProjectContext();
  await openFlow(projectFlow.flow_id);
}

function defaultApplyAsset() {
  const selectedIds = Object.values(state.inputBindings || {});
  return state.inputAssets.find((asset) => selectedIds.includes(asset.asset_id)) || null;
}

function askApplyOptions() {
  const defaultAsset = defaultApplyAsset();
  const defaultTitle = defaultAsset?.title || state.document?.name || "Flow session";
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "flow-apply-dialog";
    dialog.innerHTML = `
      <form method="dialog" class="flow-apply-form">
        <h3>Apply to Studio</h3>
        <label>Session name<input name="title" value="${esc(defaultTitle)}" maxlength="200"></label>
        <fieldset>
          <legend>Destination</legend>
          <label><input type="radio" name="mode" value="new" checked> Create a new Studio session</label>
          <label><input type="radio" name="mode" value="input"> Apply outputs to an existing library track</label>
        </fieldset>
        <label class="flow-apply-target">Input/library track
          <select name="target_asset">
            ${state.inputAssets.map((asset) => `<option value="${esc(asset.asset_id)}" ${asset.asset_id === defaultAsset?.asset_id ? "selected" : ""}>${esc(asset.title)}</option>`).join("")}
          </select>
        </label>
        <div class="flow-apply-actions"><button value="cancel">Cancel</button><button value="apply" class="primary">Apply</button></div>
      </form>`;
    document.body.appendChild(dialog);
    const sync = () => {
      const mode = dialog.querySelector('input[name="mode"]:checked')?.value;
      dialog.querySelector(".flow-apply-target")?.classList.toggle("hidden", mode !== "input");
    };
    dialog.addEventListener("change", sync);
    dialog.addEventListener("close", () => {
      if (dialog.returnValue !== "apply") { dialog.remove(); resolve(null); return; }
      const data = new FormData(dialog.querySelector("form"));
      const mode = String(data.get("mode") || "new");
      const title = String(data.get("title") || defaultTitle).trim() || defaultTitle;
      const assetId = String(data.get("target_asset") || "");
      const targetAsset = state.inputAssets.find((asset) => asset.asset_id === assetId) || null;
      dialog.remove();
      resolve({ mode, title, targetAsset });
    });
    sync();
    dialog.showModal();
  });
}

async function applyLatestRun() {
  if (!state.record) return;
  const runs = await jsonFetch(`/flow-runs?flow_id=${encodeURIComponent(state.record.flow_id)}`, { cache:"no-store" });
  const terminal = new Set(["succeeded", "partially_failed", "failed", "cancelled"]);
  const run = (runs.runs || []).find((item) => terminal.has(item.status));
  if (!run) throw new Error("This Flow has no completed Run to apply.");
  const options = await askApplyOptions();
  if (!options) return;
  if (options.mode === "input" && !options.targetAsset) throw new Error("Choose a library track to receive the Flow outputs.");
  const result = await jsonFetch(`/flow-studio/apply`, {
    method:"POST", body:JSON.stringify({ run_id:run.run_id }),
  });
  const bindingsPayload = await jsonFetch(`/flow-studio/bindings?flow_id=${encodeURIComponent(state.record.flow_id)}`, { cache:"no-store" });
  state.bindings = bindingsPayload.bindings || [];
  await materializeFlowInStudio(state.record, state.bindings, result, options);
  document.getElementById("appMenuBtn")?.click();
}

async function openFlow(flowId) {
  if (state.dirty && !confirm("Discard unsaved Flow changes?")) return;
  // Node definitions are required to render ports and therefore derived
  // edges. Opening from the composer can happen before the registry request
  // started by enterFlowMode() has completed, so wait for it explicitly.
  await ensureDefinitionsLoaded();
  const record = await jsonFetch(`/flows/${encodeURIComponent(flowId)}`, { cache: "no-store" });
  state.record = record;
  state.document = structuredClone(record.document);
  state.selected = { kind:"flow", id:null };
  state.pendingSource = null;
  state.inputBindings = {};
  state.run = null;
  closeRunEvents();
  state.compiled = state.valid = state.runnable = null;
  setDirty(false);
  restoreViewport();
  renderAll();
  await analyze();
}

async function newFlow() {
  if (state.dirty && !confirm("Discard unsaved Flow changes?")) return;
  state.record = null;
  state.document = blankDocument("Untitled Flow");
  state.selected = { kind:"flow", id:null };
  state.pendingSource = null;
  state.inputBindings = {};
  state.run = null;
  closeRunEvents();
  state.compiled = state.valid = state.runnable = null;
  state.diagnostics = [];
  setDirty(true);
  renderAll();
}

async function saveFlow() {
  if (!state.document) return;
  let record;
  if (!state.record) {
    record = await jsonFetch("/flows", { method:"POST", body:JSON.stringify({ document: state.document }) });
  } else {
    try {
      record = await jsonFetch(`/flows/${encodeURIComponent(state.record.flow_id)}`, {
        method:"PUT",
        body:JSON.stringify({ document: state.document, expected_revision: state.record.revision }),
      });
    } catch (error) {
      if (error.status === 409) {
        throw new Error("This Flow changed elsewhere. Reload it before saving to avoid overwriting another revision.");
      }
      throw error;
    }
  }
  state.record = record;
  state.document = structuredClone(record.document);
  setDirty(false);
  await loadFlows();
  window.dispatchEvent(new CustomEvent("stemdeck:flows-changed"));
  renderAll();
}

async function analyze() {
  if (!state.document) return;
  const payload = await jsonFetch("/flows/capabilities", { method:"POST", body:JSON.stringify(state.document) });
  state.compiled = !!payload.compiled;
  state.valid = !!payload.valid;
  state.runnable = !!payload.runnable;
  state.diagnostics = payload.diagnostics || [];
  renderDiagnostics();
  renderTitle();
  renderRunPanel();
}

function addNode(definitionId, x = 480, y = 180) {
  if (!state.document) return;
  const def = definitionById(definitionId);
  if (!def) return;
  const id = uniqueId(def.label || def.id, state.document.nodes);
  const params = {};
  for (const [name, p] of Object.entries(def.parameters || {})) if (p.default != null) params[name] = p.default;
  state.document.nodes[id] = { definition_id:def.id, inputs:{}, params, outputs:{} };
  setLayout("node", id, Math.max(20, x), Math.max(20, y));
  setSelected("node", id);
  markChanged();
}

function visibleCanvasPosition(side, index = 0) {
  const scrollLeft = el.scroller?.scrollLeft || 0;
  const scrollTop = el.scroller?.scrollTop || 0;
  const width = el.scroller?.clientWidth || 1000;
  const x = side === "right"
    ? scrollLeft + Math.max(220, width - 230)
    : scrollLeft + 40;
  const y = scrollTop + 80 + (index % 6) * 100;
  return { x: Math.max(20, x), y: Math.max(20, y) };
}

function addInput() {
  if (!state.document) return;
  const id = uniqueId("input", state.document.inputs);
  state.document.inputs[id] = { type:"audio", semantic_type:"mixture" };
  if (!state.document.editor?.nodes?.[layoutKey("inputs", "inputs")]) {
    const pos = visibleCanvasPosition("left", 0);
    setLayout("inputs", "inputs", pos.x, pos.y);
  }
  setSelected("input", id);
  markChanged();
}

function humanizePortId(value) {
  return String(value || "output")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function sourceDefaultLabel(source) {
  if (!source) return null;
  if (source.startsWith("inputs.")) {
    return humanizePortId(source.slice(7));
  }
  const dot = source.lastIndexOf(".");
  if (dot < 1) return null;
  const nodeId = source.slice(0, dot);
  const portId = source.slice(dot + 1);
  const node = state.document?.nodes?.[nodeId];
  const def = node ? definitionById(node.definition_id) : null;
  return def?.outputs?.[portId]?.label || humanizePortId(portId);
}

function uniqueOutputLabel(baseLabel) {
  const base = String(baseLabel || "Output").trim() || "Output";
  const used = new Set(Object.values(state.document?.outputs || {}).map((output) => String(output.label || "").trim()).filter(Boolean));
  if (!used.has(base)) return base;
  let n = 2;
  while (used.has(`${base} ${n}`)) n += 1;
  return `${base} ${n}`;
}

function addOutput(sourceOverride = undefined) {
  if (!state.document) return;
  // DOM event listeners receive the Event as the first argument. Treat it as
  // "no explicit source" rather than as a flow source reference.
  if (sourceOverride && typeof sourceOverride !== "string") sourceOverride = undefined;
  setBoundaryVisible("outputs", true);
  const id = uniqueId("output", state.document.outputs);
  // An output may intentionally start disconnected. If the user already
  // selected a source port, consume it immediately; otherwise the compiler
  // will report the expected draft diagnostic until a source is connected.
  const source = sourceOverride !== undefined ? sourceOverride : (state.pendingSource || null);
  const defaultLabel = source ? uniqueOutputLabel(sourceDefaultLabel(source)) : null;
  state.document.outputs[id] = { source, label:defaultLabel, publish:true };
  state.pendingSource = null;
  el.connectionHint?.classList.add("hidden");
  el.canvas?.classList.remove("connection-active");
  if (!state.document.editor?.nodes?.[layoutKey("outputs", "outputs")]) {
    const pos = visibleCanvasPosition("right", 0);
    setLayout("outputs", "outputs", pos.x, pos.y);
  }
  setSelected("output", id);
  markChanged();
}

function disconnectReferencesTo(prefix) {
  for (const node of Object.values(state.document.nodes || {})) {
    for (const [port, source] of Object.entries(node.inputs || {})) {
      if (source === prefix || source.startsWith(`${prefix}.`)) delete node.inputs[port];
    }
  }
  for (const output of Object.values(state.document.outputs || {})) {
    if (output.source === prefix || output.source?.startsWith(`${prefix}.`)) output.source = null;
  }
}

function connectPending(kind, ownerId, portId) {
  if (!state.pendingSource || !state.document) return;
  if (kind === "node") {
    state.document.nodes[ownerId].inputs ||= {};
    state.document.nodes[ownerId].inputs[portId] = state.pendingSource;
  } else if (kind === "output") {
    const output = state.document.outputs[ownerId];
    output.source = state.pendingSource;
    // Existing draft outputs inherit a source label only while the user has
    // not provided a label of their own. Custom labels are never overwritten.
    if (!String(output.label || "").trim()) {
      output.label = uniqueOutputLabel(sourceDefaultLabel(state.pendingSource));
    }
  }
  state.pendingSource = null;
  el.connectionHint.classList.add("hidden");
  el.canvas?.classList.remove("connection-active");
  markChanged();
  renderCanvas();
  renderInspector();
}

function beginConnection(source) {
  if (state.pendingSource === source) { cancelConnection(); return; }
  state.pendingSource = source;
  el.connectionHint.innerHTML = `<span>Connecting <strong>${esc(source)}</strong> · highlighted ports are valid drop targets</span><button type="button" data-cancel-connection>Cancel · Esc</button>`;
  el.connectionHint.classList.remove("hidden");
  el.canvas?.classList.add("connection-active");
  renderCanvas();
}

function wireCanvas() {
  el.palette.addEventListener("dragstart", (event) => {
    const item = event.target.closest("[data-definition-id]");
    if (!item) return;
    event.dataTransfer.setData("application/x-stemdeck-flow-node", item.dataset.definitionId);
    event.dataTransfer.effectAllowed = "copy";
  });
  el.palette.addEventListener("dblclick", (event) => {
    const item = event.target.closest("[data-definition-id]");
    if (item) addNode(item.dataset.definitionId);
  });
  el.canvas.addEventListener("dragover", (event) => {
    if (event.dataTransfer.types.includes("application/x-stemdeck-flow-node")) event.preventDefault();
  });
  el.canvas.addEventListener("drop", (event) => {
    const id = event.dataTransfer.getData("application/x-stemdeck-flow-node");
    if (!id) return;
    event.preventDefault();
    const r = el.canvas.getBoundingClientRect();
    addNode(id, event.clientX - r.left - 100, event.clientY - r.top - 20);
  });
  el.canvas.addEventListener("click", (event) => {
    const disconnect = event.target.closest("[data-disconnect-port]");
    if (disconnect) {
      event.stopPropagation();
      const port = disconnect.closest(".flow-port");
      if (port?.dataset.ownerKind === "node") {
        delete (state.document.nodes[port.dataset.ownerId].inputs || {})[port.dataset.portId];
      } else if (port?.dataset.ownerKind === "output") {
        state.document.outputs[port.dataset.ownerId].source = null;
      }
      markChanged(); renderCanvas(); renderInspector();
      return;
    }
    const port = event.target.closest(".flow-port");
    if (port) {
      const onDot = !!event.target.closest(".flow-port-dot");
      if (port.dataset.portDirection === "output") {
        // Selecting a virtual Flow Input must remain possible; the dot starts a
        // connection, while the row opens the inspector. Execution-node output
        // rows select their node using the same convention.
        if (onDot) beginConnection(port.dataset.sourceRef);
        else if (port.dataset.ownerKind === "input") setSelected("input", port.dataset.ownerId);
        else setSelected("node", port.dataset.ownerId);
      } else if (state.pendingSource) {
        connectPending(port.dataset.ownerKind, port.dataset.ownerId, port.dataset.portId);
      } else if (port.dataset.ownerKind === "output") {
        setSelected("output", port.dataset.ownerId);
      } else {
        setSelected("node", port.dataset.ownerId);
      }
      return;
    }
    const node = event.target.closest(".flow-node");
    if (state.pendingSource && node?.dataset.autoOutputTarget === "true") {
      addOutput(state.pendingSource);
      return;
    }
    if (node?.dataset.nodeKind === "node") setSelected("node", node.dataset.nodeId);
    else if (node?.dataset.nodeKind === "outputs") setSelected("outputs", "outputs");
    else if (node?.dataset.nodeKind === "inputs") setSelected("inputs", "inputs");
    else if (state.pendingSource) cancelConnection();
    else setSelected("flow", null);
  });
  el.nodesLayer.addEventListener("pointerdown", (event) => {
    const head = event.target.closest(".flow-node-head");
    const card = event.target.closest(".flow-node");
    if (!head || !card || event.button !== 0) return;
    event.preventDefault();

    // Do not call setSelected() here: it re-renders the canvas and would
    // replace `card` while the pointer gesture is in progress. Keep the DOM
    // node stable until pointerup and update selection without a redraw.
    if (card.dataset.nodeKind === "node") {
      state.selected = { kind:"node", id:card.dataset.nodeId };
      renderInspector();
    } else if (card.dataset.nodeKind === "outputs" || card.dataset.nodeKind === "inputs") {
      state.selected = { kind:card.dataset.nodeKind, id:card.dataset.nodeKind };
      renderInspector();
    }
    el.nodesLayer.querySelectorAll(".flow-node.selected").forEach((node) => node.classList.remove("selected"));
    card.classList.add("selected");

    const startX = event.clientX;
    const startY = event.clientY;
    const left = parseFloat(card.style.left) || 0;
    const top = parseFloat(card.style.top) || 0;
    let moved = false;

    const move = (ev) => {
      if (ev.pointerId !== event.pointerId) return;
      const dx = ev.clientX - startX;
      const dy = ev.clientY - startY;
      if (Math.abs(dx) > 2 || Math.abs(dy) > 2) moved = true;
      const x = Math.max(0, left + dx);
      const y = Math.max(0, top + dy);
      card.style.left = `${x}px`;
      card.style.top = `${y}px`;
      setLayout(card.dataset.nodeKind, card.dataset.nodeId, x, y);
      drawEdges();
    };
    const finish = (ev) => {
      if (ev.pointerId !== event.pointerId) return;
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", finish);
      window.removeEventListener("pointercancel", finish);
      if (moved) markChanged();
    };

    // Window-level listeners make dragging robust even when the pointer leaves
    // the node/canvas and avoid fragile pointer-capture state entirely.
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", finish);
  });
  el.scroller.addEventListener("scroll", () => {
    if (!state.document) return;
    state.document.editor ||= { nodes:{}, viewport:{} };
    state.document.editor.viewport = { ...(state.document.editor.viewport || {}), scroll_left:el.scroller.scrollLeft, scroll_top:el.scroller.scrollTop };
  }, { passive:true });
}

function restoreViewport() {
  requestAnimationFrame(() => {
    const viewport = state.document?.editor?.viewport || {};
    el.scroller.scrollLeft = Number(viewport.scroll_left || 0);
    el.scroller.scrollTop = Number(viewport.scroll_top || 0);
  });
}

function wireInspector() {
  el.inspectorBody.addEventListener("input", (event) => {
    if (!state.document) return;
    const target = event.target;
    if (target.dataset.flowField === "name") state.document.name = target.value;
    else if (target.dataset.flowField === "semantic") state.document.validation.semantic = target.value;
    else if (target.dataset.inputField && state.selected.kind === "input") state.document.inputs[state.selected.id][target.dataset.inputField] = target.value;
    else if (target.dataset.outputField === "label" && state.selected.kind === "output") state.document.outputs[state.selected.id].label = target.value || null;
    else if (target.dataset.outputField === "source" && state.selected.kind === "output") state.document.outputs[state.selected.id].source = target.value || null;
    else if (target.dataset.outputField === "publish" && state.selected.kind === "output") state.document.outputs[state.selected.id].publish = target.checked;
    else if (target.dataset.nodeOutputRetain && state.selected.kind === "node") {
      const node = state.document.nodes[state.selected.id];
      node.outputs ||= {};
      node.outputs[target.dataset.nodeOutputRetain] ||= {};
      node.outputs[target.dataset.nodeOutputRetain].retain = target.value;
    } else if (target.dataset.bindingPort && state.selected.kind === "node") {
      const inputs = state.document.nodes[state.selected.id].inputs ||= {};
      if (target.value) inputs[target.dataset.bindingPort] = target.value; else delete inputs[target.dataset.bindingPort];
    } else if (target.dataset.paramName && state.selected.kind === "node") {
      const node = state.document.nodes[state.selected.id];
      const def = definitionById(node.definition_id)?.parameters?.[target.dataset.paramName];
      let value;
      if (target.type === "checkbox") value = target.checked;
      else if (def?.type === "integer") value = target.value === "" ? null : parseInt(target.value, 10);
      else if (def?.type === "number") value = target.value === "" ? null : parseFloat(target.value);
      else value = target.value;
      node.params ||= {};
      if (value == null) delete node.params[target.dataset.paramName]; else node.params[target.dataset.paramName] = value;
    } else return;
    markChanged();
    renderCanvas();
  });
  el.inspectorBody.addEventListener("click", async (event) => {
    const action = event.target.closest("[data-action]")?.dataset.action;
    if (!action || !state.document) return;
    if (action === "delete-node" && state.selected.kind === "node") {
      const id = state.selected.id; delete state.document.nodes[id]; disconnectReferencesTo(id); state.selected = {kind:"flow",id:null}; markChanged(); renderAll();
    } else if (action === "delete-input" && state.selected.kind === "input") {
      const id = state.selected.id; delete state.document.inputs[id]; disconnectReferencesTo(`inputs.${id}`); state.selected = {kind:"flow",id:null}; markChanged(); renderAll();
    } else if (action === "delete-output" && state.selected.kind === "output") {
      delete state.document.outputs[state.selected.id]; state.selected = {kind:"outputs",id:"outputs"}; markChanged(); renderAll();
    } else if (action === "clear-outputs" && state.selected.kind === "outputs") {
      state.document.outputs = {}; markChanged(); renderAll();
    } else if (action === "delete-output-block" && state.selected.kind === "outputs") {
      state.document.outputs = {}; setBoundaryVisible("outputs", false); state.selected = {kind:"flow",id:null}; markChanged(); renderAll();
    } else if (action === "duplicate-flow" && state.record) {
      const copy = await jsonFetch(`/flows/${encodeURIComponent(state.record.flow_id)}/duplicate`, { method:"POST", body:JSON.stringify({ name:`${state.document.name} Copy` }) });
      await loadFlows(); await openFlow(copy.flow_id);
    } else if (action === "delete-flow" && state.record) {
      if (!confirm(`Delete “${state.document.name}”?`)) return;
      await jsonFetch(`/flows/${encodeURIComponent(state.record.flow_id)}?expected_revision=${state.record.revision}`, { method:"DELETE" });
      state.record = null; state.document = null; state.selected = {kind:"flow",id:null}; state.diagnostics=[]; setDirty(false); await loadFlows(); window.dispatchEvent(new CustomEvent("stemdeck:flows-changed")); renderAll();
    }
  });
}

function enterFlowMode() {
  document.querySelector(".daw")?.classList.add("flow-mode");
  el.flowsBtn?.setAttribute("aria-pressed", "true");
  el.flowsBtn?.classList.add("active");
  if (!state.definitions.length) ensureDefinitionsLoaded().catch(showError);
  if (!state.inputAssets.length) loadInputAssets().catch(showError);
  loadFlows().catch(showError);
}

function leaveFlowMode() {
  closeRunEvents();
  document.querySelector(".daw")?.classList.remove("flow-mode");
  el.flowsBtn?.setAttribute("aria-pressed", "false");
  el.flowsBtn?.classList.remove("active");
}

export function initFlowEditor() {
  Object.assign(el, {
    flowsBtn: document.getElementById("flowsBtn"), list: document.getElementById("flowList"), palette: document.getElementById("flowPalette"), paletteSearch: document.getElementById("flowPaletteSearch"),
    newBtn: document.getElementById("flowNewBtn"), addInput: document.getElementById("flowAddInputBtn"), addOutput: document.getElementById("flowAddOutputBtn"), analyze: document.getElementById("flowAnalyzeBtn"), run: document.getElementById("flowRunBtn"), apply: document.getElementById("flowApplyBtn"), save: document.getElementById("flowSaveBtn"),
    projectSelect: document.getElementById("flowProjectSelect"), projectNew: document.getElementById("flowProjectNewBtn"), copyToProject: document.getElementById("flowCopyToProjectBtn"), projectList: document.getElementById("flowProjectList"), bindingList: document.getElementById("flowBindingList"),
    title: document.getElementById("flowEditorTitle"), revision: document.getElementById("flowRevisionLabel"), compiled: document.getElementById("flowCompiledBadge"), valid: document.getElementById("flowValidBadge"), runnable: document.getElementById("flowRunnableBadge"),
    canvas: document.getElementById("flowCanvas"), scroller: document.getElementById("flowCanvasScroller"), nodesLayer: document.getElementById("flowNodesLayer"), edges: document.getElementById("flowEdges"), empty: document.getElementById("flowCanvasEmpty"), connectionHint: document.getElementById("flowConnectionHint"),
    inspectorTitle: document.getElementById("flowInspectorTitle"), inspectorBody: document.getElementById("flowInspectorBody"), runPanel: document.getElementById("flowRunPanel"), diagnostics: document.getElementById("flowDiagnostics"), diagCount: document.getElementById("flowDiagnosticCount"),
  });
  if (!el.flowsBtn || !el.canvas) return;
  el.flowsBtn.addEventListener("click", enterFlowMode);
  document.querySelectorAll(".sidebar-rail .rail-btn:not(.rail-flows)").forEach((button) => button.addEventListener("click", leaveFlowMode));
  window.addEventListener("stemdeck:open-flow", (event) => {
    const flowId = event.detail?.flowId;
    if (!flowId) return;
    enterFlowMode();
    openFlow(flowId).catch(showError);
  });
  el.newBtn.addEventListener("click", () => newFlow().catch(showError));
  el.addInput.addEventListener("click", addInput);
  el.addOutput.addEventListener("click", () => addOutput());
  el.analyze.addEventListener("click", () => analyze().catch(showError));
  el.run.addEventListener("click", () => startRun().catch(showError));
  el.apply.addEventListener("click", () => applyLatestRun().catch(showError));
  el.save.addEventListener("click", () => saveFlow().catch(showError));
  el.list.addEventListener("click", (event) => {
    const row = event.target.closest("[data-flow-id]");
    if (row) openFlow(row.dataset.flowId).catch(showError);
  });

  el.runPanel.addEventListener("change", (event) => {
    const uploadInputId = event.target?.dataset?.runUpload;
    if (uploadInputId) {
      const file = event.target.files?.[0];
      if (file) uploadRunInput(uploadInputId, file).catch(showError);
      event.target.value = "";
      return;
    }
    const inputId = event.target?.dataset?.runInput;
    if (!inputId) return;
    if (event.target.value) state.inputBindings[inputId] = event.target.value; else delete state.inputBindings[inputId];
    renderRunPanel();
  });
  el.runPanel.addEventListener("click", (event) => {
    const action = event.target.closest("[data-run-action]")?.dataset.runAction;
    if (action === "cancel") cancelRun().catch(showError);
    if (action === "retry") retryRun().catch(showError);
  });
  el.paletteSearch.addEventListener("input", renderPalette);
  wireCanvas();
  el.connectionHint?.addEventListener("click", (event) => { if (event.target.closest("[data-cancel-connection]")) cancelConnection(); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && state.pendingSource) { event.preventDefault(); cancelConnection(); } });
  wireInspector();
  window.addEventListener("resize", drawEdges);
  renderAll();
}
