from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from typing import Any


@dataclass(frozen=True)
class CacheKeyInput:
    model_id: str
    model_hash: str | None
    adapter: str
    adapter_version: str
    parameters: dict[str, Any]
    input_hashes: dict[str, str]
    runtime_identity: dict[str, Any]

    def digest(self) -> str:
        payload = {
            "model_id": self.model_id,
            "model_hash": self.model_hash,
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "parameters": self.parameters,
            "input_hashes": self.input_hashes,
            "runtime_identity": self.runtime_identity,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return sha256(encoded).hexdigest()


class ArtifactCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def entry_dir(self, key: str) -> Path:
        return self.root / key

    def has(self, key: str) -> bool:
        manifest = self.entry_dir(key) / "manifest.json"
        if not manifest.exists():
            return False
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            return all(
                (self.entry_dir(key) / item["file"]).is_file()
                for item in data["outputs"].values()
            )
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def store(self, key: str, outputs: dict[str, Path], metadata: dict[str, Any]) -> None:
        target = self.entry_dir(key)
        temp = self.root / f".{key}.tmp"
        if temp.exists():
            shutil.rmtree(temp)
        temp.mkdir(parents=True)
        manifest_outputs: dict[str, dict[str, str]] = {}
        for port, source in outputs.items():
            suffix = source.suffix or ".wav"
            filename = f"{port}{suffix}"
            destination = temp / filename
            self._link_or_copy(source, destination)
            manifest_outputs[port] = {"file": filename}
        manifest = {"metadata": metadata, "outputs": manifest_outputs}
        (temp / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if target.exists():
            shutil.rmtree(temp)
            return
        os.replace(temp, target)

    def metadata(self, key: str) -> dict[str, Any]:
        manifest_path = self.entry_dir(key) / "manifest.json"
        if not manifest_path.exists():
            raise KeyError(f"Cache entry '{key}' does not exist")
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        metadata = data.get("metadata", {})
        return metadata if isinstance(metadata, dict) else {}

    def materialize(self, key: str, output_dir: Path) -> dict[str, Path]:
        manifest_path = self.entry_dir(key) / "manifest.json"
        if not manifest_path.exists():
            raise KeyError(f"Cache entry '{key}' does not exist")
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        output_dir.mkdir(parents=True, exist_ok=True)
        result: dict[str, Path] = {}
        for port, entry in data["outputs"].items():
            source = self.entry_dir(key) / entry["file"]
            target = output_dir / source.name
            self._link_or_copy(source, target)
            result[port] = target
        return result

    @staticmethod
    def _link_or_copy(source: Path, target: Path) -> None:
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
