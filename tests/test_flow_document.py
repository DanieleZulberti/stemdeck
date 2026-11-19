from __future__ import annotations

from app.flow.adapters import AdapterRegistry, MockAdapter
from app.flow.capabilities import CapabilityChecker
from app.flow.compiler import FlowCompiler
from app.flow.registry import ModelRegistry
from app.flow.schema import FlowDocument, NodeDefinition, PortDefinition
from app.flow.validation import FlowValidator


def _registry() -> ModelRegistry:
    models = ModelRegistry()
    models.register(
        NodeDefinition(
            id="split",
            adapter="mock",
            category="separation",
            inputs={"audio": PortDefinition(semantic_type="mixture")},
            outputs={"vocals": PortDefinition(semantic_type="vocals")},
        )
    )
    return models


def test_flow_document_can_persist_incomplete_graph():
    document = FlowDocument.model_validate(
        {
            "name": "Draft",
            "nodes": {"draft-node": {"inputs": {"audio": None}}},
            "outputs": {"draft-output": {}},
            "editor": {"nodes": {"draft-node": {"x": 10, "y": 20}}},
        }
    )

    assert document.nodes["draft-node"].definition_id is None
    assert document.editor.nodes["draft-node"].x == 10

    result = FlowCompiler().compile(document)

    assert not result.compiled
    assert {diagnostic.code for diagnostic in result.errors} == {
        "missing_node_definition",
        "missing_flow_output_source",
    }
    assert all(diagnostic.source.value == "compiler" for diagnostic in result.errors)


def test_compiler_strips_editor_state_from_runtime_definition():
    document = FlowDocument.model_validate(
        {
            "flow_id": "flow-1",
            "name": "Demo",
            "revision": 7,
            "schema_version": 1,
            "inputs": {"song": {"semantic_type": "mixture"}},
            "nodes": {
                "primary": {
                    "definition_id": "split",
                    "inputs": {"audio": "inputs.song"},
                }
            },
            "outputs": {"vocals": {"source": "primary.vocals"}},
            "editor": {"nodes": {"primary": {"x": 120, "y": 80}}},
        }
    )

    result = FlowCompiler().compile(document)

    assert result.compiled
    assert result.definition is not None
    assert result.definition.flow_id == "flow-1"
    assert result.definition.revision == 7
    assert "editor" not in result.definition.model_dump()


def test_compiled_valid_and_runnable_are_distinct_states():
    models = _registry()
    adapters = AdapterRegistry()
    adapters.register(MockAdapter())
    document = FlowDocument.model_validate(
        {
            "name": "Missing binding",
            "inputs": {"song": {"semantic_type": "mixture"}},
            "nodes": {"primary": {"definition_id": "split", "inputs": {}}},
        }
    )

    compile_result = FlowCompiler().compile(document)

    assert compile_result.compiled
    assert compile_result.definition is not None

    validation = FlowValidator(models).validate(compile_result.definition)
    assert not validation.valid
    assert validation.errors[0].node_id == "primary"
    assert validation.errors[0].port_id == "audio"

    # Capability is a separate stage. The adapter is available even though the
    # graph itself is invalid; orchestration must not conflate these concepts.
    capability = CapabilityChecker(models, adapters).check(compile_result.definition)
    assert capability.runnable
