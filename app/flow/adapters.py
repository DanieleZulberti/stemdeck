from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
import shutil
from typing import Any, Callable

from .artifacts import AudioArtifact
from .schema import ModelDefinition
from .scheduler import ResourceClaim
from .provenance import model_artifact

ProgressCallback = Callable[[float | None, str], None]
CancellationCheck = Callable[[], bool]


class AdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class AdapterPreflightIssue:
    level: str
    message: str


@dataclass(frozen=True)
class AdapterExecutionRequest:
    node_id: str
    model: ModelDefinition
    inputs: dict[str, AudioArtifact]
    parameters: dict[str, Any]
    output_dir: Path
    progress: ProgressCallback | None = None
    context: dict[str, Any] = field(default_factory=dict)
    cancellation_requested: CancellationCheck | None = None


class ModelAdapter(ABC):
    id: str

    @property
    def version(self) -> str:
        return "unknown"

    @abstractmethod
    def available(self) -> tuple[bool, str | None]:
        """Return whether this adapter can execute in the current environment."""

    def preflight(self, model: ModelDefinition) -> list[AdapterPreflightIssue]:
        """Check external runtime requirements before graph execution."""
        return []

    def model_artifacts(self, model: ModelDefinition) -> list[dict[str, Any]]:
        """Return resolved model files used by this adapter for run provenance."""
        return []

    def cache_identity(
        self,
        model: ModelDefinition,
        parameters: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Return runtime facts that affect reproducibility/cache identity."""
        return {}

    def resource_claim(
        self,
        model: ModelDefinition,
        parameters: dict[str, Any],
        context: dict[str, Any],
    ) -> ResourceClaim:
        """Return the runtime resource group used to limit concurrent nodes."""
        return ResourceClaim("default", 1)

    @abstractmethod
    def execute(self, request: AdapterExecutionRequest) -> dict[str, Path]:
        """Execute a model and return normalized output-port -> file mappings."""


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ModelAdapter] = {}

    def register(self, adapter: ModelAdapter) -> None:
        if adapter.id in self._adapters:
            raise ValueError(f"Adapter '{adapter.id}' already registered")
        self._adapters[adapter.id] = adapter

    def get(self, adapter_id: str) -> ModelAdapter:
        try:
            return self._adapters[adapter_id]
        except KeyError as exc:
            raise KeyError(f"Adapter '{adapter_id}' is not registered") from exc

    def __contains__(self, adapter_id: str) -> bool:
        return adapter_id in self._adapters


class MockAdapter(ModelAdapter):
    """Deterministic adapter used by flow-engine tests."""

    id = "mock"

    @property
    def version(self) -> str:
        return "1"

    def available(self) -> tuple[bool, str | None]:
        return True, None

    def resource_claim(self, model, parameters, context) -> ResourceClaim:
        return ResourceClaim("mock-cpu", 32)

    def execute(self, request: AdapterExecutionRequest) -> dict[str, Path]:
        if not request.inputs:
            raise AdapterError("Mock adapter requires at least one input")
        source = next(iter(request.inputs.values())).path
        request.output_dir.mkdir(parents=True, exist_ok=True)
        outputs: dict[str, Path] = {}
        for port in request.model.outputs:
            target = request.output_dir / f"{port}{source.suffix or '.wav'}"
            shutil.copy2(source, target)
            outputs[port] = target
        if request.progress:
            request.progress(1.0, "mock complete")
        return outputs


class AudioSeparatorAdapter(ModelAdapter):
    """Generic adapter for models supported by python-audio-separator.

    Model-specific knowledge lives in ModelDefinition.runtime:
      model_name: checkpoint filename understood by audio-separator
      output_labels: normalized port -> source label used by audio-separator
      separator_kwargs: optional Separator(...) keyword arguments

    The flow engine therefore does not depend on checkpoint naming conventions.
    """

    id = "audio-separator"

    def __init__(self, model_file_dir: Path | None = None) -> None:
        self.model_file_dir = model_file_dir

    @property
    def version(self) -> str:
        from importlib import metadata

        try:
            return metadata.version("audio-separator")
        except metadata.PackageNotFoundError:
            return "unavailable"

    def available(self) -> tuple[bool, str | None]:
        try:
            from audio_separator.separator import Separator  # noqa: F401
        except Exception as exc:
            return False, f"audio-separator is unavailable: {exc}"
        return True, None

    def preflight(self, model: ModelDefinition) -> list[AdapterPreflightIssue]:
        issues: list[AdapterPreflightIssue] = []
        if shutil.which("ffmpeg") is None:
            issues.append(
                AdapterPreflightIssue(
                    "error",
                    "FFmpeg is not available on PATH; install ffmpeg before running "
                    "audio-separator models",
                )
            )
        return issues

    def model_artifacts(self, model: ModelDefinition) -> list[dict[str, Any]]:
        """Return resolved model files used by this adapter for run provenance."""
        return []

    def model_artifacts(self, model: ModelDefinition) -> list[dict[str, Any]]:
        model_name = model.runtime.get("model_name")
        if not model_name or self.model_file_dir is None:
            return []
        artifact = model_artifact(self.model_file_dir / str(model_name))
        return [artifact] if artifact else []

    def cache_identity(
        self,
        model: ModelDefinition,
        parameters: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "model_name": model.runtime.get("model_name"),
            "model_file_dir": str(self.model_file_dir) if self.model_file_dir else None,
        }

    def execute(self, request: AdapterExecutionRequest) -> dict[str, Path]:
        try:
            from audio_separator.separator import Separator
        except Exception as exc:  # pragma: no cover - depends on packaged runtime
            raise AdapterError(f"Cannot import audio-separator: {exc}") from exc

        if set(request.inputs) != {"audio"}:
            raise AdapterError("audio-separator adapter expects exactly one 'audio' input")

        model_name = request.model.runtime.get("model_name")
        if not model_name:
            raise AdapterError(
                f"Model '{request.model.id}' must declare runtime.model_name"
            )

        output_labels = request.model.runtime.get("output_labels", {})
        missing_labels = [port for port in request.model.outputs if port not in output_labels]
        if missing_labels:
            raise AdapterError(
                f"Model '{request.model.id}' has no audio-separator labels for ports "
                f"{missing_labels}"
            )

        request.output_dir.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {
            "output_dir": str(request.output_dir),
            "output_format": "WAV",
        }
        if self.model_file_dir is not None:
            kwargs["model_file_dir"] = str(self.model_file_dir)
        kwargs.update(request.model.runtime.get("separator_kwargs", {}))
        kwargs.update(request.parameters)

        separator = Separator(**kwargs)
        if request.progress:
            request.progress(0.0, f"loading {model_name}")
        separator.load_model(model_filename=str(model_name))

        rename = {
            str(output_labels[port]): port
            for port in request.model.outputs
        }
        if request.progress:
            request.progress(None, f"separating with {model_name}")
        separator.separate(str(request.inputs["audio"].path), rename)

        outputs: dict[str, Path] = {}
        for port in request.model.outputs:
            target = request.output_dir / f"{port}.wav"
            if not target.is_file():
                raise AdapterError(
                    f"audio-separator model '{model_name}' did not produce {target.name}"
                )
            outputs[port] = target
        if request.progress:
            request.progress(1.0, "audio-separator complete")
        return outputs
