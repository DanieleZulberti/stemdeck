from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from threading import RLock
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
        self._lock = RLock()

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
        # Multiple independent nodes may resolve to the same content key. Keep
        # publication of one cache entry atomic inside this process.
        with self._lock:
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
            now = datetime.now(UTC).isoformat()
            manifest = {
                "metadata": metadata,
                "outputs": manifest_outputs,
                "created_at": now,
                "last_accessed_at": now,
            }
            (temp / "manifest.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8"
            )
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
        with self._lock:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            data["last_accessed_at"] = datetime.now(UTC).isoformat()
            manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        output_dir.mkdir(parents=True, exist_ok=True)
        result: dict[str, Path] = {}
        for port, entry in data["outputs"].items():
            source = self.entry_dir(key) / entry["file"]
            target = output_dir / source.name
            self._link_or_copy(source, target)
            result[port] = target
        return result

    def collect(
        self, *, dry_run: bool = True, max_idle_seconds: float | None = None
    ) -> dict[str, Any]:
        """Optionally prune content-addressed entries by idle age.

        No cache entry is deleted unless an explicit `max_idle_seconds` policy
        is supplied. This keeps `retain=cache` meaningful without inventing an
        automatic TTL in M9. Legacy manifests without access timestamps use the
        manifest file mtime as their last-access time.
        """
        if max_idle_seconds is None:
            return {
                "enabled": False,
                "dry_run": dry_run,
                "max_idle_seconds": None,
                "entries": [],
                "deleted_files": 0,
                "reclaimed_bytes": 0,
            }
        if max_idle_seconds < 0:
            raise ValueError("max_idle_seconds must be >= 0")
        now = datetime.now(UTC)
        entries: list[dict[str, Any]] = []
        deleted_files = 0
        reclaimed_bytes = 0
        with self._lock:
            for entry in sorted(self.root.iterdir()):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                manifest = entry / "manifest.json"
                if not manifest.is_file():
                    continue
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    entries.append({
                        "key": entry.name,
                        "action": "protected",
                        "reason": "invalid_manifest",
                    })
                    continue
                stamp = data.get("last_accessed_at") or data.get("created_at")
                try:
                    accessed = datetime.fromisoformat(stamp) if stamp else None
                    if accessed is None:
                        raise ValueError
                    if accessed.tzinfo is None:
                        accessed = accessed.replace(tzinfo=UTC)
                    idle = max(0.0, (now - accessed.astimezone(UTC)).total_seconds())
                except (TypeError, ValueError):
                    idle = max(0.0, now.timestamp() - manifest.stat().st_mtime)
                if idle < max_idle_seconds:
                    entries.append({
                        "key": entry.name,
                        "action": "protected",
                        "reason": "recently_accessed",
                        "idle_seconds": idle,
                    })
                    continue
                size = sum(
                    child.stat().st_size
                    for child in entry.rglob("*")
                    if child.is_file()
                )
                file_count = sum(1 for child in entry.rglob("*") if child.is_file())
                action = "would_delete" if dry_run else "deleted"
                entries.append({
                    "key": entry.name,
                    "action": action,
                    "reason": "idle_ttl",
                    "idle_seconds": idle,
                    "bytes": size,
                })
                if not dry_run:
                    shutil.rmtree(entry)
                    deleted_files += file_count
                    reclaimed_bytes += size
        return {
            "enabled": True,
            "dry_run": dry_run,
            "max_idle_seconds": float(max_idle_seconds),
            "entries": entries,
            "deleted_files": deleted_files,
            "reclaimed_bytes": reclaimed_bytes,
        }

    @staticmethod
    def _link_or_copy(source: Path, target: Path) -> None:
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
