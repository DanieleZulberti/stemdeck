from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class SemanticValidationMode(str, Enum):
    STRICT = "strict"
    WARN = "warn"
    OFF = "off"


class RetentionPolicy(str, Enum):
    TRANSIENT = "transient"
    CACHE = "cache"
    PROJECT = "project"


class FlowInputDefinition(BaseModel):
    # Technical artifact type. The current executor supports audio artifacts,
    # while the contract intentionally accepts future registered type IDs.
    type: str = "audio"
    semantic_type: str


class OutputPolicy(BaseModel):
    # `expose` is retained only for PoC/backwards compatibility. Publication is
    # a Flow Output concern in the target architecture.
    expose: bool = False
    retain: RetentionPolicy = RetentionPolicy.CACHE


class NodeInstance(BaseModel):
    """Runtime instance of a registered node type inside a compiled flow."""

    model_config = ConfigDict(populate_by_name=True)

    definition_id: str = Field(
        validation_alias=AliasChoices("definition_id", "model"),
    )
    inputs: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, OutputPolicy] = Field(default_factory=dict)

    @property
    def model(self) -> str:
        """Temporary compatibility alias for PoC executor/validator code."""
        return self.definition_id


class FlowOutputDefinition(BaseModel):
    source: str
    # Human-facing label. The mapping key remains the stable flow_output_id.
    label: str | None = None
    # Publication is explicit at the Flow Output boundary. Legacy YAML omits
    # this field, so default=True preserves the current visible-output behavior.
    publish: bool = True


class FlowValidationSettings(BaseModel):
    semantic: SemanticValidationMode = SemanticValidationMode.STRICT


class FlowDefinition(BaseModel):
    """Compiled runtime flow.

    This model intentionally rejects unknown top-level fields. Editor-only data
    belongs to FlowDocument and must not influence execution/cache identity.
    """

    model_config = ConfigDict(extra="forbid")

    flow_id: str | None = None
    name: str
    revision: int = Field(
        default=1,
        validation_alias=AliasChoices("revision", "version"),
    )
    schema_version: int = 1
    inputs: dict[str, FlowInputDefinition]
    nodes: dict[str, NodeInstance]
    outputs: dict[str, FlowOutputDefinition] = Field(default_factory=dict)
    validation: FlowValidationSettings = Field(default_factory=FlowValidationSettings)

    @model_validator(mode="after")
    def ensure_unique_namespaces(self) -> "FlowDefinition":
        collisions = set(self.inputs) & set(self.nodes)
        if collisions:
            raise ValueError(f"Input/node identifiers collide: {sorted(collisions)}")
        return self


class PortDefinition(BaseModel):
    # Technical artifact type identifier, intentionally not restricted to
    # audio so public node contracts can evolve without a schema break.
    type: str = "audio"
    semantic_type: str
    required: bool = True
    # Human-facing port label owned by the backend node contract. The port key
    # remains the stable technical identifier used by graph references.
    label: str | None = None


class ParameterUIHints(BaseModel):
    """Optional presentation hints for a generic editor.

    These values never define runtime semantics. A frontend may ignore them and
    still render a functionally correct editor from the parameter schema.
    """

    control: Literal["number", "slider", "checkbox", "text", "select"] | None = None
    step: float | None = None
    unit: str | None = None
    placeholder: str | None = None
    advanced: bool = False


class ParameterDefinition(BaseModel):
    type: Literal["integer", "number", "boolean", "string"]
    required: bool = False
    default: Any | None = None
    minimum: float | None = None
    maximum: float | None = None
    choices: list[Any] | None = None
    label: str | None = None
    description: str | None = None
    ui: ParameterUIHints = Field(default_factory=ParameterUIHints)


class NodeDefinition(BaseModel):
    """Static backend contract for a node type.

    The PoC called this ModelDefinition. `ModelDefinition` remains as a
    compatibility alias while internal/public contracts migrate to the final
    terminology.
    """

    id: str
    adapter: str
    inputs: dict[str, PortDefinition]
    outputs: dict[str, PortDefinition]
    parameters: dict[str, ParameterDefinition] = Field(default_factory=dict)
    model_file: str | None = None
    model_sha256: str | None = None
    requirements: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    label: str | None = None
    description: str | None = None
    category: str | None = None


# Compatibility name used throughout the PoC. New code should use
# NodeDefinition for the static contract and NodeInstance for graph instances.
ModelDefinition = NodeDefinition


# ---------------------------------------------------------------------------
# Persistent/editor document schema
# ---------------------------------------------------------------------------


class FlowDocumentInput(BaseModel):
    """Permissive editor representation of a Flow Input."""

    model_config = ConfigDict(extra="allow")

    type: Any = "audio"
    semantic_type: Any = None


class FlowDocumentOutputPolicy(BaseModel):
    """Legacy per-node output policy kept import-compatible during M0."""

    model_config = ConfigDict(extra="allow")

    expose: Any = False
    retain: Any = RetentionPolicy.CACHE.value


class FlowDocumentNode(BaseModel):
    """Persistable node instance; may be incomplete."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    definition_id: Any = Field(
        default=None,
        validation_alias=AliasChoices("definition_id", "model"),
    )
    inputs: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, FlowDocumentOutputPolicy] = Field(default_factory=dict)


class FlowDocumentOutput(BaseModel):
    """Persistable Flow Output; source may be absent while editing."""

    model_config = ConfigDict(extra="allow")

    source: Any = None
    label: Any = None
    publish: Any = True


class NodeLayout(BaseModel):
    model_config = ConfigDict(extra="allow")

    x: float = 0.0
    y: float = 0.0


class FlowEditorState(BaseModel):
    """Editor-only state. Never copied into FlowDefinition."""

    model_config = ConfigDict(extra="allow")

    nodes: dict[str, NodeLayout] = Field(default_factory=dict)
    viewport: dict[str, Any] = Field(default_factory=dict)


class FlowDocument(BaseModel):
    """Persistent/editor flow model.

    Domain-invalid or incomplete graph states must remain serializable. Strict
    runtime guarantees are established by FlowCompiler, not by this model.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    flow_id: str | None = None
    name: str = ""
    revision: int = Field(
        default=1,
        validation_alias=AliasChoices("revision", "version"),
    )
    schema_version: int = 1
    inputs: dict[str, FlowDocumentInput] = Field(default_factory=dict)
    nodes: dict[str, FlowDocumentNode] = Field(default_factory=dict)
    outputs: dict[str, FlowDocumentOutput] = Field(default_factory=dict)
    validation: FlowValidationSettings = Field(default_factory=FlowValidationSettings)
    editor: FlowEditorState = Field(default_factory=FlowEditorState)
