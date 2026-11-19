# StemDeck Flow Engine Architecture

Status: active design document  
Scope: Flow Engine, Flow Editor, Flow persistence, Flow Run lifecycle, and Studio integration  
Last updated: 2026-11-17

## Purpose

This document is the architectural source of truth for the StemDeck Flow feature. It records consolidated product constraints, implementation decisions, current repository findings, open decisions, and milestone boundaries.

It must be updated whenever a change affects a public/domain contract, persistence model, execution semantics, Studio binding semantics, or frontend/backend responsibility.

## Current repository baseline

The current product and the Flow PoC are separate subsystems.

- The main web application is a static, browser-native ES Modules application with no runtime JavaScript framework or build step.
- Shared frontend state is maintained by JavaScript modules and mutable bindings rather than a dedicated state-management library.
- FastAPI routes are grouped by concern under `app/api/`.
- Existing processing is represented by `Job` and executed by a global serial queue.
- Job progress is delivered with Server-Sent Events (SSE); cancellation is Job/subprocess based.
- Audio outputs are stored in per-job directories and the job registry is persisted in `registry.json`.
- Studio/library track metadata is primarily frontend/Tauri persisted state, with browser localStorage as fallback.
- The Flow subsystem lives under `app/flow/` and has dedicated FastAPI contracts for documents, node definitions, Flow Run monitoring, direct Flow/Studio bindings, and artifact lifecycle. It remains intentionally separate from the legacy Job queue. Project tables/API remain only as a backward-compatibility persistence layer for M7-M9.6 data and are no longer part of the normal product workflow.

### Existing Flow PoC capabilities

The PoC already contains useful runtime foundations:

- typed flow schema;
- semantic audio typing and inheritance;
- graph validation;
- capability/preflight checks;
- topological DAG planning with parallel stages;
- execution adapters for existing StemDeck processing paths;
- content-addressed cache;
- run manifests and provenance;
- multiple flow inputs;
- isolated local RoFormer runtime;
- automatic tests.

The Flow runtime executes independent DAG stage members concurrently subject to adapter resource claims, isolates sibling branch failures, persists Run/NodeRun lifecycle, preserves partial outputs, supports explicit Flow/Studio Apply with rerun reconciliation, and now has reference-aware artifact lifecycle/GC.

## Core architectural principles

### AP-001 — Separate editor persistence from runtime definition

**Decision: accepted.**

The Flow system has two distinct graph models:

- `FlowDocument`: persistence/editor model. It may be incomplete or invalid and must remain saveable.
- `FlowDefinition`: compiled runtime model. It contains only the information required to validate, plan, and execute a flow.

Canonical pipeline:

```text
FlowDocument -> compile -> FlowDefinition -> validate -> capability check -> plan -> execute
```

A saved document is not required to compile, be valid, or be runnable.

### AP-002 — YAML is interchange, not primary persistence

**Decision: accepted.**

YAML remains supported for import/export and for repository-owned examples/built-ins where useful. The editor must not use YAML files as its primary persistence abstraction.

### AP-003 — Backend-driven generic Flow Editor

**Decision: accepted.**

The Flow Editor must render node types from backend-provided metadata. Frontend code must not contain model-specific branches for Demucs, UVR, RoFormer, or future processing implementations.

### AP-004 — `NodeDefinition` and `NodeInstance` are distinct concepts

**Decision: accepted.**

- `NodeDefinition`: static contract of a node type, registered by the backend.
- `NodeInstance`: one occurrence of that node type in a flow document/definition.

Current PoC terminology is inverted relative to this target model:

- current `ModelDefinition` is conceptually a `NodeDefinition`;
- current `NodeDefinition` is conceptually a `NodeInstance`.

Migration must preserve compatibility while these names are phased out of public contracts.

### AP-005 — Node definitions expose complete backend metadata

**Decision: accepted.**

A node definition must expose at least:

- stable type identifier;
- label/description where appropriate;
- category;
- input ports;
- output ports;
- technical artifact type;
- semantic type;
- parameter schema;
- current capability/status information or enough information for the capability API to provide it.

UI presentation hints may be added, but the runtime semantics remain backend-owned.

### AP-006 — Port compatibility has two dimensions

**Decision: accepted.**

A connection is compatible only when both dimensions are satisfied:

1. technical artifact type;
2. semantic type compatibility.

Semantic inheritance already implemented by the PoC remains a reusable foundation.

### AP-007 — Flow Input and Flow Output may be virtual canvas nodes

**Decision: accepted.**

The editor may render Flow Inputs and Flow Outputs as nodes for usability. They are not execution nodes and must not become `NodeRun` instances.

### AP-008 — Connections have one source of truth

**Decision: accepted.**

Node input bindings reference source outputs. Graphical edges are derived projections and are never persisted as an independent runtime source of truth.

This preserves the direction already used by the PoC (`node.inputs[port] -> source reference`).

### AP-009 — Incomplete graph states are persistable

**Decision: accepted.**

`FlowDocument` must allow graph states such as:

- missing node type;
- missing required bindings;
- dangling references;
- unavailable/deprecated node definitions;
- invalid parameter values;
- incomplete flow outputs.

Compilation reports errors rather than making persistence fail.

### AP-010 — Diagnostics use one structured contract

**Decision: accepted.**

Compiler, validator, capability checker, and execution surfaces use a common diagnostic shape with at least:

- `source`;
- `severity`;
- `code`;
- `message`;
- `node_id` when applicable;
- `port_id` when applicable;
- `output_id` when applicable.

The model may later add parameter IDs, paths, hints, and documentation references.

### AP-011 — Error domains remain distinct

**Decision: accepted.**

The UI/API must preserve the difference between:

- compile errors;
- validation errors;
- capability errors;
- execution errors.

A common `Diagnostic` shape does not collapse their source semantics.

### AP-012 — `valid` and `runnable` are distinct

**Decision: accepted.**

- `valid`: the compiled graph satisfies structural, typing, parameter, and graph rules.
- `runnable`: the valid graph can execute in the current runtime/environment.

A document may additionally be non-compilable.

### AP-013 — `retain` controls artifact lifecycle

**Decision: accepted.**

`retain` describes artifact retention/lifecycle and is not a UI publication flag.

M9 implements retention semantics through the artifact lifecycle service. `transient` artifacts are never admitted to the node-atomic content cache, `cache` artifacts remain cache-managed, and `project` artifacts are protected while the producing Flow remains Project-owned. Flow Output publication remains independent from these policies.

### AP-014 — Replace node-level `expose` with Flow Output publication semantics

**Decision: accepted.**

The current PoC `OutputPolicy.expose` is deprecated compatibility state and is not a publication contract.

Publishing is a property of explicit Flow Outputs, not arbitrary internal node outputs. `FlowOutputDefinition.publish` is a boolean publication flag and defaults to `true` for legacy imported YAML. Studio consumes only Flow Outputs where `publish == true`.

### AP-015 — Flow Engine is DAW-agnostic

**Decision: accepted.**

Flow compilation, validation, capability checking, planning, execution, cache, provenance, and artifact lifecycle must not depend on Studio/DAW concepts.

Studio integration is a separate application layer that consumes Flow Outputs.

### AP-016 — Runs are immutable snapshots

**Decision: accepted.**

Starting a run snapshots the compiled flow definition and execution-relevant configuration. Editing the source flow afterwards does not affect the running or completed run.

The current PoC already serializes the flow into `run.json`; this behavior will become an explicit domain invariant.

### AP-017 — `FlowRun` and `NodeRun` have explicit lifecycle

**Decision: accepted.**

Run lifecycle and per-node lifecycle become first-class persisted state rather than transient dictionaries inside the executor.

Initial target states:

`FlowRun`:

