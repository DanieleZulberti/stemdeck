from __future__ import annotations

from .schema import NodeDefinition


class NodeDefinitionRegistry:
    """Registry of static node-type contracts exposed by the backend."""

    def __init__(self) -> None:
        self._definitions: dict[str, NodeDefinition] = {}

    def register(self, definition: NodeDefinition) -> None:
        if definition.id in self._definitions:
            raise ValueError(f"Node definition '{definition.id}' already registered")
        self._definitions[definition.id] = definition

    def get(self, definition_id: str) -> NodeDefinition:
        try:
            return self._definitions[definition_id]
        except KeyError as exc:
            raise KeyError(f"Node definition '{definition_id}' is not registered") from exc

    def all(self) -> tuple[NodeDefinition, ...]:
        return tuple(self._definitions.values())

    def __contains__(self, definition_id: str) -> bool:
        return definition_id in self._definitions


# Compatibility name used by the current PoC and adapters.
ModelRegistry = NodeDefinitionRegistry
