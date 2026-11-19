from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_semantic_color_resolver_is_shared_by_flow_editor_and_studio():
    constants = (ROOT / "static/js/constants.js").read_text(encoding="utf-8")
    editor = (ROOT / "static/js/flowEditor.js").read_text(encoding="utf-8")
    mixer = (ROOT / "static/js/mixer.js").read_text(encoding="utf-8")
    player = (ROOT / "static/js/player.js").read_text(encoding="utf-8")

    assert "export function semanticColor" in constants
    assert 'lead_guitar: "#9ae68b"' in constants
    assert 'rhythm_guitar: "#69bd68"' in constants
    assert 'lead_guitar: "guitar"' in constants
    assert 'backing_vocals: "vocals"' in constants

    assert 'import { semanticColor } from "./constants.js";' in editor
    assert "sourceSemanticType" in editor
    assert 'path.setAttribute("stroke", semanticColor(semanticType))' in editor

    assert "semanticColor(stem.semanticType" in mixer
    assert "semanticColor(stem.semanticType" in player
    assert "waveColor: semanticColor(" in player


def test_flow_colors_are_not_persisted_in_flow_document_contract():
    schema = (ROOT / "app/flow/schema.py").read_text(encoding="utf-8")
    assert "color:" not in schema
