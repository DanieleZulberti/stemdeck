from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SemanticValidationMode(str, Enum):
    STRICT = "strict"
    WARN = "warn"
    OFF = "off"


class RetentionPolicy(str, Enum):
    TRANSIENT = "transient"
    CACHE = "cache"
    PROJECT = "project"


class FlowInputDefinition(BaseModel):
    type: Literal["audio"] = "audio"
    semantic_type: str


class OutputPolicy(BaseModel):
    expose: bool = False
    retain: RetentionPolicy = RetentionPolicy.CACHE


class NodeDefinition(BaseModel):
    model: str
    inputs: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, OutputPolicy] = Field(default_factory=dict)


class FlowOutputDefinition(BaseModel):
    source: str


class FlowValidationSettings(BaseModel):
    semantic: SemanticValidationMode = SemanticValidationMode.STRICT


class FlowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: int = 1
    inputs: dict[str, FlowInputDefinition]
    nodes: dict[str, NodeDefinition]
    outputs: dict[str, FlowOutputDefinition] = Field(default_factory=dict)
    validation: FlowValidationSettings = Field(default_factory=FlowValidationSettings)

    @model_validator(mode="after")
    def ensure_unique_namespaces(self) -> "FlowDefinition":
        collisions = set(self.inputs) & set(self.nodes)
        if collisions:
            raise ValueError(f"Input/node identifiers collide: {sorted(collisions)}")
        return self


class PortDefinition(BaseModel):
    type: Literal["audio"] = "audio"
    semantic_type: str
    required: bool = True


class ParameterDefinition(BaseModel):
    type: Literal["integer", "number", "boolean", "string"]
    required: bool = False
    default: Any | None = None
    minimum: float | None = None
    maximum: float | None = None
    choices: list[Any] | None = None


class ModelDefinition(BaseModel):
    id: str
    adapter: str
    inputs: dict[str, PortDefinition]
    outputs: dict[str, PortDefinition]
    parameters: dict[str, ParameterDefinition] = Field(default_factory=dict)
    model_file: str | None = None
    model_sha256: str | None = None
    requirements: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