- `queued`;
- `running`;
- `succeeded`;
- `partially_failed`;
- `failed`;
- `cancelled`.

`NodeRun`:

- `pending`;
- `blocked`;
- `running`;
- `cached`;
- `succeeded`;
- `failed`;
- `cancelled`;
- `skipped`.

Exact transitions will be documented before scheduler implementation.

### AP-018 — Cache hits are part of observable execution state

**Decision: accepted.**

A cache hit is visible through persisted `NodeRun` state/events and in the UI. It is not merely an executor optimization.

### AP-019 — Independent branches survive sibling failure

**Decision: accepted.**

A failed node blocks only descendants that require its unavailable outputs. Independent runnable branches continue.

Partial successful outputs remain available.

### AP-020 — Retry is cache-aware

**Decision: accepted.**

Retry creates a new execution attempt/run snapshot as appropriate and reuses valid content-addressed cached results. Retry behavior must remain observable in run history/provenance.

### AP-021 — Apply to Studio is separate from Run completion

**Decision: accepted.**

Completing a Flow Run makes outputs available. It does not mutate Studio automatically.

For MVP, Apply to Studio is an explicit user action.

### AP-022 — Studio binding identity is stable

**Decision: accepted.**

A managed Studio track binds to:

```text
flow_id + flow_output_id
```

not to a specific `run_id` or artifact path.

A rerun can therefore replace the bound media source while preserving eligible DAW state.

### AP-023 — Re-apply is idempotent and reconciliatory

**Decision: accepted.**

Applying the same Flow Outputs repeatedly must not duplicate managed tracks.

When a rerun produces the same stable Flow Output, Apply updates the media source of the existing managed track while preserving mixer/editing state when temporally compatible.

If a previously bound output is removed from the flow, its existing track is not deleted automatically. It becomes detached/unmanaged.

### AP-024 — Built-in, User, and Project Flows are distinct

**Decision: accepted.**

- Built-in Flow: product-provided template/definition.
- User Flow: reusable user-owned flow outside a specific project.
- Project Flow: project-owned snapshot copied from a built-in/user flow or created in the project.

A Project Flow does not track live changes made to the source template after the copy.

### AP-025 — Identity, revision, schema version, and display name are separate

**Decision: accepted.**

At minimum the persistence model distinguishes:

- `flow_id`: stable logical identity;
- `name`: mutable display name;
- `revision`: content revision/version inside the logical flow history;
- `schema_version`: serialization contract version.

These values must not be overloaded into the current PoC `name`/`version` pair.

### AP-026 — Editor layout is separate from runtime logic

**Decision: accepted.**

Canvas position, viewport/layout, collapsed state, and other editor-only properties live in document/editor data and do not affect `FlowDefinition` or cache identity unless explicitly promoted to runtime semantics.

### AP-027 — Studio references protect artifacts from GC

**Decision: accepted.**

An artifact referenced by Studio/project persistence is live and cannot be removed by garbage collection.

Artifact GC is reference-aware and cannot be implemented as simple run-directory cleanup. Managed **and detached** Studio bindings are live references. M9 also preserves Run input snapshots and `run.json`, because retry and provenance depend on them.

### AP-028 — MVP temporal model is whole-file, origin zero

**Decision: accepted.**

Initial processing assumes whole-file media with timeline origin `0`. The design must avoid preventing later region/timeline-aware processing.

### AP-029 — Artifact model must be extensible beyond audio

**Decision: accepted.**

Technical artifact type is represented by an opaque string identifier on ports/artifacts rather than an `audio` enum/literal. The current executor may still support only `audio`; unsupported artifact types are a runtime capability concern, not a schema break.

Public/domain contracts must be able to evolve toward artifacts such as:

- MIDI;
- JSON/structured analysis;
- text/transcriptions;
- chord analysis;
- other typed data.

Technical type therefore belongs to the artifact/port contract rather than being an implicit audio invariant.

## Integration decisions

### AI-001 — Do not turn legacy `Job` into `FlowRun`

**Decision: accepted.**

The existing `Job` domain represents the fixed StemDeck processing pipeline and is tied to a global serial queue and Job-aware subprocess cancellation. `FlowRun` needs graph-level lifecycle, branch failure isolation, cache-aware node state, partial outputs, and per-node execution state.

The two domains may share lower-level processing functions/adapters but remain distinct orchestration models.

### AI-002 — Reuse the SSE delivery pattern, not the Job event schema

**Decision: accepted.**

The existing SSE approach is suitable for Flow monitoring, but Flow events must expose FlowRun/NodeRun state rather than translating graph execution into legacy Job snapshots.

### AI-003 — Legacy processing adapters must lose their hard dependency on `Job`

**Decision: accepted and implemented for the Flow runtime boundary.**

Native Flow adapters no longer require callers to provide `context["job"]`. Execution uses graph-native cancellation/progress/resource abstractions; adapters may create a private legacy `Job` bridge internally while the existing StemDeck processors are reused.

The graph-native abstractions cover:

- cancellation;
- progress reporting;
- resource/execution context.

Adapters may internally bridge those abstractions to legacy functions during migration.

### AI-004 — Graph parallelism and resource concurrency are separate

**Decision: accepted.**

A DAG may expose independent runnable nodes while the scheduler still serializes specific resource classes.

Examples:

- two GPU-heavy nodes may need serialization;
- CPU and GPU work may be allowed concurrently;
- legacy singleton workers may define exclusive resources.

The scheduler, not the graph model, owns this policy.

### AI-005 — Flow APIs are separate FastAPI routers

**Decision: accepted.**

Flow definitions/documents, node definitions, and Flow Runs get dedicated API modules instead of being added to legacy `jobs.py`.

Target route families include conceptually:

```text
/api/node-definitions
/api/flows
/api/flow-runs
/api/projects/.../flows
/api/projects/.../studio-bindings
```

Exact route names/versioning remain subject to API design.

### AI-006 — Flow Editor is a distinct workspace

**Decision: accepted and implemented.**

The Flow Editor is not embedded into `player.js` or modeled as another special-purpose Studio panel. It is a distinct workspace/view that integrates with Studio through stable Flow Outputs and explicit Apply. M6 adds the workspace to the existing application shell while keeping the Studio modules unaware of graph state.

### AI-007 — Flow Run monitoring uses one aggregate SSE stream per Run

**Decision: accepted and implemented.**

Clients monitor a FlowRun through one `/api/flow-runs/{run_id}/events` stream. Each versioned snapshot contains the FlowRun lifecycle, all NodeRuns, structured diagnostics, cache-hit state, and current Flow Output availability. The UI must not open one EventSource per node.

The endpoint shares the existing global SSE connection budget with Job/Queue streams and closes automatically when the Run reaches a terminal status.

### AI-008 — M5 dispatch is process-local and serial at FlowRun level

**Decision: accepted for local MVP.**

The M5 API dispatches at most one FlowRun at a time in-process while preserving node-level DAG parallelism inside that Run. This prevents separate Run schedulers from concurrently entering legacy process-global workers before resource claims are promoted to a scheduler shared across FlowRuns.

Persisted queued/running state is durable, but execution dispatch is not automatically resumed after a process restart. Recovery/resume semantics require an explicit later decision and must not silently mutate an immutable historical Run.

### AI-009 — M6 Flow Editor remains a vanilla ES Module island

**Decision: accepted and implemented for MVP.**

M6 does not introduce a frontend framework or graph-library dependency. The Flow Editor is isolated in `static/js/flowEditor.js` and `static/css/flow-editor.css`, owns only Flow-editor state, and communicates with the backend exclusively through the Flow APIs. Existing Studio/catalog/player modules do not import graph-domain concepts.

