from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import app.flow.input_assets as assets


@dataclass
class FakeJob:
    id: str = "abcdef123456"
    status: str = "done"
    trashed_at: float | None = None
    title: str = "My Track"
    duration_sec: float = 42.5
    created_at: float = 10.0


def test_library_asset_listing_is_opaque(monkeypatch, tmp_path):
    job = FakeJob()
    source = tmp_path / "source.wav"
    source.write_bytes(b"audio")
    monkeypatch.setattr(assets, "all_jobs", lambda: {job.id: job})
    monkeypatch.setattr(assets, "_retained_source", lambda _job_id: source)
    monkeypatch.setattr(assets, "_stem_paths", lambda _job_id: [])

    result = assets.list_flow_input_assets()
    assert result == [
        {
            "asset_id": "job:abcdef123456:full",
            "job_id": "abcdef123456",
            "title": "My Track",
            "duration": 42.5,
            "kind": "full",
            "source": "library",
        }
    ]
    assert str(source) not in str(result)


def test_resolver_prefers_retained_source(monkeypatch, tmp_path):
    job = FakeJob()
    source = tmp_path / "source.flac"
    source.write_bytes(b"audio")
    monkeypatch.setattr(assets, "all_jobs", lambda: {job.id: job})
    monkeypatch.setattr(assets, "_retained_source", lambda _job_id: source)

    assert assets.resolve_flow_input_asset("job:abcdef123456:full") == source


def test_invalid_asset_reference_is_rejected():
    try:
        assets.resolve_flow_input_asset("../../etc/passwd")
    except assets.FlowInputAssetError as exc:
        assert exc.code == "asset_invalid"
    else:
        raise AssertionError("invalid opaque asset reference was accepted")


def test_direct_upload_is_opaque_and_resolvable(monkeypatch, tmp_path):
    upload_root = tmp_path / "uploads"
    monkeypatch.setattr(assets, "_UPLOAD_ROOT", upload_root)

    class FakeFile:
        def __init__(self, data: bytes):
            import io
            self._io = io.BytesIO(data)
        def seek(self, *args): return self._io.seek(*args)
        def tell(self): return self._io.tell()
        def read(self, *args): return self._io.read(*args)

    class Probe:
        returncode = 0
        stdout = "12.5\n"
        stderr = ""

    monkeypatch.setattr(assets.subprocess, "run", lambda *args, **kwargs: Probe())
    result = assets.store_flow_input_upload("My Song.wav", FakeFile(b"RIFFfake-audio"))

    assert result["source"] == "upload"
    assert result["title"] == "My Song"
    assert result["duration"] == 12.5
    assert result["asset_id"].startswith("upload:")
    assert str(upload_root) not in str(result)

    resolved = assets.resolve_flow_input_asset(result["asset_id"])
    assert resolved.is_file()
    assert resolved.read_bytes() == b"RIFFfake-audio"


def test_direct_upload_reference_rejects_non_generated_ids(monkeypatch, tmp_path):
    monkeypatch.setattr(assets, "_UPLOAD_ROOT", tmp_path)
    try:
        assets.resolve_flow_input_asset("upload:*:audio")
    except assets.FlowInputAssetError as exc:
        assert exc.code == "asset_invalid"
    else:
        raise AssertionError("wildcard upload reference was accepted")
