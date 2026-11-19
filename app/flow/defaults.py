from __future__ import annotations

from app.core.config import MODELS_DIR

from .registry import NodeDefinitionRegistry
from .schema import NodeDefinition, PortDefinition


def build_default_node_definition_registry() -> NodeDefinitionRegistry:
    registry = NodeDefinitionRegistry()
    registry.register(
        NodeDefinition(
            id="stemdeck-htdemucs-6s",
            label="6-stem separation",
            description="Split a full mix into vocals, drums, bass, guitar, piano, and other.",
            category="separation",
            adapter="stemdeck-demucs",
            inputs={"audio": PortDefinition(semantic_type="mixture")},
            outputs={
                "vocals": PortDefinition(semantic_type="vocals", label="Vocals"),
                "drums": PortDefinition(semantic_type="drums", label="Drums"),
                "bass": PortDefinition(semantic_type="bass", label="Bass"),
                "guitar": PortDefinition(semantic_type="guitar", label="Guitar"),
                "piano": PortDefinition(semantic_type="piano", label="Piano"),
                "other": PortDefinition(semantic_type="unknown_audio", label="Other"),
            },
        )
    )
    registry.register(
        NodeDefinition(
            id="stemdeck-lead-backing-vocals",
            label="Lead / backing vocals",
            description="Split a vocals artifact into lead and backing vocal artifacts.",
            category="separation",
            adapter="stemdeck-vocal-split",
            inputs={"audio": PortDefinition(semantic_type="vocals")},
            outputs={
                "lead": PortDefinition(semantic_type="lead_vocals", label="Lead Vocals"),
                "backing": PortDefinition(semantic_type="backing_vocals", label="Backing Vocals"),
            },
        )
    )
    registry.register(
        NodeDefinition(
            id="local-lead-rhythm-guitar-roformer",
            label="Lead / rhythm guitar",
            description="Experimental split of a guitar artifact into lead and rhythm parts.",
            category="separation",
            adapter="local-roformer",
            inputs={"audio": PortDefinition(semantic_type="guitar")},
            outputs={
                "lead": PortDefinition(semantic_type="lead_guitar", label="Lead Guitar"),
                "rhythm": PortDefinition(semantic_type="rhythm_guitar", label="Rhythm Guitar"),
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


def build_default_model_registry() -> NodeDefinitionRegistry:
    """Compatibility alias for PoC callers; new code should use node terminology."""
    return build_default_node_definition_registry()


def build_default_adapter_registry():
    # Runtime adapters are imported lazily so compiling/validating flow
    # documents and enumerating static node contracts does not eagerly import
    # the legacy processing pipeline.
    from .adapters import AdapterRegistry, AudioSeparatorAdapter
    from .local_roformer import LocalRoformerAdapter
    from .stemdeck_adapters import StemDeckDemucsAdapter, StemDeckVocalSplitAdapter

    registry = AdapterRegistry()
    registry.register(StemDeckDemucsAdapter())
    registry.register(StemDeckVocalSplitAdapter())
    registry.register(AudioSeparatorAdapter(MODELS_DIR / "audio-separator"))
    registry.register(LocalRoformerAdapter())
    return registry