This is an MVP implementation choice, not a permanent prohibition on a graph library. A later replacement is allowed if graph interaction complexity warrants it, provided the persisted `FlowDocument`, backend-driven `NodeDefinition` contract, and API boundary remain unchanged.

### AI-010 — Invalid connections are editable state; backend diagnostics remain authoritative

**Decision: accepted and implemented.**

The M6 canvas does not reject a connection merely because the current technical or semantic types are incompatible. The binding is persisted in `FlowDocument`, then compiler/validator/capability diagnostics determine `compiled`, `valid`, and `runnable`. This preserves the requirement that incomplete/invalid graph states are saveable and prevents frontend compatibility logic from becoming a second source of truth.

The editor may add non-authoritative compatibility previews later, but they must be derived from backend node metadata and cannot replace backend validation.

### AI-011 — Apply to Studio is a Project application-layer operation

**Decision: accepted and implemented.**

The Flow runtime does not know about Projects or Studio. `ProjectService` owns the transition from an immutable terminal `FlowRun` to persisted Studio bindings. Apply is permitted only when the Run snapshot belongs to a Project Flow owned by the selected Project. Direct User/Built-in Flow Runs cannot create Project Studio bindings.

M7 creates a binding on first Apply and treats re-applying the same immutable Run as harmless. M8 extends the same application-layer boundary with reconciliation: applying a different Run updates the existing stable binding rather than creating a replacement track, while removed/unpublished outputs become detached.

### AI-012 — Project Flow identity is created at snapshot/copy time

**Decision: accepted and implemented.**

Copying a User Flow into a Project creates a new persisted Flow with `kind=project`, a new `flow_id`, and revision `1`. The Project ownership record stores `source_flow_id` and `source_flow_revision` only as provenance. Later edits to the source template do not affect the Project Flow.

### AI-013 — M7 projects managed Flow Outputs into one legacy Studio session

**Decision: accepted and implemented as a migration adapter.**

The backend Studio binding remains the source of truth. `static/js/flowStudio.js` projects all managed audio bindings for one `(project_id, flow_id)` into one existing StemDeck catalog/player session. Each Studio lane is keyed by stable `flow_output_id`, uses the binding media URL, and carries binding/semantic metadata. This avoids representing each output as a separate song while allowing the current Studio to consume backend-driven outputs.

The player/mixer now supports dynamic managed-Flow lane names without teaching the frontend about processing model implementations. This adapter is transitional: a future first-class Project/Studio session model may replace the catalog projection without changing Flow or binding persistence.

### AI-014 — Re-run reconciliation mutates binding target, never binding identity

**Decision: accepted and implemented for M8.**

For an existing `(project_id, flow_id, flow_output_id)` binding, Apply of a different immutable Run updates only the binding's `current_run_id`, artifact manifest and managed status. `binding_id` and `track_id` are stable. Re-applying the exact same Run is a no-op. Re-applying a previously detached output reuses the same binding/track identity and returns it to `managed`.

A published output that still exists in the new Run definition but has no artifact because the Run partially failed does **not** detach the existing binding; the previous media remains managed. Detach is reserved for outputs that no longer exist as published Flow Outputs in the applied Run snapshot.

### AI-015 — M8 temporal compatibility is origin-zero plus duration tolerance

**Decision: accepted and implemented for the whole-file MVP.**

The current MVP has a fixed timeline origin of `0`. Two audio artifacts are temporally compatible when both durations are known and their absolute duration difference is at most **50 ms**. The tolerance absorbs probe/container rounding without treating materially different renders as the same timeline. Unknown duration is conservatively incompatible.

Media replacement happens even when artifacts are temporally incompatible, but only a compatible replacement may restore time-based Studio state. Stable session/lane identity preserves non-temporal state such as mixer settings independently of this compatibility decision. In the current legacy Studio projection, an already-open compatible managed session restores playhead and loop bounds after media replacement.

### AI-016 — Retention policy and live references are orthogonal

**Decision: accepted and implemented for M9.**

`retain` expresses the base lifecycle of a produced node artifact; it does not override a stronger persisted live reference. A Studio binding protects the exact Flow Output artifact it references regardless of whether the binding is `managed` or `detached`. `retain=project` protects node materializations and derived Flow Outputs while the producing `flow_id` is owned by a Project. Once that ownership/reference disappears, the artifact may become GC-eligible subject to age policy.

Final Flow Outputs inherit lifecycle from their source node output for GC purposes. Flow Outputs sourced directly from Flow Inputs use `cache` as the default lifecycle. Publication (`publish`) remains independent: publication controls consumer visibility, not retention.

### AI-017 — Node cache admission is atomic and transient-safe

**Decision: accepted and implemented for M9.**

The current content-addressed cache stores a complete node result as one atomic entry. If any output port of a node is `retain=transient`, the entire node is excluded from cache lookup and cache publication. This conservative rule avoids retaining a transient port indirectly inside an otherwise cacheable multi-output node. A future per-port cache format may relax this rule without changing Flow semantics.

### AI-018 — M9 GC is artifact-level, not Run-directory deletion

**Decision: accepted and implemented.**

M9 may delete eligible node-output materializations and final Flow Output media/manifests, but it does not delete Run metadata, `run.json`, or `inputs/`. Run input snapshots are required by the accepted retry contract and therefore remain outside M9 garbage collection. Run-history compaction and retry-snapshot expiry, if desired later, require a separate explicit policy.

### AI-019 — Node output labels seed Flow Output presentation, never identity

**Decision: accepted and implemented for M9.5.**

`PortDefinition` may expose a backend-owned human-readable `label` for each node port while the port mapping key remains its stable technical identifier. When a Flow Output is created from a node output, the editor initializes `FlowOutput.label` from the source port label (falling back to a humanized port ID) and disambiguates duplicate labels for presentation only. Existing user-customized Flow Output labels are never overwritten when a source is connected or changed.

The aggregated `Flow Outputs` canvas node is an editor projection only. Connecting a node output directly to the block may create a new `FlowOutput`, but the persisted graph remains a map of independent Flow Outputs and node-input source references; no persisted edge list or multi-source Flow Output is introduced. `flow_output_id` continues to be the identity used by Studio bindings and rerun reconciliation.

## Persistence direction

### PD-001 — New Flow domain should not expand `registry.json` into a relational store

**Decision: accepted and implemented.**

Flow documents/revisions, Projects, FlowRuns, NodeRuns, Studio bindings, and artifact references introduce relational state and lifecycle transitions that exceed the intended role of the legacy JSON Job registry.

Preferred direction:

- SQLite for domain metadata and relationships;
- filesystem/object paths for large artifacts;
- YAML for import/export.

SQLite is implemented through Python's standard-library `sqlite3`; no new runtime dependency is required. The default database is `DATA_DIR/flow-engine.sqlite3` and may be overridden with `STEMDECK_FLOW_DB`. Large artifact bytes remain outside SQLite.


### PD-002 — Flow identity and revisions are server-owned

**Decision: accepted and implemented.**

- `flow_id` is a stable UUID-like identifier assigned on create unless an internal caller explicitly supplies one.
- `name` is mutable display metadata and is never identity.
- `schema_version` identifies the document schema and is independent of content revision.
- persisted `revision` starts at `1` and increments on every successful document update.
- update operations require `expected_revision`; stale writes fail with a revision conflict rather than silently overwriting a newer document.
- YAML import always creates a new persisted identity and resets revision to `1`; interchange metadata cannot overwrite an existing flow.

This optimistic-concurrency rule is intended to support autosave and future multi-window editing safely.

### PD-003 — Built-in flows are repository-owned, not mutable rows in the user Flow store

**Decision: accepted direction.**

The SQLite Flow store is the mutable domain store for User Flows and, later, Project Flows. Built-in Flows are application-owned templates and must be exposed as read-only definitions/templates. Duplicating a built-in creates an independent User Flow or Project Flow snapshot rather than modifying the built-in.

