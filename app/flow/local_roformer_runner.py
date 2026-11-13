from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from audio_separator.separator import Separator
from audio_separator.separator.roformer.roformer_loader import RoformerLoader
from audio_separator.separator.uvr_lib_v5.roformer import mel_band_roformer


def _detect_roformer_type(model_data: dict[str, Any]) -> str | None:
    candidates = [model_data]
    nested_model = model_data.get("model")
    if isinstance(nested_model, dict):
        candidates.insert(0, nested_model)
    for candidate in candidates:
        if "freqs_per_bands" in candidate:
            return "bs_roformer"
        if any(key in candidate for key in ("num_bands", "n_mels", "mel_bands")):
            return "mel_band_roformer"
    hint = model_data.get("model_type", model_data.get("type", model_data.get("architecture")))
    if isinstance(hint, str):
        lowered = hint.lower()
        if "bs" in lowered and "roformer" in lowered:
            return "bs_roformer"
        if "mel" in lowered and "roformer" in lowered:
            return "mel_band_roformer"
    return None


def _install_mel_band_mask_estimator_workaround() -> None:
    """Propagate mlp_expansion_factor to MaskEstimator for audio-separator 0.48.x.

    audio-separator 0.48 accepts mlp_expansion_factor in MelBandRoformer and
    forwards it through RoformerLoader, but MelBandRoformer does not pass the
    value to MaskEstimator. The latter therefore falls back to 4, which makes
    checkpoints trained with a different factor fail with state-dict shape
    mismatches.

    Patch only the RoformerLoader construction boundary and restore the original
    MaskEstimator immediately afterwards so the workaround stays local to model
    construction in this isolated process.
    """

    original_create = RoformerLoader._create_mel_band_roformer

    def create_with_expansion_factor(self, config):  # type: ignore[no-untyped-def]
        factor = int(config.get("mlp_expansion_factor", 4))
        original_mask_estimator = mel_band_roformer.MaskEstimator

        class PatchedMaskEstimator(original_mask_estimator):
            def __init__(  # type: ignore[no-untyped-def]
                self,
                dim,
                dim_inputs,
                depth,
                mlp_expansion_factor=factor,
            ):
                super().__init__(
                    dim=dim,
                    dim_inputs=dim_inputs,
                    depth=depth,
                    mlp_expansion_factor=mlp_expansion_factor,
                )

        mel_band_roformer.MaskEstimator = PatchedMaskEstimator
        try:
            return original_create(self, config)
        finally:
            mel_band_roformer.MaskEstimator = original_mask_estimator

    RoformerLoader._create_mel_band_roformer = create_with_expansion_factor


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--labels-json", required=True)
    args = parser.parse_args()
    rename = json.loads(args.labels_json)

    _install_mel_band_mask_estimator_workaround()

    class LocalSeparator(Separator):
        def download_model_files(self, model_filename):  # type: ignore[no-untyped-def]
            return (
                args.checkpoint.name,
                "MDXC",
                args.checkpoint.stem,
                str(args.checkpoint),
                str(args.config),
            )

        def load_model_data_from_yaml(self, yaml_config_filename):
            model_data = super().load_model_data_from_yaml(yaml_config_filename)

            # MSST-style configs keep architecture parameters under `model:`.
            # Promote missing architecture keys to the root because
            # audio-separator's RoformerLoader consumes the flattened mapping.
            nested_model = model_data.get("model")
            if isinstance(nested_model, dict):
                for key, value in nested_model.items():
                    model_data.setdefault(key, value)

            model_data["is_roformer"] = True
            model_type = _detect_roformer_type(model_data)
            if model_type is not None:
                model_data["model_type"] = model_type

            return model_data

    args.output_dir.mkdir(parents=True, exist_ok=True)
    separator = LocalSeparator(
        output_dir=str(args.output_dir),
        output_format="WAV",
    )
    separator.load_model(model_filename=args.checkpoint.name)
    separator.separate(str(args.input), rename)


if __name__ == "__main__":
    main()
