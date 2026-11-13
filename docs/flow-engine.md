# Experimental Flow Engine

This directory contains the first implementation of a typed audio-processing graph for StemDeck.

It is intentionally isolated from the production import pipeline: the existing `separate()` and `split_vocals()` paths are unchanged, while native adapters let the graph reuse them.

## Goals of this prototype

- Define processing pipelines as text (YAML).
- Support one or many source tracks.
- Validate technical and semantic port compatibility before execution.
- Plan a DAG into dependency stages and expose which nodes may run in parallel.
- Persist every node's artifacts and a run manifest.
- Reuse content-addressed cached node results across runs.
- Keep model execution behind adapters so the graph is independent from Demucs/UVR naming.
- Allow additional `audio-separator` models to be described as YAML instead of hardcoded classes.
- Allow experimental models to run in isolated runtimes without forcing their dependency stack into StemDeck's main Python environment.

## Semantic typing

Technical artifacts are audio files. Musical meaning is represented separately by semantic types.

The current prototype uses Python inheritance for compatibility checks, for example:

- `LeadVocals` is a subtype of `Vocals`.
- `BackingVocals` is a subtype of `Vocals`.
- `LeadGuitar` and `RhythmGuitar` are subtypes of `Guitar`.
- `Piano` and `Keyboard` are subtypes of `Keys`.

A model accepting `vocals` therefore accepts an artifact typed as `lead_vocals`, while a `bass` artifact is rejected in strict mode.

## Stable demo flow

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

This remains the stable demo flow.

## Experimental guitar branch

`docs/flows/demo-vocals-guitar.yaml` extends the stable demo with an experimental guitar-processing branch:

```text
                 +-> vocal_split
song -> primary -+
                 +-> guitar_split
```

The `primary` node first runs Demucs `htdemucs_6s`.

The resulting `vocals` stem is passed to the existing vocal-split path, while the `guitar` stem may be passed to the experimental local RoFormer adapter to produce:

- `lead_guitar`
- `rhythm_guitar`

The planner exposes `vocal_split` and `guitar_split` in the same execution stage:

```text
stage 0: primary
stage 1: guitar_split, vocal_split
```

The current executor still serializes nodes within a stage. Resource-aware parallel scheduling is planned separately.

## Validate

With a normal StemDeck development environment installed:

```bash
python -m app.flow.cli validate docs/flows/demo-vocals.yaml
```

For the experimental guitar branch:

```bash
python -m app.flow.cli validate docs/flows/demo-vocals-guitar.yaml
```

Validation reports graph/schema errors, the execution stages, and whether the required adapters are available on the current machine.

Before execution, the capability check also runs adapter preflight checks.

The native vocal-split adapter requires `ffmpeg` on `PATH`.

The native Demucs adapter resolves the packaged checkpoint metadata; when a required checkpoint is not already cached, it verifies that the upstream URL is reachable before any node starts.

This turns missing tools and broken model-download TLS/network paths into an early flow capability error instead of a mid-run failure.

The experimental RoFormer adapter additionally verifies:

- the isolated Python runtime;
- the installed `audio-separator` version;
- the checkpoint path;
- the YAML configuration path;
- that the supplied configuration is recognized as a supported RoFormer architecture.

## Run

Stable demo:

```bash
python -m app.flow.cli run docs/flows/demo-vocals.yaml \
  --input song=/path/to/song.wav \
  --runs-dir ./flow-runs \
  --cache-dir ./flow-cache
```

Experimental guitar branch:

```bash
python -m app.flow.cli run docs/flows/demo-vocals-guitar.yaml \
  --input song=/path/to/song.wav \
  --runs-dir ./flow-runs \
  --cache-dir ./flow-cache
```

The final tracks are placed in `<run>/outputs/`.

Intermediate node outputs live under:

```text
<run>/nodes/<node>/outputs/
```

`run.json` records:

- the flow snapshot;
- node parameters;
- adapter versions;
- runtime identity;
- input hashes;
- output artifacts;
- execution plan;
- environment information;
- model-artifact provenance.

The manifest also records the current Python/OS/package/FFmpeg environment, hashes resolved model checkpoint files, and probes audio artifacts for:

- sample rate;
- channels;
- duration;
- codec/sample format;
- bit depth;
- bit rate;
- container format;

when `ffprobe` is available.

Cache entries preserve the environment and model-artifact provenance from the run that produced them.

A later cache hit therefore reports the original producing environment instead of silently substituting facts from the current machine.

Older cache entries created before provenance support fall back to the current environment and are marked accordingly.

The result directory is intentionally DAW-agnostic and can be imported manually into SoundCraft.

## External model definitions

The generic `audio-separator` adapter can use model definitions loaded from a directory:

```bash
python -m app.flow.cli validate my-flow.yaml --model-dir ./my-models
```

A verified example for the model StemDeck already uses is available at:

