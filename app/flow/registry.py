from __future__ import annotations

from .schema import ModelDefinition


class ModelRegistry:
    def __init__(self) -> None:
        self._models: dict[str, ModelDefinition] = {}

    def register(self, definition: ModelDefinition) -> None:
        if definition.id in self._models:
            raise ValueError(f"Model '{definition.id}' already registered")
        self._models[definition.id] = definition

    def get(self, model_id: str) -> ModelDefinition:
        try:
            return self._models[model_id]
        except KeyError as exc:
            raise KeyError(f"Model '{model_id}' is not registered") from exc

    def __contains__(self, model_id: str) -> bool:
        return model_id in self._models
