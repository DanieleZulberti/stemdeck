from __future__ import annotations

from app.core.config import MODELS_DIR

from .adapters import AdapterRegistry, AudioSeparatorAdapter
from .registry import ModelRegistry
from .local_roformer import LocalRoformerAdapter
from .schema import ModelDefinition, PortDefinition
from .stemdeck_adapters import StemDeckDemucsAdapter, StemDeckVocalSplitAdapter


def build_default_model_registry() -> ModelRegistry:
    registry = ModelRegistry()
    registry.register(
        ModelDefinition(
            id="stemdeck-htdemucs-6s",
            adapter="stemdeck-demucs",
            inputs={"audio": PortDefinition(semantic_type="mixture")},
            outputs={
                "vocals": PortDefinition(semantic_type="vocals"),
                "drums": PortDefinition(semantic_type="drums"),
                "bass": PortDefinition(semantic_type="bass"),
                "guitar": PortDefinition(semantic_type="guitar"),
                "piano": PortDefinition(semantic_type="piano"),
                "other": PortDefinition(semantic_type="unknown_audio"),
            },
        )
    )
    registry.register(
        ModelDefinition(
            id="stemdeck-lead-backing-vocals",
            adapter="stemdeck-vocal-split",
            inputs={"audio": PortDefinition(semantic_type="vocals")},
            outputs={
                "lead": PortDefinition(semantic_type="lead_vocals"),
                "backing": PortDefinition(semantic_type="backing_vocals"),
            },
        )
    )
    registry.register(
        ModelDefinition(
            id="local-lead-rhythm-guitar-roformer",
            adapter="local-roformer",
            inputs={"audio": PortDefinition(semantic_type="guitar")},
            outputs={
                "lead": PortDefinition(semantic_type="lead_guitar"),
                "rhythm": PortDefinition(semantic_type="rhythm_guitar"),
            },
            requirements={"user_provided_model": True},
            runtime={
                "checkpoint_env": "STEMDECK_GUITAR_SPLIT_CHECKPOINT",
                "config_env": "STEMDECK_GUITAR_SPLIT_CONFIG",
                "output_labels": {"lead": "Lead", "rhythm": "Rhythm"},
            },
        )
    )
    return registry


def build_default_adapter_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register(StemDeckDemucsAdapter())
    registry.register(StemDeckVocalSplitAdapter())
    registry.register(AudioSeparatorAdapter(MODELS_DIR / "audio-separator"))
    registry.register(LocalRoformerAdapter())
    return registry
