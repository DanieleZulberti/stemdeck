from pathlib import Path

ROOT = Path.cwd() if (Path.cwd() / "static").exists() else Path(__file__).resolve().parents[1]


def test_flow_editor_is_integrated_as_isolated_workspace():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    main = (ROOT / "static" / "js" / "main.js").read_text(encoding="utf-8")
    editor = (ROOT / "static" / "js" / "flowEditor.js").read_text(encoding="utf-8")

    assert 'id="flowsBtn"' in html
    assert 'id="flowEditorView"' in html
    assert 'id="flowCanvas"' in html
    assert 'id="flowInspectorBody"' in html
    assert 'import { initFlowEditor } from "./flowEditor.js";' in main
    assert "initFlowEditor();" in main
    assert 'jsonFetch("/node-definitions"' in editor
    assert 'jsonFetch("/flows/capabilities"' in editor


def test_flow_editor_does_not_hardcode_processing_model_names():
    editor = (ROOT / "static" / "js" / "flowEditor.js").read_text(encoding="utf-8").lower()
    for implementation_name in ("demucs", "uvr", "roformer", "htdemucs"):
        assert implementation_name not in editor


def test_edges_are_derived_and_not_stored_as_document_state():
    editor = (ROOT / "static" / "js" / "flowEditor.js").read_text(encoding="utf-8")
    assert "function drawEdges()" in editor
    assert ".inputs[portId] = state.pendingSource" in editor
    assert ".source = state.pendingSource" in editor
    assert "document.edges" not in editor


def test_m97_flow_editor_uses_direct_flow_apply_without_project_ui():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    editor = (ROOT / "static" / "js" / "flowEditor.js").read_text(encoding="utf-8")

    assert 'id="flowProjectSelect"' not in html
    assert 'id="flowProjectList"' not in html
    assert 'id="flowApplyBtn"' in html
    assert '/flow-studio/apply' in editor
    assert '/flow-studio/bindings?flow_id=' in editor


def test_apply_managed_flow_bindings_into_legacy_studio_without_model_coupling():
    editor = (ROOT / "static" / "js" / "flowEditor.js").read_text(encoding="utf-8")
    studio = (ROOT / "static" / "js" / "flowStudio.js").read_text(encoding="utf-8")
    catalog = (ROOT / "static" / "js" / "catalog.js").read_text(encoding="utf-8")
    player = (ROOT / "static" / "js" / "player.js").read_text(encoding="utf-8")

    assert 'materializeFlowInStudio' in editor
    assert 'binding.flow_output_id' in studio
    assert 'flowManaged: true' in studio
    assert 'export async function loadTrackIntoStudio' in catalog
    assert 'managedFlow = false' in player
    for implementation_name in ("demucs", "uvr", "roformer", "htdemucs"):
        assert implementation_name not in studio.lower()


def test_flow_managed_studio_sessions_are_not_reconciled_as_legacy_jobs():
    catalog = (ROOT / "static" / "js" / "catalog.js").read_text(encoding="utf-8")

    assert "if (t.flowManaged) continue;" in catalog
    assert "if (tracks[trackId]?.flowManaged) return Promise.resolve();" in catalog
    assert "if (tracks[trackId]?.flowManaged) return;" in catalog
    assert "const managedFlowIds = new Set" in catalog
    assert "const legacyJobIds = toDelete.filter" in catalog
    assert "if (managedFlowIds.has(id)) continue;" in catalog


def test_m8_managed_studio_reconciliation_preserves_stable_session_and_temporal_state():
    studio = (ROOT / "static" / "js" / "flowStudio.js").read_text(encoding="utf-8")
    assert "binding.flow_output_id" in studio
    assert "binding.binding_track_id" not in studio  # payload field remains bindingTrackId in lane projection
    assert "allUpdatesTemporallyCompatible" in studio
    assert "captureTemporalState" in studio
    assert "restoreTemporalState" in studio
    assert "noBindingChanges" in studio
    assert "setPlayheadTime" in studio
    assert "setLoopRange" in studio
    assert "selectedStems:" not in studio


def test_flow_editor_drag_does_not_depend_on_pointer_capture_or_rerender_mid_gesture():
    editor = (ROOT / "static" / "js" / "flowEditor.js").read_text(encoding="utf-8")

    assert "setPointerCapture" not in editor
    assert 'window.addEventListener("pointermove", move)' in editor
    assert 'window.addEventListener("pointerup", finish)' in editor
    assert 'window.addEventListener("pointercancel", finish)' in editor
    assert "Do not call setSelected() here" in editor