### PD-004 — FlowRun snapshots do not depend on mutable Flow rows

**Decision: accepted and implemented.**

A `FlowRun` stores the complete compiled `FlowDefinition` snapshot used to start it. The snapshot is not foreign-keyed to a mutable Flow row, so renaming, editing, or deleting the source Flow cannot change historical execution semantics. `flow_id` and `flow_revision` are retained only as provenance.

### PD-005 — Run state carries a monotonic event version

**Decision: accepted and implemented.**

Every persisted FlowRun/NodeRun lifecycle, progress, cancellation, or diagnostic mutation increments `FlowRun.version`. The SSE endpoint reuses the efficient change-detection pattern already proven by legacy Jobs without adopting the legacy Job event schema.

### PD-006 — Run-level diagnostics are persisted separately from NodeRun diagnostics

**Decision: accepted and implemented.**

Node execution failures attach diagnostics to the relevant NodeRun. Scheduler/orchestration/infrastructure failures that do not belong to a single node attach structured diagnostics to the FlowRun itself. Both forms are included in monitoring snapshots.

### PD-007 — Retry reuses the previous Run input snapshots

**Decision: accepted and implemented.**

The retry API does not require callers to re-submit external source paths. It resolves the immutable input artifacts copied into the previous Run directory and starts a new Run from those inputs plus the previous compiled FlowDefinition snapshot. This avoids retry behavior changing because an original source file was moved or replaced.

### PD-008 — Project metadata and media bytes have separate ownership

**Decision: accepted and implemented.**

Projects persist metadata and relationships in the Flow SQLite database: Project identity, Project Flow ownership/provenance, and Studio bindings. M7 does not copy FlowRun output bytes into a Project directory. A Studio binding stores the immutable artifact manifest plus the Run/output identity and references media served from the FlowRun output directory.

This means a persisted Studio binding is a live artifact reference. M9 garbage collection must consult these bindings before deleting Run/cache artifacts. Source/import media ownership remains outside the Project metadata model in the current whole-file MVP.

### PD-009 — Studio binding identity and state are first-class persistence

**Decision: accepted and implemented.**

`studio_bindings` has a unique logical key `(project_id, flow_id, flow_output_id)` and a stable `track_id`. The binding stores `current_run_id`, immutable artifact metadata, and explicit `managed|detached` state. Detaching does not delete the binding, its stable track identity, or its last artifact reference.

### PD-010 — Content cache eviction requires an explicit idle policy

**Decision: accepted and implemented for M9.**

`retain=cache` does not imply an arbitrary built-in TTL. Cache manifests now record `created_at` and `last_accessed_at`; materialization refreshes access time. The GC API prunes cache entries only when `cache_max_idle_seconds` is explicitly supplied. Legacy cache manifests without timestamps fall back to manifest mtime.

The destructive artifact-GC API is safe by default: `dry_run=true`, run-artifact `min_age_seconds=86400`, and cache pruning disabled unless an idle policy is provided.

## Reuse map

### Reuse with minimal conceptual change

- semantic type hierarchy and compatibility rules;
- DAG dependency extraction/topological planning;
- content-addressed cache key concept;
- provenance/runtime environment capture;
- local RoFormer runtime isolation;
- adapter implementations where they are not tied to orchestration lifecycle;
- existing PoC regression scenarios.

### Reuse behind new contracts

- current `ModelRegistry` -> future NodeDefinition registry;
- current `AdapterRegistry`;
- `ArtifactCache` -> cache component behind artifact lifecycle/storage abstractions;
- `FlowValidator` rule logic -> validator emitting structured diagnostics;
- StemDeck native adapters -> graph-native execution context rather than `Job` context.

### Refactor before product UI

- introduce `FlowDocument` and compiler;
- establish final `NodeDefinition` / `NodeInstance` terminology;
- introduce common diagnostics;
- avoid hard-coded audio-only assumptions in public artifact contracts;
- introduce FlowRun/NodeRun persistence and lifecycle;
- separate scheduling/orchestration from node execution;
- remove direct Flow adapter dependency on legacy `Job` lifecycle.

## Frontend baseline and Flow Editor implications

The existing web UI uses browser-native ES modules, direct DOM manipulation, and module-level state. There is no runtime framework, component library, or dedicated state-management library.

The current Studio is strongly stem-oriented: frontend code knows names such as vocals, guitar, bass, drums, and related variants. The Flow Editor must not inherit this model-specific coupling.

**FE-001 resolved for M6:** the editor is implemented as an isolated vanilla ES Module workspace with no new runtime framework or graph library. The isolation boundary is the important architectural constraint: `flowEditor.js` owns editor state and the Flow APIs own graph semantics. A graph library may still be introduced behind that boundary later if interaction complexity justifies it.

A full Studio rewrite is not a prerequisite for Flow Editor MVP.

## Current known gaps

Resolved by M0-M9:

- `FlowDocument` / compile boundary;
- final node terminology compatibility layer;
- common structured diagnostics;
- extensible technical artifact type IDs;
- Flow persistence and CRUD;
- YAML import/export boundary;
- persisted FlowRun/NodeRun snapshot/lifecycle;
- graph-native cancellation state;
- observable cache-hit state.

Still open:

- Built-in Flow discovery/catalog API is not implemented yet;
- no Flow Run monitoring UI yet (M5 backend API exists);
- no built-in Flow catalog UI/API yet.

## Implementation status

### M0 — complete

Implemented in the current working change set:

- `FlowDocument` as a permissive editor/persistence model;
- explicit `FlowCompiler` boundary;
- runtime `FlowDefinition` now carries distinct `flow_id`, `revision`, and `schema_version`; legacy YAML `version` is accepted as a temporary alias for `revision`;
- static `NodeDefinition` and runtime `NodeInstance` terminology introduced; `ModelDefinition` remains a compatibility alias during migration;
- runtime node instances use `definition_id`; legacy `model` input remains accepted during migration;
- editor layout state is represented separately and is stripped during compilation;
- common `Diagnostic` contract introduced for compiler, validator, and capability stages;
- validation diagnostics attach node/port/output/parameter locations where applicable;
- technical artifact type is an extensible string identifier rather than an audio-only literal;
- Flow Outputs carry explicit `publish`; legacy outputs default to published;
- YAML executable-flow loading passes through `FlowDocument -> FlowCompiler -> FlowDefinition`;
- the static registry is now canonically `NodeDefinitionRegistry`, with `ModelRegistry` retained as a compatibility alias.

### M1 — complete

Implemented:

- `GET /node-definitions` returns editor-safe node contracts plus current capability state;
- execution details such as adapter IDs, runtime paths, environment variables, and model implementation details stay backend-private;
- parameter contracts support optional labels/descriptions and backend-provided UI hints (`control`, `step`, `unit`, `placeholder`, `advanced`) without making those hints runtime semantics;
- `POST /flows/compile`;
- `POST /flows/validate`;
- `POST /flows/capabilities`;
- compile/validation endpoints remain usable without eagerly importing the legacy processing runtime;
- routes remain under the existing unversioned `/api` namespace while the Flow API is experimental. A public `/v1` boundary will only be introduced when the contract is intentionally frozen.

### M2 — complete

Implemented:

- SQLite-backed `FlowRepository`;
- invalid/incomplete `FlowDocument` round-trip persistence, including editor layout;
- stable `flow_id`;
- server-owned monotonically increasing `revision`;
- optimistic concurrency through `expected_revision`;
- create/list/get/update/delete;
- duplicate as independent User Flow;
- YAML import/export while keeping YAML outside primary editor persistence;
- import creates new identity and revision `1`;
- default database path `DATA_DIR/flow-engine.sqlite3`, configurable with `STEMDECK_FLOW_DB`.

