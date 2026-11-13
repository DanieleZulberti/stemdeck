# Experimental Flow Engine

This directory contains the first implementation of a typed audio-processing graph for StemDeck.
It is intentionally isolated from the production import pipeline: the existing `separate()` and
`split_vocals()` paths are unchanged, while native adapters let the graph reuse them.

## Goals of this prototype

- Define processing pipelines as text (YAML).
- Support one or many source tracks.
- Validate technical and semantic port compatibility before execution.
- Plan a DAG into dependency stages and expose which nodes may run in parallel.
- Persist every node's artifacts and a run manifest.
- Reuse content-addressed cached node results across runs.
- Keep model execution behind adapters so the graph is independent from Demucs/UVR naming.
- Allow additional `audio-separator` models to be described as YAML instead of hardcoded classes.

## Semantic typing

Technical artifacts are audio files. Musical meaning is represented separately by semantic types.
The current prototype uses Python inheritance for compatibility checks, for example:

- `LeadVocals` is a subtype of `Vocals`.
- `BackingVocals` is a subtype of `Vocals`.
- `LeadGuitar` and `RhythmGuitar` are subtypes of `Guitar`.
- `Piano` and `Keyboard` are subtypes of `Keys`.

A model accepting `vocals` therefore accepts an artifact typed as `lead_vocals`, while a `bass`
artifact is rejected in strict mode.

## Demo flow

`docs/flows/demo-vocals.yaml` uses only execution paths already present in StemDeck:

```text
song
  |
  v
htdemucs_6s
  |-- vocals --> UVR-MDX-NET Karaoke 2 --> lead vocals
  |                                    --> backing vocals
  |-- guitar
  |-- bass
  |-- drums
  |-- piano
  `-- other
```

The guitar stem is deliberately not split into lead/rhythm yet. A role-aware local checkpoint has
not been selected and verified for licensing/runtime compatibility, so the repository does not
pretend one exists.

## Validate

With a normal StemDeck development environment installed:

```bash
python -m app.flow.cli validate docs/flows/demo-vocals.yaml
```

Validation reports graph/schema errors, the execution stages, and whether the required adapters are
available on the current machine.

Before execution, the capability check also runs adapter preflight checks. The native vocal-split
adapter requires `ffmpeg` on `PATH`. The native Demucs adapter resolves the packaged checkpoint
metadata; when a required checkpoint is not already cached, it verifies that the upstream URL is
reachable before any node starts. This turns missing tools and broken model-download TLS/network
paths into an early flow capability error instead of a mid-run failure.

## Run

```bash
python -m app.flow.cli run docs/flows/demo-vocals.yaml \
  --input song=/path/to/song.wav \
  --runs-dir ./flow-runs \
  --cache-dir ./flow-cache
```

The final tracks are placed in `<run>/outputs/`. Intermediate node outputs live under
`<run>/nodes/<node>/outputs/`, and `run.json` records the flow snapshot, node parameters, adapter
versions, runtime identity, input hashes, output artifacts, and execution plan. The manifest also
records the current Python/OS/package/FFmpeg environment, hashes resolved model checkpoint files,
and probes audio artifacts for sample rate, channels, duration, codec/sample format, bit depth, bit
rate, and container format when `ffprobe` is available.

Cache entries preserve the environment and model-artifact provenance from the run that produced
them. A later cache hit therefore reports the original producing environment instead of silently
substituting facts from the current machine. Older cache entries created before provenance support
fall back to the current environment and are marked accordingly.

The result directory is intentionally DAW-agnostic and can be imported manually into SoundCraft.

## External model definitions

The generic `audio-separator` adapter can use model definitions loaded from a directory:

```bash
python -m app.flow.cli validate my-flow.yaml --model-dir ./my-models
```

A verified example for the model StemDeck already uses is available at
`docs/flows/models/uvr-mdxnet-karaoke-2.yaml`. Its output-label mapping mirrors
`app/pipeline/vocal_split_worker.py` (`Vocals` -> lead, `Instrumental` -> backing).

## Cache model

A node cache key currently contains:

- model id;
- optional declared model hash;
- adapter id and version;
- adapter runtime identity;
- node parameters;
- SHA-256 of every input artifact.

This means downstream nodes may stay cached even if an upstream node reruns, provided the newly
produced bytes are identical.

## Current limits

- Execution is serial even when the planner marks nodes as parallelizable. Resource-aware GPU/CPU
  scheduling comes later.
- `retain` / `expose` are represented in the schema, but lifecycle cleanup is intentionally deferred
  while the filesystem layout is useful for debugging the prototype.
- Model provenance currently covers resolved local checkpoint files used by the built-in Demucs,
  StemDeck vocal-split, and generic `audio-separator` adapters. Remote-only checkpoints that have
  not been downloaded yet cannot have a local SHA-256 recorded until execution materializes them.
- No graphical editor or StemDeck studio UI is connected yet.
- No lead/rhythm guitar model is bundled or assumed.