def test_new_virtual_io_nodes_are_placed_in_current_viewport_and_output_can_consume_pending_source():
    editor = (ROOT / "static" / "js" / "flowEditor.js").read_text(encoding="utf-8")

    assert "function visibleCanvasPosition" in editor
    assert 'visibleCanvasPosition("left"' in editor
    assert 'visibleCanvasPosition("right"' in editor
    assert "const source = sourceOverride !== undefined ? sourceOverride : (state.pendingSource || null);" in editor
    assert "state.document.outputs[id] = { source, label:defaultLabel, publish:true };" in editor


def test_virtual_flow_io_are_grouped_visually_without_changing_document_model():
    editor = (ROOT / "static" / "js" / "flowEditor.js").read_text(encoding="utf-8")

    assert 'virtualIoCard("inputs")' in editor
    assert 'virtualIoCard("outputs")' in editor
    assert '"Flow Inputs" : "Flow Outputs"' in editor
    assert 'ownerKind:"input"' in editor
    assert 'ownerKind:"output"' in editor
    assert 'state.document.inputs[id]' in editor
    assert 'state.document.outputs[id]' in editor
    assert "document.edges" not in editor


def test_grouped_virtual_io_keeps_per_port_selection_and_dot_connections():
    editor = (ROOT / "static" / "js" / "flowEditor.js").read_text(encoding="utf-8")

    assert 'const onDot = !!event.target.closest(".flow-port-dot")' in editor
    assert 'setSelected("input", port.dataset.ownerId)' in editor
    assert 'setSelected("output", port.dataset.ownerId)' in editor
    assert 'if (onDot) beginConnection(port.dataset.sourceRef)' in editor
    assert 'connectPending(port.dataset.ownerKind, port.dataset.ownerId, port.dataset.portId)' in editor


def test_flow_run_ui_binds_library_assets_and_monitors_node_runs():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    editor = (ROOT / "static" / "js" / "flowEditor.js").read_text(encoding="utf-8")

    assert 'id="flowRunBtn"' in html
    assert 'id="flowRunPanel"' in html
    assert 'jsonFetch("/flow-input-assets"' in editor
    assert 'input_assets' in editor
    assert 'new EventSource(`${API}/flow-runs/' in editor
    assert 'data-run-action="cancel"' in editor
    assert 'data-run-action="retry"' in editor
    assert 'cache hit' in editor


def test_m94_flow_output_label_is_editable_without_renaming_stable_output_id():
    editor = (ROOT / "static" / "js" / "flowEditor.js").read_text(encoding="utf-8")
    studio = (ROOT / "static" / "js" / "flowStudio.js").read_text(encoding="utf-8")
    mixer = (ROOT / "static" / "js" / "mixer.js").read_text(encoding="utf-8")

    assert 'data-output-field="label"' in editor
    assert "Stable technical identity" in editor
    assert "name: binding.track_id" in studio
    assert "displayName: binding.artifact?.label || binding.flow_output_id" in studio
    assert "stem.displayName ||" in mixer


def test_m94_apply_dialog_supports_new_session_or_input_track_overlay():
    editor = (ROOT / "static" / "js" / "flowEditor.js").read_text(encoding="utf-8")
    studio = (ROOT / "static" / "js" / "flowStudio.js").read_text(encoding="utf-8")
    catalog = (ROOT / "static" / "js" / "catalog.js").read_text(encoding="utf-8")

    assert "Create a new Studio session" in editor
    assert "Apply outputs to an existing library track" in editor
    assert "target_asset" in editor
    assert "applyFlowOverlayToTrack" in studio
    assert "export function applyFlowOverlayToTrack" in catalog
    assert "mergedAudioStemsWithFlowOverlays" in catalog


def test_m94_flow_managed_waveforms_use_dynamic_lane_order_and_decode_fallback():
    catalog = (ROOT / "static" / "js" / "catalog.js").read_text(encoding="utf-8")
    player = (ROOT / "static" / "js" / "player.js").read_text(encoding="utf-8")

    assert "(managedFlow || flowAugmented)" in catalog
    assert "renderMixerRowsForFlowAugmented" in player
    assert 'if (_managedFlowMixerActive) return stems.map((stem) => stem.name);' in player