### M3 — complete

Implemented domain/persistence foundations:

- persisted `FlowRun`;
- persisted `NodeRun`;
- immutable compiled FlowDefinition snapshot per run;
- explicit FlowRun and NodeRun state machines;
- creation of one NodeRun per execution node (Flow Input/Output are not NodeRuns);
- per-node progress/message state;
- observable cache-hit state;
- run history lookup;
- monotonic `FlowRun.version` suitable for future SSE;
- graph-native persisted cancellation request plus `RunCancellationToken`;
- retry ancestry field (`retry_of_run_id`) reserved in the run model.

M3 deliberately did not connect the PoC executor to this repository; that boundary is implemented in M4.

### M4 — complete

Implemented runtime orchestration:

- `FlowExecutor` can persist lifecycle directly through `FlowRunRepository`;
- `FlowRun` transitions from queued to running and to a terminal state from actual execution;
- `NodeRun` lifecycle/progress/cache-hit/diagnostics are updated by the executor;
- nodes in the same DAG stage are submitted concurrently;
- adapter-owned `ResourceClaim(key, max_concurrency)` constrains real concurrency independently of graph parallelism;
- the legacy process-global Demucs worker declares an exclusive resource and remains serialized;
- node failure no longer aborts independent sibling branches;
- descendants whose required artifacts are unavailable become `blocked`;
- successfully produced Flow Outputs remain materialized when another branch fails;
- a Run with usable outputs plus failed/blocked nodes settles as `partially_failed`; a Run with no usable Flow Outputs settles as `failed`;
- retry creates a new immutable `FlowRun` from the previous snapshot, records `retry_of_run_id`, and naturally reuses the content-addressed cache;
- cancellation is checked between scheduler stages and is propagated into active adapters through a graph-native cancellation callback;
- StemDeck-native adapters translate Flow cancellation into the existing legacy Job/process cancellation mechanism internally, without exposing `Job` to the Flow domain;
- execution diagnostics use the common structured diagnostic contract with `source=executor`;
- cache publication is protected against same-process concurrent writers.

Current resource scheduling is process-local. Cross-process/distributed resource arbitration is intentionally out of scope for the desktop/local MVP. Artifact-reference/GC semantics are implemented in M9.

### Verification status

The M0-M3 change set previously passed an isolated 11-test regression set covering:

- incomplete FlowDocument persistence and compilation diagnostics;
- editor-state stripping during compilation;
- valid vs runnable separation;
- SQLite draft round trips;
- server-owned revision increments and stale-write conflicts;
- Flow duplication;
- Flow CRUD API;
- YAML export/import with independent identity;
- immutable FlowRun snapshots;
- FlowRun/NodeRun lifecycle and progress;
- cache-hit observability;
- graph-native cancellation token behavior.

M4 additionally passes an isolated 3-test runtime regression set covering:

- sibling failure isolation, blocked descendants, and partial Flow Outputs;
- retry ancestry plus cache reuse/cache-hit persistence;
- cancellation during an active node, settling both NodeRun and FlowRun as cancelled.

M5 monitoring changes pass an isolated regression set together with M0-M4: **17 tests passed**. New coverage includes:

- pre-created queued Run visibility before asynchronous execution;
- output availability through persisted artifact manifests;
- retry from previous Run input snapshots with cache-hit observability;
- versioned FlowRun-level diagnostics;
- queued dispatch/pre-execution failure settling to `failed` with a Run diagnostic.

M6-M7 contract/domain changes passed the isolated M0-M7 regression set: **28 tests passed**. Coverage includes Flow Editor isolation, Project Flow snapshot independence, first Apply semantics, stable binding identity for same-Run Apply, manual detach, media-route contracts, dynamic managed Studio lanes, and isolation from legacy Job synchronization.

M8 raises the isolated Flow/domain/API/frontend-contract regression set to **32 tests passed**. New coverage includes:

- different-Run reconciliation updating media while preserving `binding_id` and `track_id`;
- compatible duration changes (<= 50 ms) and incompatible duration changes;
- removal/unpublication detaching rather than deleting a managed output;
- partial reruns retaining the previous managed media when a still-published artifact is unavailable;
- frontend contract for stable session projection and compatible playhead/loop restoration.

M9 raises the isolated Flow/domain/API/frontend-contract regression set to **41 tests passed**. New coverage includes:

- detached Studio bindings protecting their referenced Flow Output media from GC;
- `retain=project` protecting node and Flow Output materializations until Project ownership is removed;
- preservation of Run input snapshots during destructive artifact collection;
- transient output policy disabling atomic node cache admission;
- cache GC remaining disabled without an explicit idle-TTL policy;
- safe-by-default artifact GC API and editor retention controls.

The repository-wide pytest suite cannot currently start in the analysis environment because the existing global `tests/conftest.py` imports the legacy download pipeline, which requires `yt_dlp`; that package is not installed in this environment. This is an environment limitation, not a Flow test failure.

## Implementation milestones

### M0 — Domain consolidation

Goals:

- add `FlowDocument`;
- add compile step producing `FlowDefinition`;
- introduce shared structured diagnostics;
- establish compatibility path toward `NodeDefinition` / `NodeInstance` terminology;
- keep existing PoC YAML/runtime behavior working.

No product UI in this milestone.

### M1 — Backend-driven definition API

Goals:

- expose node definitions from the backend;
- expose compile, validation, and capability operations;
- prove the frontend can remain model-agnostic.

### M2 — Flow persistence

Goals:

- persist invalid/incomplete FlowDocuments;
- create/load/update/duplicate/delete flows;
- establish flow ID, revision, schema version, and name semantics;
- YAML import/export.

### M3 — Persistent runtime model

Goals:

- introduce `FlowRun` and `NodeRun`;
- immutable run snapshots;
- graph-native progress/cancellation state;
- run repository/history.

### M4 — Scheduler and resilient execution — complete

Delivered:

- independent branch continuation and blocked-descendant semantics;
- partial Flow Outputs;
- adapter-declared resource constraints with concurrent DAG stages;
- new-Run cache-aware retry with ancestry;
- graph-native cancellation propagation including a private legacy-process bridge.

### M5 — Monitoring API — complete

Delivered:

- start/list/query/cancel/retry Flow Runs through a dedicated `/api/flow-runs` router;
- Run is persisted in `queued` state before background dispatch, so the `202` response always returns a monitorable identity;
- single aggregate SSE stream per Run using `FlowRun.version`;
- snapshots include NodeRun lifecycle/progress, cache hits, node diagnostics, Run diagnostics, immutable definition snapshot, and current Flow Output availability;
- cancellation requests are observable immediately; queued work is cancelled before start when possible;
- retry reuses copied input snapshots from the previous Run and records ancestry;
- default run/cache directories are `DATA_DIR/flow-runs` and `DATA_DIR/flow-cache`, overrideable with `STEMDECK_FLOW_RUNS_DIR` and `STEMDECK_FLOW_CACHE_DIR`;
- FlowRun-level dispatch is serial for the local MVP while node-level DAG parallelism remains enabled.

### M6 — Flow Editor MVP — complete

Delivered:

- a dedicated `Flows` workspace integrated into the existing application rail without coupling graph state into Studio modules;
- backend-driven node palette from `GET /api/node-definitions`, including category and capability state;
- drag/drop and double-click node placement;
- Flow Input and Flow Output virtual canvas nodes that never become execution nodes;
- connection editing where node inputs / Flow Output sources remain the only persisted source of truth and SVG edges are derived;
- backend-schema-driven parameter controls with optional UI hints;
- Flow/input/output/node property editing;
- structured compiler/validator/capability diagnostics with `compiled`, `valid`, and `runnable` badges;
- invalid and incomplete draft persistence through the existing Flow CRUD API;
- editor node layout plus canvas scroll viewport stored only in `FlowDocument.editor`;
- optimistic-revision save handling and explicit conflict reporting;
- duplicate/delete User Flow operations from the editor.

