from __future__ import annotations

from importlib import metadata
from pathlib import Path
import json
import platform
import shutil
import subprocess
import sys
from typing import Any

from .artifacts import hash_file


def package_version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def executable_version(executable: str, *args: str) -> str | None:
    path = shutil.which(executable)
    if not path:
        return None
    try:
        result = subprocess.run(
            [path, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except OSError:
        return None
    output = (result.stdout or result.stderr).splitlines()
    return output[0].strip() if output else None


def git_commit(root: Path) -> str | None:
    if not (root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except OSError:
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def runtime_environment(repo_root: Path) -> dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "stemdeck": package_version("stemdeck"),
        "stemdeck_git_commit": git_commit(repo_root),
        "torch": package_version("torch"),
        "torchaudio": package_version("torchaudio"),
        "demucs": package_version("demucs"),
        "audio_separator": package_version("audio-separator"),
        "onnxruntime": package_version("onnxruntime"),
        "ffmpeg": executable_version("ffmpeg", "-version"),
        "ffprobe": executable_version("ffprobe", "-version"),
    }


def model_artifact(path: Path, *, source_url: str | None = None) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return {
        "name": path.name,
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "sha256": hash_file(path),
        "source_url": source_url,
    }


def probe_audio(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {}
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        (
            "stream=sample_rate,channels,codec_name,sample_fmt,bits_per_sample,"
            "bits_per_raw_sample,bit_rate:format=duration,bit_rate,format_name"
        ),
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except OSError:
        return {}
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    streams = payload.get("streams") or []
    stream = streams[0] if streams else {}
    fmt = payload.get("format") or {}

    def _int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    bit_depth = _int(stream.get("bits_per_raw_sample")) or _int(stream.get("bits_per_sample"))
    return {
        "sample_rate": _int(stream.get("sample_rate")),
        "channels": _int(stream.get("channels")),
        "duration_seconds": _float(fmt.get("duration")),
        "codec_name": stream.get("codec_name"),
        "sample_format": stream.get("sample_fmt"),
        "bit_depth": bit_depth,
        "bit_rate": _int(stream.get("bit_rate")) or _int(fmt.get("bit_rate")),
        "container_format": fmt.get("format_name"),
    }