def test_m95_node_output_ports_expose_human_labels_and_flow_outputs_inherit_them():
    editor = (ROOT / "static" / "js" / "flowEditor.js").read_text(encoding="utf-8")
    defaults = (ROOT / "app" / "flow" / "defaults.py").read_text(encoding="utf-8")
    schema = (ROOT / "app" / "flow" / "schema.py").read_text(encoding="utf-8")

    assert "label: str | None = None" in schema
    assert 'label="Lead Vocals"' in defaults
    assert 'label="Backing Vocals"' in defaults
    assert 'label="Lead Guitar"' in defaults
    assert 'label="Rhythm Guitar"' in defaults
    assert "def?.outputs?.[portId]?.label || humanizePortId(portId)" in editor
    assert "label:defaultLabel" in editor
    assert "if (!String(output.label || \"\").trim())" in editor


def test_m95_connecting_to_flow_outputs_block_auto_creates_output_without_persisted_edges():
    editor = (ROOT / "static" / "js" / "flowEditor.js").read_text(encoding="utf-8")

    assert 'data-auto-output-target="true"' in editor
    assert "addOutput(state.pendingSource)" in editor
    assert "Connect a node output here" in editor
    assert "document.edges" not in editor


def test_m96_flow_run_input_supports_direct_audio_upload_without_filesystem_paths():
    editor = (ROOT / "static" / "js" / "flowEditor.js").read_text(encoding="utf-8")
    api = (ROOT / "app" / "api" / "flow_input_assets.py").read_text(encoding="utf-8")

    assert 'data-run-upload=' in editor
    assert 'Choose audio file' in editor
    assert 'new FormData()' in editor
    assert '/flow-input-assets/upload' in editor
    assert 'state.inputBindings[inputId] = asset.asset_id' in editor
    assert '@router.post("/upload"' in api


def test_m97_output_block_selection_delete_and_connection_cancel_affordances():
    editor = (ROOT / "static" / "js" / "flowEditor.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "css" / "flow-editor.css").read_text(encoding="utf-8")

    assert 'setSelected("outputs", "outputs")' in editor
    assert 'data-action="clear-outputs"' in editor
    assert 'data-action="delete-output-block"' in editor
    assert 'function cancelConnection()' in editor
    assert 'event.key === "Escape"' in editor
    assert 'data-cancel-connection' in editor
    assert 'data-disconnect-port' in editor
    assert '.flow-canvas.connection-active .flow-port.input .flow-port-dot' in css


def test_m97_standard_import_bar_exposes_reusable_flow_selection_and_edit():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    flow_import = (ROOT / "static" / "js" / "flowImport.js").read_text(encoding="utf-8")
    job = (ROOT / "static" / "js" / "job.js").read_text(encoding="utf-8")

    assert 'id="composerFlowSelect"' in html
    assert 'id="composerFlowEditBtn"' in html
    assert 'Standard extraction' in html
    assert 'runSelectedFlowImport' in job
    assert '/flow-input-assets/upload' in flow_import
    assert '/flow-runs' in flow_import
    assert '/flow-studio/apply' in flow_import
    assert 'stemdeck:open-flow' in flow_import


def test_m971_recreating_output_block_does_not_treat_click_event_as_source_reference():
    editor = (ROOT / "static" / "js" / "flowEditor.js").read_text(encoding="utf-8")

    assert 'el.addOutput.addEventListener("click", () => addOutput());' in editor
    assert 'if (sourceOverride && typeof sourceOverride !== "string") sourceOverride = undefined;' in editor


def test_m981_flow_managed_session_is_relisted_when_same_stable_id_was_in_trash():
    catalog = (ROOT / "static" / "js" / "catalog.js").read_text(encoding="utf-8")

    assert 'if (track.flowManaged === true)' in catalog
    assert 'trash.items = trash.items.filter((id) => id !== track.id);' in catalog
    assert 'folder.id !== TRASH_ID && folder.items.includes(track.id)' in catalog


def test_m983_opening_flow_from_composer_waits_for_node_definitions_before_first_render():
    editor = (ROOT / "static" / "js" / "flowEditor.js").read_text(encoding="utf-8")

    assert "let definitionsPromise = null;" in editor
    assert "function ensureDefinitionsLoaded()" in editor
    assert "await ensureDefinitionsLoaded();" in editor
    assert "if (state.document) {" in editor
    assert "renderCanvas();" in editor
    assert "ensureDefinitionsLoaded().catch(showError)" in editor