M6 intentionally does not add Flow Run monitoring UI or Studio Apply; those remain separate milestones/contracts.

### M7 — Project and Studio binding — complete

Delivered:

- first-class `Project` persistence in the Flow SQLite database;
- Project Flow ownership records with source-flow provenance;
- copying a User Flow creates an independent `kind=project` snapshot with new `flow_id` and revision `1`;
- dedicated `/api/projects` router for Project CRUD, Project Flow copy/list, Studio binding list, explicit Apply, and manual detach;
- Apply accepts only terminal Runs belonging to a Project Flow in the selected Project;
- only published, available audio Flow Outputs create Studio bindings in the audio-only MVP;
- stable unique binding identity `(project_id, flow_id, flow_output_id)` plus stable `track_id`;
- explicit `managed` / `detached` binding state;
- detached bindings retain track identity and last artifact reference;
- published FlowRun media is served through a guarded `/api/flow-runs/{run_id}/outputs/{output_id}/media` endpoint;
- Flow workspace can create/select Projects, copy User Flows into a Project, browse Project Flows, inspect/detach bindings, and explicitly Apply the latest terminal Run;
- a frontend `flowStudio.js` adapter materializes managed bindings as one Studio session with dynamic lanes keyed by stable `flow_output_id`, while the backend binding remains authoritative;
- legacy Job tracks continue to use the existing fixed stem behavior; dynamic lane support is activated only for managed Flow sessions;
- managed Flow Studio sessions are excluded from legacy Job availability reconciliation and Job trash/favorite/delete API synchronization.

### M8 — Re-run reconciliation — complete

Delivered:

- Apply of the same immutable Run remains idempotent;
- Apply of a different Run updates the existing stable Studio binding instead of creating a new binding/track;
- `binding_id` and `track_id` remain stable while `current_run_id` and artifact metadata move to the newly applied Run;
- previously detached outputs can be reattached without changing track identity;
- outputs removed from the new Run definition or changed to `publish=false` become `detached` rather than being deleted;
- an output that is still published but missing from a partial/failed Run keeps its previous managed media and is reported as `artifact_unavailable`;
- temporal compatibility for the whole-file MVP is origin `0` plus a maximum duration delta of 50 ms;
- incompatible media is still applied, but time-based UI state is not restored;
- the managed Studio projection keeps the same session ID and stable `flow_output_id` lane IDs, preserving mixer state across compatible and incompatible reruns;
- when the same managed session is already open and all replaced outputs are temporally compatible, playhead and loop bounds are captured and restored across reload;
- Apply API responses now expose `updated`, `detached`, and per-update `temporal_compatibility` metadata.

### M9 — Artifact lifecycle and GC — complete

Delivered:

- reference-aware `ArtifactLifecycleService` over terminal Flow Runs;
- persisted Studio bindings, including detached bindings, protect referenced Run output media;
- `retain=project` protects node materializations and derived Flow Outputs while the producing Flow remains Project-owned;
- Flow Outputs inherit retention from their source node output; direct Flow Input outputs default to cache lifecycle;
- Run `inputs/`, input manifests, `run.json`, and Run database history are explicitly outside M9 collection so snapshot-based retry/provenance remain intact;
- transient ports disable node-atomic cache lookup/publication, preventing indirect retention of transient bytes;
- cache manifests track creation/access timestamps and support optional idle-TTL collection;
- `POST /api/flow-artifacts/gc` supports dry-run preview, run-artifact minimum age, and optional cache idle policy;
- destructive GC defaults are conservative (`dry_run=true`, one-day minimum age, no cache pruning without explicit TTL);
- the Flow Editor exposes per-node-output `transient|cache|project` retention controls while keeping Flow Output `publish` separate.

## Open decisions register

| ID | Decision | Status | Needed by |
| --- | --- | --- | --- |
| OD-001 | SQLite as metadata persistence for new Flow/Project/Run domain | Accepted/implemented | M2 |
| OD-002 | Flow Output publication uses `publish: bool`, defaulting true for legacy imports | Accepted | M0 |
| OD-003 | Technical artifact type is an opaque string identifier; concrete payload classes remain runtime-specific | Accepted | M0 |
| OD-004 | M6 uses an isolated vanilla ES Module editor with no graph library; replacement remains allowed behind the same domain/API boundary | Accepted/implemented | M6 |
| OD-005 | Scheduler resource model: adapter-owned keyed resource claims with per-resource concurrency limits; process-local for MVP | Accepted | M4 |
| OD-006 | Exact temporal-compatibility rule for replacing media in a managed Studio track | Accepted/implemented: origin 0, known durations within 50 ms | M8 |
| OD-007 | Project metadata owns relationships only; FlowRun media bytes remain filesystem artifacts referenced by bindings | Accepted/implemented | M7 |
| OD-008 | Retry creates a new immutable FlowRun and records `retry_of_run_id`; cache reuse remains observable per NodeRun | Accepted | M3/M4 |
| OD-009 | Historical FlowDocument revision retention policy (optimistic editing concurrency is already accepted) | Open | post-M6 |
| OD-010 | Keep Flow APIs under existing unversioned `/api` while experimental; introduce a versioned boundary only when intentionally frozen | Accepted | M1 |
| OD-011 | Restart recovery semantics for persisted queued/running FlowRuns | Open | post-M6 |
| OD-012 | Promote resource arbitration from per-Run scheduler to a process-shared scheduler if concurrent FlowRuns are enabled | Open | post-M6 |
| OD-013 | M9 garbage collection is artifact-level and must preserve Run input snapshots / `run.json` for retry and provenance | Accepted/implemented | M9 |
| OD-014 | Content cache has no implicit TTL; eviction requires explicit `cache_max_idle_seconds` | Accepted/implemented | M9 |
| OD-015 | Node-atomic cache is disabled when any node output is `transient` | Accepted/implemented | M9 |

## Change log

### 2026-11-13

- Created architecture source-of-truth from repository gap analysis.
- Recorded consolidated functional decisions AP-001 through AP-029.
- Recorded integration directions AI-001 through AI-006.
- Recorded persistence direction PD-001 as proposed pending implementation decision.
- Established M0-M9 milestone sequence.
- Started M0 implementation: added FlowDocument/compiler boundary, structured diagnostics, distinct flow identity/revision/schema fields, and target NodeDefinition/NodeInstance terminology with compatibility aliases.
- Routed executable YAML loading through the compiler while keeping YAML as an interchange format.
- Completed M0 contract consolidation: generic technical artifact type IDs, explicit Flow Output `publish`, canonical NodeDefinitionRegistry, and compatibility aliases for PoC terminology.
- Started M1 read-only/analysis API surface with node-definition discovery plus compile/validate/capability endpoints.
- Completed M2 SQLite Flow persistence with server-owned revisions and YAML import/export.
- Completed M3 persisted FlowRun/NodeRun lifecycle, immutable snapshots, progress/cache-hit state, and graph-native cancellation requests.
- Completed M4 resilient execution: resource-aware concurrent DAG stages, failure isolation, partial outputs, cache-aware retry, runtime lifecycle persistence, and cancellation bridging for legacy StemDeck processors.
- Completed M5 monitoring API: persisted-before-dispatch Run start, list/query/cancel/retry endpoints, aggregate versioned SSE snapshots, Run-level diagnostics, output availability, and snapshot-based retry inputs.
- Completed M6 Flow Editor MVP: isolated vanilla workspace, backend-driven palette/properties, virtual Flow I/O nodes, derived graph edges, editable invalid drafts, structured diagnostics, layout persistence, and revision-safe saves.
- Completed M7 Project/Studio binding: Project persistence, independent Project Flow snapshots, stable managed/detached Studio bindings, explicit Apply, published-output media serving, dynamic managed Studio lanes, and isolation from legacy Job synchronization.

