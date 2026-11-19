from __future__ import annotations

import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from app.core.config import DEMUCS_MODEL, MODELS_DIR, VOCAL_SPLIT_MODEL
from app.core.model_cache import vocal_split_artifacts
from app.core.models import Job
from app.core.registry import get_proc
from app.core.settings import get_demucs_device, get_separation_quality
from app.pipeline.separate import separate
from app.pipeline.vocal_split import split_vocals

from .adapters import (
    AdapterError,
    AdapterExecutionRequest,
    AdapterPreflightIssue,
    ModelAdapter,
)
from .provenance import model_artifact
from .schema import ModelDefinition
from .scheduler import ResourceClaim


def _demucs_remote_checkpoints() -> list[tuple[Path, str]]:
    """Resolve Demucs checkpoint cache paths and their upstream URLs.

    This mirrors Demucs' packaged ``remote/files.txt`` metadata without calling
    ``get_model()``, which would start a download during preflight.
    """
    import torch
    import yaml
    from demucs.pretrained import REMOTE_ROOT, ROOT_URL

    manifest_path = REMOTE_ROOT / f"{DEMUCS_MODEL}.yaml"
    if not manifest_path.is_file():
        return []
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    prefixes = {str(value) for value in manifest.get("models", [])}
    if not prefixes:
        return []

    matches: dict[str, tuple[Path, str]] = {}
    remote_dir = ""
    for raw_line in (REMOTE_ROOT / "files.txt").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("root:"):
            remote_dir = line.partition(":")[2].strip().strip("/")
            continue
        prefix = line.split("-", 1)[0]
        if prefix not in prefixes:
            continue
        cache_path = Path(torch.hub.get_dir()) / "checkpoints" / Path(line).name
        relative = f"{remote_dir}/{line}" if remote_dir else line
        matches[prefix] = (cache_path, f"{ROOT_URL}{relative}")

    return [matches[prefix] for prefix in prefixes if prefix in matches]


