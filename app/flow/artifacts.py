from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
import json


@dataclass(frozen=True)
class AudioArtifact:
    id: str
    path: Path
    semantic_type: str
    sha256: str
    artifact_type: str = "audio"
    producer: str | None = None
    producer_port: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    duration_seconds: float | None = None
    codec_name: str | None = None
    sample_format: str | None = None
    bit_depth: int | None = None
    bit_rate: int | None = None
    container_format: str | None = None

    def to_json_dict(self) -> dict:
        data = asdict(self)
        data["path"] = str(self.path)
        return data


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def save_artifact_manifest(artifact: AudioArtifact, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(artifact.to_json_dict(), indent=2), encoding="utf-8")
