import { addTrackToLibrary, loadTrackIntoStudio, applyFlowOverlayToTrack } from "./catalog.js";
import { currentJobId, loopEnabled, loopStart, loopEnd } from "./state.js";
import { transport, setPlayheadTime, setLoopRange } from "./transport.js";

function sessionId(flowId) {
  return `flow:${flowId}`;
}

function captureTemporalState(id) {
  if (currentJobId !== id) return null;
  const time = transport()?.getCurrentTime?.();
  return {
    playhead: Number.isFinite(time) ? time : 0,
    loopEnabled,
    loopStart,
    loopEnd,
  };
}

function restoreTemporalState(snapshot, duration) {
  if (!snapshot) return;
  setPlayheadTime(snapshot.playhead || 0);
  if (snapshot.loopEnabled && snapshot.loopEnd > snapshot.loopStart && snapshot.loopEnd <= duration + 0.05) {
    setLoopRange(snapshot.loopStart, snapshot.loopEnd);
  }
}

function allUpdatesTemporallyCompatible(applyResult) {
  const updates = applyResult?.updated || [];
  return updates.length > 0
    && updates.every((item) => item.temporal_compatibility?.compatible === true);
}

/**
 * Project-free Studio projection for reusable Flows.
 *
 * Binding identity remains backend-owned and keyed by flow_id + flow_output_id.
 * The UI only decides whether those managed lanes become a dedicated Studio
 * session or overlay an existing library track.
 */
export async function materializeFlowInStudio(flow, bindings, applyResult = null, options = {}) {
  const managed = (bindings || []).filter(
    (binding) => binding.status === "managed" && binding.flow_id === flow.flow_id,
  );
  if (!managed.length) return null;

  const id = sessionId(flow.flow_id);
  const noBindingChanges = applyResult
    && !(applyResult.created || []).length
    && !(applyResult.updated || []).length
    && !(applyResult.detached || []).length;
  if (noBindingChanges && currentJobId === id) return id;
  const temporalSnapshot = allUpdatesTemporallyCompatible(applyResult)
    ? captureTemporalState(options.mode === "input" && options.targetAsset?.job_id ? options.targetAsset.job_id : id)
    : null;
  const audioStems = managed.map((binding) => ({
    name: binding.track_id,
    displayName: binding.artifact?.label || binding.flow_output_id,
    flowOutputId: binding.flow_output_id,
    url: binding.media_url,
    semanticType: binding.artifact?.semantic_type || null,
    artifactType: binding.artifact?.artifact_type || "audio",
    bindingId: binding.binding_id,
    bindingTrackId: binding.track_id,
  }));
  const duration = managed.reduce(
    (max, binding) => Math.max(max, Number(binding.artifact?.duration_seconds || 0)),
    0,
  );
  if (options.mode === "input" && options.targetAsset?.job_id) {
    const targetId = options.targetAsset.job_id;
    applyFlowOverlayToTrack(targetId, {
      overlayId: id,
      flowId: flow.flow_id,
      flowRevision: flow.revision,
      title: options.title || options.targetAsset.title || "",
      audioStems,
      duration,
    });
    await loadTrackIntoStudio(targetId);
    restoreTemporalState(temporalSnapshot, Math.max(duration, Number(options.targetAsset.duration || 0)));
    return targetId;
  }

  addTrackToLibrary({
    id,
    title: options.title || options.targetAsset?.title || flow.name || "Flow",
    thumb: "",
    status: "done",
    sourceUrl: "",
    stems: audioStems.map((stem) => stem.name),
    audioStems,
    duration,
    flowManaged: true,
    flowId: flow.flow_id,
    flowRevision: flow.revision,
  });
  await loadTrackIntoStudio(id);
  restoreTemporalState(temporalSnapshot, duration);
  return id;
}

// Backwards compatibility for sessions created by M7-M9.6 code paths.
export async function materializeProjectFlowInStudio(_project, flow, bindings, applyResult = null, options = {}) {
  return materializeFlowInStudio(flow, bindings, applyResult, options);
}