### 2026-11-16

- Completed M8 rerun reconciliation: stable binding/track identity across reruns, media-source replacement, 50 ms whole-file temporal compatibility, compatible playhead/loop restoration, detach-on-output-removal semantics, and preservation of previous media for still-published outputs missing from partial reruns.
- Completed M9 artifact lifecycle/GC: reference-aware Studio/Project protection, transient-safe cache admission, project retention, retry-input preservation, explicit cache idle policy, safe-by-default GC API, and editor retention controls.

### M9.1 — Flow Editor interaction hardening — complete

Delivered:

- fixed node dragging by removing the invalid `setPointerCapture()` dependency after a selection-triggered canvas re-render;
- drag gestures now keep the existing node DOM stable and use window-level pointer move/up/cancel listeners, so dragging continues even when the pointer leaves the node;
- selection during drag updates the inspector without re-rendering the canvas mid-gesture;
- new virtual Flow Inputs and Flow Outputs are placed inside the currently visible canvas viewport instead of fixed far-away coordinates;
- `+ Output` consumes an already pending source connection when one exists, while still allowing an intentionally disconnected draft output;
- a disconnected Flow Output continues to produce the structured compiler diagnostic `missing_flow_output_source`; this is expected draft validation, not an editor exception.

### 2026-11-17

- M9.1 Flow Editor hardening: fixed broken node dragging caused by DOM replacement before pointer capture, made new virtual I/O placement viewport-aware, and improved Flow Output creation by consuming a pending source when available.

### M9.2 — Aggregated virtual Flow I/O nodes — complete

Delivered:

- the canvas now renders all `FlowDocument.inputs` inside one virtual **Flow Inputs** card;
- the canvas now renders all `FlowDocument.outputs` inside one virtual **Flow Outputs** card;
- each row/port remains an independent `FlowInput` or `FlowOutput` in the persisted document and keeps its stable ID;
- a grouped Flow Output is not a multi-source artifact: one visual port still maps to exactly one `FlowOutput.source` and therefore one published artifact/binding identity;
- per-port selection remains available for editing artifact type, semantic type, source, `publish`, and deletion in the inspector;
- connection gestures are dot-driven while row clicks select the corresponding entity, reducing accidental rewiring during inspection;
- grouped I/O cards have one editor-layout position each (`virtual:inputs`, `virtual:outputs`); legacy per-I/O layout positions are reused as a soft visual migration when loading older documents;
- runtime compilation, validation, Flow Output publication, Studio binding, retry, and artifact lifecycle contracts are unchanged.

Decision:

- aggregation is a **canvas projection only**. It must never change the one-FlowOutput/one-artifact identity required by `flow_id + flow_output_id` Studio bindings.

### 2026-11-17

- M9.2 Flow Editor UX: aggregated virtual Flow Inputs/Outputs into one card per boundary while preserving independent document entities, per-port inspector editing, derived edges, and stable Flow Output identities.


### M9.3 — Flow input binding and Run monitoring UI — complete

Delivered:

- the Flow workspace exposes a **Run** action for saved, runnable User Flows and Project Flows;
- concrete execution inputs are editor-session bindings and are not persisted into `FlowDocument`;
- `GET /api/flow-input-assets` exposes opaque library assets rather than filesystem paths;
- a library asset ID has stable server-side meaning (`job:<job_id>:full`) but the browser never receives the resolved local path;
- local-import Jobs reuse their retained source file when available; remote-import Jobs whose source was intentionally discarded are materialized as a cached full mix reconstructed from immutable base stems;
- `POST /api/flow-runs` accepts `input_assets` in addition to the pre-existing path-based `inputs` contract, with the two forms mutually exclusive;
- the Flow Run panel renders one asset selector for each `FlowInput`, starts the immutable `FlowRun`, monitors aggregate SSE snapshots, shows NodeRun lifecycle/progress/cache hits, and exposes cancel/retry;
- Run input selection does not alter graph validity, revision, layout, or Studio binding identity; only the resulting Run snapshot captures the concrete audio;
- Apply to Studio remains a separate action and remains limited to Project Flow Runs.

Decision:

- library-to-Flow integration uses opaque backend-resolved asset references. Passing browser-visible server filesystem paths is explicitly rejected as an application contract.

### 2026-11-17

- M9.3 Flow execution UX: added library-backed Flow Input selection, opaque asset resolution, Run start/monitor/cancel/retry controls, and SSE NodeRun monitoring without persisting concrete input bindings into FlowDocument.

### M9.4 — Studio projection UX and waveform parity — complete

Delivered:

- `FlowOutput` now has an optional human-facing `label`; `flow_output_id` remains the stable technical identity used by Run snapshots, Studio bindings, rerun reconciliation, and artifact lifecycle;
- the Flow Editor exposes the output label independently from the immutable/stable output ID, and the aggregated Flow Outputs card renders the label when present;
- Studio lanes use the stable binding `track_id` as their internal mixer/playback identity and render the Flow Output label as the visible lane name, so visual renames do not invalidate backend binding identity;
- Flow-managed waveform rendering now uses dynamic lane order rather than the legacy fixed StemDeck stem list, fixing empty waveform lanes for arbitrary Flow Output names;
- managed and Flow-augmented sessions intentionally bypass legacy `peaks.json` assumptions when necessary and fall back to decode-derived waveform data for dynamic Flow lanes;
- Apply to Studio now asks for a user-editable session name, defaulting to the selected input library track title rather than the Project/Flow name;
- Apply offers two projection destinations: **Create a new Studio session** or **Apply outputs to an existing library track**;
- applying to an existing input/library track is implemented as a persisted frontend Studio overlay: the legacy Job remains authoritative for its original media/metadata, while managed Flow lanes are merged into the Studio projection after every Job refresh;
- Flow overlay lanes retain their stable backend binding identities and can be reconciled by later reruns without converting the input Job into a FlowRun or altering the Flow Engine's DAW-agnostic runtime.

Decision:

- output identity and output presentation are separate contracts. Renaming a visible Studio lane must not rename `flow_output_id` or invalidate `flow_id + flow_output_id` bindings.
- "Apply to input track" is a Studio/catalog projection concern, never a mutation of the legacy processing Job or the FlowRun snapshot.

### 2026-11-17

- M9.4 Studio UX: fixed dynamic Flow waveforms, added editable Flow Output labels, and added Apply-time session naming plus new-session vs existing-input-track projection.

### M9.5 — Backend-owned port labels and auto-created Flow Outputs — complete

Delivered:

- `PortDefinition` now carries an optional human-facing `label` that is exposed through the existing backend-driven NodeDefinition API;
- built-in separation nodes provide explicit output labels such as `Vocals`, `Lead Vocals`, `Backing Vocals`, `Lead Guitar`, and `Rhythm Guitar`;
- execution-node output rows display the backend-owned label while retaining the technical port key for graph references;
- creating a Flow Output from a selected source automatically initializes `FlowOutput.label` from the source port label, with a humanized port-ID fallback;
- duplicate display labels are disambiguated (`Lead Guitar`, `Lead Guitar 2`, ...) without changing technical IDs;
- connecting a previously blank/manual Flow Output initializes its label only when it has no user-provided label; custom labels are never overwritten;
- the aggregated `Flow Outputs` node remains visible when empty and can be clicked while a source connection is pending to create and connect a Flow Output in one operation;
- no persisted graph edge list or multi-source Flow Output was introduced.

Decision:

