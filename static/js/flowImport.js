let flows = [];
let selectedFlowId = "";
let selectEl = null;
let editEl = null;

async function json(path, options = {}) {
  const response = await fetch(`/api${path}`, options);
  let payload = null;
  try { payload = await response.json(); } catch (_) {}
  if (!response.ok) {
    const detail = payload?.detail;
    const message = typeof detail === "string" ? detail : detail?.message || JSON.stringify(detail || response.statusText);
    throw new Error(message);
  }
  return payload;
}

export async function refreshComposerFlows() {
  if (!selectEl) return;
  const payload = await json("/flows?kind=user", { cache:"no-store" });
  flows = payload.flows || [];
  const current = selectedFlowId;
  selectEl.innerHTML = [
    '<option value="">Standard extraction</option>',
    ...flows.map((flow) => `<option value="${String(flow.flow_id).replace(/"/g,"&quot;")}">${String(flow.name || "Untitled Flow").replace(/</g,"&lt;")}</option>`),
  ].join("");
  if (flows.some((flow) => flow.flow_id === current)) selectEl.value = current;
  else selectedFlowId = "";
  editEl.disabled = !selectedFlowId;
}

export function selectedComposerFlowId() { return selectedFlowId; }

export function initComposerFlowPicker() {
  selectEl = document.getElementById("composerFlowSelect");
  editEl = document.getElementById("composerFlowEditBtn");
  if (!selectEl || !editEl) return;
  selectEl.addEventListener("change", () => {
    selectedFlowId = selectEl.value || "";
    editEl.disabled = !selectedFlowId;
    document.getElementById("job-form")?.classList.toggle("flow-selected", !!selectedFlowId);
    window.dispatchEvent(new CustomEvent("stemdeck:composer-flow-changed", { detail:{ flowId:selectedFlowId } }));
  });
  editEl.addEventListener("click", () => {
    if (!selectedFlowId) return;
    window.dispatchEvent(new CustomEvent("stemdeck:open-flow", { detail:{ flowId:selectedFlowId } }));
  });
  window.addEventListener("stemdeck:flows-changed", () => refreshComposerFlows().catch((error) => console.warn("[flow-import] failed to refresh flows", error)));
  refreshComposerFlows().catch((error) => console.warn("[flow-import] failed to load flows", error));
}

function titleFromFile(file) {
  return String(file?.name || "Flow result").replace(/\.[^.]+$/, "") || "Flow result";
}

async function waitForRun(runId) {
  return new Promise((resolve, reject) => {
    const es = new EventSource(`/api/flow-runs/${encodeURIComponent(runId)}/events`);
    const done = new Set(["succeeded", "partially_failed", "failed", "cancelled"]);
    es.onmessage = (event) => {
      try {
        const run = JSON.parse(event.data);
        if (!done.has(run.status)) return;
        es.close();
        if (run.status === "succeeded" || run.status === "partially_failed") resolve(run);
        else reject(new Error(`Flow Run ${run.status}`));
      } catch (error) { es.close(); reject(error); }
    };
    es.onerror = () => { es.close(); reject(new Error("Flow Run event stream disconnected")); };
  });
}

/** Run the selected reusable Flow directly from StemDeck's normal import bar.
 * M9.7 deliberately accepts one local file + one-input Flow only. Multi-input
 * graphs remain unambiguous in the dedicated Flow Run panel.
 */
export async function runSelectedFlowImport(file) {
  if (!selectedFlowId) return false;
  if (!file) throw new Error("Choose or drop a local audio file to run this Flow. URL Flow imports will be added separately.");
  const flow = await json(`/flows/${encodeURIComponent(selectedFlowId)}`, { cache:"no-store" });
  const inputIds = Object.keys(flow.document?.inputs || {});
  if (inputIds.length !== 1) {
    throw new Error(`This Flow has ${inputIds.length} inputs. Run it from the Flows page so each input can be selected explicitly.`);
  }
  const form = new FormData();
  form.append("file", file);
  const upload = await json("/flow-input-assets/upload", { method:"POST", body:form });
  const assetId = upload.asset?.asset_id;
  if (!assetId) throw new Error("Flow input upload did not return an asset id");
  const run = await json("/flow-runs", {
    method:"POST",
    headers:{ "Content-Type":"application/json" },
    body:JSON.stringify({
      flow_id:selectedFlowId,
      expected_revision:flow.revision,
      input_assets:{ [inputIds[0]]:assetId },
    }),
  });
  const finalRun = await waitForRun(run.run_id);
  const applyResult = await json("/flow-studio/apply", {
    method:"POST",
    headers:{ "Content-Type":"application/json" },
    body:JSON.stringify({ run_id:finalRun.run_id }),
  });
  const bindingPayload = await json(`/flow-studio/bindings?flow_id=${encodeURIComponent(selectedFlowId)}`, { cache:"no-store" });
  const { materializeFlowInStudio } = await import("./flowStudio.js");
  await materializeFlowInStudio(flow, bindingPayload.bindings || [], applyResult, {
    mode:"new",
    title:titleFromFile(file),
  });
  return true;
}
