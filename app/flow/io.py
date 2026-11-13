from __future__ import annotations

from pathlib import Path
import yaml

from .schema import FlowDefinition, ModelDefinition


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a YAML mapping in {path}")
    return data


def load_flow(path: Path) -> FlowDefinition:
    return FlowDefinition.model_validate(load_yaml(path))


def load_model_definition(path: Path) -> ModelDefinition:
    return ModelDefinition.model_validate(load_yaml(path))