```text
docs/flows/models/uvr-mdxnet-karaoke-2.yaml
```

Its output-label mapping mirrors `app/pipeline/vocal_split_worker.py`:

```text
Vocals       -> lead
Instrumental -> backing
```

## Cache model

A node cache key currently contains:

- model id;
- optional declared model hash;
- adapter id and version;
- adapter runtime identity;
- node parameters;
- SHA-256 of every input artifact.

This means downstream nodes may stay cached even if an upstream node reruns, provided the newly produced bytes are identical.

For the experimental local RoFormer adapter, the runtime identity also includes:

- checkpoint SHA-256;
- configuration SHA-256;
- isolated Python runtime path;
- detected `audio-separator` version.

The checkpoint and configuration are also recorded as model artifacts in the run manifest.

## User-provided local RoFormer models

The Flow Engine can run a local RoFormer checkpoint without adding it to `python-audio-separator`'s public model registry.

This is intended for models that are technically compatible with `audio-separator` but are not bundled or redistributed by StemDeck.

The built-in experimental model definition:

```text
local-lead-rhythm-guitar-roformer
```

expects an isolated Python runtime and two user-provided model files.

Configure them with:

```bash
export STEMDECK_GUITAR_SPLIT_PYTHON="/absolute/path/to/roformer-runtime/bin/python"

export STEMDECK_GUITAR_SPLIT_CHECKPOINT="/absolute/path/to/lead-rhythm.ckpt"

export STEMDECK_GUITAR_SPLIT_CONFIG="/absolute/path/to/lead-rhythm.yaml"
```

The isolated runtime must provide a compatible version of `audio-separator`.

The current experimental adapter requires:

```text
audio-separator >= 0.48
```

The adapter launches an isolated Python subprocess and runs the checkpoint through `audio-separator`'s MDXC/RoFormer execution path.

This keeps the experimental RoFormer dependency stack separate from StemDeck's main Python environment.

The flow remains portable because local filesystem paths are not stored directly in the flow definition. They are resolved at runtime from the model definition and environment variables.

StemDeck does not bundle these user-provided files and does not make any claim about their redistribution license.

Users are responsible for obtaining and using checkpoints in accordance with their licenses.

## audio-separator compatibility note

`audio-separator 0.48.0` accepts `mlp_expansion_factor` for `MelBandRoformer`, but its current `MelBandRoformer` implementation does not propagate that value to `MaskEstimator`.

As a consequence, checkpoints trained with a non-default expansion factor can fail to load with tensor shape mismatches.

For example, a model configured with:

```yaml
dim: 384
mlp_expansion_factor: 2
```

expects a hidden width of:

```text
384 * 2 = 768
```

while the unpatched `MaskEstimator` default expansion factor of `4` would construct:

```text
384 * 4 = 1536
```

This produces checkpoint/model tensor mismatches during loading.

The experimental StemDeck runner contains a scoped compatibility workaround for this behavior.

The workaround is applied only inside the isolated RoFormer subprocess.

StemDeck does not modify the installed `audio-separator` package and does not patch the main application runtime.

The workaround should be removed once a compatible upstream `audio-separator` release propagates `mlp_expansion_factor` correctly.

## Testing

The Flow Engine test suite covers:

- semantic type inheritance;
- stable demo validation;
- experimental guitar-branch validation;
- DAG stage planning;
- parallel branch exposure;
- adapter capability checks;
- multiple named inputs;
- cache reuse;
- provenance preservation;
- native Demucs adapter integration;
- native vocal-split adapter integration;
- RoFormer `mlp_expansion_factor` compatibility behavior.

Run:

```bash
uv sync --extra dev
uv run pytest tests/test_flow_engine.py -q
```

At the time of this prototype, the Flow Engine test suite contains nine passing tests.

Warnings originating from third-party packages such as `rotary_embedding_torch` are not treated as Flow Engine failures.

## Current limits

- Execution is serial even when the planner marks nodes as parallelizable. Resource-aware GPU/CPU scheduling comes later.
- `retain` / `expose` are represented in the schema, but lifecycle cleanup is intentionally deferred while the filesystem layout remains useful for debugging the prototype.
- Model provenance currently covers resolved local checkpoint files used by the built-in Demucs, StemDeck vocal-split, generic `audio-separator`, and local RoFormer adapters.
- Remote-only checkpoints that have not been downloaded yet cannot have a local SHA-256 recorded until execution materializes them.
- No graphical editor or StemDeck studio UI is connected yet.
- No lead/rhythm guitar checkpoint is bundled or redistributed by StemDeck.
- The experimental `local-lead-rhythm-guitar-roformer` definition requires a user-provided compatible checkpoint and YAML configuration.
- Experimental RoFormer execution currently depends on an isolated Python runtime.
- The current compatibility workaround for `audio-separator 0.48.0` should eventually be replaced by an upstream fix.