def _check_remote_url(url: str) -> str | None:
    request = urllib.request.Request(
        url,
        headers={"Range": "bytes=0-0", "User-Agent": "StemDeck-Flow-Preflight/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8):
            return None
    except (urllib.error.URLError, OSError) as exc:
        return str(exc)


def _job_for_request(request: AdapterExecutionRequest) -> Job:
    """Return a legacy Job bridge without making Job part of the Flow domain."""
    job = request.context.get("job")
    if isinstance(job, Job):
        return job
    return Job(id=f"flow-{request.node_id}")


def _start_cancellation_bridge(
    request: AdapterExecutionRequest, job: Job
) -> tuple[threading.Event, threading.Thread | None]:
    """Translate graph cancellation to the legacy Job/process mechanism."""
    if request.cancellation_requested is None:
        return threading.Event(), None
    done = threading.Event()

    def watch() -> None:
        while not done.wait(0.1):
            if not request.cancellation_requested():
                continue
            job.cancel_requested = True
            proc = get_proc(job.id)
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
            return

    thread = threading.Thread(target=watch, daemon=True, name=f"flow-cancel-{request.node_id}")
    thread.start()
    return done, thread


def _stop_cancellation_bridge(done: threading.Event, thread: threading.Thread | None) -> None:
    done.set()
    if thread is not None:
        thread.join(timeout=1)


class StemDeckDemucsAdapter(ModelAdapter):
    """Wrap StemDeck's existing Demucs worker without changing its behavior.

    This preserves the production worker lifecycle, cancellation registry,
    watchdog, GPU-to-CPU fallback and progress reporting already covered by
    StemDeck's tests.
    """

    id = "stemdeck-demucs"

    @property
    def version(self) -> str:
        return "1"

    def available(self) -> tuple[bool, str | None]:
        try:
            import demucs  # noqa: F401
        except Exception as exc:
            return False, f"demucs is unavailable: {exc}"
        return True, None

    def preflight(self, model: ModelDefinition) -> list[AdapterPreflightIssue]:
        try:
            checkpoints = _demucs_remote_checkpoints()
        except Exception as exc:
            return [
                AdapterPreflightIssue(
                    "error", f"Cannot resolve Demucs checkpoint metadata: {exc}"
                )
            ]
        if not checkpoints:
            return [
                AdapterPreflightIssue(
                    "error",
                    f"Cannot resolve checkpoint metadata for Demucs model '{DEMUCS_MODEL}'",
                )
            ]

        issues: list[AdapterPreflightIssue] = []
        for cache_path, url in checkpoints:
            if cache_path.is_file():
                continue
            reason = _check_remote_url(url)
            if reason:
                issues.append(
                    AdapterPreflightIssue(
                        "error",
                        f"Demucs checkpoint is not cached ({cache_path.name}) and its "
                        f"remote URL is unreachable: {reason}",
                    )
                )
        return issues

    def model_artifacts(self, model: ModelDefinition) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        try:
            checkpoints = _demucs_remote_checkpoints()
        except Exception:
            return artifacts
        for cache_path, url in checkpoints:
            artifact = model_artifact(cache_path, source_url=url)
            if artifact:
                artifacts.append(artifact)
        return artifacts

    def cache_identity(
        self,
        model: ModelDefinition,
        parameters: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "demucs_model": DEMUCS_MODEL,
            "quality": get_separation_quality(),
            "device": get_demucs_device(),
        }

    def resource_claim(self, model, parameters, context) -> ResourceClaim:
        # StemDeck's persistent Demucs worker is a process-global singleton.
        return ResourceClaim("stemdeck-demucs-worker", 1)

    def execute(self, request: AdapterExecutionRequest) -> dict[str, Path]:
        if set(request.inputs) != {"audio"}:
            raise AdapterError("StemDeck Demucs adapter expects exactly one 'audio' input")
        if request.parameters:
            raise AdapterError(
                "StemDeck Demucs adapter currently uses StemDeck settings "
                "and accepts no node params"
            )

        job = _job_for_request(request)
        cancel_done, cancel_thread = _start_cancellation_bridge(request, job)
        scratch = request.output_dir.parent / "work"
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            stems_root = separate(job, request.inputs["audio"].path, scratch)
            request.output_dir.mkdir(parents=True, exist_ok=True)
            stem_map = request.model.runtime.get("output_stem_map", {})
            outputs: dict[str, Path] = {}
            for port in request.model.outputs:
                source_name = str(stem_map.get(port, port))
                source = stems_root / f"{source_name}.wav"
                if not source.is_file():
                    raise AdapterError(
                        f"Demucs did not produce '{source_name}.wav' required by port '{port}'"
                    )
                target = request.output_dir / f"{port}.wav"
                shutil.copy2(source, target)
                outputs[port] = target
            return outputs
        finally:
            _stop_cancellation_bridge(cancel_done, cancel_thread)
            shutil.rmtree(scratch, ignore_errors=True)


class StemDeckVocalSplitAdapter(ModelAdapter):
    """Wrap StemDeck's existing UVR-MDX-NET Karaoke 2 worker."""

    id = "stemdeck-vocal-split"

    @property
    def version(self) -> str:
        return "1"

    def available(self) -> tuple[bool, str | None]:
        try:
            import audio_separator  # noqa: F401
        except Exception as exc:
            return False, f"audio-separator is unavailable: {exc}"
        return True, None

    def preflight(self, model: ModelDefinition) -> list[AdapterPreflightIssue]:
        if shutil.which("ffmpeg") is None:
            return [
                AdapterPreflightIssue(
                    "error",
                    "FFmpeg is not available on PATH; install ffmpeg before running "
                    "the StemDeck vocal split",
                )
            ]
        return []

    def model_artifacts(self, model: ModelDefinition) -> list[dict[str, Any]]:
        artifacts = vocal_split_artifacts(MODELS_DIR, VOCAL_SPLIT_MODEL)
        checkpoint = artifacts[0] if artifacts else None
        if checkpoint is None:
            return []
        artifact = model_artifact(checkpoint)
        return [artifact] if artifact else []

    def cache_identity(
        self,
        model: ModelDefinition,
        parameters: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "vocal_split_model": VOCAL_SPLIT_MODEL,
            "device": get_demucs_device(),
        }

    def resource_claim(self, model, parameters, context) -> ResourceClaim:
        return ResourceClaim("accelerator-heavy", 1)

    def execute(self, request: AdapterExecutionRequest) -> dict[str, Path]:
        if set(request.inputs) != {"audio"}:
            raise AdapterError("StemDeck vocal split adapter expects exactly one 'audio' input")
        if request.parameters:
            raise AdapterError(
                "StemDeck vocal split adapter currently accepts no per-node parameters"
            )

        job = _job_for_request(request)
        cancel_done, cancel_thread = _start_cancellation_bridge(request, job)
        scratch = request.output_dir.parent / "work"
        scratch.mkdir(parents=True, exist_ok=True)
        vocals_path = scratch / "vocals.wav"
        shutil.copy2(request.inputs["audio"].path, vocals_path)
        try:
            produced_names = split_vocals(job, scratch)
            request.output_dir.mkdir(parents=True, exist_ok=True)
            default_map = {"lead": "lead_vocals", "backing": "backing_vocals"}
            output_map = {**default_map, **request.model.runtime.get("output_stem_map", {})}
            outputs: dict[str, Path] = {}
            for port in request.model.outputs:
                source_name = str(output_map.get(port, port))
                if source_name not in produced_names:
                    raise AdapterError(
                        f"Vocal split did not report '{source_name}' required by port '{port}'"
                    )
                source = scratch / f"{source_name}.wav"
                if not source.is_file():
                    raise AdapterError(f"Vocal split output is missing: {source.name}")
                target = request.output_dir / f"{port}.wav"
                shutil.copy2(source, target)
                outputs[port] = target
            return outputs
        finally:
            _stop_cancellation_bridge(cancel_done, cancel_thread)
            shutil.rmtree(scratch, ignore_errors=True)
