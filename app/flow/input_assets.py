from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

from app.core.config import CACHE_DIR, JOBS_DIR, STEM_NAMES, ffmpeg_executable, ffprobe_executable

from app.core.registry import all_jobs


class FlowInputAssetError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


_ASSET_PREFIX = "job"
_UPLOAD_PREFIX = "upload"
_FULL_KIND = "full"
_UPLOAD_KIND = "audio"
_CACHE_ROOT = CACHE_DIR / "flow-input-assets"
_UPLOAD_ROOT = CACHE_DIR / "flow-input-uploads"
_ALLOWED_UPLOAD_EXTS = {".mp3", ".wav", ".flac", ".mp4", ".m4a", ".ogg", ".opus"}
_MAX_UPLOAD_BYTES = 400 * 1024 * 1024
_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _safe_job(job_id: str):
    job = all_jobs().get(job_id)
    if job is None or job.status != "done" or job.trashed_at is not None:
        raise FlowInputAssetError("asset_not_found", "Library track is not available")
    return job


def _retained_source(job_id: str) -> Path | None:
    root = JOBS_DIR.resolve()
    job_dir = (JOBS_DIR / job_id).resolve()
    if not job_dir.is_dir() or not job_dir.is_relative_to(root):
        return None
    for candidate in sorted(job_dir.glob("source.*")):
        path = candidate.resolve()
        if path.is_file() and path.is_relative_to(root):
            return path
    return None


def _stem_paths(job_id: str) -> list[Path]:
    root = JOBS_DIR.resolve()
    stems_dir = (JOBS_DIR / job_id / "stems").resolve()
    if not stems_dir.is_dir() or not stems_dir.is_relative_to(root):
        return []
    paths = []
    for name in STEM_NAMES:
        path = (stems_dir / f"{name}.wav").resolve()
        if path.is_file() and path.is_relative_to(root):
            paths.append(path)
    return paths


def list_flow_input_assets() -> list[dict]:
    assets = []
    for job in sorted(all_jobs().values(), key=lambda item: item.created_at, reverse=True):
        if job.status != "done" or job.trashed_at is not None:
            continue
        if _retained_source(job.id) is None and not _stem_paths(job.id):
            continue
        assets.append(
            {
                "asset_id": f"{_ASSET_PREFIX}:{job.id}:{_FULL_KIND}",
                "job_id": job.id,
                "title": job.title or f"Track {job.id[:6]}",
                "duration": job.duration_sec,
                "kind": _FULL_KIND,
                "source": "library",
            }
        )
    return assets




def store_flow_input_upload(filename: str, file_obj: object) -> dict:
    """Persist a direct Flow Run upload behind an opaque asset reference."""
    safe_name = Path(filename or "audio").name
    ext = Path(safe_name).suffix.lower()
    if ext not in _ALLOWED_UPLOAD_EXTS:
        accepted = ", ".join(sorted(_ALLOWED_UPLOAD_EXTS))
        raise FlowInputAssetError("asset_upload_type_unsupported", f"Unsupported file type '{ext}'. Accepted: {accepted}")

    file_obj.seek(0, 2)
    size = file_obj.tell()
    file_obj.seek(0)
    if size <= 0:
        raise FlowInputAssetError("asset_upload_empty", "Uploaded audio file is empty")
    if size > _MAX_UPLOAD_BYTES:
        raise FlowInputAssetError("asset_upload_too_large", "Audio file exceeds 400 MB limit")

    upload_id = uuid.uuid4().hex
    _UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    target = (_UPLOAD_ROOT / f"{upload_id}{ext}").resolve()
    root = _UPLOAD_ROOT.resolve()
    if not target.is_relative_to(root):
        raise FlowInputAssetError("asset_invalid", "Invalid Flow input upload path")

    with target.open("wb") as out:
        while True:
            chunk = file_obj.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)

    try:
        probe = subprocess.run(
            [
                ffprobe_executable(),
                "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(target),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        if probe.returncode != 0:
            raise ValueError(probe.stderr.strip() or "ffprobe failed")
        duration = float(probe.stdout.strip())
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise FlowInputAssetError("asset_upload_invalid_audio", f"Could not read uploaded audio: {exc}") from exc

    title = Path(safe_name).stem or "Uploaded audio"
    return {
        "asset_id": f"{_UPLOAD_PREFIX}:{upload_id}:{_UPLOAD_KIND}",
        "title": title,
        "duration": duration,
        "kind": _FULL_KIND,
        "source": "upload",
    }


def resolve_flow_input_asset(asset_id: str) -> Path:
    try:
        prefix, object_id, kind = asset_id.split(":", 2)
    except ValueError as exc:
        raise FlowInputAssetError("asset_invalid", "Invalid Flow input asset") from exc

    if prefix == _UPLOAD_PREFIX and kind == _UPLOAD_KIND:
        if not _UPLOAD_ID_RE.fullmatch(object_id):
            raise FlowInputAssetError("asset_invalid", "Invalid Flow input upload reference")
        root = _UPLOAD_ROOT.resolve()
        candidates = list(root.glob(f"{object_id}.*")) if root.is_dir() else []
        safe = [p.resolve() for p in candidates if p.is_file() and p.resolve().is_relative_to(root)]
        if len(safe) != 1:
            raise FlowInputAssetError("asset_not_found", "Uploaded Flow input is no longer available")
        return safe[0]

    if prefix != _ASSET_PREFIX or kind != _FULL_KIND:
        raise FlowInputAssetError("asset_invalid", "Unsupported Flow input asset")

    job_id = object_id
    _safe_job(job_id)
    source = _retained_source(job_id)
    if source is not None:
        return source

    stems = _stem_paths(job_id)
    if not stems:
        raise FlowInputAssetError("asset_unavailable", "The library track has no usable audio")

    _CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    output = (_CACHE_ROOT / f"{job_id}-full.wav").resolve()
    if output.is_file():
        return output

    # Remote imports intentionally discard their original download after stem
    # extraction. Reconstructing the full track from the immutable base stems
    # gives Flow Runs a stable library asset without exposing server paths to
    # the browser or downloading the source again.
    tmp = _CACHE_ROOT / f".{job_id}.{uuid.uuid4().hex}.tmp.wav"
    args = [ffmpeg_executable(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    for stem in stems:
        args += ["-i", str(stem)]
    labels = "".join(f"[{i}:a]" for i in range(len(stems)))
    args += [
        "-filter_complex",
        f"{labels}amix=inputs={len(stems)}:normalize=0:dropout_transition=0[a]",
        "-map",
        "[a]",
        "-c:a",
        "pcm_s16le",
        str(tmp),
    ]
    try:
        subprocess.run(args, check=True, capture_output=True, text=True)
        tmp.replace(output)
    except (OSError, subprocess.CalledProcessError) as exc:
        tmp.unlink(missing_ok=True)
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        raise FlowInputAssetError("asset_materialization_failed", f"Could not prepare library audio: {detail}") from exc
    return output