- default naming is part of the backend NodeDefinition contract, while final Flow Output labels remain user-editable presentation metadata. Stable port IDs and `flow_output_id` remain the only identities used by runtime and Studio reconciliation.

### 2026-11-17

- M9.5 Flow UX: added backend-owned output-port labels and one-step source-to-Flow-Outputs creation so common flows require little or no manual track renaming.


### M9.6 — Direct audio-file Flow inputs — complete

Delivered:

- each Flow Input in the Run panel can now be satisfied either by an existing StemDeck library asset or by directly choosing an audio file from the local machine;
- direct uploads are run-input assets, not implicit library imports: choosing a file does not create or mutate a legacy StemDeck Job;
- `POST /api/flow-input-assets/upload` accepts the same practical audio/container extensions used by StemDeck imports and returns an opaque `upload:<id>:audio` reference plus display metadata;
- uploaded Flow input bytes are stored server-side under the Flow input upload cache; browser-facing payloads never expose the resolved filesystem path;
- uploaded asset references are strictly validated as server-generated hexadecimal IDs before filesystem lookup;
- the Run button is disabled while any Flow Input upload is still in progress;
- once uploaded, the asset is inserted into the same editor-session input asset list and bound through the existing `input_assets` Run contract, so execution, immutable input snapshots, retry, cache, and provenance continue to use the existing FlowRun path;
- direct upload bindings remain editor-session state and are not persisted into `FlowDocument`.

Decision:

- **Import into StemDeck Library** and **use a local file as this Run's Flow Input** are intentionally different user actions. Direct Flow upload must not create a library Job merely to satisfy the Flow Engine input contract.

### 2026-11-17

- M9.6 Flow execution UX: added direct local audio-file binding alongside library-track selection while preserving opaque backend-resolved input assets and immutable Run snapshots.


### M9.7 — Canvas interaction hardening and Project-free product workflow — complete

Delivered:

- the aggregated **Flow Outputs** virtual node is selectable as a whole, independently from its individual FlowOutput rows;
- the Flow Outputs inspector can delete all outputs in one action or remove the visual boundary block entirely; `+ Output` recreates the boundary when needed;
- editor boundary visibility is stored only in `FlowDocument.editor` and has no runtime meaning;
- pending connection gestures are explicitly cancellable via **Cancel**, `Escape`, clicking the empty canvas, or clicking the same source handle again;
- connected node-input/FlowOutput ports expose an explicit disconnect control rather than requiring manual source-field editing;
- connection handles use button semantics, larger hover/focus affordances, crosshair cursors, and connection mode highlights valid input targets plus the Flow Outputs auto-create target;
- the user-facing **Project** picker, Project Flow list, and copy-to-project workflow have been removed from the Flow Editor;
- `POST /api/flow-studio/apply` and `GET /api/flow-studio/bindings` expose Studio reconciliation directly for ordinary reusable Flows;
- existing Project/Studio tables are retained behind a reserved internal compatibility scope so M7-M9.6 databases, stable binding IDs, rerun reconciliation, and GC live references continue to work without migration;
- StemDeck's standard import bar now has a `Standard extraction / Flow` selector plus **Edit** shortcut;
- selecting a one-input Flow and a local audio file runs that Flow from the normal import surface, then projects the published outputs into a new Studio session using the imported filename as the default title;
- multi-input Flows deliberately remain in the dedicated Flow Run panel so every input binding is explicit; URL-to-Flow import is not yet enabled in this shortcut.

Superseding decision:

- **AI-011 / AI-012 and AP-024 are no longer product-facing requirements.** A Project is not required to execute or apply a Flow. The canonical user model is now `Flow -> concrete input(s) -> FlowRun -> Apply to Studio`. The old Project persistence remains an implementation compatibility detail only and must not leak back into the editor or standard import UX.
- stable Studio reconciliation continues to use `flow_id + flow_output_id`; removing Project from the UX does not weaken binding identity, immutable Run snapshots, detach behavior, or reference-aware GC.

### 2026-11-17

- M9.7: hardened graph connection affordances and block-level Output operations, removed Projects from the normal Flow UX, added direct Flow/Studio Apply, and integrated reusable one-input Flows into StemDeck's standard local-file import bar.

### M9.7.1 — Flow Output recreation hotfix — complete

Delivered:

- fixed `+ Output` after deleting the Flow Outputs block: the DOM click `MouseEvent` is no longer passed through as a Flow source reference;
- `addOutput()` now defensively ignores non-string `sourceOverride` values, so future event-binding mistakes cannot reach `sourceDefaultLabel()` as structured objects;
- added a regression contract covering `delete output block -> + Output`.

### 2026-11-17

- M9.7.1: fixed Flow Output block recreation crash (`source.startsWith is not a function`).

### M9.8 — Semantic color system — complete

Delivered:

- the legacy StemDeck stem palette is now exposed through a shared `semanticColor()` resolver rather than being keyed only by legacy track names;
- Flow colors are derived from `semantic_type` and are never persisted in `FlowDocument`, `FlowDefinition`, `FlowRun`, or Studio binding state;
- semantic subtypes use explicit variants where useful (`lead_vocals`, `backing_vocals`, `lead_guitar`, `rhythm_guitar`) and otherwise fall back through their semantic parent hierarchy;
- artifact-family colors provide the final fallback for types that do not yet have a dedicated semantic color;
- Flow Editor node ports, Flow Inputs/Outputs and graph edges use the resolved semantic color of the source artifact;
- Flow Studio lanes, mixer accents, overview waveforms, mini-waveforms and multitrack waveforms resolve dynamic Flow lane colors from `semanticType` instead of from technical `flow_output_id`/track IDs;
- legacy standard StemDeck stems retain their existing visual palette through the backward-compatible `STEM_COLORS` mapping.

Decision:

- **Visual color is derived presentation metadata.** Resolution order is `specific semantic type -> semantic parent -> legacy semantic/name compatibility -> artifact family -> neutral fallback`. User labels and runtime IDs must never determine a lane color.
- semantic coloring is shared across the standard separator and Flow surfaces so a `vocals` or `guitar` artifact has the same visual meaning regardless of how it was produced.

### 2026-11-17

- M9.8: unified legacy StemDeck stem colors and Flow semantic types across graph ports/edges and Studio lane/waveform rendering.


## M9.8.1 — Flow Library materialization hotfix

Flow-managed Studio sessions use a stable catalog id so reruns can reconcile the same session. A session with that same id may already exist in StemDeck Trash from an earlier test or user action. `addTrackToLibrary()` previously counted Trash as an existing placement, which allowed the session to open in Studio while remaining invisible from the active Library.

For `flowManaged` sessions only, a fresh materialization now removes the same stable id from Trash and places it into an active library folder (normally Unsorted) when no active placement exists. Normal legacy Job imports preserve the existing rule that adding another import must not silently restore a trashed Job.

This hotfix intentionally does not yet redefine Flow application identity. A later architecture pass should distinguish reusable `flow_id` from a concrete Studio application/session context so the same Flow can coexist against multiple source tracks without sharing one catalog session identity.

## M9.8.2 — Offline-safe bootstrap fonts

- Removed Google Fonts (`fonts.googleapis.com` / `fonts.gstatic.com`) from desktop and mobile bootstrap HTML.
- StemDeck now relies on the existing local/system font stacks (`-apple-system`, `system-ui`, `SF Mono`, Menlo, etc.) when Inter / JetBrains Mono are not installed.
- External/cosmetic resources must never be required for application bootstrap.
- `boot-diagnostics.js` now supports resources explicitly marked with `data-boot-optional`; failures for those resources are logged as warnings and do not trigger the fatal startup banner.
- Added a frontend contract test preventing Google Fonts from being reintroduced as a boot dependency.
