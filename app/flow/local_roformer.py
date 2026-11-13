from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

import yaml

from .adapters import (
    AdapterError,
    AdapterExecutionRequest,
    AdapterPreflightIssue,
    ModelAdapter,
)
from .provenance import model_artifact
from .schema import ModelDefinition


class LocalRoformerAdapter(ModelAdapter):
    """Run a user-provided local RoFormer checkpoint through audio-separator.

    This adapter intentionally bypasses audio-separator's public model registry while
    reusing its normal Separator -> MDXCSeparator -> RoformerLoader execution path.
    The model definition declares environment variables that resolve the checkpoint
    and matching YAML configuration at runtime, so flows remain portable and StemDeck
    does not redistribute third-party checkpoints with unclear licensing.
    """

    id = "local-roformer"

    @property
    def version(self) -> str:
        return "isolated-runtime-v1"

    def available(self) -> tuple[bool, str | None]:
        # The experimental runtime is intentionally external to StemDeck's
        # Python environment, so availability is verified during preflight.
        return True, None

    @staticmethod
    def _runtime_python(model: ModelDefinition) -> Path | None:
        direct = model.runtime.get("python")
        if direct:
            return Path(str(direct)).expanduser().absolute()

        env_name = str(
            model.runtime.get(
                "python_env",
                "STEMDECK_GUITAR_SPLIT_PYTHON",
            )
        )
        value = os.environ.get(env_name)
        if value:
            return Path(value).expanduser().absolute()

        return None

    @staticmethod
    def _version_tuple(value: str) -> tuple[int, ...]:
        result: list[int] = []
        for component in value.split("."):
            digits = "".join(ch for ch in component if ch.isdigit())
            if not digits:
                break
            result.append(int(digits))
        return tuple(result)

    @staticmethod
    def _probe_runtime(runtime_python: Path) -> str:
        completed = subprocess.run(
            [
                str(runtime_python),
                "-c",
                "import importlib.metadata as m; print(m.version('audio-separator'))",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return completed.stdout.strip()

    @staticmethod
    def _runtime_path(model: ModelDefinition, key: str) -> Path | None:
        direct = model.runtime.get(key)
        if direct:
            return Path(str(direct)).expanduser().resolve()
        env_name = model.runtime.get(f"{key}_env")
        if env_name:
            value = os.environ.get(str(env_name))
            if value:
                return Path(value).expanduser().resolve()
        return None

    def _checkpoint_path(self, model: ModelDefinition) -> Path | None:
        return self._runtime_path(model, "checkpoint")

    def _config_path(self, model: ModelDefinition) -> Path | None:
        return self._runtime_path(model, "config")

    @staticmethod
    def _detect_roformer_type(model_data: dict[str, Any]) -> str | None:
        """Detect RoFormer type across audio-separator config layouts.
        audio-separator 0.44.5 only checks architecture keys at the YAML root,
        while MSST-style configs keep them under `model:`. Inspect both shapes
        so user-provided configs are validated consistently.
        """
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

    def preflight(self, model: ModelDefinition) -> list[AdapterPreflightIssue]:
        issues: list[AdapterPreflightIssue] = []
        runtime_python = self._runtime_python(model)
        if runtime_python is None:
            env_name = model.runtime.get(
                "python_env", "STEMDECK_GUITAR_SPLIT_PYTHON"
            )
            issues.append(
                AdapterPreflightIssue(
                    "error",
                    "Experimental RoFormer runtime is not configured; set "
                    f"{env_name} to its Python executable",
                )
            )
        elif not runtime_python.is_file():
            issues.append(
                AdapterPreflightIssue(
                    "error",
                    f"Experimental RoFormer Python does not exist: {runtime_python}",
                )
            )
        else:
            try:
                runtime_version = self._probe_runtime(runtime_python)
                if self._version_tuple(runtime_version) < (0, 48):
                    issues.append(
                        AdapterPreflightIssue(
                            "error",
                            "Experimental RoFormer runtime requires audio-separator "
                            f">=0.48; found {runtime_version}",
                        )
                    )
            except Exception as exc:
                issues.append(
                    AdapterPreflightIssue(
                        "error",
                        f"Cannot probe experimental RoFormer runtime: {exc}",
                    )
                )
        if shutil.which("ffmpeg") is None:
            issues.append(
                AdapterPreflightIssue(
                    "error",
                    "FFmpeg is not available on PATH; install ffmpeg before running "
                    "local RoFormer models",
                )
            )

        checkpoint = self._checkpoint_path(model)
        config = self._config_path(model)
        if checkpoint is None:
            env_name = model.runtime.get("checkpoint_env")
            suffix = f" via {env_name}" if env_name else ""
            issues.append(
                AdapterPreflightIssue(
                    "error", f"Local RoFormer checkpoint is not configured{suffix}"
                )
            )
        elif not checkpoint.is_file():
            issues.append(
                AdapterPreflightIssue(
                    "error", f"Local RoFormer checkpoint does not exist: {checkpoint}"
                )
            )

        if config is None:
            env_name = model.runtime.get("config_env")
            suffix = f" via {env_name}" if env_name else ""
            issues.append(
                AdapterPreflightIssue(
                    "error", f"Local RoFormer config is not configured{suffix}"
                )
            )
        elif not config.is_file():
            issues.append(
                AdapterPreflightIssue(
                    "error", f"Local RoFormer config does not exist: {config}"
                )
            )
        else:
            try:
                model_data = yaml.load(config.read_text(encoding="utf-8"), Loader=yaml.FullLoader)
                if not isinstance(model_data, dict):
                    raise ValueError("YAML root must be a mapping")

                model_type = self._detect_roformer_type(model_data)
                if model_type not in {"mel_band_roformer", "bs_roformer"}:
                    issues.append(
                        AdapterPreflightIssue(
                            "error",
                            f"Local config is not recognized as a supported RoFormer model: "
                            f"{model_type or 'unknown'}",
                        )
                    )
            except Exception as exc:
                issues.append(
                    AdapterPreflightIssue(
                        "error", f"Cannot load local RoFormer config '{config}': {exc}"
                    )
                )

        issues.append(
            AdapterPreflightIssue(
                "warning",
                "Local RoFormer checkpoint is user-provided; StemDeck does not bundle "
                "or verify its redistribution license",
            )
        )
        return issues

    def model_artifacts(self, model: ModelDefinition) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        for path in (self._checkpoint_path(model), self._config_path(model)):
            if path is None:
                continue
            artifact = model_artifact(path)
            if artifact:
                artifacts.append(artifact)
        return artifacts

    def cache_identity(
        self,
        model: ModelDefinition,
        parameters: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        artifacts = self.model_artifacts(model)
        runtime_python = self._runtime_python(model)
        runtime_version: str | None = None
        if runtime_python is not None and runtime_python.is_file():
            try:
                runtime_version = self._probe_runtime(runtime_python)
            except Exception:
                runtime_version = None
        return {
            "checkpoint_sha256": next(
                (item.get("sha256") for item in artifacts if str(item.get("name", "")).endswith(".ckpt")),
                None,
            ),
            "config_sha256": next(
                (item.get("sha256") for item in artifacts if str(item.get("name", "")).endswith((".yaml", ".yml"))),
                None,
            ),
            "runtime_python": str(runtime_python) if runtime_python else None,
            "audio_separator": runtime_version,
        }

    def execute(self, request: AdapterExecutionRequest) -> dict[str, Path]:
        if set(request.inputs) != {"audio"}:
            raise AdapterError("local-roformer adapter expects exactly one 'audio' input")

        checkpoint = self._checkpoint_path(request.model)
        config = self._config_path(request.model)
        if checkpoint is None or config is None:
            raise AdapterError("Local RoFormer checkpoint/config were not resolved")
        if not checkpoint.is_file() or not config.is_file():
            raise AdapterError("Local RoFormer checkpoint/config are missing")

        runtime_python = self._runtime_python(request.model)
        if runtime_python is None or not runtime_python.is_file():
            raise AdapterError("Experimental RoFormer Python runtime was not resolved")

        output_labels = request.model.runtime.get("output_labels", {})
        missing_labels = [port for port in request.model.outputs if port not in output_labels]
        if missing_labels:
            raise AdapterError(
                f"Model '{request.model.id}' has no RoFormer labels for ports {missing_labels}"
            )

        request.output_dir.mkdir(parents=True, exist_ok=True)
        runner = Path(__file__).with_name("local_roformer_runner.py")
        rename = {
            str(output_labels[port]): port
            for port in request.model.outputs
        }
        command = [
            str(runtime_python),
            str(runner),
            "--checkpoint",
            str(checkpoint),
            "--config",
            str(config),
            "--input",
            str(request.inputs["audio"].path),
            "--output-dir",
            str(request.output_dir),
            "--labels-json",
            json.dumps(rename),
        ]
        if request.progress:
            request.progress(0.0, f"starting isolated RoFormer {checkpoint.name}")

        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise AdapterError(f"Cannot start isolated RoFormer runtime: {exc}") from exc

        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise AdapterError(
                "Isolated RoFormer separation failed"
                + (f": {detail}" if detail else "")
            )

        outputs: dict[str, Path] = {}
        for port in request.model.outputs:
            target = request.output_dir / f"{port}.wav"
            if not target.is_file():
                raise AdapterError(
                    f"Local RoFormer model '{request.model.id}' did not produce {target.name}"
                )
            outputs[port] = target
        if request.progress:
            request.progress(1.0, "isolated RoFormer complete")
        return outputs
