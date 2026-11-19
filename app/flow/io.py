from __future__ import annotations

from pathlib import Path
import yaml

from .compiler import FlowCompiler
from .schema import FlowDefinition, FlowDocument, ModelDefinition


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a YAML mapping in {path}")
    return data


def load_flow_document(path: Path) -> FlowDocument:
    """Load an editor/import document without requiring runtime validity."""
    return FlowDocument.model_validate(load_yaml(path))


def load_flow(path: Path) -> FlowDefinition:
    """Compatibility loader for executable YAML flows.

    YAML now enters through FlowDocument -> compile -> FlowDefinition so the
    same compilation boundary used by the future editor is exercised by PoC
    examples and CLI execution.
    """
    result = FlowCompiler().compile(load_flow_document(path))
    if result.definition is None:
        messages = "; ".join(diagnostic.message for diagnostic in result.errors)
        raise ValueError(f"Flow compilation failed for {path}: {messages}")
    return result.definition


def load_model_definition(path: Path) -> ModelDefinition:
    return ModelDefinition.model_validate(load_yaml(path))
